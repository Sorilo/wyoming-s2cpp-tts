"""Phase 9B SpeechScheduler — explicit FIFO speech synthesis scheduler.

Extracted from SingleWorkerSynthesisQueue with identical lock/future
mechanics, exception types, and FIFO guarantees.  Owns admission,
activation, cancellation, and snapshot state.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Awaitable, Callable

from app.speech.models import SpeechRequest, SpeechState, ScheduledSpeech
from app.observability import obs_log


class QueueFullError(RuntimeError):
    """Raised when the synthesis queue is at capacity."""


class QueueTimeoutError(asyncio.TimeoutError):
    """Raised when a waiting request exceeds the queue wait timeout."""


class SpeechScheduler:
    """Bounded FIFO scheduler gate with explicit ownership handoff.

    Uses a :class:`collections.deque` of waiter futures for
    deterministic FIFO activation.  Ownership is transferred by
    resolving the next waiter's future.
    """

    worker_count = 1

    def __init__(self, max_size: int, wait_timeout_sec: float = 30) -> None:
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        self.max_size = max_size
        self.wait_timeout_sec = wait_timeout_sec
        self._lock = asyncio.Lock()
        self._active_entry: ScheduledSpeech | None = None
        self._waiters: deque[tuple[str, asyncio.Future, str, ScheduledSpeech]] = deque()
        self._active_task: asyncio.Task | None = None
        # Counter fields for backward compatibility
        self._depth = 0
        self._pending = 0
        # -- Phase 9C drain/shutdown --
        self._drain_event = asyncio.Event()
        self._active_complete_event = asyncio.Event()
        self._active_complete_event.set()  # initially idle
        # Deterministic quiescence tracking for shutdown
        self._drain_remaining: int = 0
        self._drain_done = asyncio.Event()
        self._drain_done.set()  # nothing pending initially
        self._quiescence = asyncio.Event()
        self._quiescence.set()  # idle initially

    # ── Public snapshot ────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """Return an immutable plaintext-safe summary.

        Never exposes plaintext text, request objects, or task/future objects.
        """
        active = self._active_entry
        return {
            "active_synthesis_id": (
                active.request.synthesis_id if active else None
            ),
            "active_connection_id": (
                active.request.connection_id if active else None
            ),
            "depth": self._depth,
            "pending": self._pending,
            "max_size": self.max_size,
            "waiting_count": len(self._waiters),
            "admission_latency_ms": (
                int((active.started_monotonic - active.admitted_monotonic) * 1000)
                if active and active.started_monotonic is not None
                else None
            ),
            "terminal_reason": active.terminal_reason if active else None,
        }

    # ── Admission and activation ───────────────────────────────────────

    async def run(
        self,
        request: SpeechRequest,
        operation: Callable[[], Awaitable[None]],
    ) -> None:
        """Admit *request* and execute *operation* when activated.

        Raises QueueFullError if at capacity, QueueTimeoutError on wait
        timeout, or propagates operation exceptions.
        """
        synthesis_id = request.synthesis_id
        connection_id = request.connection_id

        future: asyncio.Future | None = None
        acquired = False
        was_quiescent = False

        async with self._lock:
            if self._drain_event.is_set():
                raise QueueFullError(
                    "Scheduler is draining, not accepting new requests"
                )
            if self._depth >= self.max_size:
                raise QueueFullError(
                    f"Queue full (depth={self._depth}, max={self.max_size})"
                )

            was_quiescent = (self._depth == 0)
            self._depth += 1
            self._pending += 1
            if was_quiescent:
                self._quiescence.clear()

            entry = ScheduledSpeech(
                request=request,
                state=SpeechState.CREATED,
                admitted_monotonic=time.monotonic(),
            )
            obs_log("queue_admitted",
                    synthesis_id=synthesis_id,
                    connection_id=connection_id,
                    queue_depth=self._depth,
                    max_queue_size=self.max_size)

            if self._active_entry is None:
                # First worker: become active immediately
                entry.state = SpeechState.ACTIVE
                entry.started_monotonic = time.monotonic()
                self._active_entry = entry
                self._active_task = asyncio.current_task()
                self._active_complete_event.clear()
                acquired = True
            else:
                # Enqueue as waiter
                entry.state = SpeechState.WAITING
                future = asyncio.get_running_loop().create_future()
                self._waiters.append((synthesis_id, future, connection_id, entry))

        if not acquired:
            try:
                await asyncio.wait_for(future, timeout=self.wait_timeout_sec)
                # Successfully resumed — update active state
                async with self._lock:
                    if self._active_entry is entry:
                        # Entry already activated by previous active's handoff
                        self._active_task = asyncio.current_task()
                    else:
                        # Fallback: activate now (should not happen with atomic handoff)
                        entry.state = SpeechState.ACTIVE
                        entry.started_monotonic = time.monotonic()
                        self._active_entry = entry
                        self._active_task = asyncio.current_task()
            except asyncio.TimeoutError:
                async with self._lock:
                    self._pending -= 1
                    self._depth -= 1
                    # Remove from waiters
                    for j, (_sid, fut, _cid, _entry) in enumerate(self._waiters):
                        if fut is future:
                            del self._waiters[j]
                            break
                    entry.state = SpeechState.TIMED_OUT
                    entry.completed_monotonic = time.monotonic()
                    entry.terminal_reason = "queue_wait_timeout"
                    # Signal drain-done if this was a drained waiter
                    _signal_drain_done(self, entry)
                    if self._depth == 0:
                        self._quiescence.set()
                obs_log("queue_wait_timeout", synthesis_id=synthesis_id,
                        connection_id=connection_id,
                        queue_depth=self._depth,
                        wait_timeout_sec=self.wait_timeout_sec)
                raise QueueTimeoutError(
                    f"Queue wait timeout after {self.wait_timeout_sec}s"
                )
            except asyncio.CancelledError:
                async with self._lock:
                    self._pending -= 1
                    self._depth -= 1
                    for j, (_sid, fut, _cid, _entry) in enumerate(self._waiters):
                        if fut is future:
                            del self._waiters[j]
                            break
                    entry.state = SpeechState.CANCELLED
                    entry.completed_monotonic = time.monotonic()
                    if entry.terminal_reason is None:
                        entry.terminal_reason = "cancelled_while_waiting"
                    # Signal drain-done if this was a drained waiter
                    _signal_drain_done(self, entry)
                    if self._depth == 0:
                        self._quiescence.set()
                obs_log("queue_cancelled", synthesis_id=synthesis_id,
                        connection_id=connection_id,
                        reason="cancelled_while_waiting")
                raise

        obs_log("queue_started", synthesis_id=synthesis_id,
                connection_id=connection_id, queue_depth=self._depth)

        try:
            await operation()
            # On success
            if entry.state == SpeechState.ACTIVE:
                entry.state = SpeechState.COMPLETED
                entry.terminal_reason = "completed"
        except asyncio.CancelledError:
            if entry.state == SpeechState.ACTIVE:
                entry.state = SpeechState.CANCELLED
                entry.terminal_reason = "cancelled_while_active"
            raise
        except asyncio.TimeoutError:
            if entry.state == SpeechState.ACTIVE:
                entry.state = SpeechState.TIMED_OUT
                entry.terminal_reason = "synthesis_timeout"
            raise
        except Exception:
            if entry.state == SpeechState.ACTIVE:
                entry.state = SpeechState.FAILED
                entry.terminal_reason = "operation_failed"
            raise
        finally:
            async with self._lock:
                self._depth -= 1
                self._pending -= 1
                entry.completed_monotonic = time.monotonic()

                obs_log("queue_completed", synthesis_id=synthesis_id,
                        connection_id=connection_id,
                        terminal_reason=entry.terminal_reason,
                        queue_depth=self._depth)

                was_active = self._active_entry is entry
                if was_active:
                    self._active_entry = None
                    self._active_task = None

                # Activate next waiter if any (atomic handoff)
                if self._waiters:
                    nsid, nfut, _nc, nentry = self._waiters.popleft()
                    nentry.state = SpeechState.ACTIVE
                    nentry.started_monotonic = time.monotonic()
                    self._active_entry = nentry
                    self._active_task = None  # waiter task sets this on resume
                    nfut.set_result(None)
                elif was_active:
                    # Only set active_complete_event when the active
                    # entry finished and no waiters remain.  Drained
                    # waiters don't reach this finally block, so they
                    # cannot spuriously set it.
                    self._active_complete_event.set()

                if self._depth == 0:
                    self._quiescence.set()

    # ── Phase 9C drain / shutdown ─────────────────────────────────────

    async def drain(self) -> int:
        """Cancel all queued/waiting requests.  Idempotent — repeated
        calls after the first are no-ops and return 0.

        Returns the number of waiter futures cancelled on *this* call.
        Active synthesis is left running.  Future cancels are issued
        **outside** the scheduler lock to avoid deadlocks.
        """
        if self._drain_event.is_set():
            return 0
        self._drain_event.set()

        futures_to_cancel: list[asyncio.Future] = []
        async with self._lock:
            for _, fut, _, entry in self._waiters:
                futures_to_cancel.append(fut)
                if entry.terminal_reason is None:
                    entry.terminal_reason = "drain_cancelled"
            self._waiters.clear()

        # Set up deterministic drain-done tracking BEFORE cancelling
        # so that waiter handlers can find the counter ready.
        n = len(futures_to_cancel)
        if n > 0:
            self._drain_remaining = n
            self._drain_done.clear()

        for fut in futures_to_cancel:
            fut.cancel()

        return n

    async def shutdown(
        self, grace_timeout_sec: float
    ) -> tuple[bool, int]:
        """Drain waiters and wait for active synthesis with bounded grace.

        Returns ``(clean, waiters_cancelled)`` where *clean* is ``True``
        when the active synthesis completes within *grace_timeout_sec*
        (or there is no active work) and ``False`` when the active task
        was forcibly cancelled after the grace period expired.

        Does **not** return until the scheduler reaches deterministic
        quiescence — all counters (depth, pending) are zero and no
        waiter or active cleanup is still in-flight.

        Idempotent — repeated calls are safe and return quickly.
        """
        waiters_cancelled = await self.drain()

        # ── Wait for all drained-waiter CancelledError handlers ──────
        await self._drain_done.wait()

        # ── Snapshot active state under the lock ─────────────────────
        async with self._lock:
            has_active = self._active_entry is not None

        if not has_active:
            return True, waiters_cancelled

        clean = True
        try:
            await asyncio.wait_for(
                self._active_complete_event.wait(),
                timeout=grace_timeout_sec,
            )
        except asyncio.TimeoutError:
            # Capture the active task under the lock, cancel outside
            task_to_cancel: asyncio.Task | None = None
            async with self._lock:
                if self._active_task is not None:
                    task_to_cancel = self._active_task
            if task_to_cancel is not None:
                task_to_cancel.cancel()
                # Await the cancelled task so its finally block
                # (which decrements depth/pending) completes before
                # we return.
                try:
                    await task_to_cancel
                except asyncio.CancelledError:
                    pass
            clean = False

        # ── Final quiescence gate ────────────────────────────────────
        # Wait until depth reaches 0 — this covers the active's
        # finally block after forced cancellation as well as any
        # straggler cleanup.
        await self._quiescence.wait()

        return clean, waiters_cancelled

    # ── Cancellation ────────────────────────────────────────────────────

    async def cancel_connection(self, connection_id: str) -> int:
        """Cancel all waiting requests for *connection_id*. Returns count."""
        cancelled = 0
        async with self._lock:
            keep: list[tuple[str, asyncio.Future, str, ScheduledSpeech]] = []
            for sid, fut, cid, _entry in self._waiters:
                if cid == connection_id:
                    fut.cancel()
                    cancelled += 1
                else:
                    keep.append((sid, fut, cid, _entry))
            self._waiters = deque(keep)
        return cancelled

    def cancel_synthesis(self, synthesis_id: str) -> bool:
        """Cancel the active synthesis if its ID matches. Returns True if cancelled."""
        if (
            self._active_entry is not None
            and self._active_entry.request.synthesis_id == synthesis_id
            and self._active_task is not None
        ):
            self._active_task.cancel()
            return True
        return False

    def cancel_active_for_connection(self, connection_id: str) -> bool:
        """Cancel the active synthesis if it belongs to *connection_id*."""
        if (
            self._active_entry is not None
            and self._active_entry.request.connection_id == connection_id
            and self._active_task is not None
        ):
            self._active_task.cancel()
            return True
        return False

    async def cancel_new_request(self, connection_id: str) -> None:
        """Cancel active and waiting work for *connection_id* (CANCEL_ON_NEW_REQUEST).

        Public compatibility method — cancels the active synthesis if it
        belongs to this connection and cancels all waiting requests.
        """
        snap = self.snapshot()
        active_sid = snap.get("active_synthesis_id")
        if active_sid:
            self.cancel_synthesis(active_sid)
        await self.cancel_connection(connection_id)


# ── Module-level helper ──────────────────────────────────────────────

def _signal_drain_done(sched: SpeechScheduler, entry: ScheduledSpeech) -> None:
    """Decrement drain counter when a drained waiter finishes cleanup.

    Must be called under ``sched._lock``.  Only acts when *entry* was
    explicitly marked by :meth:`drain`.
    """
    if entry.terminal_reason == "drain_cancelled" and sched._drain_remaining > 0:
        sched._drain_remaining -= 1
        if sched._drain_remaining == 0:
            sched._drain_done.set()

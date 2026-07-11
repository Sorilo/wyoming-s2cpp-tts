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

        async with self._lock:
            if self._depth >= self.max_size:
                raise QueueFullError(
                    f"Queue full (depth={self._depth}, max={self.max_size})"
                )

            self._depth += 1
            self._pending += 1

            entry = ScheduledSpeech(
                request=request,
                state=SpeechState.CREATED,
                admitted_monotonic=time.monotonic(),
            )

            if self._active_entry is None:
                # First worker: become active immediately
                entry.state = SpeechState.ACTIVE
                entry.started_monotonic = time.monotonic()
                self._active_entry = entry
                self._active_task = asyncio.current_task()
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
                    entry.terminal_reason = "cancelled_while_waiting"
                raise

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

                if self._active_entry is entry:
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
                # else: no waiters, scheduler idle

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

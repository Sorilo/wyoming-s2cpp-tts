"""Phase 9C Slice 3: ServiceCoordinator — lifecycle owner for Wyoming TTS.

Single service-lifecycle owner that coordinates:
- ServiceLifecycle state machine
- Wyoming listener startup/closure
- Connection tracking/closure
- Scheduler drain/shutdown with one non-resetting grace deadline
- Backend stream/resource cleanup
- SIGTERM/SIGINT signal handling
- Final completion

Wire this through actual production startup/handler boundaries —
not tautological unit-only tests.
"""

from __future__ import annotations

import asyncio
import signal
from typing import Any

from app.config import Settings
from app.lifecycle import LifecycleState, ServiceLifecycle
from app.speech.scheduler import SpeechScheduler
from app.wyoming_server import (
    FakeTtsConfig,
    RunningFakeTtsServer,
    S2ClientFactory,
    S2Client,
    start_fake_tts_server,
    parse_tcp_uri,
)


class ServiceCoordinator:
    """Single lifecycle owner for the Wyoming TTS service.

    Owns:
    - ServiceLifecycle state machine (STARTING/RUNNING/DRAINING/...)
    - Wyoming listener startup (via start_fake_tts_server)
    - Wyoming listener closure (via server.stop())
    - Scheduler drain/shutdown via SpeechScheduler.shutdown()
    - SIGTERM/SIGINT dispatch via handle_signal()
    - One non-resetting grace deadline (first shutdown wins)

    Usage::

        coordinator = ServiceCoordinator(settings)
        await coordinator.start()
        # … service is running …
        await coordinator.shutdown()  # or handle_signal(signal.SIGTERM)
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.lifecycle = ServiceLifecycle(
            shutdown_grace_timeout_sec=settings.shutdown_grace_timeout_sec,
        )
        self.server: RunningFakeTtsServer | None = None
        self.scheduler: SpeechScheduler | None = None
        self._shutdown_started = False
        self._shutdown_complete = asyncio.Event()
        self._shutdown_failure: Exception | None = None
        self._signal_received: int | None = None
        self._active_handlers: set[Any] = set()
        self._signal_tasks: list[asyncio.Task[Any]] = []

    # ── Properties ──────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        """True when the service is RUNNING and accepting work."""
        return self.lifecycle.ready

    @property
    def state(self) -> LifecycleState:
        return self.lifecycle.state

    # ── Connection tracking ──────────────────────────────────────────────

    def register_handler(self, handler: Any) -> None:
        self._active_handlers.add(handler)

    def unregister_handler(self, handler: Any) -> None:
        self._active_handlers.discard(handler)

    @property
    def active_connection_count(self) -> int:
        return len(self._active_handlers)

    # ── Startup ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the Wyoming listener and transition to RUNNING.

        On startup failure (port in use, bad URI, etc.), transitions to
        FAILED and re-raises so the process can exit non-zero.
        """
        try:
            host, port = parse_tcp_uri(self.settings.wyoming_uri)
            config = FakeTtsConfig.from_settings(self.settings)

            self.server = await start_fake_tts_server(
                host=host,
                port=port,
                config=config,
                max_queue_size=self.settings.max_queue_size,
                settings=self.settings,
                s2_client_factory=S2Client.from_settings,
                coordinator=self,
            )

            # Extract the scheduler from the server's handler factory path.
            # The scheduler is created inside start_fake_tts_server; we
            # store a reference here so shutdown can drain it.
            self.scheduler = self.server.scheduler

            self.lifecycle.transition_to_running()

            print(
                f"Wyoming TTS server listening on tcp://{host}:{self.server.port} "
                f"with backend={self.settings.tts_backend}"
            )
        except Exception:
            # Partial-start cleanup: close server if created, transition FAILED
            self.lifecycle.transition_to_failed()
            if self.server is not None:
                try:
                    await self.server.stop()
                except Exception:
                    pass
            raise

    # ── Drain initiation (public, for handler coordination) ─────────

    def start_draining(self) -> bool:
        """Initiate drain via the lifecycle machine.

        Returns True if this call started draining, False if already
        draining/stopped/failed.  The handler checks this to reject
        new synthesis requests deterministically.
        """
        return self.lifecycle.start_draining()

    # ── Shutdown ────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Execute graceful shutdown.  Idempotent — repeated calls
        after the first are no-ops.  If the first shutdown failed,
        subsequent callers receive the same exception.

        Sequence:
        1. Initiate drain (rejects new synthesis)
        2. Stop Wyoming listener (no new connections)
        3. Transition to STOPPING
        4. Drain scheduler (cancel queued, wait active with grace)
        5. Close remaining handler connections
        6. Transition to STOPPED (or FAILED on error)
        """
        if self._shutdown_started:
            await self._shutdown_complete.wait()
            if self._shutdown_failure is not None:
                raise self._shutdown_failure
            return
        self._shutdown_started = True

        if not self.lifecycle.start_draining():
            # Already terminal — nothing to do
            self._shutdown_complete.set()
            return

        try:
            # 1. Stop Wyoming listener — no new connections accepted
            if self.server is not None:
                await self._stop_listener()

            # 2. Transition to STOPPING
            self.lifecycle.transition_to_stopping()

            # 3. Drain scheduler: cancel queued, wait active with grace
            # Active synthesis is allowed to complete within the grace
            # period. Closing connections BEFORE this step would
            # prematurely kill active synthesis.
            if self.scheduler is not None:
                clean, cancelled = await self.scheduler.shutdown(
                    self.lifecycle.shutdown_grace_timeout_sec,
                )

            # 4. Close remaining handler connections AFTER scheduler drain
            # Only handlers still open after active synthesis completes
            # (or is force-cancelled) are closed here.
            await self._close_all_handlers()

            # 5. Success
            self.lifecycle.transition_to_stopped()
        except Exception as exc:
            self.lifecycle.transition_to_failed()
            self._shutdown_failure = exc
            # Attempt to close handlers even on failure, but don't let
            # cleanup errors mask the original shutdown failure.
            try:
                await self._close_all_handlers()
            except Exception:
                pass
            raise
        finally:
            self._shutdown_complete.set()

    async def _stop_listener(self) -> None:
        """Stop the Wyoming TCP listener (close listening socket).

        Active handlers may still be running; they'll be closed
        after the scheduler drain via _close_all_handlers.
        """
        if self.server is not None:
            await self.server.stop()

    async def _close_all_handlers(self) -> None:
        """Close all tracked handler transports deterministically.

        Each handler's writer is closed via its disconnect path.
        Handlers that are already disconnected will have already
        unregistered themselves.  Errors during individual handler
        closes are collected and logged but do not prevent closing
        the remaining handlers.
        """
        handlers = list(self._active_handlers)
        errors: list[tuple[Any, Exception]] = []
        for handler in handlers:
            try:
                await handler.disconnect()
            except Exception as exc:
                errors.append((handler, exc))
        if errors:
            names = ", ".join(
                f"{type(h).__name__}: {type(e).__name__}"
                for h, e in errors
            )
            raise RuntimeError(
                f"{len(errors)} handler(s) failed to disconnect: {names}"
            )

    # ── Signal handling ─────────────────────────────────────────────

    def install_signal_handlers(self, loop: Any) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(
                    sig, self._make_signal_callback(sig, loop)
                )
            except NotImplementedError:
                pass

    def _make_signal_callback(self, sig: int, loop: Any):
        def _cb() -> None:
            task = asyncio.ensure_future(self.handle_signal(sig), loop=loop)
            self._signal_tasks = [
                t for t in self._signal_tasks if not t.done()
            ]
            self._signal_tasks.append(task)

        return _cb

    async def remove_signal_handlers(self, loop: Any) -> None:
        """Remove OS signal handlers and await/cancel owned tasks.

        Cancels any pending signal tasks and retrieves their
        exceptions (expected CancelledError during shutdown).
        Must be called as ``await coordinator.remove_signal_handlers(loop)``.
        """
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, ValueError):
                pass
        # Cancel any running signal tasks
        for task in self._signal_tasks:
            if not task.done():
                task.cancel()
        # Await cancellation and retrieve exceptions
        if self._signal_tasks:
            await asyncio.gather(
                *self._signal_tasks, return_exceptions=True
            )
        self._signal_tasks.clear()

    async def handle_signal(self, signum: int) -> None:
        """Handle an OS signal (SIGTERM or SIGINT).

        Idempotent — only the first signal starts shutdown.  Subsequent
        signals are logged but otherwise ignored.
        """
        if not self._shutdown_started:
            self._signal_received = signum
            await self.shutdown()
        else:
            # Already shutting down — wait for completion
            await self._shutdown_complete.wait()

    # ── Wait for completion ─────────────────────────────────────────

    async def wait_for_shutdown(self) -> None:
        """Block until shutdown completes (or is already complete)."""
        await self._shutdown_complete.wait()

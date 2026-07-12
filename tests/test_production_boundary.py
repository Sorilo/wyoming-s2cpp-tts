"""Phase 9C Slice 3: Production-boundary tests for corrected lifecycle.

Tests real connection/signal/resource ownership via ServiceCoordinator:
- Handler registration/unregistration through actual server connection
- Repeat shutdown failure propagation
- Signal task exception retrieval and cancellation
- Active synthesis allowed grace before connection close
- Handler disconnect idempotency
- Registry empty after full shutdown
"""

from __future__ import annotations

import asyncio
import time

import pytest

from wyoming.audio import AudioStart, AudioChunk, AudioStop
from wyoming.tts import Synthesize

from app.coordinator import ServiceCoordinator
from app.lifecycle import LifecycleState
from app.config import Settings
from app.wyoming_server import FakeTtsEventHandler, FakeTtsConfig
from app.speech import SpeechScheduler


def _settings(**overrides):
    kwargs = {
        "tts_backend": "fake",
        "shutdown_grace_timeout_sec": 5.0,
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


class _StubWriter:
    """Minimal StreamWriter stub for handler testing."""

    def __init__(self):
        self.close_calls = 0

    def get_extra_info(self, name, default=None):
        if name == "peername":
            return ("127.0.0.1", 12345)
        return default

    def close(self):
        self.close_calls += 1

    def write(self, data):
        pass

    async def drain(self):
        pass

    def is_closing(self):
        return False

    async def wait_closed(self):
        pass


def test_handler_registers_with_coordinator_on_init():
    """FakeTtsEventHandler calls coordinator.register_handler on init."""

    async def run():
        settings = Settings(tts_backend="fake")
        coordinator = ServiceCoordinator(settings)

        reader = asyncio.StreamReader()
        writer = _StubWriter()

        assert coordinator.active_connection_count == 0

        handler = FakeTtsEventHandler(
            reader, writer, FakeTtsConfig(),
            SpeechScheduler(max_size=3), settings,
            lambda _: None,
            coordinator=coordinator,
        )

        assert coordinator.active_connection_count == 1
        assert handler in coordinator._active_handlers

        await coordinator.shutdown()

    asyncio.run(run())


def test_handler_unregisters_on_disconnect():
    """FakeTtsEventHandler unregisters from coordinator on disconnect."""

    async def run():
        settings = Settings(tts_backend="fake")
        coordinator = ServiceCoordinator(settings)

        reader = asyncio.StreamReader()
        writer = _StubWriter()

        handler = FakeTtsEventHandler(
            reader, writer, FakeTtsConfig(),
            SpeechScheduler(max_size=3), settings,
            lambda _: None,
            coordinator=coordinator,
        )

        assert coordinator.active_connection_count == 1

        await handler.disconnect()

        assert coordinator.active_connection_count == 0
        assert handler not in coordinator._active_handlers

        # Second disconnect is idempotent
        await handler.disconnect()
        assert coordinator.active_connection_count == 0

        await coordinator.shutdown()

    asyncio.run(run())


def test_handler_disconnect_idempotent():
    """Handler.disconnect() is safe to call multiple times."""

    async def run():
        settings = Settings(tts_backend="fake")
        coordinator = ServiceCoordinator(settings)

        reader = asyncio.StreamReader()
        writer = _StubWriter()

        handler = FakeTtsEventHandler(
            reader, writer, FakeTtsConfig(),
            SpeechScheduler(max_size=3), settings,
            lambda _: None,
            coordinator=coordinator,
        )

        await handler.disconnect()
        await handler.disconnect()
        await handler.disconnect()

        assert coordinator.active_connection_count == 0
        await coordinator.shutdown()

    asyncio.run(run())


def test_registry_empty_after_shutdown():
    """After full shutdown through real server, active_handlers registry is empty."""

    async def run():
        settings = Settings(tts_backend="fake", shutdown_grace_timeout_sec=5.0,
                           wyoming_uri="tcp://127.0.0.1:0")
        coordinator = ServiceCoordinator(settings)
        await coordinator.start()

        from wyoming.client import AsyncTcpClient
        from wyoming.info import Describe
        port = coordinator.server.port
        async with AsyncTcpClient("127.0.0.1", port) as client:
            await client.write_event(Describe().event())
            event = await asyncio.wait_for(client.read_event(), timeout=2.0)
            assert event is not None

        await coordinator.shutdown()

        assert coordinator.active_connection_count == 0
        assert coordinator.lifecycle.state == LifecycleState.STOPPED

    asyncio.run(run())


def test_repeat_shutdown_propagates_failure():
    """If first shutdown fails, subsequent callers get the same exception."""

    async def run():
        settings = Settings(tts_backend="fake", shutdown_grace_timeout_sec=1.0,
                           wyoming_uri="tcp://127.0.0.1:0")
        coordinator = ServiceCoordinator(settings)
        await coordinator.start()

        # Inject a failure into shutdown by making _close_all_handlers raise
        original_close = coordinator._close_all_handlers

        async def _failing_close():
            raise RuntimeError("simulated handler close failure")

        coordinator._close_all_handlers = _failing_close

        # First shutdown will fail
        with pytest.raises(RuntimeError, match="simulated"):
            await coordinator.shutdown()

        assert coordinator._shutdown_failure is not None
        assert isinstance(coordinator._shutdown_failure, RuntimeError)

        # Restore close so second shutdown can succeed
        coordinator._close_all_handlers = original_close

        # Second shutdown re-raises the same failure
        with pytest.raises(RuntimeError, match="simulated"):
            await coordinator.shutdown()

    asyncio.run(run())


def test_repeat_shutdown_no_deadlock_after_success():
    """After successful shutdown, repeated calls return immediately."""

    async def run():
        c = ServiceCoordinator(_settings())
        await c.start()
        await c.shutdown()
        assert c.lifecycle.state == LifecycleState.STOPPED

        t0 = time.monotonic()
        await c.shutdown()
        await c.shutdown()
        await c.shutdown()
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"Repeated shutdown calls took {elapsed:.1f}s"
        assert c.lifecycle.state == LifecycleState.STOPPED

    asyncio.run(run())


def test_signal_tasks_retrieve_exceptions():
    """remove_signal_handlers awaits and retrieves signal task exceptions."""

    async def run():
        c = ServiceCoordinator(_settings())
        loop = asyncio.get_running_loop()

        c.install_signal_handlers(loop)
        assert len(c._signal_tasks) == 0

        async def _dummy():
            return 42

        t = asyncio.ensure_future(_dummy(), loop=loop)
        c._signal_tasks.append(t)
        await t

        await c.remove_signal_handlers(loop)
        assert len(c._signal_tasks) == 0

    asyncio.run(run())


def test_signal_tasks_cancelled_on_removal():
    """remove_signal_handlers cancels pending signal tasks."""

    async def run():
        c = ServiceCoordinator(_settings())
        loop = asyncio.get_running_loop()

        c.install_signal_handlers(loop)

        started = asyncio.Event()
        blocker = asyncio.Event()

        async def _blocker():
            started.set()
            try:
                await blocker.wait()
            except asyncio.CancelledError:
                pass

        t = asyncio.ensure_future(_blocker(), loop=loop)
        c._signal_tasks.append(t)
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert not t.done()

        await c.remove_signal_handlers(loop)
        assert t.done()
        assert len(c._signal_tasks) == 0

    asyncio.run(run())


def test_closing_connections_after_scheduler_grace():
    """Tracked connections close only after scheduler grace completes."""

    async def run():
        coordinator = ServiceCoordinator(_settings())
        coordinator.lifecycle.transition_to_running()
        scheduler_started = asyncio.Event()
        release_scheduler = asyncio.Event()
        handler_closed = asyncio.Event()

        class _Server:
            async def stop(self):
                return None

        class _Scheduler:
            async def shutdown(self, grace_timeout_sec):
                assert not handler_closed.is_set()
                scheduler_started.set()
                await release_scheduler.wait()
                assert not handler_closed.is_set()
                return True, 0

        class _Handler:
            async def disconnect(self):
                handler_closed.set()
                coordinator.unregister_handler(self)

        coordinator.server = _Server()
        coordinator.scheduler = _Scheduler()
        handler = _Handler()
        coordinator.register_handler(handler)

        shutdown_task = asyncio.create_task(coordinator.shutdown())
        await asyncio.wait_for(scheduler_started.wait(), timeout=1.0)
        assert not handler_closed.is_set()
        release_scheduler.set()
        await asyncio.wait_for(shutdown_task, timeout=1.0)

        assert handler_closed.is_set()
        assert coordinator.state == LifecycleState.STOPPED
        assert coordinator.active_connection_count == 0

    asyncio.run(run())

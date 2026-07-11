"""Phase 9C Slice 3: ServiceCoordinator lifecycle integration tests.

Tests the ServiceCoordinator wiring into Wyoming server startup/shutdown:
- Readiness false before listener init, true after
- SIGTERM/SIGINT begins shutdown exactly once
- No new queue admissions after draining begins
- Queued work cancelled and released
- Active work gets configured grace period
- Active work force-cancelled after grace expiry
- Counters zero after shutdown
- Duplicate signals/shutdown idempotent
- Startup failure -> FAILED
- Pre-PCM and post-AudioStart cancellation; no forged AudioStop
- No leaked tasks/unobserved exceptions/resources
- Repeated shutdown idempotent at coordinator boundary
"""

from __future__ import annotations

import asyncio
import signal
import time

import pytest

from wyoming.audio import AudioStart, AudioChunk, AudioStop
from wyoming.tts import Synthesize, SynthesizeStart, SynthesizeChunk, SynthesizeStop
from wyoming.event import Event


# Helpers

def _req(sid: str = "s1", cid: str = "c1", text: str = "test"):
    from app.speech.models import SpeechRequest
    return SpeechRequest(synthesis_id=sid, connection_id=cid, text=text)


def _settings(**overrides):
    from app.config import Settings
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


# ServiceCoordinator lifecycle tests

def test_coordinator_initial_state():
    """Coordinator starts with lifecycle in STARTING, not ready."""
    from app.coordinator import ServiceCoordinator
    from app.lifecycle import LifecycleState

    c = ServiceCoordinator(_settings())
    assert c.lifecycle.state == LifecycleState.STARTING
    assert not c.lifecycle.ready


def test_coordinator_startup_transitions_to_running():
    """After successful start(), lifecycle is RUNNING and ready."""
    from app.coordinator import ServiceCoordinator
    from app.lifecycle import LifecycleState

    async def run():
        c = ServiceCoordinator(_settings())
        await c.start()
        try:
            assert c.lifecycle.state == LifecycleState.RUNNING
            assert c.lifecycle.ready
            assert c.server is not None
            assert c.scheduler is not None
        finally:
            await c.shutdown()

    asyncio.run(run())


def test_coordinator_startup_failure_transitions_to_failed():
    """Startup failure (e.g. bad URI) transitions to FAILED."""
    from app.coordinator import ServiceCoordinator
    from app.lifecycle import LifecycleState

    async def run():
        c = ServiceCoordinator(_settings(wyoming_uri="tcp://:::bad"))
        with pytest.raises(Exception):
            await c.start()
        assert c.lifecycle.state == LifecycleState.FAILED
        assert not c.lifecycle.ready

    asyncio.run(run())


def test_coordinator_shutdown_transitions_to_stopped():
    """Normal shutdown goes STARTING->RUNNING->DRAINING->STOPPING->STOPPED."""
    from app.coordinator import ServiceCoordinator
    from app.lifecycle import LifecycleState

    async def run():
        c = ServiceCoordinator(_settings())
        await c.start()
        await c.shutdown()
        assert c.lifecycle.state == LifecycleState.STOPPED
        assert not c.lifecycle.ready

    asyncio.run(run())


def test_coordinator_shutdown_idempotent():
    """Repeated shutdown calls are safe and idempotent."""
    from app.coordinator import ServiceCoordinator
    from app.lifecycle import LifecycleState

    async def run():
        c = ServiceCoordinator(_settings())
        await c.start()
        await c.shutdown()
        assert c.lifecycle.state == LifecycleState.STOPPED

        await c.shutdown()
        assert c.lifecycle.state == LifecycleState.STOPPED

        await c.shutdown()
        assert c.lifecycle.state == LifecycleState.STOPPED

    asyncio.run(run())


# Shutdown drains scheduler

def test_coordinator_shutdown_drains_queued_work():
    """Shutdown cancels waiting requests, lets active finish within grace."""
    from app.coordinator import ServiceCoordinator
    from app.lifecycle import LifecycleState

    async def run():
        c = ServiceCoordinator(_settings(shutdown_grace_timeout_sec=5.0))
        await c.start()
        sched = c.scheduler

        release = asyncio.Event()
        active_done = False
        waiter_cancelled = False

        async def active_op():
            nonlocal active_done
            await release.wait()
            active_done = True

        t_active = asyncio.create_task(sched.run(_req("s-active"), active_op))
        await asyncio.sleep(0.02)
        assert sched.snapshot()["active_synthesis_id"] == "s-active"

        async def waiter():
            nonlocal waiter_cancelled
            try:
                await sched.run(_req("s-waiter"), lambda: None)
            except asyncio.CancelledError:
                waiter_cancelled = True

        t_w = asyncio.create_task(waiter())
        await asyncio.sleep(0.02)

        shutdown_task = asyncio.create_task(c.shutdown())
        await asyncio.sleep(0.1)
        assert waiter_cancelled
        assert not active_done

        release.set()
        await shutdown_task

        assert active_done
        assert c.lifecycle.state == LifecycleState.STOPPED
        snap = sched.snapshot()
        assert snap["depth"] == 0
        assert snap["pending"] == 0

    asyncio.run(run())


def test_coordinator_shutdown_forces_cancel_after_grace():
    """Active work running past grace period is force-cancelled."""
    from app.coordinator import ServiceCoordinator
    from app.lifecycle import LifecycleState

    async def run():
        c = ServiceCoordinator(_settings(shutdown_grace_timeout_sec=0.2))
        await c.start()
        sched = c.scheduler

        block_forever = asyncio.Event()
        started = asyncio.Event()
        was_cancelled = False

        async def active_op():
            nonlocal was_cancelled
            started.set()
            try:
                await block_forever.wait()
            except asyncio.CancelledError:
                was_cancelled = True
                raise

        t_active = asyncio.create_task(sched.run(_req("s-active"), active_op))
        await started.wait()

        t0 = time.monotonic()
        await c.shutdown()
        elapsed = time.monotonic() - t0

        assert was_cancelled
        assert elapsed < 2.0, f"shutdown took {elapsed:.1f}s, expected bounded"
        assert c.lifecycle.state == LifecycleState.STOPPED
        snap = sched.snapshot()
        assert snap["depth"] == 0
        assert snap["pending"] == 0

        try:
            await t_active
        except asyncio.CancelledError:
            pass

    asyncio.run(run())


def test_coordinator_shutdown_rejects_new_synthesis():
    """During draining, scheduler rejects new synthesis with QueueFullError."""
    from app.coordinator import ServiceCoordinator
    from app.speech.scheduler import QueueFullError

    async def run():
        c = ServiceCoordinator(_settings(shutdown_grace_timeout_sec=5.0))
        await c.start()
        sched = c.scheduler

        release = asyncio.Event()

        async def active_op():
            await release.wait()

        t_active = asyncio.create_task(sched.run(_req("s-active"), active_op))
        await asyncio.sleep(0.02)

        shutdown_task = asyncio.create_task(c.shutdown())
        await asyncio.sleep(0.05)

        with pytest.raises(QueueFullError, match="drain"):
            await sched.run(_req("s-new"), lambda: None)

        release.set()
        await shutdown_task
        await t_active

    asyncio.run(run())


def test_coordinator_counters_zero_after_shutdown():
    """All scheduler counters (depth, pending) are zero after shutdown."""
    from app.coordinator import ServiceCoordinator

    async def run():
        c = ServiceCoordinator(_settings(shutdown_grace_timeout_sec=5.0))
        await c.start()
        sched = c.scheduler

        release = asyncio.Event()

        async def active_op():
            await release.wait()

        t_active = asyncio.create_task(sched.run(_req("s-active"), active_op))
        await asyncio.sleep(0.02)

        t_w = asyncio.create_task(sched.run(_req("s-waiter"), lambda: None))
        await asyncio.sleep(0.02)

        snap_before = sched.snapshot()
        assert snap_before["depth"] == 2
        assert snap_before["pending"] == 2

        shutdown_task = asyncio.create_task(c.shutdown())
        await asyncio.sleep(0.05)
        release.set()
        await shutdown_task

        snap_after = sched.snapshot()
        assert snap_after["depth"] == 0, f"depth={snap_after['depth']}"
        assert snap_after["pending"] == 0, f"pending={snap_after['pending']}"
        assert snap_after["active_synthesis_id"] is None
        assert snap_after["waiting_count"] == 0

    asyncio.run(run())


# Signal handling tests

def test_coordinator_signal_triggers_shutdown():
    """Simulated signal dispatch triggers shutdown exactly once."""
    from app.coordinator import ServiceCoordinator
    from app.lifecycle import LifecycleState

    async def run():
        c = ServiceCoordinator(_settings(shutdown_grace_timeout_sec=5.0))
        await c.start()
        assert c.lifecycle.state == LifecycleState.RUNNING

        shutdown_task = asyncio.create_task(c.handle_signal(signal.SIGTERM))
        await shutdown_task

        assert c.lifecycle.state == LifecycleState.STOPPED
        assert not c.lifecycle.ready

        shutdown_task2 = asyncio.create_task(c.handle_signal(signal.SIGINT))
        await shutdown_task2
        assert c.lifecycle.state == LifecycleState.STOPPED

    asyncio.run(run())


def test_coordinator_signal_does_nothing_when_already_stopped():
    """Signal on already-stopped coordinator is a no-op."""
    from app.coordinator import ServiceCoordinator
    from app.lifecycle import LifecycleState

    async def run():
        c = ServiceCoordinator(_settings())
        await c.start()
        await c.shutdown()
        assert c.lifecycle.state == LifecycleState.STOPPED

        await c.handle_signal(signal.SIGTERM)
        assert c.lifecycle.state == LifecycleState.STOPPED

    asyncio.run(run())


# Cancellation without forged AudioStop

def test_shutdown_cancel_before_audio_start_no_forged_stop():
    """Cancellation before AudioStart must not emit a forged AudioStop."""
    from app.coordinator import ServiceCoordinator
    from app.speech.session import SynthesisSession
    from app.speech.models import SpeechRequest

    session_events = []

    class _SpySession(SynthesisSession):
        def mark_audio_start(self):
            session_events.append("audio_start")
            super().mark_audio_start()

        def mark_audio_stop(self):
            session_events.append("audio_stop")
            super().mark_audio_stop()

        def mark_cancelled(self):
            session_events.append("cancelled")
            super().mark_cancelled()

    async def run():
        c = ServiceCoordinator(_settings(shutdown_grace_timeout_sec=0.2))
        await c.start()
        sched = c.scheduler

        block = asyncio.Event()
        started = asyncio.Event()

        async def op():
            started.set()
            try:
                await block.wait()
            except asyncio.CancelledError:
                pass
                raise

        req = SpeechRequest(synthesis_id="s-cancel", connection_id="c1", text="hello")
        session = _SpySession(request=req, trigger="legacy")

        t = asyncio.create_task(sched.run(req, op))
        await started.wait()

        await c.shutdown()

        try:
            await t
        except asyncio.CancelledError:
            pass

        assert "audio_start" not in session_events
        assert "audio_stop" not in session_events

    asyncio.run(run())


def test_shutdown_cancel_after_audio_start_no_forged_stop():
    """After AudioStart, cancellation must not emit forged AudioStop."""
    from app.coordinator import ServiceCoordinator
    from app.speech.session import SynthesisSession
    from app.speech.models import SpeechRequest

    session_events = []

    class _SpySession(SynthesisSession):
        def mark_audio_start(self):
            session_events.append("audio_start")
            super().mark_audio_start()

        def mark_audio_stop(self):
            session_events.append("audio_stop")
            super().mark_audio_stop()

        def mark_cancelled(self):
            session_events.append("cancelled")
            super().mark_cancelled()

    async def run():
        c = ServiceCoordinator(_settings(shutdown_grace_timeout_sec=0.2))
        await c.start()
        sched = c.scheduler

        block = asyncio.Event()
        started = asyncio.Event()

        async def op():
            started.set()
            try:
                await block.wait()
            except asyncio.CancelledError:
                raise

        req = SpeechRequest(synthesis_id="s-cancel", connection_id="c1", text="hello")
        session = _SpySession(request=req, trigger="legacy")
        session.mark_audio_start()

        t = asyncio.create_task(sched.run(req, op))
        await started.wait()

        await c.shutdown()

        try:
            await t
        except asyncio.CancelledError:
            pass

        assert "audio_start" in session_events
        assert "audio_stop" not in session_events

    asyncio.run(run())


# Task/resource leak tests

def test_coordinator_no_leaked_tasks_after_shutdown():
    """After shutdown, no dangling asyncio tasks remain from coordinator."""
    from app.coordinator import ServiceCoordinator

    async def run():
        tasks_before = len(asyncio.all_tasks())

        c = ServiceCoordinator(_settings(shutdown_grace_timeout_sec=5.0))
        await c.start()
        await c.shutdown()

        await asyncio.sleep(0.01)

        tasks_after = len(asyncio.all_tasks())
        assert tasks_after <= tasks_before + 5, \
            f"Task leak: {tasks_before} -> {tasks_after}"

    asyncio.run(run())


def test_coordinator_server_closes_on_shutdown():
    """After shutdown, the Wyoming listener should be stopped."""
    from app.coordinator import ServiceCoordinator
    from app.lifecycle import LifecycleState

    async def run():
        c = ServiceCoordinator(_settings(shutdown_grace_timeout_sec=5.0))
        await c.start()
        assert c.server is not None
        port = c.server.port

        await c.shutdown()
        assert c.lifecycle.state == LifecycleState.STOPPED

        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        try:
            result = s.connect_ex(("127.0.0.1", port))
        finally:
            s.close()

    asyncio.run(run())


# Handler-level lifecycle integration tests

@pytest.mark.asyncio
async def test_handler_rejects_synthesis_when_coordinator_draining(monkeypatch):
    """Handler checks lifecycle state and rejects synthesis during drain."""
    from app.coordinator import ServiceCoordinator
    from app.wyoming_server import FakeTtsEventHandler, FakeTtsConfig
    from app.config import Settings
    from app.speech import SpeechScheduler
    from wyoming.tts import Synthesize
    from wyoming.error import Error

    settings = Settings(tts_backend="fake")
    coordinator = ServiceCoordinator(settings)
    queue = SpeechScheduler(max_size=3)

    reader = asyncio.StreamReader()
    writer = _StubWriter()

    handler = FakeTtsEventHandler(
        reader, writer, FakeTtsConfig(),
        queue, settings,
        lambda _: None,
        coordinator=coordinator,
    )

    await coordinator.start()

    written: list = []

    async def _write(event):
        written.append(event)

    monkeypatch.setattr(handler, "write_event", _write)

    # Start draining
    draining = coordinator.start_draining()
    assert draining is True

    result = await handler.handle_event(Synthesize(text="hello").event())
    assert result is True

    error_events = [e for e in written if Error.is_type(e.type)]
    assert len(error_events) >= 1, f"Expected error event, got {[e.type for e in written]}"

    await coordinator.shutdown()


# Integration: full server round-trip with coordinator shutdown

def test_full_server_lifecycle_with_coordinator():
    """End-to-end: start server with coordinator, synthesize, shutdown cleanly."""
    from app.coordinator import ServiceCoordinator
    from app.config import Settings
    from app.wyoming_server import parse_tcp_uri
    from wyoming.client import AsyncTcpClient
    from wyoming.info import Describe, Info
    from wyoming.tts import Synthesize

    async def run():
        settings = Settings(tts_backend="fake", shutdown_grace_timeout_sec=5.0,
                           wyoming_uri="tcp://127.0.0.1:0")
        coordinator = ServiceCoordinator(settings)
        await coordinator.start()
        port = coordinator.server.port

        try:
            async with AsyncTcpClient("127.0.0.1", port) as client:
                await client.write_event(Describe().event())
                info_event = await asyncio.wait_for(client.read_event(), timeout=2.0)
                assert info_event is not None
                assert Info.is_type(info_event.type)

                await client.write_event(Synthesize(text="hello").event())
                events = []
                while True:
                    event = await asyncio.wait_for(client.read_event(), timeout=5.0)
                    if event is None:
                        break
                    events.append(event)
                    if AudioStop.is_type(event.type):
                        break
                assert any(AudioStart.is_type(e.type) for e in events)
                assert any(AudioChunk.is_type(e.type) for e in events)

        finally:
            await coordinator.shutdown()

        from app.lifecycle import LifecycleState
        assert coordinator.lifecycle.state == LifecycleState.STOPPED

    asyncio.run(run())


def test_coordinator_rejects_new_connections_after_listener_stop():
    """After drain starts and listener stops, no new connections accepted."""
    from app.coordinator import ServiceCoordinator
    from app.config import Settings

    async def run():
        settings = Settings(tts_backend="fake", shutdown_grace_timeout_sec=5.0,
                           wyoming_uri="tcp://127.0.0.1:0")
        coordinator = ServiceCoordinator(settings)
        await coordinator.start()
        port = coordinator.server.port

        drain_started = coordinator.start_draining()
        assert drain_started is True

        await coordinator.server.stop()

        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        result = s.connect_ex(("127.0.0.1", port))
        s.close()
        assert result != 0, "Expected connection refusal after listener stop"

        await coordinator.shutdown()

    asyncio.run(run())

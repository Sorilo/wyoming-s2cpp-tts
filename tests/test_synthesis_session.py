"""Slice 4: SynthesisSession unit tests — TDD first.

Tests the SynthesisSession lifecycle wrapper before wiring it into
FakeTtsEventHandler.
"""

import asyncio
import pytest

from app.speech.session import SynthesisSession
from app.speech.models import SpeechRequest, SpeechMetadata


def test_session_tracks_audiostream_state_initially_false():
    """AudioStart/AudioStop not emitted until explicitly marked."""
    req = SpeechRequest(synthesis_id="s1", connection_id="c1", text="hello")
    session = SynthesisSession(request=req, trigger="legacy")
    assert not session.audio_start_emitted
    assert not session.audio_stop_emitted


def test_session_marks_audio_start_idempotently():
    """Marking AudioStart multiple times stays True."""
    req = SpeechRequest(synthesis_id="s1", connection_id="c1", text="hello")
    session = SynthesisSession(request=req, trigger="legacy")
    session.mark_audio_start()
    assert session.audio_start_emitted
    session.mark_audio_start()
    assert session.audio_start_emitted


def test_session_marks_audio_stop_idempotently():
    """Marking AudioStop multiple times stays True."""
    req = SpeechRequest(synthesis_id="s1", connection_id="c1", text="hello")
    session = SynthesisSession(request=req, trigger="legacy")
    session.mark_audio_stop()
    assert session.audio_stop_emitted
    session.mark_audio_stop()
    assert session.audio_stop_emitted


def test_streaming_synthesize_stopped_eligibility():
    """Only streaming sessions are eligible for synthesize-stopped."""
    req = SpeechRequest(synthesis_id="s1", connection_id="c1", text="hello")
    legacy = SynthesisSession(request=req, trigger="legacy")
    streaming = SynthesisSession(request=req, trigger="streaming")
    assert not legacy.eligible_for_synthesize_stopped
    assert streaming.eligible_for_synthesize_stopped


def test_client_connected_defaults_true():
    """Sessions start with client connected."""
    req = SpeechRequest(synthesis_id="s1", connection_id="c1", text="hello")
    session = SynthesisSession(request=req, trigger="legacy")
    assert session.client_connected


def test_disconnect_marks_terminal():
    """disconnect() marks client disconnected and invokes cleanup."""
    cleanup_calls = []

    async def cleanup():
        cleanup_calls.append(1)

    req = SpeechRequest(synthesis_id="s1", connection_id="c1", text="hello")
    session = SynthesisSession(request=req, trigger="legacy")

    session.set_cleanup(cleanup)
    asyncio.run(session.disconnect())

    assert not session.client_connected
    assert len(cleanup_calls) == 1


def test_disconnect_cleanup_runs_exactly_once():
    """Multiple disconnect calls run cleanup only once."""
    cleanup_calls = []

    async def cleanup():
        cleanup_calls.append(1)

    req = SpeechRequest(synthesis_id="s1", connection_id="c1", text="hello")
    session = SynthesisSession(request=req, trigger="legacy")
    session.set_cleanup(cleanup)

    asyncio.run(session.disconnect())
    asyncio.run(session.disconnect())
    asyncio.run(session.disconnect())

    assert len(cleanup_calls) == 1


def test_set_generator_and_cleanup_closes_once():
    """Generator cleanup runs exactly once across multiple disconnect calls."""
    close_calls = []

    class FakeGenerator:
        async def aclose(self):
            close_calls.append(1)

    gen = FakeGenerator()
    req = SpeechRequest(synthesis_id="s1", connection_id="c1", text="hello")
    session = SynthesisSession(request=req, trigger="streaming")
    session.set_generator(gen)

    async def cleanup_with_gen():
        if session.generator is not None:
            await session.generator.aclose()

    session.set_cleanup(cleanup_with_gen)

    asyncio.run(session.disconnect())
    asyncio.run(session.disconnect())

    assert len(close_calls) == 1


def test_cancellation_after_partial_pcm_no_false_audio_stop():
    """Cancellation after partial PCM should not auto-emit AudioStop."""
    req = SpeechRequest(synthesis_id="s1", connection_id="c1", text="hello")
    session = SynthesisSession(request=req, trigger="legacy")
    session.mark_audio_start()
    session.mark_cancelled()
    assert not session.audio_stop_emitted
    assert session.cancelled


def test_unexpected_write_propagates_without_cleanup_blocking():
    """Unexpected write errors propagate up — cleanup exists but is separate."""
    req = SpeechRequest(synthesis_id="s1", connection_id="c1", text="hello")
    session = SynthesisSession(request=req, trigger="legacy")
    session.mark_audio_start()
    session.mark_client_disconnected()
    assert not session.client_connected
    assert not session.audio_stop_emitted


def test_session_identity_preserves_request():
    """SynthesisSession exposes its request for observability."""
    req = SpeechRequest(
        synthesis_id="syn-abc",
        connection_id="conn-xyz",
        text="hello",
        metadata=SpeechMetadata(trigger="streaming"),
    )
    session = SynthesisSession(request=req, trigger="streaming")
    assert session.request is req
    assert session.synthesis_id == "syn-abc"
    assert session.connection_id == "conn-xyz"


def test_legacy_trigger_not_eligible_for_synthesize_stopped():
    """Legacy sessions never get synthesize-stopped."""
    req = SpeechRequest(synthesis_id="s1", connection_id="c1", text="hello")
    session = SynthesisSession(request=req, trigger="legacy")
    assert not session.eligible_for_synthesize_stopped


def test_disconnect_before_audio_start_no_stop():
    """Disconnect before AudioStart should not trigger AudioStop."""
    cleanup_calls = []

    async def cleanup():
        cleanup_calls.append(1)

    req = SpeechRequest(synthesis_id="s1", connection_id="c1", text="hello")
    session = SynthesisSession(request=req, trigger="legacy")
    session.set_cleanup(cleanup)

    asyncio.run(session.disconnect())
    assert not session.audio_start_emitted
    assert not session.audio_stop_emitted
    assert len(cleanup_calls) == 1


# ── Handler integration: SynthesisSession in FakeTtsEventHandler ──────────
#
# These tests invoke the *actual handler* through a mocked backend and
# verify that SynthesisSession is updated correctly at the protocol
# level — not by inspecting source code.


class _DisconnectWriter:
    """Minimal writer stub that counts close() calls."""

    def __init__(self):
        self.close_calls = 0

    def get_extra_info(self, name):
        return ("127.0.0.1", 12345) if name == "peername" else None

    def close(self):
        self.close_calls += 1


def _make_handler_infra(monkeypatch, streaming=False):
    """Construct a handler with tracking backend, writer, and session spy.

    Returns:
        (handler, writer, queue, logs, call_log, TrackedGen, audio_events_fn)
    """
    import app.wyoming_server as ws
    from app.wyoming_server import FakeTtsEventHandler, FakeTtsConfig
    from app.speech import SpeechScheduler
    from app.config import Settings
    from app.speech.session import SynthesisSession
    from wyoming.audio import AudioStart, AudioChunk, AudioStop

    # ── Spy on SynthesisSession methods ──────────────────────────────
    call_log: list[str] = []
    _orig_init = SynthesisSession.__init__

    def _spy_init(self_, *args, **kwargs):
        _orig_init(self_, *args, **kwargs)
        call_log.append(f"init:{self_.trigger}")

    _orig_disconnect = SynthesisSession.disconnect

    async def _spy_disconnect(self_):
        call_log.append(f"disconnect:{self_.trigger}")
        await _orig_disconnect(self_)

    _orig_mark_audio_start = SynthesisSession.mark_audio_start

    def _spy_mark_audio_start(self_):
        call_log.append(f"audio_start:{self_.trigger}")
        _orig_mark_audio_start(self_)

    _orig_mark_audio_stop = SynthesisSession.mark_audio_stop

    def _spy_mark_audio_stop(self_):
        call_log.append(f"audio_stop:{self_.trigger}")
        _orig_mark_audio_stop(self_)

    _orig_mark_cancelled = SynthesisSession.mark_cancelled

    def _spy_mark_cancelled(self_):
        call_log.append(f"cancelled:{self_.trigger}")
        _orig_mark_cancelled(self_)

    monkeypatch.setattr(SynthesisSession, "__init__", _spy_init)
    monkeypatch.setattr(SynthesisSession, "disconnect", _spy_disconnect)
    monkeypatch.setattr(SynthesisSession, "mark_audio_start", _spy_mark_audio_start)
    monkeypatch.setattr(SynthesisSession, "mark_audio_stop", _spy_mark_audio_stop)
    monkeypatch.setattr(SynthesisSession, "mark_cancelled", _spy_mark_cancelled)

    # ── Transport and queue ──────────────────────────────────────────
    writer = _DisconnectWriter()
    queue = SpeechScheduler(3, 1)
    logs: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        ws, "obs_log", lambda name, **fields: logs.append((name, fields))
    )

    settings = Settings(tts_backend="s2cpp", s2_stream=streaming)
    handler = FakeTtsEventHandler(
        asyncio.StreamReader(),
        writer,
        FakeTtsConfig(),
        queue,
        settings,
        lambda _: None,
    )

    # ── Tracking generator class ─────────────────────────────────────
    class _TrackedGen:
        def __init__(self, events, close_error=None):
            self.events = iter(events)
            self.close_error = close_error
            self.aclose_calls = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.events)
            except StopIteration:
                raise StopAsyncIteration

        async def aclose(self):
            self.aclose_calls += 1
            if self.close_error:
                raise self.close_error

    def _audio_events_list():
        return [
            AudioStart(rate=44100, width=2, channels=1).event(),
            AudioChunk(audio=b"\x01\x00", rate=44100, width=2, channels=1).event(),
            AudioStop().event(),
        ]

    return handler, writer, queue, logs, call_log, _TrackedGen, _audio_events_list


# ── Handler-level integration tests ───────────────────────────────────


@pytest.mark.asyncio
async def test_handler_legacy_buffered_session_tracks_audio_events(monkeypatch):
    """Legacy buffered synthesis: session marks AudioStart and AudioStop."""
    from wyoming.tts import Synthesize

    handler, writer, queue, logs, call_log, _TrackedGen, _audio_events_list = (
        _make_handler_infra(monkeypatch, streaming=False)
    )
    # Buffered backend
    async def _synthesize(*a, **kw):
        return _audio_events_list()

    monkeypatch.setattr(handler, "_synthesize_text", _synthesize)

    # Collect written events
    written: list = []

    async def _write(event):
        written.append(event)

    monkeypatch.setattr(handler, "write_event", _write)

    await handler.handle_event(Synthesize(text="hello").event())

    assert "init:legacy" in call_log
    assert "audio_start:legacy" in call_log
    assert "audio_stop:legacy" in call_log
    assert "disconnect:legacy" not in call_log
    assert "cancelled:legacy" not in call_log


@pytest.mark.asyncio
async def test_handler_streaming_session_tracks_audio_and_gates_synthesize_stopped(
    monkeypatch,
):
    """Streaming session: session gates synthesize-stopped emission."""
    from wyoming.tts import (
        SynthesizeStart,
        SynthesizeChunk,
        SynthesizeStop,
        SynthesizeStopped,
    )

    handler, writer, queue, logs, call_log, _TrackedGen, _audio_events_list = (
        _make_handler_infra(monkeypatch, streaming=False)
    )
    # Buffered backend for simplicity — we test session gating not transport
    async def _synthesize(*a, **kw):
        return _audio_events_list()

    monkeypatch.setattr(handler, "_synthesize_text", _synthesize)

    written: list = []

    async def _write(event):
        written.append(event)

    monkeypatch.setattr(handler, "write_event", _write)

    await handler.handle_event(SynthesizeStart().event())
    await handler.handle_event(SynthesizeChunk(text="hello").event())
    await handler.handle_event(SynthesizeStop().event())

    assert "init:streaming" in call_log
    assert "audio_start:streaming" in call_log
    assert "audio_stop:streaming" in call_log
    # synthesize-stopped must be emitted for streaming
    assert any(SynthesizeStopped.is_type(e.type) for e in written)


@pytest.mark.asyncio
async def test_handler_legacy_session_does_not_emit_synthesize_stopped(monkeypatch):
    """Legacy trigger: synthesize-stopped is *not* emitted."""
    from wyoming.tts import Synthesize, SynthesizeStopped

    handler, writer, queue, logs, call_log, _TrackedGen, _audio_events_list = (
        _make_handler_infra(monkeypatch, streaming=False)
    )

    async def _synthesize(*a, **kw):
        return _audio_events_list()

    monkeypatch.setattr(handler, "_synthesize_text", _synthesize)

    written: list = []

    async def _write(event):
        written.append(event)

    monkeypatch.setattr(handler, "write_event", _write)

    await handler.handle_event(Synthesize(text="hello").event())

    assert not any(SynthesizeStopped.is_type(e.type) for e in written)


@pytest.mark.asyncio
async def test_handler_streaming_disconnect_runs_session_disconnect(monkeypatch):
    """On streaming disconnect, session.disconnect() closes the generator."""
    from wyoming.tts import SynthesizeStart, SynthesizeChunk, SynthesizeStop

    handler, writer, queue, logs, call_log, _TrackedGen, _audio_events_list = (
        _make_handler_infra(monkeypatch, streaming=True)
    )
    events = _audio_events_list()
    gen = _TrackedGen(events)
    monkeypatch.setattr(
        handler, "_synthesize_text_streaming", lambda *a, **kw: gen
    )

    fail = True

    async def _write(event):
        nonlocal fail
        if fail:
            fail = False
            raise BrokenPipeError("injected")
        # remaining writes succeed silently

    monkeypatch.setattr(handler, "write_event", _write)

    await handler.handle_event(SynthesizeStart().event())
    await handler.handle_event(SynthesizeChunk(text="hello").event())
    await handler.handle_event(SynthesizeStop().event())

    # Session.disconnect() was called for the streaming path
    assert "disconnect:streaming" in call_log
    # Generator closed exactly once via session cleanup
    assert gen.aclose_calls == 1
    # No dangling generator closure through _handle_expected_disconnect
    assert writer.close_calls == 1


@pytest.mark.asyncio
async def test_handler_session_marks_cancelled_on_cancelled_error(monkeypatch):
    """CancelledError inside the operation marks the session cancelled."""
    from wyoming.tts import Synthesize

    handler, writer, queue, logs, call_log, _TrackedGen, _audio_events_list = (
        _make_handler_infra(monkeypatch, streaming=False)
    )

    async def _synthesize(*a, **kw):
        raise asyncio.CancelledError("task cancelled")

    monkeypatch.setattr(handler, "_synthesize_text", _synthesize)
    monkeypatch.setattr(handler, "write_event", lambda e: None)

    await handler.handle_event(Synthesize(text="hello").event())

    assert "cancelled:legacy" in call_log


@pytest.mark.asyncio
async def test_handler_disconnect_cleanup_runs_exactly_once(monkeypatch):
    """Generator aclose is called exactly once via session cleanup."""
    from wyoming.tts import SynthesizeStart, SynthesizeChunk, SynthesizeStop

    handler, writer, queue, logs, call_log, _TrackedGen, _audio_events_list = (
        _make_handler_infra(monkeypatch, streaming=True)
    )
    events = _audio_events_list()
    gen = _TrackedGen(events)
    monkeypatch.setattr(
        handler, "_synthesize_text_streaming", lambda *a, **kw: gen
    )
    write_count = 0

    async def _write(event):
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise BrokenPipeError("injected")

    monkeypatch.setattr(handler, "write_event", _write)

    await handler.handle_event(SynthesizeStart().event())
    await handler.handle_event(SynthesizeChunk(text="hello").event())
    await handler.handle_event(SynthesizeStop().event())

    # Session.disconnect() was called → generator closed once
    assert "disconnect:streaming" in call_log
    assert gen.aclose_calls == 1
    # Transport closed
    assert writer.close_calls == 1

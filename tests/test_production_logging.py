"""Production-path tests: verify structured observability logs reach stderr.

These tests programmatically start the server, capture stderr, and assert
that conn_open, event_in, syn_trigger, backend_start/done, audio_out, and
conn_close appear as structured JSON on the captured stream.

They would have FAILED against sha-3e49cc5 because setup_logging() was not
called — no StreamHandler was attached, so no records reached stderr.
"""

import asyncio
import io
import json
import logging
import os
import tempfile

import pytest
from wyoming.client import AsyncTcpClient
from wyoming.info import Describe
from wyoming.tts import Synthesize, SynthesizeVoice

from app.config import Settings
from app.observability import setup_logging
from app.s2_client import S2GenerateResult
from app.wyoming_server import (
    FakeTtsConfig,
    start_fake_tts_server,
    synthesize_s2cpp_tts_events,
)


REAL_PCM_HEADERS = {
    "x-audio-encoding": "pcm_s16le",
    "x-audio-channels": "1",
    "x-audio-sample-rate": "44100",
}
REAL_PCM_CONTENT_TYPE = "audio/L16; rate=44100; channels=1"


class _BufferedAsStream:
    def __init__(self, result):
        self.content_type = result.content_type
        self.response_headers = result.response_headers
        self._audio = result.audio
        self._yielded = False
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __iter__(self): return self
    def __next__(self):
        if self._yielded: raise StopIteration
        self._yielded = True
        return self._audio


class RecordingS2Client:
    def __init__(self, audio, *, content_type=REAL_PCM_CONTENT_TYPE,
                 response_headers=None):
        self.audio = audio
        self.content_type = content_type
        self.response_headers = REAL_PCM_HEADERS.copy() if response_headers is None else response_headers
        self.requests = []

    def generate_multipart(self, request):
        self.requests.append(request)
        return S2GenerateResult(
            audio=self.audio,
            content_type=self.content_type,
            response_headers=self.response_headers.copy(),
        )

    def generate_stream(self, request, files=None, boundary=None):
        self.requests.append(request)
        result = S2GenerateResult(
            audio=self.audio,
            content_type=self.content_type,
            response_headers=self.response_headers.copy(),
        )
        return _BufferedAsStream(result)


def _parse_json_lines(text: str) -> list[dict]:
    """Parse newline-delimited JSON lines from captured text."""
    records = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return records


def _events_of_type(records, event_name):
    return [r for r in records if r.get("event") == event_name]


# ── Production-path: connection lifecycle ──────────────────────────────

@pytest.mark.asyncio
async def test_conn_open_and_close_on_tcp_connect_disconnect():
    """conn_open and conn_close appear on stderr for a real TCP connection."""
    # Capture stderr
    captured = io.StringIO()
    # Reset any prior handlers so we get a clean stream.
    import app.observability as obs_module
    obs_module.logger.handlers.clear()

    setup_logging("info", stream=captured)

    server = await start_fake_tts_server(host="127.0.0.1", port=0)
    try:
        async with AsyncTcpClient("127.0.0.1", server.port) as wyoming_client:
            await wyoming_client.write_event(Describe().event())
            await asyncio.wait_for(wyoming_client.read_event(), timeout=2)
    finally:
        await server.stop()

    # Flush the handler so all records reach the stream.
    for h in obs_module.logger.handlers:
        h.flush()

    records = _parse_json_lines(captured.getvalue())

    conn_opens = _events_of_type(records, "conn_open")
    assert len(conn_opens) >= 1, (
        f"Expected >=1 conn_open, got {len(conn_opens)}. "
        f"Captured: {captured.getvalue()[:500]}"
    )
    assert "connection_id" in conn_opens[0]
    assert len(conn_opens[0]["connection_id"]) == 8

    conn_closes = _events_of_type(records, "conn_close")
    assert len(conn_closes) >= 1
    assert "connection_id" in conn_closes[0]

    # Same connection_id in open and close
    assert conn_opens[0]["connection_id"] == conn_closes[0]["connection_id"]


# ── Production-path: incoming event logging ────────────────────────────

@pytest.mark.asyncio
async def test_event_in_logged_for_describe():
    """Describe events produce event_in logs on stderr."""
    captured = io.StringIO()
    import app.observability as obs_module
    obs_module.logger.handlers.clear()

    setup_logging("info", stream=captured)
    server = await start_fake_tts_server(host="127.0.0.1", port=0)
    try:
        async with AsyncTcpClient("127.0.0.1", server.port) as wyoming_client:
            await wyoming_client.write_event(Describe().event())
            await asyncio.wait_for(wyoming_client.read_event(), timeout=2)
    finally:
        await server.stop()

    for h in obs_module.logger.handlers:
        h.flush()

    records = _parse_json_lines(captured.getvalue())
    event_ins = _events_of_type(records, "event_in")
    describe_events = [e for e in event_ins if e.get("event_type") == "describe"]
    assert len(describe_events) >= 1, f"No describe event_in found. Records: {records}"
    assert "connection_id" in describe_events[0]


# ── Production-path: full synthesis log chain ──────────────────────────

@pytest.mark.asyncio
async def test_full_synthesis_log_chain_on_stderr():
    """syn_trigger → backend_start → backend_done → audio_out on stderr."""
    captured = io.StringIO()
    import app.observability as obs_module
    obs_module.logger.handlers.clear()

    setup_logging("info", stream=captured)

    # Use s2cpp backend with a recording client.
    pcm = b"\x01\x00" * 200
    rec_client = RecordingS2Client(audio=pcm)
    settings = Settings(tts_backend="s2cpp", s2_voice_dir="/tmp/nonexistent")

    server = await start_fake_tts_server(
        host="127.0.0.1", port=0, settings=settings,
        s2_client_factory=lambda s: rec_client,
    )
    try:
        async with AsyncTcpClient("127.0.0.1", server.port) as wyoming_client:
            await wyoming_client.write_event(Synthesize(text="hello world").event())
            # Read audio events
            while True:
                evt = await asyncio.wait_for(wyoming_client.read_event(), timeout=3)
                if evt is None:
                    break
                from wyoming.audio import AudioStop
                if AudioStop.is_type(evt.type):
                    break
    finally:
        await server.stop()

    for h in obs_module.logger.handlers:
        h.flush()

    records = _parse_json_lines(captured.getvalue())

    # Verify event_in was logged
    event_ins = _events_of_type(records, "event_in")
    syn_events = [e for e in event_ins if e.get("event_type") == "synthesize"]
    assert len(syn_events) >= 1, f"No synthesize event_in. Records: {records}"
    assert "text_fp" in syn_events[0]
    assert "text_len" in syn_events[0]
    assert syn_events[0]["text_len"] == 11  # "hello world"

    # Verify syn_trigger was logged
    triggers = _events_of_type(records, "syn_trigger")
    assert len(triggers) == 1, f"Expected 1 syn_trigger, got {len(triggers)}"
    assert triggers[0]["trigger"] == "legacy"
    assert triggers[0]["text_len"] == 11

    # Verify backend_start and backend_done
    backend_starts = _events_of_type(records, "backend_start")
    assert len(backend_starts) == 1
    backend_dones = _events_of_type(records, "backend_stream_done")
    assert len(backend_dones) == 1
    assert backend_dones[0]["status"] == "ok"

    # Verify audio_out
    audio_outs = _events_of_type(records, "audio_out")
    assert len(audio_outs) == 1
    assert audio_outs[0]["audio_start"] is True
    assert audio_outs[0]["audio_stop"] is True
    assert audio_outs[0]["pcm_bytes"] > 0

    # All share the same synthesis_id
    sid = triggers[0]["synthesis_id"]
    assert backend_starts[0]["synthesis_id"] == sid
    assert backend_dones[0]["synthesis_id"] == sid
    assert audio_outs[0]["synthesis_id"] == sid


# ── Production-path: correlation IDs are consistent ────────────────────

@pytest.mark.asyncio
async def test_single_connection_single_connection_id():
    """All events in one connection share the same connection_id."""
    captured = io.StringIO()
    import app.observability as obs_module
    obs_module.logger.handlers.clear()

    setup_logging("info", stream=captured)

    pcm = b"\x01\x00" * 200
    rec_client = RecordingS2Client(audio=pcm)
    settings = Settings(tts_backend="s2cpp", s2_voice_dir="/tmp/nonexistent")

    server = await start_fake_tts_server(
        host="127.0.0.1", port=0, settings=settings,
        s2_client_factory=lambda s: rec_client,
    )
    try:
        async with AsyncTcpClient("127.0.0.1", server.port) as wyoming_client:
            await wyoming_client.write_event(Synthesize(text="one").event())
            while True:
                evt = await asyncio.wait_for(wyoming_client.read_event(), timeout=3)
                if evt is None:
                    break
                from wyoming.audio import AudioStop
                if AudioStop.is_type(evt.type):
                    break
    finally:
        await server.stop()

    for h in obs_module.logger.handlers:
        h.flush()

    records = _parse_json_lines(captured.getvalue())

    # Collect all connection_ids
    conn_ids = {r["connection_id"] for r in records if "connection_id" in r}
    assert len(conn_ids) == 1, (
        f"Expected 1 connection_id across all events, got {conn_ids}"
    )


# ── Idempotency: setup_logging is safe to call multiple times ──────────

def test_setup_logging_is_idempotent():
    """Calling setup_logging twice does not create duplicate handlers."""
    import app.observability as obs_module

    # Reset for test
    obs_module.logger.handlers.clear()

    setup_logging("info")
    handler_count_1 = len(obs_module.logger.handlers)
    assert handler_count_1 == 1

    setup_logging("info")
    handler_count_2 = len(obs_module.logger.handlers)
    assert handler_count_2 == 1, "setup_logging created duplicate handlers"

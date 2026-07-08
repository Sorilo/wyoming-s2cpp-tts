"""Tests for app.observability — correlation identifiers and text fingerprinting.

Also tests that the synthesis paths produce the expected observability
log events (conn_open, event_in, syn_trigger, backend_start/done,
audio_out, conn_close) with the correct fields.
"""

import asyncio
import json
import logging
import os
import re

import pytest
from wyoming.client import AsyncTcpClient
from wyoming.info import Describe
from wyoming.tts import Synthesize, SynthesizeVoice, SynthesizeChunk, SynthesizeStop, SynthesizeStart

from app.config import Settings
from app.observability import (
    LogContext,
    new_connection_id,
    new_synthesis_id,
    obs_log,
    text_fingerprint,
)
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


class LogCapture:
    """Capture obs_log output for assertions."""

    def __init__(self):
        self.records = []

    def handle(self, record):
        try:
            self.records.append(json.loads(record.getMessage()))
        except json.JSONDecodeError:
            self.records.append({"raw": record.getMessage()})

    def __enter__(self):
        self._handler = logging.Handler()
        self._handler.emit = self.handle
        logging.getLogger("wyoming-s2cpp-tts.obs").addHandler(self._handler)
        logging.getLogger("wyoming-s2cpp-tts.obs").setLevel(logging.INFO)
        return self

    def __exit__(self, *args):
        logging.getLogger("wyoming-s2cpp-tts.obs").removeHandler(self._handler)

    def events_of_type(self, event_name):
        return [r for r in self.records if r.get("event") == event_name]


# ── Unit tests: ID generation ──────────────────────────────────────────

def test_new_connection_id_is_hex_string():
    cid = new_connection_id()
    assert isinstance(cid, str)
    assert len(cid) == 8
    assert re.match(r'^[0-9a-f]+$', cid)


def test_new_connection_ids_are_unique():
    ids = {new_connection_id() for _ in range(100)}
    assert len(ids) == 100


def test_new_synthesis_id_is_hex_string():
    sid = new_synthesis_id()
    assert isinstance(sid, str)
    assert len(sid) == 8
    assert re.match(r'^[0-9a-f]+$', sid)


# ── Unit tests: text fingerprinting ────────────────────────────────────

def test_text_fingerprint_is_short_hex():
    fp = text_fingerprint("hello world")
    assert isinstance(fp, str)
    assert len(fp) == 12
    assert re.match(r'^[0-9a-f]+$', fp)


def test_text_fingerprint_is_deterministic():
    a = text_fingerprint("hello world")
    b = text_fingerprint("hello world")
    assert a == b


def test_text_fingerprint_differs_for_different_input():
    a = text_fingerprint("hello")
    b = text_fingerprint("world")
    assert a != b


def test_text_fingerprint_empty_returns_sentinel():
    assert text_fingerprint("") == "<empty>"


def test_text_fingerprint_does_not_contain_input():
    fp = text_fingerprint("secret message")
    assert "secret" not in fp
    assert "message" not in fp


# ── Unit tests: obs_log ────────────────────────────────────────────────

def test_obs_log_emits_json():
    with LogCapture() as cap:
        obs_log("test_event", key="value", number=42)
    assert len(cap.records) == 1
    assert cap.records[0]["event"] == "test_event"
    assert cap.records[0]["key"] == "value"
    assert cap.records[0]["number"] == 42


# ── Integration: legacy synthesize produces exactly one synthesis ──────

def test_legacy_request_produces_one_synthesis_log():
    pcm = b"\x01\x00" * 100
    client = RecordingS2Client(audio=pcm)
    settings = Settings(tts_backend="s2cpp", s2_voice_dir="/tmp/nonexistent")
    config = FakeTtsConfig(sample_rate=8000, duration_ms=120, chunk_ms=100)
    ctx = LogContext(connection_id="cccccccc", synthesis_id="ssssssss")

    with LogCapture() as cap:
        synthesize_s2cpp_tts_events("hello", client=client, settings=settings,
                                     config=config, ctx=ctx)

    triggers = cap.events_of_type("syn_trigger")
    assert len(triggers) == 0  # trigger is logged in _synthesize_text, not directly

    backend_starts = cap.events_of_type("backend_start")
    assert len(backend_starts) == 1
    assert backend_starts[0]["text_fp"] == text_fingerprint("hello")
    assert backend_starts[0]["text_len"] == 5

    backend_dones = cap.events_of_type("backend_done")
    assert len(backend_dones) == 1
    assert backend_dones[0]["status"] == "ok"
    assert backend_dones[0]["audio_bytes"] == 200

    audio_outs = cap.events_of_type("audio_out")
    assert len(audio_outs) == 1
    assert audio_outs[0]["audio_start"] is True
    assert audio_outs[0]["audio_stop"] is True
    assert audio_outs[0]["chunk_count"] > 0


# ── Integration: two connections are distinguishable ───────────────────

@pytest.mark.asyncio
async def test_two_connections_produce_distinct_connection_ids():
    """Two separate TCP connections get different connection_ids."""
    with LogCapture() as cap:
        server = await start_fake_tts_server(host="127.0.0.1", port=0)
        try:
            # Connection 1
            async with AsyncTcpClient("127.0.0.1", server.port) as client:
                await client.write_event(Describe().event())
                await asyncio.wait_for(client.read_event(), timeout=2)

            # Connection 2
            async with AsyncTcpClient("127.0.0.1", server.port) as client2:
                await client2.write_event(Describe().event())
                await asyncio.wait_for(client2.read_event(), timeout=2)
        finally:
            await server.stop()

    conn_opens = cap.events_of_type("conn_open")
    # Should be at least 2 (potentially more if HA reconnects)
    conn_ids = {c["connection_id"] for c in conn_opens}
    assert len(conn_ids) >= 2, f"Expected >=2 distinct connection_ids, got {conn_ids}"

    conn_closes = cap.events_of_type("conn_close")
    assert len(conn_closes) >= 2


# ── Integration: incoming event logs ───────────────────────────────────

@pytest.mark.asyncio
async def test_incoming_event_logs_include_event_type():
    with LogCapture() as cap:
        server = await start_fake_tts_server(host="127.0.0.1", port=0)
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as client:
                await client.write_event(Describe().event())
                await asyncio.wait_for(client.read_event(), timeout=2)
        finally:
            await server.stop()

    event_ins = cap.events_of_type("event_in")
    describe_events = [e for e in event_ins if e.get("event_type") == "describe"]
    assert len(describe_events) >= 1
    assert "connection_id" in describe_events[0]
    assert "streaming_active" in describe_events[0]


# ── Integration: text is fingerprinted, not stored ─────────────────────

def test_synthesis_log_contains_fingerprint_not_text():
    pcm = b"\x01\x00" * 100
    client = RecordingS2Client(audio=pcm)
    settings = Settings(tts_backend="s2cpp")
    config = FakeTtsConfig(sample_rate=8000, duration_ms=120, chunk_ms=100)
    ctx = LogContext(connection_id="cc", synthesis_id="ss")

    with LogCapture() as cap:
        synthesize_s2cpp_tts_events("sensitive text", client=client,
                                     settings=settings, config=config, ctx=ctx)

    all_logs = json.dumps(cap.records)
    assert "sensitive text" not in all_logs
    assert "hello" not in all_logs or "hello" in str(cap.records)  # false positive guard


# ── Integration: error path retains synthesis_id ───────────────────────

def test_error_path_logs_backend_done_with_error():
    class FailingClient:
        def generate_multipart(self, request):
            from app.s2_client import S2ClientError
            raise S2ClientError("backend timeout")

    client = FailingClient()
    settings = Settings(tts_backend="s2cpp")
    config = FakeTtsConfig(sample_rate=8000, duration_ms=120, chunk_ms=100)
    ctx = LogContext(connection_id="cc", synthesis_id="errsyn")

    with LogCapture() as cap:
        with pytest.raises(Exception):
            synthesize_s2cpp_tts_events("test", client=client, settings=settings,
                                         config=config, ctx=ctx)

    backend_starts = cap.events_of_type("backend_start")
    assert len(backend_starts) == 1

    backend_dones = cap.events_of_type("backend_done")
    assert len(backend_dones) == 1
    assert backend_dones[0]["status"] == "error"
    assert backend_dones[0]["synthesis_id"] == "errsyn"

    # No audio_out on error
    assert len(cap.events_of_type("audio_out")) == 0


# ── Integration: audio lifecycle contains start/stop summary ───────────

def test_audio_out_contains_one_start_one_stop():
    pcm = b"\x01\x00" * 100
    client = RecordingS2Client(audio=pcm)
    settings = Settings(tts_backend="s2cpp")
    config = FakeTtsConfig(sample_rate=8000, duration_ms=120, chunk_ms=100)
    ctx = LogContext(connection_id="cc", synthesis_id="ss")

    with LogCapture() as cap:
        synthesize_s2cpp_tts_events("hello", client=client, settings=settings,
                                     config=config, ctx=ctx)

    audio_outs = cap.events_of_type("audio_out")
    assert len(audio_outs) == 1
    ao = audio_outs[0]
    assert ao["audio_start"] is True
    assert ao["audio_stop"] is True
    assert ao["pcm_bytes"] > 0
    assert ao["chunk_count"] > 0


# ── Integration: existing voice selection tests still pass ─────────────

def test_voice_selection_still_works_with_observability(tmp_path):
    """Voice selection + synthesis still works with LogContext."""
    voice_dir = str(tmp_path)
    for pid in ["voice_a"]:
        with open(os.path.join(voice_dir, f"{pid}.s2voice"), "wb") as f:
            f.write(b"")

    pcm = b"\x01\x00" * 100
    client = RecordingS2Client(audio=pcm)
    settings = Settings(tts_backend="s2cpp", s2_voice_dir=voice_dir)
    config = FakeTtsConfig(sample_rate=8000, duration_ms=120, chunk_ms=100)
    ctx = LogContext(connection_id="cc", synthesis_id="ss")

    with LogCapture() as cap:
        events = synthesize_s2cpp_tts_events(
            "hello", client=client, settings=settings, config=config,
            voice="voice_a", ctx=ctx,
        )

    # Voice was forwarded
    assert client.requests[0].voice == "voice_a"
    assert client.requests[0].voice_dir == voice_dir
    # Audio events produced
    from wyoming.audio import AudioStart, AudioStop
    assert AudioStart.is_type(events[0].type)
    assert AudioStop.is_type(events[-1].type)
    # Backend log includes voice
    backend_starts = cap.events_of_type("backend_start")
    assert backend_starts[0]["voice"] == "voice_a"


# ── Integration: streaming sequence produces one synthesis log ─────────

@pytest.mark.asyncio
async def test_streaming_sequence_produces_one_synthesis():
    """A full streaming start/chunk/stop sequence triggers exactly one synthesis."""
    with LogCapture() as cap:
        server = await start_fake_tts_server(host="127.0.0.1", port=0)
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as client:
                await client.write_event(SynthesizeStart().event())
                await client.write_event(SynthesizeChunk(text="hello").event())
                await client.write_event(SynthesizeStop().event())
                # Read events until synthesize-stopped
                while True:
                    evt = await asyncio.wait_for(client.read_event(), timeout=3)
                    if evt is None or evt.type == "synthesize-stopped":
                        break
        finally:
            await server.stop()

    triggers = cap.events_of_type("syn_trigger")
    assert len(triggers) == 1, f"Expected 1 syn_trigger, got {len(triggers)}"
    assert triggers[0]["trigger"] == "streaming"
    assert triggers[0]["text_len"] == 5

    syn_stopped = cap.events_of_type("syn_stopped")
    assert len(syn_stopped) == 1

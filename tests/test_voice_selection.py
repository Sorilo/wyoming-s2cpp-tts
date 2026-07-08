"""Tests for voice selection, Describe exposure, and synthesis voice propagation."""

import asyncio
import os
import tempfile

import pytest
from wyoming.client import AsyncTcpClient
from wyoming.event import Event
from wyoming.info import Describe, Info
from wyoming.tts import Synthesize, SynthesizeVoice

from app.config import Settings
from app.s2_client import S2GenerateResult
from app.wyoming_server import (
    FakeTtsConfig,
    _resolve_voice_from_synthesize,
    build_info_event,
    start_fake_tts_server,
    synthesize_s2cpp_tts_events,
)


# ── Real PCM header constants ──────────────────────────────────────────

REAL_PCM_HEADERS = {
    "x-audio-encoding": "pcm_s16le",
    "x-audio-channels": "1",
    "x-audio-sample-rate": "44100",
}
REAL_PCM_CONTENT_TYPE = "audio/L16; rate=44100; channels=1"


class RecordingS2Client:
    """Capture generate_multipart() calls for assertions."""

    def __init__(
        self,
        audio: bytes,
        *,
        content_type: str = REAL_PCM_CONTENT_TYPE,
        response_headers: dict[str, str] | None = None,
    ):
        self.audio = audio
        self.content_type = content_type
        self.response_headers = (
            REAL_PCM_HEADERS.copy() if response_headers is None else response_headers
        )
        self.requests = []

    def generate_multipart(self, request):
        self.requests.append(request)
        return S2GenerateResult(
            audio=self.audio,
            content_type=self.content_type,
            response_headers=self.response_headers.copy(),
        )


def _make_voice_files(tmp_path, profile_ids):
    """Create .s2voice files for the given profile IDs."""
    for pid in profile_ids:
        path = os.path.join(tmp_path, f"{pid}.s2voice")
        with open(path, "wb") as f:
            f.write(b"")


# ── _resolve_voice_from_synthesize tests ───────────────────────────────

def test_resolve_client_requested_voice(tmp_path):
    _make_voice_files(tmp_path, ["voice_a", "voice_b"])
    settings = Settings(s2_voice_dir=str(tmp_path))
    syn = Synthesize(text="hi", voice=SynthesizeVoice(name="voice_a"))
    assert _resolve_voice_from_synthesize(syn, settings) == "voice_a"


def test_resolve_configured_default_when_no_client_voice(tmp_path):
    _make_voice_files(tmp_path, ["voice_a", "voice_b"])
    settings = Settings(s2_voice_dir=str(tmp_path), s2_default_voice="voice_b")
    syn = Synthesize(text="hi")
    assert _resolve_voice_from_synthesize(syn, settings) == "voice_b"


def test_resolve_none_when_no_voice_requested_and_no_default(tmp_path):
    _make_voice_files(tmp_path, ["voice_a"])
    settings = Settings(s2_voice_dir=str(tmp_path))
    syn = Synthesize(text="hi")
    assert _resolve_voice_from_synthesize(syn, settings) is None


def test_resolve_none_when_no_voice_requested_and_empty_default(tmp_path):
    _make_voice_files(tmp_path, ["voice_a"])
    settings = Settings(s2_voice_dir=str(tmp_path), s2_default_voice="")
    syn = Synthesize(text="hi")
    assert _resolve_voice_from_synthesize(syn, settings) is None


def test_reject_unknown_voice(tmp_path):
    _make_voice_files(tmp_path, ["voice_a"])
    settings = Settings(s2_voice_dir=str(tmp_path))
    syn = Synthesize(text="hi", voice=SynthesizeVoice(name="unknown"))
    with pytest.raises(ValueError, match="Unknown voice"):
        _resolve_voice_from_synthesize(syn, settings)


def test_reject_unknown_default(tmp_path):
    _make_voice_files(tmp_path, ["voice_a"])
    settings = Settings(s2_voice_dir=str(tmp_path), s2_default_voice="unknown")
    syn = Synthesize(text="hi")
    with pytest.raises(ValueError, match="S2_DEFAULT_VOICE"):
        _resolve_voice_from_synthesize(syn, settings)


def test_reject_unsafe_voice_name(tmp_path):
    _make_voice_files(tmp_path, ["safe"])
    settings = Settings(s2_voice_dir=str(tmp_path))
    syn = Synthesize(text="hi", voice=SynthesizeVoice(name="../etc/passwd"))
    with pytest.raises(ValueError, match="Unknown voice"):
        _resolve_voice_from_synthesize(syn, settings)


# ── build_info_event (Describe) tests ──────────────────────────────────

def test_describe_includes_s2_pro_voice():
    event = build_info_event(Settings(tts_backend="s2cpp"))
    info = Info.from_event(event)
    voice_names = [v.name for v in info.tts[0].voices]
    assert "s2-pro" in voice_names
    assert info.tts[0].supports_synthesize_streaming is True


def test_describe_includes_discovered_voices(tmp_path):
    _make_voice_files(tmp_path, ["cmu_bdl_male_us", "cmu_rms_male_us"])
    settings = Settings(tts_backend="s2cpp", s2_voice_dir=str(tmp_path))
    event = build_info_event(settings)
    info = Info.from_event(event)
    voice_names = [v.name for v in info.tts[0].voices]
    assert "s2-pro" in voice_names
    assert "cmu_bdl_male_us" in voice_names
    assert "cmu_rms_male_us" in voice_names


def test_describe_no_paths_exposed(tmp_path):
    _make_voice_files(tmp_path, ["my_voice"])
    settings = Settings(tts_backend="s2cpp", s2_voice_dir=str(tmp_path))
    event = build_info_event(settings)
    info = Info.from_event(event)
    for v in info.tts[0].voices:
        description = v.description or ""
        assert str(tmp_path) not in description
        assert "/voices" not in description


def test_describe_deterministic_ordering(tmp_path):
    _make_voice_files(tmp_path, ["zzz", "aaa", "mmm"])
    settings = Settings(tts_backend="s2cpp", s2_voice_dir=str(tmp_path))
    event = build_info_event(settings)
    info = Info.from_event(event)
    # s2-pro always first, then discovered voices sorted
    voice_names = [v.name for v in info.tts[0].voices]
    assert voice_names[0] == "s2-pro"
    assert voice_names[1:] == ["aaa", "mmm", "zzz"]


def test_describe_drop_in_profile_appears(tmp_path):
    _make_voice_files(tmp_path, ["alpha"])
    settings = Settings(tts_backend="s2cpp", s2_voice_dir=str(tmp_path))

    event1 = build_info_event(settings)
    info1 = Info.from_event(event1)
    names1 = [v.name for v in info1.tts[0].voices]
    assert "alpha" in names1
    assert "beta" not in names1

    # Drop in a new profile — must appear on next Describe.
    _make_voice_files(tmp_path, ["beta"])
    event2 = build_info_event(settings)
    info2 = Info.from_event(event2)
    names2 = [v.name for v in info2.tts[0].voices]
    assert "beta" in names2


def test_describe_fake_backend_unchanged():
    """Fake backend Describe must not include voice discovery."""
    event = build_info_event(Settings(tts_backend="fake"))
    info = Info.from_event(event)
    assert info.tts[0].name == "wyoming-s2cpp-tts-fake"
    assert len(info.tts[0].voices) == 1
    assert info.tts[0].voices[0].name == "fake-test-tone"


# ── Synthesis voice propagation tests ──────────────────────────────────

def test_selected_voice_forwarded_to_backend(tmp_path):
    _make_voice_files(tmp_path, ["voice_a"])
    pcm = b"\x01\x00" * 100
    client = RecordingS2Client(audio=pcm)
    settings = Settings(
        tts_backend="s2cpp", s2_voice_dir=str(tmp_path)
    )
    config = FakeTtsConfig(sample_rate=8000, duration_ms=120, chunk_ms=100)

    events = synthesize_s2cpp_tts_events(
        "hello", client=client, settings=settings, config=config,
        voice="voice_a",
    )

    assert len(client.requests) == 1
    req = client.requests[0]
    assert req.voice == "voice_a"
    assert req.voice_dir == str(tmp_path)


def test_configured_default_forwarded(tmp_path):
    _make_voice_files(tmp_path, ["voice_a", "voice_b"])
    pcm = b"\x01\x00" * 100
    client = RecordingS2Client(audio=pcm)
    settings = Settings(
        tts_backend="s2cpp",
        s2_voice_dir=str(tmp_path),
        s2_default_voice="voice_b",
    )
    config = FakeTtsConfig(sample_rate=8000, duration_ms=120, chunk_ms=100)

    # voice=None → from_settings uses s2_default_voice
    events = synthesize_s2cpp_tts_events(
        "hello", client=client, settings=settings, config=config,
        voice=None,
    )

    assert len(client.requests) == 1
    req = client.requests[0]
    assert req.voice == "voice_b"


def test_generic_fallback_omits_custom_voice_fields(tmp_path):
    _make_voice_files(tmp_path, ["voice_a"])
    pcm = b"\x01\x00" * 100
    client = RecordingS2Client(audio=pcm)
    settings = Settings(tts_backend="s2cpp", s2_voice_dir=str(tmp_path))
    config = FakeTtsConfig(sample_rate=8000, duration_ms=120, chunk_ms=100)

    # No explicit voice, no default → generic fallback.
    events = synthesize_s2cpp_tts_events(
        "hello", client=client, settings=settings, config=config,
        voice=None,
    )

    assert len(client.requests) == 1
    req = client.requests[0]
    # When no voice, the request should have voice="" (falsy)
    assert not req.voice
    # voice_dir is still set as configured
    assert req.voice_dir == str(tmp_path)


def test_voice_propagated_in_buffered_path(tmp_path):
    _make_voice_files(tmp_path, ["voice_a"])
    pcm = b"\x01\x00" * 100
    client = RecordingS2Client(audio=pcm)
    settings = Settings(
        tts_backend="s2cpp", s2_voice_dir=str(tmp_path)
    )
    config = FakeTtsConfig(sample_rate=8000, duration_ms=120, chunk_ms=100)

    events = synthesize_s2cpp_tts_events(
        "buffered test", client=client, settings=settings, config=config,
        voice="voice_a",
    )

    # Verify audio events are correct
    from wyoming.audio import AudioStart, AudioChunk, AudioStop
    assert AudioStart.is_type(events[0].type)
    assert AudioStop.is_type(events[-1].type)
    chunks = [
        AudioChunk.from_event(e) for e in events if AudioChunk.is_type(e.type)
    ]
    assert b"".join(chunk.audio for chunk in chunks) == pcm


def test_backend_request_contract_remains_multipart(tmp_path):
    """Verify the backend request is still multipart/form-data."""
    _make_voice_files(tmp_path, ["voice_a"])
    pcm = b"\x01\x00" * 100
    client = RecordingS2Client(audio=pcm)
    settings = Settings(
        tts_backend="s2cpp", s2_voice_dir=str(tmp_path)
    )
    config = FakeTtsConfig(sample_rate=8000, duration_ms=120, chunk_ms=100)

    synthesize_s2cpp_tts_events(
        "test", client=client, settings=settings, config=config,
        voice="voice_a",
    )

    assert len(client.requests) == 1
    # The request was generated via generate_multipart() — verify fields
    req = client.requests[0]
    assert req.voice == "voice_a"
    assert req.voice_dir == str(tmp_path)
    assert req.text == "test"

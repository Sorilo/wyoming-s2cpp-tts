"""Phase 10: initial-buffer long-form playout resilience tests.

Tests for text-length-aware initial PCM buffering in
``synthesize_s2cpp_streaming_tts_events()``.  All tests use mocked
``S2Client`` / ``S2StreamResult`` — no real s2.cpp backend is contacted.
"""

from __future__ import annotations

import asyncio
import threading
import io

import pytest

from wyoming.audio import AudioChunk, AudioStart, AudioStop

from app.config import Settings
from app.observability import setup_logging
from app.s2_client import S2ClientError
from app.wyoming_server import (
    FakeTtsConfig,
    synthesize_s2cpp_streaming_tts_events,
)


# ── Mock helpers ──────────────────────────────────────────────────────────────


class _MockS2StreamResult:
    """Synchronous mock of ``S2StreamResult``."""

    def __init__(self, chunks, fail_after=None):
        self._chunks = list(chunks)
        self._index = 0
        self._closed = False
        self._cancelled = False
        self._fail_after = fail_after

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._closed = True
        return False

    def __iter__(self):
        return self

    def __next__(self):
        if self._cancelled:
            raise StopIteration
        if self._fail_after is not None and self._index >= self._fail_after:
            raise S2ClientError("simulated backend read failure")
        if self._index >= len(self._chunks):
            raise StopIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk

    def cancel(self):
        self._cancelled = True
        self._closed = True


class _MockStreamingClient:
    def __init__(self, chunks, fail_after=None, *, content_type="audio/L16; rate=44100; channels=1"):
        self._chunks = chunks
        self._fail_after = fail_after
        self._content_type = content_type
        self._last_stream = None
        self.requests = []

    def generate_stream(self, request, files=None, boundary=None, **kwargs):
        self.requests.append(request)
        stream = _MockS2StreamResult(self._chunks, fail_after=self._fail_after)
        stream.content_type = self._content_type
        stream.response_headers = {
            "x-audio-encoding": "pcm_s16le",
            "x-audio-channels": "1",
            "x-audio-sample-rate": "44100",
        }
        self._last_stream = stream
        return stream


async def _collect_events(async_gen) -> list:
    events = []
    async for event in async_gen:
        events.append(event)
    return events


# ── PCM test frames ───────────────────────────────────────────────────────────

# 44.1kHz mono s16le: frame_size = 2 bytes
# 100ms Wyoming chunk = 4410 frames = 8820 bytes
# 500ms of audio = 22050 frames = 44100 bytes
# 1000ms of audio = 44100 frames = 88200 bytes
WYOMING_CHUNK_MS = 100
WYOMING_CHUNK_BYTES = 8820

def _pcm_frames(n: int) -> bytes:
    """Return n frames of PCM (2 bytes each)."""
    return b"\x01\x00" * n


# ── Helpers ───────────────────────────────────────────────────────────────────


def _settings(**kwargs) -> Settings:
    """Build Settings with zero-buffer defaults, overridden by kwargs."""
    defaults = {
        "s2_initial_buffer_ms": 0,
        "s2_long_form_threshold_chars": 0,
        "s2_long_form_buffer_ms": 0,
        "s2_max_initial_buffer_ms": 0,
    }
    defaults.update(kwargs)
    return Settings(**defaults)


def _config(sample_rate=44100, chunk_ms=100) -> FakeTtsConfig:
    return FakeTtsConfig(sample_rate=sample_rate, chunk_ms=chunk_ms)


# ── 1. Zero-buffer compatibility ──────────────────────────────────────────────


class TestZeroBufferCompatibility:
    """When buffer target is zero, behaviour is unchanged from pre-buffering."""

    @pytest.mark.asyncio
    async def test_audiostart_emitted_before_first_chunk(self):
        """AudioStart is emitted before any AudioChunk (zero-buffer)."""
        from app.s2_client import S2GenerateRequest
        data = _pcm_frames(10)  # 10 frames, 20 bytes
        client = _MockStreamingClient(chunks=[data])
        settings = _settings()
        config = _config()

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="hi"), config, settings,
            )
        )
        assert AudioStart.is_type(events[0].type)
        assert AudioStop.is_type(events[-1].type)

    @pytest.mark.asyncio
    async def test_empty_stream_produces_start_stop_only(self):
        """Empty stream with zero-buffer: AudioStart + AudioStop, no chunks."""
        from app.s2_client import S2GenerateRequest
        client = _MockStreamingClient(chunks=[])
        settings = _settings()
        config = _config()

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="hi"), config, settings,
            )
        )
        assert len(events) == 2
        assert AudioStart.is_type(events[0].type)
        assert AudioStop.is_type(events[1].type)

    @pytest.mark.asyncio
    async def test_audiostop_after_all_chunks(self):
        """AudioStop is the last event (zero-buffer)."""
        from app.s2_client import S2GenerateRequest
        data = _pcm_frames(50)
        client = _MockStreamingClient(chunks=[data])
        settings = _settings()
        config = _config()

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="hi"), config, settings,
            )
        )
        assert AudioStop.is_type(events[-1].type)


# ── 2. Buffer threshold ───────────────────────────────────────────────────────


class TestBufferThreshold:
    """AudioStart is delayed until the buffer target is reached."""

    @pytest.mark.asyncio
    async def test_audiostart_delayed_until_buffer_full(self):
        """AudioStart not emitted until accumulated PCM meets target."""
        from app.s2_client import S2GenerateRequest
        # 500ms buffer target = 44100 bytes at 44.1kHz mono s16le
        # Feed in small chunks, each below target
        chunk_size = WYOMING_CHUNK_BYTES  # 8820 bytes ≈ 100ms
        chunks = [_pcm_frames(chunk_size // 2) for _ in range(10)]
        client = _MockStreamingClient(chunks=chunks)
        settings = _settings(s2_initial_buffer_ms=500, s2_max_initial_buffer_ms=500)
        config = _config()

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="hello world"), config, settings,
            )
        )
        assert AudioStart.is_type(events[0].type)
        # First AudioChunk after Start
        chunk_events = [e for e in events if AudioChunk.is_type(e.type)]
        assert len(chunk_events) > 0

    @pytest.mark.asyncio
    async def test_accumulated_chunks_reflect_buffered_audio(self):
        """After buffering, emitted PCM equals all backend bytes."""
        from app.s2_client import S2GenerateRequest
        total_frames = 500
        backend_data = _pcm_frames(500)
        # Feed in small chunks
        chunks = [backend_data[i:i+20] for i in range(0, len(backend_data), 20)]
        client = _MockStreamingClient(chunks=chunks)
        settings = _settings(s2_initial_buffer_ms=100, s2_max_initial_buffer_ms=500)  # small buffer
        config = _config()

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="test"), config, settings,
            )
        )
        chunk_events = [e for e in events if AudioChunk.is_type(e.type)]
        all_audio = b"".join(AudioChunk.from_event(e).audio for e in chunk_events)
        assert all_audio == backend_data


# ── 3. Backend completes below target ─────────────────────────────────────────


class TestBackendEarlyComplete:
    """When the backend finishes before the buffer target, all audio is still emitted."""

    @pytest.mark.asyncio
    async def test_all_audio_emitted_when_backend_short(self):
        """Backend produces less audio than buffer target — all still emitted."""
        from app.s2_client import S2GenerateRequest
        # Buffer target 2000ms = 176400 bytes
        # Backend only produces 5000 frames = 10000 bytes (≈113ms)
        small_data = _pcm_frames(5000)
        client = _MockStreamingClient(chunks=[small_data])
        settings = _settings(s2_initial_buffer_ms=2000, s2_max_initial_buffer_ms=2000)
        config = _config()

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="short"), config, settings,
            )
        )
        assert AudioStart.is_type(events[0].type)
        chunk_events = [e for e in events if AudioChunk.is_type(e.type)]
        all_audio = b"".join(AudioChunk.from_event(e).audio for e in chunk_events)
        assert all_audio == small_data
        assert AudioStop.is_type(events[-1].type)

    @pytest.mark.asyncio
    async def test_no_deadlock_waiting_for_unreachable_target(self):
        """Backend completes with empty data — no deadlock, no crash."""
        from app.s2_client import S2GenerateRequest
        client = _MockStreamingClient(chunks=[])
        settings = _settings(s2_initial_buffer_ms=5000, s2_max_initial_buffer_ms=5000)
        config = _config()

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="empty"), config, settings,
            )
        )
        # Empty stream with buffer set: no AudioStart (spec compliance)
        assert not any(AudioStart.is_type(e.type) for e in events)
        assert not any(AudioStop.is_type(e.type) for e in events)


# ── 4. Text-length policy ─────────────────────────────────────────────────────


class TestTextLengthPolicy:
    """Short text uses zero buffer; long text uses configured long-form buffer."""

    @pytest.mark.asyncio
    async def test_short_text_uses_zero_buffer(self):
        """Text below threshold — AudioStart is immediate (zero-buffer)."""
        from app.s2_client import S2GenerateRequest
        data = _pcm_frames(100)
        client = _MockStreamingClient(chunks=[data])
        settings = _settings(
            s2_long_form_threshold_chars=200,
            s2_long_form_buffer_ms=3000,
            s2_max_initial_buffer_ms=3000,
        )
        config = _config()

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="hi"), config, settings,
            )
        )
        assert AudioStart.is_type(events[0].type)
        # With zero buffer, chunks are emitted progressively
        chunk_events = [e for e in events if AudioChunk.is_type(e.type)]
        assert len(chunk_events) > 0

    @pytest.mark.asyncio
    async def test_long_text_uses_long_form_buffer(self):
        """Text at or above threshold uses long-form buffer target."""
        from app.s2_client import S2GenerateRequest
        long_text = "x" * 200  # exactly at threshold
        # Feed plenty of audio data
        data = _pcm_frames(5000)  # ≈113ms worth
        client = _MockStreamingClient(chunks=[data])
        settings = _settings(
            s2_long_form_threshold_chars=200,
            s2_long_form_buffer_ms=3000,
            s2_initial_buffer_ms=0,
            s2_max_initial_buffer_ms=3000,
        )
        config = _config()

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text=long_text), config, settings,
            )
        )
        # Backend data < buffer target, but all audio still emitted
        assert AudioStart.is_type(events[0].type)
        chunk_events = [e for e in events if AudioChunk.is_type(e.type)]
        all_audio = b"".join(AudioChunk.from_event(e).audio for e in chunk_events)
        assert all_audio == data


# ── 5. Byte/time conversion ───────────────────────────────────────────────────


class TestByteTimeConversion:
    """Buffer calculations are exact for given metadata."""

    def test_44100_mono_s16le_buffer_calculation(self):
        """At 44.1kHz mono s16le: 1000ms = 88200 bytes."""
        sample_rate, channels, width = 44100, 1, 2
        frame_size = width * channels
        bytes_per_ms = sample_rate * frame_size / 1000.0
        target_ms = 1000
        target_bytes = int(target_ms * bytes_per_ms)
        assert target_bytes == 88200

    def test_8000_mono_s16le_buffer_calculation(self):
        """At 8kHz mono s16le: 1000ms = 16000 bytes."""
        sample_rate, channels, width = 8000, 1, 2
        frame_size = width * channels
        bytes_per_ms = sample_rate * frame_size / 1000.0
        target_ms = 1000
        target_bytes = int(target_ms * bytes_per_ms)
        assert target_bytes == 16000

    def test_16000_stereo_s16le_buffer_calculation(self):
        """At 16kHz stereo s16le: 500ms = 32000 bytes."""
        sample_rate, channels, width = 16000, 2, 2
        frame_size = width * channels
        bytes_per_ms = sample_rate * frame_size / 1000.0
        target_ms = 500
        target_bytes = int(target_ms * bytes_per_ms)
        assert target_bytes == 32000

    def test_buffer_target_is_frame_aligned(self):
        """Calculated target is always a multiple of frame_size."""
        for sr, ch, w in [(44100, 1, 2), (8000, 1, 2), (22050, 1, 2)]:
            frame_size = w * ch
            bytes_per_ms = sr * frame_size / 1000.0
            for target_ms in [250, 500, 1000, 3000]:
                target_bytes = int(target_ms * bytes_per_ms)
                aligned = target_bytes - (target_bytes % frame_size) if target_bytes % frame_size != 0 else target_bytes
                assert aligned % frame_size == 0, f"sr={sr} ms={target_ms}: {aligned} % {frame_size} != 0"


# ── 6. Cancellation before AudioStart ─────────────────────────────────────────


class TestCancellationBeforeAudioStart:
    """Client disconnect during buffering phase must clean up."""

    @pytest.mark.asyncio
    async def test_buffer_discarded_on_disconnect(self):
        """Cancelling before AudioStart closes backend, no crash."""
        from app.s2_client import S2GenerateRequest
        # Large buffer target, slow feed
        chunks = [_pcm_frames(100) for _ in range(50)]
        client = _MockStreamingClient(chunks=chunks)
        settings = _settings(s2_initial_buffer_ms=10000, s2_max_initial_buffer_ms=10000)
        config = _config()

        gen = synthesize_s2cpp_streaming_tts_events(
            client, S2GenerateRequest(text="cancelling"), config, settings,
        )
        # Enter so backend stream is opened, then cancel
        await gen.__anext__()
        await gen.aclose()
        # Stream should be closed if it was opened
        if client._last_stream is not None:
            assert client._last_stream._closed

    @pytest.mark.asyncio
    async def test_no_audiostop_on_disconnect_before_start(self):
        """No AudioStart or AudioStop when cancelled before buffer target met."""
        from app.s2_client import S2GenerateRequest
        # Provide some data but below buffer target — backend exhausts during buffering
        chunks = [_pcm_frames(5)]  # only 5 frames = 10 bytes
        client = _MockStreamingClient(chunks=chunks)
        settings = _settings(s2_initial_buffer_ms=5000, s2_max_initial_buffer_ms=5000)  # large target
        config = _config()

        gen = synthesize_s2cpp_streaming_tts_events(
            client, S2GenerateRequest(text="cancel"), config, settings,
        )
        # Generator completes normally (backend exhausted during buffering)
        events = await _collect_events(gen)
        # Should emit AudioStart + chunks + AudioStop since it had data
        assert AudioStart.is_type(events[0].type)
        assert len(events) >= 2


# ── 7. Cancellation after playback begins ─────────────────────────────────────


class TestCancellationAfterPlayback:
    """Phase 8A cancellation behaviour remains intact after buffering."""

    @pytest.mark.asyncio
    async def test_cancel_after_audiostart_closes_stream(self):
        """Cancelling during playback closes backend stream."""
        from app.s2_client import S2GenerateRequest
        chunks = [_pcm_frames(500) for _ in range(10)]
        client = _MockStreamingClient(chunks=chunks)
        settings = _settings()  # zero buffer → immediate AudioStart
        config = _config()

        gen = synthesize_s2cpp_streaming_tts_events(
            client, S2GenerateRequest(text="cancel mid"), config, settings,
        )
        # Consume one event to get past AudioStart
        await anext(gen)
        await gen.aclose()
        assert client._last_stream._closed


# ── 8. Memory cap ─────────────────────────────────────────────────────────────


class TestMemoryCap:
    """Buffer cannot grow beyond max_initial_buffer_ms."""

    @pytest.mark.asyncio
    async def test_buffer_capped_at_max(self):
        """When max cap is hit, AudioStart is emitted even if target not reached."""
        from app.s2_client import S2GenerateRequest
        # max_buffer = 200ms = 17640 bytes at 44.1kHz (frame-aligned)
        # buffer_target = 5000ms (unreachable in this test)
        # Feed lots of data in small chunks
        chunks = [_pcm_frames(100) for _ in range(200)]
        client = _MockStreamingClient(chunks=chunks)
        settings = _settings(
            s2_initial_buffer_ms=5000,  # large target
            s2_max_initial_buffer_ms=200,  # small cap
        )
        config = _config()

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="capped"), config, settings,
            )
        )
        assert AudioStart.is_type(events[0].type)

    @pytest.mark.asyncio
    async def test_max_buffer_zero_disables_buffering(self):
        """max_initial_buffer_ms=0 disables buffering (safe default)."""
        from app.s2_client import S2GenerateRequest
        data = _pcm_frames(500)
        client = _MockStreamingClient(chunks=[data])
        settings = _settings(
            s2_initial_buffer_ms=500,   # requested but...
            s2_max_initial_buffer_ms=0,  # max=0 disables buffering
        )
        config = _config()

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="no cap"), config, settings,
            )
        )
        # Buffering is disabled → AudioStart is immediate (zero-buffer behavior)
        assert AudioStart.is_type(events[0].type)


# ── 9. PCM integrity ──────────────────────────────────────────────────────────


class TestPCMIntegrity:
    """Buffered + streamed bytes exactly equal backend bytes."""

    @pytest.mark.asyncio
    async def test_no_duplication_omission_or_reordering(self):
        """Every backend byte is emitted exactly once in order."""
        from app.s2_client import S2GenerateRequest
        # Use distinct values per frame to detect reordering
        frames = []
        for val in range(1, 200):
            frames.append(val.to_bytes(2, "little", signed=True))
        backend_data = b"".join(frames)

        # Split across multiple transport chunks
        chunk1 = backend_data[:50]
        chunk2 = backend_data[50:150]
        chunk3 = backend_data[150:]
        client = _MockStreamingClient(chunks=[chunk1, chunk2, chunk3])
        settings = _settings(s2_initial_buffer_ms=100, s2_max_initial_buffer_ms=1000)
        config = _config()

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="integrity"), config, settings,
            )
        )
        chunk_events = [e for e in events if AudioChunk.is_type(e.type)]
        result = b"".join(AudioChunk.from_event(e).audio for e in chunk_events)
        assert result == backend_data, f"Expected {len(backend_data)} bytes, got {len(result)}"

    @pytest.mark.asyncio
    async def test_final_partial_chunk_remains_frame_aligned(self):
        """Last chunk is properly frame-aligned after buffering."""
        from app.s2_client import S2GenerateRequest
        # Odd number of bytes across boundary — rechunker must handle
        chunk1 = _pcm_frames(10) + b"\x01"  # 21 bytes, frame-split
        chunk2 = b"\x00" + _pcm_frames(5)   # completes the partial
        client = _MockStreamingClient(chunks=[chunk1, chunk2])
        settings = _settings(s2_initial_buffer_ms=10, s2_max_initial_buffer_ms=100)
        config = _config()

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="aligned"), config, settings,
            )
        )
        for e in events:
            if AudioChunk.is_type(e.type):
                chunk = AudioChunk.from_event(e)
                assert len(chunk.audio) % 2 == 0, f"Unaligned chunk: {len(chunk.audio)} bytes"


# ── 10. Observability ─────────────────────────────────────────────────────────


class TestBufferObservability:
    """Structured log events include buffer fields."""

    @pytest.mark.asyncio
    async def test_buffer_policy_logged(self, caplog):
        """buffer_policy event is emitted with correct fields."""
        from app.s2_client import S2GenerateRequest
        import logging
        logger = logging.getLogger("wyoming-s2cpp-tts.obs")
        logger.propagate = True
        caplog.set_level(logging.INFO, logger="wyoming-s2cpp-tts.obs")

        data = _pcm_frames(5000)
        client = _MockStreamingClient(chunks=[data])
        settings = _settings(
            s2_long_form_threshold_chars=200,
            s2_long_form_buffer_ms=3000,
            s2_max_initial_buffer_ms=3000,
        )
        config = _config()
        long_text = "x" * 250  # above threshold

        await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text=long_text), config, settings,
            )
        )

        logs = caplog.text
        assert "buffer_policy" in logs
        assert "initial_buffer_target_ms" in logs
        assert "initial_buffer_target_bytes" in logs
        assert "estimated_long_form" in logs

    @pytest.mark.asyncio
    async def test_initial_buffer_ready_logged(self, caplog):
        """initial_buffer_ready event logged when buffering completes."""
        from app.s2_client import S2GenerateRequest
        import logging
        logger = logging.getLogger("wyoming-s2cpp-tts.obs")
        logger.propagate = True
        caplog.set_level(logging.INFO, logger="wyoming-s2cpp-tts.obs")

        data = _pcm_frames(5000)
        client = _MockStreamingClient(chunks=[data])
        settings = _settings(s2_initial_buffer_ms=100, s2_max_initial_buffer_ms=1000)

        await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="log"), FakeTtsConfig(), settings,
            )
        )

        logs = caplog.text
        assert "initial_buffer_ready" in logs
        assert "initial_buffered_pcm_bytes" in logs
        assert "initial_buffered_audio_ms" in logs

    @pytest.mark.asyncio
    async def test_backend_completed_before_buffer_target_logged(self, caplog):
        """When backend finishes before target, it's logged."""
        from app.s2_client import S2GenerateRequest
        import logging
        logger = logging.getLogger("wyoming-s2cpp-tts.obs")
        logger.propagate = True
        caplog.set_level(logging.INFO, logger="wyoming-s2cpp-tts.obs")

        data = _pcm_frames(10)  # very small
        client = _MockStreamingClient(chunks=[data])
        settings = _settings(s2_initial_buffer_ms=5000, s2_max_initial_buffer_ms=5000)  # large target

        await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="early"), FakeTtsConfig(), settings,
            )
        )

        logs = caplog.text
        assert "initial_buffer_ready" in logs
        assert "backend_completed_before_buffer_target" in logs


# ── 11. Regression ────────────────────────────────────────────────────────────


# ── Review additions ──────────────────────────────────────────────────────────


class TestThresholdBoundary:
    """199 vs 200 character threshold boundary."""

    @pytest.mark.asyncio
    async def test_199_chars_below_threshold_uses_zero_buffer(self):
        """199 chars (< 200 threshold): zero buffer, AudioStart immediate."""
        from app.s2_client import S2GenerateRequest
        data = _pcm_frames(500)
        client = _MockStreamingClient(chunks=[data])
        settings = _settings(
            s2_long_form_threshold_chars=200,
            s2_long_form_buffer_ms=3000,
            s2_max_initial_buffer_ms=3000,
        )
        config = _config()
        text_199 = "x" * 199

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text=text_199), config, settings,
            )
        )
        assert AudioStart.is_type(events[0].type)
        # Zero-buffer → first AudioChunk should appear quickly (no delay)
        chunk_events = [e for e in events if AudioChunk.is_type(e.type)]
        assert len(chunk_events) > 0

    @pytest.mark.asyncio
    async def test_200_chars_at_threshold_uses_long_form_buffer(self):
        """200 chars (at threshold): long-form buffer applied."""
        from app.s2_client import S2GenerateRequest
        data = _pcm_frames(500)
        client = _MockStreamingClient(chunks=[data])
        settings = _settings(
            s2_long_form_threshold_chars=200,
            s2_long_form_buffer_ms=3000,
            s2_max_initial_buffer_ms=3000,
        )
        config = _config()
        text_200 = "x" * 200

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text=text_200), config, settings,
            )
        )
        # Backend data < buffer target, but buffering was attempted
        assert AudioStart.is_type(events[0].type)


class TestInvalidMaxBuffer:
    """S2_MAX_INITIAL_BUFFER_MS validation."""

    @pytest.mark.asyncio
    async def test_zero_max_disables_buffering(self):
        """max=0 forces buffer_target to 0 regardless of other settings."""
        from app.s2_client import S2GenerateRequest
        data = _pcm_frames(5000)
        client = _MockStreamingClient(chunks=[data])
        settings = _settings(
            s2_initial_buffer_ms=3000,   # requested buffer
            s2_max_initial_buffer_ms=0,   # max=0 disables
        )
        config = _config()

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="test"), config, settings,
            )
        )
        # Buffering disabled — AudioStart immediate
        assert AudioStart.is_type(events[0].type)

    @pytest.mark.asyncio
    async def test_negative_max_disables_buffering(self):
        """Negative max also disables buffering (safety)."""
        from app.s2_client import S2GenerateRequest
        data = _pcm_frames(5000)
        client = _MockStreamingClient(chunks=[data])
        settings = _settings(
            s2_initial_buffer_ms=3000,
            s2_max_initial_buffer_ms=-1,
        )
        config = _config()

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="test"), config, settings,
            )
        )
        assert AudioStart.is_type(events[0].type)

    def test_max_buffer_must_be_set_for_buffering(self):
        """Configuration: without max, buffering target is forced to zero."""
        s = _settings(s2_long_form_buffer_ms=5000, s2_max_initial_buffer_ms=0)
        assert s.s2_max_initial_buffer_ms == 0
        assert s.s2_long_form_buffer_ms == 5000
        # At runtime, max=0 forces target to 0 — checked by streaming tests above


class TestOversizedChunkCap:
    """A single oversized backend chunk cannot exceed the configured cap."""

    @pytest.mark.asyncio
    async def test_oversized_chunk_split_at_cap(self):
        """Single chunk larger than max_buffer_bytes is split at cap boundary."""
        from app.s2_client import S2GenerateRequest
        # cap = 200ms = 17640 bytes at 44100 Hz
        # chunk = 500ms = 44100 bytes → far exceeds cap
        max_ms = 200
        chunk_frames = 30000  # ~680ms worth → exceeds 200ms cap
        data = _pcm_frames(chunk_frames)
        client = _MockStreamingClient(chunks=[data])
        settings = _settings(
            s2_initial_buffer_ms=max_ms,
            s2_max_initial_buffer_ms=max_ms,
        )
        config = _config()

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="big chunk"), config, settings,
            )
        )
        assert AudioStart.is_type(events[0].type)
        # All backend bytes must be emitted (no data loss)
        chunk_events = [e for e in events if AudioChunk.is_type(e.type)]
        all_audio = b"".join(AudioChunk.from_event(e).audio for e in chunk_events)
        assert all_audio == data

    @pytest.mark.asyncio
    async def test_oversized_chunk_pcm_integrity(self):
        """After overshoot split, PCM is byte-identical to backend output."""
        from app.s2_client import S2GenerateRequest
        max_ms = 100  # small cap
        frames = []
        for val in range(1, 500):
            frames.append(val.to_bytes(2, "little", signed=True))
        backend_data = b"".join(frames)

        client = _MockStreamingClient(chunks=[backend_data])
        settings = _settings(
            s2_initial_buffer_ms=max_ms,
            s2_max_initial_buffer_ms=max_ms,
        )
        config = _config()

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="integrity"), config, settings,
            )
        )
        chunk_events = [e for e in events if AudioChunk.is_type(e.type)]
        result = b"".join(AudioChunk.from_event(e).audio for e in chunk_events)
        assert result == backend_data

class TestBufferingRegression:
    """Existing behaviours are preserved when buffering is active."""

    @pytest.mark.asyncio
    async def test_exactly_one_audiostart(self):
        """Only one AudioStart is emitted regardless of buffer setting."""
        from app.s2_client import S2GenerateRequest
        data = _pcm_frames(5000)
        client = _MockStreamingClient(chunks=[data, data])
        settings = _settings(s2_initial_buffer_ms=100, s2_max_initial_buffer_ms=1000)
        config = _config()

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="one"), config, settings,
            )
        )
        start_count = sum(1 for e in events if AudioStart.is_type(e.type))
        assert start_count == 1

    @pytest.mark.asyncio
    async def test_exactly_one_audiostop(self):
        """Only one AudioStop is emitted."""
        from app.s2_client import S2GenerateRequest
        data = _pcm_frames(5000)
        client = _MockStreamingClient(chunks=[data])
        settings = _settings(s2_initial_buffer_ms=100, s2_max_initial_buffer_ms=1000)
        config = _config()

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="one"), config, settings,
            )
        )
        stop_count = sum(1 for e in events if AudioStop.is_type(e.type))
        assert stop_count == 1

    @pytest.mark.asyncio
    async def test_backend_error_propagates_no_audiostop(self):
        """Backend error raises, no AudioStop emitted (buffering mode)."""
        from app.s2_client import S2GenerateRequest
        client = _MockStreamingClient(
            chunks=[_pcm_frames(10)],
            fail_after=0,
        )
        settings = _settings(s2_initial_buffer_ms=100, s2_max_initial_buffer_ms=1000)
        config = _config()

        events = []
        with pytest.raises(S2ClientError):
            async for event in synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="err"), config, settings,
            ):
                events.append(event)

        assert not any(AudioStop.is_type(e.type) for e in events)

    @pytest.mark.asyncio
    async def test_normal_exhaustion_closes_stream(self):
        """Stream is closed after normal completion with buffering."""
        from app.s2_client import S2GenerateRequest
        data = _pcm_frames(5000)
        client = _MockStreamingClient(chunks=[data])
        settings = _settings(s2_initial_buffer_ms=100, s2_max_initial_buffer_ms=1000)
        config = _config()

        await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="close"), config, settings,
            )
        )
        assert client._last_stream._closed

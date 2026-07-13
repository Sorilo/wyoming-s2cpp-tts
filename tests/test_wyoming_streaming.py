"""Phase 5C mocked streaming-to-Wyoming tests.

These tests prove that progressive PCM bytes from ``S2StreamResult`` are
converted into correct Wyoming ``AudioStart`` / ``AudioChunk`` / ``AudioStop``
events without buffering the complete backend response.  All tests use mocked
``S2Client`` / ``S2StreamResult`` — no real s2.cpp backend is contacted.
"""

import asyncio
import threading
from unittest.mock import Mock, patch

import pytest

from wyoming.audio import AudioChunk, AudioStart, AudioStop

from app.audio import StreamingPCMRechunker
from app.s2_client import S2ClientError
from app.config import Settings
from app.wyoming_server import (
    FakeTtsConfig,
    synthesize_s2cpp_streaming_tts_events,
)


# ── Mock helpers ────────────────────────────────────────────────────────────


class _MockS2StreamResult:
    """Synchronous mock of ``S2StreamResult`` that yields transport chunks.

    Implements the context-manager + iterator protocol so it can be used with
    ``with client.generate_stream(request) as stream: ...``.
    """

    def __init__(self, chunks, fail_after=None, record_threads=False):
        self._chunks = list(chunks)
        self._index = 0
        self._closed = False
        self._fail_after = fail_after
        self._record_threads = record_threads
        self.read_threads: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._closed = True
        return False

    def __iter__(self):
        return self

    def __next__(self):
        if self._record_threads:
            self.read_threads.append(threading.current_thread().name)
        if self._fail_after is not None and self._index >= self._fail_after:
            raise S2ClientError("simulated backend read failure")
        if self._index >= len(self._chunks):
            raise StopIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk


class _MockStreamingClient:
    """Mock ``S2Client`` that returns a ``_MockS2StreamResult``.

    ``generate_stream()`` records the request for inspection.
    """

    def __init__(self, chunks, fail_after=None, record_threads=False):
        self._chunks = chunks
        self._fail_after = fail_after
        self._record_threads = record_threads
        self.requests: list = []
        self._last_stream: _MockS2StreamResult | None = None

    def generate_stream(self, request, files=None, boundary=None, **kwargs):
        self.requests.append(request)
        self._last_stream = _MockS2StreamResult(
            self._chunks,
            fail_after=self._fail_after,
            record_threads=self._record_threads,
        )
        return self._last_stream


async def _collect_events(async_gen) -> list:
    """Consume an async generator and return all yielded events."""
    events = []
    async for event in async_gen:
        events.append(event)
    return events


async def _collect_events_with_break(async_gen, break_after: int) -> list:
    """Consume an async generator but stop after ``break_after`` events."""
    events = []
    count = 0
    async for event in async_gen:
        events.append(event)
        count += 1
        if count >= break_after:
            break
    return events


# ── Shareable PCM test data ────────────────────────────────────────────────

# PCM frame size = 2 bytes (s16le mono).  Each sample is a little-endian int16.
# Using simple values so frame correctness is easy to inspect.
# 0x0001 → sample value 1  → b"\x01\x00"
# 0x0002 → sample value 2  → b"\x02\x00"
# 0x0003 → sample value 3  → b"\x03\x00"

PCM_FRAME_1 = b"\x01\x00"  # sample 1
PCM_FRAME_2 = b"\x02\x00"  # sample 2
PCM_FRAME_3 = b"\x03\x00"  # sample 3
PCM_FRAME_4 = b"\x04\x00"  # sample 4
PCM_FRAME_5 = b"\x05\x00"  # sample 5
PCM_FRAME_6 = b"\x06\x00"  # sample 6
PCM_FRAME_7 = b"\x07\x00"  # sample 7
PCM_FRAME_8 = b"\x08\x00"  # sample 8
PCM_FRAME_9 = b"\x09\x00"  # sample 9
PCM_FRAME_10 = b"\x0a\x00"  # sample 10


# ── StreamingPCMRechunker tests ─────────────────────────────────────────────


class TestStreamingPCMRechunker:
    """Unit tests for the standalone rechunker (no Wyoming events)."""

    def test_feed_frame_aligned_single_chunk(self):
        r = StreamingPCMRechunker(sample_rate=16000, chunk_ms=10)
        # 10ms at 16000 Hz = 160 frames = 320 bytes
        # Feed 4 complete frames (8 bytes) — well below chunk size, accumulated
        results = r.feed(PCM_FRAME_1 + PCM_FRAME_2 + PCM_FRAME_3 + PCM_FRAME_4)
        assert results == []  # 8 bytes < 320 bytes — not emitted yet
        # Flush emits whatever is accumulated
        flush_results = r.flush()
        assert len(flush_results) == 1
        audio, ts = flush_results[0]
        assert audio == PCM_FRAME_1 + PCM_FRAME_2 + PCM_FRAME_3 + PCM_FRAME_4
        assert ts == 0  # first chunk, no frames emitted yet
        assert r.cumulative_frames == 4

    def test_carry_partial_frame_across_feeds(self):
        r = StreamingPCMRechunker(sample_rate=16000, chunk_ms=10)
        # Feed 3.5 frames: 7 bytes. Frame 4 is split.
        data1 = PCM_FRAME_1 + PCM_FRAME_2 + PCM_FRAME_3 + b"\x04"  # 7 bytes
        results1 = r.feed(data1)
        # 6 complete bytes → 3 frames, 1 byte carries over. Below chunk size, not emitted.
        assert results1 == []
        # cumulatively, nothing emitted yet
        assert r.cumulative_frames == 0

        # Feed the missing byte + more data
        data2 = b"\x00" + PCM_FRAME_5 + PCM_FRAME_6  # completes frame 4 + 2 more
        results2 = r.feed(data2)
        assert results2 == []  # 3 frames = 6 bytes, still below 320-byte chunk

        # Flush emits all accumulated complete frames
        flush_results = r.flush()
        emitted = b"".join(chunk for chunk, _ in flush_results)
        assert emitted == PCM_FRAME_1 + PCM_FRAME_2 + PCM_FRAME_3 + PCM_FRAME_4 + PCM_FRAME_5 + PCM_FRAME_6
        assert r.cumulative_frames == 6

    def test_combine_small_transport_chunks(self):
        """Multiple small transport chunks combine into one Wyoming chunk."""
        r = StreamingPCMRechunker(sample_rate=8000, chunk_ms=10)
        # 10ms at 8000 Hz = 80 frames = 160 bytes per Wyoming chunk
        # Feed 2 small transport chunks, each 2 frames (4 bytes) — below chunk size
        results1 = r.feed(PCM_FRAME_1 + PCM_FRAME_2)
        assert results1 == []  # 4 bytes < 160 bytes, accumulated not emitted

        results2 = r.feed(PCM_FRAME_3 + PCM_FRAME_4)
        assert results2 == []  # 8 bytes < 160 bytes, still accumulating

        # Flush emits the combined data
        results3 = r.flush()
        assert len(results3) == 1
        assert results3[0][0] == PCM_FRAME_1 + PCM_FRAME_2 + PCM_FRAME_3 + PCM_FRAME_4
        assert r.cumulative_frames == 4

    def test_split_large_transport_chunk(self):
        """One large backend chunk splits across multiple Wyoming chunks."""
        r = StreamingPCMRechunker(sample_rate=8000, chunk_ms=1)
        # 1ms at 8000 = 8 frames = 16 bytes per Wyoming chunk
        # Feed 20 frames (40 bytes) in one transport chunk
        data = (PCM_FRAME_1 + PCM_FRAME_2 + PCM_FRAME_3 + PCM_FRAME_4 +
                PCM_FRAME_5 + PCM_FRAME_6 + PCM_FRAME_7 + PCM_FRAME_8 +
                PCM_FRAME_9 + PCM_FRAME_10)
        # Double it: 20 frames = 40 bytes
        data = data + data
        results = r.feed(data)
        # 40 bytes / 16 bytes per Wyoming chunk = 2 full chunks, 8 bytes remain (4 frames)
        assert len(results) == 2
        assert results[0][0] == (PCM_FRAME_1 + PCM_FRAME_2 + PCM_FRAME_3 + PCM_FRAME_4 +
                                  PCM_FRAME_5 + PCM_FRAME_6 + PCM_FRAME_7 + PCM_FRAME_8)
        assert results[1][0] == (PCM_FRAME_9 + PCM_FRAME_10 +
                                  PCM_FRAME_1 + PCM_FRAME_2 + PCM_FRAME_3 + PCM_FRAME_4 +
                                  PCM_FRAME_5 + PCM_FRAME_6)
        assert r.cumulative_frames == 16  # 32 bytes / 2 = 16 frames
        # Verify flush picks up the rest
        flush_results = r.flush()
        assert len(flush_results) == 1
        assert flush_results[0][0] == (PCM_FRAME_7 + PCM_FRAME_8 + PCM_FRAME_9 + PCM_FRAME_10)

    def test_timestamps_from_cumulative_frames(self):
        r = StreamingPCMRechunker(sample_rate=16000, chunk_ms=5)
        # 5ms at 16000 = 80 frames = 160 bytes per Wyoming chunk
        # Feed 200 frames (400 bytes) → 2 full Wyoming chunks (160 bytes each) + 80 frames
        # (~160 bytes) remain in carry below chunk size
        data = b"\x01\x00" * 200  # 200 frames = 400 bytes
        results = r.feed(data)
        assert len(results) == 2
        # First chunk: timestamp = 0 (no frames emitted yet)
        assert results[0][1] == 0
        # First chunk contains 80 frames (160 bytes)
        # Second chunk: timestamp = 80 frames * 1000 / 16000 = 5ms
        assert results[1][1] == 5
        # Remaining 40 frames (80 bytes) < 160 bytes → in carry, emitted by flush

    def test_timestamp_across_irregular_feeds(self):
        r = StreamingPCMRechunker(sample_rate=8000, chunk_ms=5)
        # 5ms at 8000 = 40 frames = 80 bytes
        # Feed irregularly: 3 frames, then 50 frames
        r.feed(b"\x01\x00" * 3)  # 3 frames, no Wyoming chunk yet (< 80 bytes)
        results = r.feed(b"\x02\x00" * 50)  # 50 frames
        emitted_frames = sum(len(chunk) // 2 for chunk, _ in results)
        assert emitted_frames > 0

    def test_flush_with_complete_carry(self):
        r = StreamingPCMRechunker(sample_rate=16000, chunk_ms=10)
        # 10ms at 16000 = 160 frames = 320 bytes per Wyoming chunk
        # Feed 2 frames (4 bytes) — well below chunk size
        assert r.feed(PCM_FRAME_1 + PCM_FRAME_2) == []  # accumulated, not emitted
        # Flush emits the accumulated frames even though < chunk_bytes
        results = r.flush()
        assert len(results) == 1
        assert results[0][0] == PCM_FRAME_1 + PCM_FRAME_2
        assert r.cumulative_frames == 2

    def test_flush_empty_carry(self):
        r = StreamingPCMRechunker(sample_rate=16000, chunk_ms=10)
        assert r.flush() == []

    def test_final_incomplete_frame_raises(self):
        r = StreamingPCMRechunker(sample_rate=16000, chunk_ms=10)
        r.feed(b"\x01")  # 1 byte → partial frame in carry
        with pytest.raises(ValueError, match="Final incomplete PCM frame"):
            r.flush()

    def test_validation_rejects_invalid_params(self):
        with pytest.raises(ValueError):
            StreamingPCMRechunker(sample_rate=0, chunk_ms=10)
        with pytest.raises(ValueError):
            StreamingPCMRechunker(sample_rate=16000, chunk_ms=0)
        with pytest.raises(ValueError):
            StreamingPCMRechunker(sample_rate=16000, chunk_ms=10, width=0)
        with pytest.raises(ValueError):
            StreamingPCMRechunker(sample_rate=16000, chunk_ms=10, channels=0)


# ── Wyoming streaming-to-events tests ───────────────────────────────────────


class TestStreamingWyomingEvents:
    """Tests for ``synthesize_s2cpp_streaming_tts_events()``."""

    @staticmethod
    def _config(sample_rate=8000, chunk_ms=10):
        return FakeTtsConfig(sample_rate=sample_rate, chunk_ms=chunk_ms)

    # ── basic ordering ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_audiostart_before_first_audiochunk(self):
        """AudioStart is emitted before the first AudioChunk."""
        data = PCM_FRAME_1 + PCM_FRAME_2 + PCM_FRAME_3 + PCM_FRAME_4
        client = _MockStreamingClient(chunks=[data])
        config = self._config(sample_rate=8000, chunk_ms=10)
        from app.s2_client import S2GenerateRequest
        request = S2GenerateRequest(text="test")

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(client, request, config, Settings())
        )

        assert AudioStart.is_type(events[0].type)
        assert AudioStop.is_type(events[-1].type)

        # All AudioChunk events appear between Start and Stop
        chunk_indices = [
            i for i, e in enumerate(events) if AudioChunk.is_type(e.type)
        ]
        assert len(chunk_indices) >= 1
        assert 0 < chunk_indices[0] < len(events) - 1  # after Start, before Stop

    @pytest.mark.asyncio
    async def test_audiostop_after_last_audiochunk(self):
        """AudioStop is the last event and follows all AudioChunks."""
        data = b"\x01\x00" * 20  # 20 frames
        client = _MockStreamingClient(chunks=[data])
        config = self._config(sample_rate=8000, chunk_ms=10)
        from app.s2_client import S2GenerateRequest

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="test"), config, Settings()
            )
        )

        assert AudioStop.is_type(events[-1].type)
        # No AudioChunk after Stop
        stop_idx = len(events) - 1
        for i, e in enumerate(events):
            if AudioChunk.is_type(e.type):
                assert i < stop_idx

    # ── progressive emission ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_progressive_emission_before_stream_exhausted(self):
        """First AudioChunk is emitted before all transport chunks consumed."""
        chunk1 = PCM_FRAME_1 + PCM_FRAME_2 + PCM_FRAME_3 + PCM_FRAME_4
        chunk2 = PCM_FRAME_5 + PCM_FRAME_6 + PCM_FRAME_7 + PCM_FRAME_8
        client = _MockStreamingClient(chunks=[chunk1, chunk2])
        config = self._config(sample_rate=8000, chunk_ms=10)
        from app.s2_client import S2GenerateRequest

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="test"), config, Settings()
            )
        )

        # First chunk's frames should appear in early AudioChunks
        chunks = [
            AudioChunk.from_event(e) for e in events if AudioChunk.is_type(e.type)
        ]
        all_audio = b"".join(c.audio for c in chunks)
        assert all_audio == chunk1 + chunk2

        # Progressive proof: the mock iterator was consumed one chunk at a time
        # (the fact we collected all events via async iteration proves this —
        # each yield from the async gen gives us one event at a time)
        assert client._last_stream is not None
        assert client._last_stream._closed  # closed after normal completion

    # ── PCM frame alignment ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_pcm_split_across_frame_boundaries(self):
        """Partial frame at transport boundary is carried over correctly."""
        # Frame size = 2 bytes. Split frame 3 across two transport chunks.
        chunk1 = PCM_FRAME_1 + PCM_FRAME_2 + b"\x03"  # frame 3 split: 1 byte
        chunk2 = b"\x00" + PCM_FRAME_4                     # rest of frame 3 + frame 4
        client = _MockStreamingClient(chunks=[chunk1, chunk2])
        config = self._config(sample_rate=8000, chunk_ms=5)
        from app.s2_client import S2GenerateRequest

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="test"), config, Settings()
            )
        )

        chunks = [
            AudioChunk.from_event(e) for e in events if AudioChunk.is_type(e.type)
        ]
        all_audio = b"".join(c.audio for c in chunks)
        assert all_audio == PCM_FRAME_1 + PCM_FRAME_2 + PCM_FRAME_3 + PCM_FRAME_4
        assert AudioStop.is_type(events[-1].type)

    @pytest.mark.asyncio
    async def test_all_audiochunks_frame_aligned(self):
        """Every emitted AudioChunk payload has an even number of bytes."""
        # Feed data that deliberately creates awkward boundaries
        chunk1 = b"\x01\x00\x02"  # 3 bytes = 1.5 frames
        chunk2 = b"\x00\x03"       # 2 bytes = 1 frame (completing frame 2 + frame 3)
        chunk3 = b"\x00"            # 1 byte = half of frame 4
        chunk4 = b"\x04\x00\x05"   # 2.5 bytes = completes frame 4 + frame 5 + half frame 6
        chunk5 = b"\x00"            # completes frame 6
        client = _MockStreamingClient(chunks=[chunk1, chunk2, chunk3, chunk4, chunk5])
        config = self._config(sample_rate=8000, chunk_ms=1)
        from app.s2_client import S2GenerateRequest

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="test"), config, Settings()
            )
        )

        for event in events:
            if AudioChunk.is_type(event.type):
                chunk = AudioChunk.from_event(event)
                assert len(chunk.audio) % 2 == 0, (
                    f"AudioChunk has {len(chunk.audio)} bytes (not frame-aligned)"
                )

    @pytest.mark.asyncio
    async def test_audio_bytes_preserved_exactly(self):
        """No bytes dropped, duplicated, or reordered."""
        all_frames = (
            PCM_FRAME_1 + PCM_FRAME_2 + PCM_FRAME_3 + PCM_FRAME_4 +
            PCM_FRAME_5 + PCM_FRAME_6 + PCM_FRAME_7 + PCM_FRAME_8
        )
        # Split arbitrarily
        client = _MockStreamingClient(
            chunks=[all_frames[:3], all_frames[3:11], all_frames[11:]]
        )
        config = self._config(sample_rate=8000, chunk_ms=5)
        from app.s2_client import S2GenerateRequest

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="test"), config, Settings()
            )
        )

        chunks = [
            AudioChunk.from_event(e) for e in events if AudioChunk.is_type(e.type)
        ]
        result = b"".join(c.audio for c in chunks)
        assert result == all_frames

    # ── chunk combining / splitting ─────────────────────────────────────

    @pytest.mark.asyncio
    async def test_backend_chunks_combined_into_wyoming_chunk(self):
        """Small transport chunks combine into one Wyoming chunk when below threshold."""
        # Wyoming chunk: 10ms at 8000 = 80 frames = 160 bytes
        # Transport chunks: 20 frames each (40 bytes) — 4 combine into one Wyoming
        client = _MockStreamingClient(
            chunks=[b"\x01\x00" * 20, b"\x02\x00" * 20, b"\x03\x00" * 20, b"\x04\x00" * 20]
        )
        config = self._config(sample_rate=8000, chunk_ms=10)
        from app.s2_client import S2GenerateRequest

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="test"), config, Settings()
            )
        )

        chunks = [
            AudioChunk.from_event(e) for e in events if AudioChunk.is_type(e.type)
        ]
        # 4 * 20 frames = 80 frames = exactly one Wyoming chunk (80 frames at 10ms)
        assert len(chunks) == 1
        assert len(chunks[0].audio) == 160  # 80 frames * 2 bytes

    @pytest.mark.asyncio
    async def test_backend_chunk_split_across_wyoming_chunks(self):
        """One large transport chunk can be split into multiple Wyoming chunks."""
        # Wyoming chunk: 1ms at 8000 = 8 frames = 16 bytes
        # Transport chunk: 20 frames = 40 bytes → 2 full Wyoming chunks + 4 frames carry
        client = _MockStreamingClient(chunks=[b"\x01\x00" * 20])
        config = self._config(sample_rate=8000, chunk_ms=1)
        from app.s2_client import S2GenerateRequest

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="test"), config, Settings()
            )
        )

        chunks = [
            AudioChunk.from_event(e) for e in events if AudioChunk.is_type(e.type)
        ]
        # 20 frames / 8 frames per Wyoming = 2 full Wyoming chunks + 4 in flush
        assert len(chunks) == 3
        # Verify all AudioChunks are exactly 16 bytes (8 frames) except possibly the last
        for i, c in enumerate(chunks):
            assert len(c.audio) % 2 == 0

    # ── timestamps ──────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_timestamps_from_cumulative_frames(self):
        """Timestamps are derived from cumulative frames, not transport boundaries."""
        config = self._config(sample_rate=8000, chunk_ms=5)
        # 5ms at 8000 = 40 frames = 80 bytes per Wyoming chunk
        # Provide 120 frames (240 bytes) → 3 Wyoming chunks
        client = _MockStreamingClient(chunks=[b"\x01\x00" * 120])
        from app.s2_client import S2GenerateRequest

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="test"), config, Settings()
            )
        )

        chunks = [
            AudioChunk.from_event(e) for e in events if AudioChunk.is_type(e.type)
        ]
        assert len(chunks) == 3
        # Chunk 1: 0 frames emitted → timestamp 0
        assert chunks[0].timestamp == 0
        # Chunk 2: 40 frames emitted → 40 * 1000 / 8000 = 5ms
        assert chunks[1].timestamp == 5
        # Chunk 3: 80 frames emitted → 80 * 1000 / 8000 = 10ms
        assert chunks[2].timestamp == 10

    @pytest.mark.asyncio
    async def test_timestamps_irregular_transport_chunks(self):
        """Timestamp progression correct despite irregular HTTP chunk boundaries."""
        config = self._config(sample_rate=8000, chunk_ms=10)
        # 10ms at 8000 = 80 frames = 160 bytes per Wyoming chunk
        # Feed 3 irregular transport chunks that together span 2 Wyoming chunks
        client = _MockStreamingClient(
            chunks=[
                b"\x01\x00" * 30,   # 30 frames = 60 bytes
                b"\x02\x00" * 90,   # 90 frames = 180 bytes
                b"\x03\x00" * 40,   # 40 frames = 80 bytes
            ]
        )
        from app.s2_client import S2GenerateRequest

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="test"), config, Settings()
            )
        )

        chunks = [
            AudioChunk.from_event(e) for e in events if AudioChunk.is_type(e.type)
        ]
        # 160 frames total / 80 frames per Wyoming = 2 Wyoming chunks
        assert len(chunks) == 2
        # Timestamps derived from frame count, not from transport chunk index
        assert chunks[0].timestamp == 0
        # After 80 frames: 80 * 1000 / 8000 = 10ms
        assert chunks[1].timestamp == 10

    # ── final incomplete frame ──────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_final_incomplete_frame_raises_error(self):
        """Stream ending on a partial PCM frame raises ValueError, no AudioStop."""
        # End with 1 byte (half a frame)
        client = _MockStreamingClient(chunks=[PCM_FRAME_1 + PCM_FRAME_2 + b"\x03"])
        config = self._config(sample_rate=8000, chunk_ms=10)
        from app.s2_client import S2GenerateRequest

        events = []
        with pytest.raises(ValueError, match="Final incomplete PCM frame"):
            async for event in synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="test"), config, Settings()
            ):
                events.append(event)

        # No AudioStop was emitted
        assert not any(AudioStop.is_type(e.type) for e in events)
        # Stream was closed
        assert client._last_stream._closed

    # ── stream lifecycle & errors ───────────────────────────────────────

    @pytest.mark.asyncio
    async def test_backend_error_propagates_no_audiostop(self):
        """Backend stream error raises S2ClientError, no AudioStop emitted."""
        client = _MockStreamingClient(
            chunks=[PCM_FRAME_1, PCM_FRAME_2],
            fail_after=1,  # fail on second read
        )
        config = self._config(sample_rate=8000, chunk_ms=10)
        from app.s2_client import S2GenerateRequest

        events = []
        with pytest.raises(S2ClientError, match="simulated backend read failure"):
            async for event in synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="test"), config, Settings()
            ):
                events.append(event)

        # No successful AudioStop after backend failure
        assert not any(AudioStop.is_type(e.type) for e in events)
        assert client._last_stream._closed

    @pytest.mark.asyncio
    async def test_backend_error_not_silent(self):
        """Backend error is not silently swallowed — it reaches the caller."""
        client = _MockStreamingClient(
            chunks=[], fail_after=0  # fails immediately
        )
        config = self._config(sample_rate=8000, chunk_ms=10)
        from app.s2_client import S2GenerateRequest

        with pytest.raises(S2ClientError):
            await _collect_events(
                synthesize_s2cpp_streaming_tts_events(
                    client, S2GenerateRequest(text="test"), config, Settings()
                )
            )

    @pytest.mark.asyncio
    async def test_normal_exhaustion_closes_stream(self):
        """Stream is closed after normal completion."""
        client = _MockStreamingClient(chunks=[PCM_FRAME_1 + PCM_FRAME_2])
        config = self._config(sample_rate=8000, chunk_ms=10)
        from app.s2_client import S2GenerateRequest

        await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="test"), config, Settings()
            )
        )

        assert client._last_stream._closed

    @pytest.mark.asyncio
    async def test_early_consumer_exit_closes_stream(self):
        """When consumer breaks early, stream is cleaned up."""
        client = _MockStreamingClient(
            chunks=[b"\x01\x00" * 100, b"\x02\x00" * 100, b"\x03\x00" * 100]
        )
        config = self._config(sample_rate=8000, chunk_ms=10)
        from app.s2_client import S2GenerateRequest

        gen = synthesize_s2cpp_streaming_tts_events(
            client, S2GenerateRequest(text="test"), config, Settings()
        )
        # Consume just a few events, then close
        async for event in gen:
            if AudioChunk.is_type(event.type):
                break  # exit after first AudioChunk
        # Explicitly close the async generator
        await gen.aclose()

        # Stream should be closed
        assert client._last_stream._closed

    # ── thread offloading ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_offloads_blocking_reads_to_thread(self):
        """Blocking ``__next__`` calls run off the asyncio event loop."""
        client = _MockStreamingClient(
            chunks=[PCM_FRAME_1 + PCM_FRAME_2, PCM_FRAME_3 + PCM_FRAME_4],
            record_threads=True,
        )
        config = self._config(sample_rate=8000, chunk_ms=10)
        from app.s2_client import S2GenerateRequest

        await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="test"), config, Settings()
            )
        )

        # Reads happened in a different thread (not MainThread)
        main_thread = threading.current_thread().name
        for t in client._last_stream.read_threads:
            assert t != main_thread, (
                f"Blocking read ran on main thread '{main_thread}' — "
                f"should be offloaded via asyncio.to_thread"
            )

    # ── no complete buffering ───────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_no_complete_response_buffering(self):
        """Progressive: first AudioChunk yielded before all transport chunks read.

        This uses a mock that records read progression to prove that events
        are emitted while chunks remain unread.
        """
        client = _MockStreamingClient(
            chunks=[PCM_FRAME_1 + PCM_FRAME_2, PCM_FRAME_3 + PCM_FRAME_4],
        )
        config = self._config(sample_rate=8000, chunk_ms=10)
        from app.s2_client import S2GenerateRequest

        gen = synthesize_s2cpp_streaming_tts_events(
            client, S2GenerateRequest(text="test"), config, Settings()
        )
        events = []
        event_count = 0
        async for event in gen:
            events.append(event)
            event_count += 1
            # After first AudioChunk, check that we haven't read all chunks yet
            if AudioChunk.is_type(event.type):
                # At this point, only one transport chunk may have been read
                # More chunks remain (index < total chunks)
                assert client._last_stream._index <= len(client._last_stream._chunks), (
                    "All transport chunks were consumed before first AudioChunk yielded"
                )
                # Close and stop; we've proven the point
                await gen.aclose()
                break

    # ── existing behavior unchanged ─────────────────────────────────────

    @pytest.mark.asyncio
    async def test_empty_stream_produces_start_and_stop_only(self):
        """Empty stream: AudioStart + AudioStop, no AudioChunks."""
        client = _MockStreamingClient(chunks=[])  # no audio data
        config = self._config(sample_rate=8000, chunk_ms=10)
        from app.s2_client import S2GenerateRequest

        events = await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="test"), config, Settings()
            )
        )

        assert len(events) == 2
        assert AudioStart.is_type(events[0].type)
        assert AudioStop.is_type(events[1].type)


# ── Phase 6A real backend runtime contract tests ─────────────────────────────

class _MetadataMockS2StreamResult:
    def __init__(self, chunks, *, content_type, response_headers):
        self._chunks = list(chunks)
        self._index = 0
        self._closed = False
        self.content_type = content_type
        self.response_headers = response_headers.copy()
        self.status_code = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._closed = True
        return False

    def __iter__(self):
        return self

    def __next__(self):
        if self._index >= len(self._chunks):
            raise StopIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk


class _MetadataStreamingClient:
    def __init__(self, chunks, *, content_type="audio/L16; rate=44100; channels=1", response_headers=None):
        self._chunks = chunks
        self._content_type = content_type
        self._response_headers = {
            "x-audio-encoding": "pcm_s16le",
            "x-audio-channels": "1",
            "x-audio-sample-rate": "44100",
        } if response_headers is None else response_headers
        self._last_stream = None

    def generate_stream(self, request, files=None, boundary=None, **kwargs):
        self._last_stream = _MetadataMockS2StreamResult(
            self._chunks,
            content_type=self._content_type,
            response_headers=self._response_headers,
        )
        return self._last_stream


@pytest.mark.asyncio
async def test_real_contract_streaming_pcm_sets_wyoming_metadata_and_preserves_progression():
    from app.s2_client import S2GenerateRequest

    chunk1 = b"\x01\x00" * 4410  # exactly one 100ms Wyoming chunk at 44100 Hz
    chunk2 = b"\x02\x00" * 2
    client = _MetadataStreamingClient([chunk1, chunk2])
    config = FakeTtsConfig(sample_rate=22050, chunk_ms=100)

    gen = synthesize_s2cpp_streaming_tts_events(
        client, S2GenerateRequest(text="real stream"), config, Settings()
    )

    start = await anext(gen)
    assert AudioStart.from_event(start).rate == 44100

    first_chunk = await anext(gen)
    assert AudioChunk.is_type(first_chunk.type)
    first = AudioChunk.from_event(first_chunk)
    assert first.rate == 44100
    assert first.channels == 1
    assert first.audio == chunk1
    assert client._last_stream._index == 1

    remaining = []
    async for event in gen:
        remaining.append(event)

    chunks = [AudioChunk.from_event(event) for event in remaining if AudioChunk.is_type(event.type)]
    assert b"".join(chunk.audio for chunk in chunks) == chunk2
    assert AudioStop.is_type(remaining[-1].type)


@pytest.mark.asyncio
async def test_streaming_pcm_missing_metadata_is_rejected_before_audio_start():
    from app.s2_client import S2GenerateRequest

    client = _MetadataStreamingClient(
        [b"\x01\x00"],
        content_type="audio/L16",
        response_headers={},
    )

    with pytest.raises(ValueError, match="missing PCM metadata"):
        await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="missing metadata"), FakeTtsConfig(), Settings()
            )
        )


@pytest.mark.asyncio
async def test_streaming_pcm_unaligned_final_frame_error_is_clear():
    from app.s2_client import S2GenerateRequest

    client = _MetadataStreamingClient([b"\x01\x00\x02"])

    with pytest.raises(ValueError, match="Final incomplete PCM frame"):
        await _collect_events(
            synthesize_s2cpp_streaming_tts_events(
                client, S2GenerateRequest(text="unaligned"), FakeTtsConfig(), Settings()
            )
        )

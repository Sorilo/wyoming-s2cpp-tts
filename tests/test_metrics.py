"""Phase 5D metrics and structured tracing tests.

All tests use mocked clients, deterministic clocks, and in-process
inspection — no real s2.cpp backend is contacted.
"""

from __future__ import annotations

import asyncio
import itertools
import uuid

import pytest

from app.metrics import MetricsCollector, SynthesisMetrics


# ── fake deterministic clock ─────────────────────────────────────────────────


def _stepping_clock(start_ns: int = 1_000_000_000, step_ns: int = 1_000_000):
    """Return a callable that increments monotonically on each call."""
    state = itertools.count(start_ns, step_ns)
    return lambda: next(state)


# ── MetricsCollector unit tests ──────────────────────────────────────────────


class TestMetricsCollector:
    def test_generates_request_id_when_none_supplied(self):
        c = MetricsCollector("fake", "fake")
        metrics = c.finalize("success")
        # Valid hex UUID
        uuid.UUID(hex=metrics.request_id)
        assert metrics.trace_id is None

    def test_preserves_supplied_request_and_trace_id(self):
        c = MetricsCollector(
            "s2cpp", "streaming",
            request_id="req-abc", trace_id="trace-xyz",
        )
        metrics = c.finalize("success")
        assert metrics.request_id == "req-abc"
        assert metrics.trace_id == "trace-xyz"

    def test_backend_and_mode_preserved(self):
        c = MetricsCollector("s2cpp", "buffered")
        metrics = c.finalize("success")
        assert metrics.backend_type == "s2cpp"
        assert metrics.synthesis_mode == "buffered"

    def test_request_start_recorded_on_init(self):
        clock = _stepping_clock(1000, 100)
        c = MetricsCollector("fake", "fake", clock=clock)
        # First clock call happened in __init__
        assert c.request_start_ns == 1000

    def test_first_backend_data_recorded_once(self):
        clock = _stepping_clock(1000, 100)
        c = MetricsCollector("s2cpp", "streaming", clock=clock)
        c.record_first_backend_data()
        assert c.first_backend_data_ns == 1100  # second call
        c.record_first_backend_data()  # no-op
        assert c.first_backend_data_ns == 1100  # unchanged

    def test_first_audio_chunk_recorded_once(self):
        clock = _stepping_clock(1000, 100)
        c = MetricsCollector("s2cpp", "streaming", clock=clock)
        c.record_first_audio_chunk()
        assert c.first_audio_chunk_ns == 1100
        c.record_first_audio_chunk()
        assert c.first_audio_chunk_ns == 1100

    def test_emitted_chunk_accumulation(self):
        c = MetricsCollector("fake", "fake")
        c.record_emitted_chunk(100)
        c.record_emitted_chunk(200)
        c.record_emitted_chunk(50)
        assert c.total_emitted_bytes == 350
        assert c.emitted_chunk_count == 3

    def test_finalize_returns_immutable_snapshot(self):
        clock = _stepping_clock(1000, 100)
        c = MetricsCollector("fake", "fake", clock=clock)
        c.record_first_backend_data()
        c.record_emitted_chunk(80)
        c.record_emitted_chunk(80)

        metrics = c.finalize("success")
        # Verify frozen snapshot values
        assert metrics.request_start_ns == 1000
        assert metrics.first_backend_data_ns == 1100
        assert metrics.first_audio_chunk_ns is None  # never recorded
        assert metrics.total_emitted_bytes == 160
        assert metrics.emitted_chunk_count == 2
        assert metrics.terminal_status == "success"
        assert metrics.error_type is None
        assert metrics.duration_ns == metrics.terminal_ns - metrics.request_start_ns
        assert metrics.duration_ns > 0

    def test_finalize_error_status(self):
        c = MetricsCollector("s2cpp", "buffered")
        metrics = c.finalize("error", error_type="S2ClientError")
        assert metrics.terminal_status == "error"
        assert metrics.error_type == "S2ClientError"

    def test_finalize_cancelled_status(self):
        c = MetricsCollector("s2cpp", "streaming")
        metrics = c.finalize("cancelled")
        assert metrics.terminal_status == "cancelled"
        assert metrics.error_type is None

    def test_double_finalize_raises(self):
        c = MetricsCollector("fake", "fake")
        c.finalize("success")
        with pytest.raises(RuntimeError, match="already finalized"):
            c.finalize("success")

    def test_empty_audio_zero_bytes_zero_chunks(self):
        c = MetricsCollector("s2cpp", "buffered")
        metrics = c.finalize("success")
        assert metrics.total_emitted_bytes == 0
        assert metrics.emitted_chunk_count == 0
        assert metrics.first_audio_chunk_ns is None
        assert metrics.first_backend_data_ns is None

    def test_no_backend_data_leaves_timestamp_none(self):
        c = MetricsCollector("s2cpp", "buffered")
        # Never called record_first_backend_data()
        metrics = c.finalize("success")
        assert metrics.first_backend_data_ns is None

    def test_no_audio_chunk_leaves_timestamp_none(self):
        c = MetricsCollector("fake", "fake")
        c.record_first_backend_data()
        # Never called record_first_audio_chunk()
        metrics = c.finalize("success")
        assert metrics.first_audio_chunk_ns is None

    # ── timestamp ordering ───────────────────────────────────────────────

    def test_timestamps_non_decreasing_with_injected_clock(self):
        clock = _stepping_clock(1000, 100)
        c = MetricsCollector("s2cpp", "streaming", clock=clock)
        # request_start = 1000
        c.record_first_backend_data()  # 1100
        c.record_first_audio_chunk()   # 1200
        c.record_emitted_chunk(80)
        metrics = c.finalize("success")  # 1300

        assert metrics.request_start_ns == 1000
        assert metrics.first_backend_data_ns == 1100
        assert metrics.first_audio_chunk_ns == 1200
        assert metrics.terminal_ns == 1300
        assert 1000 <= 1100 <= 1200 <= 1300

    def test_production_equal_timestamps_permitted(self):
        """When two events happen within one tick, equal timestamps are OK."""
        clock = _stepping_clock(1000, 1)
        c = MetricsCollector("s2cpp", "streaming", clock=clock)
        c.record_first_backend_data()
        # Inject the same clock value
        class _FixedClock:
            def __init__(self, val):
                self.val = val
            def __call__(self):
                return self.val
        fixed = _FixedClock(1500)
        c._clock = fixed
        c.record_first_audio_chunk()
        c.record_emitted_chunk(100)
        metrics = c.finalize("success")
        assert metrics.first_backend_data_ns == 1001
        assert metrics.first_audio_chunk_ns == 1500
        assert metrics.terminal_ns == 1500
        # Equal is OK (non-decreasing, not strictly increasing)
        assert metrics.first_audio_chunk_ns <= metrics.terminal_ns

    # ── concurrency safety ────────────────────────────────────────────────

    def test_concurrent_collectors_independent(self):
        clock1 = _stepping_clock(1000, 100)
        clock2 = _stepping_clock(5000, 200)

        c1 = MetricsCollector("fake", "fake", clock=clock1)
        c2 = MetricsCollector("s2cpp", "streaming", clock=clock2)

        c1.record_emitted_chunk(100)
        c2.record_emitted_chunk(200)

        m1 = c1.finalize("success")
        m2 = c2.finalize("success")

        assert m1.request_start_ns == 1000
        assert m2.request_start_ns == 5000
        assert m1.total_emitted_bytes == 100
        assert m2.total_emitted_bytes == 200

    # ── privacy ───────────────────────────────────────────────────────────

    def test_metrics_do_not_contain_sensitive_data(self):
        """Metrics snapshot must not expose request text, audio, or credentials."""
        c = MetricsCollector("s2cpp", "streaming")
        c.record_emitted_chunk(200)
        metrics = c.finalize("success")
        fields = {
            f: getattr(metrics, f)
            for f in [
                "request_id", "trace_id", "backend_type", "synthesis_mode",
                "request_start_ns", "first_backend_data_ns",
                "first_audio_chunk_ns", "terminal_ns",
                "total_emitted_bytes", "emitted_chunk_count",
                "terminal_status", "error_type",
            ]
        }
        # None of these fields contain raw text or audio bytes
        for key, value in fields.items():
            if value is not None and not isinstance(value, (int, str)):
                pytest.fail(f"{key} has unexpected type {type(value)}")


# ── Fake synthesis metrics tests ─────────────────────────────────────────────


class TestFakeSynthesisMetrics:
    """Metrics collected during ``synthesize_fake_tts_events()``."""

    @staticmethod
    def _config(sample_rate=8000, duration_ms=120, chunk_ms=10):
        from app.wyoming_server import FakeTtsConfig
        return FakeTtsConfig(sample_rate=sample_rate, duration_ms=duration_ms, chunk_ms=chunk_ms)

    def test_fake_produces_completed_metrics(self):
        from app.wyoming_server import synthesize_fake_tts_events
        from wyoming.audio import AudioChunk

        clock = _stepping_clock(1000, 100)
        metrics = MetricsCollector("fake", "fake", clock=clock)
        config = self._config(sample_rate=8000, duration_ms=120, chunk_ms=10)

        events = synthesize_fake_tts_events("hello", config=config, metrics=metrics)

        assert metrics.total_emitted_bytes > 0
        assert metrics.emitted_chunk_count > 0

        chunks = [e for e in events if AudioChunk.is_type(e.type)]
        total_bytes = sum(len(AudioChunk.from_event(e).audio) for e in chunks)
        assert metrics.total_emitted_bytes == total_bytes
        assert metrics.emitted_chunk_count == len(chunks)

        # Timestamps present
        assert metrics.first_backend_data_ns is not None
        assert metrics.first_audio_chunk_ns is not None
        # Verify ordering: start <= backend <= first_chunk
        assert metrics.request_start_ns <= metrics.first_backend_data_ns
        assert metrics.first_backend_data_ns <= metrics.first_audio_chunk_ns

    def test_fake_metrics_backend_type_and_mode(self):
        from app.wyoming_server import synthesize_fake_tts_events

        metrics = MetricsCollector("fake", "fake")
        synthesize_fake_tts_events("hello", config=self._config(), metrics=metrics)

        # Metrics are finalized by the function
        snapshot = _finalized(metrics)
        assert snapshot.backend_type == "fake"
        assert snapshot.synthesis_mode == "fake"
        assert snapshot.terminal_status == "success"

    def test_fake_metrics_duration_positive(self):
        from app.wyoming_server import synthesize_fake_tts_events

        clock = _stepping_clock(1000, 100)
        metrics = MetricsCollector("fake", "fake", clock=clock)
        synthesize_fake_tts_events("hello", config=self._config(), metrics=metrics)

        snapshot = _finalized(metrics)
        assert snapshot.duration_ns > 0

    def test_fake_metrics_request_id_generated(self):
        from app.wyoming_server import synthesize_fake_tts_events

        metrics = MetricsCollector("fake", "fake")
        synthesize_fake_tts_events("hello", config=self._config(), metrics=metrics)

        snapshot = _finalized(metrics)
        uuid.UUID(hex=snapshot.request_id)

    def test_fake_metrics_preserves_supplied_ids(self):
        from app.wyoming_server import synthesize_fake_tts_events

        metrics = MetricsCollector(
            "fake", "fake", request_id="my-req", trace_id="my-trace",
        )
        synthesize_fake_tts_events("hello", config=self._config(), metrics=metrics)

        snapshot = _finalized(metrics)
        assert snapshot.request_id == "my-req"
        assert snapshot.trace_id == "my-trace"

    def test_fake_metrics_correct_bytes_and_chunks(self):
        from app.wyoming_server import synthesize_fake_tts_events
        from wyoming.audio import AudioChunk

        config = self._config(sample_rate=8000, duration_ms=120, chunk_ms=10)
        # 120ms at 8000 Hz = 960 frames = 1920 bytes. chunk_ms=10 → 80 frames/chunk = 160 bytes/chunk. 12 chunks.

        metrics = MetricsCollector("fake", "fake")
        events = synthesize_fake_tts_events("hello", config=config, metrics=metrics)

        chunks = [e for e in events if AudioChunk.is_type(e.type)]
        expected_bytes = sum(len(AudioChunk.from_event(e).audio) for e in chunks)
        assert expected_bytes == 1920
        assert len(chunks) == 12

        snapshot = _finalized(metrics)
        assert snapshot.total_emitted_bytes == expected_bytes
        assert snapshot.emitted_chunk_count == len(chunks)


# ── Buffered s2.cpp synthesis metrics tests ──────────────────────────────────


class TestBufferedS2CppMetrics:
    """Metrics collected during ``synthesize_s2cpp_tts_events()``."""

    @staticmethod
    def _config(sample_rate=8000, duration_ms=120, chunk_ms=10):
        from app.wyoming_server import FakeTtsConfig
        return FakeTtsConfig(sample_rate=sample_rate, duration_ms=duration_ms, chunk_ms=chunk_ms)

    def _recording_client(self, audio: bytes):
        from app.s2_client import S2GenerateResult

        class _Client:
            def __init__(self):
                self.requests = []
            def generate_multipart(self, request):
                self.requests.append(request)
                return S2GenerateResult(
                    audio=audio,
                    content_type="audio/L16; rate=44100; channels=1",
                    response_headers={
                        "x-audio-encoding": "pcm_s16le",
                        "x-audio-sample-rate": "44100",
                        "x-audio-channels": "1",
                    },
                )
        return _Client()

    def test_buffered_produces_completed_metrics(self):
        from app.config import Settings
        from app.wyoming_server import synthesize_s2cpp_tts_events

        pcm = b"\x01\x00" * 100
        client = self._recording_client(audio=pcm)
        settings = Settings(tts_backend="s2cpp")
        clock = _stepping_clock(1000, 100)
        metrics = MetricsCollector("s2cpp", "buffered", clock=clock)

        synthesize_s2cpp_tts_events(
            "hello", client=client, settings=settings,
            config=self._config(), metrics=metrics,
        )

        snapshot = _finalized(metrics)
        assert snapshot.backend_type == "s2cpp"
        assert snapshot.synthesis_mode == "buffered"
        assert snapshot.terminal_status == "success"
        assert snapshot.total_emitted_bytes == 200  # 100 frames * 2 bytes
        assert snapshot.emitted_chunk_count > 0
        assert snapshot.first_backend_data_ns is not None
        assert snapshot.first_audio_chunk_ns is not None
        assert snapshot.duration_ns > 0

    def test_buffered_first_backend_data_is_buffered_completion_time(self):
        """Buffered mode records timestamp when response is available, not first network byte."""
        from app.config import Settings
        from app.wyoming_server import synthesize_s2cpp_tts_events

        pcm = b"\x01\x00" * 50
        client = self._recording_client(audio=pcm)
        settings = Settings(tts_backend="s2cpp")
        clock = _stepping_clock(1000, 100)
        metrics = MetricsCollector("s2cpp", "buffered", clock=clock)

        # request_start = 1000
        synthesize_s2cpp_tts_events(
            "hello", client=client, settings=settings,
            config=self._config(), metrics=metrics,
        )
        # first_backend_data = 1100 (recorded after client.generate returns)
        snapshot = _finalized(metrics)
        assert snapshot.first_backend_data_ns == 1100

    def test_buffered_empty_audio_no_backend_data_timestamp(self):
        from app.config import Settings
        from app.wyoming_server import synthesize_s2cpp_tts_events

        client = self._recording_client(audio=b"")
        settings = Settings(tts_backend="s2cpp")
        clock = _stepping_clock(1000, 100)
        metrics = MetricsCollector("s2cpp", "buffered", clock=clock)

        with pytest.raises(ValueError, match="empty PCM response"):
            synthesize_s2cpp_tts_events(
                "hello", client=client, settings=settings,
                config=self._config(), metrics=metrics,
            )

        snapshot = _finalized(metrics)
        assert snapshot.terminal_status == "error"
        assert snapshot.error_type == "ValueError"
        assert snapshot.first_backend_data_ns is None
        assert snapshot.total_emitted_bytes == 0
        assert snapshot.emitted_chunk_count == 0
        assert snapshot.first_audio_chunk_ns is None

    def test_buffered_error_after_backend_exception(self):
        from app.config import Settings
        from app.s2_client import S2ClientError
        from app.wyoming_server import synthesize_s2cpp_tts_events

        class _FailingClient:
            def generate_multipart(self, request):
                raise S2ClientError("backend down")

        client = _FailingClient()
        settings = Settings(tts_backend="s2cpp")
        clock = _stepping_clock(1000, 100)
        metrics = MetricsCollector("s2cpp", "buffered", clock=clock)

        with pytest.raises(S2ClientError):
            synthesize_s2cpp_tts_events(
                "hello", client=client, settings=settings,
                config=self._config(), metrics=metrics,
            )

        snapshot = _finalized(metrics)
        assert snapshot.terminal_status == "error"
        assert snapshot.error_type == "S2ClientError"
        assert snapshot.total_emitted_bytes == 0
        assert snapshot.emitted_chunk_count == 0
        assert snapshot.first_backend_data_ns is None
        assert snapshot.first_audio_chunk_ns is None


# ── Streaming s2.cpp synthesis metrics tests ─────────────────────────────────


class TestStreamingS2CppMetrics:
    """Metrics collected during ``synthesize_s2cpp_streaming_tts_events()``."""

    @staticmethod
    def _config(sample_rate=8000, chunk_ms=5):
        from app.wyoming_server import FakeTtsConfig
        return FakeTtsConfig(sample_rate=sample_rate, chunk_ms=chunk_ms)

    def _mock_client(self, chunks, fail_after=None):
        from app.s2_client import S2ClientError

        class _MockStream:
            def __init__(self, chunks, fail_after):
                self._chunks = list(chunks)
                self._idx = 0
                self._closed = False
                self._fail_after = fail_after

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self._closed = True
                return False

            def __iter__(self):
                return self

            def __next__(self):
                if self._fail_after is not None and self._idx >= self._fail_after:
                    raise S2ClientError("simulated read failure")
                if self._idx >= len(self._chunks):
                    raise StopIteration
                chunk = self._chunks[self._idx]
                self._idx += 1
                return chunk

        class _Client:
            def __init__(self, chunks, fail_after):
                self._chunks = chunks
                self._fail_after = fail_after

            def generate_stream(self, request, files=None, boundary=None):
                return _MockStream(self._chunks, self._fail_after)

        return _Client(chunks, fail_after)

    async def _collect(self, async_gen):
        events = []
        async for event in async_gen:
            events.append(event)
        return events

    @pytest.mark.asyncio
    async def test_streaming_produces_completed_metrics(self):
        from app.s2_client import S2GenerateRequest
        from app.wyoming_server import synthesize_s2cpp_streaming_tts_events
        from wyoming.audio import AudioChunk

        # 5ms at 8000 = 40 frames = 80 bytes per Wyoming chunk
        data = b"\x01\x00" * 120  # 120 frames = 240 bytes → 3 Wyoming chunks
        client = self._mock_client(chunks=[data])
        config = self._config(sample_rate=8000, chunk_ms=5)

        clock = _stepping_clock(1000, 100)
        metrics = MetricsCollector("s2cpp", "streaming", clock=clock)
        request = S2GenerateRequest(text="test")

        events = await self._collect(
            synthesize_s2cpp_streaming_tts_events(client, request, config, metrics=metrics)
        )

        snapshot = _finalized(metrics)
        assert snapshot.backend_type == "s2cpp"
        assert snapshot.synthesis_mode == "streaming"
        assert snapshot.terminal_status == "success"
        assert snapshot.first_backend_data_ns is not None
        assert snapshot.first_audio_chunk_ns is not None

        chunks = [e for e in events if AudioChunk.is_type(e.type)]
        expected_bytes = sum(len(AudioChunk.from_event(e).audio) for e in chunks)
        assert snapshot.total_emitted_bytes == expected_bytes
        assert snapshot.emitted_chunk_count == len(chunks)
        assert snapshot.duration_ns > 0

    @pytest.mark.asyncio
    async def test_streaming_first_backend_chunk_timestamp(self):
        """First non-empty backend chunk is timestamped, not transport chunk boundary."""
        from app.s2_client import S2GenerateRequest
        from app.wyoming_server import synthesize_s2cpp_streaming_tts_events

        # First chunk is empty, second has data
        client = self._mock_client(chunks=[b"", b"\x01\x00" * 80])
        config = self._config(sample_rate=8000, chunk_ms=5)

        clock = _stepping_clock(1000, 100)
        metrics = MetricsCollector("s2cpp", "streaming", clock=clock)
        request = S2GenerateRequest(text="test")

        await self._collect(
            synthesize_s2cpp_streaming_tts_events(client, request, config, metrics=metrics)
        )

        snapshot = _finalized(metrics)
        # request_start=1000, empty chunk skipped (no clock call),
        # non-empty chunk → clock() → 1100
        assert snapshot.first_backend_data_ns == 1100

    @pytest.mark.asyncio
    async def test_streaming_timestamp_ordering(self):
        from app.s2_client import S2GenerateRequest
        from app.wyoming_server import synthesize_s2cpp_streaming_tts_events

        data = b"\x01\x00" * 200
        client = self._mock_client(chunks=[data])
        config = self._config(sample_rate=8000, chunk_ms=5)

        clock = _stepping_clock(1000, 100)
        metrics = MetricsCollector("s2cpp", "streaming", clock=clock)
        request = S2GenerateRequest(text="test")

        await self._collect(
            synthesize_s2cpp_streaming_tts_events(client, request, config, metrics=metrics)
        )

        snapshot = _finalized(metrics)
        assert snapshot.request_start_ns == 1000
        assert snapshot.first_backend_data_ns is not None
        assert snapshot.first_audio_chunk_ns is not None
        assert snapshot.request_start_ns <= snapshot.first_backend_data_ns
        assert snapshot.first_backend_data_ns <= snapshot.first_audio_chunk_ns
        assert snapshot.first_audio_chunk_ns <= snapshot.terminal_ns

    @pytest.mark.asyncio
    async def test_streaming_bytes_and_chunks_after_rechunking(self):
        """Total bytes match emitted AudioChunks, not backend transport boundaries."""
        from app.s2_client import S2GenerateRequest
        from app.wyoming_server import synthesize_s2cpp_streaming_tts_events
        from wyoming.audio import AudioChunk

        # Split across multiple transport chunks, some partial
        chunk1 = b"\x01\x00" * 10 + b"\x02"     # 10 full frames + 1 byte
        chunk2 = b"\x00" + b"\x03\x00" * 9      # rest of frame 10 + 9 frames
        client = self._mock_client(chunks=[chunk1, chunk2])
        config = self._config(sample_rate=8000, chunk_ms=5)

        metrics = MetricsCollector("s2cpp", "streaming")
        request = S2GenerateRequest(text="test")

        events = await self._collect(
            synthesize_s2cpp_streaming_tts_events(client, request, config, metrics=metrics)
        )

        chunks = [e for e in events if AudioChunk.is_type(e.type)]
        emitted = b"".join(AudioChunk.from_event(e).audio for e in chunks)
        # 20 frames * 2 bytes = 40 bytes total
        assert len(emitted) == 40

        snapshot = _finalized(metrics)
        assert snapshot.total_emitted_bytes == 40

    @pytest.mark.asyncio
    async def test_streaming_error_before_backend_data(self):
        """Error before any non-empty backend data: no first-data/chunk timestamps."""
        from app.s2_client import S2ClientError, S2GenerateRequest
        from app.wyoming_server import synthesize_s2cpp_streaming_tts_events

        # Fail immediately — no non-empty chunks
        client = self._mock_client(chunks=[], fail_after=0)
        config = self._config()

        clock = _stepping_clock(1000, 100)
        metrics = MetricsCollector("s2cpp", "streaming", clock=clock)
        request = S2GenerateRequest(text="test")

        with pytest.raises(S2ClientError):
            await self._collect(
                synthesize_s2cpp_streaming_tts_events(client, request, config, metrics=metrics)
            )

        snapshot = _finalized(metrics)
        assert snapshot.terminal_status == "error"
        assert snapshot.error_type == "S2ClientError"
        assert snapshot.first_backend_data_ns is None
        assert snapshot.first_audio_chunk_ns is None
        assert snapshot.total_emitted_bytes == 0
        assert snapshot.emitted_chunk_count == 0

    @pytest.mark.asyncio
    async def test_streaming_error_after_chunks_preserves_partial(self):
        """Error after some chunks: partial totals recorded, status is error."""
        from app.s2_client import S2ClientError, S2GenerateRequest
        from app.wyoming_server import synthesize_s2cpp_streaming_tts_events
        from wyoming.audio import AudioChunk

        # 200 frames → Wyoming chunks emitted, then fail_after=2 means
        # the 3rd transport chunk read fails
        client = self._mock_client(
            chunks=[b"\x01\x00" * 80, b"\x02\x00" * 80, b"\x03\x00" * 40],
            fail_after=2,
        )
        config = self._config(sample_rate=8000, chunk_ms=5)

        clock = _stepping_clock(1000, 100)
        metrics = MetricsCollector("s2cpp", "streaming", clock=clock)
        request = S2GenerateRequest(text="test")

        with pytest.raises(S2ClientError):
            await self._collect(
                synthesize_s2cpp_streaming_tts_events(client, request, config, metrics=metrics)
            )

        snapshot = _finalized(metrics)
        assert snapshot.terminal_status == "error"
        assert snapshot.error_type == "S2ClientError"
        # Partial audio was emitted
        assert snapshot.total_emitted_bytes > 0
        assert snapshot.emitted_chunk_count > 0
        # First-data and first-chunk timestamps preserved
        assert snapshot.first_backend_data_ns is not None
        assert snapshot.first_audio_chunk_ns is not None

    @pytest.mark.asyncio
    async def test_early_generator_close_finalizes_once(self):
        """When consumer closes the generator early, metrics finalize with cancelled."""
        from app.s2_client import S2GenerateRequest
        from app.wyoming_server import synthesize_s2cpp_streaming_tts_events

        data = b"\x01\x00" * 400
        client = self._mock_client(chunks=[data])
        config = self._config(sample_rate=8000, chunk_ms=5)

        metrics = MetricsCollector("s2cpp", "streaming")
        request = S2GenerateRequest(text="test")

        gen = synthesize_s2cpp_streaming_tts_events(client, request, config, metrics=metrics)
        # Consume a few events then explicitly close the generator.
        count = 0
        async for _ in gen:
            count += 1
            if count >= 3:
                await gen.aclose()
                break

        snapshot = _finalized(metrics)
        assert snapshot.terminal_status == "cancelled"
        assert snapshot.total_emitted_bytes > 0
        assert snapshot.emitted_chunk_count > 0

    @pytest.mark.asyncio
    async def test_cancellation_observed_finalizes(self):
        """When coroutine is cancelled, metrics finalize with non-success status."""
        from app.s2_client import S2GenerateRequest
        from app.wyoming_server import synthesize_s2cpp_streaming_tts_events

        # Use a client that blocks on read so cancellation can be observed
        import threading
        import time

        class _BlockingStream:
            def __init__(self):
                self._closed = False
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self._closed = True
                return False
            def __iter__(self):
                return self
            def __next__(self):
                time.sleep(10)
                raise StopIteration

        class _BlockingClient:
            def generate_stream(self, request, files=None, boundary=None):
                return _BlockingStream()

        client = _BlockingClient()
        config = self._config()
        metrics = MetricsCollector("s2cpp", "streaming")
        request = S2GenerateRequest(text="test")

        gen = synthesize_s2cpp_streaming_tts_events(client, request, config, metrics=metrics)

        # Start consuming
        async def consume_and_cancel():
            async for _ in gen:
                pass

        task = asyncio.create_task(consume_and_cancel())
        await asyncio.sleep(0.05)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        snapshot = _finalized(metrics)
        assert snapshot.terminal_status != "success"

    @pytest.mark.asyncio
    async def test_streaming_empty_backend_no_chunks(self):
        """Empty backend response: zero bytes/chunks, no first-chunk timestamp."""
        from app.s2_client import S2GenerateRequest
        from app.wyoming_server import synthesize_s2cpp_streaming_tts_events

        client = self._mock_client(chunks=[])
        config = self._config()

        metrics = MetricsCollector("s2cpp", "streaming")
        request = S2GenerateRequest(text="test")

        await self._collect(
            synthesize_s2cpp_streaming_tts_events(client, request, config, metrics=metrics)
        )

        snapshot = _finalized(metrics)
        assert snapshot.total_emitted_bytes == 0
        assert snapshot.emitted_chunk_count == 0
        assert snapshot.first_audio_chunk_ns is None
        assert snapshot.first_backend_data_ns is None  # no non-empty data
        assert snapshot.terminal_status == "success"

    @pytest.mark.asyncio
    async def test_metrics_do_not_change_wyoming_events(self):
        """Metrics collection is invisible to Wyoming event consumers."""
        from app.s2_client import S2GenerateRequest
        from app.wyoming_server import synthesize_s2cpp_streaming_tts_events
        from wyoming.audio import AudioChunk, AudioStart, AudioStop

        data = b"\x01\x00" * 80
        client = self._mock_client(chunks=[data])
        config = self._config(sample_rate=8000, chunk_ms=5)
        request = S2GenerateRequest(text="test")

        # Run twice: once with metrics, once without
        events_with = await self._collect(
            synthesize_s2cpp_streaming_tts_events(
                client, request, config,
                metrics=MetricsCollector("s2cpp", "streaming"),
            )
        )
        events_without = await self._collect(
            synthesize_s2cpp_streaming_tts_events(
                client, request, config,
                metrics=None,  # default
            )
        )

        # Same event types and payloads
        assert len(events_with) == len(events_without)
        for e1, e2 in zip(events_with, events_without):
            assert e1.type == e2.type
            if AudioStart.is_type(e1.type):
                s1 = AudioStart.from_event(e1)
                s2 = AudioStart.from_event(e2)
                assert s1.rate == s2.rate
            elif AudioChunk.is_type(e1.type):
                c1 = AudioChunk.from_event(e1)
                c2 = AudioChunk.from_event(e2)
                assert c1.audio == c2.audio
            elif AudioStop.is_type(e1.type):
                pass  # AudioStop timestamps may differ due to different clock paths

class TestStreamingPCMByteAccounting:
    """Phase 7.5B: Deterministic PCM byte-counting proofs.

    Every backend PCM byte is counted exactly once.
    Every emitted PCM byte is counted exactly once.
    For a clean aligned stream, backend total and Wyoming emitted total match.
    The first backend chunk is included in backend_stream_done totals.
    """

    @staticmethod
    def _config(sample_rate=8000, chunk_ms=5):
        from app.wyoming_server import FakeTtsConfig
        return FakeTtsConfig(sample_rate=sample_rate, chunk_ms=chunk_ms)

    def _mock_client(self, chunks):
        class _MockStream:
            def __init__(self, chunks):
                self._chunks = list(chunks)
                self._idx = 0
                self._closed = False
                self.content_type = "audio/L16; rate=8000; channels=1"
                self.response_headers = {
                    "x-audio-encoding": "pcm_s16le",
                    "x-audio-sample-rate": "8000",
                    "x-audio-channels": "1",
                }
            def __enter__(self): return self
            def __exit__(self, *args): self._closed = True; return False
            def __iter__(self): return self
            def __next__(self):
                if self._idx >= len(self._chunks): raise StopIteration
                chunk = self._chunks[self._idx]; self._idx += 1; return chunk

        class _Client:
            def __init__(self, chunks):
                self._chunks = chunks
            def generate_stream(self, request, files=None, boundary=None):
                return _MockStream(self._chunks)
        return _Client(chunks)

    async def _collect(self, async_gen):
        events = []
        async for event in async_gen:
            events.append(event)
        return events

    @pytest.mark.asyncio
    async def test_clean_aligned_stream_bytes_match(self):
        """For a stream whose PCM is exactly divisible by Wyoming chunk size,
        total_emitted_bytes equals total backend bytes, and every byte is
        counted exactly once."""
        from app.s2_client import S2GenerateRequest
        from app.wyoming_server import synthesize_s2cpp_streaming_tts_events
        from wyoming.audio import AudioChunk

        # 8000 Hz, 5ms chunks = 40 frames = 80 bytes per Wyoming chunk
        # 8 chunks * 80 = 640 bytes total — cleanly aligned
        total_backend_bytes = 640  # 8 Wyoming chunks
        data = b"\x01\x00" * (total_backend_bytes // 2)

        client = self._mock_client(chunks=[data])
        config = self._config(sample_rate=8000, chunk_ms=5)
        metrics = MetricsCollector("s2cpp", "streaming")
        request = S2GenerateRequest(text="test")

        events = await self._collect(
            synthesize_s2cpp_streaming_tts_events(client, request, config, metrics=metrics)
        )

        snapshot = _finalized(metrics)

        # Total emitted bytes must equal backend bytes
        assert snapshot.total_emitted_bytes == total_backend_bytes, (
            f"Expected {total_backend_bytes} emitted bytes, got {snapshot.total_emitted_bytes}"
        )

        # Chunk count must be exact: 640/80 = 8
        assert snapshot.emitted_chunk_count == 8, (
            f"Expected 8 chunks, got {snapshot.emitted_chunk_count}"
        )

        # Verify individual chunks sum to total
        chunks = [AudioChunk.from_event(e) for e in events if AudioChunk.is_type(e.type)]
        chunk_bytes = sum(len(c.audio) for c in chunks)
        assert chunk_bytes == total_backend_bytes
        assert len(chunks) == 8

    @pytest.mark.asyncio
    async def test_non_aligned_stream_with_flush_carry(self):
        """When total backend PCM is NOT divisible by Wyoming chunk size,
        the flush carry chunk is counted once — not double-counted."""
        from app.s2_client import S2GenerateRequest
        from app.wyoming_server import synthesize_s2cpp_streaming_tts_events
        from wyoming.audio import AudioChunk

        # 8000 Hz, 5ms chunks = 80 bytes per Wyoming chunk
        # 700 bytes = 8 full chunks (640) + 60 bytes carry (30 frames)
        total_backend_bytes = 700
        data = b"\x01\x00" * (total_backend_bytes // 2)

        client = self._mock_client(chunks=[data])
        config = self._config(sample_rate=8000, chunk_ms=5)
        metrics = MetricsCollector("s2cpp", "streaming")
        request = S2GenerateRequest(text="test")

        events = await self._collect(
            synthesize_s2cpp_streaming_tts_events(client, request, config, metrics=metrics)
        )

        snapshot = _finalized(metrics)

        # Must count all 700 bytes exactly once — not 700+60=760
        assert snapshot.total_emitted_bytes == total_backend_bytes, (
            f"Expected {total_backend_bytes} emitted bytes (flush carry counted once), "
            f"got {snapshot.total_emitted_bytes}"
        )

        # 8 full + 1 carry = 9 chunks
        assert snapshot.emitted_chunk_count == 9, (
            f"Expected 9 chunks (8 full + 1 carry), got {snapshot.emitted_chunk_count}"
        )

        # Verify AudioChunk payloads sum correctly
        chunks = [AudioChunk.from_event(e) for e in events if AudioChunk.is_type(e.type)]
        chunk_bytes = sum(len(c.audio) for c in chunks)
        assert chunk_bytes == total_backend_bytes

    @pytest.mark.asyncio
    async def test_first_backend_chunk_included_in_total(self):
        """The very first backend PCM chunk contributes to total_emitted_bytes."""
        from app.s2_client import S2GenerateRequest
        from app.wyoming_server import synthesize_s2cpp_streaming_tts_events

        # First chunk 240 bytes, second chunk 400 bytes
        chunk1 = b"\x01\x00" * 120   # 240 bytes
        chunk2 = b"\x02\x00" * 200   # 400 bytes
        total_bytes = 640

        client = self._mock_client(chunks=[chunk1, chunk2])
        config = self._config(sample_rate=8000, chunk_ms=5)
        metrics = MetricsCollector("s2cpp", "streaming")
        request = S2GenerateRequest(text="test")

        await self._collect(
            synthesize_s2cpp_streaming_tts_events(client, request, config, metrics=metrics)
        )

        snapshot = _finalized(metrics)
        assert snapshot.total_emitted_bytes == total_bytes, (
            f"Expected {total_bytes}, got {snapshot.total_emitted_bytes} — "
            f"first chunk ({len(chunk1)} bytes) may be missing"
        )
        assert snapshot.total_emitted_bytes >= len(chunk1), (
            f"total_emitted_bytes ({snapshot.total_emitted_bytes}) < first chunk ({len(chunk1)})"
        )

    @pytest.mark.asyncio
    async def test_stream_split_across_transport_boundaries(self):
        """PCM split across multiple transport chunks with partial frames
        at boundaries must still count every byte exactly once."""
        from app.s2_client import S2GenerateRequest
        from app.wyoming_server import synthesize_s2cpp_streaming_tts_events
        from wyoming.audio import AudioChunk

        # 10 full frames + 1 trailing byte, then 1 leading byte + 9 frames
        # = 20 frames total = 40 bytes
        chunk1 = b"\x01\x00" * 10 + b"\x02"     # 20 bytes + 1 odd byte
        chunk2 = b"\x00" + b"\x03\x00" * 9      # 1 odd byte + 18 bytes
        total_bytes = 40  # 20 frames * 2 bytes

        client = self._mock_client(chunks=[chunk1, chunk2])
        config = self._config(sample_rate=8000, chunk_ms=5)
        metrics = MetricsCollector("s2cpp", "streaming")
        request = S2GenerateRequest(text="test")

        events = await self._collect(
            synthesize_s2cpp_streaming_tts_events(client, request, config, metrics=metrics)
        )

        snapshot = _finalized(metrics)

        # 40 bytes = 1 Wyoming chunk (80 byte capacity not full)
        assert snapshot.total_emitted_bytes == total_bytes, (
            f"Expected {total_bytes}, got {snapshot.total_emitted_bytes}"
        )

        # Verify the emitted audio is correct (reconstructed frames)
        chunks = [AudioChunk.from_event(e) for e in events if AudioChunk.is_type(e.type)]
        emitted = b"".join(c.audio for c in chunks)
        assert len(emitted) == total_bytes
        # Frames 1-10 from chunk1: \x01\x00 repeated 10 times
        assert emitted[:20] == b"\x01\x00" * 10
        # Frames 11-20 from chunk2: \x03\x00 repeated 9 times, preceded by the carry byte
        assert emitted[20:22] == b"\x02\x00"  # reconstructed from carry
        assert emitted[22:] == b"\x03\x00" * 9

    @pytest.mark.asyncio
    async def test_every_emitted_byte_counted_exactly_once(self):
        """For 44100 Hz mono s16le (realistic config), every emitted byte
        is accounted for — no duplication, no loss."""
        from app.s2_client import S2GenerateRequest
        from app.wyoming_server import synthesize_s2cpp_streaming_tts_events
        from wyoming.audio import AudioChunk

        # Simulate the live scenario: 222580 bytes at 44100 Hz, 100ms chunks
        # 222580 bytes = 111290 frames
        total_bytes = 222580
        data = b"\x00\x00" * (total_bytes // 2)

        # Simulate HTTP reading in 4096-byte chunks
        transport_chunks = []
        for offset in range(0, len(data), 4096):
            transport_chunks.append(data[offset:offset+4096])

        # Use a mock client with real 44100 Hz metadata
        class _MockStream:
            def __init__(self, chunks):
                self._chunks = list(chunks); self._idx = 0
                self._closed = False
                self.content_type = "audio/L16; rate=44100; channels=1"
                self.response_headers = {
                    "x-audio-encoding": "pcm_s16le",
                    "x-audio-sample-rate": "44100",
                    "x-audio-channels": "1",
                }
            def __enter__(self): return self
            def __exit__(self, *a): self._closed = True; return False
            def __iter__(self): return self
            def __next__(self):
                if self._idx >= len(self._chunks): raise StopIteration
                chunk = self._chunks[self._idx]; self._idx += 1; return chunk
        class _Client:
            def generate_stream(self, r, files=None, boundary=None):
                return _MockStream(transport_chunks)

        client = _Client()
        config = self._config(sample_rate=44100, chunk_ms=100)
        metrics = MetricsCollector("s2cpp", "streaming")
        request = S2GenerateRequest(text="test")

        events = await self._collect(
            synthesize_s2cpp_streaming_tts_events(client, request, config, metrics=metrics)
        )

        snapshot = _finalized(metrics)

        # Every backend byte counted once
        assert snapshot.total_emitted_bytes == total_bytes, (
            f"Expected {total_bytes}, got {snapshot.total_emitted_bytes}"
        )

        # Verify AudioChunk sum matches
        chunks = [AudioChunk.from_event(e) for e in events if AudioChunk.is_type(e.type)]
        chunk_sum = sum(len(c.audio) for c in chunks)
        assert chunk_sum == total_bytes, (
            f"AudioChunk sum {chunk_sum} != expected {total_bytes}"
        )

        # Verify chunk count is consistent
        assert snapshot.emitted_chunk_count == len(chunks), (
            f"metrics chunks {snapshot.emitted_chunk_count} != actual {len(chunks)}"
        )

        # With 222580 at 8820 bytes per chunk (44100 Hz * 0.1s * 2 bytes):
        # 25 full + 1 partial (2080 bytes) = 26 chunks
        assert snapshot.emitted_chunk_count == 26, (
            f"Expected 26 chunks (25 full + 1 carry of 2080 bytes), "
            f"got {snapshot.emitted_chunk_count}"
        )




# ── helpers ──────────────────────────────────────────────────────────────────


def _finalized(collector: MetricsCollector) -> SynthesisMetrics:
    """Get finalized metrics from a collector that has been finalized by
    the synthesis function.  We can't call finalize() again, so we
    reconstruct from the collector's internal state.
    """
    return SynthesisMetrics(
        request_id=collector._request_id,
        trace_id=collector._trace_id,
        backend_type=collector._backend_type,
        synthesis_mode=collector._synthesis_mode,
        request_start_ns=collector._request_start_ns,
        first_backend_data_ns=collector._first_backend_data_ns,
        first_audio_chunk_ns=collector._first_audio_chunk_ns,
        terminal_ns=collector._terminal_ns,
        total_emitted_bytes=collector._total_emitted_bytes,
        emitted_chunk_count=collector._emitted_chunk_count,
        terminal_status=collector._terminal_status,
        error_type=collector._error_type,
    )

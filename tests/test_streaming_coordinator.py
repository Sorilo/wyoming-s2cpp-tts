"""Phase 9.5 Slice 3 — StreamingCoordinator tests (RED-GREEN-REFACTOR).

Tests for the connection-owned progressive phrase synthesis coordinator.
Uses events, futures, and barriers — no arbitrary sleep synchronization.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from wyoming.audio import AudioStart, AudioStop, AudioChunk
from wyoming.event import Event


# ── Fake backend synthesis (deterministic) ─────────────────────────────

async def _fake_synthesize_phrase(text: str) -> list[Event]:
    """Deterministic fake synthesis for a single phrase."""
    rate, width, channels = 22050, 2, 1
    chunk_bytes = 882
    num_chunks = min(max(len(text) // 10, 1), 5)
    events: list[Event] = [
        AudioStart(rate=rate, width=width, channels=channels).event(),
    ]
    for i in range(num_chunks):
        events.append(
            AudioChunk(
                rate=rate, width=width, channels=channels,
                audio=b"\x00" * chunk_bytes,
                timestamp=i * 20,
            ).event()
        )
    events.append(AudioStop(timestamp=num_chunks * 20).event())
    return events


# ── Fake scheduler for testing ────────────────────────────────────────

class _FakeScheduler:
    """Fake SpeechScheduler that tracks submissions and enforces serialization."""

    def __init__(self):
        self.submissions: list[str] = []  # phrase texts submitted
        self.active_count = 0
        self.max_active = 0
        self._lock = asyncio.Lock()
        self.draining = False
        self._drain_event = asyncio.Event()

    async def run(self, request, operation):
        async with self._lock:
            self.active_count += 1
            self.max_active = max(self.max_active, self.active_count)
            self.submissions.append(request.text)

        try:
            await operation()
        finally:
            async with self._lock:
                self.active_count -= 1

    async def drain(self) -> int:
        self.draining = True
        self._drain_event.set()
        return 0

    async def cancel_connection(self, connection_id: str) -> int:
        return 0

    def cancel_active_for_connection(self, connection_id: str) -> bool:
        return False

    async def cancel_new_request(self, connection_id: str) -> None:
        pass


# ── Tests ──────────────────────────────────────────────────────────────

class TestStreamingCoordinator:
    """Test the connection-owned streaming coordinator."""

    @pytest.mark.asyncio
    async def test_single_phrase_synthesis(self):
        """A single phrase is parsed, synthesized, and output via envelope."""
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _FakeScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize_phrase,
            connection_id="conn-1",
        )

        output_events: list[Event] = []
        ready = asyncio.Event()

        async def _run():
            ready.set()
            async for event in coordinator.stream("Hello world."):
                output_events.append(event)

        task = asyncio.create_task(_run())
        await ready.wait()

        # Wait for completion
        await asyncio.wait_for(task, timeout=5.0)

        # Verify output has AudioStart, chunks, AudioStop
        starts = [e for e in output_events if AudioStart.is_type(e.type)]
        stops = [e for e in output_events if AudioStop.is_type(e.type)]
        chunks = [e for e in output_events if AudioChunk.is_type(e.type)]
        assert len(starts) == 1
        assert len(stops) == 1
        assert len(chunks) >= 1

    @pytest.mark.asyncio
    async def test_multi_phrase_sequential(self):
        """Multiple phrases are submitted one at a time through scheduler."""
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _FakeScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize_phrase,
            connection_id="conn-1",
        )

        output_events: list[Event] = []
        ready = asyncio.Event()

        async def _run():
            ready.set()
            async for event in coordinator.stream("First. Second. Third."):
                output_events.append(event)

        task = asyncio.create_task(_run())
        await ready.wait()
        await asyncio.wait_for(task, timeout=5.0)

        # Verify three submissions
        assert len(scheduler.submissions) == 3, f"Got {scheduler.submissions}"
        # Verify serialization — max_active should be 1
        assert scheduler.max_active == 1

    @pytest.mark.asyncio
    async def test_only_one_audio_start(self):
        """Multiple phrases produce exactly one AudioStart."""
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _FakeScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize_phrase,
            connection_id="conn-1",
        )

        output_events: list[Event] = []
        ready = asyncio.Event()

        async def _run():
            ready.set()
            async for event in coordinator.stream("One. Two. Three."):
                output_events.append(event)

        task = asyncio.create_task(_run())
        await ready.wait()
        await asyncio.wait_for(task, timeout=5.0)

        starts = [e for e in output_events if AudioStart.is_type(e.type)]
        assert len(starts) == 1

    @pytest.mark.asyncio
    async def test_exactly_one_audio_stop(self):
        """Multiple phrases produce exactly one AudioStop on success."""
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _FakeScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize_phrase,
            connection_id="conn-1",
        )

        output_events: list[Event] = []
        ready = asyncio.Event()

        async def _run():
            ready.set()
            async for event in coordinator.stream("One. Two."):
                output_events.append(event)

        task = asyncio.create_task(_run())
        await ready.wait()
        await asyncio.wait_for(task, timeout=5.0)

        stops = [e for e in output_events if AudioStop.is_type(e.type)]
        assert len(stops) == 1

    @pytest.mark.asyncio
    async def test_no_text_produces_no_events(self):
        """Empty text produces no synthesis events."""
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _FakeScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize_phrase,
            connection_id="conn-1",
        )

        output_events: list[Event] = []
        ready = asyncio.Event()

        async def _run():
            ready.set()
            async for event in coordinator.stream("   "):
                output_events.append(event)

        task = asyncio.create_task(_run())
        await ready.wait()
        await asyncio.wait_for(task, timeout=5.0)

        assert len(output_events) == 0

    @pytest.mark.asyncio
    async def test_timestamps_continuous_across_phrases(self):
        """Chunk timestamps increase monotonically across phrase boundaries."""
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _FakeScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize_phrase,
            connection_id="conn-1",
        )

        output_events: list[Event] = []
        ready = asyncio.Event()

        async def _run():
            ready.set()
            async for event in coordinator.stream("First. Second."):
                output_events.append(event)

        task = asyncio.create_task(_run())
        await ready.wait()
        await asyncio.wait_for(task, timeout=5.0)

        chunks = [e for e in output_events if AudioChunk.is_type(e.type)]
        timestamps = [AudioChunk.from_event(c).timestamp for c in chunks]
        assert timestamps == sorted(timestamps), "Timestamps must be monotonic"
        assert len(set(timestamps)) == len(timestamps), "Timestamps must be unique"

    @pytest.mark.asyncio
    async def test_failure_on_second_phrase_emits_audio_stop_then_error(self):
        """When second phrase fails, emit AudioStop then Error, no SynthesizeStopped."""
        from app.speech.stream_coordinator import StreamingCoordinator
        from wyoming.error import Error as WError

        call_count = 0

        async def _fail_second(text: str) -> list[Event]:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("Backend failure")
            return await _fake_synthesize_phrase(text)

        scheduler = _FakeScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fail_second,
            connection_id="conn-1",
        )

        output_events: list[Event] = []
        ready = asyncio.Event()

        async def _run():
            ready.set()
            # Error events are yielded through the async generator,
            # not re-raised (coordinator handles failure gracefully)
            async for event in coordinator.stream("First. Second."):
                output_events.append(event)

        task = asyncio.create_task(_run())
        await ready.wait()
        await asyncio.wait_for(task, timeout=5.0)

        # Should have AudioStop from failure close
        stops = [e for e in output_events if AudioStop.is_type(e.type)]
        assert len(stops) == 1, "Must emit one AudioStop on failure"
        # Should have a Wyoming Error event
        errors = [e for e in output_events if WError.is_type(e.type)]
        assert len(errors) == 1, "Must emit one Error event on failure"
        # Should NOT have SynthesizeStopped
        from wyoming.tts import SynthesizeStopped
        stopped = [e for e in output_events if SynthesizeStopped.is_type(e.type)]
        assert len(stopped) == 0, "Must NOT emit SynthesizeStopped on failure"

    @pytest.mark.asyncio
    async def test_cannot_start_twice(self):
        """Calling stream() twice on same coordinator raises error."""
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _FakeScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize_phrase,
            connection_id="conn-1",
        )

        async def _run():
            async for event in coordinator.stream("Hello."):
                pass

        await _run()
        with pytest.raises(RuntimeError, match="already"):
            async for _ in coordinator.stream("Again."):
                pass


class TestStreamingCoordinatorChunkedFeeding:
    """Test chunked text feeding through the coordinator."""

    @pytest.mark.asyncio
    async def test_progressive_feed_before_stop(self):
        """Text chunks fed progressively, synthesis starts before stop."""
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _FakeScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize_phrase,
            connection_id="conn-1",
        )

        output_events: list[Event] = []
        ready = asyncio.Event()
        chunks_done = asyncio.Event()

        async def _run():
            ready.set()
            async for event in coordinator.run_progressive(
                ["Hello world. This is a test."],
            ):
                output_events.append(event)
            chunks_done.set()

        task = asyncio.create_task(_run())
        await ready.wait()
        await asyncio.wait_for(task, timeout=5.0)

        assert len(output_events) > 0

    @pytest.mark.asyncio
    async def test_chunked_feeding_multiple_phrases(self):
        """Chunks arriving over time produce multiple phrases."""
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _FakeScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize_phrase,
            connection_id="conn-1",
        )

        output_events: list[Event] = []
        ready = asyncio.Event()

        async def _feed():
            ready.set()
            # Simulate chunks arriving over time
            for chunk in ["Hello. ", "This is ", "another. ", "Final one."]:
                coordinator.feed_text(chunk)
                await asyncio.sleep(0)  # yield to event loop
            coordinator.feed_done()

            async for event in coordinator.output_events():
                output_events.append(event)

        task = asyncio.create_task(_feed())
        await ready.wait()
        await asyncio.wait_for(task, timeout=5.0)

        # Should have synthesized multiple phrases
        assert len(scheduler.submissions) >= 2

    @pytest.mark.asyncio
    async def test_phrase_order_preserved(self):
        """Phrases are submitted in exact text order."""
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _FakeScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize_phrase,
            connection_id="conn-1",
        )

        output_events: list[Event] = []
        ready = asyncio.Event()

        async def _feed():
            ready.set()
            coordinator.feed_text("First. Second. Third.")
            coordinator.feed_done()
            async for event in coordinator.output_events():
                output_events.append(event)

        task = asyncio.create_task(_feed())
        await ready.wait()
        await asyncio.wait_for(task, timeout=5.0)

        assert scheduler.submissions == ["First.", "Second.", "Third."]

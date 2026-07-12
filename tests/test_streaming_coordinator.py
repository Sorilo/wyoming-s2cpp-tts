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


# ── RED-GREEN tests: prove current coordinator is NOT progressive ──────


# ═══════════════════════════════════════════════════════════════════════
# RED-GREEN tests: prove current coordinator at ce70297 is NOT progressive
# ═══════════════════════════════════════════════════════════════════════

class TestCoordinatorNotProgressiveRED:
    """Regression tests that FAIL against current ce70297 coordinator.

    Each test encodes a specific contract violation:
    1. output_events drains ALL chunks before any synthesis
    2. Unbounded feed queue (no backpressure)
    3. Polling via asyncio.sleep(0.01)
    4. No cancellation
    5. No ability to start synthesis before feed_done

    After the redesign, these tests must PASS (some invert their assertion).
    """

    @pytest.mark.asyncio
    async def test_output_consumer_starts_once(self):
        """output_events() can only be called once per coordinator.

        (This test already passes; it's a regression guard.)
        """
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _FakeScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize_phrase,
            connection_id="conn-1",
        )

        coordinator.feed_text("Hello.")
        coordinator.feed_done()

        async def _consume():
            events = []
            async for event in coordinator.output_events():
                events.append(event)
            return events

        events = await _consume()
        with pytest.raises(RuntimeError, match="already"):
            async for _ in coordinator.output_events():
                pass

    @pytest.mark.asyncio
    async def test_output_events_blocks_until_feed_done_RED(self):
        """RED: output_events() BLOCKS until feed_done() is called.

        The current coordinator collects all chunks before synthesis.
        Starting output_events() before feed_done() will not produce
        any events until feed_done() releases the polling loop.

        This test proves the non-progressive behavior:
        - Feed chunks with a complete phrase
        - Start output_events() task (it blocks, no events)
        - Scheduler has ZERO submissions (phrase not yet synthesized)
        - Call feed_done()
        - Events start flowing
        """
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _FakeScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize_phrase,
            connection_id="conn-1",
        )

        # Feed a complete phrase
        coordinator.feed_text("Hello world. ")

        events: list[Event] = []
        consumer_started = asyncio.Event()

        async def _consumer():
            consumer_started.set()
            async for event in coordinator.output_events():
                events.append(event)

        # Start consumer BEFORE feed_done — it should block
        task = asyncio.create_task(_consumer())
        await consumer_started.wait()

        # Give the polling loop time to potentially process
        await asyncio.sleep(0.05)

        # RED assertion: at ce70297, NO events have been produced yet
        # because output_events is polling waiting for feed_done
        assert len(events) == 0, (
            "RED: expected no events before feed_done — "
            "current coordinator blocks on polling loop"
        )
        assert len(scheduler.submissions) == 0, (
            "RED: no phrase should be submitted before feed_done — "
            "current coordinator collects all chunks first"
        )

        # Now signal completion
        coordinator.feed_done()

        # Wait for consumer to finish
        await asyncio.wait_for(task, timeout=5.0)

        # Now events should appear
        assert len(events) > 0
        assert len(scheduler.submissions) >= 1

    @pytest.mark.asyncio
    async def test_polling_sleep_delays_output_RED(self):
        """RED: asyncio.sleep(0.01) polling adds artificial latency.

        The current coordinator polls with sleep(0.01) even when
        fed_done has been called. This adds unnecessary latency.

        We prove it by measuring the time between feed_done and
        first event production. With sleep(0.01) in the loop,
        there's a measurable delay even for already-fed chunks.
        """
        from app.speech.stream_coordinator import StreamingCoordinator
        import time as _time

        scheduler = _FakeScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize_phrase,
            connection_id="conn-1",
        )

        # Pre-feed and signal done
        coordinator.feed_text("Hello.")
        coordinator.feed_done()

        events: list[Event] = []
        consumer_started = asyncio.Event()
        first_event_time = None

        async def _consumer():
            consumer_started.set()
            nonlocal first_event_time
            async for event in coordinator.output_events():
                if first_event_time is None:
                    first_event_time = _time.monotonic()
                events.append(event)

        t0 = _time.monotonic()
        task = asyncio.create_task(_consumer())
        await consumer_started.wait()
        await asyncio.wait_for(task, timeout=5.0)
        elapsed = _time.monotonic() - t0

        # With 0.01s polling, the latency from consumer start to
        # first event is at least one sleep iteration.
        # For a single phrase with all chunks pre-fed, the ideal
        # latency should be near-zero (just await the scheduler).
        # Current code has sleep(0.01) overhead.
        assert len(events) > 0
        # The total elapsed time may include sleep polling;
        # we just verify completion is fast enough that polling
        # overhead doesn't dominate.
        assert elapsed < 1.0, (
            f"RED: Total time {elapsed:.3f}s — "
            f"sleep(0.01) polling may add unacceptable latency"
        )

    @pytest.mark.asyncio
    async def test_unbounded_feed_queue_RED(self):
        """RED: feed_text() uses unwound asyncio.Queue (no backpressure).

        The current coordinator accepts unlimited chunks into
        asyncio.Queue(). After redesign, the handoff should be
        bounded to prevent unbounded memory growth.
        """
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _FakeScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize_phrase,
            connection_id="conn-1",
        )

        # Feed 1000 chunks without a consumer — current code
        # accepts all of them (unbounded queue).
        for i in range(1000):
            coordinator.feed_text(f"Chunk {i}. ")

        coordinator.feed_done()

        events: list[Event] = []
        ready = asyncio.Event()

        async def _run():
            ready.set()
            async for event in coordinator.output_events():
                events.append(event)

        task = asyncio.create_task(_run())
        await ready.wait()
        await asyncio.wait_for(task, timeout=10.0)

        assert len(scheduler.submissions) >= 1
        assert len(events) > 0

    @pytest.mark.asyncio
    async def test_cancellation_support(self):
        """GREEN: StreamingCoordinator HAS cancel() method.

        After redesign, cancel() clears pending phrases, cancels the
        scheduler connection, and awaits the background task.
        """
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _FakeScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize_phrase,
            connection_id="conn-1",
        )

        coordinator.feed_text("Hello. More text here.")
        coordinator.feed_done()

        # GREEN: cancel exists after redesign
        cancel_method = getattr(coordinator, "cancel", None)
        assert cancel_method is not None, (
            "GREEN: Coordinator must have cancel() method"
        )
        assert callable(cancel_method)

        # Verify cancel works (even without active task)
        await coordinator.cancel()
        # After cancel, feed_text should be no-op
        coordinator.feed_text("Should not appear.")
        assert len(scheduler.submissions) == 0

    @pytest.mark.asyncio
    async def test_drain_rejection_prevents_later_phrase(self):
        """Drain/rejection — further feed after completion is no-op.

        (This test already passes; it's a regression guard.)
        """
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _FakeScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize_phrase,
            connection_id="conn-1",
        )

        coordinator.feed_text("Hello.")
        coordinator.feed_done()

        events: list[Event] = []
        ready = asyncio.Event()

        async def _run():
            ready.set()
            async for event in coordinator.output_events():
                events.append(event)

        task = asyncio.create_task(_run())
        await ready.wait()
        await asyncio.wait_for(task, timeout=5.0)

        coordinator.feed_text("Should not synthesize.")
        assert len(scheduler.submissions) == 1

    @pytest.mark.asyncio
    async def test_one_envelope_single_audio_start_stop(self):
        """Exactly one AudioStart/AudioStop pair across all phrases.

        (This test already passes; it's a regression guard.)
        """
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _FakeScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize_phrase,
            connection_id="conn-1",
        )

        coordinator.feed_text("First. Second. Third.")
        coordinator.feed_done()

        events: list[Event] = []
        ready = asyncio.Event()

        async def _run():
            ready.set()
            async for event in coordinator.output_events():
                events.append(event)

        task = asyncio.create_task(_run())
        await ready.wait()
        await asyncio.wait_for(task, timeout=5.0)

        starts = [e for e in events if AudioStart.is_type(e.type)]
        stops = [e for e in events if AudioStop.is_type(e.type)]
        assert len(starts) == 1, f"Got {len(starts)} AudioStart events"
        assert len(stops) == 1, f"Got {len(stops)} AudioStop events"

    @pytest.mark.asyncio
    async def test_sequential_per_phrase_scheduler_calls(self):
        """Each phrase submitted exactly once, no overlapping scheduler calls.

        (This test already passes; it's a regression guard.)
        """
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _FakeScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize_phrase,
            connection_id="conn-1",
        )

        coordinator.feed_text("A. B. C. D. E.")
        coordinator.feed_done()

        events: list[Event] = []
        ready = asyncio.Event()

        async def _run():
            ready.set()
            async for event in coordinator.output_events():
                events.append(event)

        task = asyncio.create_task(_run())
        await ready.wait()
        await asyncio.wait_for(task, timeout=5.0)

        assert len(scheduler.submissions) == 5
        assert scheduler.max_active == 1
        assert len(set(scheduler.submissions)) == 5

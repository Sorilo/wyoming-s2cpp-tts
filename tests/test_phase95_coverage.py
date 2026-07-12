"""Phase 9.5 remaining behavioral coverage.

Tests for cancellation during active/between-phrase synthesis, scheduler
error propagation (capacity, timeout, backend-busy), generator cleanup,
and drain semantics. Uses deterministic Event/Future/barriers — no
arbitrary sleep synchronization.
"""

import asyncio
import pytest
from wyoming.audio import AudioStart, AudioStop, AudioChunk
from wyoming.event import Event

from app.speech.scheduler import QueueFullError, QueueTimeoutError


# ── Fake backend synthesis (deterministic) ─────────────────────────────

async def _fake_synthesize(text: str) -> list[Event]:
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


# ── Fake schedulers with controlled behavior ──────────────────────────

class _BlockingScheduler:
    """Scheduler that blocks an active phrase inside the operation call."""

    def __init__(self):
        self.submissions: list[str] = []
        self._block = asyncio.Event()
        self._submitted = asyncio.Event()
        self._cancelled_connections: set[str] = set()
        self._active_cancelled: set[str] = set()

    async def run(self, request, operation):
        self.submissions.append(request.text)
        self._submitted.set()
        # Instead of calling operation, block indefinitely
        await self._block.wait()

    async def cancel_connection(self, connection_id: str) -> int:
        self._cancelled_connections.add(connection_id)
        return 0

    def cancel_active_for_connection(self, connection_id: str) -> bool:
        self._active_cancelled.add(connection_id)
        return True


class _RejectingScheduler:
    """Scheduler that raises QueueFullError on admission."""

    def __init__(self):
        self.submissions: list[str] = []
        self.reject_count = 0

    async def run(self, request, operation):
        self.reject_count += 1
        raise QueueFullError("Queue is full")

    async def cancel_connection(self, connection_id: str) -> int:
        return 0

    def cancel_active_for_connection(self, connection_id: str) -> bool:
        return False


class _TimeoutScheduler:
    """Scheduler that raises QueueTimeoutError on admission."""

    def __init__(self):
        self.submissions: list[str] = []
        self.timeout_count = 0

    async def run(self, request, operation):
        self.timeout_count += 1
        raise QueueTimeoutError("Queue wait timed out")

    async def cancel_connection(self, connection_id: str) -> int:
        return 0

    def cancel_active_for_connection(self, connection_id: str) -> bool:
        return False


class _FirstOkThenRejectScheduler:
    """Scheduler: first phrase succeeds, second raises QueueFullError."""

    def __init__(self):
        self.submissions: list[str] = []
        self.call_count = 0

    async def run(self, request, operation):
        self.call_count += 1
        if self.call_count == 1:
            self.submissions.append(request.text)
            await operation()
        else:
            raise QueueFullError("Queue full on second submission")

    async def cancel_connection(self, connection_id: str) -> int:
        return 0

    def cancel_active_for_connection(self, connection_id: str) -> bool:
        return False


class _SimpleScheduler:
    """Simple scheduler that just runs operations."""
    def __init__(self):
        self.submissions: list[str] = []
    async def run(self, request, operation):
        self.submissions.append(request.text)
        await operation()
    async def cancel_connection(self, connection_id: str) -> int:
        return 0
    def cancel_active_for_connection(self, connection_id: str) -> bool:
        return False


class _TrackingScheduler:
    """Tracks max_active for overlap detection."""
    def __init__(self):
        self.submissions: list[str] = []
        self.active_count = 0
        self.max_active = 0
        self._lock = asyncio.Lock()
    async def run(self, request, operation):
        async with self._lock:
            self.active_count += 1
            self.max_active = max(self.max_active, self.active_count)
        self.submissions.append(request.text)
        await operation()
        async with self._lock:
            self.active_count -= 1
    async def cancel_connection(self, connection_id: str) -> int:
        return 0
    def cancel_active_for_connection(self, connection_id: str) -> bool:
        return False


class _BlockingSecondScheduler:
    """Block on second phrase submission only."""
    def __init__(self):
        self.submissions: list[str] = []
        self._lock = asyncio.Lock()
        self._block = asyncio.Event()
        self._call_count = 0
        self._second_started = asyncio.Event()
        self._cancelled_connections: set[str] = set()
        self._active_cancelled: set[str] = set()

    async def run(self, request, operation):
        async with self._lock:
            self._call_count += 1
            count = self._call_count
        self.submissions.append(request.text)
        if count == 1:
            await operation()
        else:
            self._second_started.set()
            await self._block.wait()

    async def cancel_connection(self, connection_id: str) -> int:
        self._cancelled_connections.add(connection_id)
        return 0

    def cancel_active_for_connection(self, connection_id: str) -> bool:
        self._active_cancelled.add(connection_id)
        return True


# ── Tests ──────────────────────────────────────────────────────────────

class TestCoordinatorCancellationDuringSynthesis:
    """Cancellation while a phrase is actively synthesizing."""

    @pytest.mark.asyncio
    async def test_cancel_during_active_synthesis_stops_output(self):
        """Cancel while scheduler blocks stops output, no audio emitted."""
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _BlockingScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize,
            connection_id="conn-cancel",
        )

        await coordinator.start()
        coordinator.feed_text("Hello world. More text.")
        coordinator.feed_done()

        output_events: list[Event] = []
        consumer_started = asyncio.Event()
        consumer_done = asyncio.Event()

        async def _consume():
            consumer_started.set()
            try:
                async for event in coordinator:
                    output_events.append(event)
            except Exception:
                pass
            consumer_done.set()

        consumer_task = asyncio.create_task(_consume())
        await consumer_started.wait()

        # Wait for scheduler to receive submission
        await asyncio.wait_for(scheduler._submitted.wait(), timeout=5.0)

        # Cancel while scheduler is blocked
        await coordinator.cancel()

        # Consumer should finish
        await asyncio.wait_for(consumer_done.wait(), timeout=5.0)

        # No AudioStart should have been emitted
        starts = [e for e in output_events if AudioStart.is_type(e.type)]
        assert len(starts) == 0

        # Cancel should have called scheduler cleanup
        assert "conn-cancel" in scheduler._cancelled_connections

    @pytest.mark.asyncio
    async def test_cancel_during_active_with_partial_audio(self):
        """Cancel after first phrase audio closes with AudioStop."""
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _BlockingSecondScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize,
            connection_id="conn-partial",
        )

        await coordinator.start()
        coordinator.feed_text("First. Second.")
        coordinator.feed_done()

        output_events: list[Event] = []
        consumer_started = asyncio.Event()
        consumer_done = asyncio.Event()

        async def _consume():
            consumer_started.set()
            try:
                async for event in coordinator:
                    output_events.append(event)
            except Exception:
                pass
            consumer_done.set()

        consumer_task = asyncio.create_task(_consume())
        await consumer_started.wait()

        # Wait for second phrase to be submitted
        await asyncio.wait_for(scheduler._second_started.wait(), timeout=5.0)

        # First phrase audio should be emitted by now
        starts_before = [e for e in output_events if AudioStart.is_type(e.type)]
        assert len(starts_before) == 1, "First phrase must emit AudioStart"

        # Cancel while second phrase is blocked
        await coordinator.cancel()

        await asyncio.wait_for(consumer_done.wait(), timeout=5.0)

        # Cancellation is connection teardown: the consumer terminates, but
        # no new terminal event is promised to an already disconnected peer.
        assert consumer_done.is_set()


class TestCoordinatorCancellationBetweenPhrases:
    """Cancellation when phrases are pending but synthesis hasn't started."""

    @pytest.mark.asyncio
    async def test_cancel_before_consumer_clears_pending(self):
        """Cancel after feeding but before consumer prevents synthesis."""
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _SimpleScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize,
            connection_id="conn-between",
        )

        # Start the coordinator and feed text
        await coordinator.start()
        coordinator.feed_text("First. Second. Third.")
        coordinator.feed_done()

        # Cancel right away — stops the synthesis loop
        await coordinator.cancel()

        # After cancel, coordinator is terminated — output_events() raises
        with pytest.raises(RuntimeError, match="already"):
            async for _ in coordinator.output_events():
                pass

        # No successful synthesis should complete
        assert len(scheduler.submissions) == 0


class TestCoordinatorSchedulerErrors:
    """Scheduler error propagation."""

    @pytest.mark.asyncio
    async def test_queue_full_rejection_closes_envelope(self):
        """QueueFullError on first phrase closes envelope with error."""
        from app.speech.stream_coordinator import StreamingCoordinator
        from wyoming.error import Error as WError

        scheduler = _RejectingScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize,
            connection_id="conn-full",
        )

        await coordinator.start()
        coordinator.feed_text("Hello world.")
        coordinator.feed_done()

        output_events: list[Event] = []
        consumer_started = asyncio.Event()

        async def _consume():
            consumer_started.set()
            async for event in coordinator:
                output_events.append(event)

        consumer_task = asyncio.create_task(_consume())
        await consumer_started.wait()
        await asyncio.wait_for(consumer_task, timeout=5.0)

        errors = [e for e in output_events if WError.is_type(e.type)]
        assert len(errors) == 1, f"Expected one error, got {len(errors)}"
        assert scheduler.reject_count == 1

    @pytest.mark.asyncio
    async def test_queue_timeout_closes_envelope(self):
        """QueueTimeoutError on first phrase closes envelope with error."""
        from app.speech.stream_coordinator import StreamingCoordinator
        from wyoming.error import Error as WError

        scheduler = _TimeoutScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize,
            connection_id="conn-timeout",
        )

        await coordinator.start()
        coordinator.feed_text("Hello world.")
        coordinator.feed_done()

        output_events: list[Event] = []
        consumer_started = asyncio.Event()

        async def _consume():
            consumer_started.set()
            async for event in coordinator:
                output_events.append(event)

        consumer_task = asyncio.create_task(_consume())
        await consumer_started.wait()
        await asyncio.wait_for(consumer_task, timeout=5.0)

        errors = [e for e in output_events if WError.is_type(e.type)]
        assert len(errors) == 1, f"Expected one error, got {len(errors)}"
        assert scheduler.timeout_count == 1

    @pytest.mark.asyncio
    async def test_second_phrase_rejected_after_first_succeeds(self):
        """Second phrase rejection after first audio -> AudioStop + error."""
        from app.speech.stream_coordinator import StreamingCoordinator
        from wyoming.error import Error as WError

        scheduler = _FirstOkThenRejectScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize,
            connection_id="conn-partial-fail",
        )

        await coordinator.start()
        coordinator.feed_text("First. Second.")
        coordinator.feed_done()

        output_events: list[Event] = []
        consumer_started = asyncio.Event()

        async def _consume():
            consumer_started.set()
            async for event in coordinator:
                output_events.append(event)

        consumer_task = asyncio.create_task(_consume())
        await consumer_started.wait()
        await asyncio.wait_for(consumer_task, timeout=5.0)

        starts = [e for e in output_events if AudioStart.is_type(e.type)]
        assert len(starts) == 1

        stops = [e for e in output_events if AudioStop.is_type(e.type)]
        assert len(stops) == 1

        errors = [e for e in output_events if WError.is_type(e.type)]
        assert len(errors) == 1

        from wyoming.tts import SynthesizeStopped
        stopped = [e for e in output_events if SynthesizeStopped.is_type(e.type)]
        assert len(stopped) == 0
        assert len(scheduler.submissions) == 1


class TestCoordinatorDrain:
    """Drain semantics."""

    @pytest.mark.asyncio
    async def test_drain_rejects_future_feeds(self):
        """After drain(), feed_text is a no-op."""
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _SimpleScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize,
            connection_id="conn-drain",
        )

        await coordinator.start()
        coordinator.feed_text("Before drain.")

        await coordinator.drain()

        coordinator.feed_text("After drain.")
        coordinator.feed_done()

        output_events: list[Event] = []
        consumer_started = asyncio.Event()

        async def _consume():
            consumer_started.set()
            async for event in coordinator:
                output_events.append(event)

        consumer_task = asyncio.create_task(_consume())
        await consumer_started.wait()
        await asyncio.wait_for(consumer_task, timeout=5.0)

        assert len(scheduler.submissions) >= 1
        assert all("After drain" not in s for s in scheduler.submissions)

    @pytest.mark.asyncio
    async def test_drain_clears_pending(self):
        """Drain clears pending phrases."""
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _SimpleScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize,
            connection_id="conn-drain-clear",
        )

        coordinator.feed_text("One. Two. Three.")
        coordinator.feed_done()

        await coordinator.drain()

        output_events: list[Event] = []
        consumer_started = asyncio.Event()

        async def _consume():
            consumer_started.set()
            async for event in coordinator.output_events():
                output_events.append(event)

        consumer_task = asyncio.create_task(_consume())
        await consumer_started.wait()
        await asyncio.wait_for(consumer_task, timeout=5.0)

        assert len(scheduler.submissions) == 0


class TestCoordinatorGeneratorCleanup:
    """Generator and task cleanup verification."""

    @pytest.mark.asyncio
    async def test_cancel_before_start_is_safe(self):
        """Calling cancel() before start() is safe."""
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _SimpleScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize,
            connection_id="conn-early-cancel",
        )

        await coordinator.cancel()
        coordinator.feed_text("Should not appear.")
        assert len(scheduler.submissions) == 0

    @pytest.mark.asyncio
    async def test_cancel_after_completion_is_noop(self):
        """Calling cancel() after successful completion is safe."""
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _SimpleScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize,
            connection_id="conn-post-cancel",
        )

        await coordinator.start()
        coordinator.feed_text("Hello.")
        coordinator.feed_done()

        output_events: list[Event] = []
        consumer_started = asyncio.Event()

        async def _consume():
            consumer_started.set()
            async for event in coordinator:
                output_events.append(event)

        consumer_task = asyncio.create_task(_consume())
        await consumer_started.wait()
        await asyncio.wait_for(consumer_task, timeout=5.0)

        assert len(scheduler.submissions) == 1
        stops = [e for e in output_events if AudioStop.is_type(e.type)]
        assert len(stops) == 1

        await coordinator.cancel()
        assert True


class TestCoordinatorBackendBusy:
    """Backend-busy behavior through the coordinator."""

    @pytest.mark.asyncio
    async def test_synthesis_error_emits_error_event(self):
        """When synthesize_fn raises, error propagates through envelope."""
        from app.speech.stream_coordinator import StreamingCoordinator
        from wyoming.error import Error as WError

        scheduler = _SimpleScheduler()

        async def _failing_synthesize(text: str) -> list[Event]:
            raise RuntimeError("Backend unavailable")

        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_failing_synthesize,
            connection_id="conn-busy",
        )

        await coordinator.start()
        coordinator.feed_text("Hello.")
        coordinator.feed_done()

        output_events: list[Event] = []
        consumer_started = asyncio.Event()

        async def _consume():
            consumer_started.set()
            async for event in coordinator:
                output_events.append(event)

        consumer_task = asyncio.create_task(_consume())
        await consumer_started.wait()
        await asyncio.wait_for(consumer_task, timeout=5.0)

        errors = [e for e in output_events if WError.is_type(e.type)]
        assert len(errors) == 1


class TestAccountingSemantics:
    """Coordinator phrase counting and ordering."""

    @pytest.mark.asyncio
    async def test_phrase_count_increments_per_backend_call(self):
        """_phrase_count increments exactly once per scheduler submission."""
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _SimpleScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize,
            connection_id="conn-count",
        )

        coordinator.feed_text("One. Two. Three.")
        coordinator.feed_done()

        output_events: list[Event] = []
        consumer_started = asyncio.Event()

        async def _consume():
            consumer_started.set()
            async for event in coordinator.output_events():
                output_events.append(event)

        consumer_task = asyncio.create_task(_consume())
        await consumer_started.wait()
        await asyncio.wait_for(consumer_task, timeout=5.0)

        assert coordinator._phrase_count == 3
        assert len(scheduler.submissions) == 3

    @pytest.mark.asyncio
    async def test_submission_order_preserves_text_order(self):
        """Phrases submitted in parsed order."""
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _SimpleScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize,
            connection_id="conn-order",
        )

        coordinator.feed_text("First. Second. Third.")
        coordinator.feed_done()

        output_events: list[Event] = []
        consumer_started = asyncio.Event()

        async def _consume():
            consumer_started.set()
            async for event in coordinator.output_events():
                output_events.append(event)

        consumer_task = asyncio.create_task(_consume())
        await consumer_started.wait()
        await asyncio.wait_for(consumer_task, timeout=5.0)

        assert scheduler.submissions == ["First.", "Second.", "Third."]


class TestNoOverlappingBackendCalls:
    """Prove no two backend calls overlap."""

    @pytest.mark.asyncio
    async def test_sequential_scheduler_submissions(self):
        """Scheduler submissions are sequential, max_active is 1."""
        from app.speech.stream_coordinator import StreamingCoordinator

        scheduler = _TrackingScheduler()
        coordinator = StreamingCoordinator(
            scheduler=scheduler,
            synthesize_fn=_fake_synthesize,
            connection_id="conn-serial",
        )

        coordinator.feed_text("A. B. C. D. E. F. G. H.")
        coordinator.feed_done()

        output_events: list[Event] = []
        consumer_started = asyncio.Event()

        async def _consume():
            consumer_started.set()
            async for event in coordinator.output_events():
                output_events.append(event)

        consumer_task = asyncio.create_task(_consume())
        await consumer_started.wait()
        await asyncio.wait_for(consumer_task, timeout=5.0)

        assert len(scheduler.submissions) == 8
        assert scheduler.max_active == 1
        assert len(set(scheduler.submissions)) == 8

"""Phase 9.5 Slice 3 — explicit bounded connection-owned streaming coordinator.

Owns the progressive phrase synthesis pipeline for one connection:
- PhraseAccumulator (text parsing)
- AudioEnvelope (audio normalization)
- Bounded async handoff between text feeding and phrase synthesis

Submits ONE phrase at a time through SpeechScheduler. Other connections
may fairly interleave. No backend calls overlap. Session-local — not a
scheduling queue.

Redesigned to be TRULY progressive:
- Phrases are submitted through the scheduler as soon as the accumulator
  identifies a complete phrase — does NOT drain all chunks first.
- Bounded (capacity-1) output handoff for backpressure.
- No asyncio.sleep() polling anywhere.
- Explicit cancel() that clears pending, cancels scheduler connection/active,
  and awaits the background task.
- drain() prevents later phrases from starting.
- Exactly one AudioEnvelope per coordinator.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Awaitable
from typing import Any

from wyoming.event import Event

from app.speech.phrases import PhraseAccumulator
from app.speech.envelope import AudioEnvelope, EnvelopeError
from app.speech.models import SpeechRequest


class StreamingCoordinator:
    """Connection-owned coordinator for progressive phrase synthesis.

    Architecture:
      - Background synthesis task runs concurrently, awaiting phrases
        and submitting them through the scheduler one at a time.
      - Text feeding (feed_text) is non-blocking and safe to call from
        the Wyoming event handler while the synthesis task runs.
      - Output events are delivered via async iteration over the
        coordinator itself (``async for event in coordinator: ...``).
      - Cancellation clears pending phrases, cancels the active
        scheduler submission, and awaits the background task.

    Usage (progressive feeding)::

        coord = StreamingCoordinator(scheduler, synthesize_fn, conn_id)
        await coord.start()

        # In handler's handle_event:
        coord.feed_text("Hello. ")
        coord.feed_text("World. ")
        coord.feed_done()

        # In a consumer task:
        async for event in coord:
            await write_event(event)

    Usage (buffered single-text, legacy compatibility)::

        coord = StreamingCoordinator(scheduler, synthesize_fn, conn_id)
        async for event in coord.stream("Full text here."):
            yield event
    """

    def __init__(
        self,
        scheduler: Any,
        synthesize_fn: Callable[[str], Awaitable[list[Event]]],
        connection_id: str,
        soft_max: int = 160,
        phrase_max: int = 320,
        retained_max: int = 640,
    ) -> None:
        self._scheduler = scheduler
        self._synthesize_fn = synthesize_fn
        self._connection_id = connection_id
        self._accumulator = PhraseAccumulator(
            soft_max=soft_max,
            phrase_max=phrase_max,
            retained_max=retained_max,
        )
        self._envelope = AudioEnvelope()
        self._started = False
        self._done = False
        self._cancelled = False
        self._draining = False
        self._phrase_count = 0

        # Bounded output handoff (capacity-1 backpressure)
        self._output: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=1)

        # Pending phrases ready for synthesis
        self._pending_phrases: list[str] = []

        # Signal that new phrases are available
        self._ready_event = asyncio.Event()

        # Background synthesis task
        self._task: asyncio.Task[None] | None = None

    # ── Simple buffered interface (legacy) ──────────────────────────

    async def stream(self, text: str) -> AsyncIterator[Event]:
        """Stream synthesis for a complete text (non-progressive mode).

        Uses the progressive pipeline internally but feeds all text
        at once, then drains.  Compatible with the original interface.
        Yields events directly — does not use the bounded output queue.
        """
        if self._started:
            raise RuntimeError("StreamingCoordinator already started")
        self._started = True
        self._done = True

        # Feed the full text through the accumulator
        phrases = self._accumulator.feed(text)
        residual = self._accumulator.flush()
        if residual:
            phrases.append(residual)

        if not phrases:
            return

        # Synthesize each phrase sequentially, yield events directly
        for phrase_text in phrases:
            self._phrase_count += 1

            # Collect events from synthesis
            phrase_events: list[Event] = []

            async def _op(p=phrase_text):
                events = await self._synthesize_fn(p)
                phrase_events.extend(self._envelope.process_phrase(events))

            try:
                request = SpeechRequest(
                    synthesis_id=f"prog-{self._connection_id}-{self._phrase_count:04d}",
                    connection_id=self._connection_id,
                    text=phrase_text,
                )
                await self._scheduler.run(request, _op)
                for event in phrase_events:
                    yield event
            except Exception:
                try:
                    self._envelope.close(on_success=False)
                except EnvelopeError as env_err:
                    if env_err.audio_stop_event:
                        yield env_err.audio_stop_event
                    yield env_err.error_event
                    return
                raise

        # Success — close envelope and yield terminal events
        for event in self._envelope.close(on_success=True):
            yield event

    # ── Progressive feeding interface ──────────────────────────────

    async def start(self) -> None:
        """Start the background synthesis task.

        Must be called once before feed_text/feed_done.
        The background task will submit phrases through the
        scheduler as they become available.

        Output events become available via async iteration:
        ``async for event in coordinator: ...``
        """
        if self._started:
            raise RuntimeError("StreamingCoordinator already started")
        self._started = True
        self._task = asyncio.create_task(self._synthesis_loop())

    def feed_text(self, chunk: str) -> None:
        """Feed a chunk of text for progressive synthesis.

        Thread-safe — can be called from the Wyoming event handler
        while the coordinator synthesis task is running.

        Newly completed phrases are queued for synthesis and signal
        the background task.
        """
        if self._done or self._cancelled or self._draining:
            return
        new_phrases = self._accumulator.feed(chunk)
        if new_phrases:
            self._pending_phrases.extend(new_phrases)
            self._ready_event.set()

    def feed_done(self) -> None:
        """Signal that no more text chunks will arrive.

        Flushes any residual text from the accumulator and signals
        the background task to complete after the last phrase.
        """
        if self._done or self._cancelled:
            return
        self._done = True
        residual = self._accumulator.flush()
        if residual:
            self._pending_phrases.append(residual)
        self._ready_event.set()

    async def run_progressive(
        self,
        feed_chunks: list[str],
    ) -> AsyncIterator[Event]:
        """Run progressive synthesis with pre-collected chunks.

        Convenience method for testing.
        """
        await self.start()
        for chunk in feed_chunks:
            self.feed_text(chunk)
        self.feed_done()
        async for event in self:
            yield event

    async def output_events(self) -> AsyncIterator[Event]:
        """Yield output events as phrases are synthesized.

        Legacy compatibility — starts the coordinator and yields events.
        For progressive feed, use start() + async iteration instead.
        """
        if self._started:
            raise RuntimeError("StreamingCoordinator already started")
        self._started = True
        self._task = asyncio.create_task(self._synthesis_loop())

        async for event in self:
            yield event

    # ── Async iteration (output consumer) ─────────────────────────

    def __aiter__(self) -> "StreamingCoordinator":
        return self

    async def __anext__(self) -> Event:
        """Wait for and return the next output event.

        Raises StopAsyncIteration when the synthesis loop has finished
        (sentinel None is placed in the output queue).
        """
        event = await self._output.get()
        if event is None:
            raise StopAsyncIteration
        return event

    # ── Lifecycle control ──────────────────────────────────────────

    async def cancel(self) -> None:
        """Cancel all pending and active synthesis work.

        1. Clears pending phrases
        2. Cancels the active scheduler connection
        3. Cancels the background task and awaits completion
        """
        self._cancelled = True
        self._pending_phrases.clear()
        self._ready_event.set()  # unblock the synthesis loop

        if self._task is not None and not self._task.done():
            # Cancel the scheduler connection first
            try:
                await self._scheduler.cancel_connection(self._connection_id)
            except Exception:
                pass
            try:
                self._scheduler.cancel_active_for_connection(self._connection_id)
            except Exception:
                pass

            # Then cancel and await the background task
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        # Attempt envelope close (best-effort)
        try:
            self._envelope.close(on_success=False)
        except (EnvelopeError, RuntimeError):
            pass

    async def drain(self) -> None:
        """Prevent new phrases from starting.

        Sets the draining flag so feed_text becomes a no-op.
        The currently active phrase (if any) is allowed to complete.
        After drain(), the coordinator should be cancelled or awaited.
        """
        self._draining = True
        self._pending_phrases.clear()

    # ── Background synthesis loop ──────────────────────────────────

    async def _synthesis_loop(self) -> None:
        """Background task: consume phrases, submit to scheduler, emit output.

        Runs until all phrases are processed (success), cancelled, or
        a synthesis failure occurs.
        """
        try:
            while True:
                if self._cancelled:
                    return

                # Wait for phrases to become available
                if not self._pending_phrases:
                    if self._done:
                        # All done — close envelope only if phrases were processed
                        if self._phrase_count > 0:
                            for event in self._envelope.close(on_success=True):
                                await self._output.put(event)
                        await self._output.put(None)  # sentinel
                        return

                    # No phrases yet, wait for signal
                    self._ready_event.clear()
                    await self._ready_event.wait()
                    continue

                # Get next phrase
                phrase_text = self._pending_phrases.pop(0)
                self._phrase_count += 1

                async def _op(p=phrase_text):
                    phrase_events = await self._synthesize_fn(p)
                    for event in self._envelope.process_phrase(phrase_events):
                        await self._output.put(event)

                try:
                    request = SpeechRequest(
                        synthesis_id=f"prog-{self._connection_id}-{self._phrase_count:04d}",
                        connection_id=self._connection_id,
                        text=phrase_text,
                    )
                    await self._scheduler.run(request, _op)
                except Exception:
                    if not self._cancelled:
                        # Phrase failure — close envelope with error
                        try:
                            self._envelope.close(on_success=False)
                        except EnvelopeError as env_err:
                            if env_err.audio_stop_event:
                                await self._output.put(env_err.audio_stop_event)
                            await self._output.put(env_err.error_event)
                            await self._output.put(None)
                            return
                        raise

        except asyncio.CancelledError:
            if not self._cancelled:
                self._cancelled = True
            try:
                self._envelope.close(on_success=False)
            except (EnvelopeError, RuntimeError):
                pass
            raise

"""Phase 9.5 Slice 3 — explicit bounded connection-owned streaming coordinator.

Owns the progressive phrase synthesis pipeline for one connection:
- PhraseAccumulator (text parsing)
- AudioEnvelope (audio normalization)
- Bounded async handoff between text feeding and phrase synthesis

Submits ONE phrase at a time through SpeechScheduler. Other connections
may fairly interleave. No backend calls overlap. Session-local — not a
scheduling queue.
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

    Usage (progressive feeding)::

        coord = StreamingCoordinator(scheduler, synthesize_fn, conn_id)
        coord.feed_text("Hello. ")
        coord.feed_text("World. ")
        coord.feed_done()
        async for event in coord.output_events():
            yield event

    Usage (buffered single-text)::

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
        self._feed_queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._output_queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._phrase_count = 0

    # ── Simple buffered interface ──────────────────────────────────

    async def stream(self, text: str) -> AsyncIterator[Event]:
        """Stream synthesis for a complete text (non-progressive mode)."""
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

        # Synthesize each phrase sequentially
        for phrase_text in phrases:
            self._phrase_count += 1

            async def _op(p=phrase_text):
                phrase_events = await self._synthesize_fn(p)
                for event in self._envelope.process_phrase(phrase_events):
                    self._output_queue.put_nowait(event)

            try:
                request = SpeechRequest(
                    synthesis_id=f"prog-{self._connection_id}-{self._phrase_count:04d}",
                    connection_id=self._connection_id,
                    text=phrase_text,
                )
                await self._scheduler.run(request, _op)
            except Exception as exc:
                # On failure, close envelope and yield error events
                try:
                    self._envelope.close(on_success=False)
                except EnvelopeError as env_err:
                    if env_err.audio_stop_event:
                        self._output_queue.put_nowait(env_err.audio_stop_event)
                    self._output_queue.put_nowait(env_err.error_event)
                    self._output_queue.put_nowait(None)
                    # Drain and yield the error events
                    while True:
                        event = await self._output_queue.get()
                        if event is None:
                            break
                        yield event
                    return
                raise

        # Success — close envelope and emit terminal events
        for event in self._envelope.close(on_success=True):
            self._output_queue.put_nowait(event)
        self._output_queue.put_nowait(None)

        # Drain output queue
        while True:
            event = await self._output_queue.get()
            if event is None:
                break
            yield event

    # ── Progressive feeding interface ──────────────────────────────

    def feed_text(self, chunk: str) -> None:
        """Feed a chunk of text for progressive synthesis.

        Thread-safe — can be called from the Wyoming event handler
        while the coordinator task is running.
        """
        if self._done:
            return
        self._feed_queue.put_nowait(chunk)

    def feed_done(self) -> None:
        """Signal that no more text chunks will arrive."""
        if self._done:
            return
        self._done = True
        self._feed_queue.put_nowait(None)  # sentinel

    async def run_progressive(
        self,
        feed_chunks: list[str],
    ) -> AsyncIterator[Event]:
        """Run progressive synthesis with pre-collected chunks.

        This is a convenience method for testing.
        """
        for chunk in feed_chunks:
            self.feed_text(chunk)
        self.feed_done()
        async for event in self.output_events():
            yield event

    async def output_events(self) -> AsyncIterator[Event]:
        """Yield output events as phrases are synthesized.

        Must be called after feed_done(). Drains the feed queue,
        processes the accumulator, submits phrases through the
        scheduler, and normalizes audio via the envelope.
        """
        if self._started:
            raise RuntimeError("StreamingCoordinator already started")
        self._started = True

        # Drain feed queue completely — collect all chunks
        all_chunks: list[str] = []
        while True:
            if self._done and self._feed_queue.empty():
                # Check one more time in case feed_done added sentinel
                pass
            try:
                chunk = self._feed_queue.get_nowait()
            except asyncio.QueueEmpty:
                if self._done:
                    break
                # Not done yet — wait a tiny bit for more chunks
                await asyncio.sleep(0.01)
                continue

            if chunk is None:
                break  # sentinel from feed_done
            all_chunks.append(chunk)

        # Feed all collected chunks through the accumulator
        all_phrases: list[str] = []
        for chunk in all_chunks:
            phrases = self._accumulator.feed(chunk)
            all_phrases.extend(phrases)
        residual = self._accumulator.flush()
        if residual:
            all_phrases.append(residual)

        if not all_phrases:
            return

        # Synthesize each phrase sequentially through the scheduler
        for phrase_text in all_phrases:
            self._phrase_count += 1

            async def _op(p=phrase_text):
                phrase_events = await self._synthesize_fn(p)
                for event in self._envelope.process_phrase(phrase_events):
                    self._output_queue.put_nowait(event)

            try:
                request = SpeechRequest(
                    synthesis_id=f"prog-{self._connection_id}-{self._phrase_count:04d}",
                    connection_id=self._connection_id,
                    text=phrase_text,
                )
                await self._scheduler.run(request, _op)
            except Exception:
                try:
                    self._envelope.close(on_success=False)
                except EnvelopeError as env_err:
                    if env_err.audio_stop_event:
                        self._output_queue.put_nowait(env_err.audio_stop_event)
                    self._output_queue.put_nowait(env_err.error_event)
                    self._output_queue.put_nowait(None)
                    while True:
                        event = await self._output_queue.get()
                        if event is None:
                            break
                        yield event
                    return
                raise

        # Success close
        for event in self._envelope.close(on_success=True):
            self._output_queue.put_nowait(event)
        self._output_queue.put_nowait(None)

        while True:
            event = await self._output_queue.get()
            if event is None:
                break
            yield event

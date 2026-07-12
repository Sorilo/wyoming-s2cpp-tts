"""Phase 9.5 Slice 2 — logical audio-envelope normalizer.

Owns continuous audio format metadata, cumulative frame accounting,
and exactly-once terminal event emission across multiple backend
phrase synthesis operations for one logical Wyoming response.

Responsibilities:
- Forward AudioStart exactly once; lock rate/width/channels on first.
- Validate format consistency on subsequent phrases.
- Suppress internal phrase AudioStop events.
- Rebuild AudioChunk timestamps from cumulative emitted PCM frames.
- On success: emit one logical AudioStop at cumulative frame time.
- On failure after AudioStart: emit one AudioStop then Error.
- On failure before AudioStart: emit only Error.
- Frame-alignment validation for all audio payloads.
"""

from __future__ import annotations

from wyoming.audio import AudioStart, AudioStop, AudioChunk
from wyoming.error import Error as WyomingError
from wyoming.event import Event


class EnvelopeError(Exception):
    """Raised on terminal failures after partial audio.

    Carries the closing Wyoming events so the handler can emit them
    without replaying prior audio or emitting SynthesizeStopped.
    """
    def __init__(
        self,
        error_event: Event,
        audio_stop_event: Event | None = None,
    ) -> None:
        self.error_event = error_event
        self.audio_stop_event = audio_stop_event
        detail = WyomingError.from_event(error_event).text
        super().__init__(f"Envelope error: {detail}")


class AudioEnvelope:
    """Logical audio-envelope normalizer for one Wyoming response.

    Usage::

        env = AudioEnvelope()
        for phrase_events in phrase_backend_outputs:
            for event in env.process_phrase(phrase_events):
                yield event  # write to client
        # On success:
        for event in env.close(on_success=True):
            yield event
        # On failure after partial audio:
        try:
            env.close(on_success=False)
        except EnvelopeError as exc:
            if exc.audio_stop_event:
                yield exc.audio_stop_event
            yield exc.error_event
    """

    def __init__(self) -> None:
        self._rate: int | None = None
        self._width: int | None = None
        self._channels: int | None = None
        self._cumulative_frames: int = 0
        self._audio_start_emitted: bool = False
        self._closed: bool = False
        self._audio_stop_emitted: bool = False
        self._final_stop_event: Event | None = None

    @property
    def cumulative_frames(self) -> int:
        return self._cumulative_frames

    # ── Phrase processing ─────────────────────────────────────────

    def process_phrase(self, events: list[Event]) -> list[Event]:
        """Process one phrase's Wyoming events.

        Args:
            events: [AudioStart, AudioChunk*, AudioStop] from one
            backend phrase synthesis.

        Returns:
            Filtered/normalized events ready for client emission.
        """
        if self._closed:
            raise RuntimeError("AudioEnvelope is already closed")

        if not events:
            return []

        output: list[Event] = []

        seen_start = False
        for event in events:
            if AudioStart.is_type(event.type):
                if seen_start:
                    raise ValueError("Multiple AudioStart in one phrase")
                seen_start = True
                output.extend(self._handle_audio_start(event))
            elif AudioChunk.is_type(event.type):
                if not seen_start:
                    raise ValueError("AudioChunk without preceding AudioStart")
                result = self._handle_audio_chunk(event)
                if result is not None:
                    output.append(result)
            elif AudioStop.is_type(event.type):
                # Suppress phrase AudioStop
                pass
            else:
                # Pass through unknown events (e.g., errors)
                output.append(event)

        if not seen_start:
            # Check if events list is non-empty but lacks AudioStart
            if any(not AudioStop.is_type(e.type) for e in events):
                raise ValueError("Phrase events missing required AudioStart")

        return output

    def _handle_audio_start(self, event: Event) -> list[Event]:
        start = AudioStart.from_event(event)
        rate = start.rate
        width = start.width
        channels = start.channels

        if not self._audio_start_emitted:
            # First start: lock format and forward
            self._rate = rate
            self._width = width
            self._channels = channels
            self._audio_start_emitted = True
            return [event]

        # Subsequent start: validate format consistency
        if (
            rate != self._rate
            or width != self._width
            or channels != self._channels
        ):
            raise ValueError(
                f"Audio format drift: expected rate={self._rate} "
                f"width={self._width} channels={self._channels}, "
                f"got rate={rate} width={width} channels={channels}"
            )

        # Suppress — already forwarded
        return []

    def _handle_audio_chunk(self, event: Event) -> Event | None:
        chunk = AudioChunk.from_event(event)
        audio = chunk.audio
        if not audio:
            return None

        frame_size = self._width * self._channels
        if len(audio) % frame_size != 0:
            raise ValueError(
                f"Audio chunk not frame-aligned: {len(audio)} bytes "
                f"(frame size {frame_size})"
            )

        frames = len(audio) // frame_size
        new_timestamp = int(
            self._cumulative_frames * 1000 / self._rate
        )
        self._cumulative_frames += frames

        return AudioChunk(
            rate=self._rate,
            width=self._width,
            channels=self._channels,
            audio=audio,
            timestamp=new_timestamp,
        ).event()

    # ── Terminal closure ─────────────────────────────────────────

    def close(self, on_success: bool) -> list[Event]:
        """Close the envelope and produce terminal events.

        Args:
            on_success: True if synthesis completed normally,
            False if a failure occurred.

        Returns:
            Terminal events on success; raises EnvelopeError on failure.

        Raises:
            RuntimeError: if already closed.
            EnvelopeError: on failure (carries closing events).
        """
        if self._closed:
            raise RuntimeError("AudioEnvelope is already closed")
        self._closed = True

        if on_success:
            return self._emit_success_close()

        # Failure path
        audio_stop = None
        if self._audio_start_emitted:
            audio_stop = self._build_audio_stop()
            self._audio_stop_emitted = True
            self._final_stop_event = audio_stop

        error = WyomingError(
            text="Audio synthesis failed",
            code="synthesis_failed",
        ).event()

        raise EnvelopeError(error_event=error, audio_stop_event=audio_stop)

    def _emit_success_close(self) -> list[Event]:
        events: list[Event] = []
        stop = self._build_audio_stop()
        events.append(stop)
        self._audio_stop_emitted = True
        return events

    def _build_audio_stop(self) -> Event:
        timestamp = int(
            self._cumulative_frames * 1000 / self._rate
        ) if self._rate else 0
        return AudioStop(timestamp=timestamp).event()

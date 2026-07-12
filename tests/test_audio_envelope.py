"""Phase 9.5 Slice 2 — AudioEnvelope tests (RED-GREEN-REFACTOR).

Tests written BEFORE production implementation (strict TDD).
"""

import pytest
from wyoming.audio import AudioStart, AudioStop, AudioChunk
from wyoming.event import Event


def _fake_phrase_events(
    rate: int = 22050,
    width: int = 2,
    channels: int = 1,
    num_chunks: int = 3,
    chunk_bytes: int = 882,
) -> list[Event]:
    """Return [AudioStart, AudioChunk*N, AudioStop] for one backend phrase."""
    events: list[Event] = [
        AudioStart(rate=rate, width=width, channels=channels).event(),
    ]
    for i in range(num_chunks):
        events.append(
            AudioChunk(
                rate=rate,
                width=width,
                channels=channels,
                audio=b"\x00" * chunk_bytes,
                timestamp=i * 20,
            ).event()
        )
    events.append(AudioStop(timestamp=num_chunks * 20).event())
    return events


def _fake_phrase_events_no_start(num_chunks: int = 3) -> list[Event]:
    """Return [AudioChunk*N, AudioStop] — no AudioStart (error case)."""
    events: list[Event] = []
    for i in range(num_chunks):
        events.append(
            AudioChunk(
                rate=22050, width=2, channels=1,
                audio=b"\x00" * 882, timestamp=i * 20,
            ).event()
        )
    events.append(AudioStop(timestamp=num_chunks * 20).event())
    return events


class TestAudioEnvelope:
    """Test the AudioEnvelope component (Slice 2)."""

    def test_forward_audio_start_once(self):
        from app.speech.envelope import AudioEnvelope

        envelope = AudioEnvelope()
        events = _fake_phrase_events()
        output = list(envelope.process_phrase(events))

        starts = [e for e in output if AudioStart.is_type(e.type)]
        assert len(starts) == 1

        events2 = _fake_phrase_events(rate=22050, width=2, channels=1)
        output2 = list(envelope.process_phrase(events2))
        starts2 = [e for e in output2 if AudioStart.is_type(e.type)]
        assert len(starts2) == 0, "Second AudioStart must be suppressed"

    def test_audio_stop_suppressed(self):
        from app.speech.envelope import AudioEnvelope

        envelope = AudioEnvelope()
        events = _fake_phrase_events()
        output = list(envelope.process_phrase(events))

        stops = [e for e in output if AudioStop.is_type(e.type)]
        assert len(stops) == 0, "Phrase AudioStop must be suppressed"

    def test_timestamps_rebuilt_from_cumulative_frames(self):
        from app.speech.envelope import AudioEnvelope

        envelope = AudioEnvelope()
        events = _fake_phrase_events(chunk_bytes=882)
        output = list(envelope.process_phrase(events))

        chunks = [e for e in output if AudioChunk.is_type(e.type)]
        for i, chunk_event in enumerate(chunks):
            chunk = AudioChunk.from_event(chunk_event)
            expected_ts = i * 20
            assert chunk.timestamp == expected_ts, (
                f"Chunk {i}: expected timestamp {expected_ts}, got {chunk.timestamp}"
            )

    def test_multiple_phrases_continuous_timestamps(self):
        from app.speech.envelope import AudioEnvelope

        envelope = AudioEnvelope()
        p1 = list(envelope.process_phrase(_fake_phrase_events(num_chunks=3)))
        chunks1 = [e for e in p1 if AudioChunk.is_type(e.type)]
        p2 = list(envelope.process_phrase(_fake_phrase_events(num_chunks=2)))
        chunks2 = [e for e in p2 if AudioChunk.is_type(e.type)]

        last_ts_p1 = AudioChunk.from_event(chunks1[-1]).timestamp
        first_ts_p2 = AudioChunk.from_event(chunks2[0]).timestamp
        assert first_ts_p2 > last_ts_p1

    def test_format_validation_rejects_drift(self):
        from app.speech.envelope import AudioEnvelope

        envelope = AudioEnvelope()
        list(envelope.process_phrase(_fake_phrase_events(rate=22050, width=2, channels=1)))
        with pytest.raises(ValueError, match="format"):
            list(envelope.process_phrase(_fake_phrase_events(rate=44100, width=2, channels=1)))

    def test_format_validation_rejects_width_drift(self):
        from app.speech.envelope import AudioEnvelope

        envelope = AudioEnvelope()
        list(envelope.process_phrase(_fake_phrase_events(rate=22050, width=2, channels=1)))
        with pytest.raises(ValueError, match="format"):
            list(envelope.process_phrase(_fake_phrase_events(rate=22050, width=4, channels=1)))

    def test_format_validation_rejects_channel_drift(self):
        from app.speech.envelope import AudioEnvelope

        envelope = AudioEnvelope()
        list(envelope.process_phrase(_fake_phrase_events(rate=22050, width=2, channels=1)))
        with pytest.raises(ValueError, match="format"):
            list(envelope.process_phrase(_fake_phrase_events(rate=22050, width=2, channels=2)))

    def test_close_on_success_emits_audio_stop(self):
        from app.speech.envelope import AudioEnvelope

        envelope = AudioEnvelope()
        list(envelope.process_phrase(_fake_phrase_events(num_chunks=3)))
        output = list(envelope.close(on_success=True))

        stops = [e for e in output if AudioStop.is_type(e.type)]
        assert len(stops) == 1
        stop = AudioStop.from_event(stops[0])
        assert stop.timestamp == 60

    def test_close_on_failure_after_audio_start(self):
        from app.speech.envelope import AudioEnvelope, EnvelopeError
        from wyoming.error import Error as WError

        envelope = AudioEnvelope()
        list(envelope.process_phrase(_fake_phrase_events(num_chunks=2)))

        with pytest.raises(EnvelopeError) as exc_info:
            envelope.close(on_success=False)

        assert exc_info.value.audio_stop_event is not None
        assert AudioStop.is_type(exc_info.value.audio_stop_event.type)
        assert WError.is_type(exc_info.value.error_event.type)

    def test_close_on_failure_before_audio_start(self):
        from app.speech.envelope import AudioEnvelope, EnvelopeError
        from wyoming.error import Error as WError

        envelope = AudioEnvelope()
        with pytest.raises(EnvelopeError) as exc_info:
            envelope.close(on_success=False)

        err = exc_info.value.error_event
        assert WError.is_type(err.type)
        assert exc_info.value.audio_stop_event is None

    def test_close_exactly_once(self):
        from app.speech.envelope import AudioEnvelope

        envelope = AudioEnvelope()
        list(envelope.process_phrase(_fake_phrase_events()))
        list(envelope.close(on_success=True))
        with pytest.raises(RuntimeError, match="already closed"):
            list(envelope.close(on_success=True))

    def test_process_after_close_rejected(self):
        from app.speech.envelope import AudioEnvelope

        envelope = AudioEnvelope()
        list(envelope.process_phrase(_fake_phrase_events()))
        list(envelope.close(on_success=True))
        with pytest.raises(RuntimeError, match="already closed"):
            list(envelope.process_phrase(_fake_phrase_events()))

    def test_no_audio_start_in_phrase_events(self):
        from app.speech.envelope import AudioEnvelope

        envelope = AudioEnvelope()
        with pytest.raises(ValueError, match="AudioStart"):
            list(envelope.process_phrase(_fake_phrase_events_no_start()))

    def test_frame_alignment_validation(self):
        from app.speech.envelope import AudioEnvelope

        envelope = AudioEnvelope()
        events = [
            AudioStart(rate=22050, width=2, channels=1).event(),
            AudioChunk(
                rate=22050, width=2, channels=1,
                audio=b"\x00" * 3,
                timestamp=0,
            ).event(),
        ]
        with pytest.raises(ValueError, match="frame"):
            list(envelope.process_phrase(events))

    def test_empty_phrase_events(self):
        from app.speech.envelope import AudioEnvelope

        envelope = AudioEnvelope()
        output = list(envelope.process_phrase([]))
        assert output == []

    def test_cumulative_frame_tracking(self):
        from app.speech.envelope import AudioEnvelope

        envelope = AudioEnvelope()
        list(envelope.process_phrase(_fake_phrase_events(num_chunks=3, chunk_bytes=882)))
        assert envelope.cumulative_frames == 1323

        list(envelope.process_phrase(_fake_phrase_events(num_chunks=2, chunk_bytes=882)))
        assert envelope.cumulative_frames == 2205

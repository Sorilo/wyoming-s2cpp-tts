import asyncio

import pytest
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize

from app.config import Settings
from app.s2_client import S2GenerateResult
from app.wyoming_server import (
    FakeTtsConfig,
    start_fake_tts_server,
    synthesize_s2cpp_tts_events,
)


REAL_PCM_HEADERS = {
    "x-audio-encoding": "pcm_s16le",
    "x-audio-channels": "1",
    "x-audio-sample-rate": "44100",
}
REAL_PCM_CONTENT_TYPE = "audio/L16; rate=44100; channels=1"


class RecordingS2Client:
    def __init__(
        self,
        audio: bytes,
        *,
        content_type: str = REAL_PCM_CONTENT_TYPE,
        response_headers: dict[str, str] | None = None,
    ):
        self.audio = audio
        self.content_type = content_type
        self.response_headers = REAL_PCM_HEADERS.copy() if response_headers is None else response_headers
        self.requests = []

    def generate_multipart(self, request):
        self.requests.append(request)
        return S2GenerateResult(
            audio=self.audio,
            content_type=self.content_type,
            response_headers=self.response_headers.copy(),
        )


def _audio_events(events):
    return [AudioChunk.from_event(event) for event in events if AudioChunk.is_type(event.type)]


def test_s2cpp_backend_converts_buffered_result_to_wyoming_audio_events():
    pcm = b"\x01\x00\x02\x00\x03\x00\x04\x00"
    client = RecordingS2Client(audio=pcm)
    settings = Settings(tts_backend="s2cpp", s2_default_voice="voice-a")
    config = FakeTtsConfig(sample_rate=8000, duration_ms=120, chunk_ms=1)

    events = synthesize_s2cpp_tts_events(
        "hello backend",
        client=client,
        settings=settings,
        config=config,
    )

    assert client.requests[0].text == "hello backend"
    assert client.requests[0].voice == "voice-a"
    assert AudioStart.is_type(events[0].type)
    assert AudioStop.is_type(events[-1].type)
    chunks = _audio_events(events)
    assert b"".join(chunk.audio for chunk in chunks) == pcm
    assert all(chunk.rate == 44100 for chunk in chunks)
    assert all(chunk.width == 2 for chunk in chunks)
    assert all(chunk.channels == 1 for chunk in chunks)


def test_real_contract_buffered_pcm_sets_wyoming_metadata_from_backend_headers():
    pcm = b"\x01\x00" * 100
    client = RecordingS2Client(audio=pcm)
    settings = Settings(tts_backend="s2cpp")

    events = synthesize_s2cpp_tts_events(
        "real contract",
        client=client,
        settings=settings,
        config=FakeTtsConfig(sample_rate=22050, chunk_ms=100),
    )

    start = AudioStart.from_event(events[0])
    chunks = _audio_events(events)
    assert start.rate == 44100
    assert start.width == 2
    assert start.channels == 1
    assert chunks
    assert all(chunk.rate == 44100 for chunk in chunks)
    assert all(chunk.channels == 1 for chunk in chunks)
    assert b"".join(chunk.audio for chunk in chunks) == pcm


def test_buffered_pcm_missing_metadata_is_rejected():
    client = RecordingS2Client(
        audio=b"\x01\x00",
        content_type="audio/L16",
        response_headers={},
    )

    with pytest.raises(ValueError, match="missing PCM metadata"):
        synthesize_s2cpp_tts_events(
            "missing metadata",
            client=client,
            settings=Settings(tts_backend="s2cpp"),
        )


def test_buffered_pcm_unaligned_payload_is_rejected():
    client = RecordingS2Client(audio=b"\x01\x00\x02")

    with pytest.raises(ValueError, match="not frame-aligned"):
        synthesize_s2cpp_tts_events(
            "unaligned",
            client=client,
            settings=Settings(tts_backend="s2cpp"),
        )


def test_unknown_buffered_binary_response_is_not_treated_as_audio():
    client = RecordingS2Client(
        audio=b"\x01\x00\x02\x00",
        content_type="application/octet-stream",
        response_headers={},
    )

    with pytest.raises(ValueError, match="unsupported PCM response"):
        synthesize_s2cpp_tts_events(
            "unknown binary",
            client=client,
            settings=Settings(tts_backend="s2cpp"),
        )


def test_s2cpp_backend_roundtrip_over_wyoming_tcp_with_mocked_client():
    async def scenario():
        pcm = b"\x05\x00\x06\x00\x07\x00\x08\x00"
        recording_client = RecordingS2Client(audio=pcm)
        settings = Settings(tts_backend="s2cpp")
        server = await start_fake_tts_server(
            host="127.0.0.1",
            port=0,
            settings=settings,
            s2_client_factory=lambda _settings: recording_client,
        )
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as client:
                await client.write_event(Synthesize(text="hello over tcp").event())
                events = []
                while True:
                    event = await asyncio.wait_for(client.read_event(), timeout=2)
                    assert event is not None
                    events.append(event)
                    if AudioStop.is_type(event.type):
                        break

            assert recording_client.requests[0].text == "hello over tcp"
            assert AudioStart.is_type(events[0].type)
            assert AudioStop.is_type(events[-1].type)
            chunks = _audio_events(events)
            assert b"".join(chunk.audio for chunk in chunks) == pcm
            assert all(chunk.rate == 44100 for chunk in chunks)
        finally:
            await server.stop()

    asyncio.run(scenario())

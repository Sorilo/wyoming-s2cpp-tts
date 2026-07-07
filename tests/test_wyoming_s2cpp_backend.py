import asyncio

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


class RecordingS2Client:
    def __init__(self, audio: bytes):
        self.audio = audio
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return S2GenerateResult(audio=self.audio, content_type="audio/L16")


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
    chunks = [AudioChunk.from_event(event) for event in events if AudioChunk.is_type(event.type)]
    assert b"".join(chunk.audio for chunk in chunks) == pcm
    assert all(chunk.rate == 8000 for chunk in chunks)
    assert all(chunk.width == 2 for chunk in chunks)
    assert all(chunk.channels == 1 for chunk in chunks)


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
            chunks = [AudioChunk.from_event(event) for event in events if AudioChunk.is_type(event.type)]
            assert b"".join(chunk.audio for chunk in chunks) == pcm
        finally:
            await server.stop()

    asyncio.run(scenario())

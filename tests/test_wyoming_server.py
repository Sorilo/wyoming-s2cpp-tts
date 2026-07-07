import asyncio

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.info import Describe, Info
from wyoming.tts import Synthesize

from app.wyoming_server import (
    FakeTtsConfig,
    parse_tcp_uri,
    start_fake_tts_server,
    synthesize_fake_tts_events,
)


def test_parse_tcp_uri_returns_host_and_port():
    assert parse_tcp_uri("tcp://0.0.0.0:10200") == ("0.0.0.0", 10200)


def test_fake_tts_events_are_deterministic_pcm_sequence():
    config = FakeTtsConfig(sample_rate=8000, duration_ms=120, chunk_ms=40)

    first = synthesize_fake_tts_events("hello", config=config)
    second = synthesize_fake_tts_events("hello", config=config)

    assert [event.type for event in first] == [event.type for event in second]
    assert first[0].type == AudioStart(8000, 2, 1).event().type
    assert first[-1].type == AudioStop().event().type

    chunks = [AudioChunk.from_event(event) for event in first if AudioChunk.is_type(event.type)]
    assert len(chunks) == 3
    assert all(chunk.rate == 8000 for chunk in chunks)
    assert all(chunk.width == 2 for chunk in chunks)
    assert all(chunk.channels == 1 for chunk in chunks)
    assert b"".join(chunk.audio for chunk in chunks) == b"".join(
        AudioChunk.from_event(event).audio for event in second if AudioChunk.is_type(event.type)
    )
    assert any(byte != 0 for chunk in chunks for byte in chunk.audio)


def test_fake_tts_server_roundtrip_over_wyoming_tcp():
    async def scenario():
        server = await start_fake_tts_server(host="127.0.0.1", port=0)
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as client:
                await client.write_event(Describe().event())
                info_event = await client.read_event()
                assert info_event is not None
                assert Info.is_type(info_event.type)
                info = Info.from_event(info_event)
                assert info.tts
                assert info.tts[0].name == "wyoming-s2cpp-tts-fake"

                await client.write_event(Synthesize(text="hello from phase one").event())
                events = []
                while True:
                    event = await asyncio.wait_for(client.read_event(), timeout=2)
                    assert event is not None
                    events.append(event)
                    if AudioStop.is_type(event.type):
                        break

                assert AudioStart.is_type(events[0].type)
                assert any(AudioChunk.is_type(event.type) for event in events)
                assert AudioStop.is_type(events[-1].type)
        finally:
            await server.stop()

    asyncio.run(scenario())

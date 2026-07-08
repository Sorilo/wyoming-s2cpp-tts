"""Phase 6C: Wyoming streaming TTS protocol tests.

These tests verify the complete streaming state machine in
``FakeTtsEventHandler``:
  - synthesize-start / synthesize-chunk / synthesize-stop
  - Progressive AudioStart / AudioChunk / AudioStop
  - synthesize-stopped after final audio
  - Legacy synthesize still works
  - No duplicate synthesis from legacy + streaming compatibility
  - Clean connection completion
"""

import asyncio

import pytest
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.tts import (
    Synthesize,
    SynthesizeChunk,
    SynthesizeStart,
    SynthesizeStop,
    SynthesizeStopped,
)

from app.wyoming_server import start_fake_tts_server


async def _collect_all(client, timeout=5):
    """Read all events until timeout or synthesize-stopped."""
    events = []
    while True:
        try:
            ev = await asyncio.wait_for(client.read_event(), timeout=timeout)
        except asyncio.TimeoutError:
            break
        if ev is None:
            break
        events.append(ev)
        if SynthesizeStopped.is_type(ev.type):
            break
    return events


# ── Streaming state machine tests ──────────────────────────────────────────


class TestStreamingProtocol:
    """Full streaming TTS state machine: start → chunks → stop → audio → stopped."""

    @pytest.mark.asyncio
    async def test_streaming_basic_flow(self):
        """synthesize-start + chunks + stop produces audio + synthesize-stopped."""
        server = await start_fake_tts_server(host="127.0.0.1", port=0)
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as client:
                # Start
                await client.write_event(SynthesizeStart().event())
                # Chunks
                await client.write_event(SynthesizeChunk(text="hello").event())
                await client.write_event(SynthesizeChunk(text="world").event())
                # Stop
                await client.write_event(SynthesizeStop().event())

                events = await _collect_all(client, timeout=5)

            # Verify event sequence
            types = [e.type for e in events]
            assert "audio-start" in types, f"No AudioStart in {types}"
            assert any("audio-chunk" in t for t in types), f"No AudioChunk in {types}"
            assert "audio-stop" in types, f"No AudioStop in {types}"
            assert "synthesize-stopped" in types, (
                f"No synthesize-stopped in {types}"
            )
            # synthesize-stopped must be the last event
            assert types[-1] == "synthesize-stopped", (
                f"synthesize-stopped not last: {types}"
            )
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_streaming_no_chunks_sends_empty_start_stop(self):
        """synthesize-start + stop with no chunks still completes cleanly."""
        server = await start_fake_tts_server(host="127.0.0.1", port=0)
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as client:
                await client.write_event(SynthesizeStart().event())
                await client.write_event(SynthesizeStop().event())
                events = await _collect_all(client, timeout=5)

            types = [e.type for e in events]
            # No text → no audio → just synthesize-stopped
            assert types == ["synthesize-stopped"], f"Unexpected: {types}"
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_streaming_multiple_sessions(self):
        """Two streaming sessions in one connection both work."""
        server = await start_fake_tts_server(host="127.0.0.1", port=0)
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as client:
                # Session 1
                await client.write_event(SynthesizeStart().event())
                await client.write_event(SynthesizeChunk(text="first").event())
                await client.write_event(SynthesizeStop().event())
                events1 = await _collect_all(client, timeout=5)
                assert "synthesize-stopped" in [e.type for e in events1]

                # Session 2
                await client.write_event(SynthesizeStart().event())
                await client.write_event(SynthesizeChunk(text="second").event())
                await client.write_event(SynthesizeStop().event())
                events2 = await _collect_all(client, timeout=5)
                assert "synthesize-stopped" in [e.type for e in events2]

                # Verify both sessions produced audio
                chunks1 = [e for e in events1 if AudioChunk.is_type(e.type)]
                chunks2 = [e for e in events2 if AudioChunk.is_type(e.type)]
                assert len(chunks1) > 0
                assert len(chunks2) > 0
                # Different text should produce different audio
                audio1 = b"".join(AudioChunk.from_event(c).audio for c in chunks1)
                audio2 = b"".join(AudioChunk.from_event(c).audio for c in chunks2)
                assert audio1 != audio2, "Different texts should produce different audio"
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_legacy_synthesize_still_works(self):
        """Classic synthesize event still produces audio."""
        server = await start_fake_tts_server(host="127.0.0.1", port=0)
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as client:
                await client.write_event(Synthesize(text="hello").event())
                events = await _collect_all(client, timeout=5)

            types = [e.type for e in events]
            assert "audio-start" in types
            assert "audio-stop" in types
            assert "synthesize-stopped" not in types  # legacy doesn't send stopped
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_legacy_and_streaming_mixed(self):
        """Legacy and streaming requests coexist in one connection."""
        server = await start_fake_tts_server(host="127.0.0.1", port=0)
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as client:
                # Legacy first
                await client.write_event(Synthesize(text="legacy").event())
                events1 = await _collect_all(client, timeout=5)
                assert "audio-start" in [e.type for e in events1]

                # Streaming after
                await client.write_event(SynthesizeStart().event())
                await client.write_event(SynthesizeChunk(text="stream").event())
                await client.write_event(SynthesizeStop().event())
                events2 = await _collect_all(client, timeout=5)
                assert "synthesize-stopped" in [e.type for e in events2]
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_streaming_stop_without_start(self):
        """synthesize-stop without start is harmless (no crash)."""
        server = await start_fake_tts_server(host="127.0.0.1", port=0)
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as client:
                await client.write_event(SynthesizeStop().event())
                events = await _collect_all(client, timeout=3)
                # No crash, no response
                assert len(events) == 0
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_streaming_chunk_without_start(self):
        """synthesize-chunk without start is ignored (no crash)."""
        server = await start_fake_tts_server(host="127.0.0.1", port=0)
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as client:
                await client.write_event(SynthesizeChunk(text="orphan").event())
                await client.write_event(SynthesizeStop().event())
                events = await _collect_all(client, timeout=3)
                # Orphan chunk ignored, no synthesis triggered
                assert len(events) == 0
        finally:
            await server.stop()


# ── HA-style sentence splitting ────────────────────────────────────────────


class TestHAStyleStreaming:
    """Home Assistant sends sentences as separate chunks to handle VAD."""

    @pytest.mark.asyncio
    async def test_ha_sentence_splitting(self):
        """Three sentence chunks → one synthesis with accumulated text."""
        server = await start_fake_tts_server(host="127.0.0.1", port=0)
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as client:
                await client.write_event(SynthesizeStart().event())
                await client.write_event(
                    SynthesizeChunk(text="Hello.").event()
                )
                await client.write_event(
                    SynthesizeChunk(text="How can I assist?").event()
                )
                await client.write_event(SynthesizeStop().event())

                events = await _collect_all(client, timeout=5)

            types = [e.type for e in events]
            assert "audio-start" in types
            assert "audio-stop" in types
            assert "synthesize-stopped" in types
            # Progressive audio chunks
            chunks = [e for e in events if AudioChunk.is_type(e.type)]
            assert len(chunks) >= 1
            assert all(len(AudioChunk.from_event(c).audio) > 0 for c in chunks)
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_ha_empty_sentence_filtered(self):
        """Empty/whitespace chunk does not crash."""
        server = await start_fake_tts_server(host="127.0.0.1", port=0)
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as client:
                await client.write_event(SynthesizeStart().event())
                await client.write_event(SynthesizeChunk(text="  ").event())
                await client.write_event(SynthesizeChunk(text="Hi").event())
                await client.write_event(SynthesizeStop().event())

                events = await _collect_all(client, timeout=5)
            assert "audio-start" in [e.type for e in events]
        finally:
            await server.stop()


# ── Audio validation ──────────────────────────────────────────────────────


class TestStreamingAudioValidation:
    """Streaming audio meets the real backend contract."""

    @pytest.mark.asyncio
    async def test_audio_params_consistent(self):
        server = await start_fake_tts_server(host="127.0.0.1", port=0)
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as client:
                await client.write_event(SynthesizeStart().event())
                await client.write_event(SynthesizeChunk(text="test").event())
                await client.write_event(SynthesizeStop().event())
                events = await _collect_all(client, timeout=5)

            start = AudioStart.from_event(events[0])
            chunks = [AudioChunk.from_event(e) for e in events if AudioChunk.is_type(e.type)]
            assert start.rate == 22050  # fake backend default
            assert start.width == 2
            assert start.channels == 1
            for c in chunks:
                assert c.rate == start.rate
                assert c.width == start.width
                assert c.channels == start.channels
                assert len(c.audio) > 0
                assert len(c.audio) % 2 == 0  # frame aligned
        finally:
            await server.stop()

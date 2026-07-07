"""Minimal Wyoming Protocol fake TTS server for Phase 1.

This module proves the Home Assistant/Wyoming boundary using deterministic local
PCM test audio. It intentionally does not call s2.cpp, load GGUF models, or use
CUDA/GPU resources.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlparse

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import Attribution, Describe, Info, TtsProgram, TtsVoice
from wyoming.server import AsyncEventHandler, AsyncTcpServer
from wyoming.tts import Synthesize

from app.audio import PCM_CHANNELS, PCM_WIDTH_BYTES, chunk_pcm_s16le, pcm_s16le_test_tone
from app.config import Settings


@dataclass(frozen=True)
class FakeTtsConfig:
    """Settings for deterministic Phase 1 fake/test audio."""

    sample_rate: int = 22050
    duration_ms: int = 600
    chunk_ms: int = 100
    width: int = PCM_WIDTH_BYTES
    channels: int = PCM_CHANNELS

    @classmethod
    def from_settings(cls, settings: Settings) -> "FakeTtsConfig":
        """Build fake TTS config from app settings."""
        return cls(
            sample_rate=settings.fake_tts_sample_rate,
            duration_ms=settings.fake_tts_duration_ms,
            chunk_ms=settings.fake_tts_chunk_ms,
        )


def parse_tcp_uri(uri: str) -> tuple[str, int]:
    """Parse a Wyoming TCP URI into host and port."""
    parsed = urlparse(uri)
    if parsed.scheme != "tcp" or parsed.hostname is None or parsed.port is None:
        raise ValueError("expected Wyoming TCP URI like tcp://0.0.0.0:10200")
    return parsed.hostname, parsed.port


def build_info_event() -> Event:
    """Return Wyoming service metadata for Home Assistant discovery/describe."""
    attribution = Attribution(
        name="wyoming-s2cpp-tts",
        url="https://github.com/",
    )
    voice = TtsVoice(
        name="fake-test-tone",
        attribution=attribution,
        installed=True,
        description="Deterministic Phase 1 fake PCM test tone",
        version="0.1-phase1",
        languages=["en"],
    )
    program = TtsProgram(
        name="wyoming-s2cpp-tts-fake",
        attribution=attribution,
        installed=True,
        description="Phase 1 fake/test PCM Wyoming TTS service",
        version="0.1-phase1",
        voices=[voice],
        supports_synthesize_streaming=False,
    )
    return Info(tts=[program]).event()


def synthesize_fake_tts_events(
    text: str,
    config: FakeTtsConfig | None = None,
) -> list[Event]:
    """Synthesize deterministic fake PCM as Wyoming audio events."""
    fake_config = config or FakeTtsConfig()
    pcm = pcm_s16le_test_tone(
        text=text,
        duration_ms=fake_config.duration_ms,
        sample_rate=fake_config.sample_rate,
    )

    events: list[Event] = [
        AudioStart(
            rate=fake_config.sample_rate,
            width=fake_config.width,
            channels=fake_config.channels,
        ).event()
    ]

    timestamp_ms = 0
    for chunk in chunk_pcm_s16le(
        pcm,
        sample_rate=fake_config.sample_rate,
        chunk_ms=fake_config.chunk_ms,
        width=fake_config.width,
        channels=fake_config.channels,
    ):
        events.append(
            AudioChunk(
                rate=fake_config.sample_rate,
                width=fake_config.width,
                channels=fake_config.channels,
                audio=chunk,
                timestamp=timestamp_ms,
            ).event()
        )
        timestamp_ms += fake_config.chunk_ms

    events.append(AudioStop(timestamp=timestamp_ms).event())
    return events


class SingleWorkerSynthesisQueue:
    """Bounded one-worker queue gate for initial single-active-synthesis policy."""

    worker_count = 1

    def __init__(self, max_size: int) -> None:
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        self.max_size = max_size
        self._semaphore = asyncio.Semaphore(1)
        self._pending = 0

    @property
    def pending(self) -> int:
        """Return the number of accepted requests waiting/running."""
        return self._pending

    async def run(self, operation: Callable[[], Awaitable[None]]) -> None:
        """Run one synthesis operation when capacity is available."""
        if self._pending >= self.max_size:
            raise RuntimeError("fake TTS synthesis queue is full")

        self._pending += 1
        try:
            async with self._semaphore:
                await operation()
        finally:
            self._pending -= 1


class FakeTtsEventHandler(AsyncEventHandler):
    """Wyoming event handler for Describe and Synthesize requests."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        config: FakeTtsConfig,
        queue: SingleWorkerSynthesisQueue,
    ) -> None:
        super().__init__(reader, writer)
        self.config = config
        self.queue = queue

    async def handle_event(self, event: Event) -> bool:
        """Handle one Wyoming event."""
        if Describe.is_type(event.type):
            await self.write_event(build_info_event())
            return True

        if Synthesize.is_type(event.type):
            synthesize = Synthesize.from_event(event)

            async def send_audio() -> None:
                for audio_event in synthesize_fake_tts_events(
                    synthesize.text,
                    config=self.config,
                ):
                    await self.write_event(audio_event)

            await self.queue.run(send_audio)
            return True

        return True


@dataclass
class RunningFakeTtsServer:
    """Handle for a started fake Wyoming TCP server."""

    server: AsyncTcpServer
    host: str
    port: int

    async def stop(self) -> None:
        """Stop the TCP server and active handlers."""
        await self.server.stop()
        raw_server = getattr(self.server, "_server", None)
        if raw_server is not None:
            raw_server.close()
            await raw_server.wait_closed()


async def start_fake_tts_server(
    host: str = "0.0.0.0",
    port: int = 10200,
    config: FakeTtsConfig | None = None,
    max_queue_size: int = 3,
) -> RunningFakeTtsServer:
    """Start the Phase 1 fake Wyoming TTS server without blocking."""
    fake_config = config or FakeTtsConfig()
    queue = SingleWorkerSynthesisQueue(max_size=max_queue_size)
    server = AsyncTcpServer(host, port)

    def handler_factory(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        return FakeTtsEventHandler(reader, writer, fake_config, queue)

    await server.start(handler_factory)
    raw_server = getattr(server, "_server", None)
    bound_port = port
    if raw_server is not None and raw_server.sockets:
        bound_port = int(raw_server.sockets[0].getsockname()[1])

    return RunningFakeTtsServer(server=server, host=host, port=bound_port)


def describe_planned_server() -> str:
    """Return a human-readable description of the planned Wyoming endpoint."""
    return "Phase 1 fake Wyoming TTS server on tcp://0.0.0.0:10200"


def run_server(settings: Settings | None = None) -> None:
    """Run the Phase 1 fake Wyoming TCP server until interrupted."""
    active_settings = settings or Settings()
    host, port = parse_tcp_uri(active_settings.wyoming_uri)
    config = FakeTtsConfig.from_settings(active_settings)

    async def runner() -> None:
        server = await start_fake_tts_server(
            host=host,
            port=port,
            config=config,
            max_queue_size=active_settings.max_queue_size,
        )
        print(f"Fake Wyoming TTS server listening on tcp://{host}:{server.port}")
        try:
            await asyncio.Event().wait()
        finally:
            await server.stop()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        print("Fake Wyoming TTS server stopped")

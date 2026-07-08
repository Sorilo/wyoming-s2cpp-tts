"""Minimal Wyoming Protocol TTS server for fake and opt-in s2.cpp backends.

The default backend proves the Home Assistant/Wyoming boundary using
deterministic local PCM test audio. The opt-in Phase 2.5 backend calls an
already-running s2.cpp HTTP server and converts one buffered PCM response into
Wyoming audio events. This module does not build s2.cpp, load GGUF models, or
use CUDA/GPU resources directly.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol
from urllib.parse import urlparse

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import Attribution, Describe, Info, TtsProgram, TtsVoice
from wyoming.server import AsyncEventHandler, AsyncTcpServer
from wyoming.tts import (
    Synthesize,
    SynthesizeStart,
    SynthesizeChunk,
    SynthesizeStop,
    SynthesizeStopped,
)

from app.audio import (
    PCM_CHANNELS,
    PCM_WIDTH_BYTES,
    StreamingPCMRechunker,
    chunk_pcm_s16le,
    parse_declared_pcm_s16le_format,
    pcm_s16le_test_tone,
    validate_declared_pcm_s16le,
)
from app.config import Settings
import sys

from app.metrics import MetricsCollector
from app.s2_client import S2Client, S2ClientError, S2GenerateRequest


class S2GenerateClient(Protocol):
    """Small protocol for a sync s2.cpp client used by the Wyoming adapter."""

    def generate_multipart(self, request: S2GenerateRequest):
        """Generate one buffered audio response via multipart/form-data."""


S2ClientFactory = Callable[[Settings], S2GenerateClient]


def _pcm_to_audio_events(
    pcm: bytes,
    config: "FakeTtsConfig",
    metrics: MetricsCollector | None = None,
) -> list[Event]:
    """Convert buffered raw PCM s16le audio into Wyoming audio events."""
    events: list[Event] = [
        AudioStart(
            rate=config.sample_rate,
            width=config.width,
            channels=config.channels,
        ).event()
    ]

    emitted_any = False
    timestamp_ms = 0
    for chunk in chunk_pcm_s16le(
        pcm,
        sample_rate=config.sample_rate,
        chunk_ms=config.chunk_ms,
        width=config.width,
        channels=config.channels,
    ):
        if not emitted_any and metrics is not None:
            metrics.record_first_audio_chunk()
            emitted_any = True

        events.append(
            AudioChunk(
                rate=config.sample_rate,
                width=config.width,
                channels=config.channels,
                audio=chunk,
                timestamp=timestamp_ms,
            ).event()
        )
        if metrics is not None:
            metrics.record_emitted_chunk(len(chunk))
        timestamp_ms += config.chunk_ms

    events.append(AudioStop(timestamp=timestamp_ms).event())
    return events


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


def build_info_event(settings: Settings | None = None) -> Event:
    """Return Wyoming service metadata for Home Assistant discovery/describe.

    When TTS_BACKEND=s2cpp the Describe response reflects the real s2.cpp
    backend metadata (44100 Hz, mono, s16le).  When TTS_BACKEND=fake or
    settings is None the original fake/test metadata is returned.
    """
    attribution = Attribution(
        name="wyoming-s2cpp-tts",
        url="https://github.com/sorilo/wyoming-s2cpp-tts",
    )

    active_settings = settings or Settings()
    if active_settings.tts_backend == "s2cpp":
        voice = TtsVoice(
            name="s2-pro",
            attribution=attribution,
            installed=True,
            description="Fish Speech S2 Pro via s2.cpp — 44100 Hz mono s16le",
            version="0.1",
            languages=["en", "zh"],
        )
        program = TtsProgram(
            name="wyoming-s2cpp-tts",
            attribution=attribution,
            installed=True,
            description="Wyoming TTS service backed by s2.cpp / Fish Speech S2 Pro",
            version="0.1",
            voices=[voice],
            supports_synthesize_streaming=True,
        )
    else:
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
    metrics: MetricsCollector | None = None,
) -> list[Event]:
    """Synthesize deterministic fake PCM as Wyoming audio events."""
    if metrics is None:
        metrics = MetricsCollector(backend_type="fake", synthesis_mode="fake")

    fake_config = config or FakeTtsConfig()

    try:
        pcm = pcm_s16le_test_tone(
            text=text,
            duration_ms=fake_config.duration_ms,
            sample_rate=fake_config.sample_rate,
        )

        if pcm:
            # Synthetic test tone is the "backend data" for the fake path.
            metrics.record_first_backend_data()

        events = _pcm_to_audio_events(pcm, fake_config, metrics=metrics)
        metrics.finalize("success")
        return events
    except Exception:
        metrics.finalize("error", type(sys.exc_info()[1]).__name__)
        raise


def synthesize_s2cpp_tts_events(
    text: str,
    client: S2GenerateClient,
    settings: Settings,
    config: FakeTtsConfig | None = None,
    metrics: MetricsCollector | None = None,
) -> list[Event]:
    """Synthesize via an already-running s2.cpp backend and emit Wyoming events.

    This Phase 2.5 bridge intentionally buffers the backend response before
    converting it to Wyoming audio. Progressive streaming is reserved for a
    later phase.

    .. important::

        ``first_backend_data_ns`` records the moment the *completed*
        non-empty buffered response becomes available to the synthesis
        layer — it is **not** the literal first network byte arriving at
        the host.  The buffered API cannot observe the first network byte.
    """
    if metrics is None:
        metrics = MetricsCollector(backend_type="s2cpp", synthesis_mode="buffered")

    audio_config = config or FakeTtsConfig.from_settings(settings)
    request = S2GenerateRequest.from_settings(text=text, settings=settings)

    try:
        result = client.generate_multipart(request)
        pcm_format = validate_declared_pcm_s16le(
            result.audio,
            content_type=result.content_type,
            headers=result.response_headers,
        )
        audio_config = FakeTtsConfig(
            sample_rate=pcm_format.sample_rate,
            duration_ms=audio_config.duration_ms,
            chunk_ms=audio_config.chunk_ms,
            width=pcm_format.width,
            channels=pcm_format.channels,
        )

        # Buffered path: record when completed buffered response is available.
        # This is NOT the literal first network byte — the multipart response
        # was fully buffered before this point.
        if result.audio:
            metrics.record_first_backend_data()

        events = _pcm_to_audio_events(result.audio, audio_config, metrics=metrics)
        metrics.finalize("success")
        return events
    except S2ClientError:
        metrics.finalize("error", "S2ClientError")
        raise
    except Exception:
        metrics.finalize("error", type(sys.exc_info()[1]).__name__)
        raise



_STREAM_EOF = object()


def _read_stream_chunk(stream):
    """Read one chunk from a synchronous stream iterator.

    Returns ``_STREAM_EOF`` sentinel on ``StopIteration`` so the result can
    safely be transported through ``run_in_executor`` / ``asyncio.to_thread``
    (Python 3.13 raises ``RuntimeError`` when ``StopIteration`` is raised
    across a ``Future`` boundary).
    """
    try:
        return next(stream)
    except StopIteration:
        return _STREAM_EOF



async def synthesize_s2cpp_streaming_tts_events(
    client: S2Client,
    request: S2GenerateRequest,
    config: FakeTtsConfig,
    metrics: MetricsCollector | None = None,
):
    """Yield Wyoming audio events progressively from a streaming s2.cpp backend.

    Phase 5C: Consumes ``S2StreamResult`` one transport chunk at a time via
    ``asyncio.to_thread`` (so blocking ``response.read()`` calls never block
    the event loop).  A ``StreamingPCMRechunker`` handles PCM frame alignment
    across arbitrary HTTP chunk boundaries and produces frame-aligned
    ``AudioChunk`` payloads with frame-derived timestamps.

    Yields:
        ``AudioStart`` → one or more ``AudioChunk`` → ``AudioStop`` on success.

    On any error (backend failure, PCM validation, early consumer exit) the
    stream is cleaned up and the exception propagates — no successful
    ``AudioStop`` is emitted.

    Metrics are finalized on all paths (success, error, early close,
    cancellation).
    """
    if metrics is None:
        metrics = MetricsCollector(backend_type="s2cpp", synthesis_mode="streaming")

    audio_config = config
    rechunker: StreamingPCMRechunker | None = None
    backend_data_observed = False

    try:
        with client.generate_stream(request) as stream:
            stream_content_type = getattr(stream, "content_type", None)
            stream_headers = getattr(stream, "response_headers", None)
            if stream_content_type is not None or stream_headers is not None:
                pcm_format = parse_declared_pcm_s16le_format(
                    content_type=stream_content_type or "",
                    headers=stream_headers or {},
                )
                audio_config = FakeTtsConfig(
                    sample_rate=pcm_format.sample_rate,
                    duration_ms=config.duration_ms,
                    chunk_ms=config.chunk_ms,
                    width=pcm_format.width,
                    channels=pcm_format.channels,
                )

            rechunker = StreamingPCMRechunker(
                sample_rate=audio_config.sample_rate,
                chunk_ms=audio_config.chunk_ms,
                width=audio_config.width,
                channels=audio_config.channels,
            )

            yield AudioStart(
                rate=audio_config.sample_rate,
                width=audio_config.width,
                channels=audio_config.channels,
            ).event()

            while True:
                try:
                    chunk = await asyncio.to_thread(_read_stream_chunk, stream)
                except S2ClientError:
                    raise
                if chunk is _STREAM_EOF:
                    break

                # First non-empty backend chunk observed by this process.
                if not backend_data_observed and chunk:
                    metrics.record_first_backend_data()
                    backend_data_observed = True

                for audio_bytes, timestamp_ms in rechunker.feed(chunk):
                    metrics.record_first_audio_chunk()
                    yield AudioChunk(
                        rate=audio_config.sample_rate,
                        width=audio_config.width,
                        channels=audio_config.channels,
                        audio=audio_bytes,
                        timestamp=timestamp_ms,
                    ).event()
                    metrics.record_emitted_chunk(len(audio_bytes))

        # Stream exhausted normally — flush any remaining complete frames.
        for audio_bytes, timestamp_ms in rechunker.flush():
            metrics.record_first_audio_chunk()
            yield AudioChunk(
                rate=audio_config.sample_rate,
                width=audio_config.width,
                channels=audio_config.channels,
                audio=audio_bytes,
                timestamp=timestamp_ms,
            ).event()
            metrics.record_emitted_chunk(len(audio_bytes))

        yield AudioStop(
            timestamp=int(
                rechunker.cumulative_frames * 1000 / audio_config.sample_rate
            )
        ).event()

        metrics.finalize("success")

    except (GeneratorExit, asyncio.CancelledError) as exc:
        # Consumer closed the async generator early or the task was
        # cancelled.  The ``with`` block's ``__exit__`` already cleaned
        # up the stream; finalize then re-raise.
        metrics.finalize("cancelled")
        raise
    except S2ClientError:
        metrics.finalize("error", "S2ClientError")
        raise
    except Exception:
        metrics.finalize("error", type(sys.exc_info()[1]).__name__)
        raise

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
    """Wyoming event handler for Describe, Synthesize, and streaming TTS.

    Supports:
      - Legacy ``synthesize`` (single request)
      - Streaming ``synthesize-start`` / ``synthesize-chunk`` /
        ``synthesize-stop`` (HA multi-sentence preview)
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        config: FakeTtsConfig,
        queue: SingleWorkerSynthesisQueue,
        settings: Settings,
        s2_client_factory: S2ClientFactory,
    ) -> None:
        super().__init__(reader, writer)
        self.config = config
        self.queue = queue
        self.settings = settings
        self.s2_client_factory = s2_client_factory
        # Streaming state
        self._streaming_text_parts: list[str] = []
        self._in_streaming_session: bool = False

    async def handle_event(self, event: Event) -> bool:
        """Handle one Wyoming event."""
        if Describe.is_type(event.type):
            await self.write_event(build_info_event(self.settings))
            return True

        # ── Legacy (non-streaming) Synthesize ────────────────────────
        if Synthesize.is_type(event.type):
            synthesize = Synthesize.from_event(event)

            async def send_audio() -> None:
                audio_events = await self._synthesize_text(synthesize.text)
                for audio_event in audio_events:
                    await self.write_event(audio_event)

            await self.queue.run(send_audio)
            return True

        # ── Streaming TTS: synthesize-start ──────────────────────────
        if SynthesizeStart.is_type(event.type):
            self._streaming_text_parts = []
            self._in_streaming_session = True
            return True

        # ── Streaming TTS: synthesize-chunk ──────────────────────────
        if SynthesizeChunk.is_type(event.type):
            chunk = SynthesizeChunk.from_event(event)
            if self._in_streaming_session:
                self._streaming_text_parts.append(chunk.text)
            return True

        # ── Streaming TTS: synthesize-stop ───────────────────────────
        if SynthesizeStop.is_type(event.type):
            if not self._in_streaming_session:
                return True

            accumulated = " ".join(self._streaming_text_parts).strip()
            self._streaming_text_parts = []
            self._in_streaming_session = False

            if accumulated:
                async def send_streaming_audio() -> None:
                    audio_events = await self._synthesize_text(accumulated)
                    for audio_event in audio_events:
                        await self.write_event(audio_event)
                    # Signal end of streaming response
                    await self.write_event(SynthesizeStopped().event())

                await self.queue.run(send_streaming_audio)
            else:
                # No text accumulated — still signal end of streaming response
                await self.write_event(SynthesizeStopped().event())

            return True

        return True

    async def _synthesize_text(self, text: str) -> list[Event]:
        """Run synthesis for given text and return Wyoming audio events."""
        if self.settings.tts_backend == "s2cpp":
            s2_client = self.s2_client_factory(self.settings)
            return await asyncio.to_thread(
                synthesize_s2cpp_tts_events,
                text,
                s2_client,
                self.settings,
                self.config,
            )
        else:
            return synthesize_fake_tts_events(
                text,
                config=self.config,
            )


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
    settings: Settings | None = None,
    s2_client_factory: S2ClientFactory = S2Client.from_settings,
) -> RunningFakeTtsServer:
    """Start the Wyoming TTS server without blocking.

    Despite the historical function name, this can run either the default fake
    backend or the opt-in buffered s2.cpp backend based on `settings.tts_backend`.
    """
    active_settings = settings or Settings()
    fake_config = config or FakeTtsConfig.from_settings(active_settings)
    queue = SingleWorkerSynthesisQueue(max_size=max_queue_size)
    server = AsyncTcpServer(host, port)

    def handler_factory(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        return FakeTtsEventHandler(
            reader,
            writer,
            fake_config,
            queue,
            active_settings,
            s2_client_factory,
        )

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
            settings=active_settings,
        )
        print(
            f"Wyoming TTS server listening on tcp://{host}:{server.port} "
            f"with backend={active_settings.tts_backend}"
        )
        try:
            await asyncio.Event().wait()
        finally:
            await server.stop()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        print("Fake Wyoming TTS server stopped")

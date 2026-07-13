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
from wyoming.error import Error
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
from app.lifecycle import LifecycleState
from app.observability import (
    LogContext,
    new_connection_id,
    new_synthesis_id,
    obs_log,
    text_fingerprint,
)
from app.speech import SpeechScheduler, SpeechRequest, SpeechMetadata, QueueFullError, QueueTimeoutError, StreamingCoordinator
from app.speech.session import SynthesisSession

from app.version import __version__
from app.voice_discovery import discover_voices
import signal
import sys
import time

from app.metrics import MetricsCollector
from app.s2_client import S2BackendBusyError, S2Client, S2ClientError, S2GenerateRequest


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
    backend metadata (44100 Hz, mono, s16le) and includes any discovered
    .s2voice profiles as selectable voices.  When TTS_BACKEND=fake or
    settings is None the original fake/test metadata is returned.
    """
    attribution = Attribution(
        name="wyoming-s2cpp-tts",
        url="https://github.com/sorilo/wyoming-s2cpp-tts",
    )

    active_settings = settings or Settings()
    if active_settings.tts_backend == "s2cpp":
        # Generic default voice (always present for compatibility).
        voices = [
            TtsVoice(
                name="s2-pro",
                attribution=attribution,
                installed=True,
                description="Fish Speech S2 Pro via s2.cpp — 44100 Hz mono s16le",
                version=__version__,
                languages=["en", "zh"],
            )
        ]

        # Append discovered .s2voice profiles.
        for profile_id in discover_voices(active_settings.s2_voice_dir):
            voices.append(
                TtsVoice(
                    name=profile_id,
                    attribution=attribution,
                    installed=True,
                    description=f"Custom s2 voice profile: {profile_id}",
                    version=__version__,
                    languages=["en"],
                )
            )

        program = TtsProgram(
            name="wyoming-s2cpp-tts",
            attribution=attribution,
            installed=True,
            description="Wyoming TTS service backed by s2.cpp / Fish Speech S2 Pro",
            version=__version__,
            voices=voices,
            supports_synthesize_streaming=True,
        )
    else:
        voice = TtsVoice(
            name="fake-test-tone",
            attribution=attribution,
            installed=True,
            description="Deterministic Phase 1 fake PCM test tone",
            version=__version__,
            languages=["en"],
        )
        program = TtsProgram(
            name="wyoming-s2cpp-tts-fake",
            attribution=attribution,
            installed=True,
            description="Phase 1 fake/test PCM Wyoming TTS service",
            version=__version__,
            voices=[voice],
            supports_synthesize_streaming=False,
        )
    return Info(tts=[program]).event()


def synthesize_fake_tts_events(
    text: str,
    config: FakeTtsConfig | None = None,
    metrics: MetricsCollector | None = None,
    ctx: LogContext | None = None,
) -> list[Event]:
    """Synthesize deterministic fake PCM as Wyoming audio events."""
    _ctx = ctx or LogContext()
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
            metrics.record_first_backend_data()

        events = _pcm_to_audio_events(pcm, fake_config, metrics=metrics)

        chunk_count = sum(1 for e in events if AudioChunk.is_type(e.type))
        total_pcm = sum(
            len(AudioChunk.from_event(e).audio)
            for e in events if AudioChunk.is_type(e.type)
        )
        obs_log("audio_out",
                connection_id=_ctx.connection_id,
                synthesis_id=_ctx.synthesis_id,
                text_fp=text_fingerprint(text),
                audio_start=True,
                chunk_count=chunk_count,
                pcm_bytes=total_pcm,
                audio_stop=True,
                status="ok")

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
    voice: str | None = None,
    ctx: LogContext | None = None,
) -> list[Event]:
    """Synthesize via an already-running s2.cpp backend and emit Wyoming events.

    This Phase 2.5 bridge intentionally buffers the backend response before
    converting it to Wyoming audio. Progressive streaming is reserved for a
    later phase.

    Args:
        text: The text to synthesize.
        client: An s2.cpp client that supports ``generate_multipart``.
        settings: Runtime settings (backend, model, voice config).
        config: Audio configuration for Wyoming event construction.
        metrics: Optional metrics collector.
        voice: Explicit voice profile ID to use.  When set, the
            ``S2GenerateRequest`` is constructed with this voice and
            the configured ``voice_dir``.  When *None*, the configured
            ``S2_DEFAULT_VOICE`` (if valid) or a generic fallback
            (omitting custom voice fields) is used.
        ctx: Optional log context for correlation (connection_id, synthesis_id).

    .. important::

        ``first_backend_data_ns`` records the moment the *completed*
        non-empty buffered response becomes available to the synthesis
        layer — it is **not** the literal first network byte arriving at
        the host.  The buffered API cannot observe the first network byte.
    """
    _ctx = ctx or LogContext()
    if metrics is None:
        metrics = MetricsCollector(backend_type="s2cpp", synthesis_mode="buffered")

    audio_config = config or FakeTtsConfig.from_settings(settings)
    request = S2GenerateRequest.from_settings(
        text=text, settings=settings, voice=voice,
    )

    # ── Backend request lifecycle ─────────────────────────────────
    fp = text_fingerprint(text)
    backend_start = time.monotonic()
    obs_log("backend_start",
            connection_id=_ctx.connection_id,
            synthesis_id=_ctx.synthesis_id,
            text_fp=fp,
            text_len=len(text),
            voice=voice or "generic")

    try:
        result = client.generate_multipart(request)

        backend_elapsed_ms = int((time.monotonic() - backend_start) * 1000)
        obs_log("backend_done",
                connection_id=_ctx.connection_id,
                synthesis_id=_ctx.synthesis_id,
                text_fp=fp,
                elapsed_ms=backend_elapsed_ms,
                content_type=result.content_type,
                audio_bytes=len(result.audio),
                status="ok")
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
        if result.audio:
            metrics.record_first_backend_data()

        events = _pcm_to_audio_events(result.audio, audio_config, metrics=metrics)

        # ── Outgoing audio lifecycle summary ─────────────────────────
        chunk_count = sum(1 for e in events if AudioChunk.is_type(e.type))
        total_pcm = sum(
            len(AudioChunk.from_event(e).audio)
            for e in events if AudioChunk.is_type(e.type)
        )
        obs_log("audio_out",
                connection_id=_ctx.connection_id,
                synthesis_id=_ctx.synthesis_id,
                text_fp=fp,
                audio_start=True,
                chunk_count=chunk_count,
                pcm_bytes=total_pcm,
                audio_stop=True,
                status="ok")

        metrics.finalize("success")
        return events
    except S2BackendBusyError:
        backend_elapsed_ms = int((time.monotonic() - backend_start) * 1000)
        obs_log("backend_done",
                connection_id=_ctx.connection_id,
                synthesis_id=_ctx.synthesis_id,
                text_fp=fp,
                elapsed_ms=backend_elapsed_ms,
                status="error",
                error="backend_busy")
        metrics.finalize("error", "backend_busy")
        raise
    except S2ClientError:
        backend_elapsed_ms = int((time.monotonic() - backend_start) * 1000)
        obs_log("backend_done",
                connection_id=_ctx.connection_id,
                synthesis_id=_ctx.synthesis_id,
                text_fp=fp,
                elapsed_ms=backend_elapsed_ms,
                status="error",
                error="S2ClientError")
        metrics.finalize("error", "S2ClientError")
        raise
    except Exception:
        backend_elapsed_ms = int((time.monotonic() - backend_start) * 1000)
        exc_name = type(sys.exc_info()[1]).__name__
        obs_log("backend_done",
                connection_id=_ctx.connection_id,
                synthesis_id=_ctx.synthesis_id,
                text_fp=fp,
                elapsed_ms=backend_elapsed_ms,
                status="error",
                error=exc_name)
        metrics.finalize("error", exc_name)
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


async def _read_stream_with_deadline(stream, deadline: float):
    """Read one chunk from *stream* enforcing *deadline*.

    Starts :func:`asyncio.to_thread` to read, awaits with
    ``asyncio.wait_for`` using the remaining time.  On timeout,
    cancels the stream and reaps the read task to prevent orphan
    threads.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise asyncio.TimeoutError("Synthesis deadline passed")
    read_task = asyncio.create_task(
        asyncio.to_thread(_read_stream_chunk, stream))
    try:
        return await asyncio.wait_for(read_task, timeout=remaining)
    except asyncio.TimeoutError:
        try:
            stream.cancel()
        except Exception:
            pass
        # Reap the read task
        if not read_task.done():
            try:
                await asyncio.wait_for(read_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                read_task.cancel()
        raise asyncio.TimeoutError("Synthesis timeout during read")



async def synthesize_s2cpp_streaming_tts_events(
    client: S2Client,
    request: S2GenerateRequest,
    config: FakeTtsConfig,
    settings: "Settings",
    metrics: MetricsCollector | None = None,
    ctx: LogContext | None = None,
):
    """Yield Wyoming audio events progressively from a streaming s2.cpp backend.

    Phase 5C / 7.5A: Consumes ``S2StreamResult`` one transport chunk at a time
    via ``asyncio.to_thread`` (so blocking ``response.read()`` calls never block
    the event loop).  A ``StreamingPCMRechunker`` handles PCM frame alignment
    across arbitrary HTTP chunk boundaries and produces frame-aligned
    ``AudioChunk`` payloads with frame-derived timestamps.

    Yields:
        ``AudioStart`` → one or more ``AudioChunk`` → ``AudioStop`` on success.

    On any error (backend failure, PCM validation, early consumer exit) the
    stream is cleaned up and the exception propagates — no successful
    ``AudioStop`` is emitted.

    Metrics are finalized on all paths (success, error, early close,
    cancellation).  Structured observability logs are emitted when *ctx*
    is provided.
    """
    if metrics is None:
        metrics = MetricsCollector(backend_type="s2cpp", synthesis_mode="streaming")

    _ctx = ctx or LogContext()
    audio_config = config
    rechunker: StreamingPCMRechunker | None = None
    backend_data_observed = False
    first_audio_emitted = False
    stream_start = time.monotonic()
    synthesis_deadline = time.monotonic() + settings.s2_synthesis_timeout_sec

    fp = text_fingerprint(request.text)
    obs_log("backend_start",
            connection_id=_ctx.connection_id,
            synthesis_id=_ctx.synthesis_id,
            text_fp=fp,
            text_len=len(request.text),
            voice=request.voice or "generic",
            mode="streaming",
            low_latency=request.low_latency,
            stream_decode_stride_frames=request.stream_decode_stride_frames,
            stream_holdback_frames=request.stream_holdback_frames,
            stream_start_buffer_ms=request.stream_start_buffer_ms,
            codec_decode_context_frames=request.codec_decode_context_frames,
            segment_sentences=request.segment_sentences,
            model=settings.s2_model)

    attempt = 1
    max_total_attempts = settings.s2_backend_busy_max_retries + 1
    audio_start_emitted = False
    synthesis_deadline = time.monotonic() + settings.s2_synthesis_timeout_sec

    while True:
        remaining = synthesis_deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError("Synthesis deadline passed before connection")

        try:
                with client.generate_stream(request, synthesis_id=_ctx.synthesis_id) as stream:
                    stream_content_type = getattr(stream, "content_type", None)
                    stream_headers = getattr(stream, "response_headers", None)

                    headers_elapsed_ms = int((time.monotonic() - stream_start) * 1000)

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

                        obs_log("backend_stream_headers",
                                connection_id=_ctx.connection_id,
                                synthesis_id=_ctx.synthesis_id,
                                text_fp=fp,
                                content_type=stream_content_type,
                                sample_rate=pcm_format.sample_rate,
                                channels=pcm_format.channels,
                                elapsed_ms=headers_elapsed_ms)

                    rechunker = StreamingPCMRechunker(
                        sample_rate=audio_config.sample_rate,
                        chunk_ms=audio_config.chunk_ms,
                        width=audio_config.width,
                        channels=audio_config.channels,
                    )

                    # ── Compute initial buffer target ───────────────────────────
                    frame_size = audio_config.width * audio_config.channels
                    bytes_per_ms = audio_config.sample_rate * frame_size / 1000.0

                    estimated_long_form = False
                    buffer_policy = "zero"

                    if settings.s2_long_form_threshold_chars > 0 and len(request.text) >= settings.s2_long_form_threshold_chars:
                        estimated_long_form = True
                        buffer_target_ms = settings.s2_long_form_buffer_ms
                        buffer_policy = "long_form"
                    else:
                        buffer_target_ms = settings.s2_initial_buffer_ms

                    max_buffer_ms = settings.s2_max_initial_buffer_ms
                    if max_buffer_ms <= 0:
                        # Zero/negative max: buffering disabled (safe default)
                        buffer_target_ms = 0
                        buffer_target_bytes = 0
                    elif buffer_target_ms > max_buffer_ms:
                        buffer_target_ms = max_buffer_ms

                    if buffer_target_ms > 0:
                        buffer_target_bytes = int(buffer_target_ms * bytes_per_ms)
                        if buffer_target_bytes % frame_size != 0:
                            buffer_target_bytes -= buffer_target_bytes % frame_size
                    else:
                        buffer_target_bytes = 0

                    max_buffer_bytes = int(max_buffer_ms * bytes_per_ms) if max_buffer_ms > 0 else 0
                    if max_buffer_bytes > 0:
                        if max_buffer_bytes % frame_size != 0:
                            max_buffer_bytes -= max_buffer_bytes % frame_size

                    obs_log("buffer_policy",
                            connection_id=_ctx.connection_id,
                            synthesis_id=_ctx.synthesis_id,
                            text_fp=fp,
                            buffer_policy=buffer_policy,
                            initial_buffer_target_ms=buffer_target_ms,
                            initial_buffer_target_bytes=buffer_target_bytes,
                            estimated_long_form=estimated_long_form,
                            text_len=len(request.text))

                    # ── Buffering phase ──────────────────────────────────────────
                    buffered_pcm = bytearray()
                    backend_completed_before_buffer_target = False
                    audio_start_emitted = False

                    buffering_elapsed_ms = 0  # set when AudioStart is emitted
                    carryover = b""          # excess bytes from oversized chunk (Fix 4)

                    if buffer_target_bytes <= 0:
                        # Zero-buffer: emit AudioStart immediately (preserves current behavior)
                        buffering_elapsed_ms = int((time.monotonic() - stream_start) * 1000)
                        obs_log("playback_emission_started",
                                connection_id=_ctx.connection_id,
                                synthesis_id=_ctx.synthesis_id,
                                text_fp=fp,
                                buffer_policy=buffer_policy,
                                buffering_elapsed_ms=buffering_elapsed_ms,
                                initial_buffer_target_ms=buffer_target_ms,
                                initial_buffer_target_bytes=buffer_target_bytes,
                                initial_buffered_audio_ms=0,
                                backend_completed_before_buffer_target=False)

                        yield AudioStart(
                            rate=audio_config.sample_rate,
                            width=audio_config.width,
                            channels=audio_config.channels,
                        ).event()
                        audio_start_emitted = True
                    else:
                        # Accumulate PCM until target reached, stream ends, or cap hit
                        while True:
                            try:
                                chunk = await _read_stream_with_deadline(stream, synthesis_deadline)
                            except S2ClientError:
                                raise
                            if chunk is _STREAM_EOF:
                                backend_completed_before_buffer_target = True
                                break

                            if not backend_data_observed and chunk:
                                metrics.record_first_backend_data()
                                backend_data_observed = True
                                first_audio_elapsed_ms = int((time.monotonic() - stream_start) * 1000)
                                obs_log("backend_stream_first_audio",
                                        connection_id=_ctx.connection_id,
                                        synthesis_id=_ctx.synthesis_id,
                                        text_fp=fp,
                                        elapsed_ms=first_audio_elapsed_ms)

                            # ── Fix 4: bound oversized chunk at frame-aligned cap ──
                            space_left = max_buffer_bytes - len(buffered_pcm)
                            if len(chunk) > space_left:
                                # Chunk would exceed cap — take only what fits (frame-aligned)
                                take = space_left - (space_left % frame_size) if space_left % frame_size != 0 else space_left
                                if take > 0:
                                    buffered_pcm.extend(chunk[:take])
                                carryover = chunk[take:]
                                break
                            else:
                                buffered_pcm.extend(chunk)

                            if len(buffered_pcm) >= max_buffer_bytes:
                                break
                            if len(buffered_pcm) >= buffer_target_bytes:
                                break

                        if len(buffered_pcm) > 0:
                            buffering_elapsed_ms = int((time.monotonic() - stream_start) * 1000)
                            buffered_audio_ms = int(len(buffered_pcm) * 1000 / (audio_config.sample_rate * frame_size))
                            obs_log("initial_buffer_ready",
                                    connection_id=_ctx.connection_id,
                                    synthesis_id=_ctx.synthesis_id,
                                    text_fp=fp,
                                    initial_buffered_pcm_bytes=len(buffered_pcm),
                                    initial_buffered_audio_ms=buffered_audio_ms,
                                    initial_buffer_target_ms=buffer_target_ms,
                                    initial_buffer_target_bytes=buffer_target_bytes,
                                    max_buffer_reached=(len(buffered_pcm) >= max_buffer_bytes),
                                    backend_completed_before_buffer_target=backend_completed_before_buffer_target)

                            obs_log("playback_emission_started",
                                    connection_id=_ctx.connection_id,
                                    synthesis_id=_ctx.synthesis_id,
                                    text_fp=fp,
                                    buffer_policy=buffer_policy,
                                    buffering_elapsed_ms=buffering_elapsed_ms,
                                    initial_buffer_target_ms=buffer_target_ms,
                                    initial_buffer_target_bytes=buffer_target_bytes,
                                    initial_buffered_audio_ms=buffered_audio_ms,
                                    backend_completed_before_buffer_target=backend_completed_before_buffer_target)

                            yield AudioStart(
                                rate=audio_config.sample_rate,
                                width=audio_config.width,
                                channels=audio_config.channels,
                            ).event()
                            audio_start_emitted = True

                            for audio_bytes, timestamp_ms in rechunker.feed(bytes(buffered_pcm)):
                                metrics.record_first_audio_chunk()
                                if not first_audio_emitted:
                                    first_audio_emitted = True
                                    wyoming_elapsed_ms = int((time.monotonic() - stream_start) * 1000)
                                    obs_log("first_wyoming_audio",
                                            connection_id=_ctx.connection_id,
                                            synthesis_id=_ctx.synthesis_id,
                                            text_fp=fp,
                                            elapsed_ms=wyoming_elapsed_ms,
                                            time_to_first_backend_audio_ms=first_audio_elapsed_ms if backend_data_observed else 0,
                                            wrapper_first_audio_forwarding_overhead_ms=wyoming_elapsed_ms - first_audio_elapsed_ms if backend_data_observed else 0)
                                yield AudioChunk(
                                    rate=audio_config.sample_rate,
                                    width=audio_config.width,
                                    channels=audio_config.channels,
                                    audio=audio_bytes,
                                    timestamp=timestamp_ms,
                                ).event()
                                metrics.record_emitted_chunk(len(audio_bytes))

                            buffered_pcm = bytearray()

                    # ── Progressive streaming phase ──────────────────────────────
                    if carryover and not backend_completed_before_buffer_target:
                        # Process excess from oversized buffered chunk
                        for audio_bytes, timestamp_ms in rechunker.feed(carryover):
                            metrics.record_first_audio_chunk()
                            if not first_audio_emitted:
                                first_audio_emitted = True
                                wyoming_elapsed_ms = int((time.monotonic() - stream_start) * 1000)
                                obs_log("first_wyoming_audio",
                                        connection_id=_ctx.connection_id,
                                        synthesis_id=_ctx.synthesis_id,
                                        text_fp=fp,
                                        elapsed_ms=wyoming_elapsed_ms,
                                        time_to_first_backend_audio_ms=first_audio_elapsed_ms if backend_data_observed else 0,
                                        wrapper_first_audio_forwarding_overhead_ms=wyoming_elapsed_ms - first_audio_elapsed_ms if backend_data_observed else 0)
                            yield AudioChunk(
                                rate=audio_config.sample_rate,
                                width=audio_config.width,
                                channels=audio_config.channels,
                                audio=audio_bytes,
                                timestamp=timestamp_ms,
                            ).event()
                            metrics.record_emitted_chunk(len(audio_bytes))
                        carryover = b""

                    if not backend_completed_before_buffer_target:
                        while True:
                            if time.monotonic() >= synthesis_deadline:
                                raise asyncio.TimeoutError("Synthesis timeout exceeded")
                            try:
                                chunk = await _read_stream_with_deadline(stream, synthesis_deadline)
                            except S2ClientError:
                                raise
                            if chunk is _STREAM_EOF:
                                break

                            if not backend_data_observed and chunk:
                                metrics.record_first_backend_data()
                                backend_data_observed = True
                                first_audio_elapsed_ms = int((time.monotonic() - stream_start) * 1000)
                                obs_log("backend_stream_first_audio",
                                        connection_id=_ctx.connection_id,
                                        synthesis_id=_ctx.synthesis_id,
                                        text_fp=fp,
                                        elapsed_ms=first_audio_elapsed_ms)

                            for audio_bytes, timestamp_ms in rechunker.feed(chunk):
                                metrics.record_first_audio_chunk()
                                if not first_audio_emitted:
                                    first_audio_emitted = True
                                    wyoming_elapsed_ms = int((time.monotonic() - stream_start) * 1000)
                                    obs_log("first_wyoming_audio",
                                            connection_id=_ctx.connection_id,
                                            synthesis_id=_ctx.synthesis_id,
                                            text_fp=fp,
                                            elapsed_ms=wyoming_elapsed_ms,
                                            time_to_first_backend_audio_ms=first_audio_elapsed_ms,
                                            wrapper_first_audio_forwarding_overhead_ms=wyoming_elapsed_ms - first_audio_elapsed_ms)
                                yield AudioChunk(
                                    rate=audio_config.sample_rate,
                                    width=audio_config.width,
                                    channels=audio_config.channels,
                                    audio=audio_bytes,
                                    timestamp=timestamp_ms,
                                ).event()
                                metrics.record_emitted_chunk(len(audio_bytes))

                    # ── Flush remaining frames and emit AudioStop ────────────────
                    flush_chunks = []
                    for audio_bytes, timestamp_ms in rechunker.flush():
                        metrics.record_first_audio_chunk()
                        if not first_audio_emitted:
                            first_audio_emitted = True
                            wyoming_elapsed_ms = int((time.monotonic() - stream_start) * 1000)
                            obs_log("first_wyoming_audio",
                                    connection_id=_ctx.connection_id,
                                    synthesis_id=_ctx.synthesis_id,
                                    text_fp=fp,
                                    elapsed_ms=wyoming_elapsed_ms,
                                    time_to_first_backend_audio_ms=first_audio_elapsed_ms if backend_data_observed else 0,
                                    wrapper_first_audio_forwarding_overhead_ms=wyoming_elapsed_ms - first_audio_elapsed_ms if backend_data_observed else 0)
                        yield AudioChunk(
                            rate=audio_config.sample_rate,
                            width=audio_config.width,
                            channels=audio_config.channels,
                            audio=audio_bytes,
                            timestamp=timestamp_ms,
                        ).event()
                        metrics.record_emitted_chunk(len(audio_bytes))
                        flush_chunks.append(len(audio_bytes))

                    if audio_start_emitted:
                        yield AudioStop(
                            timestamp=int(
                                rechunker.cumulative_frames * 1000 / audio_config.sample_rate
                            )
                        ).event()

                    total_elapsed_ms = int((time.monotonic() - stream_start) * 1000)
                    obs_log("backend_stream_done",
                            connection_id=_ctx.connection_id,
                            synthesis_id=_ctx.synthesis_id,
                            text_fp=fp,
                            total_backend_stream_ms=total_elapsed_ms,
                            total_pcm_bytes=metrics.total_emitted_bytes,
                            chunk_count=metrics.emitted_chunk_count,
                            status="ok")

                    obs_log("audio_out",
                            connection_id=_ctx.connection_id,
                            synthesis_id=_ctx.synthesis_id,
                            text_fp=fp,
                            audio_start=audio_start_emitted,
                            chunk_count=metrics.emitted_chunk_count,
                            pcm_bytes=metrics.total_emitted_bytes,
                            audio_stop=audio_start_emitted,
                            mode="streaming",
                            buffer_policy=buffer_policy,
                            status="ok")

                obs_log("synthesis_terminal",
                        connection_id=_ctx.connection_id,
                        synthesis_id=_ctx.synthesis_id,
                        text_fp=fp,
                        terminal_state="completed",
                        elapsed_ms=int((time.monotonic() - stream_start) * 1000))
                metrics.finalize("success")
                break  # exit retry loop on success

        except S2BackendBusyError as exc:
            obs_log("backend_busy",
                    connection_id=_ctx.connection_id,
                    synthesis_id=_ctx.synthesis_id,
                    text_fp=fp,
                    attempt=attempt,
                    max_attempts=max_total_attempts,
                    pcm_observed=backend_data_observed,
                    audio_start_emitted=audio_start_emitted)
            if (
                    not backend_data_observed
                    and not audio_start_emitted
                    and attempt < max_total_attempts
                ):
                obs_log("backend_busy_retry",
                        connection_id=_ctx.connection_id,
                        synthesis_id=_ctx.synthesis_id,
                        text_fp=fp,
                        retry_count=attempt,
                        max_total_attempts=max_total_attempts,
                        delay_ms=settings.s2_backend_busy_retry_delay_ms)
                attempt += 1
                await asyncio.sleep(
                    settings.s2_backend_busy_retry_delay_ms / 1000.0)
                continue
            obs_log("backend_busy_exhausted",
                    connection_id=_ctx.connection_id,
                    synthesis_id=_ctx.synthesis_id,
                    text_fp=fp,
                    attempt=attempt,
                    max_total_attempts=max_total_attempts,
                    pcm_observed=backend_data_observed,
                    audio_start_emitted=audio_start_emitted)
            metrics.finalize("error", "backend_busy_exhausted")
            raise
        except asyncio.TimeoutError:
            total_elapsed_ms = int((time.monotonic() - stream_start) * 1000)
            obs_log("synthesis_timeout",
                    connection_id=_ctx.connection_id,
                    synthesis_id=_ctx.synthesis_id,
                    text_fp=fp,
                    elapsed_ms=total_elapsed_ms,
                    pcm_bytes_received=metrics.total_emitted_bytes,
                    chunk_count=metrics.emitted_chunk_count,
                    audio_start_emitted=audio_start_emitted)
            obs_log("synthesis_terminal",
                    connection_id=_ctx.connection_id,
                    synthesis_id=_ctx.synthesis_id,
                    text_fp=fp,
                    terminal_state="timed_out",
                    elapsed_ms=total_elapsed_ms)
            metrics.finalize("error", "synthesis_timeout")
            raise
        except (GeneratorExit, asyncio.CancelledError) as exc:
            total_elapsed_ms = int((time.monotonic() - stream_start) * 1000)
            # Phase 10: structured cancellation observability.
            # Emit cancellation_requested before cancelling the backend stream
            # so the harness can correlate the wrapper intent independently of
            # whether the backend cancellation succeeds.
            obs_log("cancellation_requested",
                    connection_id=_ctx.connection_id,
                    synthesis_id=_ctx.synthesis_id,
                    text_fp=fp,
                    reason=type(exc).__name__,
                    elapsed_ms=total_elapsed_ms,
                    pcm_bytes_received=metrics.total_emitted_bytes,
                    chunk_count=metrics.emitted_chunk_count,
                    audio_start_emitted=audio_start_emitted)
            # Explicitly close the backend stream so any blocked
            # asyncio.to_thread(read) is unblocked promptly.
            try:
                stream.cancel()  # type: ignore[possibly-unbound]
            except Exception:
                pass
            obs_log("cancellation_propagated",
                    connection_id=_ctx.connection_id,
                    synthesis_id=_ctx.synthesis_id,
                    text_fp=fp,
                    reason=type(exc).__name__,
                    elapsed_ms=total_elapsed_ms)
            obs_log("synthesis_cancelled",
                    connection_id=_ctx.connection_id,
                    synthesis_id=_ctx.synthesis_id,
                    text_fp=fp,
                    reason=type(exc).__name__,
                    elapsed_ms=total_elapsed_ms,
                    pcm_bytes_received=metrics.total_emitted_bytes,
                    chunk_count=metrics.emitted_chunk_count,
                    audio_start_emitted=audio_start_emitted)
            # The ``with`` block's ``__exit__`` also cleans up the stream.
            obs_log("synthesis_terminal",
                    connection_id=_ctx.connection_id,
                    synthesis_id=_ctx.synthesis_id,
                    text_fp=fp,
                    terminal_state="cancelled",
                    elapsed_ms=total_elapsed_ms)
            metrics.finalize("cancelled")
            raise
        except S2ClientError:
            total_elapsed_ms = int((time.monotonic() - stream_start) * 1000)
            obs_log("backend_stream_done",
                    connection_id=_ctx.connection_id,
                    synthesis_id=_ctx.synthesis_id,
                    text_fp=fp,
                    total_elapsed_ms=total_elapsed_ms,
                    status="error",
                    error="S2ClientError")
            obs_log("synthesis_terminal",
                    connection_id=_ctx.connection_id,
                    synthesis_id=_ctx.synthesis_id,
                    text_fp=fp,
                    terminal_state="failed",
                    elapsed_ms=total_elapsed_ms,
                    error="S2ClientError")
            metrics.finalize("error", "S2ClientError")
            raise
        except Exception:
            total_elapsed_ms = int((time.monotonic() - stream_start) * 1000)
            exc_name = type(sys.exc_info()[1]).__name__
            obs_log("backend_stream_done",
                    connection_id=_ctx.connection_id,
                    synthesis_id=_ctx.synthesis_id,
                    text_fp=fp,
                    total_elapsed_ms=total_elapsed_ms,
                    status="error",
                    error=exc_name)
            obs_log("synthesis_terminal",
                    connection_id=_ctx.connection_id,
                    synthesis_id=_ctx.synthesis_id,
                    text_fp=fp,
                    terminal_state="failed",
                    elapsed_ms=total_elapsed_ms,
                    error=exc_name)
            metrics.finalize("error", exc_name)
            raise


# QueueFullError and QueueTimeoutError are imported from app.speech (Phase 9B Slice 6).
# The SingleWorkerSynthesisQueue compatibility wrapper was removed — all code
# uses SpeechScheduler directly.




def _resolve_voice_from_synthesize(
    synthesize: Synthesize,
    settings: Settings,
) -> str | None:
    """Determine the effective voice for a synthesis request.

    Resolves the voice selection priority:
    1. Client-requested voice (from the Wyoming Synthesize event).
    2. Configured ``S2_DEFAULT_VOICE`` (when valid and discovered).
    3. ``None`` — generic s2-pro/backend fallback.

    Returns *None* when a generic fallback should be used (no custom
    voice fields sent to the backend).

    Raises *ValueError* if the client or configured default names a
    voice that is not currently discovered.
    """
    discovered = discover_voices(settings.s2_voice_dir)
    discovered_set = frozenset(discovered)

    # 1. Client-requested voice.
    if synthesize.voice is not None and synthesize.voice.name:
        requested = synthesize.voice.name
        if requested not in discovered_set:
            raise ValueError(
                "Unknown voice '%s'; available: %s"
                % (requested, ', '.join(sorted(discovered)) if discovered else '(none)')
            )
        return requested

    # 2. Configured default.
    default = settings.s2_default_voice
    if default:
        if default not in discovered_set:
            raise ValueError(
                "Configured S2_DEFAULT_VOICE '%s' is not currently discovered; available: %s"
                % (default, ', '.join(sorted(discovered)) if discovered else '(none)')
            )
        return default

    # 3. Generic fallback — no custom voice fields.
    return None


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
        queue: SpeechScheduler,  # Phase 9B: SpeechScheduler
        settings: Settings,
        s2_client_factory: S2ClientFactory,
        coordinator: "ServiceCoordinator | None" = None,
    ) -> None:
        super().__init__(reader, writer)
        self.config = config
        self.queue = queue
        self.settings = settings
        self.s2_client_factory = s2_client_factory
        self.coordinator = coordinator
        if self.coordinator is not None:
            self.coordinator.register_handler(self)
        # Streaming state
        self._streaming_text_parts: list[str] = []
        self._in_streaming_session: bool = False
        # Voice selection for current request
        self._requested_voice: str | None = None
        # Compatibility state: text from a legacy synthesize event
        # received inside an active streaming session.
        self._streaming_compat_text: str = ""
        self._streaming_compat_voice: str | None = None
        # Observability
        self._conn_id = new_connection_id()
        peer = ""
        try:
            peername = writer.get_extra_info("peername")
            if peername:
                peer = f"{peername[0]}:{peername[1]}"
        except Exception:
            pass
        obs_log("conn_open", connection_id=self._conn_id, peer=peer)
        self._disconnect_cleanup_lock = asyncio.Lock()
        self._disconnect_logged = False
        self._transport_close_requested = False
        self._closed_audio_generators: set[int] = set()
        self._disconnected = False
        # Phase 9.5: progressive streaming coordinator
        self._stream_coordinator: StreamingCoordinator | None = None
        self._stream_consumer_task: asyncio.Task[None] | None = None
        self._stream_consumer_error: Exception | None = None
        self._stream_synthesis_id: str | None = None
        self._stream_session: SynthesisSession | None = None
        self._streaming_had_chunks: bool = False

    async def _synthesize_phrase(self, text: str) -> list[Event]:
        """Synthesize one scheduler-owned phrase using the established backend mode."""
        sid = self._stream_synthesis_id or new_synthesis_id()
        if self.settings.tts_backend == "s2cpp" and self.settings.s2_stream:
            events: list[Event] = []
            audio_generator = self._synthesize_text_streaming(
                text, voice=self._requested_voice, trigger="streaming",
                synthesis_id=sid,
            )
            if self._stream_session is not None:
                self._stream_session.set_generator(audio_generator)
                self._stream_session.set_cleanup(lambda: audio_generator.aclose())
            async for event in audio_generator:
                events.append(event)
            return events

        return await self._synthesize_text(
            text, voice=self._requested_voice, trigger="streaming",
            synthesis_id=sid,
        )

    def _is_expected_disconnect_error(self, error: Exception) -> bool:
        """Recognize network teardown, including Python 3.13 selector RST state."""
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return True
        if not isinstance(error, TypeError) or str(error) != "'NoneType' object is not callable":
            return False
        try:
            return self.writer.is_closing()
        except (AttributeError, RuntimeError):
            return False

    async def _handle_expected_disconnect(
        self, synthesis_id: str, *, audio_generator=None,
    ) -> None:
        """Finalize disconnect resources once, preserving cleanup failures."""
        cleanup_error: Exception | None = None
        async with self._disconnect_cleanup_lock:
            if not self._disconnect_logged:
                self._disconnect_logged = True
                obs_log("client_disconnected", connection_id=self._conn_id,
                        synthesis_id=synthesis_id, reason="write_failed")
            if audio_generator is not None:
                generator_id = id(audio_generator)
                if generator_id not in self._closed_audio_generators:
                    self._closed_audio_generators.add(generator_id)
                    try:
                        await audio_generator.aclose()
                    except Exception as exc:
                        cleanup_error = exc
                        obs_log("disconnect_cleanup_error",
                                connection_id=self._conn_id,
                                synthesis_id=synthesis_id,
                                operation="audio_generator_aclose",
                                error_type=type(exc).__name__,
                                detail=str(exc)[:200])
            if not self._transport_close_requested:
                self._transport_close_requested = True
                try:
                    self.writer.close()
                except Exception as exc:
                    obs_log("disconnect_cleanup_error",
                            connection_id=self._conn_id,
                            synthesis_id=synthesis_id,
                            operation="writer_close",
                            error_type=type(exc).__name__,
                            detail=str(exc)[:200])
                    if cleanup_error is None:
                        cleanup_error = exc
        if cleanup_error is not None:
            raise cleanup_error

    async def _close_generator_after_unexpected_write(
        self, synthesis_id: str, audio_generator, write_error: Exception,
    ) -> None:
        """Finalize a stream without hiding the original write error."""
        try:
            await audio_generator.aclose()
        except Exception as cleanup_error:
            obs_log("disconnect_cleanup_error",
                    connection_id=self._conn_id,
                    synthesis_id=synthesis_id,
                    operation="audio_generator_aclose_after_unexpected_write",
                    error_type=type(cleanup_error).__name__,
                    detail=str(cleanup_error)[:200])
            raise write_error from cleanup_error

    async def run(self) -> None:
        """Own expected transport teardown after base handler cleanup."""
        try:
            await super().run()
        except (BrokenPipeError, ConnectionResetError):
            return
        except TypeError as error:
            if self._is_expected_disconnect_error(error):
                return
            raise

    async def handle_event(self, event: Event) -> bool:
        """Handle one Wyoming event."""
        # ── Incoming event log ─────────────────────────────────────
        event_fields = {
            "connection_id": self._conn_id,
            "event_type": event.type,
            "streaming_active": self._in_streaming_session,
        }
        # Extract text length/fingerprint when present
        event_data = event.data if event.data else {}
        text_val = event_data.get("text", "")
        if isinstance(text_val, str) and text_val:
            event_fields["text_len"] = len(text_val)
            event_fields["text_fp"] = text_fingerprint(text_val)
        # Extract voice when present
        voice_dict = event_data.get("voice", {})
        if isinstance(voice_dict, dict) and voice_dict.get("name"):
            event_fields["voice_requested"] = str(voice_dict["name"])
        obs_log("event_in", **event_fields)

        if Describe.is_type(event.type):
            await self.write_event(build_info_event(self.settings))
            return True

        # ── Legacy (non-streaming) Synthesize ────────────────────────
        if Synthesize.is_type(event.type):
            # Phase 9C: reject synthesis when draining
            if self.coordinator is not None and not self.coordinator.lifecycle.accepts_new_work():
                await self.write_event(Error(
                    text="Service is shutting down",
                    code="service_shutting_down",
                ).event())
                return True
            synthesize = Synthesize.from_event(event)
            resolved = _resolve_voice_from_synthesize(
                synthesize, self.settings
            )

            # When a streaming session is already active, the legacy
            # synthesize event is a Home Assistant compatibility event
            # — do NOT trigger an immediate synthesis.  The actual
            # synthesis will happen when synthesize-stop finalises the
            # streaming session.
            if self._in_streaming_session:
                fp = text_fingerprint(synthesize.text)

                # Voice consistency check.
                if resolved:
                    if self._requested_voice and resolved != self._requested_voice:
                        obs_log("compatibility_synthesize_deferred",
                                connection_id=self._conn_id,
                                text_fp=fp,
                                text_len=len(synthesize.text),
                                voice=resolved,
                                streaming_voice=self._requested_voice,
                                status="voice_mismatch")
                        raise ValueError(
                            "Compatibility synthesize voice '%s' does not match "
                            "streaming session voice '%s'"
                            % (resolved, self._requested_voice)
                        )

                # Phase 9.5: defer compat text; feed only at stop if no
                # non-whitespace streaming chunks arrived
                if self._stream_coordinator is not None:
                    self._streaming_compat_text = synthesize.text
                    self._streaming_compat_voice = resolved
                    if resolved and not self._requested_voice:
                        self._requested_voice = resolved
                    obs_log("compatibility_synthesize_deferred",
                            connection_id=self._conn_id,
                            text_fp=fp,
                            text_len=len(synthesize.text),
                            voice=resolved or "generic",
                            status="deferred")
                    return True

                self._streaming_compat_text = synthesize.text
                self._streaming_compat_voice = resolved
                if resolved and not self._requested_voice:
                    self._requested_voice = resolved

                obs_log("compatibility_synthesize_deferred",
                        connection_id=self._conn_id,
                        text_fp=fp,
                        text_len=len(synthesize.text),
                        voice=resolved or "generic",
                        status="deferred")
                return True

            # No active streaming session — normal standalone synthesis.
            self._requested_voice = resolved

            async def send_audio() -> None:
                session = SynthesisSession(
                    request=SpeechRequest(synthesis_id=syn_id, connection_id=self._conn_id, text=""),
                    trigger="legacy",
                )
                client_connected = True
                # Import Wyoming audio types for session tracking
                from wyoming.audio import AudioStart as WAStart, AudioStop as WAStop

                def _track(session, ev):
                    if WAStart.is_type(ev.type):
                        session.mark_audio_start()
                    elif WAStop.is_type(ev.type):
                        session.mark_audio_stop()
                try:
                    if (
                        self.settings.tts_backend == "s2cpp"
                        and self.settings.s2_stream
                    ):
                        audio_generator = self._synthesize_text_streaming(
                            synthesize.text, voice=self._requested_voice,
                        )
                        session.set_generator(audio_generator)
                        session.set_cleanup(lambda: audio_generator.aclose())
                        async for audio_event in audio_generator:
                            _track(session, audio_event)
                            try:
                                await self.write_event(audio_event)
                            except (BrokenPipeError, ConnectionResetError, TypeError) as disconnect_error:
                                if not self._is_expected_disconnect_error(disconnect_error):
                                    raise
                                client_connected = False
                                await session.disconnect()
                                await self._handle_expected_disconnect(syn_id)
                                break
                            except Exception as write_error:
                                client_connected = False
                                session.mark_client_disconnected()
                                obs_log("synthesis_error",
                                        connection_id=self._conn_id,
                                        synthesis_id=syn_id,
                                        error="unexpected_write_event_failure")
                                await self._close_generator_after_unexpected_write(
                                    syn_id, audio_generator, write_error)
                                raise
                    else:
                        audio_events = await self._synthesize_text(
                            synthesize.text, voice=self._requested_voice
                        )
                        for audio_event in audio_events:
                            _track(session, audio_event)
                            try:
                                await self.write_event(audio_event)
                            except (BrokenPipeError, ConnectionResetError, TypeError) as disconnect_error:
                                if not self._is_expected_disconnect_error(disconnect_error):
                                    raise
                                client_connected = False
                                session.mark_client_disconnected()
                                await self._handle_expected_disconnect(syn_id)
                                break
                            except Exception:
                                client_connected = False
                                obs_log("synthesis_error",
                                        connection_id=self._conn_id,
                                        synthesis_id="legacy",
                                        error="unexpected_write_event_failure")
                                raise
                except asyncio.CancelledError:
                    session.mark_cancelled()
                    obs_log("synthesis_cancel_requested",
                            connection_id=self._conn_id,
                            synthesis_id="legacy",
                            reason="task_cancelled")
                    raise
                except asyncio.TimeoutError:
                    # The wrapper boundary emits the terminal Wyoming Error.
                    raise

                except Exception as unexpected_exc:
                    obs_log("synthesis_error",
                            connection_id=self._conn_id,
                            synthesis_id="legacy",
                            error=type(unexpected_exc).__name__,
                            detail=str(unexpected_exc)[:200])
                    raise

            if self.settings.cancel_on_new_request:
                await self.queue.cancel_new_request(self._conn_id)
                await asyncio.sleep(0)

            syn_id = new_synthesis_id()
            await self._run_operational(send_audio, synthesis_id=syn_id, trigger="legacy")
            return True

        # ── Streaming TTS: synthesize-start ──────────────────────────
        if SynthesizeStart.is_type(event.type):
            # Phase 9.5: duplicate start detection
            if self._in_streaming_session or self._stream_coordinator is not None:
                # Duplicate SynthesizeStart — controlled error, preserve session
                await self.write_event(Error(
                    text="Streaming synthesis already active",
                    code="stream_already_active",
                ).event())
                return True

            self._streaming_text_parts = []
            self._streaming_compat_text = ""
            self._streaming_compat_voice = None
            self._streaming_had_chunks = False
            self._in_streaming_session = True
            # Resolve voice: client-requested > configured default > generic.
            start_data = event.data if event.data else {}
            voice_dict = start_data.get("voice", {})
            if isinstance(voice_dict, dict) and voice_dict.get("name"):
                from wyoming.tts import SynthesizeVoice
                fake_syn = Synthesize(
                    text="",
                    voice=SynthesizeVoice(name=str(voice_dict["name"])),
                )
                self._requested_voice = _resolve_voice_from_synthesize(
                    fake_syn, self.settings
                )
            else:
                self._requested_voice = _resolve_voice_from_synthesize(
                    Synthesize(text=""), self.settings
                )

            # Phase 9.5: shared synthesis ID for the streaming session
            self._stream_synthesis_id = new_synthesis_id()
            self._stream_session = SynthesisSession(
                request=SpeechRequest(
                    synthesis_id=self._stream_synthesis_id,
                    connection_id=self._conn_id,
                    text="",
                ),
                trigger="streaming",
            )
            # Phase 9.5: create and start progressive streaming coordinator
            self._stream_coordinator = StreamingCoordinator(
                scheduler=self.queue,
                synthesize_fn=self._synthesize_phrase,
                connection_id=self._conn_id,
            )
            await self._stream_coordinator.start()
            # Start consumer task immediately so it's ready for events
            self._stream_consumer_task = asyncio.create_task(
                self._consume_coordinator_events()
            )
            return True

        # ── Streaming TTS: synthesize-chunk ──────────────────────────
        if SynthesizeChunk.is_type(event.type):
            chunk = SynthesizeChunk.from_event(event)
            if not self._in_streaming_session:
                return True
            # Phase 9.5: feed text through coordinator immediately
            if self._stream_coordinator is not None:
                self._stream_coordinator.feed_text(chunk.text)
                if chunk.text and chunk.text.strip():
                    self._streaming_had_chunks = True
            else:
                self._streaming_text_parts.append(chunk.text)
            return True


        # ── Streaming TTS: synthesize-stop ───────────────────────────
        if SynthesizeStop.is_type(event.type):
            if not self._in_streaming_session:
                return True

            # Phase 9.5: progressive path via coordinator
            if self._stream_coordinator is not None:
                # Feed deferred compat text only if no non-whitespace chunks arrived
                if not self._streaming_had_chunks:
                    compat = self._streaming_compat_text.strip()
                    if compat:
                        self._stream_coordinator.feed_text(compat)

                self._stream_coordinator.feed_done()

                # Wait for consumer task to finish
                if self._stream_consumer_task is not None:
                    try:
                        await self._stream_consumer_task
                    except asyncio.CancelledError:
                        pass

                # Check if coordinator succeeded
                consumer_error = self._stream_consumer_error
                if consumer_error is not None:
                    # Synthesis failed — send error
                    try:
                        await self.write_event(Error(
                            text="Progressive synthesis failed",
                            code="stream_synthesis_failed",
                        ).event())
                    except (BrokenPipeError, ConnectionResetError, TypeError) as disconnect_error:
                        if not self._is_expected_disconnect_error(disconnect_error):
                            raise
                        await self._handle_expected_disconnect("streaming")
                elif (
                    self._stream_session is not None
                    and self._stream_session.eligible_for_synthesize_stopped
                    and self._stream_session.client_connected
                ):
                    # Success — write SynthesizeStopped
                    try:
                        await self.write_event(SynthesizeStopped().event())
                        obs_log("syn_stopped",
                                connection_id=self._conn_id,
                                synthesis_id=self._stream_synthesis_id,
                                trigger="streaming")
                    except (BrokenPipeError, ConnectionResetError, TypeError) as disconnect_error:
                        if not self._is_expected_disconnect_error(disconnect_error):
                            raise
                        await self._handle_expected_disconnect("streaming")

                # Clean up
                self._stream_coordinator = None
                self._stream_consumer_task = None
                self._stream_consumer_error = None
                self._stream_synthesis_id = None
                self._stream_session = None
                self._streaming_text_parts = []
                self._streaming_compat_text = ""
                self._streaming_compat_voice = None
                self._streaming_had_chunks = False
                self._in_streaming_session = False
                return True

            # Legacy path: accumulate all chunks, synthesize at once
            accumulated = "".join(self._streaming_text_parts).strip()
            compat_text = self._streaming_compat_text.strip()
            self._streaming_text_parts = []
            self._streaming_compat_text = ""
            self._streaming_compat_voice = None
            self._in_streaming_session = False

            # If no chunks arrived but a compatibility synthesize event
            # provided text, use that as a fallback.
            if not accumulated and compat_text:
                accumulated = compat_text

            if accumulated:
                # Phase 9C: reject streaming synthesis when draining
                if self.coordinator is not None and not self.coordinator.lifecycle.accepts_new_work():
                    await self.write_event(Error(
                        text="Service is shutting down",
                        code="service_shutting_down",
                    ).event())
                    return True
                syn_id = new_synthesis_id()
                async def send_streaming_audio() -> None:
                    session = SynthesisSession(
                        request=SpeechRequest(synthesis_id=syn_id, connection_id=self._conn_id, text=""),
                        trigger="streaming",
                    )
                    syn_start = time.monotonic()
                    client_connected = True
                    from wyoming.audio import AudioStart as WAStart, AudioStop as WAStop

                    def _track(session, ev):
                        if WAStart.is_type(ev.type):
                            session.mark_audio_start()
                        elif WAStop.is_type(ev.type):
                            session.mark_audio_stop()
                    try:
                        if (
                            self.settings.tts_backend == "s2cpp"
                            and self.settings.s2_stream
                        ):
                            audio_generator = self._synthesize_text_streaming(
                                accumulated, voice=self._requested_voice,
                                trigger="streaming", synthesis_id=syn_id,
                            )
                            session.set_generator(audio_generator)
                            session.set_cleanup(lambda: audio_generator.aclose())
                            async for audio_event in audio_generator:
                                _track(session, audio_event)
                                try:
                                    await self.write_event(audio_event)
                                except (BrokenPipeError, ConnectionResetError, TypeError) as disconnect_error:
                                    if not self._is_expected_disconnect_error(disconnect_error):
                                        raise
                                    client_connected = False
                                    await session.disconnect()
                                    await self._handle_expected_disconnect(syn_id)
                                    break
                                except Exception as write_error:
                                    client_connected = False
                                    session.mark_client_disconnected()
                                    obs_log("synthesis_error",
                                            connection_id=self._conn_id,
                                            synthesis_id=syn_id,
                                            error="unexpected_write_event_failure")
                                    await self._close_generator_after_unexpected_write(
                                        syn_id, audio_generator, write_error)
                                    raise
                        else:
                            audio_events = await self._synthesize_text(
                                accumulated, voice=self._requested_voice,
                                trigger="streaming", synthesis_id=syn_id,
                            )
                            for audio_event in audio_events:
                                _track(session, audio_event)
                                try:
                                    await self.write_event(audio_event)
                                except (BrokenPipeError, ConnectionResetError, TypeError) as disconnect_error:
                                    if not self._is_expected_disconnect_error(disconnect_error):
                                        raise
                                    client_connected = False
                                    session.mark_client_disconnected()
                                    await self._handle_expected_disconnect(syn_id)
                                    break
                                except Exception:
                                    client_connected = False
                                    obs_log("synthesis_error",
                                            connection_id=self._conn_id,
                                            synthesis_id=syn_id,
                                            error="unexpected_write_event_failure")
                                    raise
                        # Signal end of streaming response (gated on session eligibility)
                        if client_connected and session.eligible_for_synthesize_stopped:
                            total_synthesis_ms = int((time.monotonic() - syn_start) * 1000)
                            try:
                                await self.write_event(SynthesizeStopped().event())
                            except (BrokenPipeError, ConnectionResetError, TypeError) as disconnect_error:
                                if not self._is_expected_disconnect_error(disconnect_error):
                                    raise
                                client_connected = False
                                session.mark_client_disconnected()
                                await self._handle_expected_disconnect(syn_id)
                            except Exception:
                                obs_log("synthesis_error",
                                        connection_id=self._conn_id,
                                        synthesis_id=syn_id,
                                        error="unexpected_write_event_failure")
                                raise
                            if client_connected:
                                obs_log("syn_stopped",
                                        connection_id=self._conn_id,
                                        synthesis_id=syn_id,
                                        trigger="streaming",
                                        total_synthesis_ms=total_synthesis_ms)
                        else:
                            obs_log("syn_stopped",
                                    connection_id=self._conn_id,
                                    synthesis_id=syn_id,
                                    trigger="streaming",
                                    status="client_disconnected")
                    except asyncio.CancelledError:
                        session.mark_cancelled()
                        obs_log("synthesis_cancel_requested",
                                connection_id=self._conn_id,
                                synthesis_id=syn_id,
                                reason="task_cancelled")
                        raise

                if self.settings.cancel_on_new_request:
                    await self.queue.cancel_new_request(self._conn_id)
                    await asyncio.sleep(0)

                await self._run_operational(send_streaming_audio, synthesis_id=syn_id, trigger="streaming")
            else:
                # No text accumulated — still signal end of streaming response.
                syn_id = new_synthesis_id()
                try:
                    await self.write_event(SynthesizeStopped().event())
                except (BrokenPipeError, ConnectionResetError, TypeError) as disconnect_error:
                    if not self._is_expected_disconnect_error(disconnect_error):
                        raise
                    await self._handle_expected_disconnect(syn_id)
                except Exception:
                    obs_log("synthesis_error", connection_id=self._conn_id,
                            synthesis_id=syn_id,
                            error="unexpected_write_event_failure")
                    raise

            return True

        return True

    async def _consume_coordinator_events(self) -> None:
        """Consume coordinator output events and write them to the client.

        Runs as a background task started on SynthesizeStart.
        Writes audio events progressively as phrases are synthesized.
        Stores any exception in ``self._stream_consumer_error`` so the
        stop handler can observe failures.
        """
        try:
            coord = self._stream_coordinator
            if coord is None:
                return
            async for event in coord:
                try:
                    from wyoming.audio import AudioStart as WAStart, AudioStop as WAStop
                    from wyoming.error import Error as WError
                    if self._stream_session is not None:
                        if WAStart.is_type(event.type):
                            self._stream_session.mark_audio_start()
                        elif WAStop.is_type(event.type):
                            self._stream_session.mark_audio_stop()
                        elif WError.is_type(event.type):
                            self._stream_session.mark_failed()
                    await self.write_event(event)
                except (BrokenPipeError, ConnectionResetError, TypeError) as disconnect_error:
                    if not self._is_expected_disconnect_error(disconnect_error):
                        raise
                    if self._stream_session is not None:
                        await self._stream_session.disconnect()
                    await self._handle_expected_disconnect("streaming")
                    self._stream_consumer_error = disconnect_error
                    # Cancel coordinator to stop synthesis loop
                    if self._stream_coordinator is not None:
                        try:
                            await self._stream_coordinator.cancel()
                        except Exception:
                            pass
                    break
                except Exception:
                    obs_log("synthesis_error",
                            connection_id=self._conn_id,
                            synthesis_id="streaming",
                            error="unexpected_write_event_failure")
                    if self._stream_session is not None:
                        try:
                            await self._stream_session.disconnect()
                        except Exception:
                            pass
                    if self._stream_coordinator is not None:
                        try:
                            await self._stream_coordinator.cancel()
                        except Exception:
                            pass
                    raise
        except asyncio.CancelledError:
            self._stream_consumer_error = asyncio.CancelledError("coordinator consumer cancelled")
        except Exception as exc:
            self._stream_consumer_error = exc
            obs_log("synthesis_error",
                    connection_id=self._conn_id,
                    synthesis_id="streaming",
                    error="coordinator_consumer_exception")



    async def _run_operational(self, operation, synthesis_id: str,
                               trigger: str = "legacy") -> None:
        """Own expected runtime failures and terminate them on the wire."""
        try:
            request = SpeechRequest(
                synthesis_id=synthesis_id,
                connection_id=self._conn_id,
                text="",  # text is captured by observability layer separately
                metadata=SpeechMetadata(trigger=trigger),
            )
            await self.queue.run(request, operation)
            return
        except asyncio.CancelledError:
            # Queue cancellation is requested by disconnect/barge-in cleanup.
            return
        except QueueFullError as exc:
            code = "queue_full"
            detail = str(exc)
        except QueueTimeoutError as exc:
            code = "queue_timeout"
            detail = str(exc)
        except S2BackendBusyError as exc:
            code = "backend_busy"
            detail = str(exc)
        except asyncio.TimeoutError as exc:
            code = "synthesis_timeout"
            detail = str(exc)
        except S2ClientError as exc:
            code = "backend_error"
            detail = str(exc)

        obs_log("synthesis_terminal_error", connection_id=self._conn_id,
                synthesis_id=synthesis_id, code=code, detail=detail[:200])
        try:
            await self.write_event(Error(text=detail, code=code).event())
        except (BrokenPipeError, ConnectionResetError, TypeError) as disconnect_error:
            if not self._is_expected_disconnect_error(disconnect_error):
                raise
            await self._handle_expected_disconnect(synthesis_id)

    async def disconnect(self) -> None:
        """Log connection close and cancel queue entries, then delegate.

        Idempotent — safe to call multiple times (e.g. from both the
        server's handler cleanup and the coordinator's explicit close).
        """
        if self._disconnected:
            return
        self._disconnected = True

        self._streaming_text_parts = []
        self._streaming_compat_text = ""
        self._streaming_compat_voice = None
        self._in_streaming_session = False
        # Phase 9.5: cancel coordinator and consumer
        if self._stream_coordinator is not None:
            try:
                await self._stream_coordinator.cancel()
            except Exception:
                pass
            self._stream_coordinator = None
        if self._stream_consumer_task is not None and not self._stream_consumer_task.done():
            self._stream_consumer_task.cancel()
            try:
                await self._stream_consumer_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stream_consumer_task = None
        self._stream_consumer_error = None
        self._stream_synthesis_id = None
        self._stream_session = None
        self._streaming_had_chunks = False
        if self.settings.cancel_on_client_disconnect:
            await self.queue.cancel_connection(self._conn_id)
            self.queue.cancel_active_for_connection(self._conn_id)
        obs_log("conn_close", connection_id=self._conn_id)
        # Phase 9C: unregister from coordinator
        if self.coordinator is not None:
            self.coordinator.unregister_handler(self)
        await super().disconnect()

    async def _synthesize_text(
        self, text: str, voice: str | None = None,
        trigger: str = "legacy", synthesis_id: str = "",
    ) -> list[Event]:
        """Run synthesis for given text and return Wyoming audio events.

        Args:
            text: The text to synthesize.
            voice: Requested voice profile ID (from Wyoming voice selection),
                   or *None* to use the configured default or generic fallback.
            trigger: How synthesis was triggered (``"legacy"`` or ``"streaming"``).
            synthesis_id: Pre-generated ID for log correlation, or empty to
                auto-generate one.
        """
        synthesis_id = synthesis_id or new_synthesis_id()
        ctx = LogContext(connection_id=self._conn_id, synthesis_id=synthesis_id)

        # Determine voice source for logging
        voice_source = "client" if voice else (
            "default" if self.settings.s2_default_voice else "generic"
        )

        obs_log("syn_trigger",
                connection_id=self._conn_id,
                synthesis_id=synthesis_id,
                trigger=trigger,
                text_fp=text_fingerprint(text),
                text_len=len(text),
                voice=voice or self.settings.s2_default_voice or "generic",
                voice_source=voice_source)

        if self.settings.tts_backend == "s2cpp":
            s2_client = self.s2_client_factory(self.settings)
            return await asyncio.to_thread(
                synthesize_s2cpp_tts_events,
                text,
                s2_client,
                self.settings,
                self.config,
                voice=voice,
                ctx=ctx,
            )
        else:
            return synthesize_fake_tts_events(
                text,
                config=self.config,
                ctx=ctx,
            )

    async def _synthesize_text_streaming(
        self, text: str, voice: str | None = None,
        trigger: str = "legacy", synthesis_id: str = "",
    ):
        """Async generator: yield Wyoming audio events progressively.

        When ``S2_STREAM=true`` and the backend is ``s2cpp``, this uses
        ``synthesize_s2cpp_streaming_tts_events()`` to yield events as
        backend transport chunks arrive rather than buffering the complete
        response first.  The fake backend is yielded one-at-a-time for
        consistency but is still fully buffered internally.
        """
        synthesis_id = synthesis_id or new_synthesis_id()
        ctx = LogContext(connection_id=self._conn_id, synthesis_id=synthesis_id)

        voice_source = "client" if voice else (
            "default" if self.settings.s2_default_voice else "generic"
        )

        obs_log("syn_trigger",
                connection_id=self._conn_id,
                synthesis_id=synthesis_id,
                trigger=trigger,
                text_fp=text_fingerprint(text),
                text_len=len(text),
                voice=voice or self.settings.s2_default_voice or "generic",
                voice_source=voice_source,
                mode="streaming")

        if self.settings.tts_backend == "s2cpp":
            s2_client = self.s2_client_factory(self.settings)
            request = S2GenerateRequest.from_settings(
                text=text, settings=self.settings, voice=voice,
            )
            async for event in synthesize_s2cpp_streaming_tts_events(
                s2_client, request, self.config, self.settings, ctx=ctx,
            ):
                yield event
        else:
            # Fake backend yielded one event at a time for consistent interface.
            for event in synthesize_fake_tts_events(
                text, config=self.config, ctx=ctx,
            ):
                yield event


@dataclass
class RunningFakeTtsServer:
    """Handle for a started fake Wyoming TCP server."""

    server: AsyncTcpServer
    host: str
    port: int
    scheduler: SpeechScheduler | None = None  # Phase 9C: typed scheduler exposure

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
    coordinator: "ServiceCoordinator | None" = None,
) -> RunningFakeTtsServer:
    """Start the Wyoming TTS server without blocking.

    Despite the historical function name, this can run either the default fake
    backend or the opt-in buffered s2.cpp backend based on `settings.tts_backend`.
    """
    active_settings = settings or Settings()
    fake_config = config or FakeTtsConfig.from_settings(active_settings)
    counters = coordinator.counters if coordinator is not None else None
    queue = SpeechScheduler(
        max_size=max_queue_size,
        wait_timeout_sec=active_settings.s2_queue_wait_timeout_sec,
        counters=counters,
    )
    server = AsyncTcpServer(host, port)

    def handler_factory(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        return FakeTtsEventHandler(
            reader,
            writer,
            fake_config,
            queue,
            active_settings,
            s2_client_factory,
            coordinator=coordinator,
        )

    await server.start(handler_factory)
    raw_server = getattr(server, "_server", None)
    bound_port = port
    if raw_server is not None and raw_server.sockets:
        bound_port = int(raw_server.sockets[0].getsockname()[1])

    result = RunningFakeTtsServer(server=server, host=host, port=bound_port, scheduler=queue)
    return result


def describe_planned_server() -> str:
    """Return a human-readable description of the planned Wyoming endpoint."""
    return "Phase 1 fake Wyoming TTS server on tcp://0.0.0.0:10200"


def run_server(settings: Settings | None = None) -> int:
    """Run the Wyoming TCP server with graceful shutdown via ServiceCoordinator.

    Phase 9C: Uses ServiceCoordinator for lifecycle management with
    SIGTERM/SIGINT signal handling, bounded grace period, and
    deterministic drain/cancel.  Returns 0 on clean shutdown,
    non-zero on failure.
    """
    active_settings = settings or Settings()

    async def runner() -> int:
        from app.coordinator import ServiceCoordinator

        coordinator = ServiceCoordinator(active_settings)

        # Install signal handlers via coordinator (owned tasks)
        loop = asyncio.get_running_loop()
        coordinator.install_signal_handlers(loop)

        try:
            try:
                await coordinator.start()
            except Exception:
                print(
                    f"Wyoming TTS server failed to start: "
                    f"{coordinator.lifecycle.state.value}",
                    file=sys.stderr,
                )
                return 1

            await coordinator.wait_for_shutdown()
            return 0 if coordinator.lifecycle.state == LifecycleState.STOPPED else 1
        finally:
            await coordinator.remove_signal_handlers(loop)

    # Note: signal handlers are removed via coordinator.remove_signal_handlers
    # in the runner's finally block on all exit paths.
    try:
        exit_code = asyncio.run(runner())
    except KeyboardInterrupt:
        print("Wyoming TTS server stopped")
        exit_code = 0

    return exit_code

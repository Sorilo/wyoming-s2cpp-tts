"""Audio helpers for fake/test PCM and streaming utilities.

Phase 1 intentionally generates deterministic local PCM test audio only. Real
Fish Speech/s2.cpp audio conversion belongs in later phases.

Phase 5C adds ``StreamingPCMRechunker`` for progressive PCM frame alignment
across arbitrary backend transport chunk boundaries.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterator
from dataclasses import dataclass


PCM_WIDTH_BYTES = 2
PCM_CHANNELS = 1


@dataclass(frozen=True)
class DeclaredPCMFormat:
    """Validated raw PCM format declared by an s2.cpp HTTP response."""

    sample_rate: int
    channels: int
    width: int = PCM_WIDTH_BYTES


def _parse_content_type(value: str) -> tuple[str, dict[str, str]]:
    """Return lowercase media type and semicolon parameters."""
    parts = [p.strip() for p in value.split(";") if p.strip()]
    media_type = parts[0].lower() if parts else ""
    params: dict[str, str] = {}
    for part in parts[1:]:
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        params[key.strip().lower()] = val.strip().strip('"')
    return media_type, params


def _parse_positive_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def parse_declared_pcm_s16le_format(
    *,
    content_type: str,
    headers: dict[str, str] | None = None,
) -> DeclaredPCMFormat:
    """Validate response metadata for declared raw mono/stereo PCM s16le.

    This intentionally accepts only explicitly declared raw PCM responses.
    ``application/octet-stream`` or missing/contradictory metadata is rejected
    so arbitrary binary payloads are not treated as Wyoming audio.
    """
    media_type, ct_params = _parse_content_type(content_type)
    normalised_headers = {k.lower(): v for k, v in (headers or {}).items()}

    x_encoding = normalised_headers.get("x-audio-encoding")
    declares_pcm = (
        media_type in {"audio/l16", "audio/pcm", "audio/x-pcm"}
        or x_encoding == "pcm_s16le"
    )
    if not declares_pcm:
        raise ValueError("unsupported PCM response format")

    if x_encoding != "pcm_s16le":
        raise ValueError("missing PCM metadata: x-audio-encoding=pcm_s16le required")

    ct_rate = _parse_positive_int(ct_params.get("rate"))
    ct_channels = _parse_positive_int(ct_params.get("channels"))
    x_rate = _parse_positive_int(normalised_headers.get("x-audio-sample-rate"))
    x_channels = _parse_positive_int(normalised_headers.get("x-audio-channels"))

    if ct_rate is not None and x_rate is not None and ct_rate != x_rate:
        raise ValueError("conflicting PCM metadata: sample rate")
    if ct_channels is not None and x_channels is not None and ct_channels != x_channels:
        raise ValueError("conflicting PCM metadata: channels")

    sample_rate = x_rate or ct_rate
    channels = x_channels or ct_channels
    if sample_rate is None or channels is None:
        raise ValueError("missing PCM metadata: sample rate and channels required")

    return DeclaredPCMFormat(
        sample_rate=sample_rate,
        channels=channels,
        width=PCM_WIDTH_BYTES,
    )


def validate_declared_pcm_s16le(
    audio: bytes,
    *,
    content_type: str,
    headers: dict[str, str] | None = None,
) -> DeclaredPCMFormat:
    """Validate declared raw PCM s16le metadata and frame alignment."""
    fmt = parse_declared_pcm_s16le_format(
        content_type=content_type,
        headers=headers,
    )
    frame_size = fmt.width * fmt.channels
    if not audio:
        raise ValueError("empty PCM response")
    if len(audio) % frame_size != 0:
        raise ValueError(
            f"PCM payload is not frame-aligned: {len(audio)} byte(s) for "
            f"{frame_size}-byte frames"
        )
    return fmt


def pcm_s16le_silence(duration_ms: int, sample_rate: int = 22050) -> bytes:
    """Return silent mono PCM s16le bytes for cheap tests/placeholders."""
    if duration_ms < 0:
        raise ValueError("duration_ms must be non-negative")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    sample_count = int(sample_rate * (duration_ms / 1000.0))
    return b"\x00\x00" * sample_count


def deterministic_tone_frequency(text: str) -> int:
    """Return a stable, pleasant-ish test tone frequency for input text."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return 440 + (digest[0] % 220)


def pcm_s16le_test_tone(
    text: str,
    duration_ms: int = 600,
    sample_rate: int = 22050,
    volume: float = 0.18,
) -> bytes:
    """Return deterministic mono PCM s16le test tone bytes for fake TTS.

    The generated audio is intentionally simple and local. It proves Wyoming
    audio transport without depending on s2.cpp, CUDA, models, or network calls.
    """
    if duration_ms < 0:
        raise ValueError("duration_ms must be non-negative")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if not 0 <= volume <= 1:
        raise ValueError("volume must be between 0 and 1")

    sample_count = int(sample_rate * (duration_ms / 1000.0))
    frequency = deterministic_tone_frequency(text)
    max_amplitude = int(32767 * volume)

    samples = bytearray()
    for sample_index in range(sample_count):
        sample = int(
            max_amplitude
            * math.sin(2 * math.pi * frequency * (sample_index / sample_rate))
        )
        samples.extend(sample.to_bytes(PCM_WIDTH_BYTES, byteorder="little", signed=True))

    return bytes(samples)


class StreamingPCMRechunker:
    """Convert progressive raw PCM s16le bytes into frame-aligned Wyoming chunks.

    Phase 5C: Accepts arbitrary transport chunks from a streaming backend
    (``S2StreamResult``) and produces properly frame-aligned audio byte
    sequences ready for ``AudioChunk`` events.  Partial PCM frames that span
    transport boundaries are carried over in a bounded internal buffer (at
    most ``frame_size - 1`` bytes).  Timestamps are computed from cumulative
    complete PCM frames emitted, not from transport chunk boundaries.
    """

    def __init__(
        self,
        sample_rate: int,
        chunk_ms: int,
        width: int = PCM_WIDTH_BYTES,
        channels: int = PCM_CHANNELS,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if chunk_ms <= 0:
            raise ValueError("chunk_ms must be positive")
        if width <= 0:
            raise ValueError("width must be positive")
        if channels <= 0:
            raise ValueError("channels must be positive")

        self.sample_rate = sample_rate
        self.chunk_ms = chunk_ms
        self.frame_size: int = width * channels
        chunk_frames = max(1, int(sample_rate * (chunk_ms / 1000.0)))
        self._chunk_bytes: int = chunk_frames * self.frame_size

        self._carry: bytes = b""
        self._cumulative_frames: int = 0

    @property
    def cumulative_frames(self) -> int:
        """Total complete PCM frames emitted so far."""
        return self._cumulative_frames

    def feed(self, data: bytes) -> list[tuple[bytes, int]]:
        """Feed one backend transport chunk; return Wyoming-ready chunks.

        Each returned tuple is ``(aligned_audio_bytes, timestamp_ms)``.
        Partial PCM frames are retained internally.  Complete frames are
        accumulated until a full Wyoming output chunk is ready (combining
        across transport boundaries), then emitted.  Large transport chunks
        are split into multiple Wyoming chunks.
        """
        buffer = self._carry + data

        # Isolate complete PCM frames from trailing partial-frame bytes.
        complete_bytes = len(buffer) - (len(buffer) % self.frame_size)
        framed = buffer[:complete_bytes]
        carry = buffer[complete_bytes:]

        results: list[tuple[bytes, int]] = []
        offset = 0
        while offset + self._chunk_bytes <= len(framed):
            chunk = framed[offset : offset + self._chunk_bytes]
            offset += self._chunk_bytes
            timestamp_ms = int(
                self._cumulative_frames * 1000 / self.sample_rate
            )
            results.append((chunk, timestamp_ms))
            self._cumulative_frames += len(chunk) // self.frame_size

        # Retain: complete frames below chunk size + partial-frame bytes.
        self._carry = framed[offset:] + carry
        return results

    def flush(self) -> list[tuple[bytes, int]]:
        """Emit any remaining complete frames in the carry buffer.

        Raises ``ValueError`` when the carry buffer holds an incomplete
        PCM frame at stream end — this explicitly rejects truncated audio
        from a misbehaving backend rather than silently dropping or
        emitting malformed bytes.
        """
        if not self._carry:
            return []

        if len(self._carry) % self.frame_size != 0:
            raise ValueError(
                f"Final incomplete PCM frame: {len(self._carry)} byte(s) "
                f"remaining after stream end ({self.frame_size}-byte frames)"
            )

        chunk = self._carry
        timestamp_ms = int(
            self._cumulative_frames * 1000 / self.sample_rate
        )
        self._cumulative_frames += len(chunk) // self.frame_size
        self._carry = b""
        return [(chunk, timestamp_ms)]


def chunk_pcm_s16le(
    pcm: bytes,
    sample_rate: int,
    chunk_ms: int,
    width: int = PCM_WIDTH_BYTES,
    channels: int = PCM_CHANNELS,
) -> Iterator[bytes]:
    """Yield PCM chunks aligned to whole samples."""
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if chunk_ms <= 0:
        raise ValueError("chunk_ms must be positive")
    if width <= 0:
        raise ValueError("width must be positive")
    if channels <= 0:
        raise ValueError("channels must be positive")

    frame_width = width * channels
    chunk_frames = max(1, int(sample_rate * (chunk_ms / 1000.0)))
    chunk_bytes = chunk_frames * frame_width

    for offset in range(0, len(pcm), chunk_bytes):
        chunk = pcm[offset : offset + chunk_bytes]
        if chunk:
            yield chunk

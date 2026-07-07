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


PCM_WIDTH_BYTES = 2
PCM_CHANNELS = 1


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

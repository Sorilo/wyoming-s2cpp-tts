"""Audio helpers for fake/test PCM and future streaming utilities.

Phase 1 intentionally generates deterministic local PCM test audio only. Real
Fish Speech/s2.cpp audio conversion belongs in later phases.
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

"""Audio helper scaffold.

Future phases will add PCM generation for fake tests, WAV header handling, and
stream chunk utilities. Keep helpers small and testable.
"""

from __future__ import annotations


def pcm_s16le_silence(duration_ms: int, sample_rate: int = 22050) -> bytes:
    """Return silent mono PCM s16le bytes for cheap tests/placeholders."""
    if duration_ms < 0:
        raise ValueError("duration_ms must be non-negative")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    sample_count = int(sample_rate * (duration_ms / 1000.0))
    return b"\x00\x00" * sample_count

"""Bounded, safe parser for .s2voice binary voice profiles.

Implements the upstream binary format specified in s2_voice.cpp:
  - magic:  "S2VOICE\0" (8 bytes)
  - version: uint32 (currently 1)
  - num_codebooks: int32
  - T_prompt:      int32
  - sample_rate:   int32
  - codebook_size: int32
  - transcript_len: uint64  (including null terminator)
  - codes_size:     uint64  (byte length of codes array)
  - transcript:     null-terminated UTF-8 string
  - codes:          raw int32_t code bytes

All parsing is bounded — length fields are validated against maximums to
prevent memory exhaustion.  Native little-endian (same as upstream C++).
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Constants matching upstream s2_voice.cpp
# ---------------------------------------------------------------------------

_S2VOICE_MAGIC = b"S2VOICE\x00"
_S2VOICE_VERSION: int = 1
_HEADER_SIZE: int = 44  # 8 (magic) + 4*5 (int32s) + 8*2 (uint64s)

# Safety bounds — prevent memory exhaustion from malformed files.
_MAX_TRANSCRIPT_LEN: int = 1 * 1024 * 1024   # 1 MB transcript
_MAX_CODES_SIZE: int = 100 * 1024 * 1024     # 100 MB codes
_MAX_FILE_SIZE: int = _HEADER_SIZE + _MAX_TRANSCRIPT_LEN + _MAX_CODES_SIZE


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class VoiceProfileError(ValueError):
    """Raised when a .s2voice file is invalid or malformed."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=False)
class S2VoiceProfile:
    """Parsed representation of a .s2voice binary voice profile.

    Attributes:
        transcript: Null-terminated transcript string (stripped of null).
        codes: List of int32 code values.
        num_codebooks: Number of codebooks (compatibility contract).
        T_prompt: Prompt length parameter (NOT in compatibility contract).
        sample_rate: Audio sample rate in Hz (compatibility contract).
        codebook_size: Codebook size (compatibility contract).

    Compatibility Contract (matching upstream ``is_compatible``):
        Only ``num_codebooks``, ``codebook_size``, and ``sample_rate``
        are checked.  ``T_prompt`` is explicitly excluded.
    """

    transcript: str = ""
    codes: list[int] = field(default_factory=list)
    num_codebooks: int = 0
    T_prompt: int = 0
    sample_rate: int = 44100
    codebook_size: int = 4096

    def is_compatible(
        self,
        num_codebooks: int,
        codebook_size: int,
        sample_rate: int,
    ) -> bool:
        """Check whether this profile is compatible with the given model parameters.

        Per the upstream C++ contract, only ``num_codebooks``,
        ``codebook_size``, and ``sample_rate`` are checked.
        ``T_prompt`` is intentionally excluded.
        """
        return (
            self.num_codebooks == num_codebooks
            and self.codebook_size == codebook_size
            and self.sample_rate == sample_rate
        )


# ---------------------------------------------------------------------------
# Bounded binary parser
# ---------------------------------------------------------------------------

def parse_s2voice(data: bytes) -> S2VoiceProfile:
    """Parse a .s2voice binary blob with strict bounds checking.

    Args:
        data: Raw bytes of the .s2voice file.

    Returns:
        A fully-populated ``S2VoiceProfile``.

    Raises:
        VoiceProfileError: If the data is malformed, truncated,
            contains oversized length fields, or has trailing garbage.
    """
    if len(data) < _HEADER_SIZE:
        raise VoiceProfileError(
            f"Truncated header: expected at least {_HEADER_SIZE} bytes, "
            f"got {len(data)}"
        )

    # --- Parse fixed header (44 bytes) ---
    (
        magic,
        version,
        num_codebooks,
        T_prompt,
        sample_rate,
        codebook_size,
        transcript_len,
        codes_size,
    ) = struct.unpack_from("<8sIiiiiQQ", data, 0)

    # --- Magic ---
    if magic != _S2VOICE_MAGIC:
        raise VoiceProfileError(
            f"Invalid magic: expected {_S2VOICE_MAGIC!r}, got {magic!r}"
        )

    # --- Version ---
    if version != _S2VOICE_VERSION:
        raise VoiceProfileError(
            f"Unsupported version: {version} (expected {_S2VOICE_VERSION})"
        )

    # --- Signed-field validity: reject negative values ---
    if num_codebooks < 0:
        raise VoiceProfileError(
            f"num_codebooks is negative: {num_codebooks}"
        )
    if sample_rate < 0:
        raise VoiceProfileError(
            f"sample_rate is negative: {sample_rate}"
        )
    if codebook_size < 0:
        raise VoiceProfileError(
            f"codebook_size is negative: {codebook_size}"
        )

    # --- Bounds: transcript length ---
    if transcript_len == 0:
        raise VoiceProfileError("Transcript length is zero")
    if transcript_len > _MAX_TRANSCRIPT_LEN:
        raise VoiceProfileError(
            f"Transcript length {transcript_len} exceeds maximum "
            f"{_MAX_TRANSCRIPT_LEN}"
        )

    # --- Bounds: codes size ---
    if codes_size > _MAX_CODES_SIZE:
        raise VoiceProfileError(
            f"Codes size {codes_size} exceeds maximum {_MAX_CODES_SIZE}"
        )

    # --- Check total expected size against max ---
    expected_size = _HEADER_SIZE + transcript_len + codes_size
    if expected_size > _MAX_FILE_SIZE:
        raise VoiceProfileError(
            f"Total file size {expected_size} exceeds maximum {_MAX_FILE_SIZE}"
        )
    if len(data) > expected_size:
        raise VoiceProfileError(
            f"Trailing data: expected {expected_size} bytes, got {len(data)}"
        )
    if len(data) < expected_size:
        raise VoiceProfileError(
            f"Truncated data: expected {expected_size} bytes, got {len(data)}"
        )

    # --- Extract transcript ---
    transcript_start = _HEADER_SIZE
    transcript_end = transcript_start + transcript_len
    transcript_bytes = data[transcript_start:transcript_end]

    if transcript_bytes[-1] != 0:
        raise VoiceProfileError("Transcript is not null-terminated")

    # Decode as UTF-8, strip the null terminator
    try:
        transcript_str = transcript_bytes[:-1].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VoiceProfileError(
            f"Transcript contains invalid UTF-8: {exc}"
        ) from exc

    # --- Extract codes ---
    codes_start = transcript_end
    codes_end = codes_start + codes_size
    codes_bytes = data[codes_start:codes_end]

    n_codes = codes_size // 4  # sizeof(int32_t) == 4
    if codes_size % 4 != 0:
        raise VoiceProfileError(
            f"Codes size {codes_size} is not a multiple of 4 (sizeof int32)"
        )

    codes = list(struct.unpack_from(f"<{n_codes}i", codes_bytes, 0)) if n_codes > 0 else []

    return S2VoiceProfile(
        transcript=transcript_str,
        codes=codes,
        num_codebooks=num_codebooks,
        T_prompt=T_prompt,
        sample_rate=sample_rate,
        codebook_size=codebook_size,
    )


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def compute_voice_hash(data: bytes) -> str:
    """Compute the SHA-256 hex digest of a .s2voice binary blob.

    Returns:
        64-character lowercase hex string.
    """
    return hashlib.sha256(data).hexdigest()


def verify_voice_hash(data: bytes, expected_hash: str) -> None:
    """Verify that *data* matches *expected_hash*.

    Raises:
        VoiceProfileError: If the hash does not match.
    """
    actual = compute_voice_hash(data)
    if actual != expected_hash:
        raise VoiceProfileError(
            f"Hash mismatch: expected {expected_hash[:16]}..., "
            f"got {actual[:16]}..."
        )


# ---------------------------------------------------------------------------
# Manifest generation
# ---------------------------------------------------------------------------

def generate_manifest(
    data: bytes,
    profile: S2VoiceProfile,
    voice_id: str,
    sidecar: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate a manifest dict summarising a voice profile.

    The manifest includes hash, format version, compatibility parameters,
    transcript metadata, and provenance data from the optional sidecar.

    Args:
        data: Raw .s2voice bytes.
        profile: Parsed S2VoiceProfile.
        voice_id: The profile's ID.
        sidecar: Optional sidecar dict (validated against schema).

    Returns:
        A dict with keys: id, format_version, hash_sha256,
        num_codebooks, codebook_size, sample_rate, T_prompt,
        transcript_length, codes_byte_length, codes_count,
        plus license/attribution/provenance if sidecar is provided.
    """
    manifest: dict[str, Any] = {
        "id": voice_id,
        "format_version": _S2VOICE_VERSION,
        "hash_sha256": compute_voice_hash(data),
        "num_codebooks": profile.num_codebooks,
        "codebook_size": profile.codebook_size,
        "sample_rate": profile.sample_rate,
        "T_prompt": profile.T_prompt,
        "transcript_length": len(profile.transcript),
        "codes_byte_length": len(profile.codes) * 4,
        "codes_count": len(profile.codes),
    }

    if sidecar:
        manifest["license"] = sidecar.get("license", "")
        manifest["attribution"] = sidecar.get("attribution", "")
        manifest["provenance"] = sidecar.get("provenance", {})
        for extra in ("description", "language", "gender", "tags", "notes"):
            if extra in sidecar:
                manifest[extra] = sidecar[extra]

    return manifest

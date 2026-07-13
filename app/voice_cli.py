"""CLI commands for voice profile management: validate, import, audit, licenses.

All commands operate locally — no network, no URL downloader, no production
hooks.  They are designed for operator-side management of .s2voice profiles
and their JSON sidecars.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .voice_profile import (
    VoiceProfileError,
    compute_voice_hash,
    generate_manifest,
    parse_s2voice,
)
from .voice_schema import VOICE_SIDECAR_SCHEMA

# Re-use the same ID pattern as voice_discovery for consistency.
_VALID_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

# Import: refuse to import if target already exists (collision guard).
# Override with force=True.

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_symlink(path: str | Path) -> bool:
    """Check if *path* is a symlink. Works on POSIX."""
    return Path(path).is_symlink()


def _safe_voice_id(voice_id: str) -> bool:
    """Return True if *voice_id* matches the safe naming convention."""
    return bool(_VALID_ID_RE.match(voice_id))


def _read_sidecar_if_exists(binary_path: str | Path) -> dict[str, Any] | None:
    """Read and parse a JSON sidecar if it exists next to the binary file.

    Returns:
        Parsed sidecar dict, or None if no sidecar file exists.
    """
    sidecar_path = Path(str(binary_path) + ".json")
    if not sidecar_path.is_file() or _is_symlink(sidecar_path):
        return None
    try:
        raw = sidecar_path.read_text(encoding="utf-8")
        return json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return None


def _validate_sidecar_data(
    sidecar: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate sidecar dict against the JSON Schema.

    Returns:
        Dict with 'valid' (bool) and 'errors' (list[str]) keys.
    """
    if sidecar is None:
        return {"valid": False, "errors": ["No sidecar file found"]}

    import jsonschema

    schema = json.loads(VOICE_SIDECAR_SCHEMA)
    errors: list[str] = []

    try:
        jsonschema.validate(instance=sidecar, schema=schema)
    except jsonschema.ValidationError as exc:
        return {"valid": False, "errors": [str(exc)]}

    # Extra checks beyond schema
    if not sidecar.get("license", "").strip():
        errors.append("License field is empty")
    if not sidecar.get("attribution", "").strip():
        errors.append("Attribution field is empty")

    return {"valid": len(errors) == 0, "errors": errors}


# ---------------------------------------------------------------------------
# CLI: validate
# ---------------------------------------------------------------------------

def cmd_validate(path: str) -> dict[str, Any]:
    """Validate a single .s2voice file (and its optional sidecar).

    Args:
        path: Absolute or relative path to a .s2voice file.

    Returns:
        Dict with keys: valid, hash_sha256, error (if invalid),
        sidecar (if present), sidecar_errors (if sidecar issues).
    """
    result: dict[str, Any] = {"valid": False}
    file_path = Path(path)

    if not file_path.is_file():
        result["error"] = f"File not found: {path}"
        return result

    try:
        data = file_path.read_bytes()
    except OSError as exc:
        result["error"] = f"Cannot read file: {exc}"
        return result

    try:
        profile = parse_s2voice(data)
    except VoiceProfileError as exc:
        result["error"] = str(exc)
        return result

    hash_hex = compute_voice_hash(data)
    result["valid"] = True
    result["hash_sha256"] = hash_hex
    result["format_version"] = 1
    result["num_codebooks"] = profile.num_codebooks
    result["sample_rate"] = profile.sample_rate
    result["codebook_size"] = profile.codebook_size
    result["transcript_length"] = len(profile.transcript)

    # Sidecar
    sidecar = _read_sidecar_if_exists(path)
    if sidecar is not None:
        result["sidecar"] = sidecar
        sc_result = _validate_sidecar_data(sidecar)
        if not sc_result["valid"]:
            result["sidecar_errors"] = sc_result["errors"]

    return result


# ---------------------------------------------------------------------------
# CLI: import
# ---------------------------------------------------------------------------

def cmd_import(
    source_path: str,
    dest_dir: str,
    voice_id: str,
    force: bool = False,
) -> dict[str, Any]:
    """Import a .s2voice file into a managed voice directory.

    The import is atomic on the same filesystem:
    1. Parse and validate the source file.
    2. Write to a temp file in *dest_dir*.
    3. Atomically rename the temp file to the final name.
    4. Copy the sidecar if present.
    5. Clean up temp file on any failure.

    Args:
        source_path: Path to the source .s2voice file.
        dest_dir: Target directory for imported profiles.
        voice_id: Desired profile ID (must pass safety check).
        force: If True, overwrite an existing file at the destination.

    Returns:
        Dict with keys: imported (bool), voice_id, error (if failed).
    """
    result: dict[str, Any] = {"imported": False}

    # --- Validate voice_id safety ---
    if not _safe_voice_id(voice_id):
        result["error"] = f"Unsafe voice ID: {voice_id!r}"
        return result

    dest_path = Path(dest_dir) / f"{voice_id}.s2voice"

    # --- Collision check ---
    if dest_path.is_symlink() or dest_path.exists():
        if dest_path.is_symlink():
            result["error"] = f"Destination exists as symlink: {dest_path}"
            return result
        if not force:
            result["error"] = (
                f"Collision: {dest_path} already exists. Use force=True to overwrite."
            )
            return result

    # --- Read and validate source ---
    try:
        data = Path(source_path).read_bytes()
        parse_s2voice(data)  # Validate before writing
    except VoiceProfileError as exc:
        result["error"] = f"Source file is not a valid .s2voice: {exc}"
        return result
    except OSError as exc:
        result["error"] = f"Cannot read source: {exc}"
        return result

    # --- Atomic write via temp file (same filesystem) ---
    dest_dir_path = Path(dest_dir)
    dest_dir_path.mkdir(parents=True, exist_ok=True)

    tmp_fd = -1
    tmp_path = None
    try:
        # Create temp file in same directory for atomic rename
        tmp_fd, tmp_path_str = tempfile.mkstemp(
            dir=str(dest_dir_path),
            prefix=".tmp-import-",
            suffix=".s2voice",
        )
        tmp_path = Path(tmp_path_str)
        os.close(tmp_fd)

        # Write data to temp file with fsync for durability
        with open(tmp_path_str, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

        # Atomic rename
        os.replace(str(tmp_path), str(dest_path))

        result["imported"] = True
        result["voice_id"] = voice_id
        result["path"] = str(dest_path)

        # --- Copy sidecar atomically if present ---
        source_sidecar = Path(str(source_path) + ".json")
        if source_sidecar.is_file():
            dest_sidecar = Path(str(dest_path) + ".json")
            try:
                sidecar_data = source_sidecar.read_bytes()
                # Write sidecar via temp file in dest dir for atomicity
                sc_tmp_fd, sc_tmp_path = tempfile.mkstemp(
                    dir=str(dest_dir_path),
                    prefix=".tmp-sidecar-",
                    suffix=".json",
                )
                try:
                    with os.fdopen(sc_tmp_fd, "wb") as sc_f:
                        sc_f.write(sidecar_data)
                        sc_f.flush()
                        os.fsync(sc_f.fileno())
                    os.replace(sc_tmp_path, str(dest_sidecar))
                except OSError:
                    # Clean up temp sidecar on failure
                    try:
                        Path(sc_tmp_path).unlink()
                    except OSError:
                        pass
            except OSError:
                pass  # Best-effort, sidecar is optional

    except OSError as exc:
        result["error"] = f"Import failed: {exc}"
        # Clean up temp file
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    return result


# ---------------------------------------------------------------------------
# CLI: audit
# ---------------------------------------------------------------------------

def cmd_audit(voice_dir: str) -> dict[str, Any]:
    """Audit all .s2voice profiles in a directory, reporting
    validity, metadata, licenses, and issues.

    Args:
        voice_dir: Path to a directory containing .s2voice files.

    Returns:
        Dict with keys: total_voices, voices (list of per-voice dicts).
    """
    result: dict[str, Any] = {"total_voices": 0, "voices": []}
    dir_path = Path(voice_dir)

    if not dir_path.is_dir():
        return result

    try:
        entries = sorted(
            [e for e in dir_path.iterdir() if e.is_file() and not e.is_symlink()],
            key=lambda e: e.name,
        )
    except OSError:
        return result

    for entry in entries:
        if not entry.name.endswith(".s2voice"):
            continue

        voice_id = entry.name[: -len(".s2voice")]
        if not voice_id:
            continue

        voice_info: dict[str, Any] = {
            "id": voice_id,
            "path": str(entry),
        }

        try:
            data = entry.read_bytes()
            profile = parse_s2voice(data)
            voice_info["valid"] = True
            voice_info["hash_sha256"] = compute_voice_hash(data)
            voice_info["num_codebooks"] = profile.num_codebooks
            voice_info["sample_rate"] = profile.sample_rate
            voice_info["codebook_size"] = profile.codebook_size
            voice_info["transcript_length"] = len(profile.transcript)
        except (VoiceProfileError, OSError) as exc:
            voice_info["valid"] = False
            voice_info["error"] = str(exc)

        # Sidecar / managed status
        sidecar = _read_sidecar_if_exists(str(entry))
        if sidecar is not None:
            voice_info["managed"] = True
            voice_info["license"] = sidecar.get("license", "")
            voice_info["attribution"] = sidecar.get("attribution", "")
            sc_validation = _validate_sidecar_data(sidecar)
            if sc_validation["errors"]:
                voice_info.setdefault("issues", []).extend(sc_validation["errors"])
        else:
            voice_info["managed"] = False

        # Check for missing rights
        issues: list[str] = voice_info.get("issues", [])
        if not voice_info.get("license"):
            issues.append("Missing license information")
        if not voice_info.get("attribution"):
            issues.append("Missing attribution information")
        if issues:
            voice_info["issues"] = issues

        result["voices"].append(voice_info)

    result["total_voices"] = len(result["voices"])
    return result


# ---------------------------------------------------------------------------
# CLI: licenses
# ---------------------------------------------------------------------------

def cmd_licenses(voice_dir: str) -> dict[str, Any]:
    """Summarise licenses across all voice profiles in a directory.

    Args:
        voice_dir: Path to the voice profiles directory.

    Returns:
        Dict with keys: total_voices, unlicensed_count, licenses
        (dict of license_name -> {count, voices}).
    """
    result: dict[str, Any] = {
        "total_voices": 0,
        "unlicensed_count": 0,
        "licenses": {},
    }

    audit_result = cmd_audit(voice_dir)
    result["total_voices"] = audit_result["total_voices"]

    for voice in audit_result.get("voices", []):
        lic = voice.get("license", "")
        if not lic:
            result["unlicensed_count"] += 1
            continue

        if lic not in result["licenses"]:
            result["licenses"][lic] = {"count": 0, "voices": []}
        result["licenses"][lic]["count"] += 1
        result["licenses"][lic]["voices"].append(voice["id"])

    return result

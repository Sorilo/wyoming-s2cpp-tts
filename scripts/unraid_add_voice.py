#!/usr/bin/env python3
"""Unraid host voice-import operator — safe, daemon-free, repository-owned.

Orchestrates the full lifecycle on an Unraid host:
  PREFLIGHT → PLAN → LOCKED → BACKEND_STOPPING → BACKEND_STOPPED
  → IMPORT_RUNNING → IMPORT_VALIDATING → BACKEND_STARTING
  → BACKEND_HEALTH_CHECK → COMPLETE

All Docker operations use argument arrays.  Fully testable without
a real Docker daemon.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Ensure app package is importable for post-import validation
_here = Path(__file__).resolve().parent.parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

_SIGNAL_RECEIVED: threading.Event = threading.Event()


def _signal_cleanup() -> None:
    """Signal handler: set the event and request cleanup."""
    _SIGNAL_RECEIVED.set()


_signal_installed: bool = False


def _install_signal_handlers() -> None:
    """Install SIGINT and SIGTERM handlers once."""
    global _signal_installed
    if _signal_installed:
        return
    try:
        signal.signal(signal.SIGINT, lambda s, f: _signal_cleanup())
        signal.signal(signal.SIGTERM, lambda s, f: _signal_cleanup())
    except (ValueError, OSError):
        pass  # Not in main thread or in restricted environments
    _signal_installed = True


# ---------------------------------------------------------------------------
# Configuration keys (strict allowlist)
# ---------------------------------------------------------------------------

_ALLOWED_CONFIG_KEYS: set[str] = {
    "BACKEND_CONTAINER",
    "BACKEND_IMAGE",
    "MODELS_DIR",
    "VOICES_DIR",
    "IMPORT_INPUTS_DIR",
    "MODEL_CONTAINER_PATH",
    "TOKENIZER_CONTAINER_PATH",
    "CUDA_DEVICE",
    "GPU_LAYERS",
    "STOP_TIMEOUT_SEC",
    "IMPORT_TIMEOUT_SEC",
    "RESTART_TIMEOUT_SEC",
    "LOCK_FILE",
    "HEALTH_POLL_INTERVAL_SEC",
    "HEALTH_POLL_TIMEOUT_SEC",
    "EXPECTED_SOURCE_REVISION",
    "EXPECTED_S2CPP_REVISION",
}

_VALID_AUDIO_SUFFIXES: set[str] = {
    ".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".webm",
}

_VOICE_ID_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

# SHA-256 hex digest: exactly 64 lowercase hex characters
_SHA256_HEX_RE: re.Pattern[str] = re.compile(r"^[a-f0-9]{64}$")

# Immutable sha256: digest prefix
_SHA256_DIGEST_RE: re.Pattern[str] = re.compile(r"^sha256:[a-f0-9]{64}$")

# Tag with sha- prefix: registry/repo:sha-<hex> (40 hex for git revision)
_SHA_TAG_RE: re.Pattern[str] = re.compile(
    r"^(?:[a-zA-Z0-9][a-zA-Z0-9_.\-]*(?:/[a-zA-Z0-9][a-zA-Z0-9_.\-]*)*):sha-[a-f0-9]{40}$"
)

_S2CPP_REVISION_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{40}$")

_REPORT_SCHEMA_VERSION: int = 1

_MAX_ERROR_LENGTH: int = 2000

# From app.voice_import
MAX_TRANSCRIPT_BYTES: int = 1024 * 1024


# ---------------------------------------------------------------------------
# Container path resolution (B)
# ---------------------------------------------------------------------------


def _resolve_container_path(container_path: str) -> dict[str, str]:
    """Resolve a container-side path to container-absolute + host-relative.

    Requirements:
      - Must be absolute.
      - Must start with /models/ or be exactly /models.
      - Returns dict with 'container_path' and 'host_relative'.
      - host_relative is the path relative to /models (preserving subdirs).
    """
    if not container_path.startswith("/"):
        raise ConfigError(
            f"Container path must be absolute: {container_path}"
        )
    if not (container_path == "/models"
            or container_path.startswith("/models/")):
        raise ConfigError(
            f"Container path must be under /models: {container_path}"
        )
    # Derive host-relative from /models/ prefix
    if container_path == "/models":
        host_relative = ""
    else:
        host_relative = container_path[len("/models/"):]
    return {
        "container_path": container_path,
        "host_relative": host_relative,
    }


# ---------------------------------------------------------------------------
# Revision validation (C)
# ---------------------------------------------------------------------------


def _validate_revisions(
    expected_source_revision: str,
    expected_s2cpp_revision: str,
) -> None:
    """Validate that both expected revisions are nonempty 40-hex.

    Raises:
        ConfigError: If either revision is empty or invalid format.
    """
    if not expected_source_revision or not expected_source_revision.strip():
        raise ConfigError(
            "EXPECTED_SOURCE_REVISION must be a nonempty 40-character "
            "lowercase hex string"
        )
    if not expected_s2cpp_revision or not expected_s2cpp_revision.strip():
        raise ConfigError(
            "EXPECTED_S2CPP_REVISION must be a nonempty 40-character "
            "lowercase hex string"
        )
    if not _S2CPP_REVISION_RE.fullmatch(expected_source_revision):
        raise ConfigError(
            f"EXPECTED_SOURCE_REVISION must be exactly 40 lowercase hex "
            f"characters, got: {expected_source_revision[:80]}"
        )
    if not _S2CPP_REVISION_RE.fullmatch(expected_s2cpp_revision):
        raise ConfigError(
            f"EXPECTED_S2CPP_REVISION must be exactly 40 lowercase hex "
            f"characters, got: {expected_s2cpp_revision[:80]}"
        )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OperatorError(Exception):
    """Base exception for the host operator."""


class ConfigError(OperatorError):
    """Configuration parsing or validation error."""


class PreflightError(OperatorError):
    """Preflight validation failure."""


class BackendStateError(OperatorError):
    """Unsafe or unexpected backend state."""


class LockError(OperatorError):
    """Lock acquisition failure (concurrent import)."""


class IdentityError(OperatorError):
    """Image/revision identity mismatch — fatal."""


# ---------------------------------------------------------------------------
# Config parsing (strict dotenv-style)
# ---------------------------------------------------------------------------


def parse_config(text: str) -> dict[str, str]:
    """Parse a strict dotenv-style configuration string.

    Only known keys are accepted.  Unknown keys raise ConfigError.
    Duplicate keys raise ConfigError.
    Comments (#) and blank lines are skipped.
    """
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"Invalid config line (no '='): {line[:80]}")
        key, _, raw_value = line.partition("=")
        key = key.strip()
        if key not in _ALLOWED_CONFIG_KEYS:
            raise ConfigError(
                f"Unknown config key: {key} — allowed: "
                f"{', '.join(sorted(_ALLOWED_CONFIG_KEYS))}"
            )
        if key in result:
            raise ConfigError(
                f"Duplicate config key: {key} — each key must appear exactly once"
            )
        value = raw_value.strip()
        # Strip optional quoting
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key] = value
    return result


def merge_config_with_args(
    config: dict[str, str], args: argparse.Namespace
) -> dict[str, str]:
    """Merge parsed config values with CLI args. CLI args take precedence."""
    merged: dict[str, str] = dict(config)
    for key in _ALLOWED_CONFIG_KEYS:
        attr = key.lower()
        if hasattr(args, attr):
            val = getattr(args, attr)
            if val is not None:
                merged[key] = str(val)
    return merged


# ---------------------------------------------------------------------------
# Voice ID validation (delegates to app.voice_import)
# ---------------------------------------------------------------------------


def validate_voice_id_outer(voice_id: str) -> str:
    """Validate voice ID using the same contract as app.voice_import."""
    if not isinstance(voice_id, str) or not _VOICE_ID_RE.fullmatch(voice_id):
        raise ValueError(
            "Invalid voice ID: use 1-128 ASCII letters, digits, underscore, "
            "or hyphen; the first character must be alphanumeric"
        )
    return voice_id


# ---------------------------------------------------------------------------
# Image reference validation
# ---------------------------------------------------------------------------


def validate_image_reference(image_ref: str) -> str:
    """Require immutable image references.

    Accepted forms:
      - sha256:<exactly-64-lowercase-hex>
      - [registry/]repo:sha-<40-lowercase-hex>

    Rejected: latest, edge, sha-local, unpinned/floating tags.
    """
    if not image_ref:
        raise ValueError("Image reference must not be empty")
    if _SHA256_DIGEST_RE.fullmatch(image_ref):
        return image_ref
    if _SHA_TAG_RE.fullmatch(image_ref):
        return image_ref
    raise ValueError(
        f"Image reference must be immutable: "
        f"sha256:<64-hex-digest> or registry/repo:sha-<40-hex-git-revision>. "
        f"Got: {image_ref}"
    )


# ---------------------------------------------------------------------------
# Preflight validation
# ---------------------------------------------------------------------------


def preflight_validate(
    *,
    audio_path: Path,
    transcript_path: Path,
    voice_id: str,
    license_str: str,
    attribution: str,
    provenance: str,
    models_dir: Path,
    voices_dir: Path,
    import_inputs_dir: Path,
    model_rel: str,
    tokenizer_rel: str,
    force: bool = False,
    validation_wav_rel: str | None = None,
) -> dict[str, Any]:
    """Validate all inputs before any Docker state change.

    Returns a dict of resolved paths for use by downstream steps.
    """
    errors: list[str] = []

    # Voice ID
    try:
        validate_voice_id_outer(voice_id)
    except ValueError as exc:
        errors.append(str(exc))

    # Metadata
    for label, value in (
        ("license", license_str),
        ("attribution", attribution),
        ("provenance", provenance),
    ):
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{label} must not be empty")

    # --- Path safety: reject newline/NUL in path strings ---
    for label, p in (
        ("audio_path", str(audio_path)),
        ("transcript_path", str(transcript_path)),
        ("models_dir", str(models_dir)),
        ("voices_dir", str(voices_dir)),
        ("import_inputs_dir", str(import_inputs_dir)),
        ("model_rel", model_rel),
        ("tokenizer_rel", tokenizer_rel),
    ):
        if "\n" in p or "\0" in p:
            errors.append(f"{label} contains newline or NUL characters")

    if validation_wav_rel is not None:
        if "\n" in validation_wav_rel or "\0" in validation_wav_rel:
            errors.append("validation_wav_rel contains newline or NUL characters")

    # Audio file
    _check_path(audio_path, "audio", errors, import_inputs_dir)

    # Audio extension
    if audio_path.suffix.lower() not in _VALID_AUDIO_SUFFIXES:
        errors.append(
            f"Unsupported audio extension: {audio_path.suffix}; "
            f"use one of: {', '.join(sorted(_VALID_AUDIO_SUFFIXES))}"
        )

    # Transcript file
    _check_path(transcript_path, "transcript", errors, import_inputs_dir)
    if transcript_path.exists() and not transcript_path.is_symlink() and transcript_path.is_file():
        try:
            content = transcript_path.read_text(encoding="utf-8").strip()
            if not content:
                errors.append("Transcript file is empty")
            # Check transcript size against importer limit
            st_size = transcript_path.stat().st_size
            if st_size > MAX_TRANSCRIPT_BYTES:
                errors.append(
                    f"Transcript file size ({st_size} bytes) exceeds "
                    f"importer maximum ({MAX_TRANSCRIPT_BYTES} bytes)"
                )
        except UnicodeError:
            errors.append("Transcript file is not valid UTF-8")

    # Input containment (resolve to catch symlink traversal)
    for label, path in (
        ("audio", audio_path),
        ("transcript", transcript_path),
    ):
        if path.exists():
            try:
                resolved = path.resolve(strict=False)
                resolved.relative_to(import_inputs_dir.resolve(strict=True))
                # Also check the unresolved path has no lexical symlink components
                _check_no_traversal(str(path), label, errors)
            except ValueError:
                errors.append(f"{label} path is outside import-input directory")

    # Directories
    for label, dir_path in (
        ("models", models_dir),
        ("voices", voices_dir),
        ("import-inputs", import_inputs_dir),
    ):
        if not dir_path.is_dir():
            errors.append(f"{label} directory does not exist: {dir_path}")

    # Model and tokenizer — must be within models_dir
    model_candidate = models_dir / model_rel
    tokenizer_candidate = models_dir / tokenizer_rel
    model_path = model_candidate.resolve()
    tokenizer_path = tokenizer_candidate.resolve()
    models_root = models_dir.resolve(strict=True)
    for label, candidate, path, rel in (
        ("model", model_candidate, model_path, model_rel),
        ("tokenizer", tokenizer_candidate, tokenizer_path, tokenizer_rel),
    ):
        try:
            path.relative_to(models_root)
        except ValueError:
            errors.append(f"{label} path is outside models directory: {path}")
            continue
        if _has_symlink_component(models_dir, rel):
            errors.append(f"{label} must not contain a symlink: {candidate}")
        elif not path.is_file():
            errors.append(f"{label} not found: {path}")

    # Lexical symlink components in model_rel / tokenizer_rel
    for label, rel in (("model_rel", model_rel), ("tokenizer_rel", tokenizer_rel)):
        if ".." in Path(rel).parts:
            errors.append(f"{label} contains parent-directory traversal: {rel}")

    # Validation WAV path validation
    if validation_wav_rel is not None:
        wav_path = Path(validation_wav_rel)
        if wav_path.is_absolute():
            errors.append("validation_wav_rel must be a relative path under voices directory")
        elif ".." in wav_path.parts:
            errors.append("validation_wav_rel contains parent-directory traversal")
        else:
            # Check that the resolved path would be within voices_dir
            resolved_wav = (voices_dir / validation_wav_rel).resolve()
            try:
                resolved_wav.relative_to(voices_dir.resolve(strict=True))
            except ValueError:
                errors.append("validation_wav_rel resolves outside voices directory")

    # Destination collision (unless force)
    profile_path = voices_dir / f"{voice_id}.s2voice"
    sidecar_path = voices_dir / f"{voice_id}.s2voice.json"
    if not force:
        for label, path in (
            ("profile", profile_path),
            ("sidecar", sidecar_path),
        ):
            if path.exists():
                errors.append(
                    f"{label} destination already exists: {path} (use --force to override)"
                )

    if errors:
        raise PreflightError("; ".join(errors))

    return {
        "model_path": model_path,
        "tokenizer_path": tokenizer_path,
        "profile_path": profile_path,
        "sidecar_path": sidecar_path,
    }


def _check_no_traversal(path_str: str, label: str, errors: list[str]) -> None:
    """Check for lexical symlink components (..) in path string."""
    parts = Path(path_str).parts
    if ".." in parts:
        errors.append(f"{label} path contains parent-directory traversal: {path_str}")


def _has_symlink_component(root: Path, relative_path: str) -> bool:
    """Return whether a relative path traverses any symlink below root."""
    relative = Path(relative_path)
    if relative.is_absolute():
        return False
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _check_path(
    path: Path,
    label: str,
    errors: list[str],
    parent: Path | None = None,
) -> None:
    """Validate a single input path."""
    if not path.exists():
        errors.append(f"{label} file not found: {path}")
        return
    if path.is_symlink():
        errors.append(f"{label} must not be a symlink: {path}")
        return
    if not path.is_file():
        errors.append(f"{label} is not a regular file: {path}")


# ---------------------------------------------------------------------------
# Docker backend state detection
# ---------------------------------------------------------------------------

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _docker_runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Default Docker runner using subprocess.run with argument arrays."""
    return subprocess.run(args, capture_output=True, text=True, **kwargs)


# Known safe backend states (only these proceed)
_SAFE_BACKEND_STATES: set[str] = {"running", "exited"}
# State mapping
_STATE_MAP: dict[str, str] = {
    "running": "running",
    "exited": "stopped",
    "paused": "paused",
    "restarting": "restarting",
    "dead": "dead",
    "created": "created",
    "removing": "removing",
}


def detect_backend_state(
    container_name: str,
    *,
    runner: Runner = _docker_runner,
) -> str:
    """Detect the state of the configured backend container.

    Returns the canonical state string. Only 'running' and 'stopped'
    (exited) are considered safe to proceed.
    """
    result = runner(
        ["docker", "inspect", container_name],
        timeout=30,
    )
    if result.returncode != 0:
        return "missing"

    try:
        data = json.loads(result.stdout)
        if len(data) != 1:
            return "missing"
        container = data[0]
        state = container.get("State", {})
        status = state.get("Status", "")

        mapped = _STATE_MAP.get(status, status)
        return mapped
    except (json.JSONDecodeError, IndexError, KeyError):
        return "missing"


def is_safe_backend_state(state: str) -> bool:
    """Return True if the backend state is safe to proceed."""
    return state in _SAFE_BACKEND_STATES or state == "stopped"  # "stopped" is mapped from "exited"


def inspect_container_image(
    container_name: str,
    *,
    runner: Runner = _docker_runner,
) -> dict[str, Any] | None:
    """Inspect the backend container and return parsed JSON, or None."""
    result = runner(
        ["docker", "inspect", container_name],
        timeout=30,
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
        return data[0]
    except (json.JSONDecodeError, IndexError):
        return None


def inspect_image(
    image_ref: str,
    *,
    runner: Runner = _docker_runner,
) -> dict[str, Any] | None:
    """Inspect an image and return parsed JSON, or None."""
    result = runner(
        ["docker", "image", "inspect", image_ref],
        timeout=30,
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
        return data[0]
    except (json.JSONDecodeError, IndexError):
        return None


def verify_image_identity(
    expected_image_ref: str,
    expected_source_revision: str,
    expected_s2cpp_revision: str,
    *,
    runner: Runner = _docker_runner,
    container_name: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Verify that the configured image matches expected identity.

    Returns (success, identity_report) where identity_report contains
    resolved image ID, digest, revisions, and any mismatches.

    Checks:
      1. Image ref is immutable
      2. If container_name given: container Config.Image == configured ref
      3. If container_name given: container .Image == inspected image Id
      4. Image OCI label org.opencontainers.image.revision == expected_source_revision
      5. Image OCI label wyoming-s2cpp-tts.s2cpp-revision == expected_s2cpp_revision
      6. Container Env S2CPP_REVISION == expected_s2cpp_revision (if container given)
    """
    report: dict[str, Any] = {
        "configured_image": expected_image_ref,
        "resolved_image_id": None,
        "resolved_digest": None,
        "source_revision_match": None,
        "s2cpp_revision_match": None,
        "checks_passed": False,
    }

    # 1. Validate immutability
    try:
        validate_image_reference(expected_image_ref)
    except ValueError as exc:
        report["error"] = str(exc)
        return False, report

    # 2. Inspect image
    image_data = inspect_image(expected_image_ref, runner=runner)
    if image_data is None:
        report["error"] = f"Could not inspect image: {expected_image_ref}"
        return False, report

    image_id = image_data.get("Id", "")
    report["resolved_image_id"] = image_id

    # RepoDigests
    repo_digests = image_data.get("RepoDigests", [])
    if repo_digests:
        report["resolved_digest"] = repo_digests[0]
        # If configured ref is sha256:..., check it appears in image ID
        if expected_image_ref.startswith("sha256:"):
            digest_part = expected_image_ref[len("sha256:"):]
            if digest_part not in image_id:
                report["error"] = (
                    f"Image ID mismatch: expected digest containing {digest_part[:16]}..., "
                    f"got {image_id[:32]}..."
                )
                return False, report

    # 3. Container identity checks (if container given)
    if container_name:
        container_data = inspect_container_image(container_name, runner=runner)
        if container_data is None:
            report["error"] = f"Could not inspect container: {container_name}"
            return False, report

        config_image = container_data.get("Config", {}).get("Image", "")
        container_image_id = container_data.get("Image", "")
        report["container_config_image"] = config_image
        report["container_image_id"] = container_image_id

        # Container Config.Image should match configured immutable ref OR the resolved image
        # For sha256: refs, the Config.Image and .Image should contain the digest
        if expected_image_ref.startswith("sha256:"):
            digest_part = expected_image_ref[len("sha256:"):]
            if digest_part not in config_image and digest_part not in image_id:
                report["error"] = (
                    f"Container image mismatch: configured {expected_image_ref[:32]}..., "
                    f"container Config.Image={config_image[:32]}..."
                )
                return False, report

        # Container .Image should match inspected image Id
        if container_image_id != image_id:
            report["error"] = (
                f"Container Image ID mismatch: container={container_image_id[:16]}..., "
                f"inspected image={image_id[:16]}..."
            )
            return False, report

        # Container Env S2CPP_REVISION check
        env_list = container_data.get("Config", {}).get("Env", [])
        env_s2cpp_revision = ""
        for env_var in env_list:
            if env_var.startswith("S2CPP_REVISION="):
                env_s2cpp_revision = env_var.split("=", 1)[1]
                break
        report["container_s2cpp_revision"] = env_s2cpp_revision
        if expected_s2cpp_revision and env_s2cpp_revision != expected_s2cpp_revision:
            report["error"] = (
                f"Container S2CPP_REVISION mismatch: expected {expected_s2cpp_revision[:16]}..., "
                f"got {env_s2cpp_revision[:16] if env_s2cpp_revision else '(missing)'}..."
            )
            return False, report

    # 4 & 5. OCI label checks
    labels = image_data.get("Config", {}).get("Labels", {})
    report["oci_labels"] = {
        k: v for k, v in labels.items()
        if k in (
            "org.opencontainers.image.revision",
            "wyoming-s2cpp-tts.s2cpp-revision",
        )
    }

    oci_source_revision = labels.get("org.opencontainers.image.revision", "")
    oci_s2cpp_revision = labels.get("wyoming-s2cpp-tts.s2cpp-revision", "")

    report["source_revision_match"] = oci_source_revision == expected_source_revision
    report["s2cpp_revision_match"] = oci_s2cpp_revision == expected_s2cpp_revision

    if expected_source_revision:
        if not oci_source_revision:
            report["error"] = (
                "Image missing org.opencontainers.image.revision label"
            )
            return False, report
        if oci_source_revision != expected_source_revision:
            report["error"] = (
                f"Source revision mismatch: expected {expected_source_revision[:16]}..., "
                f"got {oci_source_revision[:16]}..."
            )
            return False, report

    if expected_s2cpp_revision:
        if not oci_s2cpp_revision:
            report["error"] = (
                "Image missing wyoming-s2cpp-tts.s2cpp-revision label"
            )
            return False, report
        if oci_s2cpp_revision != expected_s2cpp_revision:
            report["error"] = (
                f"S2CPP revision mismatch: expected {expected_s2cpp_revision[:16]}..., "
                f"got {oci_s2cpp_revision[:16]}..."
            )
            return False, report

    report["checks_passed"] = True
    return True, report


# ---------------------------------------------------------------------------
# Docker command generation
# ---------------------------------------------------------------------------


def build_importer_command(
    *,
    image: str,
    models_dir_host: str,
    voices_dir_host: str,
    import_inputs_dir_host: str,
    model_container_path: str,
    tokenizer_container_path: str,
    audio_relative: str,
    transcript_relative: str,
    voice_id: str,
    license_str: str,
    attribution: str,
    provenance: str,
    cuda_device: int = 0,
    gpu_layers: int = -1,
    validation_wav_rel: str | None = None,
    importer_timeout: int = 600,
    force: bool = False,
) -> list[str]:
    """Build the docker run command as an argument array.

    Security properties:
      - --network none
      - --rm
      - Exact three mounts (models:ro, inputs:ro, voices:rw)
      - --entrypoint for explicit binary path
      - Image as first command argument (not duplicate binary)
      - --force flag
      - Uses --gpus device=N (no raw /dev/nvidia)
      - Uses relative-to-root paths inside container
      - No Docker socket mount
      - No privilege escalation
      - Uses --transcript-file (no inline transcript)
    """
    cmd: list[str] = [
        "docker",
        "run",
        "--rm",
        "--network", "none",
        "--entrypoint", "/usr/local/bin/import-s2voice",
        "-v", f"{models_dir_host}:/models:ro",
        "-v", f"{import_inputs_dir_host}:/inputs:ro",
        "-v", f"{voices_dir_host}:/voices:rw",
    ]

    # GPU configuration — use --gpus only (no raw /dev/nvidia device)
    if cuda_device >= 0:
        cmd.extend(["--gpus", f"device={cuda_device}"])

    # Image as first argument after docker run options
    cmd.append(image)

    # Importer arguments — use relative paths inside container
    cmd.append(f"/inputs/{audio_relative}")
    cmd.append("--transcript-file")
    cmd.append(f"/inputs/{transcript_relative}")
    cmd.extend(["--id", voice_id])
    cmd.extend(["--license", license_str])
    cmd.extend(["--attribution", attribution])
    cmd.extend(["--provenance-source", provenance])
    cmd.extend(["--model", model_container_path])
    cmd.extend(["--tokenizer", tokenizer_container_path])
    cmd.extend(["--voice-dir", "/voices"])
    cmd.extend(["--cuda-device", str(cuda_device)])
    cmd.extend(["--gpu-layers", str(gpu_layers)])
    cmd.extend(["--timeout", str(importer_timeout)])

    if force:
        cmd.append("--force")

    if validation_wav_rel:
        cmd.extend(["--validation-wav", f"/voices/{validation_wav_rel}"])

    return cmd


# ---------------------------------------------------------------------------
# Lock and runner abstraction for testing
# ---------------------------------------------------------------------------


class _RealFileLock:
    """Real file-based lock using flock-style semantics."""

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Try to acquire the lock. Returns True on success."""
        import fcntl

        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR)
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (IOError, OSError):
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            return False

    def release(self) -> None:
        """Release the lock."""
        if self._fd is not None:
            import fcntl

            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except (IOError, OSError):
                pass
            finally:
                os.close(self._fd)
                self._fd = None


# ---------------------------------------------------------------------------
# Bounded polling helpers
# ---------------------------------------------------------------------------


def poll_until_stopped(
    container_name: str,
    stop_timeout: int,
    poll_interval: float = 1.0,
    *,
    runner: Runner = _docker_runner,
) -> bool:
    """Poll docker inspect until container is exited or timeout.

    Returns True if container stopped, False on timeout.
    """
    deadline = time.monotonic() + stop_timeout
    while time.monotonic() < deadline:
        if _SIGNAL_RECEIVED.is_set():
            return False
        state = detect_backend_state(container_name, runner=runner)
        if state == "stopped":
            return True
        time.sleep(min(poll_interval, deadline - time.monotonic()))
    return False


def poll_until_healthy(
    container_name: str,
    restart_timeout: int,
    poll_interval: float = 2.0,
    *,
    runner: Runner = _docker_runner,
) -> tuple[bool, dict[str, Any]]:
    """Poll docker inspect after restart until container is running and healthy.

    Returns (success, evidence).
    """
    evidence: dict[str, Any] = {"state": "unknown", "health": "unknown"}
    deadline = time.monotonic() + restart_timeout
    while time.monotonic() < deadline:
        if _SIGNAL_RECEIVED.is_set():
            evidence["state"] = "interrupted"
            return False, evidence
        state = detect_backend_state(container_name, runner=runner)
        evidence["state"] = state
        if state == "running":
            # Check Docker health if available
            container = inspect_container_image(container_name, runner=runner)
            if container:
                health = container.get("State", {}).get("Health", {})
                if health is not None and health:
                    health_status = health.get("Status", "")
                    evidence["health"] = health_status
                    if health_status == "healthy":
                        return True, evidence
                    # Non-healthy but health exists: keep polling
                    evidence["evidence"] = health_status
                elif health is None:
                    # No HEALTHCHECK configured — NOT healthy
                    evidence["health"] = None
                    evidence["evidence"] = "running_no_healthcheck"
                    return False, evidence
                else:
                    # Empty health dict — treat as no healthcheck
                    evidence["health"] = None
                    evidence["evidence"] = "running_no_healthcheck"
                    return False, evidence
        time.sleep(min(poll_interval, deadline - time.monotonic()))
    evidence["state"] = detect_backend_state(container_name, runner=runner)
    return False, evidence


# ---------------------------------------------------------------------------
# Interruptible importer runner (subprocess.Popen based)
# ---------------------------------------------------------------------------


# Default poll interval and grace periods for the interruptible runner
_IMPORTER_POLL_INTERVAL: float = 1.0
_IMPORTER_TERMINATE_GRACE: float = 10.0
_IMPORTER_KILL_GRACE: float = 5.0


def _importer_runner(
    args: list[str],
    *,
    timeout: float,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run an importer command with interruptible Popen-based lifecycle.

    Uses ``subprocess.Popen`` with argument arrays, captures stdout/stderr,
    polls boundedly, observes ``_SIGNAL_RECEIVED``, forwards SIGINT (via
    terminate) once on interrupt, waits a bounded grace, then terminates
    and bounded-waits if needed.

    On timeout: terminate bounded, then kill bounded, raise
    ``subprocess.TimeoutExpired``.

    Returns ``CompletedProcess`` or raises ``CalledProcessError`` if
    ``check=True`` and the exit code is non-zero.

    Never uses shell execution, forced docker kill, os dot system, or
    eval or exec.
    """
    popen: subprocess.Popen[str] | None = None
    stdout_buf: str = ""
    stderr_buf: str = ""
    try:
        popen = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + timeout

        # Bounded poll loop
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Timeout — terminate then kill
                _terminate_and_wait(popen, args, timeout)
                raise subprocess.TimeoutExpired(args, timeout)

            # Check signal
            if _SIGNAL_RECEIVED.is_set():
                _terminate_and_wait(popen, args, timeout)
                # After termination, collect output and return
                rc = popen.returncode
                if rc is None:
                    rc = -15  # SIGTERM
                stdout_buf, stderr_buf = popen.communicate(timeout=30)
                result = subprocess.CompletedProcess(
                    args, rc, stdout=stdout_buf, stderr=stderr_buf,
                )
                if check and rc != 0:
                    raise subprocess.CalledProcessError(
                        rc, args, output=stdout_buf, stderr=stderr_buf,
                    )
                return result

            # Poll with bounded sleep
            ret = popen.poll()
            if ret is not None:
                # Process finished normally
                stdout_buf, stderr_buf = popen.communicate(timeout=30)
                result = subprocess.CompletedProcess(
                    args, ret, stdout=stdout_buf, stderr=stderr_buf,
                )
                if check and ret != 0:
                    raise subprocess.CalledProcessError(
                        ret, args, output=stdout_buf, stderr=stderr_buf,
                    )
                return result

            # Sleep for a short interval before next poll
            sleep_time = min(_IMPORTER_POLL_INTERVAL, remaining)
            if sleep_time > 0:
                time.sleep(sleep_time)

    except subprocess.TimeoutExpired:
        # Already handled above, but belt-and-suspenders
        if popen is not None and popen.returncode is None:
            _terminate_and_wait(popen, args, timeout)
        raise
    except Exception:
        if popen is not None and popen.returncode is None:
            try:
                popen.kill()
                popen.wait(timeout=_IMPORTER_KILL_GRACE)
            except Exception:
                pass
        raise


def _terminate_and_wait(
    popen: subprocess.Popen[str],
    args: list[str],
    timeout: float,
) -> None:
    """Terminate popen, wait bounded grace, then kill if still alive."""
    if popen.returncode is not None:
        return
    try:
        popen.terminate()
    except OSError:
        pass
    try:
        popen.wait(timeout=_IMPORTER_TERMINATE_GRACE)
    except subprocess.TimeoutExpired:
        # Still alive after terminate grace — force kill
        try:
            popen.kill()
        except OSError:
            pass
        try:
            popen.wait(timeout=_IMPORTER_KILL_GRACE)
        except subprocess.TimeoutExpired:
            pass  # Best effort


def run_operator(
    *,
    audio_path: Path,
    transcript_path: Path,
    voice_id: str,
    license_str: str,
    attribution: str,
    provenance: str,
    models_dir: Path,
    voices_dir: Path,
    import_inputs_dir: Path,
    model_rel: str,
    tokenizer_rel: str,
    backend_container: str,
    backend_image: str,
    cuda_device: int = 0,
    gpu_layers: int = 99,
    stop_timeout: int = 30,
    import_timeout: int = 600,
    restart_timeout: int = 120,
    health_poll_interval: float = 2.0,
    health_poll_timeout: int = 120,
    expected_source_revision: str = "",
    expected_s2cpp_revision: str = "",
    dry_run: bool = False,
    force: bool = False,
    restart_backend: bool = False,
    validation_wav_rel: str | None = None,
    runner: Runner = _docker_runner,
    lock: Any = None,
    importer_runner: Runner | None = None,
) -> dict[str, Any]:
    """Run the full operator lifecycle.

    Returns a structured dict suitable for JSON serialization.
    """
    # Clear any stale signal from a prior interrupted invocation
    _SIGNAL_RECEIVED.clear()

    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    errors: list[str] = []
    warnings: list[str] = []
    planned_commands: list[list[str]] | None = None
    backend_initial_state: str = "unknown"
    importer_exit_code: int | None = None
    importer_duration_sec: float | None = None
    backend_restarted: bool = False
    backend_final_state: str = "unknown"
    backend_recovery_result: str = "not_attempted"
    identity_report: dict[str, Any] | None = None
    resolved_image_id: str | None = None
    resolved_digest: str | None = None

    _install_signal_handlers()

    # --- REVISION VALIDATION (C): fail before any Docker call ---
    try:
        _validate_revisions(expected_source_revision, expected_s2cpp_revision)
    except ConfigError as exc:
        finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return _build_report(
            status="failed",
            started_at=started_at,
            finished_at=finished_at,
            voice_id=voice_id,
            audio_fname=audio_path.name,
            transcript_fname=transcript_path.name,
            backend_container=backend_container,
            backend_image=backend_image,
            backend_initial_state="unknown",
            backend_final_state="unknown",
            backend_would_restart=False,
            backend_restarted=False,
            backend_recovery_result="not_attempted",
            restart_attempted=False,
            dry_run=dry_run,
            force=force,
            importer_exit_code=None,
            resolved_image_id=None,
            resolved_digest=None,
            identity_report=None,
            errors=[str(exc)],
            warnings=[],
        )

    # --- PREFLIGHT ---
    try:
        validate_image_reference(backend_image)
    except ValueError as exc:
        raise PreflightError(str(exc))

    preflight_result = preflight_validate(
        audio_path=audio_path,
        transcript_path=transcript_path,
        voice_id=voice_id,
        license_str=license_str,
        attribution=attribution,
        provenance=provenance,
        models_dir=models_dir,
        voices_dir=voices_dir,
        import_inputs_dir=import_inputs_dir,
        model_rel=model_rel,
        tokenizer_rel=tokenizer_rel,
        force=force,
        validation_wav_rel=validation_wav_rel,
    )

    # Compute relative paths for container
    try:
        audio_relative = str(audio_path.resolve().relative_to(import_inputs_dir.resolve()))
        transcript_relative = str(transcript_path.resolve().relative_to(import_inputs_dir.resolve()))
    except ValueError as exc:
        raise PreflightError(f"Path not under import-inputs: {exc}")

    # --- PLAN ---
    # Detect backend state even in dry-run for accurate reporting
    backend_initial_state = detect_backend_state(backend_container, runner=runner)

    # Verify image identity (fail-closed)
    identity_ok, identity_report = verify_image_identity(
        backend_image,
        expected_source_revision,
        expected_s2cpp_revision,
        runner=runner,
        container_name=backend_container if backend_initial_state != "missing" else None,
    )
    if identity_report:
        resolved_image_id = identity_report.get("resolved_image_id")
        resolved_digest = identity_report.get("resolved_digest")

    importer_cmd = build_importer_command(
        image=backend_image,
        models_dir_host=str(models_dir),
        voices_dir_host=str(voices_dir),
        import_inputs_dir_host=str(import_inputs_dir),
        model_container_path=(
            preflight_result.get("model_container_path")
            or f"/models/{model_rel}"
        ),
        tokenizer_container_path=(
            preflight_result.get("tokenizer_container_path")
            or f"/models/{tokenizer_rel}"
        ),
        audio_relative=audio_relative,
        transcript_relative=transcript_relative,
        voice_id=voice_id,
        license_str=license_str,
        attribution=attribution,
        provenance=provenance,
        cuda_device=cuda_device,
        gpu_layers=gpu_layers,
        validation_wav_rel=validation_wav_rel,
        importer_timeout=import_timeout,
        force=force,
    )
    planned_commands = [importer_cmd]

    if dry_run:
        was_running = backend_initial_state == "running"
        was_stopped = backend_initial_state == "stopped"
        finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        report = _build_report(
            status="dry_run_complete",
            started_at=started_at,
            finished_at=finished_at,
            voice_id=voice_id,
            audio_fname=audio_path.name,
            transcript_fname=transcript_path.name,
            backend_container=backend_container,
            backend_image=backend_image,
            backend_initial_state=backend_initial_state,
            backend_final_state=backend_initial_state,
            backend_would_restart=(
                True if was_running else (False if was_stopped else None)
            ),
            backend_restarted=False,
            backend_recovery_result="not_attempted",
            planned_commands=planned_commands,
            dry_run=True,
            force=force,
            importer_exit_code=None,
            resolved_image_id=resolved_image_id,
            resolved_digest=resolved_digest,
            identity_report=identity_report,
            errors=[],
            warnings=[] if identity_ok else ["Image identity verification failed — import would be blocked"],
        )
        if not identity_ok and identity_report:
            report["errors"] = [identity_report.get("error", "Identity check failed")]
            report["status"] = "failed"
        return report

    # --- Identity: fail-closed ---
    if not identity_ok:
        error_msg = identity_report.get("error", "Image identity verification failed") if identity_report else "Identity check failed"
        finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return _build_report(
            status="failed",
            started_at=started_at,
            finished_at=finished_at,
            voice_id=voice_id,
            audio_fname=audio_path.name,
            transcript_fname=transcript_path.name,
            backend_container=backend_container,
            backend_image=backend_image,
            backend_initial_state=backend_initial_state,
            backend_final_state=backend_initial_state,
            backend_would_restart=False,
            backend_restarted=False,
            backend_recovery_result="not_attempted",
            dry_run=False,
            force=force,
            importer_exit_code=None,
            resolved_image_id=resolved_image_id,
            resolved_digest=resolved_digest,
            identity_report=identity_report,
            errors=[error_msg],
            warnings=[],
        )

    # --- LOCKED ---
    if lock is not None:
        if not lock.acquire():
            raise LockError(
                "Another voice import is already active (lock acquisition failed). "
                "Wait for it to finish or remove the lock file manually."
            )
    try:
        return _run_locked(
            started_at=started_at,
            audio_path=audio_path,
            transcript_path=transcript_path,
            voice_id=voice_id,
            license_str=license_str,
            attribution=attribution,
            provenance=provenance,
            voices_dir=voices_dir,
            backend_container=backend_container,
            backend_image=backend_image,
            importer_cmd=importer_cmd,
            stop_timeout=stop_timeout,
            import_timeout=import_timeout,
            restart_timeout=restart_timeout,
            health_poll_interval=health_poll_interval,
            health_poll_timeout=health_poll_timeout,
            force=force,
            runner=runner,
            lock=lock,
            importer_runner=importer_runner,
            identity_report=identity_report,
            resolved_image_id=resolved_image_id,
            resolved_digest=resolved_digest,
            expected_source_revision=expected_source_revision,
            expected_s2cpp_revision=expected_s2cpp_revision,
            restart_backend=restart_backend,
            validation_wav_rel=validation_wav_rel,
        )
    finally:
        if lock is not None:
            lock.release()


def _run_locked(
    *,
    started_at: str,
    audio_path: Path,
    transcript_path: Path,
    voice_id: str,
    license_str: str,
    attribution: str,
    provenance: str,
    voices_dir: Path,
    backend_container: str,
    backend_image: str,
    importer_cmd: list[str],
    stop_timeout: int,
    import_timeout: int,
    restart_timeout: int,
    health_poll_interval: float,
    health_poll_timeout: int,
    force: bool,
    runner: Runner,
    lock: Any,
    importer_runner: Runner | None = None,
    identity_report: dict[str, Any] | None,
    resolved_image_id: str | None,
    resolved_digest: str | None,
    expected_source_revision: str,
    expected_s2cpp_revision: str,
    restart_backend: bool = False,
    validation_wav_rel: str | None = None,
) -> dict[str, Any]:
    """Core lifecycle after lock acquisition."""
    finished_at: str | None = None
    errors: list[str] = []
    warnings: list[str] = []
    importer_exit_code: int | None = None
    importer_duration_sec: float | None = None
    backend_initial_state: str = "unknown"
    backend_final_state: str = "unknown"
    backend_restarted: bool = False
    backend_recovery_result: str = "not_attempted"
    backend_would_restart: bool | None = None
    restart_attempted: bool = False

    # --- Detect backend state ---
    backend_initial_state = detect_backend_state(backend_container, runner=runner)
    was_running = backend_initial_state == "running"
    was_stopped = backend_initial_state == "stopped"

    # Fail closed on unknown/unsafe states
    if not is_safe_backend_state(backend_initial_state):
        return _build_report(
            status="failed",
            started_at=started_at,
            finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            voice_id=voice_id,
            audio_fname=audio_path.name,
            transcript_fname=transcript_path.name,
            backend_container=backend_container,
            backend_image=backend_image,
            backend_initial_state=backend_initial_state,
            backend_final_state=backend_initial_state,
            backend_would_restart=False,
            backend_restarted=False,
            backend_recovery_result="not_attempted",
            dry_run=False,
            force=force,
            importer_exit_code=None,
            resolved_image_id=resolved_image_id,
            resolved_digest=resolved_digest,
            identity_report=identity_report,
            errors=[f"Backend in unsafe state: {backend_initial_state}"],
            warnings=[],
        )

    if backend_initial_state == "missing":
        return _build_report(
            status="failed",
            started_at=started_at,
            finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            voice_id=voice_id,
            audio_fname=audio_path.name,
            transcript_fname=transcript_path.name,
            backend_container=backend_container,
            backend_image=backend_image,
            backend_initial_state="missing",
            backend_final_state="missing",
            backend_would_restart=False,
            backend_restarted=False,
            backend_recovery_result="not_attempted",
            dry_run=False,
            force=force,
            importer_exit_code=None,
            resolved_image_id=resolved_image_id,
            resolved_digest=resolved_digest,
            identity_report=identity_report,
            errors=[f"Backend container not found: {backend_container}"],
            warnings=[],
        )

    # --- BACKEND_STOPPING ---
    backend_stopped = False
    if was_running:
        if _SIGNAL_RECEIVED.is_set():
            return _build_report(
                status="aborted",
                started_at=started_at,
                finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                voice_id=voice_id,
                audio_fname=audio_path.name,
                transcript_fname=transcript_path.name,
                backend_container=backend_container,
                backend_image=backend_image,
                backend_initial_state=backend_initial_state,
                backend_final_state="running",
                backend_would_restart=True,
                backend_restarted=False,
                backend_recovery_result="not_attempted",
                dry_run=False,
                force=force,
                importer_exit_code=None,
                resolved_image_id=resolved_image_id,
                resolved_digest=resolved_digest,
                identity_report=identity_report,
                errors=["Interrupted before backend stop"],
                warnings=[],
            )

        try:
            runner(
                ["docker", "stop", "--time", str(stop_timeout), backend_container],
                timeout=stop_timeout + 10,
                check=True,
            )
            # Bounded poll until actually stopped
            backend_stopped = poll_until_stopped(
                backend_container, stop_timeout, runner=runner
            )
            if not backend_stopped:
                errors.append(
                    f"Backend did not stop within {stop_timeout}s after docker stop"
                )
        except subprocess.CalledProcessError as exc:
            errors.append(f"Backend stop failed: exit {exc.returncode}")
        except subprocess.TimeoutExpired:
            errors.append(f"Backend stop timed out after {stop_timeout}s")
        except Exception as exc:
            errors.append(f"Backend stop error: {_bounded_str(exc)}")

    # --- BACKEND_STOPPED ---
    if was_running and not backend_stopped:
        # Attempt to restart even though stop failed
        try:
            runner(
                ["docker", "start", backend_container],
                timeout=restart_timeout,
            )
        except Exception:
            pass

        finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return _build_report(
            status="failed",
            started_at=started_at,
            finished_at=finished_at,
            voice_id=voice_id,
            audio_fname=audio_path.name,
            transcript_fname=transcript_path.name,
            backend_container=backend_container,
            backend_image=backend_image,
            backend_initial_state=backend_initial_state,
            backend_final_state="unknown",
            backend_would_restart=True,
            backend_restarted=False,
            backend_recovery_result="failed",
            dry_run=False,
            force=force,
            importer_exit_code=None,
            resolved_image_id=resolved_image_id,
            resolved_digest=resolved_digest,
            identity_report=identity_report,
            errors=errors,
            warnings=[],
        )

    # --- IMPORT_RUNNING ---
    import_failed = False
    import_start = time.monotonic()
    # Use the interruptible importer runner if provided, otherwise fall back to
    # the synchronous runner (for backward-compatible test injection).
    selected_importer_runner = importer_runner if importer_runner is not None else runner
    try:
        if _SIGNAL_RECEIVED.is_set():
            errors.append("Interrupted before import")
            import_failed = True
        else:
            result = selected_importer_runner(
                importer_cmd,
                timeout=import_timeout,
                check=True,
            )
            importer_exit_code = result.returncode
    except subprocess.CalledProcessError as exc:
        importer_exit_code = exc.returncode
        errors.append(f"Importer failed with exit code {exc.returncode}")
        import_failed = True
    except subprocess.TimeoutExpired:
        errors.append(f"Importer timed out after {import_timeout}s")
        import_failed = True
    except Exception as exc:
        errors.append(f"Importer error: {_bounded_str(exc)}")
        import_failed = True
    importer_duration_sec = time.monotonic() - import_start

    # --- IMPORT_VALIDATING ---
    profile_path = voices_dir / f"{voice_id}.s2voice"
    sidecar_path = voices_dir / f"{voice_id}.s2voice.json"
    profile_sha: str | None = None
    file_ownership: dict[str, Any] = {}
    if not import_failed and importer_exit_code == 0:
        validation_errors, profile_sha, file_ownership = _validate_output(
            profile_path, sidecar_path, voice_id,
            expected_s2cpp_revision=expected_s2cpp_revision,
            validation_wav_rel=validation_wav_rel,
        )
        if validation_errors:
            errors.extend(validation_errors)
            import_failed = True

    # --- BACKEND_STARTING ---
    should_restart = was_running or restart_backend
    if should_restart:
        restart_attempted = True
        try:
            if _SIGNAL_RECEIVED.is_set():
                errors.append("Interrupted before backend restart")
            else:
                result = runner(
                    ["docker", "start", backend_container],
                    timeout=restart_timeout,
                    check=True,
                )
                backend_restarted = True
                # Bounded health check
                healthy, health_evidence = poll_until_healthy(
                    backend_container,
                    restart_timeout if restart_timeout > 0 else health_poll_timeout,
                    poll_interval=health_poll_interval,
                    runner=runner,
                )
                if healthy:
                    backend_final_state = "running"
                    backend_recovery_result = "healthy"
                else:
                    backend_final_state = detect_backend_state(
                        backend_container, runner=runner
                    )
                    backend_recovery_result = "unhealthy"
                    errors.append(
                        f"Backend started but health check did not confirm readiness "
                        f"(state={backend_final_state}, health={health_evidence.get('health', 'unknown')})"
                    )
        except subprocess.CalledProcessError as exc:
            errors.append(f"Backend restart failed: exit {exc.returncode}")
        except subprocess.TimeoutExpired:
            errors.append("Backend restart timed out")
        except Exception as exc:
            errors.append(f"Backend restart error: {_bounded_str(exc)}")
    else:
        # Initially stopped — don't restart
        backend_final_state = "stopped"
        backend_recovery_result = "not_attempted"

    # --- COMPLETE ---
    finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    status = "complete" if not errors else "failed"

    return _build_report(
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        voice_id=voice_id,
        audio_fname=audio_path.name,
        transcript_fname=transcript_path.name,
        backend_container=backend_container,
        backend_image=backend_image,
        backend_initial_state=backend_initial_state,
        backend_final_state=backend_final_state,
        backend_would_restart=(
            True if was_running else (False if was_stopped else None)
        ),
        backend_restarted=backend_restarted,
        backend_recovery_result=backend_recovery_result,
        restart_attempted=restart_attempted,
        dry_run=False,
        force=force,
        importer_exit_code=importer_exit_code,
        importer_duration_sec=importer_duration_sec,
        resolved_image_id=resolved_image_id,
        resolved_digest=resolved_digest,
        identity_report=identity_report,
        profile_path=str(profile_path) if profile_path.exists() else None,
        sidecar_path=str(sidecar_path) if sidecar_path.exists() else None,
        profile_sha=profile_sha,
        file_ownership=file_ownership,
        validation_wav_rel=validation_wav_rel,
        expected_source_revision=expected_source_revision if expected_source_revision else None,
        expected_s2cpp_revision=expected_s2cpp_revision if expected_s2cpp_revision else None,
        errors=errors,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Post-import validation
# ---------------------------------------------------------------------------


def _validate_output(
    profile_path: Path,
    sidecar_path: Path,
    voice_id: str,
    *,
    expected_s2cpp_revision: str = "",
    validation_wav_rel: str | None = None,
) -> tuple[list[str], str | None, dict[str, Any]]:
    """Validate post-import profile and sidecar files.

    Returns (errors, profile_sha, file_ownership).
    Reuses app.voice_schema and app.voice_profile where available.
    """
    errors: list[str] = []
    ownership: dict[str, Any] = {}
    profile_sha: str | None = None

    for label, path in (
        ("profile", profile_path),
        ("sidecar", sidecar_path),
    ):
        if not path.exists():
            errors.append(f"{label} not found after import: {path}")
            continue
        if path.is_symlink():
            errors.append(f"{label} must not be a symlink: {path}")
            continue
        if not path.is_file():
            errors.append(f"{label} is not a regular file: {path}")
            continue

        st = path.stat()
        ownership[label] = {
            "uid": st.st_uid,
            "gid": st.st_gid,
            "mode_octal": oct(stat.S_IMODE(st.st_mode)),
        }
        if st.st_size == 0:
            errors.append(f"{label} is empty: {path}")

    # Compute profile SHA and validate binary format
    if profile_path.exists() and profile_path.is_file() and not profile_path.is_symlink():
        try:
            data = profile_path.read_bytes()
            profile_sha = hashlib.sha256(data).hexdigest()

            # Use app.voice_profile — FAIL CLOSED
            try:
                from app.voice_profile import parse_s2voice, VoiceProfileError
                parse_s2voice(data)
            except ImportError:
                errors.append(
                    "Profile validation unavailable: app.voice_profile module not importable. "
                    "Cannot verify profile binary integrity."
                )
            except VoiceProfileError as exc:
                errors.append(f"Profile validation error: {_bounded_str(exc)}")
        except Exception as exc:
            errors.append(f"Profile validation error: {_bounded_str(exc)}")

    # Validate sidecar JSON against schema
    if sidecar_path.exists() and sidecar_path.is_file() and not sidecar_path.is_symlink():
        try:
            sidecar_data = json.loads(sidecar_path.read_text(encoding="utf-8"))

            # Validate against schema — FAIL CLOSED
            try:
                import jsonschema

                from app.voice_schema import VOICE_SIDECAR_SCHEMA

                schema = json.loads(VOICE_SIDECAR_SCHEMA)
                jsonschema.validate(sidecar_data, schema)
            except ImportError:
                errors.append(
                    "Sidecar schema validation unavailable: jsonschema module not importable. "
                    "Cannot verify sidecar schema integrity."
                )
            except Exception as exc:
                errors.append(f"Sidecar schema validation failed: {_bounded_str(exc)}")

            # ID match
            sidecar_id = sidecar_data.get("id")
            if sidecar_id != voice_id:
                errors.append(
                    f"Sidecar voice ID mismatch: expected {voice_id}, "
                    f"got {sidecar_id}"
                )

            # SHA-256 hash match
            sidecar_hash = sidecar_data.get("hash_sha256")
            if sidecar_hash:
                if not _SHA256_HEX_RE.fullmatch(sidecar_hash):
                    errors.append(f"Sidecar hash_sha256 is not a valid 64-char hex digest: {sidecar_hash}")
                elif profile_sha and sidecar_hash != profile_sha:
                    errors.append(
                        f"Profile hash mismatch: sidecar={sidecar_hash[:16]}..., "
                        f"computed={profile_sha[:16]}..."
                    )

            # Provenance s2cpp_revision check
            if expected_s2cpp_revision:
                provenance = sidecar_data.get("provenance")
                provenance_s2cpp = (
                    provenance.get("s2cpp_revision")
                    if isinstance(provenance, dict)
                    else None
                )
                if not isinstance(provenance_s2cpp, str) or not _S2CPP_REVISION_RE.fullmatch(
                    provenance_s2cpp
                ):
                    errors.append(
                        "Sidecar provenance.s2cpp_revision must be exactly 40 "
                        "lowercase hexadecimal characters"
                    )
                elif provenance_s2cpp != expected_s2cpp_revision:
                    errors.append(
                        f"Sidecar s2cpp_revision mismatch: "
                        f"expected {expected_s2cpp_revision[:16]}..., "
                        f"got {provenance_s2cpp[:16]}..."
                    )

        except (json.JSONDecodeError, UnicodeError) as exc:
            errors.append(f"Sidecar is not valid JSON: {_bounded_str(exc)}")

    # Validate optional validation WAV
    if validation_wav_rel is not None:
        wav_path = profile_path.parent / validation_wav_rel
        try:
            resolved_wav = wav_path.resolve()
            resolved_wav.relative_to(profile_path.parent.resolve(strict=True))
        except ValueError:
            errors.append(f"Validation WAV path resolves outside voices directory: {validation_wav_rel}")
        else:
            if not wav_path.exists():
                errors.append(f"Validation WAV not found after import: {wav_path}")
            else:
                if wav_path.is_symlink():
                    errors.append(f"Validation WAV must not be a symlink: {wav_path}")
                elif not wav_path.is_file():
                    errors.append(f"Validation WAV is not a regular file: {wav_path}")
                else:
                    wav_st = wav_path.stat()
                    ownership["validation_wav"] = {
                        "uid": wav_st.st_uid,
                        "gid": wav_st.st_gid,
                        "mode_octal": oct(stat.S_IMODE(wav_st.st_mode)),
                    }
                    if wav_st.st_size == 0:
                        errors.append(f"Validation WAV is empty: {wav_path}")

    # Check for staging residue
    if profile_path.exists():
        voice_dir = profile_path.parent
        for entry in voice_dir.iterdir():
            if entry.name.startswith(f"{voice_id}.staging"):
                errors.append(f"Staging residue detected: {entry.name}")

    return errors, profile_sha, ownership


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def _build_report(
    *,
    status: str,
    started_at: str,
    finished_at: str,
    voice_id: str,
    audio_fname: str,
    transcript_fname: str,
    backend_container: str,
    backend_image: str,
    backend_initial_state: str,
    backend_final_state: str,
    backend_would_restart: bool | None,
    backend_restarted: bool,
    backend_recovery_result: str = "not_attempted",
    restart_attempted: bool = False,
    dry_run: bool,
    force: bool,
    importer_exit_code: int | None = None,
    importer_duration_sec: float | None = None,
    planned_commands: list[list[str]] | None = None,
    resolved_image_id: str | None = None,
    resolved_digest: str | None = None,
    identity_report: dict[str, Any] | None = None,
    profile_path: str | None = None,
    sidecar_path: str | None = None,
    profile_sha: str | None = None,
    file_ownership: dict[str, Any] | None = None,
    validation_wav_rel: str | None = None,
    expected_source_revision: str | None = None,
    expected_s2cpp_revision: str | None = None,
    errors: list[str] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build the sanitized structured report."""
    report: dict[str, Any] = {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "voice_id": voice_id,
        "audio_filename": audio_fname,
        "transcript_filename": transcript_fname,
        "backend_container": backend_container,
        "backend_initial_state": backend_initial_state,
        "backend_final_state": backend_final_state,
        "backend_restarted": backend_restarted,
        "backend_recovery_result": backend_recovery_result,
        "restart_attempted": restart_attempted,
        "configured_image": backend_image,
        "dry_run": dry_run,
        "force": force,
    }

    if backend_would_restart is not None:
        report["backend_would_restart"] = backend_would_restart

    if importer_exit_code is not None:
        report["importer_exit_code"] = importer_exit_code

    if importer_duration_sec is not None:
        report["importer_duration_sec"] = round(importer_duration_sec, 3)

    if resolved_image_id:
        report["resolved_image_id"] = resolved_image_id

    if resolved_digest:
        report["resolved_digest"] = resolved_digest

    if identity_report:
        report["identity"] = {
            "checks_passed": identity_report.get("checks_passed", False),
            "source_revision_match": identity_report.get("source_revision_match"),
            "s2cpp_revision_match": identity_report.get("s2cpp_revision_match"),
        }

    if profile_path:
        report["profile_path"] = profile_path

    if sidecar_path:
        report["sidecar_path"] = sidecar_path

    if profile_sha:
        report["profile_sha256"] = profile_sha

    if file_ownership:
        report["file_ownership"] = file_ownership

    if validation_wav_rel:
        report["validation_wav_relative"] = validation_wav_rel

    if expected_source_revision:
        report["expected_source_revision"] = expected_source_revision

    if expected_s2cpp_revision:
        report["expected_s2cpp_revision"] = expected_s2cpp_revision

    if errors:
        report["errors"] = [_bounded_str(e) for e in errors]

    if warnings:
        report["warnings"] = [_bounded_str(w) for w in warnings]

    if planned_commands:
        # Redact transcript from planned commands
        redacted: list[list[str]] = []
        for cmd in planned_commands:
            redacted_cmd = list(cmd)
            if "--transcript-file" in redacted_cmd:
                idx = redacted_cmd.index("--transcript-file") + 1
                if idx < len(redacted_cmd):
                    redacted_cmd[idx] = "[REDACTED PATH]"
            redacted.append(redacted_cmd)
        report["planned_commands"] = redacted

    return report


def _bounded_str(value: object) -> str:
    """Truncate a string to _MAX_ERROR_LENGTH for safe reporting."""
    s = str(value)
    if len(s) > _MAX_ERROR_LENGTH:
        return s[:_MAX_ERROR_LENGTH] + "...[truncated]"
    return s


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for add-s2voice."""
    parser = argparse.ArgumentParser(
        prog="add-s2voice",
        description=(
            "Safe Unraid host voice-import operator.  Stops the s2.cpp backend, "
            "runs a one-shot importer, validates output, and restarts the backend."
        ),
    )
    parser.add_argument(
        "--audio", required=True, help="Path to reference audio file (WAV/FLAC/MP3/etc)"
    )
    parser.add_argument(
        "--transcript-file",
        required=True,
        help="Path to UTF-8 transcript file",
    )
    parser.add_argument(
        "--voice-id", required=True, help="Voice profile ID (alphanumeric, 1-128 chars)"
    )
    parser.add_argument(
        "--license", required=True, help="License identifier (e.g., permission-granted)"
    )
    parser.add_argument(
        "--attribution", required=True, help="Attribution string"
    )
    parser.add_argument(
        "--provenance-source", required=True, help="Provenance source description"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and print planned commands without executing",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force overwrite of existing voice profile",
    )
    parser.add_argument(
        "--config",
        help="Path to config file (strict dotenv format)",
    )
    parser.add_argument(
        "--report-file",
        help="Write JSON report to file (in addition to stdout)",
    )
    parser.add_argument(
        "--validation-wav-relative",
        dest="validation_wav_rel",
        help="Relative path under voices directory for validation WAV retention",
    )
    # Override config values via CLI
    parser.add_argument(
        "--backend-container",
        help="Backend Docker container name",
    )
    parser.add_argument(
        "--backend-image",
        help="Immutable backend image reference (sha256:... or tag:sha-<40-hex>)",
    )
    parser.add_argument(
        "--models-dir",
        help="Host models directory path",
    )
    parser.add_argument(
        "--voices-dir",
        help="Host voices directory path",
    )
    parser.add_argument(
        "--import-inputs-dir",
        help="Host import-inputs directory path",
    )
    parser.add_argument(
        "--model-container-path",
        help="Model path inside container",
    )
    parser.add_argument(
        "--tokenizer-container-path",
        help="Tokenizer path inside container",
    )
    parser.add_argument(
        "--cuda-device", type=int, help="CUDA device number",
    )
    parser.add_argument(
        "--gpu-layers", type=int, help="GPU layer count",
    )
    parser.add_argument(
        "--expected-source-revision",
        help="Expected source git revision (40-char hex)",
    )
    parser.add_argument(
        "--expected-s2cpp-revision",
        help="Expected s2.cpp build revision (40-char hex)",
    )
    parser.add_argument(
        "--restart-backend",
        action="store_true",
        default=False,
        help="Restart the backend even if it was initially stopped",
    )
    return parser


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# Defaults — BACKEND_IMAGE is purposely invalid to force explicit configuration
_DEFAULTS: dict[str, str] = {
    "BACKEND_CONTAINER": "s2cpp-backend",
    "BACKEND_IMAGE": "REQUIRED_SET_BACKEND_IMAGE_VIA_CONFIG_OR_CLI",
    "MODELS_DIR": "/mnt/user/appdata/s2cpp/models",
    "VOICES_DIR": "/mnt/user/appdata/s2cpp/voices",
    "IMPORT_INPUTS_DIR": "/mnt/user/appdata/s2cpp/voice-import-inputs",
    "MODEL_CONTAINER_PATH": "/models/s2-pro-q6_k.gguf",
    "TOKENIZER_CONTAINER_PATH": "/models/tokenizer.json",
    "CUDA_DEVICE": "0",
    "GPU_LAYERS": "99",
    "STOP_TIMEOUT_SEC": "30",
    "IMPORT_TIMEOUT_SEC": "600",
    "RESTART_TIMEOUT_SEC": "120",
    "HEALTH_POLL_INTERVAL_SEC": "2",
    "HEALTH_POLL_TIMEOUT_SEC": "120",
    "EXPECTED_SOURCE_REVISION": "",
    "EXPECTED_S2CPP_REVISION": "",
    "LOCK_FILE": "/mnt/user/appdata/s2cpp/operator/import.lock",
}


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def _write_all(fd: int, data: bytes) -> None:
    """Write a complete byte buffer, failing if the descriptor makes no progress."""
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("report write made no progress")
        remaining = remaining[written:]


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for add-s2voice."""
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    # Load config — auto-load adjacent config.env if no explicit --config
    config: dict[str, str] = {}
    if args.config:
        config_path = Path(args.config)
        if config_path.is_file():
            config = parse_config(config_path.read_text(encoding="utf-8"))
    else:
        # Auto-load adjacent config.env if present
        script_dir = Path(__file__).resolve().parent
        adjacent_config = script_dir / "config.env"
        if adjacent_config.is_file():
            config = parse_config(adjacent_config.read_text(encoding="utf-8"))

    # Merge defaults, config, and CLI args
    merged: dict[str, str] = dict(_DEFAULTS)
    merged.update(config)
    merged = merge_config_with_args(merged, args)

    # Resolve paths
    models_dir = Path(merged["MODELS_DIR"])
    voices_dir = Path(merged["VOICES_DIR"])
    import_inputs_dir = Path(merged["IMPORT_INPUTS_DIR"])
    audio_path = Path(args.audio)
    transcript_path = Path(args.transcript_file)

    # Compute model_rel and tokenizer_rel from validated container paths,
    # preserving any subdirectories below /models.
    model_path = _resolve_container_path(merged["MODEL_CONTAINER_PATH"])
    tokenizer_path = _resolve_container_path(merged["TOKENIZER_CONTAINER_PATH"])
    model_rel = model_path["host_relative"]
    tokenizer_rel = tokenizer_path["host_relative"]

    # Build lock
    lock_path = Path(merged.get("LOCK_FILE", _DEFAULTS["LOCK_FILE"]))
    file_lock = _RealFileLock(lock_path)

    try:
        result = run_operator(
            audio_path=audio_path,
            transcript_path=transcript_path,
            voice_id=args.voice_id,
            license_str=args.license,
            attribution=args.attribution,
            provenance=args.provenance_source,
            models_dir=models_dir,
            voices_dir=voices_dir,
            import_inputs_dir=import_inputs_dir,
            model_rel=model_rel,
            tokenizer_rel=tokenizer_rel,
            backend_container=merged["BACKEND_CONTAINER"],
            backend_image=merged["BACKEND_IMAGE"],
            cuda_device=int(merged.get("CUDA_DEVICE", "0")),
            gpu_layers=int(merged.get("GPU_LAYERS", "99")),
            stop_timeout=int(merged.get("STOP_TIMEOUT_SEC", "30")),
            import_timeout=int(merged.get("IMPORT_TIMEOUT_SEC", "600")),
            restart_timeout=int(merged.get("RESTART_TIMEOUT_SEC", "120")),
            health_poll_interval=float(merged.get("HEALTH_POLL_INTERVAL_SEC", "2")),
            health_poll_timeout=int(merged.get("HEALTH_POLL_TIMEOUT_SEC", "120")),
            expected_source_revision=merged.get("EXPECTED_SOURCE_REVISION", ""),
            expected_s2cpp_revision=merged.get("EXPECTED_S2CPP_REVISION", ""),
            dry_run=args.dry_run,
            force=args.force,
            restart_backend=args.restart_backend,
            validation_wav_rel=args.validation_wav_rel,
            lock=file_lock,
            importer_runner=_importer_runner,
        )
    except LockError as exc:
        _write_error_report(args, str(exc))
        return 2
    except PreflightError as exc:
        _write_error_report(args, str(exc))
        return 2
    except OperatorError as exc:
        _write_error_report(args, str(exc))
        return 2

    # Write report
    report_json = json.dumps(result, indent=2)
    sys.stdout.write(report_json + "\n")

    if args.report_file:
        try:
            report_path = Path(args.report_file)
            # Parent must already exist — do not mkdir arbitrary paths
            if not report_path.parent.is_dir():
                sys.stderr.write(
                    f"Warning: report file parent directory does not exist: "
                    f"{report_path.parent}. Report not written.\n"
                )
            else:
                # Atomic write: write to temp file then rename
                import tempfile as _tmp
                tmp_fd, tmp_path = _tmp.mkstemp(
                    dir=str(report_path.parent),
                    prefix=".report-",
                    suffix=".tmp",
                )
                try:
                    _write_all(tmp_fd, (report_json + "\n").encode("utf-8"))
                finally:
                    os.close(tmp_fd)
                os.replace(tmp_path, str(report_path))
        except OSError as exc:
            sys.stderr.write(f"Warning: could not write report file: {exc}\n")

    # Determine exit code based on result
    if result["status"] == "complete":
        return 0
    elif result["status"] == "dry_run_complete" and not result.get("errors"):
        return 0
    else:
        return 1


def _write_error_report(args: argparse.Namespace, error_msg: str) -> None:
    """Write a sanitized error report to stdout and optionally --report-file."""
    report = {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "status": "failed",
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "voice_id": getattr(args, "voice_id", "unknown"),
        "dry_run": getattr(args, "dry_run", False),
        "force": getattr(args, "force", False),
        "errors": [_bounded_str(error_msg)],
    }
    report_json = json.dumps(report, indent=2)
    sys.stdout.write(report_json + "\n")
    if args.report_file:
        try:
            report_path = Path(args.report_file)
            if not report_path.parent.is_dir():
                sys.stderr.write(
                    f"Warning: report file parent directory does not exist: "
                    f"{report_path.parent}. Report not written.\n"
                )
            else:
                import tempfile as _tmp
                tmp_fd, tmp_path = _tmp.mkstemp(
                    dir=str(report_path.parent),
                    prefix=".report-",
                    suffix=".tmp",
                )
                try:
                    _write_all(tmp_fd, (report_json + "\n").encode("utf-8"))
                finally:
                    os.close(tmp_fd)
                os.replace(tmp_path, str(report_path))
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())

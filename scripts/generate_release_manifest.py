#!/usr/bin/env python3
"""Generate a deterministic release-pair manifest for the CI release workflow.

Produces a JSON manifest documenting both the wrapper and backend images
as a paired release.  The manifest is consumed by the publish workflow to
record image digests, upstream revision, and provenance.

Environment variables:
    VERSION            Release version (default from app.version: 0.1.0)
    S2CPP_REVISION     Upstream s2.cpp revision SHA (required)
    SOURCE_SHA         Git SHA of this repository (required in release mode)
    WRAPPER_DIGEST     Wrapper image digest (required in release mode)
    BACKEND_DIGEST     Backend image digest (required in release mode)
    TEST_RESULTS       Test results summary JSON (required in release mode)
    GENERATED_AT       ISO 8601 UTC timestamp for build_meta (always required)
    CREATED            ISO 8601 UTC timestamp for created field (optional)
    RELEASE_MODE       If 'true', enforces strict requirements

Exits non-zero with a message on:
    Missing required GENERATED_AT
    Missing required S2CPP_REVISION
    Invalid version format (must be valid SemVer and match canonical)
    Invalid digest format (must be sha256:hex)
    Release mode missing required fields
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# ── Import canonical version ─────────────────────────────────────────────────
# Single source of truth: app/version.py
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.version import __version__ as CANONICAL_VERSION  # type: ignore[import-untyped]
except ImportError:
    sys.exit("ERROR: cannot import canonical version from app.version")

# ── Constants ─────────────────────────────────────────────────────────────────
SCHEMA_VERSION = "1.0.0"
WRAPPER_IMAGE_NAME = "wyoming-s2cpp-tts"
BACKEND_IMAGE_NAME = "s2cpp-backend"

# ── Format validators ─────────────────────────────────────────────────────────

_SEMVER_RE = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([\da-z-]+(?:\.[\da-z-]+)*))?"
    r"(?:\+([\da-z-]+(?:\.[\da-z-]+)*))?$"
)

_DIGEST_RE = re.compile(r"^sha256:[a-fA-F0-9]{64}$")

_ISO8601_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:Z|[+-]\d{2}:\d{2})$"
)


def _strip_v(version: str) -> str:
    """Strip leading 'v' from version string."""
    return version[1:] if version.startswith("v") else version


def _validate_version(version: str) -> None:
    """Fail closed if version is not valid SemVer."""
    if not _SEMVER_RE.match(version):
        sys.exit(f"ERROR: invalid version format: {version!r}")


def _validate_against_canonical(version: str) -> None:
    """Fail if the base version (without pre-release) doesn't match canonical."""
    m = _SEMVER_RE.match(version)
    if not m:
        return  # already caught by _validate_version
    major, minor, patch = m.group(1), m.group(2), m.group(3)
    base_ver = f"{major}.{minor}.{patch}"
    if base_ver != CANONICAL_VERSION:
        sys.exit(
            f"ERROR: version {base_ver} does not match canonical source version {CANONICAL_VERSION}"
        )
    # Validate pre-release format: must be rc.N
    pre = m.group(4)
    if pre:
        if not re.match(r"^rc\.\d+$", pre):
            sys.exit(f"ERROR: pre-release must be rc.N, got {pre!r}")


def _validate_digest(label: str, digest: str) -> None:
    """Fail closed if digest is not sha256:<hex>."""
    if not _DIGEST_RE.match(digest):
        sys.exit(f"ERROR: invalid {label} digest: {digest!r}")


def _validate_iso8601_utc(label: str, value: str) -> None:
    """Fail closed if value is not ISO 8601 UTC timestamp."""
    if not _ISO8601_UTC_RE.match(value):
        sys.exit(
            f"ERROR: invalid {label} timestamp: {value!r} "
            f"(must be ISO 8601 UTC, e.g. YYYY-MM-DDTHH:MM:SSZ or +00:00)"
        )


# ── Collect inputs ────────────────────────────────────────────────────────────

# Check release mode
_release_mode = os.environ.get("RELEASE_MODE", "").strip().lower() == "true"

# Upstream s2.cpp revision (required).
_s2cpp_revision = os.environ.get("S2CPP_REVISION", "").strip()
if not _s2cpp_revision:
    sys.exit("ERROR: S2CPP_REVISION is required")

# Validate revision looks like a git SHA (7-64 hex chars).
if not re.match(r"^[a-fA-F0-9]{7,64}$", _s2cpp_revision):
    sys.exit(f"ERROR: invalid S2CPP_REVISION format: {_s2cpp_revision!r}")

# Source repository SHA.
_source_sha = os.environ.get("SOURCE_SHA", "").strip()
if _source_sha:
    if not re.match(r"^[a-fA-F0-9]{7,64}$", _source_sha):
        sys.exit(f"ERROR: invalid SOURCE_SHA format: {_source_sha!r}")

# Version resolution: prefer VERSION env var; fall back to canonical.
_VERSION = os.environ.get("VERSION", "").strip()
if not _VERSION:
    _VERSION = CANONICAL_VERSION

# Validate version.
_validate_version(_VERSION)
_validate_against_canonical(_VERSION)

# Strip v prefix for release_version output
_release_version = _strip_v(_VERSION)

# Image digests
_wrapper_digest = os.environ.get("WRAPPER_DIGEST", "").strip()
_backend_digest = os.environ.get("BACKEND_DIGEST", "").strip()
if _wrapper_digest:
    _validate_digest("wrapper", _wrapper_digest)
if _backend_digest:
    _validate_digest("backend", _backend_digest)

# Test results
_test_results_raw = os.environ.get("TEST_RESULTS", "").strip()
_test_results: dict | None = None
if _test_results_raw:
    try:
        _test_results = json.loads(_test_results_raw)
    except json.JSONDecodeError as exc:
        sys.exit(f"ERROR: invalid TEST_RESULTS JSON: {exc}")

# GENERATED_AT is always required
_generated_at = os.environ.get("GENERATED_AT", "").strip()
if not _generated_at:
    sys.exit("ERROR: GENERATED_AT is required")
_validate_iso8601_utc("generated_at", _generated_at)

_created_at = os.environ.get("CREATED", "").strip()
if _created_at:
    _validate_iso8601_utc("created", _created_at)

# ── Release mode validation ───────────────────────────────────────────────────
if _release_mode:
    if not _source_sha:
        sys.exit("ERROR: SOURCE_SHA is required in release mode")
    if not _wrapper_digest:
        sys.exit("ERROR: WRAPPER_DIGEST is required in release mode")
    if not _backend_digest:
        sys.exit("ERROR: BACKEND_DIGEST is required in release mode")
    if _test_results is None:
        sys.exit("ERROR: TEST_RESULTS is required in release mode")
    if not isinstance(_test_results, dict) or len(_test_results) == 0:
        sys.exit("ERROR: TEST_RESULTS must be a non-empty object in release mode")

# ── Patch checksum (if patches exist) ─────────────────────────────────────────
_patch_dir = Path(__file__).resolve().parent.parent / "docker" / "s2cpp" / "patches"
_patch_checksum: str | None = None
if _patch_dir.is_dir():
    import hashlib

    _patch_files = sorted(_patch_dir.glob("*.patch"))
    if _patch_files:
        _hasher = hashlib.sha256()
        for _pf in _patch_files:
            _hasher.update(_pf.read_bytes())
        _patch_checksum = f"sha256:{_hasher.hexdigest()}"

# ── Build manifest ────────────────────────────────────────────────────────────

_manifest: dict = {
    "schema_version": SCHEMA_VERSION,
    "release_version": _release_version,
    "source_sha": _source_sha or None,
    "wrapper": {
        "name": WRAPPER_IMAGE_NAME,
        "version": _release_version,
        "digest": _wrapper_digest or None,
    },
    "backend": {
        "name": BACKEND_IMAGE_NAME,
        "version": _release_version,
        "digest": _backend_digest or None,
        "s2cpp_revision": _s2cpp_revision,
    },
    "pair": {
        "wrapper": WRAPPER_IMAGE_NAME,
        "backend": BACKEND_IMAGE_NAME,
    },
    "build_meta": {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _generated_at,
        "generator": "scripts/generate_release_manifest.py",
    },
    "patch_checksum": _patch_checksum,
    "test_summary": _test_results,
}

if _created_at:
    _manifest["created"] = _created_at

print(json.dumps(_manifest, indent=2))

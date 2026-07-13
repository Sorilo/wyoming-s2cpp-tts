#!/usr/bin/env python3
"""Generate a deterministic release-pair manifest for the CI release workflow.

Produces a JSON manifest documenting both the wrapper and backend images
as a paired release.  The manifest is consumed by the publish workflow to
record image digests, upstream revision, and provenance.

Environment variables:
    VERSION           Release version (default from app.version: 0.1.0)
    S2CPP_REVISION    Upstream s2.cpp revision SHA (required)
    SOURCE_SHA         Git SHA of this repository (optional)
    WRAPPER_DIGEST    Wrapper image digest (optional, set by CI)
    BACKEND_DIGEST    Backend image digest (optional, set by CI)
    TEST_RESULTS       Test results summary JSON (optional)

Exits non-zero with a message on:
    Missing required S2CPP_REVISION
    Invalid version format (must be valid SemVer)
    Invalid digest format (must be sha256:hex)
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Constants ─────────────────────────────────────────────────────────────────
SCHEMA_VERSION = "1.0.0"
WRAPPER_IMAGE_NAME = "wyoming-s2cpp-tts"
BACKEND_IMAGE_NAME = "s2cpp-backend"

# ── Version resolution ────────────────────────────────────────────────────────
# Prefer the env var; fall back to app.version (canonical source).
_VERSION = os.environ.get("VERSION", "").strip()
if not _VERSION:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from app.version import __version__ as _app_version  # type: ignore[import-untyped]

        _VERSION = _app_version
    except ImportError:
        _VERSION = "0.1.0"

# ── Format validators ─────────────────────────────────────────────────────────

_SEMVER_RE = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([\da-z-]+(?:\.[\da-z-]+)*))?"
    r"(?:\+([\da-z-]+(?:\.[\da-z-]+)*))?$"
)

_DIGEST_RE = re.compile(r"^sha256:[a-fA-F0-9]{64}$")


def _validate_version(version: str) -> None:
    """Fail closed if version is not valid SemVer."""
    if not _SEMVER_RE.match(version):
        sys.exit(f"ERROR: invalid version format: {version!r}")


def _validate_digest(label: str, digest: str) -> None:
    """Fail closed if digest is not sha256:<hex>."""
    if not _DIGEST_RE.match(digest):
        sys.exit(f"ERROR: invalid {label} digest: {digest!r}")


# ── Collect inputs ────────────────────────────────────────────────────────────

# Upstream s2.cpp revision (required).
_s2cpp_revision = os.environ.get(
    "S2CPP_REVISION",
    # Backward-compat: also support the typo version used by some test callers.
    os.environ.get("WITH_S2CPP_REVISION", ""),
).strip()

if not _s2cpp_revision:
    sys.exit("ERROR: S2CPP_REVISION is required")

# Validate revision looks like a git SHA (40-char hex).
if not re.match(r"^[a-fA-F0-9]{7,64}$", _s2cpp_revision):
    sys.exit(f"ERROR: invalid S2CPP_REVISION format: {_s2cpp_revision!r}")

# Source repository SHA.
_source_sha = os.environ.get("SOURCE_SHA", "").strip()
if _source_sha:
    if not re.match(r"^[a-fA-F0-9]{7,64}$", _source_sha):
        sys.exit(f"ERROR: invalid SOURCE_SHA format: {_source_sha!r}")

# Validate version.
_validate_version(_VERSION)

# Optional image digests from CI.
_wrapper_digest = os.environ.get("WRAPPER_DIGEST", "").strip()
_backend_digest = os.environ.get("BACKEND_DIGEST", "").strip()
if _wrapper_digest:
    _validate_digest("wrapper", _wrapper_digest)
if _backend_digest:
    _validate_digest("backend", _backend_digest)

# Optional test results.
_test_results_raw = os.environ.get("TEST_RESULTS", "").strip()
_test_results: dict | None = None
if _test_results_raw:
    try:
        _test_results = json.loads(_test_results_raw)
    except json.JSONDecodeError as exc:
        sys.exit(f"ERROR: invalid TEST_RESULTS JSON: {exc}")

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
    "release_version": _VERSION,
    "source_sha": _source_sha or None,
    "wrapper": {
        "name": WRAPPER_IMAGE_NAME,
        "version": _VERSION,
        "digest": _wrapper_digest or None,
    },
    "backend": {
        "name": BACKEND_IMAGE_NAME,
        "version": _VERSION,
        "digest": _backend_digest or None,
        "s2cpp_revision": _s2cpp_revision,
    },
    "pair": {
        "wrapper": WRAPPER_IMAGE_NAME,
        "backend": BACKEND_IMAGE_NAME,
    },
    "build_meta": {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": "scripts/generate_release_manifest.py",
    },
    "patch_checksum": _patch_checksum,
    "test_summary": _test_results,
}

print(json.dumps(_manifest, indent=2))

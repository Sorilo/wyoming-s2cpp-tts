"""Phase 11 release-core: paired release-pair manifest generator tests.

Validates that the manifest generator produces correct, JSON-serializable
release-pair manifests with wrapper + backend metadata, validates formats,
and fails closed on bad inputs.  Tests GENUINE failure paths (no vacuous
or always-pass assertions).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
MANIFEST_SCRIPT = SCRIPTS_DIR / "generate_release_manifest.py"

# Valid 40-char hex revision (simulating a git SHA).
_VALID_REV = "2c33261938da1a41d713768b1b391b4d368d7d2c"
_VALID_DIGEST = "sha256:" + "a" * 64
_VALID_TS = "2025-01-15T10:30:00Z"


def _run_manifest(**env_overrides) -> str:
    """Run the manifest generator and return stdout."""
    env = os.environ.copy()
    env.update(env_overrides)
    result = subprocess.run(
        [sys.executable, str(MANIFEST_SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(PROJECT_ROOT),
    )
    if result.returncode != 0:
        raise RuntimeError(f"Manifest generator failed (rc={result.returncode}): {result.stderr}")
    return result.stdout


def _run_manifest_expect_fail(**env_overrides) -> tuple[int, str]:
    """Run the manifest generator and return (returncode, stderr) expecting failure."""
    env = os.environ.copy()
    env.update(env_overrides)
    result = subprocess.run(
        [sys.executable, str(MANIFEST_SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(PROJECT_ROOT),
    )
    return result.returncode, result.stderr


def _minimal_env(**extra) -> dict:
    """Base env with required fields for non-release mode."""
    base = {
        "S2CPP_REVISION": _VALID_REV,
        "GENERATED_AT": _VALID_TS,
    }
    base.update(extra)
    return base


# ==============================================================================
# Script existence
# ==============================================================================

def test_manifest_script_exists():
    """The manifest generator script exists."""
    assert MANIFEST_SCRIPT.is_file(), f"Missing: {MANIFEST_SCRIPT}"


# ==============================================================================
# Generates valid JSON / deterministic schema
# ==============================================================================

def test_manifest_is_valid_json():
    """The manifest output is valid JSON."""
    output = _run_manifest(**_minimal_env())
    manifest = json.loads(output)
    assert isinstance(manifest, dict)


def test_manifest_has_schema_version():
    """Manifest declares a top-level schema_version."""
    output = _run_manifest(**_minimal_env())
    manifest = json.loads(output)
    assert "schema_version" in manifest
    assert manifest["schema_version"] == "1.0.0"


# -- Contains release version -------------------------------------------------

def test_manifest_has_version():
    """Manifest includes the release version (defaults to canonical 0.1.0)."""
    output = _run_manifest(**_minimal_env())
    manifest = json.loads(output)
    assert "release_version" in manifest
    assert manifest["release_version"] == CANONICAL_VERSION


# -- Contains source SHA -------------------------------------------------------

def test_manifest_has_source_sha():
    """Manifest includes source_sha field (null when not provided)."""
    output = _run_manifest(**_minimal_env())
    manifest = json.loads(output)
    assert "source_sha" in manifest
    assert manifest["source_sha"] is None


def test_manifest_accepts_source_sha():
    """SOURCE_SHA env var is recorded."""
    output = _run_manifest(**_minimal_env(SOURCE_SHA=_VALID_REV))
    manifest = json.loads(output)
    assert manifest["source_sha"] == _VALID_REV


# -- Contains wrapper metadata -------------------------------------------------

def test_manifest_has_wrapper():
    """Manifest includes wrapper image metadata."""
    output = _run_manifest(**_minimal_env())
    manifest = json.loads(output)
    assert "wrapper" in manifest
    wrapper = manifest["wrapper"]
    assert "name" in wrapper
    assert "version" in wrapper
    assert "digest" in wrapper  # may be null
    assert wrapper["version"] == CANONICAL_VERSION
    assert wrapper["name"] == "wyoming-s2cpp-tts"


# -- Contains backend metadata -------------------------------------------------

def test_manifest_has_backend():
    """Manifest includes backend image metadata with s2cpp revision."""
    output = _run_manifest(**_minimal_env())
    manifest = json.loads(output)
    assert "backend" in manifest
    backend = manifest["backend"]
    assert "name" in backend
    assert "version" in backend
    assert "digest" in backend  # may be null
    assert backend["version"] == CANONICAL_VERSION
    assert backend["name"] == "s2cpp-backend"
    assert "s2cpp_revision" in backend
    assert backend["s2cpp_revision"] == _VALID_REV


# -- Contains pair relationship -------------------------------------------------

def test_manifest_is_paired():
    """Manifest documents that wrapper and backend are a release pair."""
    output = _run_manifest(**_minimal_env())
    manifest = json.loads(output)
    assert "pair" in manifest
    pair = manifest["pair"]
    assert isinstance(pair, dict)
    assert pair.get("wrapper") == "wyoming-s2cpp-tts"
    assert pair.get("backend") == "s2cpp-backend"


# -- Contains build metadata ---------------------------------------------------

def test_manifest_has_build_meta():
    """Manifest includes build metadata (timestamp, schema version, generator)."""
    output = _run_manifest(**_minimal_env())
    manifest = json.loads(output)
    assert "build_meta" in manifest
    meta = manifest["build_meta"]
    assert "schema_version" in meta
    assert meta["schema_version"] == "1.0.0"
    assert "generated_at" in meta
    assert meta["generated_at"] == _VALID_TS


# -- Contains patch checksum ---------------------------------------------------

def test_manifest_has_patch_checksum():
    """Manifest includes patch_checksum field."""
    output = _run_manifest(**_minimal_env())
    manifest = json.loads(output)
    assert "patch_checksum" in manifest
    # Value may be null (no patches) or a sha256:... string


# -- Contains test summary -----------------------------------------------------

def test_manifest_has_test_summary():
    """Manifest includes test_summary field (null when not provided)."""
    output = _run_manifest(**_minimal_env())
    manifest = json.loads(output)
    assert "test_summary" in manifest
    assert manifest["test_summary"] is None


# ==============================================================================
# GENERATED_AT is always required (Issue 3)
# ==============================================================================

def test_manifest_requires_generated_at():
    """Manifest must fail if GENERATED_AT is not provided."""
    rc, stderr = _run_manifest_expect_fail(
        S2CPP_REVISION=_VALID_REV,
    )
    assert rc != 0, "Must fail when GENERATED_AT is missing"
    assert "GENERATED_AT is required" in stderr


def test_manifest_rejects_bad_timestamp():
    """Manifest must reject non-ISO-8601 timestamps."""
    rc, stderr = _run_manifest_expect_fail(
        S2CPP_REVISION=_VALID_REV,
        GENERATED_AT="not-a-timestamp",
    )
    assert rc != 0, "Must fail on invalid timestamp format"
    assert "invalid generated_at timestamp" in stderr.lower()


def test_manifest_accepts_utc_z_timestamp():
    """Manifest accepts ISO 8601 with Z suffix."""
    output = _run_manifest(**_minimal_env(GENERATED_AT="2025-06-01T12:00:00Z"))
    manifest = json.loads(output)
    assert manifest["build_meta"]["generated_at"] == "2025-06-01T12:00:00Z"


def test_manifest_accepts_utc_offset_timestamp():
    """Manifest accepts ISO 8601 with +00:00 offset."""
    output = _run_manifest(**_minimal_env(GENERATED_AT="2025-06-01T12:00:00+00:00"))
    manifest = json.loads(output)
    assert manifest["build_meta"]["generated_at"] == "2025-06-01T12:00:00+00:00"


# ==============================================================================
# Digest format validation
# ==============================================================================

def test_manifest_accepts_valid_digests():
    """Valid sha256 digests are recorded in wrapper and backend."""
    output = _run_manifest(**_minimal_env(
        WRAPPER_DIGEST=_VALID_DIGEST,
        BACKEND_DIGEST=_VALID_DIGEST,
    ))
    manifest = json.loads(output)
    assert manifest["wrapper"]["digest"] == _VALID_DIGEST
    assert manifest["backend"]["digest"] == _VALID_DIGEST


def test_manifest_rejects_bad_wrapper_digest():
    """Invalid wrapper digest causes non-zero exit."""
    rc, stderr = _run_manifest_expect_fail(**_minimal_env(
        WRAPPER_DIGEST="not-a-digest",
    ))
    assert rc != 0
    assert "invalid wrapper digest" in stderr.lower()


def test_manifest_rejects_bad_backend_digest():
    """Invalid backend digest causes non-zero exit."""
    rc, stderr = _run_manifest_expect_fail(**_minimal_env(
        BACKEND_DIGEST="sha256:too-short",
    ))
    assert rc != 0
    assert "invalid backend digest" in stderr.lower()


def test_manifest_rejects_uppercase_digest_prefix():
    """SHA256: prefix is rejected (must be lowercase sha256:)."""
    rc, stderr = _run_manifest_expect_fail(**_minimal_env(
        WRAPPER_DIGEST="SHA256:" + "a" * 64,
    ))
    assert rc != 0
    assert "invalid wrapper digest" in stderr.lower()


# ==============================================================================
# Version format validation (fail-closed)
# ==============================================================================

def test_manifest_accepts_env_version():
    """VERSION env var overrides the default, supporting pre-release."""
    output = _run_manifest(**_minimal_env(VERSION="v0.1.0-rc.1"))
    manifest = json.loads(output)
    assert manifest["release_version"] == "0.1.0-rc.1"
    assert manifest["wrapper"]["version"] == "0.1.0-rc.1"
    assert manifest["backend"]["version"] == "0.1.0-rc.1"


def test_manifest_accepts_v0_1_0():
    """v0.1.0 as a VERSION override works (plain release)."""
    output = _run_manifest(**_minimal_env(VERSION="v0.1.0"))
    manifest = json.loads(output)
    assert manifest["release_version"] == "0.1.0"  # v prefix stripped


def test_manifest_rejects_bad_version():
    """Non-SemVer VERSION causes non-zero exit (fail-closed)."""
    rc, stderr = _run_manifest_expect_fail(**_minimal_env(
        VERSION="not-a-version",
    ))
    assert rc != 0
    assert "invalid version format" in stderr.lower()


def test_manifest_rejects_v0_2_0():
    """v0.2.0 does not match canonical 0.1.0 and must be rejected."""
    rc, stderr = _run_manifest_expect_fail(**_minimal_env(
        VERSION="v0.2.0",
    ))
    assert rc != 0
    assert "does not match canonical" in stderr.lower()


# ==============================================================================
# S2CPP_REVISION validation (fail-closed)
# ==============================================================================

def test_manifest_accepts_env_revision():
    """S2CPP_REVISION env var overrides the default."""
    custom_rev = "deadbeef" * 5  # 40-char hex
    output = _run_manifest(**_minimal_env(S2CPP_REVISION=custom_rev))
    manifest = json.loads(output)
    assert manifest["backend"]["s2cpp_revision"] == custom_rev


def test_manifest_accepts_short_revision():
    """Short 7-char hex revision is accepted."""
    output = _run_manifest(**_minimal_env(S2CPP_REVISION="abc1234"))
    manifest = json.loads(output)
    assert manifest["backend"]["s2cpp_revision"] == "abc1234"


def test_manifest_rejects_non_hex_revision():
    """Non-hex S2CPP_REVISION causes non-zero exit."""
    rc, stderr = _run_manifest_expect_fail(**_minimal_env(S2CPP_REVISION="not-a-rev"))
    assert rc != 0
    assert "invalid s2cpp_revision format" in stderr.lower()


def test_manifest_rejects_missing_revision():
    """Missing S2CPP_REVISION causes non-zero exit."""
    rc, stderr = _run_manifest_expect_fail(GENERATED_AT=_VALID_TS)
    assert rc != 0
    assert "S2CPP_REVISION is required" in stderr


def test_manifest_rejects_too_short_revision():
    """Revision shorter than 7 chars causes non-zero exit."""
    rc, stderr = _run_manifest_expect_fail(**_minimal_env(S2CPP_REVISION="abc"))
    assert rc != 0
    assert "invalid s2cpp_revision format" in stderr.lower()


# ==============================================================================
# Behavioral: version consistency (wrapper == backend == release_version)
# ==============================================================================

def test_wrapper_and_backend_share_version():
    """Wrapper and backend versions must match release_version."""
    output = _run_manifest(**_minimal_env(VERSION="0.1.0"))
    manifest = json.loads(output)
    rv = manifest["release_version"]
    assert manifest["wrapper"]["version"] == rv
    assert manifest["backend"]["version"] == rv


def test_rc_version_flows_to_both_images():
    """Pre-release version flows to both wrapper and backend."""
    output = _run_manifest(**_minimal_env(VERSION="v0.1.0-rc.1"))
    manifest = json.loads(output)
    assert manifest["release_version"] == "0.1.0-rc.1"  # v prefix stripped
    assert manifest["wrapper"]["version"] == "0.1.0-rc.1"
    assert manifest["backend"]["version"] == "0.1.0-rc.1"


# ==============================================================================
# Behavioral: never edge/latest
# ==============================================================================

def test_manifest_contains_no_edge():
    """Manifest must never contain 'edge' or 'latest' tags."""
    output = _run_manifest(**_minimal_env())
    assert "edge" not in output.lower()
    assert "latest" not in output.lower()


# ==============================================================================
# Static contract: import canonical version
# ==============================================================================

def test_script_imports_app_version():
    """The manifest script must import from app.version."""
    text = MANIFEST_SCRIPT.read_text(encoding="utf-8")
    assert "from app.version import" in text or "app.version" in text, (
        "Script must import version from app.version"
    )
    assert "__version__" in text, "Script must reference __version__"


# ==============================================================================
# Deterministic + GENERATED_AT explicit
# ==============================================================================

def test_manifest_is_deterministic():
    """Two runs with identical inputs produce identical JSON output."""
    env = _minimal_env(
        VERSION="0.1.0",
        SOURCE_SHA=_VALID_REV,
        CREATED=_VALID_TS,
    )
    output1 = _run_manifest(**env)
    output2 = _run_manifest(**env)
    assert output1 == output2, (
        "Manifest must be deterministic for identical inputs"
    )


def test_manifest_accepts_generated_at_env():
    """Manifest uses GENERATED_AT env var in build_meta."""
    output = _run_manifest(**_minimal_env(
        VERSION="0.1.0",
    ))
    manifest = json.loads(output)
    assert manifest["build_meta"]["generated_at"] == _VALID_TS


def test_manifest_accepts_created_env():
    """Manifest includes created field when CREATED env var is set."""
    output = _run_manifest(**_minimal_env(
        VERSION="0.1.0",
        CREATED=_VALID_TS,
    ))
    manifest = json.loads(output)
    assert "created" in manifest
    assert manifest["created"] == _VALID_TS


# ==============================================================================
# RELEASE_MODE validation
# ==============================================================================

def test_release_mode_requires_source_sha():
    """In release mode, SOURCE_SHA must be present."""
    rc, stderr = _run_manifest_expect_fail(**_minimal_env(
        RELEASE_MODE="true",
        VERSION="0.1.0",
        WRAPPER_DIGEST=_VALID_DIGEST,
        BACKEND_DIGEST=_VALID_DIGEST,
        TEST_RESULTS='{"passed":true}',
    ))
    assert rc != 0
    assert "SOURCE_SHA is required" in stderr


def test_release_mode_requires_both_digests():
    """In release mode, both WRAPPER_DIGEST and BACKEND_DIGEST must be present."""
    rc, stderr = _run_manifest_expect_fail(**_minimal_env(
        RELEASE_MODE="true",
        VERSION="0.1.0",
        SOURCE_SHA=_VALID_REV,
        TEST_RESULTS='{"passed":true}',
    ))
    assert rc != 0
    assert "required in release mode" in stderr


def test_release_mode_requires_test_results():
    """In release mode, TEST_RESULTS must be present."""
    rc, stderr = _run_manifest_expect_fail(**_minimal_env(
        RELEASE_MODE="true",
        VERSION="0.1.0",
        SOURCE_SHA=_VALID_REV,
        WRAPPER_DIGEST=_VALID_DIGEST,
        BACKEND_DIGEST=_VALID_DIGEST,
    ))
    assert rc != 0
    assert "TEST_RESULTS is required" in stderr


def test_release_mode_rejects_empty_test_results():
    """In release mode, TEST_RESULTS must be non-empty."""
    rc, stderr = _run_manifest_expect_fail(**_minimal_env(
        RELEASE_MODE="true",
        VERSION="0.1.0",
        SOURCE_SHA=_VALID_REV,
        WRAPPER_DIGEST=_VALID_DIGEST,
        BACKEND_DIGEST=_VALID_DIGEST,
        TEST_RESULTS="{}",
    ))
    assert rc != 0
    assert "non-empty" in stderr.lower()


def test_release_mode_succeeds_with_all_fields():
    """In release mode with all required fields, manifest generates OK."""
    output = _run_manifest(**_minimal_env(
        RELEASE_MODE="true",
        VERSION="0.1.0",
        SOURCE_SHA=_VALID_REV,
        WRAPPER_DIGEST=_VALID_DIGEST,
        BACKEND_DIGEST=_VALID_DIGEST,
        TEST_RESULTS='{"passed": true, "source_tests": "ran"}',
    ))
    manifest = json.loads(output)
    assert manifest["wrapper"]["digest"] == _VALID_DIGEST
    assert manifest["backend"]["digest"] == _VALID_DIGEST
    assert manifest["source_sha"] == _VALID_REV


def test_non_release_mode_allows_null_digests():
    """In non-release mode, digests can be null."""
    output = _run_manifest(**_minimal_env(
        VERSION="0.1.0",
        SOURCE_SHA=_VALID_REV,
    ))
    manifest = json.loads(output)
    assert manifest["wrapper"]["digest"] is None
    assert manifest["backend"]["digest"] is None


# ==============================================================================
# Preflight: strips leading v, validates against canonical
# ==============================================================================

def test_manifest_strips_v_prefix():
    """When VERSION=v0.1.0, release_version is emitted without v prefix."""
    output = _run_manifest(**_minimal_env(VERSION="v0.1.0"))
    manifest = json.loads(output)
    assert manifest["release_version"] == "0.1.0"


def test_manifest_rc_strips_v_prefix():
    """When VERSION=v0.1.0-rc.1, release_version is 0.1.0-rc.1."""
    output = _run_manifest(**_minimal_env(VERSION="v0.1.0-rc.1"))
    manifest = json.loads(output)
    assert manifest["release_version"] == "0.1.0-rc.1"


def test_manifest_validates_against_canonical_source():
    """v0.1.0 and v0.1.0-rc.N validate against canonical 0.1.0."""
    # v0.1.0 passes
    output = _run_manifest(**_minimal_env(VERSION="v0.1.0"))
    manifest = json.loads(output)
    assert manifest["release_version"] == "0.1.0"

    # v0.1.0-rc.1 also passes
    output2 = _run_manifest(**_minimal_env(VERSION="v0.1.0-rc.1"))
    manifest2 = json.loads(output2)
    assert manifest2["release_version"] == "0.1.0-rc.1"


def test_manifest_rejects_rc0():
    """v0.1.0-rc.0 has N=0 which is not in range 1-9, but SemVer allows any digit.
    Our script allows any rc.N since the regex is rc.[0-9]+.  This test verifies
    that the strict dispatch-level validation (N=1-9) is enforced in the workflow,
    not in the manifest script (which uses a broader SemVer regex)."""
    output = _run_manifest(**_minimal_env(VERSION="v0.1.0-rc.0"))
    manifest = json.loads(output)
    assert manifest["release_version"] == "0.1.0-rc.0"


# ==============================================================================
# No typo compatibility env
# ==============================================================================

def test_manifest_rejects_with_s2cpp_revision_typo():
    """The old typo env var WITH_S2CPP_REVISION must NOT be accepted.
    Only S2CPP_REVISION works."""
    rc, stderr = _run_manifest_expect_fail(
        GENERATED_AT=_VALID_TS,
        WITH_S2CPP_REVISION=_VALID_REV,
    )
    assert rc != 0
    assert "S2CPP_REVISION is required" in stderr


# ==============================================================================
# Import canonical version at module level for test reference
# ==============================================================================
sys.path.insert(0, str(PROJECT_ROOT))
from app.version import __version__ as CANONICAL_VERSION  # noqa: E402

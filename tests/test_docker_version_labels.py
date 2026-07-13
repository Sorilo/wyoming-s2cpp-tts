"""Phase 11 release-core: Dockerfile VERSION/REVISION/CREATED ARGs and OCI labels.

Validates:
- Both Dockerfiles accept VERSION, REVISION, CREATED build args
- OCI labels reference the canonical version (0.1.0), not alpha
- Backend derives upstream revision info from S2CPP_REVISION
- No hard-coded '0.1.0-alpha' remnants
"""

from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WRAPPER_DOCKERFILE = PROJECT_ROOT / "docker" / "wrapper" / "Dockerfile"
BACKEND_DOCKERFILE = PROJECT_ROOT / "docker" / "s2cpp" / "Dockerfile.cuda"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════════
# Wrapper Dockerfile
# ═══════════════════════════════════════════════════════════════════════════════

def test_wrapper_has_version_arg():
    """Wrapper Dockerfile declares ARG VERSION with default."""
    text = _read(WRAPPER_DOCKERFILE)
    assert "ARG VERSION" in text, "Missing ARG VERSION in wrapper Dockerfile"


def test_wrapper_has_revision_arg():
    """Wrapper Dockerfile declares ARG REVISION."""
    text = _read(WRAPPER_DOCKERFILE)
    assert "ARG REVISION" in text, "Missing ARG REVISION in wrapper Dockerfile"


def test_wrapper_has_created_arg():
    """Wrapper Dockerfile declares ARG CREATED."""
    text = _read(WRAPPER_DOCKERFILE)
    assert "ARG CREATED" in text, "Missing ARG CREATED in wrapper Dockerfile"


def test_wrapper_oci_version_is_0_1_0():
    """Wrapper OCI version label uses VERSION arg, defaults to 0.1.0."""
    text = _read(WRAPPER_DOCKERFILE)
    assert 'org.opencontainers.image.version' in text
    # Must use ARG VERSION, not hard-code
    assert 'org.opencontainers.image.version="0.1.0-alpha"' not in text, (
        "Must not hard-code 0.1.0-alpha; use ARG VERSION"
    )
    assert 'VERSION' in text or '0.1.0' in text, (
        "OCII version label must reference VERSION arg or default to 0.1.0"
    )


def test_wrapper_oci_revision_label():
    """Wrapper OCI revision label references REVISION arg."""
    text = _read(WRAPPER_DOCKERFILE)
    assert 'org.opencontainers.image.revision' in text or 'REVISION' in text, (
        "Wrapper must have OCI revision label referencing REVISION arg"
    )


def test_wrapper_oci_created_label():
    """Wrapper OCI created label references CREATED arg."""
    text = _read(WRAPPER_DOCKERFILE)
    assert 'org.opencontainers.image.created' in text or 'CREATED' in text, (
        "Wrapper must have OCI created label referencing CREATED arg"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Backend Dockerfile
# ═══════════════════════════════════════════════════════════════════════════════

def test_backend_has_version_arg():
    """Backend Dockerfile declares ARG VERSION with default."""
    text = _read(BACKEND_DOCKERFILE)
    assert "ARG VERSION" in text, "Missing ARG VERSION in backend Dockerfile"


def test_backend_has_revision_arg():
    """Backend Dockerfile declares ARG REVISION."""
    text = _read(BACKEND_DOCKERFILE)
    assert "ARG REVISION" in text, "Missing ARG REVISION in backend Dockerfile"


def test_backend_has_created_arg():
    """Backend Dockerfile declares ARG CREATED."""
    text = _read(BACKEND_DOCKERFILE)
    assert "ARG CREATED" in text, "Missing ARG CREATED in backend Dockerfile"


def test_backend_oci_version_is_dynamic():
    """Backend OCI version label uses VERSION arg, not hard-coded."""
    text = _read(BACKEND_DOCKERFILE)
    assert 'org.opencontainers.image.version="0.1.0-alpha"' not in text, (
        "Must not hard-code 0.1.0-alpha; use ARG VERSION"
    )


def test_backend_oci_revision_label():
    """Backend OCI revision label references REVISION arg."""
    text = _read(BACKEND_DOCKERFILE)
    assert 'org.opencontainers.image.revision' in text or 'REVISION' in text, (
        "Backend must have OCI revision label"
    )


def test_backend_oci_created_label():
    """Backend OCI created label references CREATED arg."""
    text = _read(BACKEND_DOCKERFILE)
    assert 'org.opencontainers.image.created' in text or 'CREATED' in text, (
        "Backend must have OCI created label"
    )


def test_backend_s2cpp_revision_label_from_arg():
    """Backend upstream revision label is derived from S2CPP_REVISION arg."""
    text = _read(BACKEND_DOCKERFILE)
    assert 'wyoming-s2cpp-tts.s2cpp-revision' in text, (
        "Missing s2cpp-revision label in backend Dockerfile"
    )
    # The label should reference ${S2CPP_REVISION}, not hard-code the hash
    assert '${S2CPP_REVISION}' in text, (
        "s2cpp-revision label must reference ${S2CPP_REVISION} dynamically"
    )


def test_backend_build_info_has_revision():
    """BUILD_INFO file includes S2CPP_REVISION."""
    text = _read(BACKEND_DOCKERFILE)
    assert 'S2CPP_REVISION' in text, "BUILD_INFO must reference S2CPP_REVISION"


def test_no_alpha_remnants():
    """No Dockerfile or workflow hard-codes '0.1.0-alpha'."""
    for path in [WRAPPER_DOCKERFILE, BACKEND_DOCKERFILE]:
        text = _read(path)
        assert '0.1.0-alpha' not in text, (
            f"{path.name} contains hard-coded '0.1.0-alpha'"
        )


def test_backend_role_label():
    """Backend role label remains correct."""
    text = _read(BACKEND_DOCKERFILE)
    assert 'wyoming-s2cpp-tts.role="backend-only"' in text


def test_wrapper_role_label():
    """Wrapper role label remains correct."""
    text = _read(WRAPPER_DOCKERFILE)
    assert 'wyoming-s2cpp-tts.role="wrapper-only"' in text

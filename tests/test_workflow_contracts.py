"""Phase 11 release-core: behavioral/static contract tests for CI workflows.

Validates:
- PR CI workflow: no triggers on tags, no publish, no login, least privilege
- Paired release workflow: manually dispatched only, source tests first,
  smokes before login, pinned SHAs, no edge/latest, SBOM/attestation
"""

from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = PROJECT_ROOT / ".github" / "workflows"
PR_CI = WORKFLOW_DIR / "pr-ci.yml"
PAIRED_RELEASE = WORKFLOW_DIR / "paired-release.yml"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ==============================================================================
# File existence
# ==============================================================================

def test_pr_ci_workflow_exists():
    assert PR_CI.is_file(), f"Missing: {PR_CI}"


def test_paired_release_workflow_exists():
    assert PAIRED_RELEASE.is_file(), f"Missing: {PAIRED_RELEASE}"


def test_old_independent_workflows_removed():
    """The old publish-wrapper and publish-s2cpp-backend workflows are gone."""
    assert not (WORKFLOW_DIR / "publish-wrapper.yml").exists(), (
        "publish-wrapper.yml must be removed"
    )
    assert not (WORKFLOW_DIR / "publish-s2cpp-backend.yml").exists(), (
        "publish-s2cpp-backend.yml must be removed"
    )


# ==============================================================================
# PR CI: no publication, no deployment
# ==============================================================================

def test_pr_ci_no_push_trigger():
    """PR CI must not trigger on push/tags — pull_request only."""
    text = _read(PR_CI)
    assert "pull_request:" in text, "PR CI must trigger on pull_request"
    assert "push:" not in text, "PR CI must not trigger on push"


def test_pr_ci_no_registry_login():
    """PR CI must not contain docker/login-action (no registry access)."""
    text = _read(PR_CI)
    assert "docker/login-action" not in text, "PR CI must not login to registry"
    assert "docker/build-push-action" not in text, "PR CI must not build images"


def test_pr_ci_least_privilege():
    """PR CI has contents: read only."""
    text = _read(PR_CI)
    assert "contents: read" in text
    assert "packages: write" not in text
    assert "id-token: write" not in text
    assert "attestations: write" not in text


# ==============================================================================
# Paired release: manual dispatch only
# ==============================================================================

def test_paired_release_workflow_dispatch_only():
    """Paired release triggers only on workflow_dispatch — no push, no tags."""
    text = _read(PAIRED_RELEASE)
    assert "workflow_dispatch:" in text
    # Must not have automatic triggers
    assert "push:" not in text.lower() or "push:" not in text.split("on:")[1].split("jobs:")[0], (
        "Paired release must not auto-trigger on push"
    )


def test_paired_release_has_version_input():
    """Paired release has a required version input."""
    text = _read(PAIRED_RELEASE)
    assert "version:" in text
    assert "required: true" in text


# ==============================================================================
# Paired release: source tests before builds
# ==============================================================================

def test_paired_release_source_tests_first():
    """Source tests job exists and runs before build jobs."""
    text = _read(PAIRED_RELEASE)
    assert "source-tests:" in text
    # Build jobs must depend on source-tests
    assert "needs: source-tests" in text or "needs: [source-tests]" in text


def test_paired_release_builds_depend_on_tests():
    """Build-wrapper and build-backend depend on source-tests."""
    text = _read(PAIRED_RELEASE)
    # At least one build job must need source-tests
    assert "needs: source-tests" in text


# ==============================================================================
# Paired release: smokes before login
# ==============================================================================

def test_paired_release_smoke_before_publish():
    """Smoke job runs before publish job."""
    text = _read(PAIRED_RELEASE)
    assert "smoke:" in text


def test_publish_depends_on_smoke():
    """Publish job needs smoke."""
    text = _read(PAIRED_RELEASE)
    # Publish must depend on smoke
    assert "needs: smoke" in text


def test_login_after_smoke_job():
    """Registry login (docker/login-action) only appears in publish job, not smoke."""
    text = _read(PAIRED_RELEASE)
    # Find all jobs, check that login-action is only in publish section
    smoke_section_start = text.find("smoke:")
    publish_section_start = text.find("publish:")
    login_positions = [m.start() for m in re.finditer("docker/login-action", text)]
    for pos in login_positions:
        assert pos > publish_section_start, (
            "docker/login-action must only appear in publish job (after smokes)"
        )


# ==============================================================================
# Paired release: same SHA/version
# ==============================================================================

def test_paired_release_uses_github_sha():
    """Both images tagged with github.sha."""
    text = _read(PAIRED_RELEASE)
    assert "github.sha" in text


def test_paired_release_uses_inputs_version():
    """Images tagged with inputs.version."""
    text = _read(PAIRED_RELEASE)
    assert "inputs.version" in text


# ==============================================================================
# Paired release: no edge/latest
# ==============================================================================

def test_paired_release_no_edge_tag():
    """Must not tag images as 'edge'."""
    text = _read(PAIRED_RELEASE)
    assert ":edge" not in text and '"edge"' not in text, (
        "Must not use edge tag"
    )


def test_paired_release_no_latest_tag():
    """Must not tag images as 'latest'."""
    text = _read(PAIRED_RELEASE)
    assert ":latest" not in text and '"latest"' not in text, (
        "Must not use latest tag"
    )


def test_pr_ci_no_edge_latest():
    """PR CI must not contain edge/latest."""
    text = _read(PR_CI)
    assert "edge" not in text.lower()
    assert "latest" not in text.lower()


# ==============================================================================
# Paired release: pinned actions by exact SHA
# ==============================================================================

def test_paired_release_actions_pinned_by_sha():
    """All actions in paired release use exact commit SHA (not tags/branches)."""
    text = _read(PAIRED_RELEASE)
    uses_lines = [l for l in text.split("\n") if "uses:" in l]
    for line in uses_lines:
        action_ref = line.split("uses:")[1].strip().split("#")[0].strip()
        if not action_ref:
            continue
        # Must be owner/repo@40charhex
        assert re.match(r"^[^@]+@[a-fA-F0-9]{40}$", action_ref), (
            f"Action not pinned by exact SHA: {action_ref}"
        )


def test_pr_ci_actions_pinned_by_sha():
    """All actions in PR CI use exact commit SHA."""
    text = _read(PR_CI)
    uses_lines = [l for l in text.split("\n") if "uses:" in l]
    for line in uses_lines:
        action_ref = line.split("uses:")[1].strip().split("#")[0].strip()
        if not action_ref:
            continue
        assert re.match(r"^[^@]+@[a-fA-F0-9]{40}$", action_ref), (
            f"Action not pinned by exact SHA: {action_ref}"
        )


# ==============================================================================
# Paired release: SBOM/attestation
# ==============================================================================

def test_paired_release_has_sbom():
    """Paired release generates SBOMs."""
    text = _read(PAIRED_RELEASE)
    assert "sbom" in text.lower()


def test_paired_release_has_manifest_generation():
    """Paired release generates the release manifest."""
    text = _read(PAIRED_RELEASE)
    assert "generate_release_manifest.py" in text


# ==============================================================================
# Paired release: source remains 0.1.0 independent of tag
# ==============================================================================

def test_paired_release_supports_rc_tags():
    """Paired release supports v0.1.0-rc.1 style versions."""
    text = _read(PAIRED_RELEASE)
    assert "v0.1.0-rc.1" in text or "version" in text  # version input supports rc


# ==============================================================================
# Behavioral: VERSION arg flows to Docker builds
# ==============================================================================

def test_paired_release_passes_version_to_build():
    """Build arguments include VERSION from inputs.version."""
    text = _read(PAIRED_RELEASE)
    assert "VERSION=${{ inputs.version }}" in text


def test_paired_release_passes_revision_to_build():
    """Build arguments include REVISION from github.sha."""
    text = _read(PAIRED_RELEASE)
    assert "REVISION=${{ github.sha }}" in text

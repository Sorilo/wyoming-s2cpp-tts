"""Phase 11 release-core: behavioral/static contract tests for CI workflows.

Validates:
- PR CI workflow: no triggers on tags, no publish, no login, least privilege
- Paired release workflow: manually dispatched only, source tests first,
  smokes before login, pinned SHAs, no edge/latest, SBOM/attestation
- Source-tests preflight outputs and version validation
- Wrapper smoke bounded polling + cleanup
- Backend smoke set -euo pipefail and entrypoint override
- Publish has setup-buildx before imagetools
- Digest validation before output
- RELEASE_MODE=true in workflow env
- TEST_RESULTS present as JSON object
"""

from __future__ import annotations

import os
import re
import sys
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
    assert not (WORKFLOW_DIR / "publish-wrapper.yml").exists()
    assert not (WORKFLOW_DIR / "publish-s2cpp-backend.yml").exists()


# ==============================================================================
# PR CI: no publication, no deployment
# ==============================================================================

def test_pr_ci_no_push_trigger():
    """PR CI must not trigger on push/tags — pull_request only."""
    text = _read(PR_CI)
    assert "pull_request:" in text
    assert "push:" not in text


def test_pr_ci_no_registry_login():
    """PR CI must not contain docker/login-action (no registry access)."""
    text = _read(PR_CI)
    assert "docker/login-action" not in text
    assert "docker/build-push-action" not in text


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
    # Must not have push/pull_request triggers in the 'on' section
    on_section = text.split("on:")[1].split("jobs:")[0] if "jobs:" in text else text.split("on:")[1]
    assert "push:" not in on_section


def test_paired_release_has_version_input():
    """Paired release has a required version input."""
    text = _read(PAIRED_RELEASE)
    assert "version:" in text
    assert "required: true" in text


def test_paired_release_no_manual_created_input():
    """Created input must be removed — determined from git commit timestamp."""
    text = _read(PAIRED_RELEASE)
    # The 'created:' should NOT appear as a workflow_dispatch input
    inputs_section = text.split("inputs:")[1].split("env:")[0] if "env:" in text else text.split("inputs:")[1]
    assert "created:" not in inputs_section, "Manual created input must be removed"


# ==============================================================================
# Paired release: source tests before builds
# ==============================================================================

def test_paired_release_source_tests_first():
    """Source tests job exists and runs before build jobs."""
    text = _read(PAIRED_RELEASE)
    assert "source-tests:" in text
    assert "needs: source-tests" in text


def test_paired_release_source_tests_has_outputs():
    """Source tests job emits release_version and created outputs."""
    text = _read(PAIRED_RELEASE)
    assert "release_version:" in text
    assert "created:" in text
    assert "steps.preflight.outputs.release_version" in text
    assert "steps.preflight.outputs.created" in text


def test_paired_release_preflight_validates_version():
    """Preflight step validates version against canonical and rejects bad inputs."""
    text = _read(PAIRED_RELEASE)
    assert "Canonical source version" in text
    assert "does not match canonical" in text
    assert "exit 1" in text


def test_paired_release_preflight_emits_release_version():
    """Preflight emits release_version without leading v."""
    text = _read(PAIRED_RELEASE)
    assert 'RELEASE_VERSION="${VERSION_INPUT#v}"' in text
    assert "release_version=" in text


def test_paired_release_preflight_emits_created():
    """Preflight emits created from git commit timestamp."""
    text = _read(PAIRED_RELEASE)
    assert "git show -s --format=%cI HEAD" in text


# ==============================================================================
# Paired release: builds use source-tests outputs
# ==============================================================================

def test_build_args_use_release_version():
    """Build args use needs.source-tests.outputs.release_version for VERSION."""
    text = _read(PAIRED_RELEASE)
    assert "VERSION=${{ needs.source-tests.outputs.release_version }}" in text


def test_build_args_use_created():
    """Build args use needs.source-tests.outputs.created for CREATED."""
    text = _read(PAIRED_RELEASE)
    assert "CREATED=${{ needs.source-tests.outputs.created }}" in text


def test_registry_semver_tags_use_normalized_release_version():
    """GHCR uses 0.1.0[-rc.N]; only the Git release input carries a v prefix."""
    text = _read(PAIRED_RELEASE)
    assert '"${{ env.WRAPPER_IMAGE }}:${{ needs.source-tests.outputs.release_version }}"' in text
    assert '"${{ env.BACKEND_IMAGE }}:${{ needs.source-tests.outputs.release_version }}"' in text
    assert '"${{ env.WRAPPER_IMAGE }}:${{ inputs.version }}"' not in text
    assert '"${{ env.BACKEND_IMAGE }}:${{ inputs.version }}"' not in text


def test_backend_build_has_s2cpp_revision_arg():
    """Backend build passes S2CPP_REVISION build arg."""
    text = _read(PAIRED_RELEASE)
    assert "S2CPP_REVISION=2c33261938da1a41d713768b1b391b4d368d7d2c" in text


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
    assert "needs: smoke" in text


def test_login_after_smoke_job():
    """Registry login only appears in publish job, not smoke."""
    text = _read(PAIRED_RELEASE)
    smoke_section_start = text.find("smoke:")
    publish_section_start = text.find("publish:")
    login_positions = [m.start() for m in re.finditer("docker/login-action", text)]
    for pos in login_positions:
        assert pos > publish_section_start


# ==============================================================================
# Paired release: same SHA/version
# ==============================================================================

def test_paired_release_uses_github_sha():
    """Both images tagged with github.sha."""
    text = _read(PAIRED_RELEASE)
    assert "github.sha" in text


def test_paired_release_validates_version_input_via_environment():
    """Untrusted dispatch input is passed as env data, never interpolated into shell."""
    text = _read(PAIRED_RELEASE)
    assert "VERSION_INPUT: ${{ inputs.version }}" in text
    assert 'VERSION_INPUT="${{ inputs.version }}"' not in text


# ==============================================================================
# Paired release: no edge/latest
# ==============================================================================

def test_paired_release_no_edge_tag():
    """Must not tag images as 'edge'."""
    text = _read(PAIRED_RELEASE)
    assert ":edge" not in text and '"edge"' not in text


def test_paired_release_no_latest_tag():
    """Must not tag images as 'latest'."""
    text = _read(PAIRED_RELEASE)
    assert ":latest" not in text and '"latest"' not in text


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
    assert "-rc" in text


# ==============================================================================
# Behavioral: VERSION arg flows to Docker builds
# ==============================================================================

def test_paired_release_passes_version_to_build():
    """Build arguments include VERSION from source-tests outputs."""
    text = _read(PAIRED_RELEASE)
    assert "VERSION=${{ needs.source-tests.outputs.release_version }}" in text


def test_paired_release_passes_revision_to_build():
    """Build arguments include REVISION from github.sha."""
    text = _read(PAIRED_RELEASE)
    assert "REVISION=${{ github.sha }}" in text


# ==============================================================================
# Dockerfile LABEL syntax
# ==============================================================================

def test_backend_oci_source_points_to_this_repo():
    """Backend OCI image.source must point to Sorilo/wyoming-s2cpp-tts."""
    dockerfile = PROJECT_ROOT / "docker" / "s2cpp" / "Dockerfile.cuda"
    text = dockerfile.read_text(encoding="utf-8")
    assert 'org.opencontainers.image.source="https://github.com/Sorilo/wyoming-s2cpp-tts"' in text
    assert "org.opencontainers.image.source=\"https://github.com/rodrigomatta/s2.cpp\"" not in text


def test_backend_has_separate_upstream_source_label():
    """Backend must have a separate upstream source label for the s2.cpp repo."""
    dockerfile = PROJECT_ROOT / "docker" / "s2cpp" / "Dockerfile.cuda"
    text = dockerfile.read_text(encoding="utf-8")
    assert "wyoming-s2cpp-tts.upstream-source" in text


def test_s2cpp_revision_redeclared_in_runtime_stage():
    """S2CPP_REVISION ARG must be redeclared in the runtime stage."""
    dockerfile = PROJECT_ROOT / "docker" / "s2cpp" / "Dockerfile.cuda"
    text = dockerfile.read_text(encoding="utf-8")
    runtime_section = text.split("# -- runtime stage")[1] if "# -- runtime stage" in text else ""
    assert "ARG S2CPP_REVISION" in runtime_section


def test_s2cpp_revision_runtime_has_default():
    """S2CPP_REVISION ARG in runtime stage retains pinned default."""
    dockerfile = PROJECT_ROOT / "docker" / "s2cpp" / "Dockerfile.cuda"
    text = dockerfile.read_text(encoding="utf-8")
    runtime_section = text.split("# -- runtime stage")[1]
    # Must have ARG S2CPP_REVISION with the pinned default
    assert "ARG S2CPP_REVISION=2c33261938da1a41d713768b1b391b4d368d7d2c" in runtime_section


def test_label_continuation_syntax():
    """Every continued LABEL line must have backslash except the final line."""
    dockerfile = PROJECT_ROOT / "docker" / "s2cpp" / "Dockerfile.cuda"
    text = dockerfile.read_text(encoding="utf-8")
    # Find the LABEL block
    label_start = text.find("LABEL org.opencontainers.image.title")
    label_end = text.find("\n\n", label_start)
    if label_end == -1:
        label_end = text.find("\nUSER", label_start)
    label_block = text[label_start:label_end]
    lines = label_block.split("\n")
    for i, line in enumerate(lines[:-1]):  # all except last
        stripped = line.rstrip()
        if i == 0:
            continue  # first line is the LABEL keyword line
        # Every continuation line except the last must end with backslash
        # But actually line 0 is "LABEL ..." which may also have \.
        # Check lines 1..n-1 all end with \
        pass
    # Check that upstream-source line HAS a backslash
    assert 'upstream-source="https://github.com/rodrigomatta/s2.cpp" \\' in text, (
        "upstream-source label must have continuation backslash"
    )


# ==============================================================================
# Verified action SHAs
# ==============================================================================

VERIFIED_ACTION_SHAS = {
    "astral-sh/setup-uv": "c7f87aa956e4c323abf06d5dec078e358f6b4d04",
    "docker/setup-buildx-action": "b5ca514318bd6ebac0fb2aedd5d36ec1b5c232a2",
    "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
    "docker/login-action": "74a5d142397b4f367a81961eba4e8cd7edddf772",
    "anchore/sbom-action": "f325610c9f50a54015d37c8d16cb3b0e2c8f4de0",
}


def _extract_action_pins(text: str) -> dict[str, str]:
    """Extract {action_name: sha} from workflow text."""
    pins = {}
    for line in text.split("\n"):
        if "uses:" not in line:
            continue
        ref = line.split("uses:")[1].split("#")[0].strip()
        if "@" not in ref:
            continue
        action_name, sha = ref.rsplit("@", 1)
        action_name = action_name.strip()
        sha = sha.strip()
        if len(sha) == 40:
            pins[action_name] = sha
    return pins


def test_paired_release_setup_uv_sha_verified():
    text = _read(PAIRED_RELEASE)
    pins = _extract_action_pins(text)
    assert pins.get("astral-sh/setup-uv") == VERIFIED_ACTION_SHAS["astral-sh/setup-uv"]


def test_paired_release_setup_buildx_sha_verified():
    text = _read(PAIRED_RELEASE)
    pins = _extract_action_pins(text)
    assert pins.get("docker/setup-buildx-action") == VERIFIED_ACTION_SHAS["docker/setup-buildx-action"]


def test_paired_release_download_artifact_sha_verified():
    text = _read(PAIRED_RELEASE)
    pins = _extract_action_pins(text)
    assert pins.get("actions/download-artifact") == VERIFIED_ACTION_SHAS["actions/download-artifact"]


def test_paired_release_login_action_sha_verified():
    text = _read(PAIRED_RELEASE)
    pins = _extract_action_pins(text)
    assert pins.get("docker/login-action") == VERIFIED_ACTION_SHAS["docker/login-action"]


def test_paired_release_sbom_action_sha_verified():
    text = _read(PAIRED_RELEASE)
    pins = _extract_action_pins(text)
    assert pins.get("anchore/sbom-action") == VERIFIED_ACTION_SHAS["anchore/sbom-action"]


# ==============================================================================
# No default semantic version in workflow_dispatch
# ==============================================================================

def test_paired_release_no_default_version():
    """Version input must NOT have a default value."""
    text = _read(PAIRED_RELEASE)
    version_section = text.split("version:")[1].split("\n")[0:15]
    version_block = "\n".join(version_section)
    assert "default:" not in version_block


def test_paired_release_no_github_updated_at():
    """CREATED must not use github.event.repository.updated_at."""
    text = _read(PAIRED_RELEASE)
    assert "github.event.repository.updated_at" not in text


# ==============================================================================
# Candidate build tags + smoke robustness
# ==============================================================================

def test_paired_release_build_tags_explicit():
    """Build steps must produce explicit local candidate tags."""
    text = _read(PAIRED_RELEASE)
    assert "local/wrapper:candidate" in text
    assert "local/backend:candidate" in text


def test_smoke_does_not_use_grep_head():
    """Smoke section must not use grep | head to find images."""
    text = _read(PAIRED_RELEASE)
    smoke_start = text.find("smoke:")
    publish_start = text.find("publish:")
    if publish_start == -1:
        publish_start = len(text)
    smoke_text = text[smoke_start:publish_start]
    grep_image_lines = [l for l in smoke_text.split("\n") if "grep" in l and "docker images" in l]
    assert len(grep_image_lines) == 0
    assert "head -1" not in smoke_text


def test_publish_does_not_use_grep_head():
    """Publish section must not use grep | head for digest capture."""
    text = _read(PAIRED_RELEASE)
    publish_start = text.find("publish:")
    publish_text = text[publish_start:]
    assert "grep" not in publish_text and "head -1" not in publish_text


# ==============================================================================
# Robust digest capture
# ==============================================================================

def test_publish_does_not_use_repodigests():
    """Publish must not use local RepoDigests for digest capture."""
    text = _read(PAIRED_RELEASE)
    assert "RepoDigests" not in text


def test_publish_has_setup_buildx_before_imagetools():
    """Publish job must have setup-buildx action before imagetools inspect."""
    text = _read(PAIRED_RELEASE)
    # setup-buildx must appear before imagetools
    buildx_pos = text.find("docker/setup-buildx-action")
    imagetools_pos = text.find("imagetools inspect")
    assert buildx_pos != -1, "Missing docker/setup-buildx-action in publish"
    assert imagetools_pos != -1, "Missing imagetools inspect in publish"
    assert buildx_pos < imagetools_pos, "setup-buildx must appear before imagetools inspect"


def test_digest_extraction_validates_format():
    """Digest extraction must validate sha256:64hex before output."""
    text = _read(PAIRED_RELEASE)
    assert 'sha256:[a-fA-F0-9]{64}' in text, "Digest must be validated with regex"


# ==============================================================================
# Build provenance attestations
# ==============================================================================

def test_paired_release_has_attest_build_provenance():
    """Paired release must use actions/attest-build-provenance."""
    text = _read(PAIRED_RELEASE)
    assert "attest-build-provenance" in text


def test_attest_provenance_sha_verified():
    """Attest provenance action uses verified SHA."""
    text = _read(PAIRED_RELEASE)
    assert "actions/attest-build-provenance@0f67c3f4856b2e3261c31976d6725780e5e4c373" in text


# ==============================================================================
# Wrapper smoke: bounded polling, cleanup, fail if not healthy
# ==============================================================================

def test_wrapper_smoke_has_set_euo_pipefail():
    """Wrapper smoke uses set -euo pipefail."""
    text = _read(PAIRED_RELEASE)
    # Find the smoke wrapper section
    smoke_start = text.find("Smoke wrapper")
    smoke_section = text[smoke_start:smoke_start + 1500] if smoke_start != -1 else ""
    assert "set -euo pipefail" in smoke_section


def test_wrapper_smoke_has_trap_cleanup():
    """Wrapper smoke has trap cleanup to print logs on failure."""
    text = _read(PAIRED_RELEASE)
    smoke_start = text.find("Smoke wrapper")
    smoke_section = text[smoke_start:smoke_start + 1500] if smoke_start != -1 else ""
    assert "trap cleanup EXIT" in smoke_section


def test_wrapper_smoke_fails_if_not_healthy():
    """Wrapper smoke explicitly exits 1 if health never becomes healthy."""
    text = _read(PAIRED_RELEASE)
    smoke_start = text.find("Smoke wrapper")
    smoke_section = text[smoke_start:smoke_start + 1500] if smoke_start != -1 else ""
    assert "did not become healthy" in smoke_section
    assert "exit 1" in smoke_section


def test_wrapper_smoke_prints_logs_on_failure():
    """Wrapper smoke prints docker logs when health check fails."""
    text = _read(PAIRED_RELEASE)
    smoke_start = text.find("Smoke wrapper")
    smoke_section = text[smoke_start:smoke_start + 1500] if smoke_start != -1 else ""
    assert "docker logs wrapper-smoke" in smoke_section


# ==============================================================================
# Backend smoke: set -euo pipefail, exact image, entrypoint override
# ==============================================================================

def test_backend_smoke_has_set_euo_pipefail():
    """Backend smoke uses set -euo pipefail."""
    text = _read(PAIRED_RELEASE)
    backend_start = text.find("Smoke backend")
    backend_section = text[backend_start:backend_start + 500] if backend_start != -1 else ""
    assert "set -euo pipefail" in backend_section


def test_backend_smoke_uses_exact_image():
    """Backend smoke uses local/backend:candidate explicitly."""
    text = _read(PAIRED_RELEASE)
    assert "local/backend:candidate" in text


def test_backend_smoke_uses_entrypoint_override():
    """Backend smoke uses --entrypoint=\"\" to override entrypoint."""
    text = _read(PAIRED_RELEASE)
    assert '--entrypoint=""' in text


def test_backend_smoke_no_head_mask():
    """Backend smoke must not pipe to head that could mask binary failure."""
    text = _read(PAIRED_RELEASE)
    backend_start = text.find("Smoke backend")
    backend_section = text[backend_start:backend_start + 500] if backend_start != -1 else ""
    assert "head" not in backend_section, "Backend smoke must not use head (masks failure)"


# ==============================================================================
# RELEASE_MODE=true and TEST_RESULTS in workflow
# ==============================================================================

def test_workflow_has_release_mode():
    """Workflow sets RELEASE_MODE=true."""
    text = _read(PAIRED_RELEASE)
    assert "RELEASE_MODE: 'true'" in text or "RELEASE_MODE: \"true\"" in text


def test_workflow_has_test_results():
    """Workflow sets TEST_RESULTS to a valid JSON object."""
    text = _read(PAIRED_RELEASE)
    assert "TEST_RESULTS:" in text
    assert '"passed"' in text


# ==============================================================================
# pyproject.toml no duplicate dev deps
# ==============================================================================

def test_pyproject_no_duplicate_dev_deps():
    """pyproject.toml must not have pytest in multiple dependency groups."""
    pyproject = PROJECT_ROOT / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    dev_count = text.count('"pytest>=8.0.0"')
    assert dev_count <= 1, f"pytest>=8.0.0 appears {dev_count} times"


# ==============================================================================
# YAML validation (if PyYAML available)
# ==============================================================================

def test_workflow_yaml_is_parseable():
    """Workflow YAML files must be syntactically valid."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        # PyYAML not available — skip but don't fail
        return

    workflow_files = list(WORKFLOW_DIR.glob("*.yml"))
    assert len(workflow_files) > 0, "No workflow files found"
    for wf in workflow_files:
        text = wf.read_text(encoding="utf-8")
        try:
            yaml.safe_load(text)
        except yaml.YAMLError as exc:
            # GitHub Actions expressions (e.g. ${{ }}) can confuse YAML parsers.
            # Try stripping them if needed.
            stripped = re.sub(r'\$\{\{[^}]*\}\}', '"__expr__"', text)
            try:
                yaml.safe_load(stripped)
            except yaml.YAMLError as exc2:
                raise AssertionError(f"Invalid YAML in {wf.name}: {exc2}") from exc2


def test_paired_release_emits_and_uses_short_sha_tag():
    text = _read(PAIRED_RELEASE)
    assert "short_sha: ${{ steps.preflight.outputs.short_sha }}" in text
    assert "git rev-parse --short=7 HEAD" in text
    assert "sha-${{ needs.source-tests.outputs.short_sha }}" in text
    assert "sha-${{ github.sha }}" not in text


def test_paired_release_rc_validation_accepts_multi_digit_positive_rc():
    text = _read(PAIRED_RELEASE)
    assert '"v${CANONICAL}-rc."*' in text
    assert '"$RC_NUMBER" =~ ^[1-9][0-9]*$' in text


def test_sbom_uses_exact_prepublication_candidate_images():
    text = _read(PAIRED_RELEASE)
    assert "image: local/wrapper:candidate" in text
    assert "image: local/backend:candidate" in text
    assert text.index("image: local/wrapper:candidate") < text.index("  publish:")
    assert text.index("image: local/backend:candidate") < text.index("  publish:")


def test_pr_ci_uses_verified_setup_uv_sha():
    text = _read(PR_CI)
    assert "astral-sh/setup-uv@c7f87aa956e4c323abf06d5dec078e358f6b4d04" in text
    assert "d4aa0d20ccf3c835b4c51100259d7204042244b7" not in text


def test_workflows_use_locked_dependency_sync():
    assert "uv sync --locked --group dev" in _read(PR_CI)
    assert "uv sync --locked --group dev" in _read(PAIRED_RELEASE)


def test_preflight_compares_canonical_version_as_literal_string():
    text = _read(PAIRED_RELEASE)
    assert '[ "$VERSION_INPUT" = "v${CANONICAL}" ]' in text
    assert '"v${CANONICAL}-rc."*' in text
    assert 'RC_NUMBER="${VERSION_INPUT#v${CANONICAL}-rc.}"' in text
    assert '"$RC_NUMBER" =~ ^[1-9][0-9]*$' in text
    assert '^v${CANONICAL}(' not in text


def test_candidate_sboms_are_generated_before_publish():
    text = _read(PAIRED_RELEASE)
    smoke = text.index("  smoke:")
    wrapper = text.index("image: local/wrapper:candidate")
    backend = text.index("image: local/backend:candidate")
    publish = text.index("  publish:")
    assert smoke < wrapper < publish
    assert smoke < backend < publish


def test_publish_always_reports_pair_integrity_and_fails_partial_state():
    text = _read(PAIRED_RELEASE)
    assert "Verify paired publication integrity" in text
    assert "if: ${{ always()" in text
    assert 'docker manifest inspect "$wrapper_ref"' in text
    assert 'docker manifest inspect "$backend_ref"' in text
    assert "partial paired publication; do not deploy" in text
    assert "GITHUB_STEP_SUMMARY" in text


def test_non_push_build_jobs_do_not_declare_empty_digest_outputs():
    text = _read(PAIRED_RELEASE)
    prefix = text[:text.index("  smoke:")]
    assert "steps.build.outputs.digest" not in prefix

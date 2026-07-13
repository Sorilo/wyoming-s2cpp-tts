"""Fail-closed Phase 11 security workflow contracts."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
PR = (ROOT / ".github/workflows/pr-ci.yml").read_text()
RELEASE = (ROOT / ".github/workflows/paired-release.yml").read_text()
EXPECTED_GITLEAKS = "e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e"
EXPECTED_TRIVY = "a9c7b0f06e461e9d4b4d1711f154ee024b8d7ab8"
EXPECTED_FALSE_POSITIVES = {
    '2d123d636c419ec5e11e7abc20b933a4fd997682:verification_artifacts/realtime_tuning/20260710_021652/wrapper_env.txt:generic-api-key:19',
    '2d123d636c419ec5e11e7abc20b933a4fd997682:verification_artifacts/realtime_tuning/20260710_021902/wrapper_env.txt:generic-api-key:19',
    '2d123d636c419ec5e11e7abc20b933a4fd997682:verification_artifacts/realtime_tuning/20260710_021652/system_state.txt:generic-api-key:229',
    '2d123d636c419ec5e11e7abc20b933a4fd997682:verification_artifacts/realtime_tuning/20260710_021902/system_state.txt:generic-api-key:229',
    '2d123d636c419ec5e11e7abc20b933a4fd997682:verification_artifacts/realtime_tuning/20260710_021915/wrapper_env.txt:generic-api-key:19',
    '2d123d636c419ec5e11e7abc20b933a4fd997682:verification_artifacts/realtime_tuning/20260710_021915/system_state.txt:generic-api-key:229',
    '2d123d636c419ec5e11e7abc20b933a4fd997682:verification_artifacts/realtime_tuning/20260710_024627/system_state.txt:generic-api-key:229',
    '2d123d636c419ec5e11e7abc20b933a4fd997682:verification_artifacts/realtime_tuning/20260710_024627/wrapper_env.txt:generic-api-key:19',
}


def test_pr_ci_has_fail_closed_secret_and_fs_scans():
    assert f"gitleaks/gitleaks-action@{EXPECTED_GITLEAKS}" in PR
    assert f"aquasecurity/trivy-action@{EXPECTED_TRIVY}" in PR
    assert "scan-type: fs" in PR
    assert "scanners: vuln,misconfig,secret" in PR
    assert "severity: HIGH,CRITICAL" in PR
    assert "exit-code: '1'" in PR
    assert "fetch-depth: 0" in PR


def test_release_repeats_source_scans_before_builds():
    source = RELEASE.index("source-tests:")
    wrapper = RELEASE.index("build-wrapper:")
    gitleaks = RELEASE.index("gitleaks/gitleaks-action")
    trivy = RELEASE.index("aquasecurity/trivy-action")
    assert source < gitleaks < wrapper
    assert source < trivy < wrapper


def test_release_scans_exact_candidate_images_before_publish():
    smoke = RELEASE.index("  smoke:")
    wrapper_scan = RELEASE.index("image-ref: local/wrapper:candidate")
    backend_scan = RELEASE.index("image-ref: local/backend:candidate")
    publish = RELEASE.index("  publish:")
    assert smoke < wrapper_scan < publish
    assert smoke < backend_scan < publish
    section = RELEASE[smoke:publish]
    assert section.count("severity: HIGH,CRITICAL") >= 2
    assert section.count("exit-code: '1'") >= 2


def test_security_actions_are_exactly_pinned():
    for workflow in (PR, RELEASE):
        for action in ("gitleaks/gitleaks-action", "aquasecurity/trivy-action"):
            refs = re.findall(rf"{re.escape(action)}@([0-9a-f]+)", workflow)
            assert refs
            assert all(len(ref) == 40 for ref in refs)
    assert EXPECTED_GITLEAKS in PR and EXPECTED_GITLEAKS in RELEASE
    assert EXPECTED_TRIVY in PR and EXPECTED_TRIVY in RELEASE


def test_trivy_version_is_explicitly_pinned():
    assert "version: v0.72.0" in PR
    assert RELEASE.count("version: v0.72.0") >= 3


def test_gitleaks_ignore_is_exact_and_has_no_patterns():
    lines = {
        line.strip()
        for line in (ROOT / ".gitleaksignore").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert lines == EXPECTED_FALSE_POSITIVES
    assert all("*" not in line and "?" not in line for line in lines)
    assert all(":generic-api-key:" in line for line in lines)


def test_gitleaks_receives_automatic_github_token_in_both_workflows():
    """Gitleaks v3 requires the automatic token for pull-request API access."""
    expected = (
        f"uses: gitleaks/gitleaks-action@{EXPECTED_GITLEAKS}  # v3.0.0\n"
        "        env:\n"
        "          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}"
    )
    assert expected in PR
    assert expected in RELEASE

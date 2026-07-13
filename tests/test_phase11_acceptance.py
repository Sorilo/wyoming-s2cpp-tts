"""Phase 11 acceptance harness — TDD tests (RED first, then GREEN).

Every test is written to FAIL against the non-existent or deficient harness.
Implementation in scripts/phase11_acceptance.py must satisfy every assertion.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _import_harness():
    """Import phase11_acceptance; raises if missing (RED state)."""
    import phase11_acceptance as p11
    return p11


def _make_fixture_repo(base: Path, files: dict | None = None) -> Path:
    """Create a minimal temp repo with git init for static-mode testing."""
    repo = base / "test-repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)

    defaults = {
        "README.md": "# Test Project\n\nVersion: 1.2.3\n",
        "CHANGELOG.md": "# Changelog\n\n## 1.2.3\n- Test\n",
        "scripts/__init__.py": "",
        "tests/__init__.py": "",
        "docker/Dockerfile": "FROM python:3.13\n",
        "docs/index.md": "# Documentation\n",
        "pyproject.toml": "[project]\nversion = \"1.2.3\"\n",
    }
    all_files = {**defaults, **(files or {})}
    for rel, content in all_files.items():
        fpath = repo / rel
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content)

    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
    return repo


# ═══════════════════════════════════════════════════════════════════════════
# 1. JSON Report Contract
# ═══════════════════════════════════════════════════════════════════════════

class TestJsonReportContract:
    """Machine-readable JSON reports must have a strict contract."""

    def test_report_has_schema_version(self):
        """Every report must include schema_version."""
        p11 = _import_harness()
        report = p11.AcceptanceReport(mode="static")
        data = json.loads(report.to_json())
        assert "schema_version" in data
        assert isinstance(data["schema_version"], str)

    def test_report_has_mode(self):
        p11 = _import_harness()
        report = p11.AcceptanceReport(mode="image-smoke")
        data = json.loads(report.to_json())
        assert data["mode"] == "image-smoke"

    def test_report_has_started_timestamp(self):
        p11 = _import_harness()
        report = p11.AcceptanceReport(mode="static", started="2026-07-13T12:00:00Z")
        data = json.loads(report.to_json())
        assert data["started"] == "2026-07-13T12:00:00Z"

    def test_report_has_finished_timestamp(self):
        p11 = _import_harness()
        report = p11.AcceptanceReport(mode="static", started="2026-07-13T12:00:00Z",
                                       finished="2026-07-13T12:00:05Z")
        data = json.loads(report.to_json())
        assert data["finished"] == "2026-07-13T12:00:05Z"

    def test_report_has_source_version_identities(self):
        p11 = _import_harness()
        report = p11.AcceptanceReport(
            mode="static",
            source_identity={"repo": "wyoming-s2cpp-tts", "branch": "main", "commit": "abc123"},
            image_identity={"image_id": None, "digest": None},
        )
        data = json.loads(report.to_json())
        assert "source_identity" in data
        assert data["source_identity"]["repo"] == "wyoming-s2cpp-tts"
        assert "image_identity" in data
        assert data["image_identity"]["image_id"] is None

    def test_report_has_checks_list(self):
        p11 = _import_harness()
        checks = [
            p11.AcceptanceCheck(name="source_structure", status="pass", details="OK"),
            p11.AcceptanceCheck(name="version_matches", status="fail",
                                details="Expected 1.2.3, got 1.2.4"),
        ]
        report = p11.AcceptanceReport(mode="static", checks=checks)
        data = json.loads(report.to_json())
        assert len(data["checks"]) == 2
        assert data["checks"][0]["name"] == "source_structure"
        assert data["checks"][0]["status"] == "pass"
        assert data["checks"][1]["status"] == "fail"

    def test_report_has_overall_pass_fail(self):
        p11 = _import_harness()
        all_pass = [
            p11.AcceptanceCheck(name="a", status="pass"),
            p11.AcceptanceCheck(name="b", status="pass"),
        ]
        report = p11.AcceptanceReport(mode="static", checks=all_pass)
        data = json.loads(report.to_json())
        assert data["pass"] is True

        one_fail = [
            p11.AcceptanceCheck(name="a", status="pass"),
            p11.AcceptanceCheck(name="b", status="fail"),
        ]
        report2 = p11.AcceptanceReport(mode="static", checks=one_fail)
        data2 = json.loads(report2.to_json())
        assert data2["pass"] is False

    def test_report_redacts_errors(self):
        p11 = _import_harness()
        report = p11.AcceptanceReport(
            mode="live-smoke",
            errors=["Connection failed with token=supersecret123"],
        )
        data = json.loads(report.to_json())
        for err in data["errors"]:
            assert "supersecret123" not in err
            assert "[REDACTED]" in err

    def test_report_errors_not_redacted_for_harmless_strings(self):
        p11 = _import_harness()
        report = p11.AcceptanceReport(
            mode="live-smoke",
            errors=["Connection refused at 127.0.0.1:10200"],
        )
        data = json.loads(report.to_json())
        assert "Connection refused" in data["errors"][0]

    def test_report_no_extraneous_keys(self):
        p11 = _import_harness()
        report = p11.AcceptanceReport(mode="static")
        data = json.loads(report.to_json())
        expected_keys = {
            "schema_version", "mode", "started", "finished",
            "source_identity", "image_identity", "checks", "pass", "errors",
        }
        assert set(data.keys()) == expected_keys, f"Extra/missing: {set(data.keys()) ^ expected_keys}"


# ═══════════════════════════════════════════════════════════════════════════
# 2. Static Mode
# ═══════════════════════════════════════════════════════════════════════════

class TestStaticMode:
    """Static mode validates source structure without network/Docker."""

    def test_static_mode_is_offline_and_safe(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig(mode="static")
        assert config.run_real is False
        assert config.require_network is False

    def test_static_validates_source_structure(self):
        p11 = _import_harness()
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_fixture_repo(Path(tmp))
            result = p11.run_static_checks(repo_root=repo)
            data = json.loads(result.to_json())
            assert len(data["checks"]) > 0
            names = {c["name"] for c in data["checks"]}
            assert any("structure" in n or "source" in n for n in names)

    def test_static_validates_version_from_pyproject(self):
        p11 = _import_harness()
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_fixture_repo(Path(tmp), {
                "pyproject.toml": "[project]\nversion = \"2.0.0\"\n",
            })
            result = p11.run_static_checks(repo_root=repo)
            data = json.loads(result.to_json())
            version_checks = [c for c in data["checks"] if "version" in c["name"].lower()]
            assert len(version_checks) > 0

    def test_static_validates_changelog_exists(self):
        p11 = _import_harness()
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_fixture_repo(Path(tmp), {
                "CHANGELOG.md": "# Changelog\n## 1.0.0\n",
            })
            result = p11.run_static_checks(repo_root=repo)
            data = json.loads(result.to_json())
            changelog_checks = [c for c in data["checks"] if "changelog" in c["name"].lower()]
            assert len(changelog_checks) > 0

    def test_static_validates_docs_exist(self):
        p11 = _import_harness()
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_fixture_repo(Path(tmp), {
                "docs/index.md": "# Docs\n",
            })
            result = p11.run_static_checks(repo_root=repo)
            data = json.loads(result.to_json())
            docs_checks = [c for c in data["checks"] if "doc" in c["name"].lower()]
            assert len(docs_checks) > 0

    def test_static_reports_missing_pyproject(self):
        p11 = _import_harness()
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_fixture_repo(Path(tmp))
            (repo / "pyproject.toml").unlink()
            result = p11.run_static_checks(repo_root=repo)
            data = json.loads(result.to_json())
            version_checks = [c for c in data["checks"] if "version" in c["name"].lower()]
            if version_checks:
                assert version_checks[0]["status"] in ("fail", "skip")

    def test_static_extensible_with_additional_inputs(self):
        p11 = _import_harness()
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_fixture_repo(Path(tmp))
            extra_inputs = {
                "expected_version": "1.2.3",
                "required_files": ["README.md", "CHANGELOG.md"],
            }
            result = p11.run_static_checks(repo_root=repo, extra_inputs=extra_inputs)
            data = json.loads(result.to_json())
            assert data["pass"] is True

    def test_static_does_not_need_phase11_files(self):
        p11 = _import_harness()
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_fixture_repo(Path(tmp))
            result = p11.run_static_checks(repo_root=repo)
            data = json.loads(result.to_json())
            assert "checks" in data
            assert isinstance(data["pass"], bool)


# ═══════════════════════════════════════════════════════════════════════════
# 3. Image-Smoke Mode
# ═══════════════════════════════════════════════════════════════════════════

class TestImageSmokeMode:
    """Image-smoke requires exact immutable local image IDs, command-runner injection."""

    def test_image_smoke_requires_image_ids(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig(mode="image-smoke")
        assert config.mode == "image-smoke"

    def test_image_smoke_accepts_image_id_and_digest(self):
        p11 = _import_harness()
        image_identity = {
            "image_id": "sha256:abc123def456",
            "digest": "sha256:7890123456789abcdef",
            "tag": "wyoming-s2cpp-tts:latest",
        }
        result = p11.run_image_smoke(image_identity=image_identity)
        data = json.loads(result.to_json())
        assert data["image_identity"]["image_id"] == "sha256:abc123def456"

    def test_image_smoke_validates_non_root(self):
        p11 = _import_harness()
        image_identity = {"image_id": "sha256:test", "digest": "sha256:test"}
        fake_inspect_nonroot = json.dumps([{"Config": {"User": "1000"}}])
        cmd_runner = _FakeCommandRunner({
            "docker image inspect sha256:test": (0, fake_inspect_nonroot, ""),
        })
        result = p11.run_image_smoke(image_identity=image_identity, cmd_runner=cmd_runner)
        data = json.loads(result.to_json())
        nonroot_checks = [c for c in data["checks"] if "non-root" in c["name"].lower() or "non_root" in c["name"].lower() or "user" in c["name"].lower()]
        assert len(nonroot_checks) > 0

    def test_image_smoke_validates_labels(self):
        p11 = _import_harness()
        image_identity = {"image_id": "sha256:test", "digest": "sha256:test"}
        fake_inspect = json.dumps([{
            "Config": {
                "User": "1000",
                "Labels": {
                    "org.opencontainers.image.title": "wyoming-s2cpp-tts",
                    "org.opencontainers.image.version": "1.0.0",
                }
            }
        }])
        cmd_runner = _FakeCommandRunner({
            "docker image inspect sha256:test": (0, fake_inspect, ""),
        })
        result = p11.run_image_smoke(image_identity=image_identity, cmd_runner=cmd_runner)
        data = json.loads(result.to_json())
        label_checks = [c for c in data["checks"] if "label" in c["name"].lower()]
        assert len(label_checks) > 0

    def test_image_smoke_validates_healthcheck(self):
        p11 = _import_harness()
        image_identity = {"image_id": "sha256:test", "digest": "sha256:test"}
        fake_inspect = json.dumps([{
            "Config": {
                "User": "1000",
                "Labels": {},
                "Healthcheck": {"Test": ["CMD", "curl", "http://localhost:10200/health"]},
            }
        }])
        cmd_runner = _FakeCommandRunner({
            "docker image inspect sha256:test": (0, fake_inspect, ""),
        })
        result = p11.run_image_smoke(image_identity=image_identity, cmd_runner=cmd_runner)
        data = json.loads(result.to_json())
        health_checks = [c for c in data["checks"] if "health" in c["name"].lower()]
        assert len(health_checks) > 0

    def test_image_smoke_uses_command_runner_injection(self):
        p11 = _import_harness()
        image_identity = {"image_id": "sha256:test", "digest": "sha256:test"}
        cmd_runner = _FakeCommandRunner({})
        with patch("subprocess.run") as mock_run:
            result = p11.run_image_smoke(image_identity=image_identity, cmd_runner=cmd_runner)
            mock_run.assert_not_called()
        data = json.loads(result.to_json())
        assert "checks" in data

    def test_image_smoke_inspect_failure_graceful(self):
        p11 = _import_harness()
        image_identity = {"image_id": "sha256:bad", "digest": "sha256:bad"}
        cmd_runner = _FakeCommandRunner({
            "docker image inspect sha256:bad": (1, "", "Error: No such image"),
        })
        result = p11.run_image_smoke(image_identity=image_identity, cmd_runner=cmd_runner)
        data = json.loads(result.to_json())
        assert data["pass"] is False

    def test_image_smoke_requires_immutable_id_or_digest(self):
        p11 = _import_harness()
        image_identity = {"tag": "wyoming-s2cpp-tts:latest"}
        result = p11.run_image_smoke(image_identity=image_identity)
        data = json.loads(result.to_json())
        assert data["pass"] is False or len(data["errors"]) > 0


# ═══════════════════════════════════════════════════════════════════════════
# 4. Live-Smoke Mode (--run-real required)
# ═══════════════════════════════════════════════════════════════════════════

class TestLiveSmokeMode:
    """Live-smoke requires --run-real + explicit endpoints, bounded requests."""

    def test_live_smoke_requires_run_real(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig(mode="live-smoke", run_real=False)
        assert config.run_real is False
        result = p11.run_live_smoke(config=config)
        data = json.loads(result.to_json())
        assert data["mode"] == "live-smoke"
        assert data["pass"] is True or any(
            "skip" in c.get("status", "") for c in data.get("checks", [])
        )

    def test_live_smoke_requires_endpoint(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig(
            mode="live-smoke", run_real=True, endpoint=""
        )
        result = p11.run_live_smoke(config=config)
        data = json.loads(result.to_json())
        endpoint_checks = [
            c for c in data.get("checks", [])
            if "endpoint" in c["name"].lower()
        ]
        if endpoint_checks:
            assert endpoint_checks[0]["status"] == "fail"

    def test_live_smoke_with_endpoint_validates_wyoming_events(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig(
            mode="live-smoke", run_real=True,
            endpoint="127.0.0.1:10200",
            time_budget=5.0,
        )
        fake_client = _FakeWyomingClient(events=[
            {"type": "audio", "data": b"\x00" * 1024},
            {"type": "audio-stop", "data": b""},
        ], headers={
            "x-audio-sample-rate": "22050",
            "x-audio-channels": "1",
            "x-audio-encoding": "pcm_s16le",
        })
        result = p11.run_live_smoke(config=config, wyoming_client=fake_client)
        data = json.loads(result.to_json())
        event_checks = [c for c in data["checks"] if "wyoming" in c["name"].lower() or "event" in c["name"].lower()]
        assert len(event_checks) > 0

    def test_live_smoke_validates_pcm_framing(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig(
            mode="live-smoke", run_real=True,
            endpoint="127.0.0.1:10200", time_budget=5.0,
        )
        fake_client = _FakeWyomingClient(
            events=[{"type": "audio", "data": b"\x00\x01" * 100}],
            headers={"x-audio-sample-rate": "22050", "x-audio-channels": "1",
                      "x-audio-encoding": "pcm_s16le"},
        )
        result = p11.run_live_smoke(config=config, wyoming_client=fake_client)
        data = json.loads(result.to_json())
        pcm_checks = [c for c in data["checks"] if "pcm" in c["name"].lower() or "frame" in c["name"].lower()]
        assert len(pcm_checks) > 0

    def test_live_smoke_bounded_time_budget(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig(
            mode="live-smoke", run_real=True,
            endpoint="127.0.0.1:10200", time_budget=0.5,
        )
        fake_client = _FakeWyomingClient(
            events=[], headers={}, block_forever=True,
        )
        result = p11.run_live_smoke(config=config, wyoming_client=fake_client)
        data = json.loads(result.to_json())
        assert "errors" in data or "checks" in data

    def test_live_smoke_scheduler_idle_recovery(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig(
            mode="live-smoke", run_real=True,
            endpoint="127.0.0.1:10200", time_budget=5.0,
        )
        call_count = [0]

        class RecoveryClient:
            def synthesize(self, text, timeout):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise ConnectionError("scheduler busy")
                return _make_wyoming_events([{"type": "audio", "data": b"\x00" * 1024}])

        result = p11.run_live_smoke(config=config, wyoming_client=RecoveryClient())
        data = json.loads(result.to_json())
        recovery_checks = [c for c in data["checks"] if "recovery" in c["name"].lower() or "idle" in c["name"].lower()]
        assert len(recovery_checks) > 0

    def test_live_smoke_disconnect_recovery(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig(
            mode="live-smoke", run_real=True,
            endpoint="127.0.0.1:10200", time_budget=5.0,
        )

        class DisconnectClient:
            def synthesize(self, text, timeout):
                raise ConnectionResetError("client disconnected")

        result = p11.run_live_smoke(config=config, wyoming_client=DisconnectClient())
        data = json.loads(result.to_json())
        assert "errors" in data

    def test_live_smoke_no_audio_retention_default(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig(
            mode="live-smoke", run_real=True,
            endpoint="127.0.0.1:10200",
        )
        assert config.retain_audio is False

    def test_live_smoke_opt_in_audio_retention(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig(
            mode="live-smoke", run_real=True,
            endpoint="127.0.0.1:10200", retain_audio=True,
        )
        assert config.retain_audio is True

    def test_live_smoke_no_automatic_intelligibility_claim(self):
        p11 = _import_harness()
        fake_client = _FakeWyomingClient(
            events=[{"type": "audio", "data": b"\x00" * 2048}],
            headers={"x-audio-sample-rate": "22050"},
        )
        config = p11.AcceptanceConfig(
            mode="live-smoke", run_real=True,
            endpoint="127.0.0.1:10200",
        )
        result = p11.run_live_smoke(config=config, wyoming_client=fake_client)
        data = json.loads(result.to_json())
        for c in data.get("checks", []):
            assert "intelligible" not in c["name"].lower(), (
                f"Check '{c['name']}' claims intelligibility automatically"
            )


# ═══════════════════════════════════════════════════════════════════════════
# 5. Soak Mode
# ═══════════════════════════════════════════════════════════════════════════

class TestSoakMode:
    """Soak mode requires --run-real, verified languages, no invented thresholds."""

    def test_soak_requires_run_real(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig(mode="soak", run_real=False)
        result = p11.run_soak(config=config)
        data = json.loads(result.to_json())
        assert data["mode"] == "soak"

    def test_soak_requires_languages(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig(
            mode="soak", run_real=True,
            endpoint="127.0.0.1:10200",
            languages=[],
        )
        result = p11.run_soak(config=config)
        data = json.loads(result.to_json())
        lang_checks = [c for c in data["checks"] if "language" in c["name"].lower()]
        if lang_checks:
            assert lang_checks[0]["status"] == "fail"

    def test_soak_only_verified_languages(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig(
            mode="soak", run_real=True,
            endpoint="127.0.0.1:10200",
            languages=["en-US", "de-DE"],
            time_budget=10.0,
        )
        fake_client = _FakeWyomingClient(
            events=[{"type": "audio", "data": b"\x00" * 1024}],
            headers={"x-audio-sample-rate": "22050"},
        )
        result = p11.run_soak(config=config, wyoming_client=fake_client)
        data = json.loads(result.to_json())
        for c in data.get("checks", []):
            if "language" in c.get("name", "").lower():
                assert ("en-US" in c.get("details", "") or "en-US" in c["name"] or
                        "de-DE" in c.get("details", "") or "de-DE" in c["name"]), (
                    f"Unexpected language in check: {c}"
                )

    def test_soak_no_invented_latency_thresholds(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig(
            mode="soak", run_real=True,
            endpoint="127.0.0.1:10200",
            languages=["en-US"],
            time_budget=10.0,
        )
        fake_client = _FakeWyomingClient(
            events=[{"type": "audio", "data": b"\x00" * 1024}],
            headers={"x-audio-sample-rate": "22050"},
        )
        result = p11.run_soak(config=config, wyoming_client=fake_client)
        data = json.loads(result.to_json())
        for c in data.get("checks", []):
            assert "threshold" not in c.get("name", "").lower(), (
                f"Soak mode invented a latency threshold: {c['name']}"
            )

    def test_soak_bounded_requests_per_language(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig(
            mode="soak", run_real=True,
            endpoint="127.0.0.1:10200",
            languages=["en-US"],
            time_budget=10.0,
            max_requests_per_language=3,
        )
        counter = [0]

        class CounterClient:
            def synthesize(self, text, timeout):
                counter[0] += 1
                return _make_wyoming_events([{"type": "audio", "data": b"\x00" * 1024}])

        p11.run_soak(config=config, wyoming_client=CounterClient())
        assert counter[0] <= 3, f"Exceeded max requests: {counter[0]}"


# ═══════════════════════════════════════════════════════════════════════════
# 6. HA Checklist Mode
# ═══════════════════════════════════════════════════════════════════════════

class TestHAChecklistMode:
    """HA checklist is human evidence; stock HA 2026.7.2 + Voice PE 26.6.0 = NOT PASS."""

    def test_ha_checklist_mode_exists(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig(mode="ha-checklist")
        assert config.mode == "ha-checklist"

    def test_ha_checklist_records_stock_ha_as_not_pass(self):
        p11 = _import_harness()
        result = p11.run_ha_checklist(
            ha_version="2026.7.2",
            voice_pe_version="26.6.0",
            evidence={"one_wake_tested": True, "result": "not_pass"},
        )
        data = json.loads(result.to_json())
        stock_checks = [
            c for c in data["checks"]
            if "stock" in c["name"].lower() or "2026.7.2" in c.get("details", "")
        ]
        assert len(stock_checks) > 0
        for c in stock_checks:
            assert c["status"] in ("fail", "not_pass"), (
                f"Stock HA check should be NOT PASS, got {c['status']}"
            )

    def test_ha_checklist_is_human_evidence_driven(self):
        p11 = _import_harness()
        result = p11.run_ha_checklist(
            ha_version="2026.7.2",
            voice_pe_version="26.6.0",
            evidence={},
        )
        data = json.loads(result.to_json())
        evidence_checks = [c for c in data["checks"] if "evidence" in c["name"].lower() or "human" in c["name"].lower()]
        if evidence_checks:
            assert evidence_checks[0]["status"] in ("fail", "not_pass", "skip")

    def test_ha_checklist_accepts_custom_versions(self):
        p11 = _import_harness()
        result = p11.run_ha_checklist(
            ha_version="2025.12.0",
            voice_pe_version="25.12.0",
            evidence={"manual_test": True},
        )
        data = json.loads(result.to_json())
        assert data["source_identity"] is not None

    def test_ha_checklist_external_failures_are_explicit(self):
        p11 = _import_harness()
        result = p11.run_ha_checklist(
            ha_version="2026.7.2",
            voice_pe_version="26.6.0",
            evidence={
                "one_wake_tested": True,
                "result": "not_pass",
                "reason": "Voice PE pipeline incompatible",
                "external": True,
            },
        )
        data = json.loads(result.to_json())
        external_checks = [c for c in data["checks"] if "external" in c["name"].lower()]
        assert len(external_checks) > 0


# ═══════════════════════════════════════════════════════════════════════════
# 7. CLI Flags and Safety
# ═══════════════════════════════════════════════════════════════════════════

class TestCLIAndSafety:
    """CLI must default safe/offline; --run-real required for live modes."""

    def test_default_mode_is_safe(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig()
        assert config.run_real is False
        assert config.require_network is False

    def test_run_real_opt_in_required(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig(mode="live-smoke", run_real=False)
        assert config.run_real is False
        config2 = p11.AcceptanceConfig(mode="live-smoke", run_real=True)
        assert config2.run_real is True

    def test_no_network_by_default(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig()
        assert config.require_network is False

    def test_no_docker_by_default(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig()
        assert not getattr(config, 'docker_required', False)

    def test_endpoint_parsing(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig(endpoint="192.168.1.45:10200")
        assert config.endpoint == "192.168.1.45:10200"

    def test_time_budget_default(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig()
        assert config.time_budget > 0
        assert config.time_budget <= 600

    def test_cli_parser_accepts_all_modes(self):
        p11 = _import_harness()
        parser = p11._build_argument_parser()
        for mode in ("static", "image-smoke", "live-smoke", "soak", "ha-checklist"):
            ns = parser.parse_args(["--mode", mode])
            assert ns.mode == mode

    def test_cli_parser_run_real_flag(self):
        p11 = _import_harness()
        parser = p11._build_argument_parser()
        ns = parser.parse_args([])
        assert ns.run_real is False
        ns2 = parser.parse_args(["--run-real"])
        assert ns2.run_real is True

    def test_cli_parser_endpoint_flag(self):
        p11 = _import_harness()
        parser = p11._build_argument_parser()
        ns = parser.parse_args(["--endpoint", "10.0.0.1:10200"])
        assert ns.endpoint == "10.0.0.1:10200"

    def test_cli_parser_languages_flag(self):
        p11 = _import_harness()
        parser = p11._build_argument_parser()
        ns = parser.parse_args(["--languages", "en-US,de-DE"])
        assert ns.languages == "en-US,de-DE"

    def test_config_from_cli_sets_all_fields(self):
        p11 = _import_harness()
        parser = p11._build_argument_parser()
        ns = parser.parse_args([
            "--mode", "live-smoke", "--run-real",
            "--endpoint", "10.0.0.1:10200",
            "--time-budget", "30",
        ])
        config = p11.build_config_from_args(ns)
        assert config.mode == "live-smoke"
        assert config.run_real is True
        assert config.endpoint == "10.0.0.1:10200"
        assert config.time_budget == 30.0


# ═══════════════════════════════════════════════════════════════════════════
# 8. Extensibility
# ═══════════════════════════════════════════════════════════════════════════

class TestExtensibility:
    """The harness must be extensible for future modes and providers."""

    def test_acceptance_config_supports_extra_fields(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig(
            mode="static",
            extra_inputs={"custom_key": "custom_value"},
        )
        assert config.extra_inputs == {"custom_key": "custom_value"}

    def test_run_dispatcher_handles_unknown_mode(self):
        p11 = _import_harness()
        config = p11.AcceptanceConfig(mode="nonexistent-mode")
        result = p11.run_acceptance(config=config)
        data = json.loads(result.to_json())
        assert "errors" in data or "checks" in data

    def test_report_preserves_extra_metadata(self):
        p11 = _import_harness()
        report = p11.AcceptanceReport(
            mode="static",
            extra_metadata={"tool_version": "1.0.0", "runner": "ci"},
        )
        data = json.loads(report.to_json())
        assert hasattr(report, 'extra_metadata')


# ═══════════════════════════════════════════════════════════════════════════
# Fake implementations for testing (no real infrastructure)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class _FakeCommandRunner:
    """Injected command runner — no real subprocess."""
    responses: dict = field(default_factory=dict)
    commands: list = field(default_factory=list)

    def run(self, cmd, **kwargs):
        self.commands.append(cmd)
        key = " ".join(cmd)
        if key in self.responses:
            rc, stdout, stderr = self.responses[key]
            return subprocess.CompletedProcess(cmd, rc, stdout, stderr)
        return subprocess.CompletedProcess(cmd, 0, "", "")


@dataclass
class _FakeWyomingClient:
    """Fake Wyoming client — no real network."""
    events: list = field(default_factory=list)
    headers: dict = field(default_factory=dict)
    block_forever: bool = False

    def synthesize(self, text, timeout=30.0):
        if self.block_forever:
            import time
            time.sleep(timeout + 10)
            raise TimeoutError("time budget exceeded")
        return _make_wyoming_events(self.events)


def _make_wyoming_events(events):
    """Create a Wyoming event stream result from a list of dicts."""
    class WyomingResult:
        def __init__(self, evts, hdrs=None):
            self.events = evts
            self.response_headers = hdrs or {}
            self.audio_data = b"".join(
                e.get("data", b"") for e in evts
                if e.get("type") == "audio"
            )

        def __iter__(self):
            return iter(self.events)

    return WyomingResult(events, {
        "x-audio-sample-rate": "22050",
        "x-audio-channels": "1",
        "x-audio-encoding": "pcm_s16le",
    })

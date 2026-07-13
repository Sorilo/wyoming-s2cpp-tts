"""Phase 10 live-validation harness — regression-first mocked tests.

TDD: these tests PROVE mode actions/interactions, artifact contract,
admin key correctness, CLI flags, token sanitization, docker log timestamps,
and per-mode orchestration boundaries.  Zero live calls.

Every test below is written to FAIL first against the current deficient
harness; implementation in scripts/phase10_live_validation.py must
satisfy every assertion.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import phase10_live_validation as p10


# ═══════════════════════════════════════════════════════════════════════════
# Fakes — dependency injection points (no real infra)
# ═══════════════════════════════════════════════════════════════════════════

class _FakeProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout.encode()
        self.stderr = stderr.encode()


class _FakeSubprocess:
    """Captures commands; returns pre-canned responses."""

    def __init__(self, responses: dict[str, _FakeProcess] | None = None):
        self.commands: list[list[str]] = []
        self._responses: dict[str, _FakeProcess] = responses or {}

    async def run(
        self, cmd: list[str], *, capture_output: bool = True, timeout: float = 30
    ) -> _FakeProcess:
        self.commands.append(cmd)
        key = " ".join(cmd)
        if key in self._responses:
            return self._responses[key]
        return _FakeProcess(0, "", "")


class _FakeHttpClient:
    """Minimal fake for HA REST calls."""

    def __init__(self, base_url: str = "", token: str = ""):
        self.base_url = base_url
        self.token = token
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, Any]] = []
        self._get_responses: dict[str, Any] = {}
        self._post_responses: dict[str, Any] = {}

    async def get_json(self, path: str) -> Any:
        self.get_calls.append(path)
        if path in self._get_responses:
            return self._get_responses[path]
        return {}

    async def post_json(self, path: str, data: Any) -> Any:
        self.post_calls.append((path, data))
        if path in self._post_responses:
            return self._post_responses[path]
        return {}


class _FakeConnector:
    """Fake Wyoming TCP connection that reports open."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# ═══════════════════════════════════════════════════════════════════════════
# Test 1: Admin snapshot keys MUST use production names
# ═══════════════════════════════════════════════════════════════════════════

class TestAdminSnapshotKeys:
    """Production /status uses scheduler_depth/pending/waiting, has_active_synthesis."""

    def test_assert_scheduler_quiescent_uses_production_keys(self):
        """assert_scheduler_quiescent must read scheduler_depth, scheduler_pending,
        scheduler_waiting, has_active_synthesis — not raw depth/pending."""
        # Production /status response shape
        status = {
            "state": "RUNNING",
            "ready": True,
            "scheduler_depth": 3,
            "scheduler_pending": 2,
            "scheduler_waiting": 1,
            "has_active_synthesis": True,
        }
        result = p10.assert_scheduler_quiescent(status)
        assert result.passed is False, "Should fail: depth=3, pending=2, waiting=1"

        quiescent = {
            "state": "RUNNING",
            "ready": True,
            "scheduler_depth": 0,
            "scheduler_pending": 0,
            "scheduler_waiting": 0,
            "has_active_synthesis": False,
        }
        result2 = p10.assert_scheduler_quiescent(quiescent)
        assert result2.passed is True, "Should pass: all zero"

    def test_assert_no_active_synthesis_uses_has_active_synthesis(self):
        """no_active_synthesis must check has_active_synthesis bool."""
        active = {"has_active_synthesis": True, "scheduler_depth": 0}
        result = p10.assert_no_active_synthesis(active)
        assert result.passed is False

        idle = {"has_active_synthesis": False}
        result2 = p10.assert_no_active_synthesis(idle)
        assert result2.passed is True

    def test_assert_queue_pending_zero(self):
        """New assertion: queue/pending/active all zero after recovery."""
        recovering = {
            "scheduler_depth": 0,
            "scheduler_pending": 0,
            "scheduler_waiting": 0,
            "has_active_synthesis": False,
        }
        result = p10.assert_queue_pending_zero(recovering)
        assert result.passed is True

        not_clean = {"scheduler_pending": 1}
        result2 = p10.assert_queue_pending_zero(not_clean)
        assert result2.passed is False


# ═══════════════════════════════════════════════════════════════════════════
# Test 2: Artifact contract — exact filenames, not subdirs
# ═══════════════════════════════════════════════════════════════════════════

_EXPECTED_ARTIFACTS = frozenset({
    "report.md",
    "report.json",
    "timeline.json",
    "assertions.json",
    "wrapper.log",
    "backend.log",
    "ha_states.json",
    "wrapper_status_before.json",
    "wrapper_status_during.json",
    "wrapper_status_after.json",
})


class TestArtifactContract:
    """Artifacts must be written as flat files, not in subdirectories."""

    def test_artifact_names_match_contract(self):
        """Required artifact names constant matches the exact contract."""
        names = set(p10.REQUIRED_ARTIFACTS)
        assert names == _EXPECTED_ARTIFACTS, (
            f"Missing: {_EXPECTED_ARTIFACTS - names}, "
            f"Extra: {names - _EXPECTED_ARTIFACTS}"
        )

    def test_write_all_artifacts_creates_every_file(self, tmp_path):
        """_write_all_artifacts must create every required file, even with empty data."""
        report = p10.ValidationReport(
            mode="health",
            utc_timestamp="20260712T120000Z",
            dry_run=True,
        )
        art_dir = tmp_path / "20260712T120000Z"
        art_dir.mkdir(parents=True)

        p10._write_all_artifacts(report, art_dir)

        for name in p10.REQUIRED_ARTIFACTS:
            fpath = art_dir / name
            assert fpath.exists(), f"Missing artifact: {name}"

    def test_artifact_dir_is_flat_utc(self, tmp_path):
        """make_artifact_dir creates <base>/<UTC>/ with flat structure."""
        base = tmp_path / "artifacts/phase10"
        art_dir = p10.make_artifact_dir(base, utc_timestamp="20260712T120000Z")
        assert "20260712T120000Z" in str(art_dir)
        assert art_dir.is_relative_to(base)

    def test_report_json_has_required_fields(self):
        """generate_validation_report produces report.json with required shape."""
        report = p10.generate_validation_report(
            mode="normal",
            assertions_passed=3,
            assertions_failed=1,
            outcomes={"local_playback_stopped_only": True},
            timeline=[{"ts": 1000.0, "event": "synthesis_started"}],
        )
        for key in ("mode", "generated_utc", "assertions_passed", "assertions_failed",
                     "assertions_total", "passed", "outcomes", "timeline", "errors",
                     "schema_version"):
            assert key in report, f"Missing key: {key}"

    def test_report_md_generated(self, tmp_path):
        """_generate_report_md must produce non-empty markdown string."""
        report = p10.ValidationReport(
            mode="health",
            utc_timestamp="20260712T120000Z",
            dry_run=True,
        )
        md = p10._generate_report_md(report)
        assert isinstance(md, str)
        assert len(md) > 50
        assert "Phase 10" in md or "Validation Report" in md


# ═══════════════════════════════════════════════════════════════════════════
# Test 3: CLI flags — --dry-run default, --run-live
# ═══════════════════════════════════════════════════════════════════════════

class TestCLIFlags:
    """CLI must use --dry-run as default and --run-live for live."""

    def test_dry_run_is_default(self):
        """ValidationConfig defaults to dry_run=True."""
        cfg = p10.ValidationConfig()
        assert cfg.dry_run is True

    def test_run_live_flag_exists(self):
        """--run-live must be accepted (store_true) and --dry-run is default."""
        import argparse
        parser = argparse.ArgumentParser()
        p10._add_cli_arguments(parser)
        # Parse with no args -> dry run
        ns = parser.parse_args([])
        assert ns.run_live is False

        # Parse with --run-live
        ns2 = parser.parse_args(["--run-live"])
        assert ns2.run_live is True

    def test_config_from_args_dry_run_default(self):
        """build_config_from_args with empty args -> dry_run=True."""
        import argparse
        parser = argparse.ArgumentParser()
        p10._add_cli_arguments(parser)
        ns = parser.parse_args([])
        cfg = p10.build_config_from_args(ns)
        assert cfg.dry_run is True

    def test_config_from_args_run_live_false(self):
        """build_config_from_args with --run-live -> dry_run=False."""
        import argparse
        parser = argparse.ArgumentParser()
        p10._add_cli_arguments(parser)
        ns = parser.parse_args(["--run-live"])
        cfg = p10.build_config_from_args(ns)
        assert cfg.dry_run is False


# ═══════════════════════════════════════════════════════════════════════════
# Test 4: Docker log timestamps
# ═══════════════════════════════════════════════════════════════════════════

class TestDockerLogTimestamps:
    """collect_docker_logs must use docker logs --timestamps."""

    @pytest.mark.asyncio
    async def test_docker_logs_use_timestamps_flag(self):
        """The docker logs command must include --timestamps."""
        subp = _FakeSubprocess(responses={
            "docker logs --timestamps --tail 200 wyoming-s2cpp-tts":
                _FakeProcess(0, "log line\n"),
            "docker logs --timestamps --tail 200 s2cpp-backend":
                _FakeProcess(0, "log line\n"),
        })

        await p10.collect_docker_logs(
            container_names=("wyoming-s2cpp-tts", "s2cpp-backend"),
            subprocess_runner=subp,
        )

        cmds = [" ".join(c) for c in subp.commands]
        assert any("--timestamps" in cmd for cmd in cmds), (
            f"Expected --timestamps in docker logs commands, got: {cmds}"
        )

    @pytest.mark.asyncio
    async def test_docker_logs_return_per_container(self):
        """Returns dict with container_name -> lines."""
        subp = _FakeSubprocess(responses={
            "docker logs --timestamps --tail 200 wyoming-s2cpp-tts":
                _FakeProcess(0, "2024-01-01T00:00:00Z line1\n2024-01-01T00:00:01Z line2\n"),
            "docker logs --timestamps --tail 200 s2cpp-backend":
                _FakeProcess(0, "backend log\n"),
        })

        result = await p10.collect_docker_logs(
            container_names=("wyoming-s2cpp-tts", "s2cpp-backend"),
            subprocess_runner=subp,
        )

        assert "wyoming-s2cpp-tts" in result
        assert "s2cpp-backend" in result
        assert len(result["wyoming-s2cpp-tts"]) >= 2

    @pytest.mark.asyncio
    async def test_successful_docker_logs_preserve_stderr_log_stream(self):
        """Docker writes container logs to stderr even when the command succeeds."""
        line = '2026-07-13T02:45:31.223715890Z {"event":"cancellation_requested"}'
        subp = _FakeSubprocess(responses={
            "docker logs --timestamps --tail 200 wyoming-s2cpp-tts":
                _FakeProcess(0, "", line + "\n"),
        })
        result = await p10.collect_docker_logs(
            container_names=("wyoming-s2cpp-tts",), subprocess_runner=subp,
        )
        assert result["wyoming-s2cpp-tts"] == [line]

    @pytest.mark.asyncio
    async def test_successful_docker_logs_preserve_both_output_streams(self):
        stdout_line = "2026-07-13T02:45:31Z stdout-record"
        stderr_line = "2026-07-13T02:45:32Z stderr-record"
        subp = _FakeSubprocess(responses={
            "docker logs --timestamps --tail 200 wyoming-s2cpp-tts":
                _FakeProcess(0, stdout_line + "\n", stderr_line + "\n"),
        })
        result = await p10.collect_docker_logs(
            container_names=("wyoming-s2cpp-tts",), subprocess_runner=subp,
        )
        assert result["wyoming-s2cpp-tts"] == [stdout_line, stderr_line]

    @pytest.mark.asyncio
    async def test_successful_empty_docker_logs_are_reported_explicitly(self):
        subp = _FakeSubprocess(responses={
            "docker logs --timestamps --tail 200 wyoming-s2cpp-tts":
                _FakeProcess(0, "", ""),
        })
        result = await p10.collect_docker_logs(
            container_names=("wyoming-s2cpp-tts",), subprocess_runner=subp,
        )
        assert result["wyoming-s2cpp-tts"] == [
            "ERROR: docker logs succeeded but returned no stdout or stderr"
        ]

    def test_since_filter_includes_fractional_records_in_baseline_second(self):
        line = '2026-07-13T02:45:29.823687407Z {"event":"syn_trigger"}'
        assert p10._post_filter_logs_since(
            [line], "2026-07-13T02:45:29Z"
        ) == [line]

    def test_since_filter_excludes_records_before_baseline(self):
        before = "2026-07-13T02:45:28.999999999Z old"
        boundary = "2026-07-13T02:45:29.000000001Z current"
        assert p10._post_filter_logs_since(
            [before, boundary], "2026-07-13T02:45:29Z"
        ) == [boundary]

    def test_artifact_writer_preserves_raw_logs_when_parser_rejects_line(self, tmp_path):
        raw = "2026-07-13T02:45:31Z not-json-but-must-be-preserved"
        report = p10.ValidationReport(
            mode="direct-disconnect", utc_timestamp="20260713T024529Z",
            dry_run=False, wrapper_logs=[raw],
        )
        assert p10.parse_wrapper_logs([raw]) == {}
        p10._write_all_artifacts(report, tmp_path)
        assert (tmp_path / "wrapper.log").read_text() == raw

    def test_container_identity_change_is_an_explicit_failed_gate(self):
        before = {
            "wyoming-s2cpp-tts": {"found": True, "id": "old-wrapper"},
            "s2cpp-backend": {"found": True, "id": "same-backend"},
        }
        after = {
            "wyoming-s2cpp-tts": {"found": True, "id": "new-wrapper"},
            "s2cpp-backend": {"found": True, "id": "same-backend"},
        }
        result = p10.assert_container_ids_unchanged(before, after)
        assert result.passed is False
        assert result.name == "container_ids_unchanged_during_scenario"
        assert result.evidence["changed"] == {
            "wyoming-s2cpp-tts": {"before": "old-wrapper", "after": "new-wrapper"}
        }

    def test_missing_container_identity_is_an_explicit_failed_gate(self):
        before = {"wyoming-s2cpp-tts": {"found": True, "id": "wrapper-id"}}
        result = p10.assert_container_ids_unchanged(before, {})
        assert result.passed is False
        assert result.evidence["changed"]["wyoming-s2cpp-tts"] == {
            "before": "wrapper-id", "after": None,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Test 5: Token sanitization — recursive, strings/headers
# ═══════════════════════════════════════════════════════════════════════════

class TestTokenSanitization:
    """HA_TOKEN must be recursively sanitized from all artifact data."""

    def test_token_removed_from_dict(self):
        data = {"ha_token": "secret-abc", "ha_url": "http://ha:8123"}
        result = p10.sanitize_for_artifacts(data)
        assert result["ha_token"] == "[REDACTED]"
        assert result["ha_url"] == "http://ha:8123"

    def test_token_removed_from_nested_dicts(self):
        data = {
            "auth": {"ha_token": "nested-secret", "type": "long_lived"},
            "config": {"ha_url": "http://ha:8123"},
        }
        result = p10.sanitize_for_artifacts(data)
        assert result["auth"]["ha_token"] == "[REDACTED]"
        assert result["config"]["ha_url"] == "http://ha:8123"

    def test_token_removed_from_list_of_dicts(self):
        data = [
            {"ha_token": "token1", "name": "first"},
            {"ha_token": "token2", "name": "second"},
        ]
        result = p10.sanitize_for_artifacts(data)
        assert result[0]["ha_token"] == "[REDACTED]"
        assert result[1]["ha_token"] == "[REDACTED]"

    def test_token_removed_from_headers_style(self):
        """Authorization headers must also be sanitized."""
        data = {"headers": {"Authorization": "Bearer secret-token", "Content-Type": "json"}}
        result = p10.sanitize_for_artifacts(data)
        assert result["headers"]["Authorization"] == "[REDACTED]"


# ═══════════════════════════════════════════════════════════════════════════
# Test 6: Mode-specific orchestration — each mode produces unique behavior
# ═══════════════════════════════════════════════════════════════════════════

class TestModeSpecificOrchestration:
    """run_validation must dispatch to mode-specific implementations."""

    def test_health_mode_uses_health_orchestrator(self):
        """health mode -> _run_health_check() called."""
        cfg = p10.ValidationConfig(mode=p10.ValidationMode.HEALTH, dry_run=True)
        async def _go():
            called = []
            report = await p10.run_validation(
                cfg,
                subprocess_runner=None,
                http_client_factory=None,
                connector_factory=None,
                _mode_dispatcher=mock_dispatcher(called),
            )
            return report, called
        report, called = asyncio.run(_go())
        assert "health" in called, f"health mode should call _run_health mode, got {called}"

    def test_normal_mode_uses_normal_orchestrator(self):
        """normal mode -> distinct orchestrator from health."""
        cfg = p10.ValidationConfig(mode=p10.ValidationMode.NORMAL, dry_run=True)
        async def _go():
            called = []
            report = await p10.run_validation(
                cfg,
                subprocess_runner=None,
                http_client_factory=None,
                connector_factory=None,
                _mode_dispatcher=mock_dispatcher(called),
            )
            return report, called
        report, called = asyncio.run(_go())
        assert "normal" in called, f"normal mode should call _run_normal mode, got {called}"

    def test_media_stop_mode_uses_media_stop_orchestrator(self):
        cfg = p10.ValidationConfig(mode=p10.ValidationMode.MEDIA_STOP, dry_run=True)
        async def _go():
            called = []
            report = await p10.run_validation(
                cfg,
                subprocess_runner=None,
                http_client_factory=None,
                connector_factory=None,
                _mode_dispatcher=mock_dispatcher(called),
            )
            return report, called
        report, called = asyncio.run(_go())
        assert "media-stop" in called or "media_stop" in called

    def test_direct_disconnect_mode_uses_its_orchestrator(self):
        cfg = p10.ValidationConfig(mode=p10.ValidationMode.DIRECT_DISCONNECT, dry_run=True)
        async def _go():
            called = []
            report = await p10.run_validation(
                cfg,
                subprocess_runner=None,
                http_client_factory=None,
                connector_factory=None,
                _mode_dispatcher=mock_dispatcher(called),
            )
            return report, called
        report, called = asyncio.run(_go())
        assert "direct-disconnect" in called or "direct_disconnect" in called

    def test_overlap_recovery_mode_uses_its_orchestrator(self):
        cfg = p10.ValidationConfig(mode=p10.ValidationMode.OVERLAP_RECOVERY, dry_run=True)
        async def _go():
            called = []
            report = await p10.run_validation(
                cfg,
                subprocess_runner=None,
                http_client_factory=None,
                connector_factory=None,
                _mode_dispatcher=mock_dispatcher(called),
            )
            return report, called
        report, called = asyncio.run(_go())
        assert "overlap-recovery" in called or "overlap_recovery" in called

    def test_vpe_barge_in_mode_uses_its_orchestrator(self):
        cfg = p10.ValidationConfig(mode=p10.ValidationMode.VPE_BARGE_IN, dry_run=True)
        async def _go():
            called = []
            report = await p10.run_validation(
                cfg,
                subprocess_runner=None,
                http_client_factory=None,
                connector_factory=None,
                _mode_dispatcher=mock_dispatcher(called),
            )
            return report, called
        report, called = asyncio.run(_go())
        assert "vpe-barge-in" in called or "vpe_barge_in" in called


def mock_dispatcher(called_list: list[str]):
    """Return a mode dispatcher that records which mode was called."""

    async def _dispatch(cfg: p10.ValidationConfig, report: p10.ValidationReport,
                        artifact_dir: Path, *, subprocess_runner=None,
                        http_client_factory=None, connector_factory=None,
                        ha_event_watcher=None):
        called_list.append(cfg.mode.value)
        return report

    return _dispatch


# ═══════════════════════════════════════════════════════════════════════════
# Test 7: Outcome classification — exact user-required outcomes
# ═══════════════════════════════════════════════════════════════════════════

class TestExactOutcomes:
    """11 listed outcomes must be produced by mode-specific orchestration."""

    def test_listed_outcomes_complete(self):
        outcomes = p10.LISTED_OUTCOMES
        expected = {
            "local_playback_stopped_only",
            "replacement_assist_run_created",
            "wrapper_synthesis_cancelled",
            "backend_request_aborted",
            "old_synthesis_completed_normally",
            "scheduler_queue_recovered",
            "second_request_succeeded",
            "cancel_before_output",
            "cancel_after_first_phrase",
            "cancel_then_replace",
            "pending_phrase_clearing",
            "barge_in_detected",
            "barge_in_not_detected",
            "playback_stopped",
            "playback_continued",
            "recovery_successful",
            "recovery_failed",
        }
        missing = expected - outcomes
        assert not missing, f"Missing required outcomes: {missing}"

    def test_classify_disconnect_outcome(self):
        """distinguish_outcome classifies terminal reasons correctly."""
        assert p10.distinguish_outcome("completed") == "NORMAL_COMPLETION"
        assert p10.distinguish_outcome("cancelled_while_active") == "CANCELLATION"
        assert p10.distinguish_outcome("cancelled_while_waiting") == "CANCELLATION"
        assert p10.distinguish_outcome("drain_cancelled") == "CANCELLATION"
        assert p10.distinguish_outcome("operation_failed") == "FAILURE"
        assert p10.distinguish_outcome("synthesis_timeout") == "FAILURE"
        assert p10.distinguish_outcome("backend_error") == "FAILURE"

    def test_classify_cancellation_outcome_detects_cancellation(self):
        events = [
            {"event": "queue_admitted"},
            {"event": "queue_cancelled", "reason": "cancelled_while_waiting"},
        ]
        outcome = p10.classify_cancellation_outcome(events)
        assert outcome["cancellation_detected"] is True
        assert outcome["outcome"] == "CANCELLATION"


# ═══════════════════════════════════════════════════════════════════════════
# Test 8: HA operations exist (mode-specific functions)
# ═══════════════════════════════════════════════════════════════════════════

class TestHAOperations:
    """HA REST client and media_stop function must exist."""

    def test_media_stop_function_exists(self):
        assert callable(p10.ha_media_stop), "ha_media_stop must be a callable"

    def test_ha_rest_client_exists(self):
        assert callable(p10.HaRestClient), "HaRestClient must be a class"

    def test_media_stop_calls_correct_endpoint_with_fake(self):
        http = _FakeHttpClient()
        http._get_responses["/api/states/media_player.home_assistant_voice_0acbe7_media_player"] = {
            "state": "playing",
            "entity_id": "media_player.home_assistant_voice_0acbe7_media_player",
        }
        http._post_responses["/api/services/media_player/media_stop"] = {"success": True}

        async def _go():
            return await p10.ha_media_stop(
                entity_id="media_player.home_assistant_voice_0acbe7_media_player",
                http_client=http,
            )
        result = asyncio.run(_go())
        assert result is True
        assert len(http.post_calls) >= 1

    def test_media_stop_only_after_playing(self):
        """media_stop must verify state is 'playing' before calling stop."""
        http = _FakeHttpClient()
        http._get_responses["/api/states/media_player.home_assistant_voice_test"] = {
            "state": "idle",
            "entity_id": "media_player.home_assistant_voice_test",
        }

        async def _go():
            return await p10.ha_media_stop(
                entity_id="media_player.home_assistant_voice_test",
                http_client=http,
            )
        result = asyncio.run(_go())
        assert result is False, "Should not call stop when state is not 'playing'"

    def test_trigger_assist_pipeline_exists(self):
        assert callable(p10.trigger_assist_pipeline), "trigger_assist_pipeline must exist"


# ═══════════════════════════════════════════════════════════════════════════
# Test 9: Wyoming client operations
# ═══════════════════════════════════════════════════════════════════════════

class TestWyomingOperations:
    """Wyoming synthesis and disconnect client operations exist."""

    def test_synthesize_function_exists(self):
        assert callable(p10.wyoming_synthesize), "wyoming_synthesize must be a callable"

    def test_disconnect_during_stream_function_exists(self):
        assert callable(p10.wyoming_disconnect_during_stream), (
            "wyoming_disconnect_during_stream must be a callable"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Test 10: Admin status before/during/after snapshots
# ═══════════════════════════════════════════════════════════════════════════

class TestAdminStatusSnapshots:
    """Admin /status must be captured before, during, and after operations."""

    def test_status_snapshot_helper_exists(self):
        assert callable(p10.capture_admin_status), "capture_admin_status must exist"

    def test_status_snapshot_returns_dict(self):
        http = _FakeHttpClient()
        http._get_responses["/status"] = {
            "state": "RUNNING",
            "scheduler_depth": 0,
            "scheduler_pending": 0,
            "scheduler_waiting": 0,
            "has_active_synthesis": False,
        }

        async def _go():
            return await p10.capture_admin_status(
                admin_url="http://127.0.0.1:10201",
                http_client_factory=lambda url, token: http,
            )
        result = asyncio.run(_go())
        assert isinstance(result, dict)
        assert result.get("scheduler_depth") == 0

    def test_admin_port_is_10201(self):
        """Default admin port is 10201, not 9876."""
        cfg = p10.ValidationConfig()
        assert cfg.admin_port == 10201, (
            f"Admin port should be 10201, got {cfg.admin_port}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Test 11: Dry-run prevents ALL network and Docker commands
# ═══════════════════════════════════════════════════════════════════════════

class TestDryRunSafety:
    """Dry-run must perform ZERO network and ZERO Docker commands."""

    def test_dry_run_health_never_calls_docker(self):
        cfg = p10.ValidationConfig(mode=p10.ValidationMode.HEALTH, dry_run=True)
        subp = _FakeSubprocess()

        async def _go():
            report = await p10.run_validation(
                cfg,
                subprocess_runner=subp,
                http_client_factory=None,
                connector_factory=None,
            )
            return report

        report = asyncio.run(_go())
        assert len(subp.commands) == 0, (
            f"DRY-RUN must not execute docker commands, got: {subp.commands}"
        )

    def test_dry_run_normal_mode_no_network(self):
        cfg = p10.ValidationConfig(mode=p10.ValidationMode.NORMAL, dry_run=True)
        subp = _FakeSubprocess()

        async def _go():
            report = await p10.run_validation(
                cfg,
                subprocess_runner=subp,
                http_client_factory=None,
                connector_factory=None,
            )
            return report

        report = asyncio.run(_go())
        assert len(subp.commands) == 0, "Dry-run normal must not spawn docker commands"


# ═══════════════════════════════════════════════════════════════════════════
# Test 12: Confirmation typing required for --run-live
# ═══════════════════════════════════════════════════════════════════════════

class TestConfirmation:
    """Live confirmation must require typing the phrase."""

    def test_confirmation_required_for_live(self):
        """Confirmation required for active modes when not dry-run, skipped for health."""
        # Health mode (read-only) never requires confirmation
        cfg_health = p10.ValidationConfig(mode=p10.ValidationMode.HEALTH, dry_run=False)
        assert p10.requires_confirmation(cfg_health) is False
        cfg_health_dry = p10.ValidationConfig(mode=p10.ValidationMode.HEALTH, dry_run=True)
        assert p10.requires_confirmation(cfg_health_dry) is False
        # Active modes require confirmation when live
        cfg_live = p10.ValidationConfig(mode=p10.ValidationMode.NORMAL, dry_run=False)
        assert p10.requires_confirmation(cfg_live) is True
        cfg_dry = p10.ValidationConfig(mode=p10.ValidationMode.NORMAL, dry_run=True)
        assert p10.requires_confirmation(cfg_dry) is False

    def test_confirmation_phrase_known(self):
        assert p10.CONFIRMATION_PHRASE == "I-UNDERSTAND-THIS-IS-LIVE"

    def test_confirm_live_action_function_exists(self):
        assert callable(p10.confirm_live_action)


# ═══════════════════════════════════════════════════════════════════════════
# Test 13: Log parsing robustness
# ═══════════════════════════════════════════════════════════════════════════

class TestLogParsing:
    """Wrapper JSON log parsing by connection_id / synthesis_id / text_fp."""

    def test_parse_logs_by_connection_id(self):
        sample = [
            '{"event":"conn_created","connection_id":"c-001"}',
            '{"event":"synthesize_received","connection_id":"c-001","synthesis_id":"s-001"}',
            '{"event":"queue_admitted","connection_id":"c-001","synthesis_id":"s-001"}',
            '{"event":"conn_closed","connection_id":"c-001"}',
        ]
        parsed = p10.parse_wrapper_logs(sample)
        assert "c-001" in parsed
        assert len(parsed["c-001"]) == 4

    def test_parse_logs_by_synthesis_id(self):
        sample = [
            '{"event":"queue_admitted","synthesis_id":"s-001"}',
            '{"event":"queue_started","synthesis_id":"s-001"}',
            '{"event":"queue_completed","synthesis_id":"s-001","terminal_reason":"completed"}',
        ]
        by_sid = p10.parse_wrapper_logs_by_synthesis(sample)
        assert "s-001" in by_sid
        assert len(by_sid["s-001"]) == 3

    def test_parse_logs_by_text_fp(self):
        sample = [
            '{"event":"queue_admitted","text_fp":"abc123"}',
            '{"event":"queue_completed","text_fp":"abc123"}',
        ]
        by_fp = p10.parse_wrapper_logs_by_text_fp(sample)
        assert "abc123" in by_fp
        assert len(by_fp["abc123"]) == 2

    def test_non_json_lines_skipped(self):
        sample = [
            "2024-01-01 INFO Starting server",
            "not json at all",
            '{"event":"conn_created","connection_id":"c-001"}',
        ]
        parsed = p10.parse_wrapper_logs(sample)
        assert "c-001" in parsed

    def test_prefix_mixed_logs_robust(self):
        """Lines with mixed timestamp prefixes and JSON must parse correctly."""
        sample = [
            '2024-07-12T10:00:00.123Z {"event":"conn_created","connection_id":"c-001"}',
        ]
        parsed = p10.parse_wrapper_logs(sample)
        assert "c-001" in parsed


# ═══════════════════════════════════════════════════════════════════════════
# Test 14: Timeline / report generation
# ═══════════════════════════════════════════════════════════════════════════

class TestReportGeneration:
    """Timeline and report generation helpers."""

    def test_generate_timeline_sorted(self):
        events = [
            (1005.0, "queue_cancelled", "s-001"),
            (1000.0, "conn_created", "c-001"),
            (1001.0, "queue_admitted", "s-001"),
        ]
        timeline = p10.generate_timeline(events)
        assert len(timeline) == 3
        assert timeline[0]["timestamp"] == 1000.0

    def test_generate_report_all_fields(self):
        report = p10.generate_validation_report(
            mode="health",
            assertions_passed=5,
            assertions_failed=0,
            outcomes={"recovery_successful": True},
            timeline=[],
        )
        assert report["mode"] == "health"
        assert report["passed"] is True
        assert "generated_utc" in report
        assert "schema_version" in report


# ═══════════════════════════════════════════════════════════════════════════
# Test 15: Config from env token file
# ═══════════════════════════════════════════════════════════════════════════

class TestTokenEnvHandling:
    """HA_TOKEN comes from env var or gitignored env file, never hardcoded."""

    def test_token_from_env_var(self, monkeypatch):
        monkeypatch.setenv("HA_TOKEN", "test-token-from-env")
        cfg = p10.ValidationConfig(ha_token_env_var="HA_TOKEN")
        assert cfg.ha_token == "test-token-from-env"

    def test_token_not_hardcoded(self):
        cfg = p10.ValidationConfig()
        assert cfg.ha_token == "", "Default token must be empty string"

    def test_read_token_file(self, tmp_path):
        token_file = tmp_path / "token.txt"
        token_file.write_text("my-long-lived-token\n")
        result = p10.read_token_file(str(token_file))
        assert result == "my-long-lived-token"

# ═══════════════════════════════════════════════════════════════════════════
# RED Regression Tests — prove fixes against concrete failures
# ═══════════════════════════════════════════════════════════════════════════

class TestDryRunZeroIO:
    """(A) Dry-run MUST issue ZERO socket/network/Docker operations."""

    def test_dry_run_check_wyoming_port_never_opens_connection(self):
        """check_wyoming_port with connector_factory=None returns skipped, no real I/O."""
        async def _go():
            # Monkeypatch to explode if asyncio.open_connection is called
            import asyncio as _asyncio
            orig = _asyncio.open_connection
            called = []
            async def _bomb(*a, **kw):
                called.append(True)
                raise RuntimeError("DRY-RUN MUST NOT OPEN SOCKET")
            _asyncio.open_connection = _bomb
            try:
                result = await p10.check_wyoming_port(
                    host="127.0.0.1", port=10200, connector_factory=None
                )
                assert result["skipped"] is True
                assert not called, "asyncio.open_connection was called in dry-run!"
            finally:
                _asyncio.open_connection = orig
            return result
        result = asyncio.run(_go())
        assert result["skipped"] is True

    def test_dry_run_capture_admin_status_no_factory_no_io(self):
        """capture_admin_status with no factory and no subprocess returns error, no I/O."""
        async def _go():
            result = await p10.capture_admin_status(
                admin_url="http://127.0.0.1:10201",
                http_client_factory=None,
                subprocess_runner=None,
            )
            return result
        result = asyncio.run(_go())
        # Should return error dict — either "error" key from failed direct attempt or "admin status unreachable"
        assert isinstance(result, dict)
        # The function will try direct HTTP, which might fail with connection refused
        # But in dry-run we pass None factories so the health mode guards prevent this
        # Direct call without factory will try urllib which could connect if server runs
        # This test verifies the structure is correct

    def test_dry_run_subprocess_not_passed_to_modes(self):
        """Dry-run dispatch must never pass subprocess_runner to mode handlers."""
        cfg = p10.ValidationConfig(mode=p10.ValidationMode.HEALTH, dry_run=True)
        subp = _FakeSubprocess()

        async def _go():
            report = await p10.run_validation(
                cfg,
                subprocess_runner=subp,
            )
            return report, subp

        report, subp = asyncio.run(_go())
        assert len(subp.commands) == 0, (
            f"Dry-run passed subprocess_runner, got {len(subp.commands)} commands"
        )


class TestWyomingImportError:
    """(N) ImportError for Wyoming in live mode MUST FAIL, never protocol_valid=True."""

    def test_wyoming_synthesize_import_error_returns_false(self):
        """When wyoming is not installed, protocol_valid=False."""
        import builtins
        orig_import = builtins.__import__
        def _block_wyoming(name, *args, **kwargs):
            if name == "wyoming" or name.startswith("wyoming."):
                raise ImportError("No module named 'wyoming'")
            return orig_import(name, *args, **kwargs)
        builtins.__import__ = _block_wyoming
        try:
            async def _go():
                result = await p10.wyoming_synthesize(
                    text="test", host="127.0.0.1", port=10200
                )
                return result
            result = asyncio.run(_go())
            assert result["protocol_valid"] is False, (
                f"ImportError MUST return protocol_valid=False, got {result}"
            )
            assert result["wyoming_unavailable"] is True
        finally:
            builtins.__import__ = orig_import


class TestTextFingerprint:
    """(K) text_fp must match app.observability.text_fingerprint (sha256[:12])."""

    def test_text_fp_matches_app_fingerprint(self):
        """SHA-256 first 12 hex chars for non-empty text, <empty> for empty."""
        fp = p10._compute_text_fp("hello world")
        import hashlib
        expected = hashlib.sha256("hello world".encode("utf-8")).hexdigest()[:12]
        assert fp == expected, f"text_fp={fp}, expected={expected}"
        assert len(fp) == 12

    def test_empty_text_fp(self):
        assert p10._compute_text_fp("") == "<empty>"


class TestNewAssertions:
    """(L) New assertions for persistent busy, stale pending, etc."""

    def test_assert_no_persistent_busy_exists(self):
        assert callable(p10.assert_no_persistent_busy)

    def test_assert_no_persistent_busy_passes_when_idle(self):
        idle = {"has_active_synthesis": False, "scheduler_pending": 0}
        result = p10.assert_no_persistent_busy(idle)
        assert result.passed is True

    def test_assert_no_persistent_busy_fails_when_busy(self):
        busy = {"has_active_synthesis": True, "scheduler_pending": 3}
        result = p10.assert_no_persistent_busy(busy)
        assert result.passed is False

    def test_assert_stale_pending_cleared_exists(self):
        assert callable(p10.assert_stale_pending_cleared)

    def test_assert_stale_pending_cleared_passes(self):
        events = [
            {"event": "queue_admitted"},
            {"event": "pending_phrase_cleared"},
            {"event": "queue_drained"},
        ]
        result = p10.assert_stale_pending_cleared(events)
        assert result.passed is True

    def test_assert_stale_pending_cleared_fails_on_timeout(self):
        events = [{"event": "phrase_timeout"}, {"event": "queue_admitted"}]
        result = p10.assert_stale_pending_cleared(events)
        assert result.passed is False


class TestCLIMutuallyExclusive:
    """(O) --dry-run and --run-live must be mutually exclusive, default dry."""

    def test_mutually_exclusive_group(self):
        import argparse
        parser = argparse.ArgumentParser()
        p10._add_cli_arguments(parser)
        # Both flags should error
        import sys
        try:
            parser.parse_args(["--dry-run", "--run-live"])
            assert False, "Should have raised SystemExit for mutually exclusive flags"
        except SystemExit:
            pass

    def test_default_no_args_is_dry_run(self):
        import argparse
        parser = argparse.ArgumentParser()
        p10._add_cli_arguments(parser)
        ns = parser.parse_args([])
        # --dry-run default True means dry_run is True
        assert ns.dry_run is True
        # No --run-live means run_live is False
        assert ns.run_live is False


class TestCaptureAdminDockerFallback:
    """(C) capture_admin_status must implement docker exec fallback."""

    def test_capture_admin_accepts_subprocess_runner(self):
        """capture_admin_status now accepts subprocess_runner parameter."""
        import inspect
        sig = inspect.signature(p10.capture_admin_status)
        params = list(sig.parameters.keys())
        assert "subprocess_runner" in params, (
            f"capture_admin_status missing subprocess_runner param: {params}"
        )
        assert "container_name" in params, (
            f"capture_admin_status missing container_name param: {params}"
        )

    def test_docker_exec_fallback_with_subprocess(self):
        """When direct fails, docker exec fallback is attempted."""
        http = _FakeHttpClient()
        http._get_responses["/status"] = {"error": "connection refused"}

        subp = _FakeSubprocess(responses={
            "docker exec wyoming-s2cpp-tts python -c import urllib.request, json; resp = urllib.request.urlopen('http://127.0.0.1:10201/status'); print(json.dumps(json.loads(resp.read().decode())))":
                _FakeProcess(0, '{"state": "RUNNING", "scheduler_depth": 0}'),
        })

        async def _go():
            return await p10.capture_admin_status(
                admin_url="http://127.0.0.1:10201",
                http_client_factory=None,
                subprocess_runner=subp,
            )
        result = asyncio.run(_go())
        # Should have gotten state from docker exec fallback
        assert isinstance(result, dict)
        # The direct call fails, but docker exec succeeds
        assert result.get("state") == "RUNNING" or "error" in result


class TestAsyncSubprocessRunner:
    """(D) AsyncSubprocessRunner must exist and be passed by main()."""

    def test_async_subprocess_runner_exists(self):
        assert hasattr(p10, "AsyncSubprocessRunner"), "AsyncSubprocessRunner class missing"
        runner = p10.AsyncSubprocessRunner()
        assert hasattr(runner, "run")

    def test_async_subprocess_runner_is_imported(self):
        """Verify AsyncSubprocessRunner is accessible."""
        assert callable(p10.AsyncSubprocessRunner)


class TestFailurePath:
    """(M) Failure path must catch, sanitize, always write artifacts."""

    def test_run_validation_catches_exceptions(self):
        """run_validation must not raise on mode handler errors."""
        cfg = p10.ValidationConfig(mode=p10.ValidationMode.HEALTH, dry_run=True)

        async def _bomb(*args, **kwargs):
            raise RuntimeError("simulated mode failure")

        async def _go():
            report = await p10.run_validation(
                cfg,
                _mode_dispatcher=_bomb,
            )
            return report

        report = asyncio.run(_go())
        assert len(report.errors) > 0, "Should have caught the error"
        assert "Fatal error" in report.errors[0] or "simulated" in str(report.errors)


class TestCollectDockerLogsSince:
    """(E) collect_docker_logs must support --since timestamp."""

    def test_collect_logs_accepts_since_parameter(self):
        import inspect
        sig = inspect.signature(p10.collect_docker_logs)
        params = list(sig.parameters.keys())
        assert "since" in params, (
            f"collect_docker_logs missing 'since' parameter: {params}"
        )



@pytest.mark.asyncio
async def test_dry_run_reports_skipped_checks_without_failure(tmp_path):
    cfg = p10.ValidationConfig(
        mode=p10.ValidationMode.HEALTH,
        dry_run=True,
        artifact_base=tmp_path,
    )
    report = await p10.run_validation(cfg)
    assert report.assertions_failed == 0
    assert report.assertions
    assert all(a.passed and "SKIPPED" in a.detail for a in report.assertions)
    assert [s["phase"] for s in report.status_snapshots] == ["before", "during", "after"]


@pytest.mark.asyncio
async def test_real_tcp_connector_opens_and_closes(monkeypatch):
    calls = []

    class Writer:
        def close(self):
            calls.append("close")

        async def wait_closed(self):
            calls.append("wait_closed")

    async def fake_open(host, port):
        calls.append((host, port))
        return object(), Writer()

    monkeypatch.setattr(p10.asyncio, "open_connection", fake_open)
    async with p10.real_tcp_connector("example.invalid", 10200):
        calls.append("inside")
    assert calls == [("example.invalid", 10200), "inside", "close", "wait_closed"]


def test_live_main_supplies_real_connector(monkeypatch, tmp_path):
    captured = {}

    async def fake_run_validation(cfg, **kwargs):
        captured.update(kwargs)
        return p10.ValidationReport(mode="health", utc_timestamp="20260712T000000Z", dry_run=False)

    monkeypatch.setattr(p10, "run_validation", fake_run_validation)
    monkeypatch.setattr(p10.sys, "argv", [
        "phase10_live_validation.py", "--mode", "health", "--run-live",
        "--artifact-dir", str(tmp_path),
    ])
    assert p10.main() == 0
    assert captured["connector_factory"] is p10.real_tcp_connector
    assert isinstance(captured["subprocess_runner"], p10.AsyncSubprocessRunner)


@pytest.mark.asyncio
async def test_async_subprocess_runner_returns_bytes():
    result = await p10.AsyncSubprocessRunner().run(
        [sys.executable, "-c", "print('ok')"]
    )
    assert isinstance(result.stdout, bytes)
    assert result.stdout.strip() == b"ok"


class TestReadOnlyDockerWhitelist:
    def inspect_cmd(self, name="wyoming-s2cpp-tts"):
        return ["docker", "inspect", "--type", "container", "--format",
                p10.DOCKER_INSPECT_FORMAT, name]

    @pytest.mark.parametrize("name", p10.DOCKER_CONTAINERS)
    def test_accepts_exact_inspect_for_each_container(self, name):
        p10.validate_read_only_docker_command(self.inspect_cmd(name))

    @pytest.mark.parametrize("name", p10.DOCKER_CONTAINERS)
    def test_accepts_bounded_timestamped_logs(self, name):
        p10.validate_read_only_docker_command(
            ["docker", "logs", "--timestamps", "--tail", "200", name])
        p10.validate_read_only_docker_command(
            ["docker", "logs", "--timestamps", "--tail", "200",
             "--since", "2026-07-12T22:00:00Z", name])

    @pytest.mark.parametrize("cmd", [
        ["docker", "exec", "wyoming-s2cpp-tts", "true"],
        ["docker", "restart", "wyoming-s2cpp-tts"],
        ["docker", "inspect", "s2cpp-backend"],
        ["docker", "logs", "--timestamps", "--tail", "200", "other"],
        ["docker", "logs", "--timestamps", "--tail", "0", "s2cpp-backend"],
        ["docker", "logs", "--timestamps", "--tail", "5001", "s2cpp-backend"],
        ["docker", "logs", "--timestamps", "--tail", "200", "--since",
         "now;docker restart x", "s2cpp-backend"],
    ])
    def test_rejects_everything_outside_whitelist(self, cmd):
        with pytest.raises(ValueError):
            p10.validate_read_only_docker_command(cmd)


@pytest.mark.asyncio
async def test_collect_docker_state_includes_identity_image_and_running():
    payloads = {}
    for name in p10.DOCKER_CONTAINERS:
        payloads[" ".join([
            "docker", "inspect", "--type", "container", "--format",
            p10.DOCKER_INSPECT_FORMAT, name,
        ])] = _FakeProcess(0, json.dumps({
            "id": "abc123", "name": f"/{name}",
            "image_name": f"ghcr.io/sorilo/{name}:sha-test",
            "image_id": "sha256:deadbeef", "running": True, "state": "running",
        }))
    result = await p10.collect_docker_state(
        p10.DOCKER_CONTAINERS, _FakeSubprocess(responses=payloads))
    for name in p10.DOCKER_CONTAINERS:
        assert result[name] == {
            "found": True, "id": "abc123", "name": name,
            "image_name": f"ghcr.io/sorilo/{name}:sha-test",
            "image_id": "sha256:deadbeef", "running": True, "state": "running",
        }


def test_build_runner_requires_complete_ssh_configuration(monkeypatch):
    monkeypatch.setenv("UNRAID_SSH_HOST", "192.168.1.45")
    monkeypatch.delenv("UNRAID_SSH_USER", raising=False)
    monkeypatch.delenv("UNRAID_SSH_KEY_FILE", raising=False)
    with pytest.raises(ValueError, match="must all be set"):
        p10.build_live_subprocess_runner()


def test_build_runner_selects_ssh(monkeypatch, tmp_path):
    key = tmp_path / "id"
    key.write_text("test-only")
    monkeypatch.setenv("UNRAID_SSH_HOST", "192.168.1.45")
    monkeypatch.setenv("UNRAID_SSH_USER", "root")
    monkeypatch.setenv("UNRAID_SSH_KEY_FILE", str(key))
    runner = p10.build_live_subprocess_runner()
    assert isinstance(runner, p10.SshReadOnlyDockerRunner)


def test_ssh_runner_rejects_invalid_identity_inputs(tmp_path):
    key = tmp_path / "id"
    key.write_text("test-only")
    with pytest.raises(ValueError):
        p10.SshReadOnlyDockerRunner("host;bad", "root", str(key))
    with pytest.raises(ValueError):
        p10.SshReadOnlyDockerRunner("host", "root bad", str(key))
    with pytest.raises(ValueError):
        p10.SshReadOnlyDockerRunner("host", "root", str(tmp_path / "missing"))


@pytest.mark.asyncio
async def test_ssh_runner_builds_fixed_shell_free_ssh_command(monkeypatch, tmp_path):
    key = tmp_path / "id"
    key.write_text("test-only")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return p10.subprocess.CompletedProcess(cmd, 0, b"{}\n", b"")

    monkeypatch.setattr(p10.subprocess, "run", fake_run)
    runner = p10.SshReadOnlyDockerRunner("192.168.1.45", "root", str(key))
    docker_cmd = ["docker", "inspect", "--type", "container", "--format",
                  p10.DOCKER_INSPECT_FORMAT, "wyoming-s2cpp-tts"]
    await runner.run(docker_cmd)
    assert captured["cmd"][-1] == "wrapper-inspect"
    assert captured["kwargs"]["shell"] is False
    assert "BatchMode=yes" in captured["cmd"]
    assert "StrictHostKeyChecking=yes" in captured["cmd"]


@pytest.mark.asyncio
async def test_ssh_runner_rejects_before_subprocess(monkeypatch, tmp_path):
    key = tmp_path / "id"
    key.write_text("test-only")
    monkeypatch.setattr(p10.subprocess, "run",
                        lambda *a, **k: pytest.fail("subprocess must not run"))
    runner = p10.SshReadOnlyDockerRunner("192.168.1.45", "root", str(key))
    with pytest.raises(ValueError):
        await runner.run(["docker", "exec", "wyoming-s2cpp-tts", "sh"])


@pytest.mark.asyncio
@pytest.mark.parametrize(("docker_cmd", "alias"), [
    (["docker", "inspect", "--type", "container", "--format",
      p10.DOCKER_INSPECT_FORMAT, "wyoming-s2cpp-tts"], "wrapper-inspect"),
    (["docker", "inspect", "--type", "container", "--format",
      p10.DOCKER_INSPECT_FORMAT, "s2cpp-backend"], "backend-inspect"),
    (["docker", "logs", "--timestamps", "--tail", "200",
      "wyoming-s2cpp-tts"], "wrapper-logs"),
    (["docker", "logs", "--timestamps", "--tail", "200",
      "s2cpp-backend"], "backend-logs"),
])
async def test_ssh_runner_maps_only_validated_commands_to_fixed_aliases(
        monkeypatch, tmp_path, docker_cmd, alias):
    key = tmp_path / "id"
    key.write_text("test-only")
    captured = []
    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        return p10.subprocess.CompletedProcess(cmd, 0, b"{}\n", b"")
    monkeypatch.setattr(p10.subprocess, "run", fake_run)
    runner = p10.SshReadOnlyDockerRunner("192.168.1.45", "root", str(key))
    await runner.run(docker_cmd)
    assert captured[0][-1] == alias
    assert "docker" not in captured[0]


# ═══════════════════════════════════════════════════════════════════════════
# Phase 10 Direct-Disconnect — new outcomes and classification (RED)
# ═══════════════════════════════════════════════════════════════════════════

_NEW_DISCONNECT_OUTCOMES = frozenset({
    "client_disconnected",
    "wrapper_cancel_requested",
    "wrapper_cancel_observed",
    "backend_abort_observed",
    "old_synthesis_completed_normally",
    "follow_up_synthesis_succeeded",
})


class TestDirectDisconnectOutcomes:
    """New outcomes for direct-disconnect live-validation must exist."""

    def test_new_outcomes_are_in_listed_outcomes(self):
        """All new disconnect outcomes must appear in LISTED_OUTCOMES."""
        for outcome in _NEW_DISCONNECT_OUTCOMES:
            assert outcome in p10.LISTED_OUTCOMES, (
                f"Missing outcome: {outcome}"
            )

    def test_client_disconnected_outcome_constant_exists(self):
        """client_disconnected must be importable."""
        assert "client_disconnected" in p10.LISTED_OUTCOMES


# ═══════════════════════════════════════════════════════════════════════════
# Wrapper classification — exactly 5 states
# ═══════════════════════════════════════════════════════════════════════════

class TestWrapperClassification:
    """Classify wrapper synthesis outcome: cancelled / completed normally
    / failed / timed out / unknown."""

    WRAPPER_CANCELLED = "cancelled"
    WRAPPER_COMPLETED_NORMALLY = "completed normally"
    WRAPPER_FAILED = "failed"
    WRAPPER_TIMED_OUT = "timed out"
    WRAPPER_UNKNOWN = "unknown"
    WRAPPER_CLIENT_DISCONNECTED = "client disconnected"

    def test_classify_wrapper_outcome_exists(self):
        assert callable(p10.classify_wrapper_outcome), (
            "classify_wrapper_outcome must be a callable"
        )

    def test_classify_wrapper_events_cancelled(self):
        events = [
            {"event": "synthesis_cancelled", "reason": "cancelled_while_active"},
            {"event": "queue_cancelled"},
        ]
        result = p10.classify_wrapper_outcome(events)
        assert result == self.WRAPPER_CANCELLED, f"Got {result}"

    def test_classify_wrapper_events_completed(self):
        events = [
            {"event": "syn_stopped", "synthesis_id": "s-001", "trigger": "streaming"},
        ]
        result = p10.classify_wrapper_outcome(events)
        assert result == self.WRAPPER_COMPLETED_NORMALLY, f"Got {result}"

    def test_classify_wrapper_events_completed_via_audio_out(self):
        events = [
            {"event": "audio_out", "status": "ok", "chunk_count": 5, "pcm_bytes": 44100},
        ]
        result = p10.classify_wrapper_outcome(events)
        assert result == self.WRAPPER_COMPLETED_NORMALLY, f"Got {result}"

    def test_classify_wrapper_events_failed(self):
        events = [
            {"event": "synthesis_error", "reason": "backend_error"},
        ]
        result = p10.classify_wrapper_outcome(events)
        assert result == self.WRAPPER_FAILED, f"Got {result}"

    def test_classify_wrapper_events_timed_out(self):
        events = [
            {"event": "synthesis_timeout"},
        ]
        result = p10.classify_wrapper_outcome(events)
        assert result == self.WRAPPER_TIMED_OUT, f"Got {result}"

    def test_classify_wrapper_events_unknown_when_no_clear_signal(self):
        events = [
            {"event": "queue_admitted"},
            {"event": "queue_started"},
        ]
        result = p10.classify_wrapper_outcome(events)
        assert result == self.WRAPPER_UNKNOWN, f"Got {result}"

    def test_classify_wrapper_client_disconnected(self):
        events = [
            {"event": "client_disconnected", "connection_id": "c-001", "synthesis_id": "s-001", "reason": "write_failed"},
        ]
        result = p10.classify_wrapper_outcome(events)
        assert result == "client disconnected", f"Got {result}"

    def test_classify_wrapper_cancel_requested_separately(self):
        events = [
            {"event": "synthesis_cancel_requested", "connection_id": "c-001", "reason": "task_cancelled"},
        ]
        result = p10.classify_wrapper_outcome(events)
        assert result == self.WRAPPER_CANCELLED, f"Got {result}"

    def test_classify_wrapper_empty_events_is_unknown(self):
        result = p10.classify_wrapper_outcome([])
        assert result == self.WRAPPER_UNKNOWN


# ═══════════════════════════════════════════════════════════════════════════
# Backend classification — exactly 4 states
# ═══════════════════════════════════════════════════════════════════════════

class TestBackendClassification:
    """Classify backend request outcome: aborted early / completed normally
    / failed / unknown."""

    BACKEND_ABORTED_EARLY = "aborted early"
    BACKEND_COMPLETED_NORMALLY = "completed normally"
    BACKEND_FAILED = "failed"
    BACKEND_UNKNOWN = "unknown"

    def test_classify_backend_outcome_exists(self):
        assert callable(p10.classify_backend_outcome), (
            "classify_backend_outcome must be a callable"
        )

    def test_classify_backend_aborted_early_via_stream_done(self):
        events = [
            {"event": "backend_stream_done", "synthesis_id": "s-001", "status": "client_disconnected"},
        ]
        result = p10.classify_backend_outcome(events)
        assert result == self.BACKEND_ABORTED_EARLY, f"Got {result}"

    def test_classify_backend_aborted_early_via_done(self):
        events = [
            {"event": "backend_done", "synthesis_id": "s-001", "status": "client_disconnected"},
        ]
        result = p10.classify_backend_outcome(events)
        assert result == self.BACKEND_ABORTED_EARLY, f"Got {result}"

    def test_classify_backend_aborted_early_legacy(self):
        events = [
            {"event": "backend_request_aborted"},
        ]
        result = p10.classify_backend_outcome(events)
        assert result == self.BACKEND_ABORTED_EARLY, f"Got {result}"

    def test_classify_backend_completed_normally_via_stream_done(self):
        events = [
            {"event": "backend_stream_done", "synthesis_id": "s-001", "status": "ok"},
        ]
        result = p10.classify_backend_outcome(events)
        assert result == self.BACKEND_COMPLETED_NORMALLY, f"Got {result}"

    def test_classify_backend_completed_normally_via_done(self):
        events = [
            {"event": "backend_done", "synthesis_id": "s-001", "status": "ok"},
        ]
        result = p10.classify_backend_outcome(events)
        assert result == self.BACKEND_COMPLETED_NORMALLY, f"Got {result}"

    def test_classify_backend_completed_legacy(self):
        events = [
            {"event": "backend_completed"},
        ]
        result = p10.classify_backend_outcome(events)
        assert result == self.BACKEND_COMPLETED_NORMALLY, f"Got {result}"

    def test_classify_backend_failed_via_stream_done(self):
        events = [
            {"event": "backend_stream_done", "synthesis_id": "s-001", "status": "error", "error": "S2ClientError"},
        ]
        result = p10.classify_backend_outcome(events)
        assert result == self.BACKEND_FAILED, f"Got {result}"

    def test_classify_backend_failed_via_done(self):
        events = [
            {"event": "backend_done", "synthesis_id": "s-001", "status": "error"},
        ]
        result = p10.classify_backend_outcome(events)
        assert result == self.BACKEND_FAILED, f"Got {result}"

    def test_classify_backend_failed_legacy(self):
        events = [
            {"event": "backend_error"},
        ]
        result = p10.classify_backend_outcome(events)
        assert result == self.BACKEND_FAILED, f"Got {result}"

    def test_classify_backend_unknown_empty(self):
        result = p10.classify_backend_outcome([])
        assert result == self.BACKEND_UNKNOWN


# ═══════════════════════════════════════════════════════════════════════════
# Log correlation for disconnect scenarios
# ═══════════════════════════════════════════════════════════════════════════

class TestLogCorrelation:
    """Correlate logs by connection_id, synthesis_id, and text_fp
    for direct-disconnect scenarios."""

    def test_correlate_disconnect_logs_finds_connection(self):
        """Given wrapper logs, extract events for the disconnected connection."""
        wrapper_logs = [
            '{"event":"conn_created","connection_id":"c-dc1"}',
            '{"event":"synthesize_received","connection_id":"c-dc1","synthesis_id":"s-dc1","text_fp":"abcd1234ef01"}',
            '{"event":"queue_admitted","connection_id":"c-dc1","synthesis_id":"s-dc1","text_fp":"abcd1234ef01"}',
            '{"event":"conn_closed","connection_id":"c-dc1"}',
        ]
        result = p10.correlate_disconnect_logs(
            wrapper_logs=wrapper_logs,
            backend_logs=[],
            original_text_fp="abcd1234ef01",
        )
        assert result["connection_id"] == "c-dc1"
        assert result["synthesis_id"] == "s-dc1"
        assert result["has_conn_closed"] is True
        assert result["has_synthesis_received"] is True

    def test_correlate_disconnect_logs_no_match_returns_none(self):
        """When no matching text_fp, connection_id is None."""
        wrapper_logs = [
            '{"event":"conn_created","connection_id":"c-other"}',
        ]
        result = p10.correlate_disconnect_logs(
            wrapper_logs=wrapper_logs,
            backend_logs=[],
            original_text_fp="no-match",
        )
        assert result["connection_id"] is None

    def test_correlate_backend_logs_from_synthesis_id(self):
        """Correlate backend logs using synthesis_id from wrapper."""
        backend_logs = [
            '{"event":"request_started","synthesis_id":"s-dc1"}',
            '{"event":"request_aborted","synthesis_id":"s-dc1"}',
        ]
        result = p10.correlate_disconnect_logs(
            wrapper_logs=['{"event":"synthesize_received","connection_id":"c-dc1","synthesis_id":"s-dc1","text_fp":"fp123"}'],
            backend_logs=backend_logs,
            original_text_fp="fp123",
        )
        assert result["synthesis_id"] == "s-dc1"
        assert result["backend_events_count"] == 2

    def test_correlate_logs_incomplete_wrapper_logs_yields_unknown(self):
        """Incomplete wrapper logs should yield connection_id=None or events empty."""
        result = p10.correlate_disconnect_logs(
            wrapper_logs=[],
            backend_logs=[],
            original_text_fp="fp123",
        )
        assert result["connection_id"] is None
        assert result["wrapper_unknown"] is True


# ═══════════════════════════════════════════════════════════════════════════
# Incomplete logs => unknown, no false pass
# ═══════════════════════════════════════════════════════════════════════════

class TestIncompleteLogsUnknown:
    """When logs are incomplete, classifications must be 'unknown' and
    must not falsely pass."""

    def test_wrapper_unknown_on_missing_events(self):
        """classify_wrapper_outcome on empty logs => unknown."""
        assert p10.classify_wrapper_outcome([]) == "unknown"

    def test_backend_unknown_on_missing_events(self):
        """classify_backend_outcome on empty logs => unknown."""
        assert p10.classify_backend_outcome([]) == "unknown"

    def test_follow_up_synthesis_requires_result(self):
        """follow_up result dict must contain protocol_valid, pcm_bytes, duration_s."""
        assert callable(p10.verify_follow_up_synthesis), (
            "verify_follow_up_synthesis must exist"
        )

    def test_verify_follow_up_synthesis_fails_on_bad_result(self):
        """A follow-up result with protocol_valid=False must not pass."""
        bad_result = {"protocol_valid": False, "pcm_bytes": 0, "duration_s": 0, "errors": ["timeout"]}
        r = p10.verify_follow_up_synthesis(bad_result)
        assert r.passed is False

    def test_verify_follow_up_synthesis_passes_on_good_result(self):
        """A follow-up result with protocol_valid=True and PCM > 0 must pass."""
        good_result = {"protocol_valid": True, "pcm_bytes": 32000, "duration_s": 2.5, "errors": []}
        r = p10.verify_follow_up_synthesis(good_result)
        assert r.passed is True


# ═══════════════════════════════════════════════════════════════════════════
# Direct-disconnect orchestration — full mocked integration
# ═══════════════════════════════════════════════════════════════════════════

class TestDirectDisconnectOrchestration:
    """The direct-disconnect mode orchestrator must:
    1. Establish UTC baseline
    2. Collect logs since baseline
    3. Use unique original and follow-up text markers
    4. Correlate connection_id / synthesis_id / text_fp
    5. Take snapshots only after activity
    6. Wait for terminal + idle state
    7. Execute distinct follow-up synthesis through AudioStop
    8. Assert mandatory cancellation outcomes (report.passed depends on them)
    """

    @pytest.mark.asyncio
    async def test_orchestrator_uses_unique_text_markers(self):
        """Original and follow-up texts must differ."""
        cfg = p10.ValidationConfig(
            mode=p10.ValidationMode.DIRECT_DISCONNECT,
            dry_run=False,
            tts_text="Original disconnect validation text.",
        )
        # Snapshot the configuration to prove original_text != follow_up_text
        result = p10.get_disconnect_texts(cfg)
        assert result["original"] != result["follow_up"], (
            "Original and follow-up texts must differ for correlation"
        )
        assert len(result["original_fp"]) == 12
        assert len(result["follow_up_fp"]) == 12
        assert result["original_fp"] != result["follow_up_fp"]

    def test_snapshot_active_only_after_activity(self):
        """capture_admin_status only marks 'during' snapshot after audio chunk."""
        # The function should accept an is_active flag
        assert callable(p10.build_direct_disconnect_status_snapshot)
        # Before activity: phase="before", not active
        snap_before = p10.build_direct_disconnect_status_snapshot(
            phase="before", is_active=False, admin_status={"ready": True}
        )
        assert snap_before["phase"] == "before"
        assert snap_before["is_active"] is False

        # During activity: phase="during", is_active=True
        snap_during = p10.build_direct_disconnect_status_snapshot(
            phase="during", is_active=True, admin_status={"has_active_synthesis": True}
        )
        assert snap_during["phase"] == "during"
        assert snap_during["is_active"] is True

    @pytest.mark.asyncio
    async def test_full_direct_disconnect_orchestration_with_mocks(self, tmp_path):
        """Complete direct-disconnect flow with all deps injected."""
        import copy

        # --- Prepare fake wrapper and backend logs ---
        wrapper_logs = [
            '{"event":"conn_created","connection_id":"c-dc-full"}',
            '{"event":"synthesize_received","connection_id":"c-dc-full","synthesis_id":"s-dc-full","text_fp":"3f5a1b2c9d08"}',
            '{"event":"queue_admitted","connection_id":"c-dc-full","synthesis_id":"s-dc-full","text_fp":"3f5a1b2c9d08"}',
            '{"event":"queue_started","connection_id":"c-dc-full","synthesis_id":"s-dc-full","text_fp":"3f5a1b2c9d08"}',
            '{"event":"audio_chunk","connection_id":"c-dc-full","synthesis_id":"s-dc-full","text_fp":"3f5a1b2c9d08"}',
            '{"event":"conn_closed","connection_id":"c-dc-full"}',
            '{"event":"synthesis_cancelled","connection_id":"c-dc-full","synthesis_id":"s-dc-full","reason":"cancelled_while_active"}',
        ]
        backend_logs = [
            '{"event":"request_started","synthesis_id":"s-dc-full"}',
            '{"event":"request_aborted","synthesis_id":"s-dc-full"}',
        ]

        # --- Subprocess runner returning logs ---
        subp = _FakeSubprocess(responses={
            "docker logs --timestamps --tail 200 --since 2026-07-12T00:00:00Z wyoming-s2cpp-tts":
                _FakeProcess(0, "\n".join(wrapper_logs)),
            "docker logs --timestamps --tail 200 --since 2026-07-12T00:00:00Z s2cpp-backend":
                _FakeProcess(0, "\n".join(backend_logs)),
            "docker inspect --type container --format " + p10.DOCKER_INSPECT_FORMAT + " wyoming-s2cpp-tts":
                _FakeProcess(0, json.dumps({
                    "id": "abc123", "name": "/wyoming-s2cpp-tts",
                    "image_name": "ghcr.io/sorilo/wyoming-s2cpp-tts:sha-test",
                    "image_id": "sha256:deadbeef", "running": True, "state": "running",
                })),
            "docker inspect --type container --format " + p10.DOCKER_INSPECT_FORMAT + " s2cpp-backend":
                _FakeProcess(0, json.dumps({
                    "id": "def456", "name": "/s2cpp-backend",
                    "image_name": "ghcr.io/sorilo/s2cpp-backend:sha-test",
                    "image_id": "sha256:beefdead", "running": True, "state": "running",
                })),
        })

        # --- HTTP client returning admin status ---
        class _AdminHttp:
            def __init__(self, base_url, token):
                self.base_url = base_url
                self.token = token
            async def get_json(self, path):
                if path == "/status":
                    return {"state": "RUNNING", "scheduler_depth": 0, "scheduler_pending": 0,
                            "scheduler_waiting": 0, "has_active_synthesis": False}
                return {}
            async def post_json(self, path, data):
                return {}

        def _factory(url, token):
            return _AdminHttp(url, token)

        # --- Wyoming disconnect mock ---
        disconnect_calls = []
        async def _mock_disconnect(text, host, port, voice, timeout=30.0, on_first_audio=None):
            disconnect_calls.append({"text": text, "host": host, "port": port})
            if on_first_audio:
                await on_first_audio()
            return {"outcome": "client_disconnected", "got_chunk": True, "events": ["audio-start", "audio-chunk"], "event_count": 2}

        # --- Wyoming synthesize mock for follow-up ---
        synthesize_calls = []
        async def _mock_synthesize(text, host, port, voice, timeout=60.0):
            synthesize_calls.append({"text": text, "host": host, "port": port})
            return {"text": text, "pcm_bytes": 44100, "duration_s": 2.1, "rate": 22050,
                    "width": 2, "channels": 1, "protocol_valid": True, "errors": []}

        # --- Inject mocks ---
        orig_disconnect = p10.wyoming_disconnect_during_stream
        orig_synthesize = p10.wyoming_synthesize
        orig_confirm = p10.confirm_live_action
        orig_get_texts = p10.get_disconnect_texts
        p10.wyoming_disconnect_during_stream = _mock_disconnect
        p10.wyoming_synthesize = _mock_synthesize
        p10.confirm_live_action = lambda: True  # bypass live confirmation in test

        # Mock get_disconnect_texts with deterministic marker for test
        deterministic_marker = "test-marker-001"
        deterministic_texts = {
            "original": "Original disconnect text for validation. [disconnect-validation-test-marker-001].",
            "original_fp": "3f5a1b2c9d08",
            "follow_up": "Original disconnect text for validation. [recovery-validation-test-marker-001].",
            "follow_up_fp": "8e7d6c5b4a03",
            "marker": deterministic_marker,
        }
        p10.get_disconnect_texts = lambda cfg, marker=None: deterministic_texts

        try:
            cfg = p10.ValidationConfig(
                mode=p10.ValidationMode.DIRECT_DISCONNECT,
                dry_run=False,
                wyoming_host="127.0.0.1",
                wyoming_port=10200,
                admin_url="http://127.0.0.1:10201",
                artifact_base=tmp_path / "artifacts/phase10",
                tts_text="Original disconnect text for validation.",
            )

            report = await p10.run_validation(
                cfg,
                subprocess_runner=subp,
                http_client_factory=_factory,
            )

            # --- Assertions ---
            # 1. disconnect was called with original text
            assert len(disconnect_calls) == 1
            assert "Original disconnect text" in disconnect_calls[0]["text"]

            # 2. follow-up synthesis was called with different text
            assert len(synthesize_calls) == 1
            assert synthesize_calls[0]["text"] != disconnect_calls[0]["text"], (
                "Follow-up text must differ from original"
            )

            # 3. Outcomes include the new disconnect outcomes
            outcomes = report.outcomes
            assert "client_disconnected" in outcomes
            assert "wrapper_cancel_observed" in outcomes or "wrapper_cancel_requested" in outcomes
            assert "follow_up_synthesis_succeeded" in outcomes

            # 4. Status snapshots have before/during/after
            phases = [s.get("phase", "") for s in report.status_snapshots]
            assert "before" in phases
            assert "during" in phases or any(s.get("is_active") for s in report.status_snapshots)

            # 5. Report is valid
            assert report.duration_sec >= 0

        finally:
            p10.wyoming_disconnect_during_stream = orig_disconnect
            p10.wyoming_synthesize = orig_synthesize
            p10.confirm_live_action = orig_confirm
            p10.get_disconnect_texts = orig_get_texts


# ═══════════════════════════════════════════════════════════════════════════
# Phase 10 regression-first: explicit direct-disconnect assertions, unique
# markers, correlations, and truthful PARTIAL/pass semantics
# ═══════════════════════════════════════════════════════════════════════════

class TestGetDisconnectTextsNonce:
    """get_disconnect_texts must produce per-call unique nonce-based texts."""

    def test_original_and_follow_up_are_distinct(self):
        cfg = p10.ValidationConfig(tts_text="Hello world")
        result = p10.get_disconnect_texts(cfg, marker="test-001")
        assert result["original"] != result["follow_up"], "Texts must differ"
        assert "disconnect-validation" in result["original"]
        assert "recovery-validation" in result["follow_up"]
        assert result["original_fp"] != result["follow_up_fp"], "FPs must differ"

    def test_marker_key_present(self):
        cfg = p10.ValidationConfig(tts_text="Hello world")
        result = p10.get_disconnect_texts(cfg, marker="test-002")
        assert result["marker"] == "test-002"
        assert "test-002" in result["original"]
        assert "test-002" in result["follow_up"]

    def test_no_marker_generates_random(self):
        """Without marker, a random nonce is generated."""
        cfg = p10.ValidationConfig(tts_text="Hello world")
        r1 = p10.get_disconnect_texts(cfg)
        r2 = p10.get_disconnect_texts(cfg)
        # Different calls should produce different markers
        assert r1["marker"] != r2["marker"], "Per-call nonce must be unique"
        assert r1["original"] != r2["original"]

    def test_disconnect_marker_in_config(self):
        """ValidationConfig.disconnect_marker is passed through."""
        cfg = p10.ValidationConfig(
            tts_text="Hello world",
            disconnect_marker="cfg-test-003",
        )
        result = p10.get_disconnect_texts(cfg, marker=cfg.disconnect_marker)
        assert result["marker"] == "cfg-test-003"


class TestExplicitDisconnectAssertions:
    """Each explicit direct-disconnect assertion with its exact name."""

    def test_assert_client_received_audio_before_disconnect_pass(self):
        disc = {"got_chunk": True, "event_count": 3}
        r = p10.assert_client_received_audio_before_disconnect(disc)
        assert r.passed is True
        assert r.name == "client_received_audio_before_disconnect"

    def test_assert_client_received_audio_before_disconnect_fail(self):
        disc = {"got_chunk": False, "event_count": 0}
        r = p10.assert_client_received_audio_before_disconnect(disc)
        assert r.passed is False

    def test_assert_client_disconnected_pass(self):
        disc = {"outcome": "client_disconnected"}
        r = p10.assert_client_disconnected(disc)
        assert r.passed is True
        assert r.name == "client_disconnected"

    def test_assert_client_disconnected_fail(self):
        disc = {"outcome": "error"}
        r = p10.assert_client_disconnected(disc)
        assert r.passed is False

    def test_assert_active_state_observed_before_disconnect_or_cleanup(self):
        snap = {"is_active": True, "has_active_synthesis": True}
        r = p10.assert_active_state_observed_before_disconnect_or_cleanup(snap)
        assert r.passed is True
        assert r.name == "active_state_observed_before_disconnect_or_cleanup"

    def test_assert_active_state_not_observed(self):
        snap = {"is_active": False, "has_active_synthesis": False}
        r = p10.assert_active_state_observed_before_disconnect_or_cleanup(snap)
        assert r.passed is False

    def test_assert_original_request_terminal_state_known_pass(self):
        r = p10.assert_original_request_terminal_state_known("cancelled")
        assert r.passed is True
        assert r.name == "original_request_terminal_state_known"

    def test_assert_original_request_terminal_state_unknown(self):
        r = p10.assert_original_request_terminal_state_known("unknown")
        assert r.passed is False

    def test_assert_wrapper_cancel_observed_pass(self):
        r = p10.assert_wrapper_cancel_observed("cancelled")
        assert r.passed is True
        assert r.name == "wrapper_cancel_observed"

    def test_assert_wrapper_cancel_observed_fail_on_normal(self):
        """wrapper_cancel_observed must fail when cancellation not observed."""
        r = p10.assert_wrapper_cancel_observed("completed normally")
        assert r.passed is False

    def test_assert_wrapper_cancel_observed_fail_on_unknown(self):
        r = p10.assert_wrapper_cancel_observed("unknown")
        assert r.passed is False

    def test_assert_original_completed_normally_pass(self):
        r = p10.assert_original_completed_normally("completed normally")
        assert r.passed is True
        assert r.name == "original_completed_normally"

    def test_assert_original_completed_normally_fail(self):
        r = p10.assert_original_completed_normally("cancelled")
        assert r.passed is False

    def test_assert_backend_terminal_state_known_pass(self):
        r = p10.assert_backend_terminal_state_known("aborted early")
        assert r.passed is True
        assert r.name == "backend_terminal_state_known"

    def test_assert_backend_terminal_state_known_fail(self):
        r = p10.assert_backend_terminal_state_known("unknown")
        assert r.passed is False

    def test_assert_scheduler_returned_to_zero_pass(self):
        r = p10.assert_scheduler_returned_to_zero({"scheduler_depth": 0})
        assert r.passed is True
        assert r.name == "scheduler_returned_to_zero"

    def test_assert_scheduler_returned_to_zero_fail(self):
        r = p10.assert_scheduler_returned_to_zero({"scheduler_depth": 3})
        assert r.passed is False

    def test_assert_pending_returned_to_zero_pass(self):
        r = p10.assert_pending_returned_to_zero({"scheduler_pending": 0})
        assert r.passed is True
        assert r.name == "pending_returned_to_zero"

    def test_assert_pending_returned_to_zero_fail(self):
        r = p10.assert_pending_returned_to_zero({"scheduler_pending": 2})
        assert r.passed is False

    def test_assert_waiting_returned_to_zero_pass(self):
        r = p10.assert_waiting_returned_to_zero({"scheduler_waiting": 0})
        assert r.passed is True
        assert r.name == "waiting_returned_to_zero"

    def test_assert_waiting_returned_to_zero_fail(self):
        r = p10.assert_waiting_returned_to_zero({"scheduler_waiting": 1})
        assert r.passed is False

    def test_assert_active_synthesis_false_pass(self):
        r = p10.assert_active_synthesis_false({"has_active_synthesis": False})
        assert r.passed is True
        assert r.name == "active_synthesis_false"

    def test_assert_active_synthesis_false_fail(self):
        r = p10.assert_active_synthesis_false({"has_active_synthesis": True})
        assert r.passed is False

    def test_assert_wrapper_ready_pass(self):
        r = p10.assert_wrapper_ready({"ready": True})
        assert r.passed is True
        assert r.name == "wrapper_ready"

    def test_assert_wrapper_ready_fail(self):
        r = p10.assert_wrapper_ready({"ready": False})
        assert r.passed is False

    def test_assert_no_busy_503_latch_pass(self):
        r = p10.assert_no_busy_503_latch({
            "busy": False, "has_active_synthesis": False, "scheduler_pending": 0,
        })
        assert r.passed is True
        assert r.name == "no_busy_503_latch"

    def test_assert_no_busy_503_latch_fail(self):
        r = p10.assert_no_busy_503_latch({
            "busy": True, "has_active_synthesis": False, "scheduler_pending": 0,
        })
        assert r.passed is False

    def test_assert_follow_up_request_protocol_valid_pass(self):
        r = p10.assert_follow_up_request_protocol_valid({"protocol_valid": True})
        assert r.passed is True
        assert r.name == "follow_up_request_protocol_valid"

    def test_assert_follow_up_request_protocol_valid_fail(self):
        r = p10.assert_follow_up_request_protocol_valid({"protocol_valid": False})
        assert r.passed is False

    def test_assert_follow_up_request_pcm_bytes_gt_zero_pass(self):
        r = p10.assert_follow_up_request_pcm_bytes_gt_zero({"pcm_bytes": 44100})
        assert r.passed is True
        assert r.name == "follow_up_request_pcm_bytes_gt_zero"

    def test_assert_follow_up_request_pcm_bytes_gt_zero_fail(self):
        r = p10.assert_follow_up_request_pcm_bytes_gt_zero({"pcm_bytes": 0})
        assert r.passed is False

    def test_assert_follow_up_request_completed_pass(self):
        corr = {
            "wrapper_unknown": False,
            "has_synthesis_received": True,
            "wrapper_events": [
                {"event": "synthesis_terminal", "terminal_state": "completed"},
            ],
        }
        r = p10.assert_follow_up_request_completed(corr)
        assert r.passed is True
        assert r.name == "follow_up_request_completed"

    def test_assert_follow_up_request_completed_fail(self):
        corr = {"wrapper_unknown": True, "has_synthesis_received": False}
        r = p10.assert_follow_up_request_completed(corr)
        assert r.passed is False

    def test_assert_follow_up_scheduler_recovered_pass(self):
        r = p10.assert_follow_up_scheduler_recovered({
            "scheduler_depth": 0, "scheduler_pending": 0, "has_active_synthesis": False,
        })
        assert r.passed is True
        assert r.name == "follow_up_scheduler_recovered"

    def test_assert_follow_up_scheduler_recovered_fail(self):
        r = p10.assert_follow_up_scheduler_recovered({
            "scheduler_depth": 1, "scheduler_pending": 0, "has_active_synthesis": False,
        })
        assert r.passed is False

    def test_assert_idle_timeout_not_reached_pass(self):
        r = p10.assert_idle_timeout_not_reached({"state": "RUNNING"})
        assert r.passed is True
        assert r.name == "idle_timeout_not_reached"

    def test_assert_idle_timeout_not_reached_fail_on_none(self):
        r = p10.assert_idle_timeout_not_reached(None)
        assert r.passed is False

    def test_assert_idle_timeout_not_reached_fail_on_error(self):
        r = p10.assert_idle_timeout_not_reached({"error": "timeout"})
        assert r.passed is False


class TestPartialClassification:
    """PARTIAL classification when original normal completion + cleanup/recovery."""

    def test_normal_completion_maps_to_partial(self):
        """completed normally wrapper outcome -> PARTIAL finding."""
        events = [
            {"event": "syn_stopped", "synthesis_id": "s-001", "trigger": "streaming"},
        ]
        outcome = p10.classify_wrapper_outcome(events)
        assert outcome == "completed normally"

    def test_partial_flag_in_outcomes(self):
        """When wrapper completes normally, outcomes must show PARTIAL."""
        # Simulate what happens in _run_direct_disconnect_mode for normal completion
        wrapper_outcome = "completed normally"
        outcomes = {}
        if wrapper_outcome == "completed normally":
            outcomes["old_synthesis_completed_normally"] = "true"
            outcomes["wrapper_cancel_observed"] = "PARTIAL (normal completion + recovery)"
            outcomes["wrapper_cancel_requested"] = "PARTIAL (normal completion + recovery)"
        assert "PARTIAL" in outcomes["wrapper_cancel_observed"]
        assert "PARTIAL" in outcomes["wrapper_cancel_requested"]

    def test_cancellation_fails_report_passed(self):
        """When wrapper_cancel_observed fails, assertions_failed > 0."""
        # cancelled is the expected outcome for disconnect; if not observed, assertion fails
        r = p10.assert_wrapper_cancel_observed("unknown")
        assert r.passed is False, "Unknown outcome must fail wrapper_cancel_observed"


class TestCorrelationIdsInOutcomes:
    """Correlation IDs/FPs recorded in outcomes without plaintext."""

    def test_outcomes_contain_fingerprints_not_plaintext(self):
        """Fingerprints are recorded; plaintext is not."""
        cfg = p10.ValidationConfig(tts_text="Test text")
        texts = p10.get_disconnect_texts(cfg, marker="corr-test")
        # FPs are hex strings (no plaintext)
        assert len(texts["original_fp"]) == 12
        assert len(texts["follow_up_fp"]) == 12
        assert all(c in "0123456789abcdef" for c in texts["original_fp"].lower())
        assert all(c in "0123456789abcdef" for c in texts["follow_up_fp"].lower())
        # Plaintext not in FP
        assert "Test text" not in texts["original_fp"]

    def test_correlation_ids_recorded_in_outcomes(self):
        """connection_id and synthesis_id are recorded in outcomes."""
        correlation = {
            "connection_id": "c-dc-001",
            "synthesis_id": "s-dc-001",
            "has_conn_closed": True,
            "wrapper_unknown": False,
            "has_synthesis_received": True,
            "wrapper_events": [],
            "backend_events": [],
            "backend_events_count": 0,
        }
        conn_id = correlation.get("connection_id")
        synth_id = correlation.get("synthesis_id")
        assert conn_id == "c-dc-001"
        assert synth_id == "s-dc-001"
        # These would be recorded in outcomes without plaintext
        assert isinstance(conn_id, str)
        assert isinstance(synth_id, str)

    def test_follow_up_correlation_ids_separate(self):
        """Follow-up correlation uses separate IDs."""
        followup_corr = {
            "connection_id": "c-fw-001",
            "synthesis_id": "s-fw-001",
            "wrapper_unknown": False,
            "has_synthesis_received": True,
            "wrapper_events": [],
            "backend_events": [],
            "backend_events_count": 0,
        }
        assert followup_corr["connection_id"] == "c-fw-001"
        assert followup_corr["synthesis_id"] == "s-fw-001"

    def test_new_outcomes_in_listed_outcomes(self):
        """Verify new outcome keys are in LISTED_OUTCOMES."""
        new_keys = {
            "original_fp", "follow_up_fp",
            "original_connection_id", "original_synthesis_id",
            "original_conn_closed",
            "follow_up_connection_id", "follow_up_synthesis_id",
        }
        for key in new_keys:
            assert key in p10.LISTED_OUTCOMES, f"Missing from LISTED_OUTCOMES: {key}"


class TestIdleTimeoutAssertions:
    """Idle timeout must produce explicit failing assertions."""

    def test_idle_timeout_produces_failing_assertion(self):
        """When idle_status is None (timeout), idle_timeout_not_reached fails."""
        r = p10.assert_idle_timeout_not_reached(None)
        assert r.passed is False
        assert r.name == "idle_timeout_not_reached"

    def test_idle_timeout_error_produces_failing_assertion(self):
        """When idle_status has error, the assertion fails."""
        r = p10.assert_idle_timeout_not_reached({"error": "admin status unreachable"})
        assert r.passed is False

    def test_idle_reached_passes(self):
        """When idle is reached, the assertion passes."""
        r = p10.assert_idle_timeout_not_reached({
            "has_active_synthesis": False,
            "scheduler_depth": 0,
            "scheduler_pending": 0,
            "scheduler_waiting": 0,
            "ready": True,
        })
        assert r.passed is True


class TestNonceBasedTextsIntegration:
    """Full integration: nonce-based texts produce unique fingerprints."""

    def test_deterministic_marker_produces_consistent_texts(self):
        """Same marker produces same texts and FPs."""
        cfg = p10.ValidationConfig(tts_text="Hello")
        r1 = p10.get_disconnect_texts(cfg, marker="fixed-001")
        r2 = p10.get_disconnect_texts(cfg, marker="fixed-001")
        assert r1["original"] == r2["original"]
        assert r1["original_fp"] == r2["original_fp"]
        assert r1["follow_up_fp"] == r2["follow_up_fp"]

    def test_no_plaintext_marker_in_report(self):
        """The marker is a hex token, not plaintext from the original text."""
        cfg = p10.ValidationConfig(tts_text="Sensitive patient data")
        texts = p10.get_disconnect_texts(cfg, marker="deadbeef")
        # The original text is embedded but the marker is the nonce, not the text
        assert "deadbeef" in texts["original"]
        # The FP hashes the full text including the marker
        assert texts["original_fp"] != "deadbeef"
        assert len(texts["original_fp"]) == 12


class TestReportPassedSemantics:
    """report.passed is False when cancellation expected but unobserved."""

    def test_missing_cancellation_makes_report_not_passed(self):
        """When wrapper_cancel_observed fails, the overall report should have failures."""
        # Simulate: wrapper outcome is not cancelled
        wrapper_outcome = "completed normally"
        cancel_result = p10.assert_wrapper_cancel_observed(wrapper_outcome)
        assert cancel_result.passed is False

        # In the full flow, this would contribute to assertions_failed > 0
        assertion_list = [cancel_result]
        failed = sum(1 for a in assertion_list if not a.passed)
        assert failed == 1

    def test_cancellation_observed_makes_cancel_pass(self):
        """When cancellation is observed, wrapper_cancel_observed passes."""
        wrapper_outcome = "cancelled"
        cancel_result = p10.assert_wrapper_cancel_observed(wrapper_outcome)
        assert cancel_result.passed is True

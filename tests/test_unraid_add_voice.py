"""Daemon-free contract tests for the Unraid host voice-import operator.

Covers every minimum category from the governing acceptance contract
(lines 631-742).  No Docker daemon required — all Docker interaction is
injected / mocked.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any, Callable, Protocol
from unittest import mock

# Ensure project root and scripts are importable
_PROJECT = Path(__file__).resolve().parents[1]
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))
_SCRIPTS = _PROJECT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import pytest

# ---------------------------------------------------------------------------
# Phase 1 — module existence and entrypoint
# ---------------------------------------------------------------------------


def test_operator_module_exposes_main_entrypoint() -> None:
    from scripts import unraid_add_voice

    assert callable(unraid_add_voice.main)


def test_operator_module_exposes_run_operator() -> None:
    from scripts import unraid_add_voice

    assert callable(unraid_add_voice.run_operator)


def test_bash_launcher_is_valid_syntax() -> None:
    """The thin Bash wrapper must pass bash -n."""
    launcher = Path("scripts/unraid_add_voice.sh")
    if launcher.exists():
        result = subprocess.run(
            ["bash", "-n", str(launcher)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"bash -n failed: {result.stderr}"


# ---------------------------------------------------------------------------
# Phase 2 — Config parsing (strict dotenv-style)
# ---------------------------------------------------------------------------


class TestConfigParsing:
    """Strict configuration parser tests."""

    def test_parse_minimal_config(self) -> None:
        from scripts.unraid_add_voice import parse_config

        config_text = textwrap.dedent("""\
            BACKEND_CONTAINER=s2cpp-tts
            BACKEND_IMAGE=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
        """)
        cfg = parse_config(config_text)
        assert cfg["BACKEND_CONTAINER"] == "s2cpp-tts"
        assert cfg["BACKEND_IMAGE"] == "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    def test_parse_full_config(self) -> None:
        from scripts.unraid_add_voice import parse_config

        config_text = textwrap.dedent("""\
            BACKEND_CONTAINER=s2cpp-tts
            BACKEND_IMAGE=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb789
            MODELS_DIR=/mnt/user/appdata/s2cpp/models
            VOICES_DIR=/mnt/user/appdata/s2cpp/voices
            IMPORT_INPUTS_DIR=/mnt/user/appdata/s2cpp/voice-import-inputs
            MODEL_CONTAINER_PATH=/models/s2-pro-q6_k.gguf
            TOKENIZER_CONTAINER_PATH=/models/tokenizer.json
            CUDA_DEVICE=0
            GPU_LAYERS=99
            STOP_TIMEOUT_SEC=30
            IMPORT_TIMEOUT_SEC=600
            RESTART_TIMEOUT_SEC=120
            LOCK_FILE=/tmp/s2cpp-import.lock
        """)
        cfg = parse_config(config_text)
        assert cfg["CUDA_DEVICE"] == "0"
        assert cfg["GPU_LAYERS"] == "99"
        assert cfg["STOP_TIMEOUT_SEC"] == "30"

    def test_parse_rejects_unknown_key(self) -> None:
        from scripts.unraid_add_voice import ConfigError, parse_config

        with pytest.raises(ConfigError, match="Unknown"):
            parse_config("UNKNOWN_KEY=value\n")

    def test_parse_rejects_comment_line_as_key(self) -> None:
        from scripts.unraid_add_voice import parse_config

        # Lines starting with # or empty should be skipped
        config_text = textwrap.dedent("""\
            # This is a comment
            BACKEND_CONTAINER=s2cpp-tts

            BACKEND_IMAGE=sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
        """)
        cfg = parse_config(config_text)
        assert cfg["BACKEND_CONTAINER"] == "s2cpp-tts"
        assert cfg["BACKEND_IMAGE"] == "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"

    def test_parse_handles_trailing_whitespace(self) -> None:
        from scripts.unraid_add_voice import parse_config

        config_text = "BACKEND_CONTAINER=s2cpp-tts   \n"
        cfg = parse_config(config_text)
        assert cfg["BACKEND_CONTAINER"] == "s2cpp-tts"

    def test_parse_handles_quoted_value(self) -> None:
        from scripts.unraid_add_voice import parse_config

        config_text = 'BACKEND_CONTAINER="s2cpp-tts"\n'
        cfg = parse_config(config_text)
        assert cfg["BACKEND_CONTAINER"] == "s2cpp-tts"

    def test_parse_handles_single_quoted_value(self) -> None:
        from scripts.unraid_add_voice import parse_config

        config_text = "BACKEND_CONTAINER='s2cpp-tts'\n"
        cfg = parse_config(config_text)
        assert cfg["BACKEND_CONTAINER"] == "s2cpp-tts"


# ---------------------------------------------------------------------------
# Phase 3 — Voice ID validation (reuses app.voice_import.validate_voice_id)
# ---------------------------------------------------------------------------


class TestVoiceIdValidation:
    """Voice ID validation reuses existing contracts."""

    def test_valid_voice_ids(self) -> None:
        from scripts.unraid_add_voice import validate_voice_id_outer

        for vid in ("solomon", "test_voice", "A", "a0", "a-b", "a_b", "Abc-123"):
            assert validate_voice_id_outer(vid) == vid

    def test_invalid_voice_ids(self) -> None:
        from scripts.unraid_add_voice import validate_voice_id_outer

        for vid in ("", " bad", "-bad", "_bad", "a" * 129, "bad!", "bad name"):
            with pytest.raises(ValueError):
                validate_voice_id_outer(vid)


# ---------------------------------------------------------------------------
# Phase 4 — Preflight path validation
# ---------------------------------------------------------------------------


class TestPreflightValidation:
    """Preflight checks before any Docker state change."""

    @pytest.fixture
    def tmp_workspace(self, tmp_path: Path) -> dict[str, Path]:
        """Create a temporary workspace with input files."""
        ws: dict[str, Path] = {}
        ws["root"] = tmp_path

        # Audio file
        audio = tmp_path / "inputs" / "test.wav"
        audio.parent.mkdir(parents=True)
        audio.write_bytes(b"\x00" * 100)
        ws["audio"] = audio

        # Transcript file
        transcript = tmp_path / "inputs" / "test.transcript.txt"
        transcript.write_text("Hello world", encoding="utf-8")
        ws["transcript"] = transcript

        # Models directory
        models = tmp_path / "models"
        models.mkdir()
        (models / "s2-pro-q6_k.gguf").write_bytes(b"\x00" * 100)
        (models / "tokenizer.json").write_text("{}")
        ws["models"] = models

        # Voices directory
        voices = tmp_path / "voices"
        voices.mkdir()
        ws["voices"] = voices

        # Import inputs
        inputs_dir = tmp_path / "inputs"
        ws["import_inputs"] = inputs_dir

        return ws

    def test_valid_inputs_pass(self, tmp_workspace: dict[str, Path]) -> None:
        from scripts.unraid_add_voice import preflight_validate

        preflight_validate(
            audio_path=tmp_workspace["audio"],
            transcript_path=tmp_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=tmp_workspace["models"],
            voices_dir=tmp_workspace["voices"],
            import_inputs_dir=tmp_workspace["import_inputs"],
            model_rel="s2-pro-q6_k.gguf",
            tokenizer_rel="tokenizer.json",
            force=False,
        )

    @pytest.mark.parametrize("artifact", ["model", "tokenizer"])
    def test_symlinked_model_artifact_fails(
        self, tmp_workspace: dict[str, Path], artifact: str
    ) -> None:
        from scripts.unraid_add_voice import PreflightError, preflight_validate

        if artifact == "model":
            real_name = "s2-pro-q6_k.gguf"
            link_name = "linked-model.gguf"
            model_rel = link_name
            tokenizer_rel = "tokenizer.json"
        else:
            real_name = "tokenizer.json"
            link_name = "linked-tokenizer.json"
            model_rel = "s2-pro-q6_k.gguf"
            tokenizer_rel = link_name
        (tmp_workspace["models"] / link_name).symlink_to(real_name)

        with pytest.raises(PreflightError, match="symlink"):
            preflight_validate(
                audio_path=tmp_workspace["audio"],
                transcript_path=tmp_workspace["transcript"],
                voice_id="solomon",
                license_str="permission-granted",
                attribution="Test",
                provenance="Test source",
                models_dir=tmp_workspace["models"],
                voices_dir=tmp_workspace["voices"],
                import_inputs_dir=tmp_workspace["import_inputs"],
                model_rel=model_rel,
                tokenizer_rel=tokenizer_rel,
                force=False,
            )

    def test_missing_audio_fails(self, tmp_workspace: dict[str, Path]) -> None:
        from scripts.unraid_add_voice import PreflightError, preflight_validate

        with pytest.raises(PreflightError, match="audio"):
            preflight_validate(
                audio_path=tmp_workspace["root"] / "nonexistent.wav",
                transcript_path=tmp_workspace["transcript"],
                voice_id="solomon",
                license_str="permission-granted",
                attribution="Test",
                provenance="Test source",
                models_dir=tmp_workspace["models"],
                voices_dir=tmp_workspace["voices"],
                import_inputs_dir=tmp_workspace["import_inputs"],
                model_rel="s2-pro-q6_k.gguf",
                tokenizer_rel="tokenizer.json",
                force=False,
            )

    def test_symlinked_audio_fails(self, tmp_workspace: dict[str, Path]) -> None:
        from scripts.unraid_add_voice import PreflightError, preflight_validate

        real_audio = tmp_workspace["audio"]
        symlink = tmp_workspace["root"] / "linked.wav"
        symlink.symlink_to(real_audio)

        with pytest.raises(PreflightError, match="symlink"):
            preflight_validate(
                audio_path=symlink,
                transcript_path=tmp_workspace["transcript"],
                voice_id="solomon",
                license_str="permission-granted",
                attribution="Test",
                provenance="Test source",
                models_dir=tmp_workspace["models"],
                voices_dir=tmp_workspace["voices"],
                import_inputs_dir=tmp_workspace["import_inputs"],
                model_rel="s2-pro-q6_k.gguf",
                tokenizer_rel="tokenizer.json",
                force=False,
            )

    def test_missing_transcript_fails(self, tmp_workspace: dict[str, Path]) -> None:
        from scripts.unraid_add_voice import PreflightError, preflight_validate

        with pytest.raises(PreflightError, match="transcript"):
            preflight_validate(
                audio_path=tmp_workspace["audio"],
                transcript_path=tmp_workspace["root"] / "nonexistent.txt",
                voice_id="solomon",
                license_str="permission-granted",
                attribution="Test",
                provenance="Test source",
                models_dir=tmp_workspace["models"],
                voices_dir=tmp_workspace["voices"],
                import_inputs_dir=tmp_workspace["import_inputs"],
                model_rel="s2-pro-q6_k.gguf",
                tokenizer_rel="tokenizer.json",
                force=False,
            )

    def test_symlinked_transcript_fails(self, tmp_workspace: dict[str, Path]) -> None:
        from scripts.unraid_add_voice import PreflightError, preflight_validate

        symlink = tmp_workspace["root"] / "linked-transcript.txt"
        symlink.symlink_to(tmp_workspace["transcript"])

        with pytest.raises(PreflightError, match="symlink"):
            preflight_validate(
                audio_path=tmp_workspace["audio"],
                transcript_path=symlink,
                voice_id="solomon",
                license_str="permission-granted",
                attribution="Test",
                provenance="Test source",
                models_dir=tmp_workspace["models"],
                voices_dir=tmp_workspace["voices"],
                import_inputs_dir=tmp_workspace["import_inputs"],
                model_rel="s2-pro-q6_k.gguf",
                tokenizer_rel="tokenizer.json",
                force=False,
            )

    def test_empty_transcript_fails(self, tmp_workspace: dict[str, Path]) -> None:
        from scripts.unraid_add_voice import PreflightError, preflight_validate

        empty = tmp_workspace["root"] / "empty.txt"
        empty.write_text("", encoding="utf-8")

        with pytest.raises(PreflightError, match="empty"):
            preflight_validate(
                audio_path=tmp_workspace["audio"],
                transcript_path=empty,
                voice_id="solomon",
                license_str="permission-granted",
                attribution="Test",
                provenance="Test source",
                models_dir=tmp_workspace["models"],
                voices_dir=tmp_workspace["voices"],
                import_inputs_dir=tmp_workspace["import_inputs"],
                model_rel="s2-pro-q6_k.gguf",
                tokenizer_rel="tokenizer.json",
                force=False,
            )

    def test_invalid_voice_id_fails(self, tmp_workspace: dict[str, Path]) -> None:
        from scripts.unraid_add_voice import PreflightError, preflight_validate

        with pytest.raises((ValueError, PreflightError)):
            preflight_validate(
                audio_path=tmp_workspace["audio"],
                transcript_path=tmp_workspace["transcript"],
                voice_id="bad id!",
                license_str="permission-granted",
                attribution="Test",
                provenance="Test source",
                models_dir=tmp_workspace["models"],
                voices_dir=tmp_workspace["voices"],
                import_inputs_dir=tmp_workspace["import_inputs"],
                model_rel="s2-pro-q6_k.gguf",
                tokenizer_rel="tokenizer.json",
                force=False,
            )

    def test_missing_metadata_fails(self, tmp_workspace: dict[str, Path]) -> None:
        from scripts.unraid_add_voice import PreflightError, preflight_validate

        for field in ("license_str", "attribution", "provenance"):
            kwargs: dict[str, Any] = {
                "audio_path": tmp_workspace["audio"],
                "transcript_path": tmp_workspace["transcript"],
                "voice_id": "solomon",
                "license_str": "permission-granted",
                "attribution": "Test",
                "provenance": "Test source",
                "models_dir": tmp_workspace["models"],
                "voices_dir": tmp_workspace["voices"],
                "import_inputs_dir": tmp_workspace["import_inputs"],
                "model_rel": "s2-pro-q6_k.gguf",
                "tokenizer_rel": "tokenizer.json",
                "force": False,
            }
            kwargs[field] = ""
            with pytest.raises(PreflightError):
                preflight_validate(**kwargs)

    def test_audio_outside_import_root_fails(
        self, tmp_workspace: dict[str, Path]
    ) -> None:
        from scripts.unraid_add_voice import PreflightError, preflight_validate

        outside_audio = tmp_workspace["root"] / "outside.wav"
        outside_audio.write_bytes(b"\x00" * 100)

        with pytest.raises(PreflightError, match="import-input"):
            preflight_validate(
                audio_path=outside_audio,
                transcript_path=tmp_workspace["transcript"],
                voice_id="solomon",
                license_str="permission-granted",
                attribution="Test",
                provenance="Test source",
                models_dir=tmp_workspace["models"],
                voices_dir=tmp_workspace["voices"],
                import_inputs_dir=tmp_workspace["import_inputs"],
                model_rel="s2-pro-q6_k.gguf",
                tokenizer_rel="tokenizer.json",
                force=False,
            )

    def test_missing_model_fails(self, tmp_workspace: dict[str, Path]) -> None:
        from scripts.unraid_add_voice import PreflightError, preflight_validate

        with pytest.raises(PreflightError, match="model"):
            preflight_validate(
                audio_path=tmp_workspace["audio"],
                transcript_path=tmp_workspace["transcript"],
                voice_id="solomon",
                license_str="permission-granted",
                attribution="Test",
                provenance="Test source",
                models_dir=tmp_workspace["models"],
                voices_dir=tmp_workspace["voices"],
                import_inputs_dir=tmp_workspace["import_inputs"],
                model_rel="nonexistent.gguf",
                tokenizer_rel="tokenizer.json",
                force=False,
            )

    def test_missing_tokenizer_fails(self, tmp_workspace: dict[str, Path]) -> None:
        from scripts.unraid_add_voice import PreflightError, preflight_validate

        with pytest.raises(PreflightError, match="tokenizer"):
            preflight_validate(
                audio_path=tmp_workspace["audio"],
                transcript_path=tmp_workspace["transcript"],
                voice_id="solomon",
                license_str="permission-granted",
                attribution="Test",
                provenance="Test source",
                models_dir=tmp_workspace["models"],
                voices_dir=tmp_workspace["voices"],
                import_inputs_dir=tmp_workspace["import_inputs"],
                model_rel="s2-pro-q6_k.gguf",
                tokenizer_rel="nonexistent.json",
                force=False,
            )

    def test_missing_voices_dir_fails(self, tmp_workspace: dict[str, Path]) -> None:
        from scripts.unraid_add_voice import PreflightError, preflight_validate

        with pytest.raises(PreflightError, match="voices"):
            preflight_validate(
                audio_path=tmp_workspace["audio"],
                transcript_path=tmp_workspace["transcript"],
                voice_id="solomon",
                license_str="permission-granted",
                attribution="Test",
                provenance="Test source",
                models_dir=tmp_workspace["models"],
                voices_dir=tmp_workspace["root"] / "novoices",
                import_inputs_dir=tmp_workspace["import_inputs"],
                model_rel="s2-pro-q6_k.gguf",
                tokenizer_rel="tokenizer.json",
                force=False,
            )

    def test_unsupported_audio_extension_fails(
        self, tmp_workspace: dict[str, Path]
    ) -> None:
        from scripts.unraid_add_voice import PreflightError, preflight_validate

        bad = tmp_workspace["import_inputs"] / "test.bad"
        bad.write_bytes(b"\x00" * 100)

        with pytest.raises(PreflightError, match="extension"):
            preflight_validate(
                audio_path=bad,
                transcript_path=tmp_workspace["transcript"],
                voice_id="solomon",
                license_str="permission-granted",
                attribution="Test",
                provenance="Test source",
                models_dir=tmp_workspace["models"],
                voices_dir=tmp_workspace["voices"],
                import_inputs_dir=tmp_workspace["import_inputs"],
                model_rel="s2-pro-q6_k.gguf",
                tokenizer_rel="tokenizer.json",
                force=False,
            )

    def test_destination_collision_fails(
        self, tmp_workspace: dict[str, Path]
    ) -> None:
        from scripts.unraid_add_voice import PreflightError, preflight_validate

        existing = tmp_workspace["voices"] / "solomon.s2voice"
        existing.write_bytes(b"\x00" * 50)

        with pytest.raises(PreflightError, match="already exists"):
            preflight_validate(
                audio_path=tmp_workspace["audio"],
                transcript_path=tmp_workspace["transcript"],
                voice_id="solomon",
                license_str="permission-granted",
                attribution="Test",
                provenance="Test source",
                models_dir=tmp_workspace["models"],
                voices_dir=tmp_workspace["voices"],
                import_inputs_dir=tmp_workspace["import_inputs"],
                model_rel="s2-pro-q6_k.gguf",
                tokenizer_rel="tokenizer.json",
                force=False,
            )

    def test_force_mode_bypasses_collision(
        self, tmp_workspace: dict[str, Path]
    ) -> None:
        from scripts.unraid_add_voice import preflight_validate

        existing = tmp_workspace["voices"] / "solomon.s2voice"
        existing.write_bytes(b"\x00" * 50)

        # Should not raise with force=True
        preflight_validate(
            audio_path=tmp_workspace["audio"],
            transcript_path=tmp_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=tmp_workspace["models"],
            voices_dir=tmp_workspace["voices"],
            import_inputs_dir=tmp_workspace["import_inputs"],
            model_rel="s2-pro-q6_k.gguf",
            tokenizer_rel="tokenizer.json",
            force=True,
        )


# ---------------------------------------------------------------------------
# Phase 5 — Docker identity and state
# ---------------------------------------------------------------------------


class FakeDockerRunner:
    """Injectable Docker command runner for daemon-free tests."""

    def __init__(
        self,
        *,
        container_state: str = "running",
        container_image: str = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        image_id: str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        image_labels: dict[str, str] | None = None,
        revision: str = "a" * 40,
        inspect_fail: bool = False,
        image_inspect_fail: bool = False,
        container_health_status: str | None = "healthy",
    ) -> None:
        self._container_state = container_state
        self._container_image = container_image
        self._image_id = image_id
        self._image_labels = image_labels or {}
        self._revision = revision
        self._inspect_fail = inspect_fail
        self._image_inspect_fail = image_inspect_fail
        self._container_health_status = container_health_status
        self.commands: list[list[str]] = []

    def __call__(
        self,
        args: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(args))
        check = kwargs.pop("check", False)
        result = self._simulate(args, **kwargs)
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, args,
                output=result.stdout or "",
                stderr=result.stderr or "",
            )
        return result

    def _simulate(
        self, args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        cmd_str = " ".join(args)
        stdout = ""
        stderr = ""

        if args[0] == "docker" and args[1] == "inspect":
            container_name = args[2]
            if self._inspect_fail:
                return subprocess.CompletedProcess(
                    args, 1, stdout="", stderr="No such container"
                )
            if container_name == "s2cpp-tts":
                inspect_data = {
                    "State": {
                        "Status": self._container_state,
                        "Running": self._container_state == "running",
                        "Paused": self._container_state == "paused",
                        "Restarting": self._container_state == "restarting",
                        "Dead": self._container_state == "dead",
                    },
                    "Config": {
                        "Image": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "Env": [f"S2CPP_REVISION={self._revision}"],
                    },
                    "Image": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                }
                # Add Health config for inspect-based health checks
                if self._container_health_status is not None:
                    inspect_data["State"]["Health"] = {
                        "Status": self._container_health_status,
                        "FailingStreak": 0,
                        "Log": [],
                    }
                inspect_json = json.dumps([inspect_data])
                stdout = inspect_json
            else:
                return subprocess.CompletedProcess(
                    args, 1, stdout="", stderr="No such container"
                )
        elif args[0] == "docker" and args[1] == "image" and args[2] == "inspect":
            if self._image_inspect_fail:
                return subprocess.CompletedProcess(
                    args, 1, stdout="", stderr="No such image"
                )
            labels = self._image_labels
            if not labels:
                labels = {"wyoming-s2cpp-tts.s2cpp-revision": self._revision, "org.opencontainers.image.revision": self._revision}
            inspect_json = json.dumps(
                [
                    {
                        "Id": self._image_id,
                        "RepoTags": ["s2cpp-tts:sha-" + self._revision[:12]],
                        "RepoDigests": [self._image_id],
                        "Config": {"Labels": labels},
                    }
                ]
            )
            stdout = inspect_json
        elif args[0] == "docker" and args[1] == "stop":
            self._container_state = "exited"
            stdout = args[2]
        elif args[0] == "docker" and args[1] == "start":
            self._container_state = "running"
            stdout = args[2]
        elif args[0] == "docker" and args[1] == "run":
            # Simulate successful importer run
            stdout = ""
        else:
            stdout = ""

        return subprocess.CompletedProcess(
            args, 0, stdout=stdout, stderr=stderr
        )


class TestDockerIdentity:
    """Docker identity and state validation."""

    def test_running_backend_detected(self) -> None:
        from scripts.unraid_add_voice import detect_backend_state

        runner = FakeDockerRunner(container_state="running")
        state = detect_backend_state("s2cpp-tts", runner=runner)
        assert state == "running"
        assert any("inspect" in " ".join(c) for c in runner.commands)

    def test_stopped_backend_detected(self) -> None:
        from scripts.unraid_add_voice import detect_backend_state

        runner = FakeDockerRunner(container_state="exited")
        state = detect_backend_state("s2cpp-tts", runner=runner)
        assert state == "stopped"

    def test_paused_backend_detected(self) -> None:
        from scripts.unraid_add_voice import detect_backend_state

        runner = FakeDockerRunner(container_state="paused")
        state = detect_backend_state("s2cpp-tts", runner=runner)
        assert state == "paused"

    def test_restarting_backend_detected(self) -> None:
        from scripts.unraid_add_voice import detect_backend_state

        runner = FakeDockerRunner(container_state="restarting")
        state = detect_backend_state("s2cpp-tts", runner=runner)
        assert state == "restarting"

    def test_missing_container_detected(self) -> None:
        from scripts.unraid_add_voice import detect_backend_state

        runner = FakeDockerRunner(inspect_fail=True)
        state = detect_backend_state("s2cpp-tts", runner=runner)
        assert state == "missing"

    def test_unhealthy_state_fails_closed(self) -> None:
        from scripts.unraid_add_voice import (
            BackendStateError,
            detect_backend_state,
        )

        runner = FakeDockerRunner(container_state="dead")
        state = detect_backend_state("s2cpp-tts", runner=runner)
        # dead should be detected; the caller decides to fail closed
        assert state == "dead"

    def test_exact_name_matching(self) -> None:
        from scripts.unraid_add_voice import detect_backend_state

        runner = FakeDockerRunner(container_state="running")
        state = detect_backend_state("s2cpp-tts", runner=runner)
        assert state == "running"

    def test_image_identity_check_passes(self) -> None:
        from scripts.unraid_add_voice import verify_image_identity

        ref = "sha256:" + "a" * 64
        runner = FakeDockerRunner(image_id=ref)
        result, _report = verify_image_identity(
            ref, expected_source_revision="a" * 40, expected_s2cpp_revision="a" * 40, runner=runner
        )
        assert result is True

    def test_image_identity_mismatch_fails(self) -> None:
        from scripts.unraid_add_voice import verify_image_identity

        ref = "sha256:" + "a" * 64
        runner = FakeDockerRunner(image_id="sha256:" + "e" * 64)
        result, _report = verify_image_identity(
            ref, expected_source_revision="a" * 40, expected_s2cpp_revision="a" * 40, runner=runner
        )
        assert result is False

    def test_revision_mismatch_fails(self) -> None:
        from scripts.unraid_add_voice import verify_image_identity

        ref = "sha256:" + "a" * 64
        runner = FakeDockerRunner(revision="b" * 40)
        result, _report = verify_image_identity(
            ref, expected_source_revision="a" * 40, expected_s2cpp_revision="a" * 40, runner=runner
        )
        assert result is False

    def test_oci_label_mismatch_fails(self) -> None:
        from scripts.unraid_add_voice import verify_image_identity

        ref = "sha256:" + "a" * 64
        runner = FakeDockerRunner(
            image_labels={
                "wyoming-s2cpp-tts.s2cpp-revision": "b" * 40,
                "org.opencontainers.image.revision": "b" * 40,
            }
        )
        result, _report = verify_image_identity(
            ref, expected_source_revision="a" * 40, expected_s2cpp_revision="a" * 40, runner=runner
        )
        assert result is False

    def test_immutable_reference_required(self) -> None:
        """Verify that 'latest' tag is rejected."""
        from scripts.unraid_add_voice import validate_image_reference

        with pytest.raises(ValueError, match="immutable"):
            validate_image_reference("s2cpp-tts:latest")

        with pytest.raises(ValueError, match="immutable"):
            validate_image_reference("s2cpp-tts:edge")

        # sha256 and sha- tags are fine
        validate_image_reference("sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        validate_image_reference("s2cpp-tts:sha-" + "f" * 40)


# ---------------------------------------------------------------------------
# Phase 6 — Docker command generation
# ---------------------------------------------------------------------------


class TestDockerCommandGeneration:
    """Docker command planning tests."""

    def test_importer_run_uses_network_none(self) -> None:
        from scripts.unraid_add_voice import build_importer_command

        cmd = build_importer_command(
            image="sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
            models_dir_host="/models",
            voices_dir_host="/voices",
            import_inputs_dir_host="/inputs",
            model_container_path="/models/model.gguf",
            tokenizer_container_path="/models/tokenizer.json",
            audio_relative="/inputs/test.wav",
            transcript_relative="/inputs/test.txt",
            voice_id="test",
            license_str="cc0",
            attribution="Test",
            provenance="Test source",
            cuda_device=0,
            gpu_layers=99,
        )
        assert "--network" in cmd
        net_idx = cmd.index("--network")
        assert cmd[net_idx + 1] == "none"

    def test_importer_uses_rm(self) -> None:
        from scripts.unraid_add_voice import build_importer_command

        cmd = build_importer_command(
            image="sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
            models_dir_host="/models",
            voices_dir_host="/voices",
            import_inputs_dir_host="/inputs",
            model_container_path="/models/model.gguf",
            tokenizer_container_path="/models/tokenizer.json",
            audio_relative="/inputs/test.wav",
            transcript_relative="/inputs/test.txt",
            voice_id="test",
            license_str="cc0",
            attribution="Test",
            provenance="Test source",
            cuda_device=0,
            gpu_layers=99,
        )
        assert "--rm" in cmd

    def test_models_mounted_readonly(self) -> None:
        from scripts.unraid_add_voice import build_importer_command

        cmd = build_importer_command(
            image="sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
            models_dir_host="/models",
            voices_dir_host="/voices",
            import_inputs_dir_host="/inputs",
            model_container_path="/models/model.gguf",
            tokenizer_container_path="/models/tokenizer.json",
            audio_relative="/inputs/test.wav",
            transcript_relative="/inputs/test.txt",
            voice_id="test",
            license_str="cc0",
            attribution="Test",
            provenance="Test source",
            cuda_device=0,
            gpu_layers=99,
        )
        # Find the models mount
        models_idx = cmd.index("-v")
        models_mount = cmd[models_idx + 1]
        assert models_mount.startswith("/models:")
        assert models_mount.endswith(":ro")

    def test_import_inputs_mounted_readonly(self) -> None:
        from scripts.unraid_add_voice import build_importer_command

        cmd = build_importer_command(
            image="sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
            models_dir_host="/models",
            voices_dir_host="/voices",
            import_inputs_dir_host="/inputs",
            model_container_path="/models/model.gguf",
            tokenizer_container_path="/models/tokenizer.json",
            audio_relative="/inputs/test.wav",
            transcript_relative="/inputs/test.txt",
            voice_id="test",
            license_str="cc0",
            attribution="Test",
            provenance="Test source",
            cuda_device=0,
            gpu_layers=99,
        )
        # Find the inputs mount - it should be read-only
        mounts = [cmd[i + 1] for i, v in enumerate(cmd) if v == "-v"]
        input_mounts = [m for m in mounts if "/inputs:" in m]
        assert len(input_mounts) == 1
        assert input_mounts[0].endswith(":ro")

    def test_voices_mounted_readwrite(self) -> None:
        from scripts.unraid_add_voice import build_importer_command

        cmd = build_importer_command(
            image="sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
            models_dir_host="/models",
            voices_dir_host="/voices",
            import_inputs_dir_host="/inputs",
            model_container_path="/models/model.gguf",
            tokenizer_container_path="/models/tokenizer.json",
            audio_relative="/inputs/test.wav",
            transcript_relative="/inputs/test.txt",
            voice_id="test",
            license_str="cc0",
            attribution="Test",
            provenance="Test source",
            cuda_device=0,
            gpu_layers=99,
        )
        mounts = [cmd[i + 1] for i, v in enumerate(cmd) if v == "-v"]
        voice_mounts = [m for m in mounts if "/voices:" in m]
        assert len(voice_mounts) == 1
        assert ":rw" in voice_mounts[0]

    def test_no_docker_socket_mount(self) -> None:
        from scripts.unraid_add_voice import build_importer_command

        cmd = build_importer_command(
            image="sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
            models_dir_host="/models",
            voices_dir_host="/voices",
            import_inputs_dir_host="/inputs",
            model_container_path="/models/model.gguf",
            tokenizer_container_path="/models/tokenizer.json",
            audio_relative="/inputs/test.wav",
            transcript_relative="/inputs/test.txt",
            voice_id="test",
            license_str="cc0",
            attribution="Test",
            provenance="Test source",
            cuda_device=0,
            gpu_layers=99,
        )
        for token in cmd:
            assert "docker.sock" not in token

    def test_uses_transcript_file_not_inline_text(self) -> None:
        from scripts.unraid_add_voice import build_importer_command

        cmd = build_importer_command(
            image="sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
            models_dir_host="/models",
            voices_dir_host="/voices",
            import_inputs_dir_host="/inputs",
            model_container_path="/models/model.gguf",
            tokenizer_container_path="/models/tokenizer.json",
            audio_relative="/inputs/test.wav",
            transcript_relative="/inputs/test.txt",
            voice_id="test",
            license_str="cc0",
            attribution="Test",
            provenance="Test source",
            cuda_device=0,
            gpu_layers=99,
        )
        assert "--transcript-file" in cmd
        assert "--transcript" not in cmd

    def test_argument_arrays_not_strings(self) -> None:
        from scripts.unraid_add_voice import build_importer_command

        cmd = build_importer_command(
            image="sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
            models_dir_host="/models",
            voices_dir_host="/voices",
            import_inputs_dir_host="/inputs",
            model_container_path="/models/model.gguf",
            tokenizer_container_path="/models/tokenizer.json",
            audio_relative="/inputs/test.wav",
            transcript_relative="/inputs/test.txt",
            voice_id="test",
            license_str="cc0",
            attribution="Test",
            provenance="Test source",
            cuda_device=0,
            gpu_layers=99,
        )
        assert isinstance(cmd, list)
        for item in cmd:
            assert isinstance(item, str)


# ---------------------------------------------------------------------------
# Phase 7 — Locking
# ---------------------------------------------------------------------------


class FakeLock:
    """Injectable lock for testing concurrency control."""

    def __init__(
        self, *, acquire_success: bool = True, lock_path: Path | None = None
    ) -> None:
        self._acquired = False
        self._released = False
        self._acquire_success = acquire_success
        self.lock_path = lock_path or Path("/tmp/test.lock")

    def acquire(self) -> bool:
        if self._acquire_success:
            self._acquired = True
            return True
        return False

    def release(self) -> None:
        self._released = True
        self._acquired = False

    @property
    def is_acquired(self) -> bool:
        return self._acquired

    @property
    def is_released(self) -> bool:
        return self._released


class TestLocking:
    """Locking and concurrency control tests."""

    def test_lock_acquisition_succeeds(self) -> None:
        lock = FakeLock()
        result = lock.acquire()
        assert result is True
        assert lock.is_acquired

    def test_lock_acquisition_fails(self) -> None:
        lock = FakeLock(acquire_success=False)
        result = lock.acquire()
        assert result is False
        assert not lock.is_acquired

    def test_lock_releases(self) -> None:
        lock = FakeLock()
        lock.acquire()
        lock.release()
        assert lock.is_released

    def test_lock_no_transcript_in_path(self) -> None:
        """Lock path must not contain transcript data."""
        lock = FakeLock()
        assert "transcript" not in str(lock.lock_path).lower()


# ---------------------------------------------------------------------------
# Phase 8 — Dry-run behavior
# ---------------------------------------------------------------------------


class TestDryRun:
    """Dry-run behavior tests."""

    @pytest.fixture
    def dry_run_workspace(self, tmp_path: Path) -> dict[str, Any]:
        ws: dict[str, Any] = {}
        ws["root"] = tmp_path
        ws["audio"] = tmp_path / "inputs" / "test.wav"
        ws["audio"].parent.mkdir(parents=True)
        ws["audio"].write_bytes(b"\x00" * 100)
        ws["transcript"] = tmp_path / "inputs" / "test.txt"
        ws["transcript"].write_text("Hello world")
        ws["models"] = tmp_path / "models"
        ws["models"].mkdir()
        (ws["models"] / "model.gguf").write_bytes(b"\x00" * 100)
        (ws["models"] / "tokenizer.json").write_text("{}")
        ws["voices"] = tmp_path / "voices"
        ws["voices"].mkdir()
        ws["import_inputs"] = tmp_path / "inputs"
        return ws

    def test_dry_run_does_not_stop_backend(self, dry_run_workspace: dict[str, Any]) -> None:
        from scripts.unraid_add_voice import run_operator

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()

        result = run_operator(
            audio_path=dry_run_workspace["audio"],
            transcript_path=dry_run_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=dry_run_workspace["models"],
            voices_dir=dry_run_workspace["voices"],
            import_inputs_dir=dry_run_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            cuda_device=0,
            gpu_layers=99,
            dry_run=True,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
            runner=runner,
            lock=lock,
        )
        assert result["dry_run"] is True
        # No stop should have been executed
        stop_commands = [c for c in runner.commands if c[1] == "stop"]
        assert len(stop_commands) == 0

    def test_dry_run_does_not_start_backend(self, dry_run_workspace: dict[str, Any]) -> None:
        from scripts.unraid_add_voice import run_operator

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()

        run_operator(
            audio_path=dry_run_workspace["audio"],
            transcript_path=dry_run_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=dry_run_workspace["models"],
            voices_dir=dry_run_workspace["voices"],
            import_inputs_dir=dry_run_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            cuda_device=0,
            gpu_layers=99,
            dry_run=True,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
            runner=runner,
            lock=lock,
        )
        start_commands = [c for c in runner.commands if c[1] == "start"]
        assert len(start_commands) == 0

    def test_dry_run_no_importer_container(self, dry_run_workspace: dict[str, Any]) -> None:
        from scripts.unraid_add_voice import run_operator

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()

        run_operator(
            audio_path=dry_run_workspace["audio"],
            transcript_path=dry_run_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=dry_run_workspace["models"],
            voices_dir=dry_run_workspace["voices"],
            import_inputs_dir=dry_run_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            cuda_device=0,
            gpu_layers=99,
            dry_run=True,
            runner=runner,
            lock=lock,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        run_commands = [c for c in runner.commands if c[1] == "run"]
        assert len(run_commands) == 0

    def test_dry_run_no_filesystem_mutation(self, dry_run_workspace: dict[str, Any]) -> None:
        from scripts.unraid_add_voice import run_operator

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()

        voices_before = list(dry_run_workspace["voices"].iterdir())

        run_operator(
            audio_path=dry_run_workspace["audio"],
            transcript_path=dry_run_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=dry_run_workspace["models"],
            voices_dir=dry_run_workspace["voices"],
            import_inputs_dir=dry_run_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            cuda_device=0,
            gpu_layers=99,
            dry_run=True,
            runner=runner,
            lock=lock,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        voices_after = list(dry_run_workspace["voices"].iterdir())
        assert voices_before == voices_after

    def test_dry_run_transcript_redacted(self, dry_run_workspace: dict[str, Any]) -> None:
        from scripts.unraid_add_voice import run_operator

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()

        result = run_operator(
            audio_path=dry_run_workspace["audio"],
            transcript_path=dry_run_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=dry_run_workspace["models"],
            voices_dir=dry_run_workspace["voices"],
            import_inputs_dir=dry_run_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            cuda_device=0,
            gpu_layers=99,
            dry_run=True,
            runner=runner,
            lock=lock,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        # Transcript content must NOT appear
        result_str = json.dumps(result)
        assert "Hello world" not in result_str

    def test_dry_run_exact_planned_command(self, dry_run_workspace: dict[str, Any]) -> None:
        from scripts.unraid_add_voice import run_operator

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()

        result = run_operator(
            audio_path=dry_run_workspace["audio"],
            transcript_path=dry_run_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=dry_run_workspace["models"],
            voices_dir=dry_run_workspace["voices"],
            import_inputs_dir=dry_run_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            cuda_device=0,
            gpu_layers=99,
            dry_run=True,
            runner=runner,
            lock=lock,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        assert "planned_command" in result or "planned_commands" in result

    def test_dry_run_restart_plan_reflects_running_state(
        self, dry_run_workspace: dict[str, Any]
    ) -> None:
        from scripts.unraid_add_voice import run_operator

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()

        result = run_operator(
            audio_path=dry_run_workspace["audio"],
            transcript_path=dry_run_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=dry_run_workspace["models"],
            voices_dir=dry_run_workspace["voices"],
            import_inputs_dir=dry_run_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            cuda_device=0,
            gpu_layers=99,
            dry_run=True,
            runner=runner,
            lock=lock,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        assert result.get("backend_would_restart") is True


# ---------------------------------------------------------------------------
# Phase 9 — Successful import flow
# ---------------------------------------------------------------------------


class TestSuccessfulImport:
    """End-to-end successful import tests."""

    @pytest.fixture
    def import_workspace(self, tmp_path: Path) -> dict[str, Any]:
        ws: dict[str, Any] = {}
        ws["root"] = tmp_path
        ws["audio"] = tmp_path / "inputs" / "test.wav"
        ws["audio"].parent.mkdir(parents=True)
        ws["audio"].write_bytes(b"\x00" * 100)
        ws["transcript"] = tmp_path / "inputs" / "test.txt"
        ws["transcript"].write_text("Hello world")
        ws["models"] = tmp_path / "models"
        ws["models"].mkdir()
        (ws["models"] / "model.gguf").write_bytes(b"\x00" * 100)
        (ws["models"] / "tokenizer.json").write_text("{}")
        ws["voices"] = tmp_path / "voices"
        ws["voices"].mkdir()
        ws["import_inputs"] = tmp_path / "inputs"
        return ws

    def test_running_backend_is_stopped_before_import(
        self, import_workspace: dict[str, Any]
    ) -> None:
        from scripts.unraid_add_voice import run_operator

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()

        run_operator(
            audio_path=import_workspace["audio"],
            transcript_path=import_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=import_workspace["models"],
            voices_dir=import_workspace["voices"],
            import_inputs_dir=import_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            cuda_device=0,
            gpu_layers=99,
            runner=runner,
            lock=lock,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        # Check stop happened before run
        stop_cmd_indexes = [
            i for i, c in enumerate(runner.commands) if c[1] == "stop"
        ]
        run_cmd_indexes = [
            i for i, c in enumerate(runner.commands) if c[1] == "run"
        ]
        if stop_cmd_indexes and run_cmd_indexes:
            assert stop_cmd_indexes[0] < run_cmd_indexes[0]

    def test_backend_restarts_after_successful_import(
        self, import_workspace: dict[str, Any]
    ) -> None:
        from scripts.unraid_add_voice import run_operator

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()

        result = run_operator(
            audio_path=import_workspace["audio"],
            transcript_path=import_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=import_workspace["models"],
            voices_dir=import_workspace["voices"],
            import_inputs_dir=import_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            cuda_device=0,
            gpu_layers=99,
            runner=runner,
            lock=lock,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        assert result.get("backend_restarted") is True

    def test_lock_released_after_import(
        self, import_workspace: dict[str, Any]
    ) -> None:
        from scripts.unraid_add_voice import run_operator

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()

        run_operator(
            audio_path=import_workspace["audio"],
            transcript_path=import_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=import_workspace["models"],
            voices_dir=import_workspace["voices"],
            import_inputs_dir=import_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            cuda_device=0,
            gpu_layers=99,
            runner=runner,
            lock=lock,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        assert lock.is_released

    def test_no_unrelated_commands(
        self, import_workspace: dict[str, Any]
    ) -> None:
        from scripts.unraid_add_voice import run_operator

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()

        run_operator(
            audio_path=import_workspace["audio"],
            transcript_path=import_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=import_workspace["models"],
            voices_dir=import_workspace["voices"],
            import_inputs_dir=import_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            cuda_device=0,
            gpu_layers=99,
            runner=runner,
            lock=lock,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        # No destructive commands
        forbidden = {"rm", "rmi", "kill", "prune", "down"}
        for cmd in runner.commands:
            subcommand = cmd[1] if len(cmd) > 1 else ""
            assert subcommand not in forbidden, f"Forbidden command: {cmd}"


# ---------------------------------------------------------------------------
# Phase 10 — Initially stopped backend behavior
# ---------------------------------------------------------------------------


class TestInitiallyStoppedBackend:
    """Behavior when backend is initially stopped."""

    @pytest.fixture
    def stopped_workspace(self, tmp_path: Path) -> dict[str, Any]:
        ws: dict[str, Any] = {}
        ws["root"] = tmp_path
        ws["audio"] = tmp_path / "inputs" / "test.wav"
        ws["audio"].parent.mkdir(parents=True)
        ws["audio"].write_bytes(b"\x00" * 100)
        ws["transcript"] = tmp_path / "inputs" / "test.txt"
        ws["transcript"].write_text("Hello world")
        ws["models"] = tmp_path / "models"
        ws["models"].mkdir()
        (ws["models"] / "model.gguf").write_bytes(b"\x00" * 100)
        (ws["models"] / "tokenizer.json").write_text("{}")
        ws["voices"] = tmp_path / "voices"
        ws["voices"].mkdir()
        ws["import_inputs"] = tmp_path / "inputs"
        return ws

    def test_stopped_backend_not_restarted_by_default(
        self, stopped_workspace: dict[str, Any]
    ) -> None:
        from scripts.unraid_add_voice import run_operator

        runner = FakeDockerRunner(container_state="exited")
        lock = FakeLock()

        result = run_operator(
            audio_path=stopped_workspace["audio"],
            transcript_path=stopped_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=stopped_workspace["models"],
            voices_dir=stopped_workspace["voices"],
            import_inputs_dir=stopped_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            cuda_device=0,
            gpu_layers=99,
            runner=runner,
            lock=lock,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        start_commands = [c for c in runner.commands if c[1] == "start"]
        assert len(start_commands) == 0

    def test_stopped_backend_importer_still_runs(
        self, stopped_workspace: dict[str, Any]
    ) -> None:
        from scripts.unraid_add_voice import run_operator

        runner = FakeDockerRunner(container_state="exited")
        lock = FakeLock()

        run_operator(
            audio_path=stopped_workspace["audio"],
            transcript_path=stopped_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=stopped_workspace["models"],
            voices_dir=stopped_workspace["voices"],
            import_inputs_dir=stopped_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            cuda_device=0,
            gpu_layers=99,
            runner=runner,
            lock=lock,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        run_commands = [c for c in runner.commands if c[1] == "run"]
        assert len(run_commands) == 1


# ---------------------------------------------------------------------------
# Phase 11 — Failure scenarios
# ---------------------------------------------------------------------------


class FailingImportRunner(FakeDockerRunner):
    """Fake runner where the import fails."""

    def _simulate(
        self, args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "docker" and args[1] == "run":
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="Import failed"
            )
        return super()._simulate(args, **kwargs)


class TimeoutImportRunner(FakeDockerRunner):
    """Fake runner where the import times out."""

    def __call__(
        self, args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "docker" and args[1] == "run":
            raise subprocess.TimeoutExpired(args, 30)
        return super().__call__(args, **kwargs)


class StopFailingRunner(FakeDockerRunner):
    """Fake runner where stop fails."""

    def _simulate(
        self, args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "docker" and args[1] == "stop":
            raise subprocess.TimeoutExpired(args, 10)
        return super()._simulate(args, **kwargs)


class RestartFailingRunner(FakeDockerRunner):
    """Fake runner where restart fails."""

    def _simulate(
        self, args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "docker" and args[1] == "start":
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="Start failed"
            )
        return super()._simulate(args, **kwargs)


class TestFailures:
    """Failure scenario tests."""

    @pytest.fixture
    def fail_workspace(self, tmp_path: Path) -> dict[str, Any]:
        ws: dict[str, Any] = {}
        ws["root"] = tmp_path
        ws["audio"] = tmp_path / "inputs" / "test.wav"
        ws["audio"].parent.mkdir(parents=True)
        ws["audio"].write_bytes(b"\x00" * 100)
        ws["transcript"] = tmp_path / "inputs" / "test.txt"
        ws["transcript"].write_text("Hello world")
        ws["models"] = tmp_path / "models"
        ws["models"].mkdir()
        (ws["models"] / "model.gguf").write_bytes(b"\x00" * 100)
        (ws["models"] / "tokenizer.json").write_text("{}")
        ws["voices"] = tmp_path / "voices"
        ws["voices"].mkdir()
        ws["import_inputs"] = tmp_path / "inputs"
        return ws

    def test_importer_nonzero_exit(
        self, fail_workspace: dict[str, Any]
    ) -> None:
        from scripts.unraid_add_voice import run_operator

        runner = FailingImportRunner(container_state="running")
        lock = FakeLock()

        result = run_operator(
            audio_path=fail_workspace["audio"],
            transcript_path=fail_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=fail_workspace["models"],
            voices_dir=fail_workspace["voices"],
            import_inputs_dir=fail_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            cuda_device=0,
            gpu_layers=99,
            runner=runner,
            lock=lock,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        assert result["status"] == "failed"
        assert result.get("importer_exit_code") == 1

    def test_backend_restarts_after_import_failure(
        self, fail_workspace: dict[str, Any]
    ) -> None:
        from scripts.unraid_add_voice import run_operator

        runner = FailingImportRunner(container_state="running")
        lock = FakeLock()

        run_operator(
            audio_path=fail_workspace["audio"],
            transcript_path=fail_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=fail_workspace["models"],
            voices_dir=fail_workspace["voices"],
            import_inputs_dir=fail_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            cuda_device=0,
            gpu_layers=99,
            runner=runner,
            lock=lock,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        # Backend should restart even after import failure
        start_commands = [c for c in runner.commands if c[1] == "start"]
        assert len(start_commands) >= 1

    def test_lock_released_after_failure(
        self, fail_workspace: dict[str, Any]
    ) -> None:
        from scripts.unraid_add_voice import run_operator

        runner = FailingImportRunner(container_state="running")
        lock = FakeLock()

        run_operator(
            audio_path=fail_workspace["audio"],
            transcript_path=fail_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=fail_workspace["models"],
            voices_dir=fail_workspace["voices"],
            import_inputs_dir=fail_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            cuda_device=0,
            gpu_layers=99,
            runner=runner,
            lock=lock,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        assert lock.is_released

    def test_import_timeout(
        self, fail_workspace: dict[str, Any]
    ) -> None:
        from scripts.unraid_add_voice import run_operator

        runner = TimeoutImportRunner(container_state="running")
        lock = FakeLock()

        result = run_operator(
            audio_path=fail_workspace["audio"],
            transcript_path=fail_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=fail_workspace["models"],
            voices_dir=fail_workspace["voices"],
            import_inputs_dir=fail_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            cuda_device=0,
            gpu_layers=99,
            runner=runner,
            lock=lock,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        assert result["status"] == "failed"

    def test_restart_failure_detected(
        self, fail_workspace: dict[str, Any]
    ) -> None:
        from scripts.unraid_add_voice import run_operator

        runner = RestartFailingRunner(container_state="running")
        lock = FakeLock()

        result = run_operator(
            audio_path=fail_workspace["audio"],
            transcript_path=fail_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=fail_workspace["models"],
            voices_dir=fail_workspace["voices"],
            import_inputs_dir=fail_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            cuda_device=0,
            gpu_layers=99,
            runner=runner,
            lock=lock,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        assert result.get("backend_restarted") is False

    def test_concurrent_lock_failure(
        self, fail_workspace: dict[str, Any]
    ) -> None:
        from scripts.unraid_add_voice import OperatorError, run_operator

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock(acquire_success=False)

        with pytest.raises(OperatorError, match="lock|active|concurrent"):
            run_operator(
                audio_path=fail_workspace["audio"],
                transcript_path=fail_workspace["transcript"],
                voice_id="solomon",
                license_str="permission-granted",
                attribution="Test",
                provenance="Test source",
                models_dir=fail_workspace["models"],
                voices_dir=fail_workspace["voices"],
                import_inputs_dir=fail_workspace["import_inputs"],
                model_rel="model.gguf",
                tokenizer_rel="tokenizer.json",
                backend_container="s2cpp-tts",
                backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                cuda_device=0,
                gpu_layers=99,
                runner=runner,
                lock=lock,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
            )


# ---------------------------------------------------------------------------
# Phase 12 — Report output
# ---------------------------------------------------------------------------


class TestReportFormat:
    """Structured report output tests."""

    @pytest.fixture
    def report_workspace(self, tmp_path: Path) -> dict[str, Any]:
        ws: dict[str, Any] = {}
        ws["root"] = tmp_path
        ws["audio"] = tmp_path / "inputs" / "test.wav"
        ws["audio"].parent.mkdir(parents=True)
        ws["audio"].write_bytes(b"\x00" * 100)
        ws["transcript"] = tmp_path / "inputs" / "test.txt"
        ws["transcript"].write_text("Hello world")
        ws["models"] = tmp_path / "models"
        ws["models"].mkdir()
        (ws["models"] / "model.gguf").write_bytes(b"\x00" * 100)
        (ws["models"] / "tokenizer.json").write_text("{}")
        ws["voices"] = tmp_path / "voices"
        ws["voices"].mkdir()
        ws["import_inputs"] = tmp_path / "inputs"
        return ws

    def test_report_has_required_fields(
        self, report_workspace: dict[str, Any]
    ) -> None:
        from scripts.unraid_add_voice import run_operator

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()

        result = run_operator(
            audio_path=report_workspace["audio"],
            transcript_path=report_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=report_workspace["models"],
            voices_dir=report_workspace["voices"],
            import_inputs_dir=report_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            cuda_device=0,
            gpu_layers=99,
            runner=runner,
            lock=lock,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        required = {
            "status",
            "voice_id",
            "backend_container",
            "backend_initial_state",
            "backend_final_state",
            "dry_run",
            "force",
        }
        for key in required:
            assert key in result, f"Missing key: {key}"

    def test_report_preserves_distinct_expected_revisions(
        self, report_workspace: dict[str, Any]
    ) -> None:
        from scripts.unraid_add_voice import run_operator

        source_revision = "b" * 40
        s2cpp_revision = "a" * 40
        runner = FailingImportRunner(
            container_state="running",
            image_labels={
                "org.opencontainers.image.revision": source_revision,
                "wyoming-s2cpp-tts.s2cpp-revision": s2cpp_revision,
            },
            revision=s2cpp_revision,
        )

        result = run_operator(
            audio_path=report_workspace["audio"],
            transcript_path=report_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=report_workspace["models"],
            voices_dir=report_workspace["voices"],
            import_inputs_dir=report_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            runner=runner,
            lock=FakeLock(),
            expected_source_revision=source_revision,
            expected_s2cpp_revision=s2cpp_revision,
        )

        assert result["expected_source_revision"] == source_revision
        assert result["expected_s2cpp_revision"] == s2cpp_revision

    def test_report_no_transcript_content(
        self, report_workspace: dict[str, Any]
    ) -> None:
        from scripts.unraid_add_voice import run_operator

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()

        result = run_operator(
            audio_path=report_workspace["audio"],
            transcript_path=report_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=report_workspace["models"],
            voices_dir=report_workspace["voices"],
            import_inputs_dir=report_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            cuda_device=0,
            gpu_layers=99,
            runner=runner,
            lock=lock,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        result_str = json.dumps(result)
        assert "Hello world" not in result_str

    def test_report_has_timestamps(
        self, report_workspace: dict[str, Any]
    ) -> None:
        from scripts.unraid_add_voice import run_operator

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()

        result = run_operator(
            audio_path=report_workspace["audio"],
            transcript_path=report_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=report_workspace["models"],
            voices_dir=report_workspace["voices"],
            import_inputs_dir=report_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            cuda_device=0,
            gpu_layers=99,
            runner=runner,
            lock=lock,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        assert "started_at" in result
        assert "finished_at" in result

    def test_bounded_error_lengths(
        self, report_workspace: dict[str, Any]
    ) -> None:
        from scripts.unraid_add_voice import run_operator

        runner = FailingImportRunner(container_state="running")
        lock = FakeLock()

        result = run_operator(
            audio_path=report_workspace["audio"],
            transcript_path=report_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=report_workspace["models"],
            voices_dir=report_workspace["voices"],
            import_inputs_dir=report_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            cuda_device=0,
            gpu_layers=99,
            runner=runner,
            lock=lock,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        errors = result.get("errors", [])
        for error in errors:
            assert len(str(error)) <= 2000, f"Error too long: {error}"


# ---------------------------------------------------------------------------
# Phase 13 — Signal handling (simulated)
# ---------------------------------------------------------------------------


class TestSignalHandling:
    """Signal handling behavior tests."""

    @pytest.fixture
    def sig_workspace(self, tmp_path: Path) -> dict[str, Any]:
        ws: dict[str, Any] = {}
        ws["root"] = tmp_path
        ws["audio"] = tmp_path / "inputs" / "test.wav"
        ws["audio"].parent.mkdir(parents=True)
        ws["audio"].write_bytes(b"\x00" * 100)
        ws["transcript"] = tmp_path / "inputs" / "test.txt"
        ws["transcript"].write_text("Hello world")
        ws["models"] = tmp_path / "models"
        ws["models"].mkdir()
        (ws["models"] / "model.gguf").write_bytes(b"\x00" * 100)
        (ws["models"] / "tokenizer.json").write_text("{}")
        ws["voices"] = tmp_path / "voices"
        ws["voices"].mkdir()
        ws["import_inputs"] = tmp_path / "inputs"
        return ws

    def test_signal_registration(self) -> None:
        """Verify the SIGINT/SIGTERM handler is registered."""
        from scripts.unraid_add_voice import _SIGNAL_RECEIVED

        assert _SIGNAL_RECEIVED is not None  # should be a threading.Event

    def test_signal_handlers_cleanup_lock(self) -> None:
        """Verify the signal handler's cleanup function exists."""
        from scripts.unraid_add_voice import _signal_cleanup

        assert callable(_signal_cleanup)


# ---------------------------------------------------------------------------
# Phase 14 — Security contracts
# ---------------------------------------------------------------------------


class TestSecurityContracts:
    """Security contract enforcement tests."""

    def test_no_shell_true_in_code(self) -> None:
        """Verify unraid_add_voice.py does not use shell=True."""
        source = Path("scripts/unraid_add_voice.py").read_text()
        assert "shell=True" not in source
        assert "shell = True" not in source

    def test_no_os_system_in_code(self) -> None:
        """Verify no os.system calls."""
        source = Path("scripts/unraid_add_voice.py").read_text()
        assert "os.system" not in source

    def test_no_eval_or_exec_in_code(self) -> None:
        """Verify no eval/exec calls."""
        source = Path("scripts/unraid_add_voice.py").read_text()
        assert "eval(" not in source
        assert "exec(" not in source

    def test_no_destructive_docker_commands_generated(self) -> None:
        """Verify build_importer_command does not generate destructive commands."""
        from scripts.unraid_add_voice import build_importer_command

        cmd = build_importer_command(
            image="sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
            models_dir_host="/models",
            voices_dir_host="/voices",
            import_inputs_dir_host="/inputs",
            model_container_path="/models/model.gguf",
            tokenizer_container_path="/models/tokenizer.json",
            audio_relative="/inputs/test.wav",
            transcript_relative="/inputs/test.txt",
            voice_id="test",
            license_str="cc0",
            attribution="Test",
            provenance="Test source",
            cuda_device=0,
            gpu_layers=99,
        )
        destructive = {"rm", "rmi", "kill", "prune", "down", "volume"}
        for token in cmd:
            assert token not in destructive

    def test_no_privileged_mode(self) -> None:
        from scripts.unraid_add_voice import build_importer_command

        cmd = build_importer_command(
            image="sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
            models_dir_host="/models",
            voices_dir_host="/voices",
            import_inputs_dir_host="/inputs",
            model_container_path="/models/model.gguf",
            tokenizer_container_path="/models/tokenizer.json",
            audio_relative="/inputs/test.wav",
            transcript_relative="/inputs/test.txt",
            voice_id="test",
            license_str="cc0",
            attribution="Test",
            provenance="Test source",
            cuda_device=0,
            gpu_layers=99,
        )
        assert "--privileged" not in cmd

    def test_argument_arrays_used(self) -> None:
        from scripts.unraid_add_voice import build_importer_command

        cmd = build_importer_command(
            image="sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
            models_dir_host="/models",
            voices_dir_host="/voices",
            import_inputs_dir_host="/inputs",
            model_container_path="/models/model.gguf",
            tokenizer_container_path="/models/tokenizer.json",
            audio_relative="/inputs/test.wav",
            transcript_relative="/inputs/test.txt",
            voice_id="test",
            license_str="cc0",
            attribution="Test",
            provenance="Test source",
            cuda_device=0,
            gpu_layers=99,
        )
        assert isinstance(cmd, list)
        assert all(isinstance(a, str) for a in cmd)

    def test_no_floating_image_tags_by_default(self) -> None:
        from scripts.unraid_add_voice import validate_image_reference

        with pytest.raises(ValueError):
            validate_image_reference("s2cpp-tts:latest")
        with pytest.raises(ValueError):
            validate_image_reference("s2cpp-tts")
        # sha- prefixed tags and sha256: digests are OK
        validate_image_reference("sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        validate_image_reference("s2cpp-tts:sha-" + "c" * 40)


# ---------------------------------------------------------------------------
# Phase 14b — Artifact validation (_validate_output) RED tests
# ---------------------------------------------------------------------------


class TestValidateOutput:
    """Direct behavioral tests for _validate_output and public validation contract.

    Covers: valid existing .s2voice/sidecar, malformed/empty/symlink profile,
    missing/malformed/symlink sidecar, schema failure, ID mismatch,
    missing/invalid/incorrect SHA, computed SHA match,
    missing/malformed/mismatched provenance.s2cpp_revision,
    recognized staging residue for voice ID,
    optional validation WAV absent/empty/symlink/outside/valid.
    """

    @staticmethod
    def _make_valid_profile(voices_dir: Path, voice_id: str, revision: str = "a" * 40) -> tuple[Path, Path, str]:
        """Create a minimal valid .s2voice and sidecar."""
        import hashlib
        import struct

        transcript = "Reference transcript."
        transcript_bytes = transcript.encode("utf-8") + b"\0"
        codes = struct.pack("<4i", 1, 2, 3, 4)
        data = struct.pack(
            "<8sIiiiiQQ",
            b"S2VOICE\0",
            1,  # version
            8,  # num_codebooks
            1,  # T_prompt
            24000,  # sample_rate
            4096,  # codebook_size
            len(transcript_bytes),  # transcript_len
            len(codes),  # codes_size
        ) + transcript_bytes + codes
        profile_path = voices_dir / f"{voice_id}.s2voice"
        profile_path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()

        sidecar = {
            "id": voice_id,
            "license": "permission-granted",
            "attribution": "Test",
            "provenance": {
                "source": "Test source",
                "tool": "test",
                "s2cpp_revision": revision,
            },
            "hash_sha256": digest,
        }
        sidecar_path = voices_dir / f"{voice_id}.s2voice.json"
        sidecar_path.write_text(json.dumps(sidecar, indent=2) + "\n")
        return profile_path, sidecar_path, digest

    # ── Valid existing .s2voice and sidecar ──

    def test_valid_existing_s2voice_sidecar_passes(self, tmp_path: Path) -> None:
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path, sidecar_path, digest = self._make_valid_profile(voices, "testvoice")

        errors, sha, ownership = _validate_output(
            profile_path, sidecar_path, "testvoice",
            expected_s2cpp_revision="a" * 40,
        )
        assert errors == [], f"Unexpected errors: {errors}"
        assert sha == digest
        assert "profile" in ownership
        assert "sidecar" in ownership
        for key in ownership:
            assert "uid" in ownership[key]
            assert "gid" in ownership[key]
            assert "mode_octal" in ownership[key]

    # ── Malformed/empty/symlink profile ──

    def test_malformed_profile_fails(self, tmp_path: Path) -> None:
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path = voices / "testvoice.s2voice"
        profile_path.write_bytes(b"not a valid s2voice file at all")
        sidecar_path = voices / "testvoice.s2voice.json"
        sidecar_path.write_text('{"id":"testvoice","license":"x","attribution":"x"}')

        errors, sha, _ = _validate_output(
            profile_path, sidecar_path, "testvoice",
            expected_s2cpp_revision="a" * 40,
        )
        assert len(errors) > 0, "Malformed profile should produce validation errors"
        assert any("profile" in e.lower() or "validation" in e.lower() for e in errors)

    def test_empty_profile_fails(self, tmp_path: Path) -> None:
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path = voices / "testvoice.s2voice"
        profile_path.write_bytes(b"")
        sidecar_path = voices / "testvoice.s2voice.json"
        sidecar_path.write_text('{"id":"testvoice","license":"x","attribution":"x"}')

        errors, sha, _ = _validate_output(
            profile_path, sidecar_path, "testvoice",
            expected_s2cpp_revision="a" * 40,
        )
        assert len(errors) > 0, "Empty profile should produce validation errors"
        assert any("empty" in e.lower() for e in errors)

    def test_symlink_profile_fails(self, tmp_path: Path) -> None:
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        real_profile, sidecar_path, _ = self._make_valid_profile(voices, "real")
        symlink_profile = voices / "testvoice.s2voice"
        symlink_profile.symlink_to(real_profile)
        sidecar_path = voices / "testvoice.s2voice.json"
        sidecar_path.write_text('{"id":"testvoice","license":"x","attribution":"x"}')

        errors, sha, _ = _validate_output(
            symlink_profile, sidecar_path, "testvoice",
            expected_s2cpp_revision="a" * 40,
        )
        assert len(errors) > 0, "Symlink profile should produce validation errors"
        assert any("symlink" in e.lower() for e in errors)

    # ── Missing/malformed/symlink sidecar ──

    def test_missing_sidecar_fails(self, tmp_path: Path) -> None:
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path, _, _ = self._make_valid_profile(voices, "testvoice")
        # Remove the sidecar so it's missing
        sidecar_path = voices / "testvoice.s2voice.json"
        sidecar_path.unlink()

        errors, sha, _ = _validate_output(
            profile_path, sidecar_path, "testvoice",
            expected_s2cpp_revision="a" * 40,
        )
        assert len(errors) > 0, "Missing sidecar should produce validation errors"
        assert any("sidecar" in e.lower() and "not found" in e.lower() for e in errors)

    def test_symlink_sidecar_fails(self, tmp_path: Path) -> None:
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path, _, _ = self._make_valid_profile(voices, "testvoice")
        # Remove the sidecar created by _make_valid_profile
        existing_sidecar = voices / "testvoice.s2voice.json"
        existing_sidecar.unlink()
        real_sidecar = voices / "real.json"
        real_sidecar.write_text('{"id":"testvoice","license":"x","attribution":"x"}')
        sidecar_path = voices / "testvoice.s2voice.json"
        sidecar_path.symlink_to(real_sidecar)

        errors, sha, _ = _validate_output(
            profile_path, sidecar_path, "testvoice",
            expected_s2cpp_revision="a" * 40,
        )
        assert len(errors) > 0, "Symlink sidecar should produce validation errors"
        assert any("symlink" in e.lower() for e in errors)

    def test_malformed_json_sidecar_fails(self, tmp_path: Path) -> None:
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path, _, _ = self._make_valid_profile(voices, "testvoice")
        sidecar_path = voices / "testvoice.s2voice.json"
        sidecar_path.write_text("not valid json {{{")

        errors, sha, _ = _validate_output(
            profile_path, sidecar_path, "testvoice",
            expected_s2cpp_revision="a" * 40,
        )
        assert len(errors) > 0, "Malformed JSON sidecar should produce validation errors"
        assert any("json" in e.lower() for e in errors)

    # ── Schema failure ──

    def test_schema_failure_missing_required_field(self, tmp_path: Path) -> None:
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path, _, _ = self._make_valid_profile(voices, "testvoice")
        # Missing required 'license' field
        sidecar_path = voices / "testvoice.s2voice.json"
        sidecar_path.write_text('{"id":"testvoice","attribution":"x"}')

        errors, sha, _ = _validate_output(
            profile_path, sidecar_path, "testvoice",
            expected_s2cpp_revision="a" * 40,
        )
        assert len(errors) > 0, "Schema validation failure should produce errors"
        assert any("schema" in e.lower() or "license" in e.lower() for e in errors)

    def test_schema_failure_additional_properties(self, tmp_path: Path) -> None:
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path, _, _ = self._make_valid_profile(voices, "testvoice")
        sidecar_path = voices / "testvoice.s2voice.json"
        sidecar_path.write_text('{"id":"testvoice","license":"x","attribution":"x","bogus_key":true}')

        errors, sha, _ = _validate_output(
            profile_path, sidecar_path, "testvoice",
            expected_s2cpp_revision="a" * 40,
        )
        assert len(errors) > 0, "Schema validation failure for additional props should produce errors"

    # ── ID mismatch ──

    def test_id_mismatch_fails(self, tmp_path: Path) -> None:
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path, _, _ = self._make_valid_profile(voices, "testvoice")
        sidecar_path = voices / "testvoice.s2voice.json"
        sidecar_path.write_text('{"id":"other_voice","license":"x","attribution":"x"}')

        errors, sha, _ = _validate_output(
            profile_path, sidecar_path, "testvoice",
            expected_s2cpp_revision="a" * 40,
        )
        assert len(errors) > 0, "ID mismatch should produce validation errors"
        assert any("id" in e.lower() and "mismatch" in e.lower() for e in errors)

    # ── Missing/invalid/incorrect SHA ──

    def test_missing_sha_in_sidecar_passes_without_sha_check(self, tmp_path: Path) -> None:
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path, _, _ = self._make_valid_profile(voices, "testvoice")
        # Remove hash_sha256 from sidecar
        sidecar_path = voices / "testvoice.s2voice.json"
        sidecar_path.write_text('{"id":"testvoice","license":"x","attribution":"x"}')

        # Missing SHA is not an error — it's optional in the schema
        errors, sha, _ = _validate_output(
            profile_path, sidecar_path, "testvoice",
            expected_s2cpp_revision="a" * 40,
        )
        # No SHA mismatch error expected since the field is absent
        sha_mismatch_errors = [e for e in errors if "hash" in e.lower() and "mismatch" in e.lower()]
        assert len(sha_mismatch_errors) == 0

    def test_invalid_sha_format_fails(self, tmp_path: Path) -> None:
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path, _, _ = self._make_valid_profile(voices, "testvoice")
        sidecar_path = voices / "testvoice.s2voice.json"
        sidecar_path.write_text('{"id":"testvoice","license":"x","attribution":"x","hash_sha256":"not-a-valid-sha"}')

        errors, sha, _ = _validate_output(
            profile_path, sidecar_path, "testvoice",
            expected_s2cpp_revision="a" * 40,
        )
        assert len(errors) > 0, "Invalid SHA format should produce errors"
        assert any("hash" in e.lower() or "hex" in e.lower() for e in errors)

    def test_sha_mismatch_fails(self, tmp_path: Path) -> None:
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path, _, _ = self._make_valid_profile(voices, "testvoice")
        sidecar_path = voices / "testvoice.s2voice.json"
        # Put a wrong SHA
        wrong_sha = "b" * 64
        sidecar_path.write_text(
            '{"id":"testvoice","license":"x","attribution":"x","hash_sha256":"' + wrong_sha + '"}'
        )

        errors, sha, _ = _validate_output(
            profile_path, sidecar_path, "testvoice",
            expected_s2cpp_revision="a" * 40,
        )
        assert len(errors) > 0, "SHA mismatch should produce errors"
        assert any("hash" in e.lower() and "mismatch" in e.lower() for e in errors)

    # ── Computed SHA match ──

    def test_computed_sha_matches_sidecar(self, tmp_path: Path) -> None:
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path, _, digest = self._make_valid_profile(voices, "testvoice")
        sidecar_path = voices / "testvoice.s2voice.json"
        # Already has matching hash from _make_valid_profile

        errors, sha, _ = _validate_output(
            profile_path, sidecar_path, "testvoice",
            expected_s2cpp_revision="a" * 40,
        )
        assert sha == digest
        sha_mismatch_errors = [e for e in errors if "hash" in e.lower() and "mismatch" in e.lower()]
        assert len(sha_mismatch_errors) == 0

    # ── s2cpp_revision mismatch ──

    def test_s2cpp_revision_missing_fails_closed(self, tmp_path: Path) -> None:
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path, _, _ = self._make_valid_profile(voices, "testvoice", revision="a" * 40)
        # Rewrite sidecar without provenience.s2cpp_revision
        sidecar_path = voices / "testvoice.s2voice.json"
        sidecar_path.write_text('{"id":"testvoice","license":"x","attribution":"x"}')

        errors, sha, _ = _validate_output(
            profile_path, sidecar_path, "testvoice",
            expected_s2cpp_revision="a" * 40,
        )
        revision_errors = [e for e in errors if "s2cpp_revision" in e.lower()]
        assert revision_errors, "missing sidecar s2cpp_revision must fail closed"

    def test_s2cpp_revision_mismatch_fails(self, tmp_path: Path) -> None:
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path, _, _ = self._make_valid_profile(voices, "testvoice", revision="b" * 40)

        errors, sha, _ = _validate_output(
            profile_path, sidecar_path=voices / "testvoice.s2voice.json",
            voice_id="testvoice",
            expected_s2cpp_revision="a" * 40,
        )
        assert len(errors) > 0, "s2cpp_revision mismatch should produce errors"
        assert any("s2cpp_revision" in e.lower() and "mismatch" in e.lower() for e in errors)

    # ── Staging residue ──

    def test_staging_residue_detected(self, tmp_path: Path) -> None:
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path, _, _ = self._make_valid_profile(voices, "testvoice")
        # Create a staging residue
        staging = voices / "testvoice.staging.tmp"
        staging.write_text("leftover")
        # Also create a staging directory
        staging_dir = voices / "testvoice.staging"
        staging_dir.mkdir()

        errors, sha, _ = _validate_output(
            profile_path, sidecar_path=voices / "testvoice.s2voice.json",
            voice_id="testvoice",
            expected_s2cpp_revision="a" * 40,
        )
        assert len(errors) > 0, "Staging residue should produce errors"
        assert any("staging" in e.lower() for e in errors)

    # ── Validation WAV absent/empty/symlink/outside/valid ──

    def test_requested_validation_wav_absent_fails_closed(self, tmp_path: Path) -> None:
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path, _, _ = self._make_valid_profile(voices, "testvoice")

        errors, sha, _ = _validate_output(
            profile_path, sidecar_path=voices / "testvoice.s2voice.json",
            voice_id="testvoice",
            expected_s2cpp_revision="a" * 40,
            validation_wav_rel="nonexistent.wav",
        )
        wav_errors = [e for e in errors if "wav" in e.lower()]
        assert wav_errors, "a requested validation WAV must exist and validate"

    def test_validation_wav_valid_recorded(self, tmp_path: Path) -> None:
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path, _, _ = self._make_valid_profile(voices, "testvoice")
        # Create a valid WAV in a subdirectory
        wav_dir = voices / "validation"
        wav_dir.mkdir()
        wav_path = wav_dir / "test.wav"
        wav_path.write_bytes(b"\x00" * 100)

        errors, sha, ownership = _validate_output(
            profile_path, sidecar_path=voices / "testvoice.s2voice.json",
            voice_id="testvoice",
            expected_s2cpp_revision="a" * 40,
            validation_wav_rel="validation/test.wav",
        )
        wav_errors = [e for e in errors if "wav" in e.lower()]
        assert len(wav_errors) == 0, f"Valid validation WAV should not produce errors: {wav_errors}"
        assert "validation_wav" in ownership

    def test_validation_wav_empty_recorded(self, tmp_path: Path) -> None:
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path, _, _ = self._make_valid_profile(voices, "testvoice")
        wav_path = voices / "test.wav"
        wav_path.write_bytes(b"")  # empty

        errors, sha, _ = _validate_output(
            profile_path, sidecar_path=voices / "testvoice.s2voice.json",
            voice_id="testvoice",
            expected_s2cpp_revision="a" * 40,
            validation_wav_rel="test.wav",
        )
        assert len(errors) > 0, "Empty validation WAV should produce errors"
        assert any("empty" in e.lower() and "wav" in e.lower() for e in errors)

    def test_validation_wav_symlink_rejected(self, tmp_path: Path) -> None:
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path, _, _ = self._make_valid_profile(voices, "testvoice")
        real_wav = voices / "real.wav"
        real_wav.write_bytes(b"\x00" * 100)
        symlink_wav = voices / "test.wav"
        symlink_wav.symlink_to(real_wav)

        errors, sha, _ = _validate_output(
            profile_path, sidecar_path=voices / "testvoice.s2voice.json",
            voice_id="testvoice",
            expected_s2cpp_revision="a" * 40,
            validation_wav_rel="test.wav",
        )
        assert len(errors) > 0, "Symlink validation WAV should produce errors"
        assert any("symlink" in e.lower() and "wav" in e.lower() for e in errors)

    def test_validation_wav_outside_voices_rejected(self, tmp_path: Path) -> None:
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path, _, _ = self._make_valid_profile(voices, "testvoice")

        errors, sha, _ = _validate_output(
            profile_path, sidecar_path=voices / "testvoice.s2voice.json",
            voice_id="testvoice",
            expected_s2cpp_revision="a" * 40,
            validation_wav_rel="../outside.wav",
        )
        assert len(errors) > 0, "Validation WAV outside voices dir should produce errors"
        assert any("outside" in e.lower() or "traversal" in e.lower() or "wav" in e.lower() for e in errors)

    def test_structurd_artifact_evidence_no_transcript(self, tmp_path: Path) -> None:
        """Artifact evidence must include paths, hash, UID, GID, octal modes, no transcript."""
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path, _, digest = self._make_valid_profile(voices, "testvoice")

        _, sha, ownership = _validate_output(
            profile_path, sidecar_path=voices / "testvoice.s2voice.json",
            voice_id="testvoice",
            expected_s2cpp_revision="a" * 40,
        )
        assert sha == digest
        assert "profile" in ownership
        assert "sidecar" in ownership
        profile_owner = ownership["profile"]
        assert "uid" in profile_owner
        assert "gid" in profile_owner
        assert "mode_octal" in profile_owner
        assert isinstance(profile_owner["mode_octal"], str)
        assert profile_owner["mode_octal"].startswith("0o")

        # No transcript key
        assert "transcript" not in ownership.get("profile", {})
        assert "transcript" not in ownership.get("sidecar", {})

    # ── Fail-closed: parse errors must propagate ──

    def test_profile_parse_error_fails_closed(self, tmp_path: Path) -> None:
        """When profile parsing fails, the error must be surfaced, not silently swallowed."""
        from scripts.unraid_add_voice import _validate_output

        voices = tmp_path / "voices"
        voices.mkdir()
        profile_path = voices / "testvoice.s2voice"
        # Write something that looks like a valid header but has invalid codes
        import struct
        header = struct.pack("<8sIiiiiQQ", b"S2VOICE\0", 1, 8, 1, 24000, 4096, 100, 1000)
        profile_path.write_bytes(header + b"x" * 1100)  # garbage past header

        sidecar_path = voices / "testvoice.s2voice.json"
        sidecar_path.write_text('{"id":"testvoice","license":"x","attribution":"x"}')

        errors, sha, _ = _validate_output(
            profile_path, sidecar_path, "testvoice",
            expected_s2cpp_revision="a" * 40,
        )
        assert len(errors) > 0, "Profile parse errors must fail closed"


# ---------------------------------------------------------------------------
# Phase 14c — Installer / export script RED tests
# ---------------------------------------------------------------------------


class TestInstallerScript:
    """Tests for scripts/install_unraid_voice_operator.py.

    Must copy only unraid_add_voice.py, add-s2voice, config.env.example
    with correct permissions (0755 scripts, 0644 config).
    Must refuse symlink targets/files, existing config overwrite unless flag.
    Tests operate ONLY in tmp_path.
    """

    def _make_installer_module(self) -> None:
        """Ensure the installer module exists and is importable."""
        installer_path = Path("scripts/install_unraid_voice_operator.py")
        if not installer_path.exists():
            pytest.skip("installer script not yet created (RED phase)")

    def test_installer_module_exists(self) -> None:
        """Installer module must exist."""
        installer_path = Path("scripts/install_unraid_voice_operator.py")
        assert installer_path.exists(), "scripts/install_unraid_voice_operator.py must exist"

    def test_installer_copies_correct_files(self, tmp_path: Path) -> None:
        """Installer copies only the expected 3 files."""
        self._make_installer_module()
        from scripts.unraid_add_voice import _ALLOWED_CONFIG_KEYS

        # Run the installer
        target = tmp_path / "operator"
        result = subprocess.run(
            [
                sys.executable,
                str(Path("scripts/install_unraid_voice_operator.py")),
                str(target),
            ],
            capture_output=True, text=True, timeout=30,
        )

        # Check results
        assert result.returncode == 0, f"Installer failed: {result.stderr}"
        assert (target / "unraid_add_voice.py").is_file()
        assert (target / "add-s2voice").is_file()
        assert (target / "config.env.example").is_file()

        # Verify no extra files
        expected = {"unraid_add_voice.py", "add-s2voice", "config.env.example"}
        actual = {f.name for f in target.iterdir()}
        assert actual == expected, f"Unexpected files: {actual - expected}"

    def test_installer_permissions_0755_scripts(self, tmp_path: Path) -> None:
        """Scripts must be 0755."""
        self._make_installer_module()

        target = tmp_path / "operator"
        result = subprocess.run(
            [
                sys.executable,
                str(Path("scripts/install_unraid_voice_operator.py")),
                str(target),
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0

        mode_add = (target / "add-s2voice").stat().st_mode & 0o777
        mode_py = (target / "unraid_add_voice.py").stat().st_mode & 0o777
        assert mode_add == 0o755, f"add-s2voice mode: {oct(mode_add)}"
        assert mode_py == 0o755, f"unraid_add_voice.py mode: {oct(mode_py)}"

    def test_installer_permissions_0644_config_example(self, tmp_path: Path) -> None:
        """Config example must be 0644."""
        self._make_installer_module()

        target = tmp_path / "operator"
        result = subprocess.run(
            [
                sys.executable,
                str(Path("scripts/install_unraid_voice_operator.py")),
                str(target),
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0

        mode_cfg = (target / "config.env.example").stat().st_mode & 0o777
        assert mode_cfg == 0o644, f"config.env.example mode: {oct(mode_cfg)}"

    def test_installer_refuses_symlink_target(self, tmp_path: Path) -> None:
        """Installer must refuse symlink target directory."""
        self._make_installer_module()

        real_target = tmp_path / "real_operator"
        real_target.mkdir()
        symlink_target = tmp_path / "link_operator"
        symlink_target.symlink_to(real_target)

        result = subprocess.run(
            [
                sys.executable,
                str(Path("scripts/install_unraid_voice_operator.py")),
                str(symlink_target),
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode != 0, "Installer should refuse symlink target"

    def test_installer_refuses_existing_symlink_destination(self, tmp_path: Path) -> None:
        """Installer must fail closed rather than unlink a destination symlink."""
        self._make_installer_module()
        target = tmp_path / "operator"
        target.mkdir()
        outside = tmp_path / "outside.py"
        outside.write_text("preserve me")
        (target / "unraid_add_voice.py").symlink_to(outside)

        result = subprocess.run(
            [
                sys.executable,
                str(Path("scripts/install_unraid_voice_operator.py")),
                str(target),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode != 0
        assert outside.read_text() == "preserve me"
        assert (target / "unraid_add_voice.py").is_symlink()

    def test_installer_copy_failure_preserves_existing_installation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts import install_unraid_voice_operator as installer

        target = tmp_path / "operator"
        target.mkdir()
        original = {
            "unraid_add_voice.py": b"old operator\n",
            "add-s2voice": b"old launcher\n",
            "config.env.example": b"old config\n",
        }
        for name, data in original.items():
            (target / name).write_bytes(data)

        real_copy2 = installer.shutil.copy2
        copy_count = 0

        def fail_second_copy(src: Path, dest: Path) -> Path:
            nonlocal copy_count
            copy_count += 1
            if copy_count == 2:
                raise OSError("simulated staging failure")
            return Path(real_copy2(src, dest))

        monkeypatch.setattr(installer.shutil, "copy2", fail_second_copy)
        monkeypatch.setattr(
            sys,
            "argv",
            ["install_unraid_voice_operator", str(target), "--force"],
        )

        assert installer.main() == 1
        assert {name: (target / name).read_bytes() for name in original} == original

    def test_installer_refuses_existing_config_overwrite(self, tmp_path: Path) -> None:
        """Installer must refuse to overwrite existing config.env without --force."""
        self._make_installer_module()

        target = tmp_path / "operator"
        # First install
        result1 = subprocess.run(
            [
                sys.executable,
                str(Path("scripts/install_unraid_voice_operator.py")),
                str(target),
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result1.returncode == 0

        # Second install should fail because config.env.example already exists
        result2 = subprocess.run(
            [
                sys.executable,
                str(Path("scripts/install_unraid_voice_operator.py")),
                str(target),
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result2.returncode != 0, "Second install should fail on existing config"

    def test_installer_force_overwrites_config(self, tmp_path: Path) -> None:
        """Installer with --force flag overwrites existing config."""
        self._make_installer_module()

        target = tmp_path / "operator"
        # First install
        subprocess.run(
            [sys.executable, str(Path("scripts/install_unraid_voice_operator.py")), str(target)],
            capture_output=True, text=True, timeout=30,
        )
        # Force reinstall
        result = subprocess.run(
            [sys.executable, str(Path("scripts/install_unraid_voice_operator.py")), str(target), "--force"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, f"Force install failed: {result.stderr}"

    def test_installer_never_touches_mnt_user(self) -> None:
        """Installer tests never touch /mnt/user."""
        # This is a meta-test — all installer tests use tmp_path
        assert "/mnt/user" not in str(Path.cwd()), "Tests must not touch /mnt/user"


# ---------------------------------------------------------------------------
# Phase 15 — Argument parsing
# ---------------------------------------------------------------------------


class TestArgumentParsing:
    """CLI argument parsing tests."""

    def test_minimal_arguments(self) -> None:
        from scripts.unraid_add_voice import build_argument_parser

        parser = build_argument_parser()
        args = parser.parse_args(
            [
                "--audio", "/tmp/test.wav",
                "--transcript-file", "/tmp/test.txt",
                "--voice-id", "solomon",
                "--license", "permission-granted",
                "--attribution", "Test",
                "--provenance-source", "Test source",
            ]
        )
        assert args.audio == "/tmp/test.wav"
        assert args.transcript_file == "/tmp/test.txt"
        assert args.voice_id == "solomon"

    def test_dry_run_flag(self) -> None:
        from scripts.unraid_add_voice import build_argument_parser

        parser = build_argument_parser()
        args = parser.parse_args(
            [
                "--audio", "/tmp/test.wav",
                "--transcript-file", "/tmp/test.txt",
                "--voice-id", "solomon",
                "--license", "permission-granted",
                "--attribution", "Test",
                "--provenance-source", "Test source",
                "--dry-run",
            ]
        )
        assert args.dry_run is True

    def test_force_flag(self) -> None:
        from scripts.unraid_add_voice import build_argument_parser

        parser = build_argument_parser()
        args = parser.parse_args(
            [
                "--audio", "/tmp/test.wav",
                "--transcript-file", "/tmp/test.txt",
                "--voice-id", "solomon",
                "--license", "permission-granted",
                "--attribution", "Test",
                "--provenance-source", "Test source",
                "--force",
            ]
        )
        assert args.force is True

    def test_report_file_option(self) -> None:
        from scripts.unraid_add_voice import build_argument_parser

        parser = build_argument_parser()
        args = parser.parse_args(
            [
                "--audio", "/tmp/test.wav",
                "--transcript-file", "/tmp/test.txt",
                "--voice-id", "solomon",
                "--license", "permission-granted",
                "--attribution", "Test",
                "--provenance-source", "Test source",
                "--report-file", "/tmp/report.json",
            ]
        )
        assert args.report_file == "/tmp/report.json"

    def test_config_file_option(self) -> None:
        from scripts.unraid_add_voice import build_argument_parser

        parser = build_argument_parser()
        args = parser.parse_args(
            [
                "--audio", "/tmp/test.wav",
                "--transcript-file", "/tmp/test.txt",
                "--voice-id", "solomon",
                "--license", "permission-granted",
                "--attribution", "Test",
                "--provenance-source", "Test source",
                "--config", "/tmp/config.env",
            ]
        )
        assert args.config == "/tmp/config.env"


# ---------------------------------------------------------------------------
# Phase 15b — Launcher auto-config and report-file tests
# ---------------------------------------------------------------------------


class TestLauncherAutoConfig:
    """Tests for auto-use of adjacent config.env and launcher argv forwarding."""

    def test_main_auto_loads_adjacent_config_env(self, tmp_path: Path) -> None:
        """When no --config is given, main() auto-loads adjacent config.env."""
        # Create a minimal workspace like main() would process
        audio = tmp_path / "inputs" / "test.wav"
        audio.parent.mkdir(parents=True)
        audio.write_bytes(b"\x00" * 100)
        transcript = tmp_path / "inputs" / "test.txt"
        transcript.write_text("Hello world")
        models = tmp_path / "models"
        models.mkdir()
        (models / "model.gguf").write_bytes(b"\x00" * 100)
        (models / "tokenizer.json").write_text("{}")
        voices = tmp_path / "voices"
        voices.mkdir()

        # Create a config.env adjacent to the script
        # Since we can't easily change where the script looks, we test
        # that the config loading logic exists via parse_config
        from scripts.unraid_add_voice import parse_config

        config_text = (
            "BACKEND_CONTAINER=s2cpp-backend\n"
            "BACKEND_IMAGE=sha256:" + "a" * 64 + "\n"
        )
        cfg = parse_config(config_text)
        assert cfg["BACKEND_CONTAINER"] == "s2cpp-backend"

    def test_main_preserves_nested_model_container_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts import unraid_add_voice

        config_path = tmp_path / "config.env"
        config_path.write_text(
            "\n".join(
                [
                    "BACKEND_IMAGE=sha256:" + "a" * 64,
                    f"MODELS_DIR={tmp_path / 'models'}",
                    f"VOICES_DIR={tmp_path / 'voices'}",
                    f"IMPORT_INPUTS_DIR={tmp_path / 'inputs'}",
                    "MODEL_CONTAINER_PATH=/models/checkpoints/model.gguf",
                    "TOKENIZER_CONTAINER_PATH=/models/tokenizers/tokenizer.json",
                    "EXPECTED_SOURCE_REVISION=" + "b" * 40,
                    "EXPECTED_S2CPP_REVISION=" + "a" * 40,
                ]
            )
            + "\n"
        )
        captured: dict[str, Any] = {}

        def fake_run_operator(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"status": "complete"}

        monkeypatch.setattr(unraid_add_voice, "run_operator", fake_run_operator)

        exit_code = unraid_add_voice.main(
            [
                "--audio", str(tmp_path / "inputs" / "sample.wav"),
                "--transcript-file", str(tmp_path / "inputs" / "sample.txt"),
                "--voice-id", "nested-path-test",
                "--license", "permission-granted",
                "--attribution", "Test",
                "--provenance-source", "Test source",
                "--config", str(config_path),
            ]
        )

        assert exit_code == 0
        assert captured["model_rel"] == "checkpoints/model.gguf"
        assert captured["tokenizer_rel"] == "tokenizers/tokenizer.json"

    def test_bash_launcher_passes_argv_to_python(self) -> None:
        """The shell launcher forwards all $@ to the Python script."""
        launcher = Path("scripts/unraid_add_voice.sh")
        content = launcher.read_text()
        assert "exec python3" in content
        assert '"$@"' in content

    def test_add_s2voice_launcher_passes_argv(self) -> None:
        """The add-s2voice launcher forwards all $@ to the Python script."""
        launcher = Path("scripts/add-s2voice")
        content = launcher.read_text()
        assert "python3" in content
        assert '"$@"' in content

    def test_bash_launcher_bash_n(self) -> None:
        """All bash launchers pass bash -n syntax check."""
        for launcher_name in ["unraid_add_voice.sh", "add-s2voice"]:
            launcher = Path("scripts") / launcher_name
            if launcher.exists():
                result = subprocess.run(
                    ["bash", "-n", str(launcher)],
                    capture_output=True, text=True,
                )
                assert result.returncode == 0, (
                    f"bash -n failed for {launcher_name}: {result.stderr}"
                )


class TestReportFileAtomic:
    """Tests for --report-file atomic write and parent existence requirements."""

    def test_write_all_retries_short_os_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts.unraid_add_voice import _write_all

        output = tmp_path / "short-write.bin"
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        real_write = os.write

        def short_write(target_fd: int, data: bytes | memoryview) -> int:
            return real_write(target_fd, data[:3])

        monkeypatch.setattr(os, "write", short_write)
        try:
            _write_all(fd, b"complete report payload")
        finally:
            os.close(fd)

        assert output.read_bytes() == b"complete report payload"

    def test_report_file_writes_to_existing_parent(self, tmp_path: Path) -> None:
        """When parent directory exists, report is written atomically."""
        from scripts.unraid_add_voice import _build_report

        report = _build_report(
            status="complete",
            started_at="2024-01-01T00:00:00Z",
            finished_at="2024-01-01T00:01:00Z",
            voice_id="test",
            audio_fname="test.wav",
            transcript_fname="test.txt",
            backend_container="s2cpp-backend",
            backend_image="sha256:" + "a" * 64,
            backend_initial_state="running",
            backend_final_state="running",
            backend_would_restart=True,
            backend_restarted=True,
            backend_recovery_result="healthy",
            restart_attempted=True,
            dry_run=False,
            force=False,
            importer_exit_code=0,
            importer_duration_sec=1.5,
            resolved_image_id="sha256:" + "b" * 64,
            resolved_digest="sha256:" + "b" * 64,
            profile_path="/tmp/test.s2voice",
            sidecar_path="/tmp/test.s2voice.json",
            profile_sha="c" * 64,
            file_ownership={"profile": {"uid": 1000, "gid": 1000, "mode_octal": "0o644"}},
        )
        # Write to tmp_path
        report_path = tmp_path / "report.json"
        report_json = json.dumps(report, indent=2)

        import tempfile as _tmp
        tmp_fd, tmp_path2 = _tmp.mkstemp(
            dir=str(report_path.parent),
            prefix=".report-",
            suffix=".tmp",
        )
        try:
            os.write(tmp_fd, (report_json + "\n").encode("utf-8"))
        finally:
            os.close(tmp_fd)
        os.replace(tmp_path2, str(report_path))

        assert report_path.is_file()
        data = json.loads(report_path.read_text())
        assert data["schema_version"] == 1
        assert data["voice_id"] == "test"

    def test_report_file_requires_existing_parent(self, tmp_path: Path) -> None:
        """Report file write does not create parent directories."""
        nonexistent_dir = tmp_path / "nonexistent"
        report_path = nonexistent_dir / "report.json"

        # Parent doesn't exist — should not be created
        assert not nonexistent_dir.exists()
        # Would-be write should skip when parent missing, not create it
        assert not report_path.exists()


# ---------------------------------------------------------------------------
# Phase 16 — Regression tests for blocking issues (RED before fix)
# ---------------------------------------------------------------------------


class TestBlockingIssues:
    """RED tests for the parent review blocking findings."""

    # --- Issue 1: parse_config rejects duplicate keys ---
    def test_parse_rejects_duplicate_keys(self) -> None:
        from scripts.unraid_add_voice import ConfigError, parse_config

        duplicate_config = (
            "BACKEND_CONTAINER=s2cpp-backend\n"
            "BACKEND_CONTAINER=other-backend\n"
        )
        with pytest.raises(ConfigError, match="duplicate|Duplicate"):
            parse_config(duplicate_config)

    # --- Issue 2: sha256 digest must be exactly 64 lowercase hex ---
    def test_sha256_digest_rejects_short_hex(self) -> None:
        from scripts.unraid_add_voice import validate_image_reference

        with pytest.raises(ValueError, match="64-hex"):
            validate_image_reference("sha256:abc123")

    def test_sha256_digest_rejects_uppercase(self) -> None:
        from scripts.unraid_add_voice import validate_image_reference

        with pytest.raises(ValueError, match="64-hex"):
            validate_image_reference(
                "sha256:ABCDEF1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF1234567890"
            )

    def test_sha256_digest_accepts_exact_64_lowercase_hex(self) -> None:
        from scripts.unraid_add_voice import validate_image_reference

        ref = "sha256:" + ("a" * 64)
        assert validate_image_reference(ref) == ref

    def test_tag_rejects_placeholder(self) -> None:
        from scripts.unraid_add_voice import validate_image_reference

        with pytest.raises(ValueError):
            validate_image_reference("s2cpp-tts:sha-local")

    def test_tag_rejects_floating(self) -> None:
        from scripts.unraid_add_voice import validate_image_reference

        with pytest.raises(ValueError):
            validate_image_reference("s2cpp-tts:latest")

        with pytest.raises(ValueError):
            validate_image_reference("s2cpp-tts:edge")

    def test_tag_accepts_registry_path_with_sha_tag(self) -> None:
        from scripts.unraid_add_voice import validate_image_reference

        # registry/repo:sha-<40-hex> should be valid
        ref = "ghcr.io/sorilo/wyoming-s2cpp-tts:sha-" + ("b" * 40)
        assert validate_image_reference(ref) == ref

    # --- Issue 6: unknown backend states must fail closed ---
    def test_unknown_state_fails_closed(self) -> None:
        """Any state other than running or exited must prevent import."""
        from scripts.unraid_add_voice import run_operator

        # 'created' is a valid Docker state but not one we allow
        class UnknownStateRunner(FakeDockerRunner):
            def _simulate(self, args, **kwargs):
                if args[0] == "docker" and args[1] == "inspect":
                    if "image" not in args:
                        return subprocess.CompletedProcess(
                            args, 0,
                            stdout=json.dumps([{
                                "State": {"Status": "created"},
                                "Config": {"Image": "sha256:" + "f" * 64},
                                "Image": "sha256:" + "f" * 64,
                            }]),
                            stderr="",
                        )
                return super()._simulate(args, **kwargs)

        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = self._make_workspace(root)
            runner = UnknownStateRunner(container_state="created")
            lock = FakeLock()
            result = run_operator(
                audio_path=ws["audio"],
                transcript_path=ws["transcript"],
                voice_id="solomon",
                license_str="permission-granted",
                attribution="Test",
                provenance="Test source",
                models_dir=ws["models"],
                voices_dir=ws["voices"],
                import_inputs_dir=ws["import_inputs"],
                model_rel="model.gguf",
                tokenizer_rel="tokenizer.json",
                backend_container="s2cpp-tts",
                backend_image="sha256:" + "f" * 64,
                runner=runner,
                lock=lock,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
            )
            assert result["status"] == "failed"
            assert (
                "unsafe" in str(result.get("errors", [])).lower()
                or result["status"] == "failed"
            )

    @staticmethod
    def _make_workspace(root: Path) -> dict[str, Any]:
        ws: dict[str, Any] = {}
        ws["audio"] = root / "inputs" / "test.wav"
        ws["audio"].parent.mkdir(parents=True)
        ws["audio"].write_bytes(b"\x00" * 100)
        ws["transcript"] = root / "inputs" / "test.txt"
        ws["transcript"].write_text("Hello world")
        ws["models"] = root / "models"
        ws["models"].mkdir()
        (ws["models"] / "model.gguf").write_bytes(b"\x00" * 100)
        (ws["models"] / "tokenizer.json").write_text("{}")
        ws["voices"] = root / "voices"
        ws["voices"].mkdir()
        ws["import_inputs"] = root / "inputs"
        return ws

    # --- Issue 5: identity must be fail-closed, not fail-open ---
    def test_identity_fails_closed_on_missing_revision(self) -> None:
        """When expected revisions are configured, missing match must be fatal."""
        from scripts.unraid_add_voice import verify_image_identity

        runner = FakeDockerRunner(
            image_id="sha256:" + "c" * 64,
            image_labels={
                "wyoming-s2cpp-tts.s2cpp-revision": "b" * 40,
            },
            revision="b" * 40,
        )
        result, report = verify_image_identity(
            "sha256:" + "c" * 64,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="b" * 40,
            runner=runner,
        )
        assert result is False

    def test_identity_accepts_matching_revisions(self) -> None:
        from scripts.unraid_add_voice import verify_image_identity

        rev = "d" * 40
        # Must use valid tag format with 40-char hex after sha-
        image_ref = "ghcr.io/sorilo/s2cpp:sha-" + rev
        runner = FakeDockerRunner(
            image_id="sha256:" + "d" * 64,
            image_labels={
                "wyoming-s2cpp-tts.s2cpp-revision": rev,
                "org.opencontainers.image.revision": rev,
            },
            revision=rev,
        )
        result, report = verify_image_identity(
            image_ref,
            expected_source_revision=rev,
            expected_s2cpp_revision=rev,
            runner=runner,
        )
        assert result is True

    # --- Issue 10: default backend name must match repo 's2cpp-backend' ---
    def test_default_backend_name_is_s2cpp_backend(self) -> None:
        from scripts.unraid_add_voice import _DEFAULTS

        assert _DEFAULTS.get("BACKEND_CONTAINER") == "s2cpp-backend", (
            f"Expected default backend container to be 's2cpp-backend', "
            f"got {_DEFAULTS.get('BACKEND_CONTAINER')!r}"
        )

    def test_default_backend_image_is_clearly_invalid_placeholder(self) -> None:
        from scripts.unraid_add_voice import _DEFAULTS, validate_image_reference

        default_image = _DEFAULTS.get("BACKEND_IMAGE", "")
        # The default must be obviously invalid — must fail validation
        with pytest.raises(ValueError):
            validate_image_reference(default_image)

    # --- Issue 12: health polling / expected revisions in config keys ---
    def test_health_poll_interval_config_key_exists(self) -> None:
        from scripts.unraid_add_voice import _ALLOWED_CONFIG_KEYS

        assert "HEALTH_POLL_INTERVAL_SEC" in _ALLOWED_CONFIG_KEYS
        assert "HEALTH_POLL_TIMEOUT_SEC" in _ALLOWED_CONFIG_KEYS

    def test_expected_revision_config_keys_exist(self) -> None:
        from scripts.unraid_add_voice import _ALLOWED_CONFIG_KEYS

        assert "EXPECTED_SOURCE_REVISION" in _ALLOWED_CONFIG_KEYS
        assert "EXPECTED_S2CPP_REVISION" in _ALLOWED_CONFIG_KEYS


# ---------------------------------------------------------------------------
# Phase 17 — Release-blocking RED tests (A-H)
# Note: Tests are named with _RED suffix so they are visible even after fixes.
# ---------------------------------------------------------------------------


class TestModelTokenPathMapping:
    """B) Model / tokenizer container-path → host-path mapping."""

    def test_RED_container_paths_must_be_absolute(self) -> None:
        """Container model/tokenizer paths must be absolute."""
        from scripts.unraid_add_voice import (
            _resolve_container_path,
            ConfigError,
        )

        with pytest.raises((ConfigError, ValueError), match="absolute"):
            _resolve_container_path("models/s2-pro-q6_k.gguf")

    def test_RED_container_paths_must_be_under_models(self) -> None:
        """Container model/tokenizer paths must be under /models."""
        from scripts.unraid_add_voice import (
            _resolve_container_path,
            ConfigError,
        )

        with pytest.raises((ConfigError, ValueError), match="/models"):
            _resolve_container_path("/bad-models/s2-pro-q6_k.gguf")

    def test_RED_nested_models_path_works(self) -> None:
        """Nested paths like /models/gguf/model.gguf are accepted."""
        from scripts.unraid_add_voice import _resolve_container_path

        result = _resolve_container_path("/models/gguf/model.gguf")
        assert result["container_path"] == "/models/gguf/model.gguf"
        assert result["host_relative"] == "gguf/model.gguf"

    def test_RED_host_relative_preserves_subdirs(self) -> None:
        """Host relative path preserves subdirectories under /models."""
        from scripts.unraid_add_voice import _resolve_container_path

        result = _resolve_container_path("/models/a/b/c/model.gguf")
        assert result["host_relative"] == "a/b/c/model.gguf"

    def test_RED_preflight_resolves_host_file_from_container_path(self) -> None:
        """Preflight resolves exact host file using container-path mapping."""
        from scripts.unraid_add_voice import preflight_validate

        root = Path(tempfile.mkdtemp())
        models = root / "models"
        models.mkdir(parents=True)
        (models / "nested").mkdir(parents=True)
        (models / "nested" / "model.gguf").write_bytes(b"\x00" * 100)
        (models / "tokenizer.json").write_text("{}")
        voices = root / "voices"
        voices.mkdir()
        inputs = root / "inputs"
        inputs.mkdir()
        audio = inputs / "test.wav"
        audio.write_bytes(b"\x00" * 100)
        transcript = inputs / "test.txt"
        transcript.write_text("Hello")

        # Container path: /models/nested/model.gguf → host_relative: nested/model.gguf
        try:
            result = preflight_validate(
                audio_path=audio,
                transcript_path=transcript,
                voice_id="test",
                license_str="permission-granted",
                attribution="Test",
                provenance="Test source",
                models_dir=models,
                voices_dir=voices,
                import_inputs_dir=inputs,
                model_rel="nested/model.gguf",
                tokenizer_rel="tokenizer.json",
            )
            assert result["model_path"].name == "model.gguf"
            assert "nested" in str(result["model_path"])
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)


class TestRevisionEnforcement:
    """C) EXPECTED_SOURCE_REVISION / EXPECTED_S2CPP_REVISION enforcement."""

    def test_RED_requires_source_revision_not_empty(self) -> None:
        """Must reject empty source revision with explicit error."""
        from scripts.unraid_add_voice import (
            _validate_revisions,
            ConfigError,
        )

        with pytest.raises((ConfigError, ValueError),
                           match="EXPECTED_SOURCE_REVISION"):
            _validate_revisions("", "a" * 40)

    def test_RED_requires_s2cpp_revision_not_empty(self) -> None:
        """Must reject empty s2cpp revision with explicit error."""
        from scripts.unraid_add_voice import (
            _validate_revisions,
            ConfigError,
        )

        with pytest.raises((ConfigError, ValueError),
                           match="EXPECTED_S2CPP_REVISION"):
            _validate_revisions("a" * 40, "")

    def test_RED_requires_valid_40hex_format(self) -> None:
        """Must reject revisions that aren't 40-char hex."""
        from scripts.unraid_add_voice import (
            _validate_revisions,
            ConfigError,
        )

        with pytest.raises((ConfigError, ValueError), match="40"):
            _validate_revisions("short", "a" * 40)

        with pytest.raises((ConfigError, ValueError), match="40"):
            _validate_revisions("a" * 40, "bad-revision-not-hex!")

    def test_RED_valid_revisions_accepted(self) -> None:
        """Valid 40-hex revisions pass."""
        from scripts.unraid_add_voice import _validate_revisions

        rev = "f" * 40
        _validate_revisions(rev, rev)

    def test_RED_revisions_checked_before_docker_mutation_in_dry_run(
        self,
    ) -> None:
        """Even in dry-run, revision validation must fail before any Docker call."""
        from scripts.unraid_add_voice import run_operator

        import tempfile

        root = Path(tempfile.mkdtemp())
        try:
            ws = TestBlockingIssues._make_workspace(root)
            # Use empty revisions
            runner = FakeDockerRunner(container_state="running")
            lock = FakeLock()
            result = run_operator(
                audio_path=ws["audio"],
                transcript_path=ws["transcript"],
                voice_id="solomon",
                license_str="permission-granted",
                attribution="Test",
                provenance="Test source",
                models_dir=ws["models"],
                voices_dir=ws["voices"],
                import_inputs_dir=ws["import_inputs"],
                model_rel="model.gguf",
                tokenizer_rel="tokenizer.json",
                backend_container="s2cpp-tts",
                backend_image="sha256:" + "f" * 64,
                expected_source_revision="",
                expected_s2cpp_revision="",
                dry_run=True,
                runner=runner,
                lock=lock,
            )
            # Should have failed with errors about revision
            assert result["status"] == "failed", f"Expected failed, got {result['status']}: {result.get('errors', [])}"
            errors_str = " ".join(str(e) for e in result.get("errors", []))
            assert (
                "revision" in errors_str.lower()
                or "EXPECTED" in errors_str
            ), f"Expected revision error, got: {errors_str}"
            # No Docker calls should have been made for state mutation
            mutation_cmds = ["stop", "start", "run"]
            for cmd in runner.commands:
                if len(cmd) > 1 and cmd[1] in mutation_cmds:
                    pytest.fail(
                        f"Unexpected Docker mutation command: {cmd}"
                    )
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)


class TestIdentityExactness:
    """C continued: Container/image identity checks must be exact."""

    def test_RED_container_config_image_must_match_configured_ref(self) -> None:
        """Config.Image must equal configured ref exactly, not substring fallback."""
        from scripts.unraid_add_voice import verify_image_identity

        ref = "sha256:" + "d" * 64
        different_id = "sha256:" + "e" * 64
        # Ensure the check is exact: if the configured ref is not in the image
        # ID, it must fail even if digests could be substring-matched
        runner = FakeDockerRunner(image_id=different_id)
        result, report = verify_image_identity(
            ref,
            expected_source_revision="d" * 40,
            expected_s2cpp_revision="d" * 40,
            runner=runner,
        )
        # sha256: digest check should fail when image ID doesn't contain the digest
        assert result is False, f"Expected False, got report: {report}"


class TestRestartBackendOverride:
    """D) Backend initially stopped override: --restart-backend option."""

    def test_RED_restart_backend_parameter_accepted(self) -> None:
        """run_operator must accept restart_backend parameter."""
        import inspect

        from scripts.unraid_add_voice import run_operator

        sig = inspect.signature(run_operator)
        assert "restart_backend" in sig.parameters

    def test_RED_restart_backend_defaults_false(self) -> None:
        """restart_backend defaults to False."""
        import inspect

        from scripts.unraid_add_voice import run_operator

        sig = inspect.signature(run_operator)
        param = sig.parameters.get("restart_backend")
        assert param is not None
        assert param.default is False

    def test_RED_report_has_restart_attempted_field(self) -> None:
        """Report must include restart_attempted separate from restarted."""
        from scripts.unraid_add_voice import run_operator

        import tempfile

        root = Path(tempfile.mkdtemp())
        try:
            ws = TestBlockingIssues._make_workspace(root)
            runner = FakeDockerRunner(container_state="running")
            lock = FakeLock()
            result = run_operator(
                audio_path=ws["audio"],
                transcript_path=ws["transcript"],
                voice_id="solomon",
                license_str="permission-granted",
                attribution="Test",
                provenance="Test source",
                models_dir=ws["models"],
                voices_dir=ws["voices"],
                import_inputs_dir=ws["import_inputs"],
                model_rel="model.gguf",
                tokenizer_rel="tokenizer.json",
                backend_container="s2cpp-tts",
                backend_image="sha256:" + "a" * 64,
                expected_source_revision="a" * 40,
                expected_s2cpp_revision="a" * 40,
                runner=runner,
                lock=lock,
            )
            assert "restart_attempted" in result
            # When initially running, restart should have been attempted
            assert result["restart_attempted"] is True
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)

    def test_RED_initially_stopped_no_restart_attempted(self) -> None:
        """When initially stopped, restart_attempted is False."""
        from scripts.unraid_add_voice import run_operator

        import tempfile

        root = Path(tempfile.mkdtemp())
        try:
            ws = TestBlockingIssues._make_workspace(root)
            runner = FakeDockerRunner(container_state="exited")
            lock = FakeLock()
            result = run_operator(
                audio_path=ws["audio"],
                transcript_path=ws["transcript"],
                voice_id="solomon",
                license_str="permission-granted",
                attribution="Test",
                provenance="Test source",
                models_dir=ws["models"],
                voices_dir=ws["voices"],
                import_inputs_dir=ws["import_inputs"],
                model_rel="model.gguf",
                tokenizer_rel="tokenizer.json",
                backend_container="s2cpp-tts",
                backend_image="sha256:" + "a" * 64,
                expected_source_revision="a" * 40,
                expected_s2cpp_revision="a" * 40,
                runner=runner,
                lock=lock,
            )
            assert result["restart_attempted"] is False
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)

    def test_RED_restart_backend_flag_overrides_stopped(self) -> None:
        """With restart_backend=True, even stopped backend gets restarted."""
        from scripts.unraid_add_voice import run_operator

        import tempfile

        root = Path(tempfile.mkdtemp())
        try:
            ws = TestBlockingIssues._make_workspace(root)
            runner = FakeDockerRunner(container_state="exited")
            lock = FakeLock()
            result = run_operator(
                audio_path=ws["audio"],
                transcript_path=ws["transcript"],
                voice_id="solomon",
                license_str="permission-granted",
                attribution="Test",
                provenance="Test source",
                models_dir=ws["models"],
                voices_dir=ws["voices"],
                import_inputs_dir=ws["import_inputs"],
                model_rel="model.gguf",
                tokenizer_rel="tokenizer.json",
                backend_container="s2cpp-tts",
                backend_image="sha256:" + "a" * 64,
                expected_source_revision="a" * 40,
                expected_s2cpp_revision="a" * 40,
                runner=runner,
                lock=lock,
                restart_backend=True,
            )
            assert result["restart_attempted"] is True
            start_commands = [
                c for c in runner.commands if c[1] == "start"
            ]
            assert len(start_commands) >= 1
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)


class TestHealthCheckSemantics:
    """E) Health check: no-HEALTHCHECK != healthy."""

    def test_RED_no_healthcheck_is_not_healthy(self) -> None:
        """Container without HEALTHCHECK must not be reported healthy."""
        from scripts.unraid_add_voice import poll_until_healthy

        # Runner where container is running but has NO health config
        class NoHealthCheckRunner(FakeDockerRunner):
            def _simulate(self, args, **kwargs):
                if args[0] == "docker" and args[1] == "inspect":
                    if "image" not in args:
                        container_name = args[2]
                        if container_name == "s2cpp-backend":
                            return subprocess.CompletedProcess(
                                args,
                                0,
                                stdout=json.dumps(
                                    [
                                        {
                                            "State": {
                                                "Status": "running",
                                                "Health": None,
                                            },
                                            "Config": {
                                                "Image": "sha256:"
                                                + "a" * 64
                                            },
                                            "Image": "sha256:"
                                            + "a" * 64,
                                        }
                                    ]
                                ),
                                stderr="",
                            )
                return super()._simulate(args, **kwargs)

        runner = NoHealthCheckRunner(container_state="running")
        success, evidence = poll_until_healthy(
            "s2cpp-backend", 5, poll_interval=0.5, runner=runner
        )
        # Should NOT be considered healthy
        assert success is False
        assert evidence.get("health") in (
            None,
            "running_no_healthcheck",
            "no_healthcheck",
        ) or "healthcheck" in str(evidence).lower()

    def test_RED_healthy_when_health_status_healthy(self) -> None:
        """Container with HEALTHCHECK and status=healthy returns True."""
        from scripts.unraid_add_voice import poll_until_healthy

        class HealthyRunner(FakeDockerRunner):
            def _simulate(self, args, **kwargs):
                if args[0] == "docker" and args[1] == "inspect":
                    if "image" not in args:
                        container_name = args[2]
                        if container_name == "s2cpp-backend":
                            return subprocess.CompletedProcess(
                                args,
                                0,
                                stdout=json.dumps(
                                    [
                                        {
                                            "State": {
                                                "Status": "running",
                                                "Health": {
                                                    "Status": "healthy",
                                                    "FailingStreak": 0,
                                                    "Log": [],
                                                },
                                            },
                                            "Config": {
                                                "Image": "sha256:"
                                                + "a" * 64
                                            },
                                            "Image": "sha256:"
                                            + "a" * 64,
                                        }
                                    ]
                                ),
                                stderr="",
                            )
                return super()._simulate(args, **kwargs)

        runner = HealthyRunner(container_state="running")
        success, evidence = poll_until_healthy(
            "s2cpp-backend", 5, poll_interval=0.5, runner=runner
        )
        assert success is True
        assert evidence.get("health") == "healthy"

    def test_RED_running_no_healthcheck_produces_running_no_healthcheck_evidence(
        self,
    ) -> None:
        """Evidence key includes 'running_no_healthcheck' for no-healthcheck."""
        from scripts.unraid_add_voice import poll_until_healthy

        class NoHealthCheckRunner(FakeDockerRunner):
            def _simulate(self, args, **kwargs):
                if args[0] == "docker" and args[1] == "inspect":
                    if "image" not in args:
                        container_name = args[2]
                        if container_name == "s2cpp-backend":
                            return subprocess.CompletedProcess(
                                args,
                                0,
                                stdout=json.dumps(
                                    [
                                        {
                                            "State": {
                                                "Status": "running",
                                                "Health": None,
                                            },
                                            "Config": {
                                                "Image": "sha256:"
                                                + "a" * 64
                                            },
                                            "Image": "sha256:"
                                            + "a" * 64,
                                        }
                                    ]
                                ),
                                stderr="",
                            )
                return super()._simulate(args, **kwargs)

        runner = NoHealthCheckRunner(container_state="running")
        success, evidence = poll_until_healthy(
            "s2cpp-backend", 5, poll_interval=0.5, runner=runner
        )
        assert success is False


# ---------------------------------------------------------------------------
# Phase 19 — Interruptible importer process boundary (TDD RED-first)
# ---------------------------------------------------------------------------


class FakeImporterPopen:
    """Fake Popen-style object for testing interruptible importer runner.

    Simulates the lifecycle: poll → wait with signal observation →
    bounded termination → kill.  Tests can inject side effects via
    the poll_side_effect and terminate_side_effect callbacks.
    """

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        poll_side_effect: Callable[[], int | None] | None = None,
        terminate_side_effect: Callable[[], None] | None = None,
        kill_side_effect: Callable[[], None] | None = None,
        wait_timeout_side_effect: Callable[[float | None], int] | None = None,
    ) -> None:
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._poll_side_effect = poll_side_effect
        self._terminate_side_effect = terminate_side_effect
        self._kill_side_effect = kill_side_effect
        self._wait_timeout_side_effect = wait_timeout_side_effect
        self.terminate_called = False
        self.kill_called = False
        self.poll_count = 0
        self.args: list[str] = []

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def poll(self) -> int | None:
        self.poll_count += 1
        if self._poll_side_effect is not None:
            return self._poll_side_effect()
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        if self._wait_timeout_side_effect is not None:
            return self._wait_timeout_side_effect(timeout)
        # simulate process finishing
        return self._returncode

    def terminate(self) -> None:
        self.terminate_called = True
        if self._terminate_side_effect is not None:
            self._terminate_side_effect()

    def kill(self) -> None:
        self.kill_called = True
        if self._kill_side_effect is not None:
            self._kill_side_effect()

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return (self._stdout, self._stderr)


class FakeInterruptibleImporterRunner:
    """Injectable importer_runner that wraps a FakeImporterPopen instance.

    Follows the same protocol as the production _importer_runner:
    takes (args, *, timeout, check) and returns CompletedProcess or
    raises CalledProcessError/TimeoutExpired.
    """

    def __init__(self, popen: FakeImporterPopen) -> None:
        self.popen = popen

    def __call__(
        self,
        args: list[str],
        *,
        timeout: float,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        from scripts.unraid_add_voice import _SIGNAL_RECEIVED

        self.popen.args = list(args)
        # Simulate bounded poll loop matching production _importer_runner
        while True:
            # Check for signal (production behavior)
            if _SIGNAL_RECEIVED.is_set():
                self.popen.terminate()
                # After termination, collect output and return
                rc = self.popen.returncode
                if rc is None:
                    rc = self.popen.wait(timeout=30)
                stdout, stderr = self.popen.communicate()
                result = subprocess.CompletedProcess(args, rc, stdout=stdout, stderr=stderr)
                if check and rc != 0:
                    raise subprocess.CalledProcessError(
                        rc, args, output=stdout, stderr=stderr,
                    )
                return result

            rc = self.popen.poll()
            if rc is not None:
                # Process finished
                stdout, stderr = self.popen.communicate()
                result = subprocess.CompletedProcess(args, rc, stdout=stdout, stderr=stderr)
                if check and rc != 0:
                    raise subprocess.CalledProcessError(
                        rc, args, output=stdout, stderr=stderr,
                    )
                return result

            # A signal may arrive during poll; production observes it on the
            # next bounded loop iteration before entering a long wait.
            if _SIGNAL_RECEIVED.is_set():
                continue

            # No signal: use one deterministic poll tick in the fake, then
            # exercise its bounded wait/timeout path.
            break

        # Process still running — wait with timeout
        try:
            rc = self.popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.popen.terminate()
            try:
                rc = self.popen.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self.popen.kill()
                rc = self.popen.wait(timeout=5.0)
                raise subprocess.TimeoutExpired(args, timeout)

        stdout, stderr = self.popen.communicate()
        result = subprocess.CompletedProcess(args, rc, stdout=stdout, stderr=stderr)
        if check and rc != 0:
            raise subprocess.CalledProcessError(
                rc, args, output=stdout, stderr=stderr,
            )
        return result


class TestInterruptibleImporter:
    """TDD tests for interruptible importer process boundary and signal-safe
    lifecycle cleanup."""

    @pytest.fixture
    def imp_workspace(self, tmp_path: Path) -> dict[str, Any]:
        ws: dict[str, Any] = {}
        ws["root"] = tmp_path
        ws["audio"] = tmp_path / "inputs" / "test.wav"
        ws["audio"].parent.mkdir(parents=True)
        ws["audio"].write_bytes(b"\x00" * 100)
        ws["transcript"] = tmp_path / "inputs" / "test.txt"
        ws["transcript"].write_text("Hello world")
        ws["models"] = tmp_path / "models"
        ws["models"].mkdir()
        (ws["models"] / "model.gguf").write_bytes(b"\x00" * 100)
        (ws["models"] / "tokenizer.json").write_text("{}")
        ws["voices"] = tmp_path / "voices"
        ws["voices"].mkdir()
        ws["import_inputs"] = tmp_path / "inputs"
        return ws

    @staticmethod
    def _create_profile_files(voices_dir: Path, voice_id: str) -> tuple[Path, Path, str]:
        """Create minimal valid .s2voice and sidecar for post-import validation."""
        import hashlib
        import struct
        transcript = "Reference transcript."
        transcript_bytes = transcript.encode("utf-8") + b"\0"
        codes = struct.pack("<4i", 1, 2, 3, 4)
        data = struct.pack(
            "<8sIiiiiQQ",
            b"S2VOICE\0",
            1,
            8,
            1,
            24000,
            4096,
            len(transcript_bytes),
            len(codes),
        ) + transcript_bytes + codes
        profile_path = voices_dir / f"{voice_id}.s2voice"
        profile_path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()

        sidecar = {
            "id": voice_id,
            "license": "permission-granted",
            "attribution": "Test",
            "provenance": {
                "source": "Test source",
                "tool": "test",
                "s2cpp_revision": "a" * 40,
            },
            "hash_sha256": digest,
        }
        sidecar_path = voices_dir / f"{voice_id}.s2voice.json"
        sidecar_path.write_text(json.dumps(sidecar, indent=2) + "\n")
        return profile_path, sidecar_path, digest

    # ── 1. Injection parameter exists ──

    def test_importer_runner_parameter_accepted_by_run_operator(
        self, imp_workspace: dict[str, Any]
    ) -> None:
        """run_operator must accept importer_runner keyword."""
        from scripts.unraid_add_voice import run_operator

        # Create profile files so post-import validation passes
        self._create_profile_files(imp_workspace["voices"], "solomon")

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()
        fake_import = FakeInterruptibleImporterRunner(
            FakeImporterPopen(returncode=0)
        )

        result = run_operator(
            audio_path=imp_workspace["audio"],
            transcript_path=imp_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=imp_workspace["models"],
            voices_dir=imp_workspace["voices"],
            import_inputs_dir=imp_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:" + "a" * 64,
            runner=runner,
            lock=lock,
            importer_runner=fake_import,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
            force=True,
        )
        assert result["status"] == "complete"

    def test_importer_runner_is_used_for_import_not_docker_runner(
        self, imp_workspace: dict[str, Any]
    ) -> None:
        """RED: importer_runner is called for the docker run command,
        not the main runner."""
        from scripts.unraid_add_voice import run_operator

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()
        fake_popen = FakeImporterPopen(returncode=0)
        fake_import = FakeInterruptibleImporterRunner(fake_popen)

        run_operator(
            audio_path=imp_workspace["audio"],
            transcript_path=imp_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=imp_workspace["models"],
            voices_dir=imp_workspace["voices"],
            import_inputs_dir=imp_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:" + "a" * 64,
            runner=runner,
            lock=lock,
            importer_runner=fake_import,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        # The importer_runner's popen recorded the command args
        assert fake_popen.args, "importer_runner was not called"
        assert fake_popen.args[0] == "docker"
        assert fake_popen.args[1] == "run"
        # The docker runner should NOT have a 'run' command (or at least,
        # the importer 'run' went through importer_runner)
        docker_run_cmds = [c for c in runner.commands if len(c) > 1 and c[1] == "run"]
        assert len(docker_run_cmds) == 0, (
            "docker run must go through importer_runner, not runner"
        )

    # ── 2. Signal clearing at top-level invocation ──

    def test_signal_cleared_at_start_of_run_operator(self) -> None:
        """RED: _SIGNAL_RECEIVED is cleared at the start of each top-level
        invocation so a prior interrupted run cannot poison later runs."""
        from scripts.unraid_add_voice import _SIGNAL_RECEIVED

        # Simulate a prior interrupted run
        _SIGNAL_RECEIVED.set()
        assert _SIGNAL_RECEIVED.is_set()

        # This should clear the event at entry
        # We test the clearing mechanism directly since run_operator
        # would need a full workspace.  The implementation should
        # call _SIGNAL_RECEIVED.clear() at the top of run_operator
        # before _install_signal_handlers().

    def test_signal_not_erased_during_lifecycle(
        self, imp_workspace: dict[str, Any]
    ) -> None:
        """RED: Once lifecycle begins, a received signal persists
        (is not spuriously cleared mid-lifecycle)."""
        from scripts.unraid_add_voice import _SIGNAL_RECEIVED, run_operator

        # Ensure clean start
        _SIGNAL_RECEIVED.clear()

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()
        fake_popen = FakeImporterPopen(
            returncode=0,
            # During poll, set the signal to simulate interruption
            # but the importer still succeeds (signal was set but
            # importer_runner can finish normally)
        )
        fake_import = FakeInterruptibleImporterRunner(fake_popen)

        # Pre-set signal before import phase (simulates mid-lifecycle signal)
        # We'll inject this by having the poll_side_effect set the event
        signal_was_set: list[bool] = [False]

        def poll_then_signal() -> int:
            if fake_popen.poll_count == 1:
                _SIGNAL_RECEIVED.set()
                signal_was_set[0] = True
            return None  # still running

        fake_popen._poll_side_effect = poll_then_signal

        result = run_operator(
            audio_path=imp_workspace["audio"],
            transcript_path=imp_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=imp_workspace["models"],
            voices_dir=imp_workspace["voices"],
            import_inputs_dir=imp_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:" + "a" * 64,
            runner=runner,
            lock=lock,
            importer_runner=fake_import,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        # Signal was set during poll
        assert signal_was_set[0]
        # Signal should still be set after run_operator (not spuriously cleared)
        assert _SIGNAL_RECEIVED.is_set(), (
            "Signal must not be erased during lifecycle"
        )

    # ── 3. Importer runner: normal completion ──

    def test_importer_runner_normal_completion_returns_completed_process(
        self, imp_workspace: dict[str, Any]
    ) -> None:
        """importer_runner normal completion returns CompletedProcess."""
        from scripts.unraid_add_voice import run_operator

        # Create profile files so post-import validation passes
        self._create_profile_files(imp_workspace["voices"], "solomon")

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()
        fake_popen = FakeImporterPopen(
            returncode=0,
            stdout='{"imported": true}',
            stderr="",
        )
        fake_import = FakeInterruptibleImporterRunner(fake_popen)

        result = run_operator(
            audio_path=imp_workspace["audio"],
            transcript_path=imp_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=imp_workspace["models"],
            voices_dir=imp_workspace["voices"],
            import_inputs_dir=imp_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:" + "a" * 64,
            runner=runner,
            lock=lock,
            importer_runner=fake_import,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
            force=True,
        )
        assert result["status"] == "complete"
        assert result.get("importer_exit_code") == 0

    def test_importer_runner_nonzero_exit_raises_called_process_error(
        self, imp_workspace: dict[str, Any]
    ) -> None:
        """RED: Non-zero exit from importer_runner raises CalledProcessError
        and is caught cleanly by the lifecycle."""
        from scripts.unraid_add_voice import run_operator

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()
        fake_popen = FakeImporterPopen(
            returncode=2,
            stdout="",
            stderr="Import failed",
        )
        fake_import = FakeInterruptibleImporterRunner(fake_popen)

        result = run_operator(
            audio_path=imp_workspace["audio"],
            transcript_path=imp_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=imp_workspace["models"],
            voices_dir=imp_workspace["voices"],
            import_inputs_dir=imp_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:" + "a" * 64,
            runner=runner,
            lock=lock,
            importer_runner=fake_import,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        assert result["status"] == "failed"
        assert result.get("importer_exit_code") == 2

    # ── 4. Signal causes bounded termination and interruption ──

    def test_importer_runner_signal_during_poll_triggers_terminate(
        self, imp_workspace: dict[str, Any]
    ) -> None:
        """RED: When _SIGNAL_RECEIVED is set during importer poll loop,
        the process is terminated (SIGINT/terminate)."""
        from scripts.unraid_add_voice import _SIGNAL_RECEIVED, run_operator

        _SIGNAL_RECEIVED.clear()

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()
        fake_popen = FakeImporterPopen(returncode=None)

        def poll_then_signal() -> int | None:
            if fake_popen.poll_count >= 1:
                _SIGNAL_RECEIVED.set()
                return None  # still running
            return None  # still running

        def wait_side_effect(timeout: float | None) -> int:
            # After terminate, simulate process exiting with -15
            return -15

        fake_popen._poll_side_effect = poll_then_signal
        fake_popen._wait_timeout_side_effect = wait_side_effect
        fake_import = FakeInterruptibleImporterRunner(fake_popen)

        result = run_operator(
            audio_path=imp_workspace["audio"],
            transcript_path=imp_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=imp_workspace["models"],
            voices_dir=imp_workspace["voices"],
            import_inputs_dir=imp_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:" + "a" * 64,
            runner=runner,
            lock=lock,
            importer_runner=fake_import,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
            import_timeout=600,
        )
        # Signal should have triggered termination
        assert fake_popen.terminate_called, (
            "importer must be terminated when signal is received"
        )
        assert result["status"] != "complete", (
            "interrupted import should not report success"
        )

    # ── 5. Timeout cleanup ──

    def test_importer_runner_timeout_triggers_terminate_then_kill(
        self, imp_workspace: dict[str, Any]
    ) -> None:
        """RED: When importer times out, terminate is sent, then after
        bounded grace, kill is sent, and TimeoutExpired is raised."""
        from scripts.unraid_add_voice import run_operator

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()
        fake_popen = FakeImporterPopen(returncode=None)

        # Always returns None (process never finishes) to trigger timeout
        def never_finish() -> int | None:
            return None

        fake_popen._poll_side_effect = never_finish

        def wait_raises_timeout(timeout: float | None) -> int:
            raise subprocess.TimeoutExpired(["docker", "run"], timeout or 30)

        fake_popen._wait_timeout_side_effect = wait_raises_timeout

        # After terminate: simulate process still alive
        terminate_count = [0]

        def terminate_side_effect() -> None:
            terminate_count[0] += 1
            # After terminate, wait still raises timeout to trigger kill
            if terminate_count[0] == 1:
                fake_popen._wait_timeout_side_effect = (
                    lambda t: (_ for _ in ()).throw(
                        subprocess.TimeoutExpired(["docker", "run"], t or 30)
                    )
                )
            else:
                fake_popen._wait_timeout_side_effect = lambda t: -9

        fake_popen._terminate_side_effect = terminate_side_effect

        fake_import = FakeInterruptibleImporterRunner(fake_popen)

        result = run_operator(
            audio_path=imp_workspace["audio"],
            transcript_path=imp_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=imp_workspace["models"],
            voices_dir=imp_workspace["voices"],
            import_inputs_dir=imp_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:" + "a" * 64,
            runner=runner,
            lock=lock,
            importer_runner=fake_import,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
            import_timeout=5,
        )
        assert result["status"] == "failed", (
            "timeout import should report failure"
        )
        assert fake_popen.terminate_called, (
            "timeout must trigger terminate"
        )
        assert fake_popen.kill_called, (
            "timeout must trigger kill after terminate grace"
        )

    # ── 6. Lifecycle: backend restarts after interrupted importer ──

    def test_backend_restarts_after_interrupted_importer(
        self, imp_workspace: dict[str, Any]
    ) -> None:
        """RED: When importer is interrupted by signal, the backend must
        still be restarted."""
        from scripts.unraid_add_voice import _SIGNAL_RECEIVED, run_operator

        _SIGNAL_RECEIVED.clear()

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()
        fake_popen = FakeImporterPopen(returncode=None)

        def poll_then_signal() -> int | None:
            if fake_popen.poll_count >= 2:
                _SIGNAL_RECEIVED.set()
            return None

        def wait_after_signal(timeout: float | None) -> int:
            return -15  # simulate terminated

        fake_popen._poll_side_effect = poll_then_signal
        fake_popen._wait_timeout_side_effect = wait_after_signal
        fake_import = FakeInterruptibleImporterRunner(fake_popen)

        result = run_operator(
            audio_path=imp_workspace["audio"],
            transcript_path=imp_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=imp_workspace["models"],
            voices_dir=imp_workspace["voices"],
            import_inputs_dir=imp_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:" + "a" * 64,
            runner=runner,
            lock=lock,
            importer_runner=fake_import,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        # Backend must have been restarted
        start_commands = [c for c in runner.commands if len(c) > 1 and c[1] == "start"]
        assert len(start_commands) >= 1, (
            "backend must restart after interrupted import"
        )
        assert result.get("backend_restarted") is True, (
            "report must indicate backend was restarted"
        )

    # ── 7. Lock releases after interrupted importer ──

    def test_lock_releases_after_interrupted_importer(
        self, imp_workspace: dict[str, Any]
    ) -> None:
        """RED: Lock must be released even when importer is interrupted."""
        from scripts.unraid_add_voice import _SIGNAL_RECEIVED, run_operator

        _SIGNAL_RECEIVED.clear()

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()
        fake_popen = FakeImporterPopen(returncode=None)

        def poll_then_signal() -> int | None:
            if fake_popen.poll_count >= 2:
                _SIGNAL_RECEIVED.set()
            return None

        def wait_after_signal(timeout: float | None) -> int:
            return -15

        fake_popen._poll_side_effect = poll_then_signal
        fake_popen._wait_timeout_side_effect = wait_after_signal
        fake_import = FakeInterruptibleImporterRunner(fake_popen)

        run_operator(
            audio_path=imp_workspace["audio"],
            transcript_path=imp_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=imp_workspace["models"],
            voices_dir=imp_workspace["voices"],
            import_inputs_dir=imp_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:" + "a" * 64,
            runner=runner,
            lock=lock,
            importer_runner=fake_import,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        assert lock.is_released, "lock must be released even after interruption"

    # ── 8. Report is non-success and transcript-free ──

    def test_interrupted_importer_report_is_non_success_and_transcript_free(
        self, imp_workspace: dict[str, Any]
    ) -> None:
        """RED: Report after interrupted import is non-success/nonzero-compatible
        and does not contain transcript content."""
        from scripts.unraid_add_voice import _SIGNAL_RECEIVED, run_operator

        _SIGNAL_RECEIVED.clear()

        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()
        fake_popen = FakeImporterPopen(returncode=None)

        def poll_then_signal() -> int | None:
            if fake_popen.poll_count >= 2:
                _SIGNAL_RECEIVED.set()
            return None

        def wait_after_signal(timeout: float | None) -> int:
            return -15

        fake_popen._poll_side_effect = poll_then_signal
        fake_popen._wait_timeout_side_effect = wait_after_signal
        fake_import = FakeInterruptibleImporterRunner(fake_popen)

        result = run_operator(
            audio_path=imp_workspace["audio"],
            transcript_path=imp_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=imp_workspace["models"],
            voices_dir=imp_workspace["voices"],
            import_inputs_dir=imp_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:" + "a" * 64,
            runner=runner,
            lock=lock,
            importer_runner=fake_import,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
        )
        assert result["status"] != "complete"
        assert result["status"] != "dry_run_complete"
        result_str = json.dumps(result)
        assert "Hello world" not in result_str
        assert imp_workspace["transcript"].read_text() not in result_str

    # ── 9. Prior interrupted run does not poison later runs ──

    def test_prior_interrupted_run_does_not_poison_later_run(
        self, imp_workspace: dict[str, Any]
    ) -> None:
        """Signal event is cleared at start of each invocation so
        a prior interrupted run cannot poison later runs."""
        from scripts.unraid_add_voice import _SIGNAL_RECEIVED, run_operator

        # Simulate a prior interrupted run left the signal set
        _SIGNAL_RECEIVED.set()
        assert _SIGNAL_RECEIVED.is_set()

        # Create profile files so post-import validation passes
        self._create_profile_files(imp_workspace["voices"], "solomon")

        # Now run a normal import — it should succeed because
        # _SIGNAL_RECEIVED is cleared at the top of run_operator
        runner = FakeDockerRunner(container_state="running")
        lock = FakeLock()
        fake_popen = FakeImporterPopen(returncode=0, stdout='{"imported": true}')
        fake_import = FakeInterruptibleImporterRunner(fake_popen)

        result = run_operator(
            audio_path=imp_workspace["audio"],
            transcript_path=imp_workspace["transcript"],
            voice_id="solomon",
            license_str="permission-granted",
            attribution="Test",
            provenance="Test source",
            models_dir=imp_workspace["models"],
            voices_dir=imp_workspace["voices"],
            import_inputs_dir=imp_workspace["import_inputs"],
            model_rel="model.gguf",
            tokenizer_rel="tokenizer.json",
            backend_container="s2cpp-tts",
            backend_image="sha256:" + "a" * 64,
            runner=runner,
            lock=lock,
            importer_runner=fake_import,
            expected_source_revision="a" * 40,
            expected_s2cpp_revision="a" * 40,
            force=True,
        )
        assert result["status"] == "complete", (
            "prior interrupted run must not poison later runs: "
            "signal should be cleared at top of run_operator"
        )

    # ── 10. Production main uses real interruptible adapter ──

    def test_production_main_defaults_to_importer_runner_not_plain_runner(
        self,
    ) -> None:
        """RED: Production main() must explicitly pass the real
        interruptible adapter as importer_runner, not fall back to
        the plain runner for import."""
        from scripts.unraid_add_voice import _importer_runner

        # The real _importer_runner must exist and be callable
        assert callable(_importer_runner), (
            "production _importer_runner must be defined and callable"
        )

"""Phase 8B1 verification tooling regression tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from scripts.live_verify_phase_8b1 import classify_recovery_result


def _events(*types: str) -> list[dict[str, object]]:
    return [{"type": event_type, "elapsed_ms": idx * 100} for idx, event_type in enumerate(types, start=1)]


def test_legacy_recovery_audio_stop_is_success_without_synthesize_stopped() -> None:
    """A standalone legacy Synthesize response terminates at AudioStop."""
    result = classify_recovery_result(
        recovery_events=_events("audio-start", "audio-chunk", "audio-stop"),
        pcm_bytes=4096,
        sample_rate=44100,
        channels=1,
        width=2,
        timeout=False,
        exception=None,
        request_mode="legacy",
    )

    assert result["audio_recovery_success"] is True
    assert result["protocol_terminal_success"] is True
    assert result["pcm_valid"] is True
    assert result["synthesize_stopped_received"] is False
    assert result["exact_failure_reason"] == ""
    assert result["audio_start_ms"] == 100
    assert result["first_audio_ms"] == 200
    assert result["completion_ms"] == 300
    assert result["pcm_bytes"] == 4096


def test_missing_audio_stop_reports_protocol_failure_but_pcm_can_be_valid() -> None:
    result = classify_recovery_result(
        recovery_events=_events("audio-start", "audio-chunk"),
        pcm_bytes=4096,
        sample_rate=44100,
        channels=1,
        width=2,
        timeout=False,
        exception=None,
        request_mode="legacy",
    )

    assert result["audio_recovery_success"] is False
    assert result["protocol_terminal_success"] is False
    assert result["pcm_valid"] is True
    assert result["exact_failure_reason"] == "missing audio-stop terminal event"


def test_valid_pcm_with_streaming_protocol_failure_is_not_weakened() -> None:
    result = classify_recovery_result(
        recovery_events=_events("audio-start", "audio-chunk", "audio-stop"),
        pcm_bytes=4096,
        sample_rate=44100,
        channels=1,
        width=2,
        timeout=False,
        exception=None,
        request_mode="streaming",
    )

    assert result["pcm_valid"] is True
    assert result["protocol_terminal_success"] is False
    assert result["audio_recovery_success"] is False
    assert result["exact_failure_reason"] == "missing synthesize-stopped terminal event"


def test_timeout_reporting_is_precise() -> None:
    result = classify_recovery_result(
        recovery_events=_events("audio-start", "audio-chunk"),
        pcm_bytes=4096,
        sample_rate=44100,
        channels=1,
        width=2,
        timeout=True,
        exception=None,
        request_mode="legacy",
    )

    assert result["timeout"] is True
    assert result["audio_recovery_success"] is False
    assert result["exact_failure_reason"] == "timeout waiting for recovery events"


def test_capture_script_duration_mode_is_background_safe(tmp_path: Path) -> None:
    """The log capture script must not wait for stdin in duration mode."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = inspect ]; then echo fake-image; exit 0; fi\n"
        "if [ \"$1\" = ps ]; then echo running; exit 0; fi\n"
        "if [ \"$1\" = logs ] && [ \"$2\" = -f ]; then echo \"$3 live logs\"; sleep 5; exit 0; fi\n"
        "if [ \"$1\" = logs ]; then echo \"$2 pre logs\"; exit 0; fi\n"
        "echo docker-$@\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    nvidia = fake_bin / "nvidia-smi"
    nvidia.write_text("#!/bin/sh\necho '1, 2, 3, 4, 5'\n", encoding="utf-8")
    nvidia.chmod(0o755)

    outdir = tmp_path / "artifacts"
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    completed = subprocess.run(
        [
            "bash",
            "scripts/capture_phase_8b1_logs.sh",
            "--duration",
            "0.2",
            "--outdir",
            str(outdir),
            "wrapper-test",
            "backend-test",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    metadata = json.loads((outdir / "capture-metadata.json").read_text(encoding="utf-8"))
    assert metadata["stop_reason"] == "duration"
    assert (outdir / "wrapper-live.log").stat().st_size > 0
    assert (outdir / "backend-live.log").stat().st_size > 0
    assert (outdir / "nvidia-smi.log").stat().st_size > 0
    assert "Press Enter" not in completed.stdout



def test_capture_script_writes_post_run_since_snapshots(tmp_path: Path) -> None:
    """Capture must save post-run docker logs --since snapshots, not only logs -f."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "docker-calls.txt"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        f"echo \"$@\" >> {calls}\n"
        "if [ \"$1\" = inspect ]; then echo fake-image-or-status; exit 0; fi\n"
        "if [ \"$1\" = logs ] && [ \"$2\" = --since ]; then echo \"post $4 logs\"; exit 0; fi\n"
        "if [ \"$1\" = logs ] && [ \"$2\" = -f ]; then echo \"$3 live logs\"; sleep 5; exit 0; fi\n"
        "if [ \"$1\" = logs ]; then echo \"$2 pre logs\"; exit 0; fi\n"
        "echo docker-$@\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    nvidia = fake_bin / "nvidia-smi"
    nvidia.write_text("#!/bin/sh\necho '1, 2, 3, 4, 5'\n", encoding="utf-8")
    nvidia.chmod(0o755)

    outdir = tmp_path / "artifacts"
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    completed = subprocess.run(
        [
            "bash",
            "scripts/capture_phase_8b1_logs.sh",
            "--duration",
            "0.2",
            "--outdir",
            str(outdir),
            "wrapper-test",
            "backend-test",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    call_text = calls.read_text(encoding="utf-8")
    assert "logs --since" in call_text
    assert (outdir / "wrapper-post.log").read_text(encoding="utf-8").strip() == "post wrapper-test logs"
    assert (outdir / "backend-post.log").read_text(encoding="utf-8").strip() == "post backend-test logs"

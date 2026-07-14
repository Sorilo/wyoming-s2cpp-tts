"""Offline local-audio to .s2voice importer tests."""

from __future__ import annotations

from pathlib import Path
import json
import os
import struct
import subprocess
import sys

import pytest


TEST_S2CPP_REVISION = "2c33261938da1a41d713768b1b391b4d368d7d2c"


@pytest.fixture(autouse=True)
def _set_runtime_s2cpp_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S2CPP_REVISION", TEST_S2CPP_REVISION)


def test_validate_voice_id_rejects_path_like_values() -> None:
    from app.voice_import import VoiceImportError, validate_voice_id

    for unsafe_id in ("../escape", "/absolute", "bad name", ".hidden", "", ".."):
        with pytest.raises(VoiceImportError, match="voice ID"):
            validate_voice_id(unsafe_id)


def test_build_ffmpeg_command_normalizes_to_mono_44_1khz_pcm(tmp_path: Path) -> None:
    from app.voice_import import build_ffmpeg_command

    source = tmp_path / "input file.flac"
    output = tmp_path / "normalized.wav"

    assert build_ffmpeg_command(source, output, ffmpeg_bin="ffmpeg") == [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-vn", "-ac", "1", "-ar", "44100",
        "-c:a", "pcm_s16le", str(output),
    ]


def test_build_s2_command_matches_pinned_voice_creation_contract(tmp_path: Path) -> None:
    from app.voice_import import build_s2_command

    model = tmp_path / "model.gguf"
    tokenizer = tmp_path / "tokenizer.json"
    prompt = tmp_path / "normalized.wav"
    voice_dir = tmp_path / "staging"
    preview = tmp_path / "discard.wav"

    command = build_s2_command(
        model_path=model,
        tokenizer_path=tokenizer,
        prompt_audio_path=prompt,
        transcript="The exact reference transcript.",
        voice_id="speaker-1",
        voice_dir=voice_dir,
        output_path=preview,
        s2_bin="s2",
        cuda_device=0,
        gpu_layers=-1,
    )

    assert command == [
        "s2", "--model", str(model), "--tokenizer", str(tokenizer),
        "--text", ".", "--prompt-audio", str(prompt),
        "--prompt-text", "The exact reference transcript.",
        "--voice", "speaker-1", "--save-voice",
        "--voice-dir", str(voice_dir), "--output", str(preview),
        "--cuda", "0", "--gpu-layers", "-1",
    ]


def _s2voice_bytes(transcript: str = "Reference transcript.") -> bytes:
    transcript_bytes = transcript.encode("utf-8") + b"\0"
    codes = struct.pack("<4i", 1, 2, 3, 4)
    return struct.pack(
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


def _request(tmp_path: Path, **overrides):
    from app.voice_import import ImportRequest

    source = tmp_path / "reference.flac"
    model = tmp_path / "model.gguf"
    tokenizer = tmp_path / "tokenizer.json"
    voice_dir = tmp_path / "voices"
    source.write_bytes(b"invented local audio")
    model.write_bytes(b"model")
    tokenizer.write_text("{}")
    voice_dir.mkdir(exist_ok=True)
    values = {
        "source_path": source,
        "transcript": "Reference transcript.",
        "voice_id": "speaker-1",
        "license": "CC-BY-4.0",
        "attribution": "Test Speaker",
        "provenance_source": "local recording",
        "model_path": model,
        "tokenizer_path": tokenizer,
        "voice_dir": voice_dir,
    }
    values.update(overrides)
    return ImportRequest(**values)


class _SuccessfulRunner:
    def __init__(self, *, corrupt_profile: bool = False) -> None:
        self.commands: list[list[str]] = []
        self.corrupt_profile = corrupt_profile

    def __call__(self, command: list[str], **kwargs):
        self.commands.append(command)
        if command[0].endswith("ffmpeg"):
            Path(command[-1]).write_bytes(b"normalized wav")
        else:
            voice_id = command[command.index("--voice") + 1]
            voice_dir = Path(command[command.index("--voice-dir") + 1])
            data = b"corrupt" if self.corrupt_profile else _s2voice_bytes()
            (voice_dir / f"{voice_id}.s2voice").write_bytes(data)
            Path(command[command.index("--output") + 1]).write_bytes(b"preview")
        return subprocess.CompletedProcess(command, 0, "", "")


def test_dry_run_executes_nothing_and_writes_nothing(tmp_path: Path) -> None:
    from app.voice_import import import_voice

    request = _request(tmp_path, dry_run=True)
    calls: list[list[str]] = []
    result = import_voice(request, runner=lambda command, **kwargs: calls.append(command))

    assert result.dry_run is True
    assert result.imported is False
    assert len(result.commands) == 2
    assert calls == []
    assert list(request.voice_dir.iterdir()) == []
    rendered = json.dumps(result.to_dict())
    assert request.transcript not in rendered
    assert "[REDACTED TRANSCRIPT]" in rendered


def test_import_rejects_symlink_source_before_execution(tmp_path: Path) -> None:
    from app.voice_import import VoiceImportError, import_voice

    request = _request(tmp_path)
    link = tmp_path / "reference-link.flac"
    os.symlink(request.source_path, link)
    request = _request(tmp_path, source_path=link)
    calls: list[list[str]] = []

    with pytest.raises(VoiceImportError, match="symlink"):
        import_voice(request, runner=lambda command, **kwargs: calls.append(command))
    assert calls == []


def test_import_rejects_collision_before_execution(tmp_path: Path) -> None:
    from app.voice_import import VoiceImportError, import_voice

    request = _request(tmp_path)
    (request.voice_dir / "speaker-1.s2voice").write_bytes(b"existing")
    calls: list[list[str]] = []

    with pytest.raises(VoiceImportError, match="exists"):
        import_voice(request, runner=lambda command, **kwargs: calls.append(command))
    assert calls == []


def test_active_s2_server_detection_uses_exact_process_shape(tmp_path: Path) -> None:
    from app.voice_import import has_active_s2_server

    proc = tmp_path / "proc"
    (proc / "101").mkdir(parents=True)
    (proc / "101" / "cmdline").write_bytes(
        b"/usr/local/bin/s2\0--model\0/models/model.gguf\0--server\0"
    )
    (proc / "102").mkdir()
    (proc / "102" / "cmdline").write_bytes(
        b"python3\0worker.py\0--server\0"
    )

    assert has_active_s2_server(proc) is True
    (proc / "101" / "cmdline").write_bytes(b"/usr/local/bin/s2\0--help\0")
    assert has_active_s2_server(proc) is False


def test_real_import_refuses_while_s2_server_is_active(tmp_path: Path) -> None:
    from app.voice_import import VoiceImportError, import_voice

    request = _request(tmp_path)
    calls: list[list[str]] = []

    with pytest.raises(VoiceImportError, match="active s2 --server"):
        import_voice(
            request,
            runner=lambda command, **kwargs: calls.append(command),
            server_check=lambda: True,
        )
    assert calls == []
    assert list(request.voice_dir.iterdir()) == []


def test_dry_run_remains_available_while_s2_server_is_active(tmp_path: Path) -> None:
    from app.voice_import import import_voice

    request = _request(tmp_path, dry_run=True)
    result = import_voice(request, server_check=lambda: True)

    assert result.dry_run is True
    assert result.imported is False
    assert list(request.voice_dir.iterdir()) == []


def test_success_validates_profile_writes_sidecar_and_cleans_temp(tmp_path: Path) -> None:
    from app.voice_import import import_voice
    from app.voice_profile import parse_s2voice
    from app.voice_schema import VOICE_SIDECAR_SCHEMA
    import jsonschema

    request = _request(tmp_path)
    runner = _SuccessfulRunner()
    result = import_voice(request, runner=runner, server_check=lambda: False)

    assert result.imported is True
    assert len(runner.commands) == 2
    profile_path = request.voice_dir / "speaker-1.s2voice"
    sidecar_path = request.voice_dir / "speaker-1.s2voice.json"
    profile = parse_s2voice(profile_path.read_bytes())
    assert profile.transcript == "Reference transcript."
    sidecar = json.loads(sidecar_path.read_text())
    jsonschema.validate(sidecar, json.loads(VOICE_SIDECAR_SCHEMA))
    assert sidecar["license"] == "CC-BY-4.0"
    assert sidecar["attribution"] == "Test Speaker"
    assert sidecar["provenance"]["source"] == "local recording"
    assert sidecar["provenance"]["s2cpp_revision"].startswith("2c332619")
    assert result.hash_sha256 == sidecar["hash_sha256"]
    assert not any(path.name.startswith(".voice-import-") for path in request.voice_dir.iterdir())
    assert not list(tmp_path.glob("**/normalized.wav"))
    assert not list(tmp_path.glob("**/discard.wav"))


def test_corrupt_generated_profile_leaves_no_managed_artifacts(tmp_path: Path) -> None:
    from app.voice_import import VoiceImportError, import_voice

    request = _request(tmp_path)
    with pytest.raises(VoiceImportError, match="generated profile"):
        import_voice(request, runner=_SuccessfulRunner(corrupt_profile=True))

    assert list(request.voice_dir.iterdir()) == []


def test_subprocess_failure_leaves_no_managed_artifacts(tmp_path: Path) -> None:
    from app.voice_import import VoiceImportError, import_voice

    request = _request(tmp_path)

    def failing_runner(command: list[str], **kwargs):
        raise subprocess.CalledProcessError(2, command, stderr="invented failure")

    with pytest.raises(VoiceImportError, match="failed"):
        import_voice(request, runner=failing_runner)
    assert list(request.voice_dir.iterdir()) == []


def test_subprocess_timeout_is_distinct_and_redacted(tmp_path: Path) -> None:
    from app.voice_import import VoiceImportError, import_voice

    request = _request(tmp_path, timeout_seconds=12.5)

    def timeout_runner(command: list[str], **kwargs):
        raise subprocess.TimeoutExpired(command, 12.5, stderr=request.transcript)

    with pytest.raises(VoiceImportError, match=r"ffmpeg timed out after 12\.5 seconds") as exc:
        import_voice(request, runner=timeout_runner)
    assert request.transcript not in str(exc.value)
    assert list(request.voice_dir.iterdir()) == []


def test_import_rejects_missing_runtime_revision_before_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.voice_import import VoiceImportError, import_voice

    request = _request(tmp_path, dry_run=True)
    monkeypatch.delenv("S2CPP_REVISION")

    with pytest.raises(VoiceImportError, match="S2CPP_REVISION"):
        import_voice(request)


def test_sidecar_records_runtime_s2cpp_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.voice_import import import_voice

    request = _request(tmp_path)
    runtime_revision = "a" * 40
    monkeypatch.setenv("S2CPP_REVISION", runtime_revision)

    result = import_voice(
        request,
        runner=_SuccessfulRunner(),
        server_check=lambda: False,
    )

    sidecar = json.loads(result.sidecar_path.read_text())
    assert sidecar["provenance"]["s2cpp_revision"] == runtime_revision


def test_no_overwrite_race_preserves_racing_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.voice_import as voice_import

    request = _request(tmp_path)
    profile_path = request.voice_dir / "speaker-1.s2voice"
    sidecar_path = request.voice_dir / "speaker-1.s2voice.json"
    racing_profile = b"racing writer profile"
    real_link = voice_import.os.link

    def inject_racer(source, destination):
        destination_path = Path(destination)
        if destination_path == profile_path and not destination_path.exists():
            destination_path.write_bytes(racing_profile)
        return real_link(source, destination)

    monkeypatch.setattr(voice_import.os, "link", inject_racer)

    with pytest.raises(FileExistsError):
        voice_import.import_voice(
            request,
            runner=_SuccessfulRunner(),
            server_check=lambda: False,
        )

    assert profile_path.read_bytes() == racing_profile
    assert not sidecar_path.exists()
    assert sorted(path.name for path in request.voice_dir.iterdir()) == [
        "speaker-1.s2voice"
    ]


def test_failed_forced_overwrite_restores_existing_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.voice_import as voice_import

    request = _request(tmp_path, overwrite=True)
    profile_path = request.voice_dir / "speaker-1.s2voice"
    sidecar_path = request.voice_dir / "speaker-1.s2voice.json"
    old_profile = b"existing profile bytes"
    old_sidecar = b'{"existing": true}\n'
    profile_path.write_bytes(old_profile)
    sidecar_path.write_bytes(old_sidecar)
    real_replace = voice_import.os.replace

    def fail_final_profile(source, destination):
        source_path = Path(source)
        if (
            source_path.name == "speaker-1.s2voice"
            and source_path.parent != request.voice_dir
        ):
            raise OSError("injected final profile placement failure")
        return real_replace(source, destination)

    monkeypatch.setattr(voice_import.os, "replace", fail_final_profile)

    with pytest.raises(OSError, match="injected"):
        voice_import.import_voice(
            request,
            runner=_SuccessfulRunner(),
            server_check=lambda: False,
        )

    assert profile_path.read_bytes() == old_profile
    assert sidecar_path.read_bytes() == old_sidecar
    assert sorted(path.name for path in request.voice_dir.iterdir()) == [
        "speaker-1.s2voice",
        "speaker-1.s2voice.json",
    ]


def test_validation_wav_is_optionally_preserved_inside_voice_dir(tmp_path: Path) -> None:
    from app.voice_import import import_voice

    voice_dir = tmp_path / "voices"
    validation_wav = voice_dir / "validation" / "speaker-1.wav"
    validation_wav.parent.mkdir(parents=True)
    request = _request(tmp_path, validation_wav_path=validation_wav)

    result = import_voice(
        request,
        runner=_SuccessfulRunner(),
        server_check=lambda: False,
    )

    assert result.validation_wav_path == validation_wav
    assert validation_wav.read_bytes() == b"preview"
    assert not any(path.name.startswith(".voice-import-") for path in voice_dir.iterdir())


def test_validation_wav_outside_voice_dir_is_rejected_before_execution(
    tmp_path: Path,
) -> None:
    from app.voice_import import VoiceImportError, import_voice

    calls: list[list[str]] = []
    request = _request(
        tmp_path,
        validation_wav_path=tmp_path / "outside" / "speaker-1.wav",
    )

    with pytest.raises(VoiceImportError, match="inside the voice directory"):
        import_voice(
            request,
            runner=lambda command, **kwargs: calls.append(command),
            server_check=lambda: False,
        )
    assert calls == []


def test_validation_wav_rejects_nested_symlink_before_execution(tmp_path: Path) -> None:
    from app.voice_import import VoiceImportError, import_voice

    voice_dir = tmp_path / "voices"
    real_dir = voice_dir / "real"
    real_dir.mkdir(parents=True)
    linked_dir = voice_dir / "linked"
    os.symlink(real_dir, linked_dir)
    request = _request(
        tmp_path,
        validation_wav_path=linked_dir / "speaker-1.wav",
    )
    calls: list[list[str]] = []

    with pytest.raises(VoiceImportError, match="symlink"):
        import_voice(
            request,
            runner=lambda command, **kwargs: calls.append(command),
            server_check=lambda: False,
        )
    assert calls == []


def test_operator_cli_dry_run_from_transcript_file_is_json_and_redacted(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    transcript_file = tmp_path / "transcript.txt"
    transcript_file.write_text(request.transcript, encoding="utf-8")
    validation_dir = request.voice_dir / "validation"
    validation_dir.mkdir()
    validation_wav = validation_dir / "speaker-1.wav"
    script = Path(__file__).resolve().parents[1] / "scripts" / "import_voice.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            str(request.source_path),
            "--transcript-file",
            str(transcript_file),
            "--id",
            request.voice_id,
            "--license",
            request.license,
            "--attribution",
            request.attribution,
            "--provenance-source",
            request.provenance_source,
            "--model",
            str(request.model_path),
            "--tokenizer",
            str(request.tokenizer_path),
            "--voice-dir",
            str(request.voice_dir),
            "--validation-wav",
            str(validation_wav),
            "--dry-run",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "S2CPP_REVISION": "a" * 40},
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["dry_run"] is True
    assert payload["imported"] is False
    assert payload["s2cpp_revision"] == "a" * 40
    assert payload["validation_wav_path"] == str(validation_wav)
    assert request.transcript not in completed.stdout
    assert "[REDACTED TRANSCRIPT]" in completed.stdout
    assert completed.stderr == ""


@pytest.mark.parametrize("runtime_revision", [None, "latest", "A" * 40])
def test_operator_cli_rejects_missing_or_invalid_runtime_revision(
    tmp_path: Path,
    runtime_revision: str | None,
) -> None:
    request = _request(tmp_path)
    script = Path(__file__).resolve().parents[1] / "scripts" / "import_voice.py"
    env = os.environ.copy()
    if runtime_revision is None:
        env.pop("S2CPP_REVISION", None)
    else:
        env["S2CPP_REVISION"] = runtime_revision

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            str(request.source_path),
            "--transcript",
            request.transcript,
            "--id",
            request.voice_id,
            "--license",
            request.license,
            "--attribution",
            request.attribution,
            "--provenance-source",
            request.provenance_source,
            "--model",
            str(request.model_path),
            "--tokenizer",
            str(request.tokenizer_path),
            "--voice-dir",
            str(request.voice_dir),
            "--dry-run",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 2
    payload = json.loads(completed.stderr)
    assert payload["imported"] is False
    assert "S2CPP_REVISION" in payload["error"]
    assert request.transcript not in completed.stderr
    assert "Traceback" not in completed.stderr


def test_operator_cli_error_is_json_without_traceback_or_transcript(tmp_path: Path) -> None:
    request = _request(tmp_path)
    script = Path(__file__).resolve().parents[1] / "scripts" / "import_voice.py"
    secret_transcript = "Do not echo this exact transcript."

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            str(tmp_path / "missing.wav"),
            "--transcript",
            secret_transcript,
            "--id",
            request.voice_id,
            "--license",
            request.license,
            "--attribution",
            request.attribution,
            "--provenance-source",
            request.provenance_source,
            "--model",
            str(request.model_path),
            "--tokenizer",
            str(request.tokenizer_path),
            "--voice-dir",
            str(request.voice_dir),
            "--dry-run",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "S2CPP_REVISION": "a" * 40},
    )

    assert completed.returncode == 2
    payload = json.loads(completed.stderr)
    assert payload["imported"] is False
    assert "regular file" in payload["error"]
    assert "Traceback" not in completed.stderr
    assert secret_transcript not in completed.stderr

"""Secure, offline import of local reference audio into ``.s2voice`` profiles."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import jsonschema

from .voice_profile import VoiceProfileError, compute_voice_hash, parse_s2voice
from .voice_schema import VOICE_SIDECAR_SCHEMA


_VOICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_S2CPP_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_COMMON_AUDIO_SUFFIXES = {
    ".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".webm",
}
_MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_TRANSCRIPT_BYTES = 1024 * 1024


class VoiceImportError(ValueError):
    """Raised when a voice import request is invalid or cannot be completed."""


def validate_voice_id(voice_id: str) -> str:
    """Return a safe profile ID or raise before any filesystem access."""
    if not isinstance(voice_id, str) or not _VOICE_ID_RE.fullmatch(voice_id):
        raise VoiceImportError(
            "Invalid voice ID: use 1-128 ASCII letters, digits, underscore, "
            "or hyphen; the first character must be alphanumeric"
        )
    return voice_id


def resolve_s2cpp_revision() -> str:
    """Return validated build provenance for results and managed sidecars."""
    revision = os.getenv("S2CPP_REVISION")
    if revision is None or not _S2CPP_REVISION_RE.fullmatch(revision):
        raise VoiceImportError(
            "S2CPP_REVISION must be set to the 40-character lowercase "
            "hexadecimal build revision"
        )
    return revision


def build_ffmpeg_command(
    source_path: Path,
    output_path: Path,
    *,
    ffmpeg_bin: str = "ffmpeg",
) -> list[str]:
    """Build a non-shell FFmpeg command for s2.cpp reference audio."""
    return [
        ffmpeg_bin,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "44100",
        "-c:a",
        "pcm_s16le",
        str(output_path),
    ]


def build_s2_command(
    *,
    model_path: Path,
    tokenizer_path: Path,
    prompt_audio_path: Path,
    transcript: str,
    voice_id: str,
    voice_dir: Path,
    output_path: Path,
    s2_bin: str = "s2",
    cuda_device: int = 0,
    gpu_layers: int = -1,
) -> list[str]:
    """Build the pinned s2.cpp ``--save-voice`` command contract."""
    validate_voice_id(voice_id)
    return [
        s2_bin,
        "--model",
        str(model_path),
        "--tokenizer",
        str(tokenizer_path),
        "--text",
        ".",
        "--prompt-audio",
        str(prompt_audio_path),
        "--prompt-text",
        transcript,
        "--voice",
        voice_id,
        "--save-voice",
        "--voice-dir",
        str(voice_dir),
        "--output",
        str(output_path),
        "--cuda",
        str(cuda_device),
        "--gpu-layers",
        str(gpu_layers),
    ]


@dataclass(frozen=True)
class ImportRequest:
    """Inputs for one local, offline voice-profile import transaction."""

    source_path: Path
    transcript: str
    voice_id: str
    license: str
    attribution: str
    provenance_source: str
    model_path: Path
    tokenizer_path: Path
    voice_dir: Path
    validation_wav_path: Path | None = None
    dry_run: bool = False
    overwrite: bool = False
    ffmpeg_bin: str = "ffmpeg"
    s2_bin: str = "s2"
    cuda_device: int = 0
    gpu_layers: int = -1
    timeout_seconds: float = 600.0


@dataclass(frozen=True)
class ImportResult:
    """Auditable result without retaining source or normalized audio."""

    imported: bool
    dry_run: bool
    voice_id: str
    profile_path: Path
    sidecar_path: Path
    commands: tuple[tuple[str, ...], ...]
    s2cpp_revision: str
    hash_sha256: str | None = None
    validation_wav_path: Path | None = None

    def to_dict(self) -> dict[str, object]:
        rendered_commands: list[list[str]] = []
        for stored_command in self.commands:
            command = list(stored_command)
            if "--prompt-text" in command:
                index = command.index("--prompt-text") + 1
                if index < len(command):
                    command[index] = "[REDACTED TRANSCRIPT]"
            rendered_commands.append(command)
        return {
            "imported": self.imported,
            "dry_run": self.dry_run,
            "voice_id": self.voice_id,
            "profile_path": str(self.profile_path),
            "sidecar_path": str(self.sidecar_path),
            "commands": rendered_commands,
            "s2cpp_revision": self.s2cpp_revision,
            "hash_sha256": self.hash_sha256,
            "validation_wav_path": (
                str(self.validation_wav_path) if self.validation_wav_path else None
            ),
        }


Runner = Callable[..., subprocess.CompletedProcess[str]]
ServerCheck = Callable[[], bool]


def has_active_s2_server(proc_root: Path = Path("/proc")) -> bool:
    """Return whether an exact ``s2 ... --server`` process is currently visible."""
    try:
        processes = proc_root.iterdir()
    except OSError:
        return False
    for process in processes:
        if not process.name.isdigit():
            continue
        try:
            arguments = (process / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if not arguments or not arguments[0]:
            continue
        executable = Path(os.fsdecode(arguments[0])).name
        decoded_arguments = {os.fsdecode(argument) for argument in arguments[1:] if argument}
        if executable == "s2" and "--server" in decoded_arguments:
            return True
    return False


def _require_regular_file(path: Path, label: str) -> None:
    if path.is_symlink():
        raise VoiceImportError(f"{label} must not be a symlink: {path}")
    if not path.is_file():
        raise VoiceImportError(f"{label} is not a regular file: {path}")


def _validate_request(request: ImportRequest) -> tuple[Path, Path, Path | None]:
    validate_voice_id(request.voice_id)
    if not isinstance(request.transcript, str) or not request.transcript.strip():
        raise VoiceImportError("Reference transcript must not be empty")
    transcript_size = len(request.transcript.encode("utf-8"))
    if transcript_size > MAX_TRANSCRIPT_BYTES or "\0" in request.transcript:
        raise VoiceImportError("Reference transcript is invalid or exceeds 1 MiB")
    for label, value in (
        ("license", request.license),
        ("attribution", request.attribution),
        ("provenance source", request.provenance_source),
    ):
        if not isinstance(value, str) or not value.strip():
            raise VoiceImportError(f"{label} must not be empty")

    source = Path(request.source_path)
    model = Path(request.model_path)
    tokenizer = Path(request.tokenizer_path)
    voice_dir = Path(request.voice_dir)
    _require_regular_file(source, "Source audio")
    _require_regular_file(model, "GGUF model")
    _require_regular_file(tokenizer, "Tokenizer")
    if source.suffix.lower() not in _COMMON_AUDIO_SUFFIXES:
        raise VoiceImportError(
            f"Unsupported source-audio extension {source.suffix!r}; "
            "use a common local audio format"
        )
    if source.stat().st_size > _MAX_SOURCE_BYTES:
        raise VoiceImportError("Source audio exceeds the 512 MiB safety limit")
    if voice_dir.is_symlink():
        raise VoiceImportError(f"Voice directory must not be a symlink: {voice_dir}")
    if not voice_dir.is_dir():
        raise VoiceImportError(f"Voice directory does not exist: {voice_dir}")

    profile_path = voice_dir / f"{request.voice_id}.s2voice"
    sidecar_path = voice_dir / f"{request.voice_id}.s2voice.json"
    for path in (profile_path, sidecar_path):
        if path.is_symlink():
            raise VoiceImportError(f"Destination must not be a symlink: {path}")
        if path.exists() and not request.overwrite:
            raise VoiceImportError(f"Destination already exists: {path}")

    validation_wav_path: Path | None = None
    if request.validation_wav_path is not None:
        validation_wav_path = Path(request.validation_wav_path)
        if validation_wav_path.suffix.lower() != ".wav":
            raise VoiceImportError("Validation WAV destination must use a .wav suffix")
        voice_root = voice_dir.resolve(strict=True)
        lexical_target = Path(os.path.abspath(validation_wav_path))
        lexical_parent = lexical_target.parent
        try:
            relative_lexical_parent = lexical_parent.relative_to(voice_root)
        except ValueError as exc:
            raise VoiceImportError(
                "Validation WAV destination must be inside the voice directory"
            ) from exc
        current = voice_root
        for part in relative_lexical_parent.parts:
            current /= part
            if current.is_symlink():
                raise VoiceImportError(
                    f"Validation WAV path must not traverse symlinks: {current}"
                )
        try:
            validation_parent = lexical_parent.resolve(strict=True)
            validation_parent.relative_to(voice_root)
        except (FileNotFoundError, ValueError) as exc:
            raise VoiceImportError(
                "Validation WAV destination must be inside the voice directory"
            ) from exc
        validation_wav_path = lexical_target
        if validation_wav_path.is_symlink():
            raise VoiceImportError(
                f"Validation WAV destination must not be a symlink: {validation_wav_path}"
            )
        if validation_parent.stat().st_dev != voice_root.stat().st_dev:
            raise VoiceImportError(
                "Validation WAV destination must use the voice-directory filesystem"
            )
        if validation_wav_path.exists() and not request.overwrite:
            raise VoiceImportError(
                f"Destination already exists: {validation_wav_path}"
            )

    if request.timeout_seconds <= 0:
        raise VoiceImportError("timeout_seconds must be positive")
    return profile_path, sidecar_path, validation_wav_path


def _commands_for(
    request: ImportRequest,
    staging_dir: Path,
) -> tuple[list[str], list[str]]:
    normalized = staging_dir / "normalized.wav"
    preview = staging_dir / "discard.wav"
    return (
        build_ffmpeg_command(
            Path(request.source_path), normalized, ffmpeg_bin=request.ffmpeg_bin
        ),
        build_s2_command(
            model_path=Path(request.model_path),
            tokenizer_path=Path(request.tokenizer_path),
            prompt_audio_path=normalized,
            transcript=request.transcript,
            voice_id=request.voice_id,
            voice_dir=staging_dir,
            output_path=preview,
            s2_bin=request.s2_bin,
            cuda_device=request.cuda_device,
            gpu_layers=request.gpu_layers,
        ),
    )


def _run(command: list[str], request: ImportRequest, runner: Runner) -> None:
    executable = Path(command[0]).name if command else "command"
    try:
        runner(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=request.timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise VoiceImportError(
            f"Voice import command failed: {executable} timed out after "
            f"{request.timeout_seconds:g} seconds"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise VoiceImportError(
            f"Voice import command failed: {executable} (exit {exc.returncode})"
        ) from exc
    except OSError as exc:
        raise VoiceImportError(
            f"Voice import command could not start: {executable}"
        ) from exc
    except subprocess.SubprocessError as exc:
        raise VoiceImportError(
            f"Voice import command failed: {executable}"
        ) from exc


def _publish_file(staged: Path, destination: Path, *, overwrite: bool) -> None:
    """Atomically publish one same-filesystem file, respecting no-overwrite."""
    if overwrite:
        os.replace(staged, destination)
        return
    os.link(staged, destination)
    staged.unlink()


def _build_sidecar(
    request: ImportRequest,
    digest: str,
    s2cpp_revision: str,
) -> dict[str, object]:
    sidecar: dict[str, object] = {
        "id": request.voice_id,
        "license": request.license.strip(),
        "attribution": request.attribution.strip(),
        "provenance": {
            "source": request.provenance_source.strip(),
            "tool": "wyoming-s2cpp-tts offline voice importer",
            "s2cpp_revision": s2cpp_revision,
        },
        "hash_sha256": digest,
    }
    try:
        jsonschema.validate(sidecar, json.loads(VOICE_SIDECAR_SCHEMA))
    except jsonschema.ValidationError as exc:
        raise VoiceImportError(f"Generated sidecar is invalid: {exc.message}") from exc
    return sidecar


def import_voice(
    request: ImportRequest,
    *,
    runner: Runner = subprocess.run,
    server_check: ServerCheck = has_active_s2_server,
) -> ImportResult:
    """Create, validate, and commit one profile without retaining audio."""
    profile_path, sidecar_path, validation_wav_path = _validate_request(request)
    s2cpp_revision = resolve_s2cpp_revision()
    dry_staging = Path(request.voice_dir) / f".voice-import-{request.voice_id}-dry-run"
    dry_commands = _commands_for(request, dry_staging)
    if request.dry_run:
        return ImportResult(
            imported=False,
            dry_run=True,
            voice_id=request.voice_id,
            profile_path=profile_path,
            sidecar_path=sidecar_path,
            commands=tuple(tuple(command) for command in dry_commands),
            s2cpp_revision=s2cpp_revision,
            validation_wav_path=validation_wav_path,
        )

    if server_check():
        raise VoiceImportError(
            "Refusing real voice import while an active s2 --server process is running; "
            "stop it manually or use --dry-run"
        )

    with tempfile.TemporaryDirectory(
        prefix=f".voice-import-{request.voice_id}-", dir=request.voice_dir
    ) as temporary:
        staging_dir = Path(temporary)
        ffmpeg_command, s2_command = _commands_for(request, staging_dir)
        _run(ffmpeg_command, request, runner)
        normalized = staging_dir / "normalized.wav"
        _require_regular_file(normalized, "Normalized reference audio")
        if normalized.stat().st_size == 0:
            raise VoiceImportError("Normalized reference audio is empty")

        _run(s2_command, request, runner)
        staged_profile = staging_dir / f"{request.voice_id}.s2voice"
        _require_regular_file(staged_profile, "Generated profile")
        profile_data = staged_profile.read_bytes()
        try:
            parsed = parse_s2voice(profile_data)
        except VoiceProfileError as exc:
            raise VoiceImportError(f"Invalid generated profile: {exc}") from exc
        if parsed.transcript != request.transcript:
            raise VoiceImportError("Invalid generated profile: transcript mismatch")

        digest = compute_voice_hash(profile_data)
        sidecar = _build_sidecar(request, digest, s2cpp_revision)
        staged_sidecar = staging_dir / f"{request.voice_id}.s2voice.json"
        staged_sidecar.write_text(
            json.dumps(sidecar, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        with staged_sidecar.open("rb") as handle:
            os.fsync(handle.fileno())
        with staged_profile.open("rb") as handle:
            os.fsync(handle.fileno())

        # Commit optional validation audio and metadata first, then the validated
        # binary last. The profile is the managed commit point. Roll back earlier
        # placements if the final atomic replace fails.
        preview = staging_dir / "discard.wav"
        previous_validation = staging_dir / ".previous-validation.wav"
        had_previous_validation = bool(
            validation_wav_path and validation_wav_path.exists()
        )
        validation_published = False
        if validation_wav_path is not None:
            _require_regular_file(preview, "Generated validation WAV")
            if preview.stat().st_size == 0:
                raise VoiceImportError("Generated validation WAV is empty")
            if had_previous_validation:
                shutil.copy2(validation_wav_path, previous_validation)
                with previous_validation.open("rb") as handle:
                    os.fsync(handle.fileno())
            _publish_file(
                preview, validation_wav_path, overwrite=request.overwrite
            )
            validation_published = True

        previous_sidecar = staging_dir / ".previous-sidecar"
        had_previous_sidecar = sidecar_path.exists()
        if had_previous_sidecar:
            shutil.copy2(sidecar_path, previous_sidecar)
            with previous_sidecar.open("rb") as handle:
                os.fsync(handle.fileno())
        try:
            _publish_file(
                staged_sidecar, sidecar_path, overwrite=request.overwrite
            )
            try:
                _publish_file(
                    staged_profile, profile_path, overwrite=request.overwrite
                )
            except BaseException:
                if had_previous_sidecar:
                    os.replace(previous_sidecar, sidecar_path)
                else:
                    sidecar_path.unlink(missing_ok=True)
                raise
        except BaseException:
            if validation_published and validation_wav_path is not None:
                if had_previous_validation:
                    os.replace(previous_validation, validation_wav_path)
                else:
                    validation_wav_path.unlink(missing_ok=True)
            raise

    return ImportResult(
        imported=True,
        dry_run=False,
        voice_id=request.voice_id,
        profile_path=profile_path,
        sidecar_path=sidecar_path,
        commands=(tuple(ffmpeg_command), tuple(s2_command)),
        s2cpp_revision=s2cpp_revision,
        hash_sha256=digest,
        validation_wav_path=validation_wav_path,
    )

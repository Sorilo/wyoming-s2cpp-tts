#!/usr/bin/env python3
"""Create a managed .s2voice profile from local reference audio, offline."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import NoReturn


for _candidate in (
    Path(__file__).resolve().parent.parent,
    Path("/usr/local/lib/wyoming-s2cpp-tts"),
):
    if (_candidate / "app").is_dir():
        sys.path.insert(0, str(_candidate))
        break

from app.voice_import import (
    MAX_TRANSCRIPT_BYTES,
    ImportRequest,
    VoiceImportError,
    import_voice,
)


class JsonArgumentParser(argparse.ArgumentParser):
    """Emit parse errors as one JSON object on stderr."""

    def error(self, message: str) -> NoReturn:
        json.dump({"imported": False, "error": message}, sys.stderr)
        sys.stderr.write("\n")
        raise SystemExit(2)


def _build_parser() -> argparse.ArgumentParser:
    model_default = os.getenv("S2_MODEL", "/models/s2-pro-q6_k.gguf")
    parser = JsonArgumentParser(
        prog="import-s2voice",
        description=(
            "Offline importer: normalize local audio, invoke pinned s2.cpp, "
            "validate the profile, and atomically commit profile metadata."
        ),
    )
    parser.add_argument("source", help="Local WAV/FLAC/MP3/M4A/OGG/Opus/WebM/AAC file")
    transcript = parser.add_mutually_exclusive_group(required=True)
    transcript.add_argument("--transcript", help="Exact words spoken in the reference audio")
    transcript.add_argument(
        "--transcript-file", help="UTF-8 file containing the exact transcript"
    )
    parser.add_argument("--id", required=True, dest="voice_id", help="Safe profile ID")
    parser.add_argument("--license", required=True, help="SPDX ID or explicit license name")
    parser.add_argument("--attribution", required=True, help="Required speaker/source credit")
    parser.add_argument(
        "--provenance-source", required=True, help="Local recording or dataset provenance"
    )
    parser.add_argument("--model", default=model_default, help="GGUF model path")
    parser.add_argument(
        "--tokenizer",
        default=os.getenv("S2_TOKENIZER"),
        help="tokenizer.json path (default: beside --model)",
    )
    parser.add_argument(
        "--voice-dir",
        default=os.getenv("S2_VOICE_DIR", "/voices"),
        help="Managed destination directory",
    )
    parser.add_argument(
        "--validation-wav",
        help="Optionally retain the generated validation WAV beneath --voice-dir",
    )
    parser.add_argument("--ffmpeg-bin", default="ffmpeg")
    parser.add_argument("--s2-bin", default=os.getenv("S2_BIN", "/usr/local/bin/s2"))
    parser.add_argument(
        "--cuda-device", type=int, default=int(os.getenv("S2_CUDA_DEVICE", "0"))
    )
    parser.add_argument(
        "--gpu-layers", type=int, default=int(os.getenv("S2_GPU_LAYERS", "-1"))
    )
    parser.add_argument("--timeout", type=float, default=600.0, dest="timeout_seconds")
    parser.add_argument("--force", action="store_true", dest="overwrite")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and print redacted commands without execution or writes",
    )
    return parser


def _read_transcript(args: argparse.Namespace) -> str:
    if args.transcript is not None:
        return args.transcript
    path = Path(args.transcript_file)
    if path.is_symlink():
        raise VoiceImportError(f"Transcript file must not be a symlink: {path}")
    if not path.is_file():
        raise VoiceImportError(f"Transcript file is not a regular file: {path}")
    if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
        raise VoiceImportError("Transcript file exceeds 1 MiB")
    try:
        return path.read_text(encoding="utf-8").rstrip("\r\n")
    except UnicodeError as exc:
        raise VoiceImportError("Transcript file must be valid UTF-8") from exc


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    model_path = Path(args.model)
    tokenizer_path = (
        Path(args.tokenizer) if args.tokenizer else model_path.parent / "tokenizer.json"
    )
    try:
        request = ImportRequest(
            source_path=Path(args.source),
            transcript=_read_transcript(args),
            voice_id=args.voice_id,
            license=args.license,
            attribution=args.attribution,
            provenance_source=args.provenance_source,
            model_path=model_path,
            tokenizer_path=tokenizer_path,
            voice_dir=Path(args.voice_dir),
            validation_wav_path=(
                Path(args.validation_wav) if args.validation_wav else None
            ),
            dry_run=args.dry_run,
            overwrite=args.overwrite,
            ffmpeg_bin=args.ffmpeg_bin,
            s2_bin=args.s2_bin,
            cuda_device=args.cuda_device,
            gpu_layers=args.gpu_layers,
            timeout_seconds=args.timeout_seconds,
        )
        result = import_voice(request)
    except (OSError, VoiceImportError) as exc:
        json.dump({"imported": False, "error": str(exc)}, sys.stderr)
        sys.stderr.write("\n")
        return 2

    json.dump(result.to_dict(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

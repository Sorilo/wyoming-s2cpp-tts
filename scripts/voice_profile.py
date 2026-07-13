#!/usr/bin/env python3
"""CLI entrypoint for voice profile management: validate, import, audit, licenses.

Usage:
    python scripts/voice_profile.py validate <path>
    python scripts/voice_profile.py import <source> <dest-dir> <voice-id> [--force]
    python scripts/voice_profile.py audit <voice-dir>
    python scripts/voice_profile.py licenses <voice-dir>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure app package is importable
_here = Path(__file__).resolve().parent.parent
if str(_here) not in sys.path:
    sys.path.insert(0, str(_here))

from app.voice_cli import cmd_validate, cmd_import, cmd_audit, cmd_licenses


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voice_profile",
        description="Manage .s2voice voice profiles — validate, import, audit, licenses.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # validate
    v = sub.add_parser("validate", help="Validate a .s2voice file and its sidecar")
    v.add_argument("path", help="Path to .s2voice file")

    # import
    i = sub.add_parser("import", help="Import a .s2voice into a managed directory")
    i.add_argument("source", help="Path to source .s2voice file")
    i.add_argument("dest_dir", help="Target directory")
    i.add_argument("voice_id", help="Profile ID for the imported voice")
    i.add_argument("--force", action="store_true", help="Overwrite existing")

    # audit
    a = sub.add_parser("audit", help="Audit all profiles in a directory")
    a.add_argument("voice_dir", help="Path to voice directory")

    # licenses
    lic = sub.add_parser("licenses", help="Summarise licenses across profiles")
    lic.add_argument("voice_dir", help="Path to voice directory")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "validate":
        result = cmd_validate(args.path)
    elif args.command == "import":
        result = cmd_import(args.source, args.dest_dir, args.voice_id, force=args.force)
    elif args.command == "audit":
        result = cmd_audit(args.voice_dir)
    elif args.command == "licenses":
        result = cmd_licenses(args.voice_dir)
    else:
        parser.print_help()
        return 1

    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if result.get("valid", result.get("imported", True)) else 1


if __name__ == "__main__":
    raise SystemExit(main())

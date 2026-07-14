#!/usr/bin/env python3
"""Safe installer for unraid_add_voice operator files.

Copies only the minimal required files to a target directory:
  - unraid_add_voice.py  (0755)
  - add-s2voice           (0755)
  - config.env.example    (0644, from unraid_add_voice_config.env.example)

Refuses:
  - Symlink target directories
  - Overwriting existing config.env.example unless --force
  - Files in the target that are symlinks

Usage:
  python3 install_unraid_voice_operator.py /path/to/operator
  python3 install_unraid_voice_operator.py /path/to/operator --force
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="install_unraid_voice_operator",
        description="Install unraid_add_voice operator files to target directory",
    )
    parser.add_argument(
        "target",
        help="Target directory for operator files",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing config.env.example",
    )
    args = parser.parse_args()

    target = Path(args.target)
    script_dir = Path(__file__).resolve().parent

    # Required source files
    sources = {
        "unraid_add_voice.py": (script_dir / "unraid_add_voice.py", 0o755),
        "add-s2voice": (script_dir / "add-s2voice", 0o755),
        "config.env.example": (script_dir / "unraid_add_voice_config.env.example", 0o644),
    }

    # Validate sources exist
    for name, (src, _) in sources.items():
        if not src.exists():
            print(f"ERROR: Source file not found: {src}", file=sys.stderr)
            return 1
        if src.is_symlink():
            print(f"ERROR: Source file is a symlink: {src}", file=sys.stderr)
            return 1
        if not src.is_file():
            print(f"ERROR: Source is not a regular file: {src}", file=sys.stderr)
            return 1

    # Validate target
    if target.exists():
        if target.is_symlink():
            print(f"ERROR: Target is a symlink: {target}", file=sys.stderr)
            return 1
        if not target.is_dir():
            print(f"ERROR: Target exists but is not a directory: {target}", file=sys.stderr)
            return 1

    # Create target directory
    target.mkdir(parents=True, exist_ok=True)

    # Refuse regular and dangling destination symlinks before copying anything.
    for name in sources:
        dest = target / name
        if dest.is_symlink():
            print(f"ERROR: Destination is a symlink: {dest}", file=sys.stderr)
            return 1

    # Check for existing config overwrite
    config_dest = target / "config.env.example"
    if config_dest.exists() and not args.force:
        print(
            f"ERROR: {config_dest} already exists. Use --force to overwrite.",
            file=sys.stderr,
        )
        return 1

    # Stage every file before publishing any destination.  This keeps an
    # existing installation unchanged if a source copy or chmod fails.
    staged: dict[str, Path] = {}
    for name, (src, mode) in sources.items():
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=target,
                prefix=f".{name}.",
                suffix=".tmp",
            )
            os.close(fd)
            staged_path = Path(tmp_name)
            staged[name] = staged_path
            shutil.copy2(src, staged_path)
            staged_path.chmod(mode)
        except OSError as exc:
            for staged_path in staged.values():
                try:
                    staged_path.unlink(missing_ok=True)
                except OSError:
                    pass
            print(f"ERROR: Failed to stage {name}: {exc}", file=sys.stderr)
            return 1

    # All copies are complete; publish each staged file atomically. os.replace
    # replaces a destination symlink itself rather than following its target.
    for name, (_, mode) in sources.items():
        dest = target / name
        try:
            os.replace(staged[name], dest)
            print(f"  Installed: {dest} ({oct(mode)})")
        except OSError as exc:
            for staged_path in staged.values():
                try:
                    staged_path.unlink(missing_ok=True)
                except OSError:
                    pass
            print(f"ERROR: Failed to publish {name}: {exc}", file=sys.stderr)
            return 1

    print(f"\nOperator files installed to: {target}")
    print("Next steps:")
    print(f"  1. Review and edit {target}/config.env.example")
    print(f"  2. Copy config.env.example to config.env and set your values")
    print(f"  3. Make the launcher available: ln -s {target}/add-s2voice /usr/local/bin/add-s2voice")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

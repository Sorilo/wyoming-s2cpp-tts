"""Voice discovery: scan a directory for saved ``.s2voice`` profile files.

This module is the single authority for deriving public voice IDs from
filenames and for rejecting unsafe/stale profile names before they are
exposed through Wyoming Describe or forwarded in synthesis requests.
"""

from __future__ import annotations

import os
import re
from typing import List


_VOICE_SUFFIX = ".s2voice"

# Profiles must match this naming convention (letters, digits, underscores,
# hyphens only) and must not be empty.  This is stricter than what the
# filesystem allows to keep downstream clients safe.
_VALID_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _sanitize_voice_id(filename: str) -> str | None:
    """Return a safe voice ID from a basename, or *None* if it is rejected.

    Accepts only regular files whose names end exactly with ``.s2voice``
    and whose ID portion (the basename without the suffix) matches the
    repository's profile naming convention.

    Rejected:
      - hidden files (``.`` prefix)
      - empty IDs
      - names containing path separators, ``..``, control characters
      - unexpected suffixes or unsupported characters
    """
    if not filename.endswith(_VOICE_SUFFIX):
        return None

    candidate = filename[: -len(_VOICE_SUFFIX)]
    if not candidate:
        return None
    if candidate.startswith("."):
        return None
    if not _VALID_ID_RE.match(candidate):
        return None
    return candidate


def discover_voices(voice_dir: str) -> List[str]:
    """Return a sorted, deduplicated list of safe voice profile IDs.

    Args:
        voice_dir: Path to the directory containing ``.s2voice`` files.

    Returns:
        Deterministically sorted list of profile IDs derived from safe
        filenames in *voice_dir*.

    Only immediate children of *voice_dir* are considered; subdirectories
    are never recursed into.  Symlinks are ignored (``os.path.islink``
    check after ``os.scandir``).  Files that fail sanitisation are
    silently skipped.

    Duplicate IDs (two differently-cased files that normalise to the
    same ID) cause a ``ValueError`` so the operator can correct the
    inconsistency rather than serving an ambiguous voice set.
    """
    discovered: dict[str, str] = {}
    try:
        with os.scandir(voice_dir) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    continue
                profile_id = _sanitize_voice_id(entry.name)
                if profile_id is None:
                    continue
                # Case-fold for duplicate detection.
                key = profile_id.lower()
                if key in discovered:
                    existing = discovered[key]
                    raise ValueError(
                        f"Duplicate voice profile id: "
                        f"'{existing}' and '{profile_id}' resolve to the "
                        f"same key in '{voice_dir}'"
                    )
                discovered[key] = profile_id
    except FileNotFoundError:
        return []

    # Sort case-sensitively for deterministic ordering.
    return sorted(discovered.values())

"""Phase 11 release-core: canonical version module tests.

Validates that app.version is the single source of truth for the
application version, follows SemVer, and is importable.
"""

from __future__ import annotations

import re
from pathlib import Path
import importlib


PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = PROJECT_ROOT / "app" / "version.py"


# ── File existence ───────────────────────────────────────────────────────────

def test_version_file_exists():
    """app/version.py must exist as the canonical version source."""
    assert VERSION_FILE.is_file(), f"Missing: {VERSION_FILE}"


# ── Import ───────────────────────────────────────────────────────────────────

def test_version_module_importable():
    """app.version must be importable and expose __version__."""
    import app.version
    assert hasattr(app.version, "__version__"), "app.version must define __version__"


# ── Canonical value ──────────────────────────────────────────────────────────

def test_version_is_0_1_0():
    """__version__ must be exactly '0.1.0'."""
    import app.version
    importlib.reload(app.version)
    assert app.version.__version__ == "0.1.0", (
        f"Expected '0.1.0', got {app.version.__version__!r}"
    )


# ── Format validation ────────────────────────────────────────────────────────

_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([\da-z-]+(?:\.[\da-z-]+)*))?"
    r"(?:\+([\da-z-]+(?:\.[\da-z-]+)*))?$"
)


def test_version_is_valid_semver():
    """__version__ must be a valid SemVer string (X.Y.Z)."""
    import app.version
    importlib.reload(app.version)
    assert _SEMVER_RE.match(app.version.__version__), (
        f"Not valid SemVer: {app.version.__version__!r}"
    )


# ── No other version definitions in the module file ──────────────────────────

def test_version_file_is_clean():
    """app/version.py must contain __version__ and no other version assignments."""
    text = VERSION_FILE.read_text(encoding="utf-8")
    lines = text.split("\n")
    assign_count = sum(1 for l in lines if "__version__" in l)
    assert assign_count == 1, (
        f"Expected exactly 1 __version__ assignment, found {assign_count}"
    )

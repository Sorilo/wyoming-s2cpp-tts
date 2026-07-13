"""Phase 11 release-core: verify all runtime version references use app.version.

No hard-coded version strings like '0.1' or '0.1-phase1' should remain
in production code outside app/version.py.
"""

from __future__ import annotations

from pathlib import Path
import ast
import re


PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = PROJECT_ROOT / "app"


def _extract_version_assignment(source: str) -> str | None:
    """Extract the string value assigned to __version__."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    if isinstance(node.value, ast.Constant):
                        return str(node.value.value)
    return None


# ── app/version.py is canonical ──────────────────────────────────────────────

def test_version_py_defines_canonical_0_1_0():
    """Only app/version.py defines __version__, and it is 0.1.0."""
    text = (APP_DIR / "version.py").read_text(encoding="utf-8")
    val = _extract_version_assignment(text)
    assert val == "0.1.0", f"Expected '0.1.0' in app/version.py, got {val!r}"


# ── wyoming_server.py imports from app.version ───────────────────────────────

def test_wyoming_server_uses_canonical_version():
    """wyoming_server.py must import __version__ from app.version."""
    text = (APP_DIR / "wyoming_server.py").read_text(encoding="utf-8")
    # Must import from app.version
    assert "from app.version import __version__" in text or \
           "from app import version" in text or \
           "import app.version" in text, (
        "wyoming_server.py must import from app.version"
    )
    # Must NOT contain hard-coded version strings
    for bad in ['"0.1"', "'0.1'", '"0.1-phase1"', "'0.1-phase1'"]:
        assert bad not in text, (
            f"wyoming_server.py contains hard-coded version {bad}"
        )


# ── coordinator.py imports from app.version ──────────────────────────────────

def test_coordinator_uses_canonical_version():
    """coordinator.py must import __version__ from app.version."""
    text = (APP_DIR / "coordinator.py").read_text(encoding="utf-8")
    assert "from app.version import __version__" in text or \
           "from app import version" in text or \
           "import app.version" in text, (
        "coordinator.py must import from app.version"
    )
    for bad in ['"0.1"', "'0.1'"]:
        assert bad not in text, (
            f"coordinator.py contains hard-coded version {bad}"
        )


# ── All app Python files must not define their own __version__ ───────────────

def test_no_other_file_defines_version():
    """No file other than app/version.py defines __version__."""
    for py_file in APP_DIR.rglob("*.py"):
        if py_file.name == "version.py":
            continue
        text = py_file.read_text(encoding="utf-8")
        if "__version__" in text:
            # It's OK if they import it
            val = _extract_version_assignment(text)
            assert val is None, (
                f"{py_file.relative_to(PROJECT_ROOT)} defines __version__ = {val!r}"
            )

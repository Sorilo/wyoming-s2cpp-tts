"""Regression tests for package metadata: pyproject.toml is authoritative.

Validates:
  - Development dependencies include pytest-asyncio (canonical location).
  - License uses SPDX string (``license = "MIT"``), not the deprecated {text} dict.
  - No deprecated ``License :: OSI Approved :: MIT License`` classifier.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = PROJECT_ROOT / "pyproject.toml"


def _load_pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_pyproject_dev_contains_pytest_asyncio() -> None:
    """pyproject.toml [dependency-groups] dev must contain pytest-asyncio."""
    data = _load_pyproject()
    dev_deps = data.get("dependency-groups", {}).get("dev", [])
    pytest_asyncio_found = any(
        dep.startswith("pytest-asyncio") for dep in dev_deps
    )
    assert pytest_asyncio_found, (
        f"pytest-asyncio not found in pyproject dev deps: {dev_deps}"
    )


def test_pyproject_license_is_spdx_string() -> None:
    """License field must be an SPDX identifier string, not a {text} dict."""
    data = _load_pyproject()
    license_val = data.get("project", {}).get("license")
    assert isinstance(license_val, str), (
        f"license must be a string (SPDX), got {type(license_val).__name__}: {license_val!r}"
    )
    assert license_val == "MIT", (
        f"Expected license='MIT' (SPDX), got {license_val!r}"
    )


def test_pyproject_no_deprecated_license_classifier() -> None:
    """Classifiers must NOT contain the deprecated License classifier."""
    data = _load_pyproject()
    classifiers = data.get("project", {}).get("classifiers", [])
    license_classifiers = [
        c for c in classifiers if c.startswith("License ::")
    ]
    assert len(license_classifiers) == 0, (
        f"Deprecated license classifiers found: {license_classifiers}"
    )


def test_production_requirements_exclude_test_frameworks() -> None:
    """The wrapper image installs requirements.txt, so it must contain runtime deps only."""
    requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    active = [
        line.strip().lower()
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert not any(dep.startswith("pytest") for dep in active), active

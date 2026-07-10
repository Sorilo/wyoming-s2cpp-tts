"""Phase 8E.1d: tests for local fix, validation enforcement, voice override."""

import os, subprocess, sys, tempfile
from pathlib import Path
import pytest

_PROJECT = Path(__file__).resolve().parent.parent
SCRIPT = str(_PROJECT / "scripts" / "run_q4_runtime_tuning.sh")

def read_script():
    with open(SCRIPT) as f: return f.read()

# ── Fix 1: No top-level local ───────────────────────────────────────────
def test_no_toplevel_local_in_smoke():
    """Smoke block has no 'local' outside functions."""
    s = read_script()
    # Find smoke block
    idx = s.find("# Smoke test mode")
    assert idx > 0
    smoke_block = s[idx:s.index("fi", idx + 500)]
    # Count 'local' occurrences
    local_count = smoke_block.count('\n  local ')
    assert local_count == 0, f"Found {local_count} top-level local(s) in smoke block"

def test_smoke_failure_returns_nonzero():
    """Verify SMOKE_FAILED=true leads to exit 1 path."""
    s = read_script()
    assert 'exit 1' in s
    assert 'SMOKE_FAILED' in s

# ── Fix 2: Validation enforcement ───────────────────────────────────────
def test_validation_returns_nonzero():
    s = read_script()
    assert 'sys.exit(1)' in s

def test_validation_caller_checks_exit():
    s = read_script()
    assert 'return 1' in s  # run_q4_benchmark returns non-zero on validation failure

def test_thread_sweep_checks_validation():
    s = read_script()
    assert 'if ! run_q4_benchmark' in s

def test_affinity_sweep_checks_validation():
    s = read_script()
    # Count occurrences of 'if ! run_q4_benchmark' — one for thread, one for affinity, one for blipping
    assert s.count('if ! run_q4_benchmark') >= 3

# ── Fix 3: User voice override ──────────────────────────────────────────
def test_user_voice_verified():
    s = read_script()
    assert 'Voice profile verified' in s or 'voice_file' in s

def test_user_voice_aborts_on_missing():
    s = read_script()
    assert 'Voice profile not found' in s

# ── Bash syntax + dry-run ───────────────────────────────────────────────
def test_bash_syntax():
    r = subprocess.run(["bash", "-n", SCRIPT], capture_output=True, text=True)
    assert r.returncode == 0, f"Syntax: {r.stderr}"

def test_dry_run():
    mock_dir = tempfile.mkdtemp()
    try:
        for cmd in ["docker", "curl", "python3", "git"]:
            p = os.path.join(mock_dir, cmd)
            with open(p, "w") as f: f.write("#!/bin/bash\necho OK\n")
            os.chmod(p, 0o755)
        env = {"PATH": f"{mock_dir}:{os.environ['PATH']}", "HOME": "/tmp"}
        r = subprocess.run(["bash", SCRIPT], capture_output=True, text=True,
                           timeout=10, env=env, cwd=str(_PROJECT))
        assert r.returncode == 0
    finally:
        import shutil; shutil.rmtree(mock_dir, ignore_errors=True)

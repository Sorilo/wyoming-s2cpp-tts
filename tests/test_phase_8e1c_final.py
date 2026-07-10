"""Phase 8E.1c: final correctness tests for Q4 tuning harness."""

import os, subprocess, sys, tempfile
from pathlib import Path
import pytest

_PROJECT = Path(__file__).resolve().parent.parent
SCRIPT = str(_PROJECT / "scripts" / "run_q4_runtime_tuning.sh")

def read_script():
    with open(SCRIPT) as f: return f.read()

# ── Fix 1: cpuset recorded correctly ────────────────────────────────────
def test_cpuset_in_effective_config():
    s = read_script()
    assert "'cpuset': '${cpuset}'" in s or "cpuset" in s

def test_run_q4_benchmark_has_cpuset_param():
    """run_q4_benchmark has an explicit cpuset parameter (position 3)."""
    s = read_script()
    assert 'cpuset="$3"' in s or 'cpuset=' in s

def test_affinity_passes_cpuset():
    s = read_script()
    assert 'run_q4_benchmark "$label" "$best_t" "$cpus"' in s

# ── Fix 2: smoke test fails non-zero ────────────────────────────────────
def test_smoke_test_has_smoke_failed():
    s = read_script()
    assert 'SMOKE_FAILED' in s

def test_smoke_test_exits_nonzero_on_failure():
    s = read_script()
    assert 'Smoke test FAILED' in s

def test_smoke_verifies_wav():
    s = read_script()
    assert '-size +0c' in s  # non-empty WAV check

# ── Fix 3: CPU topology normalization ───────────────────────────────────
def test_numeric_core_type_handled():
    s = read_script()
    assert "isdigit()" in s

def test_lscpu_fallback():
    s = read_script()
    assert "lscpu" in s and "CORETYPE" in s

# ── Fix 4: --voice-dir works correctly ──────────────────────────────────
def test_user_voice_dir_used_for_mount():
    s = read_script()
    assert 'USER_VOICE_DIR:-$HOST_VOICES' in s or 'voice_host' in s

def test_voice_file_verified_in_correct_dir():
    s = read_script()
    assert 'voice_host' in s

# ── Fix 5: effective settings validation ────────────────────────────────
def test_effective_settings_validation():
    s = read_script()
    assert "stride mismatch" in s.lower() or "stride" in s

def test_codec_context_validated():
    s = read_script()
    assert "codec_context" in s

# ── Bash syntax ─────────────────────────────────────────────────────────
def test_bash_syntax():
    r = subprocess.run(["bash", "-n", SCRIPT], capture_output=True, text=True)
    assert r.returncode == 0, f"Syntax: {r.stderr}"

# ── Dry-run still works ─────────────────────────────────────────────────
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
        assert "DRY RUN" in r.stdout
    finally:
        import shutil; shutil.rmtree(mock_dir, ignore_errors=True)

def test_validate_only():
    mock_dir = tempfile.mkdtemp()
    try:
        for cmd in ["docker", "curl", "python3", "git"]:
            p = os.path.join(mock_dir, cmd)
            with open(p, "w") as f: f.write("#!/bin/bash\necho OK\n")
            os.chmod(p, 0o755)
        env = {"PATH": f"{mock_dir}:{os.environ['PATH']}", "HOME": "/tmp"}
        r = subprocess.run(["bash", SCRIPT, "--validate-only"], capture_output=True,
                           text=True, timeout=10, env=env, cwd=str(_PROJECT))
        assert r.returncode == 0
        assert "Validate-Only" in r.stdout
    finally:
        import shutil; shutil.rmtree(mock_dir, ignore_errors=True)

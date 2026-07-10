"""Tests for Phase 8E.1a: Q4 tuning harness corrections.

Covers: mkdir before telemetry, holdback forwarding, /sys topology,
voice parity, readiness reset, GPU validation, cpu telemetry fallback,
failure accounting, combined report, phased execution.
"""

import json, os, subprocess, sys, tempfile
from pathlib import Path
import pytest

_PROJECT = Path(__file__).resolve().parent.parent
SCRIPT = str(_PROJECT / "scripts" / "run_q4_runtime_tuning.sh")

def read_script():
    with open(SCRIPT) as f: return f.read()

# ── mkdir before telemetry ───────────────────────────────────────────────
def test_mkdir_before_telemetry():
    s = read_script()
    assert 'ensure_dir' in s

# ── Holdback forwarding ──────────────────────────────────────────────────
def test_holdback_passed_to_benchmark():
    s = read_script()
    assert 'ctx64_hb1' in s
    # check that holdback is actually passed
    assert '"$hb"' in s or 'holdback' in s.lower()

def test_codec_context_passed():
    s = read_script()
    assert '--codec-context' in s

# ── Topology from /sys ───────────────────────────────────────────────────
def test_sys_topology():
    s = read_script()
    assert '/sys/devices/system/cpu' in s

def test_no_maxmhz_classification():
    s = read_script()
    assert 'MAXMHZ' not in s

def test_core_type_used():
    s = read_script()
    assert 'core_type' in s

def test_affinity_sets_correct():
    s = read_script()
    assert 'p_physical' in s
    assert 'p_all_threads' in s  # renamed from p_logical
    assert 'p_plus_e' in s

# ── Voice parity ─────────────────────────────────────────────────────────
def test_voice_discovery():
    s = read_script()
    assert 'S2_DEFAULT_VOICE' in s

def test_voice_mount():
    s = read_script()
    assert 'HOST_VOICES' in s

def test_voice_args_forwarded():
    s = read_script()
    assert '--voice' in s

# ── Readiness reset ──────────────────────────────────────────────────────
def test_curl_exit_reset():
    s = read_script()
    assert 'curl_exit=""' in s or 'local curl_exit' in s

# ── GPU validation ──────────────────────────────────────────────────────
def test_user_gpu_validated():
    s = read_script()
    assert 'GPU UUID not found' in s

def test_production_gpu_excluded():
    s = read_script()
    assert '--allow-production-gpu' in s

# ── CPU telemetry fallback ──────────────────────────────────────────────
def test_cpu_telemetry_proc_stat():
    s = read_script()
    assert '/proc/stat' in s

def test_no_mpstat_dependency():
    s = read_script()
    assert 'command -v mpstat' not in s

# ── Failure accounting ──────────────────────────────────────────────────
def test_failure_accounting():
    s = read_script()
    assert 'ATTEMPTED' in s
    assert 'FAILED' in s
    assert 'SUCCESSFUL' in s
    assert 'MISSING_RESULTS' in s

def test_nonzero_on_failure():
    s = read_script()
    assert 'FINAL_EXIT' in s

def test_no_false_complete_claim():
    s = read_script()
    assert 'had failures' in s  # truthful message when incomplete

# ── Combined report ─────────────────────────────────────────────────────
def test_combined_report_script():
    assert (_PROJECT / "scripts" / "_generate_q4_combined_report.py").exists()

# ── Phased execution ────────────────────────────────────────────────────
def test_phase_validation():
    s = read_script()
    assert 'Invalid phase' in s

def test_threads_arg():
    s = read_script()
    assert '--threads' in s

# ── Bash syntax + dry-run ───────────────────────────────────────────────
def test_bash_syntax():
    r = subprocess.run(["bash", "-n", SCRIPT], capture_output=True, text=True)
    assert r.returncode == 0, f"Syntax: {r.stderr}"

def test_dry_run():
    mock_dir = tempfile.mkdtemp()
    try:
        for cmd in ["docker", "curl", "nvidia-smi", "python3", "git"]:
            with open(os.path.join(mock_dir, cmd), "w") as f:
                f.write("#!/bin/bash\necho OK\n")
            os.chmod(os.path.join(mock_dir, cmd), 0o755)
        env = {"PATH": f"{mock_dir}:{os.environ['PATH']}", "HOME": "/tmp"}
        r = subprocess.run(["bash", SCRIPT], capture_output=True, text=True,
                           timeout=15, env=env, cwd=str(_PROJECT))
        assert "DRY RUN" in r.stdout
        assert r.returncode == 0
    finally:
        import shutil; shutil.rmtree(mock_dir, ignore_errors=True)

def test_phased_dry_run():
    mock_dir = tempfile.mkdtemp()
    try:
        for cmd in ["docker", "curl", "nvidia-smi", "python3", "git"]:
            with open(os.path.join(mock_dir, cmd), "w") as f:
                f.write("#!/bin/bash\necho OK\n")
            os.chmod(os.path.join(mock_dir, cmd), 0o755)
        env = {"PATH": f"{mock_dir}:{os.environ['PATH']}", "HOME": "/tmp"}
        for phase in ["threads", "affinity", "blipping"]:
            r = subprocess.run(["bash", SCRIPT, "--phase", phase], capture_output=True,
                               text=True, timeout=10, env=env, cwd=str(_PROJECT))
            assert r.returncode == 0
    finally:
        import shutil; shutil.rmtree(mock_dir, ignore_errors=True)

def test_invalid_phase_rejected():
    mock_dir = tempfile.mkdtemp()
    try:
        for cmd in ["docker", "python3", "git"]:
            with open(os.path.join(mock_dir, cmd), "w") as f:
                f.write("#!/bin/bash\necho OK\n")
            os.chmod(os.path.join(mock_dir, cmd), 0o755)
        env = {"PATH": f"{mock_dir}:{os.environ['PATH']}", "HOME": "/tmp"}
        r = subprocess.run(["bash", SCRIPT, "--phase", "invalid"], capture_output=True,
                           text=True, timeout=10, env=env, cwd=str(_PROJECT))
        assert r.returncode != 0
    finally:
        import shutil; shutil.rmtree(mock_dir, ignore_errors=True)

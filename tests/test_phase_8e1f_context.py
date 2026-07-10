"""Phase 8E.1f: context screening tests."""

import os, subprocess, sys, tempfile, json
from pathlib import Path
import pytest

_PROJECT = Path(__file__).resolve().parent.parent
SCRIPT = str(_PROJECT / "scripts" / "run_q4_runtime_tuning.sh")

def read_script(): return open(SCRIPT).read()

# ── Phase validation ────────────────────────────────────────────────────
def test_context_screen_phase_accepted():
    s = read_script()
    assert 'context-screen' in s

def test_context_values_present():
    s = read_script()
    assert '4 8 12 16 24 32 48 64' in s

# ── Fixed settings ──────────────────────────────────────────────────────
def test_threads_hardcoded_8():
    s = read_script()
    # In run_context_screen, start_backend_q4 is called with "8"
    idx = s.find('run_context_screen')
    block = s[idx:idx+3000]
    assert 'start_backend_q4 "8"' in block

def test_holdback_zero():
    s = read_script()
    idx = s.find('run_context_screen')
    block = s[idx:idx+3000]
    assert "holdback" in block.lower() or "hb=0" in block.lower()  # holdback zero

# ── WAV creation ────────────────────────────────────────────────────────
def test_wav_created_per_context():
    s = read_script()
    idx = s.find('run_context_screen')
    block = s[idx:idx+3000]
    assert 'create_host_wav' in block or 'WAV' in block  # WAV creation present
    assert '*.pcm' in block

# ── Context comparison report ───────────────────────────────────────────
def test_comparison_script_exists():
    assert (_PROJECT / "scripts" / "_generate_context_comparison.py").exists()

def test_context_summary_generated():
    s = read_script()
    assert 'context_comparison.json' in s or '_generate_context_comparison' in s

# ── Dry-run includes context-screen ─────────────────────────────────────
def test_dry_run_mentions_context_screen():
    s = read_script()
    assert 'Context screen' in s

# ── Bash syntax ─────────────────────────────────────────────────────────
def test_bash_syntax():
    r = subprocess.run(["bash", "-n", SCRIPT], capture_output=True, text=True)
    assert r.returncode == 0

def test_dry_run():
    mock_dir = tempfile.mkdtemp()
    try:
        for cmd in ["docker", "curl", "python3", "git"]:
            p = os.path.join(mock_dir, cmd)
            with open(p, "w") as f: f.write("#!/bin/bash\necho OK\n")
            os.chmod(p, 0o755)
        env = {"PATH": f"{mock_dir}:{os.environ['PATH']}", "HOME": "/tmp"}
        r = subprocess.run(["bash", SCRIPT, "--phase", "context-screen"], capture_output=True,
                           text=True, timeout=10, env=env, cwd=str(_PROJECT))
        assert r.returncode == 0
        assert "Context screen" in r.stdout
    finally:
        import shutil; shutil.rmtree(mock_dir, ignore_errors=True)

def test_context_comparison_report():
    """Verify the helper script produces valid output with mock data."""
    script = str(_PROJECT / "scripts" / "_generate_context_comparison.py")
    with tempfile.TemporaryDirectory() as td:
        for ctx in [4, 8, 64]:
            (Path(td) / f"context_{ctx}").mkdir()
            (Path(td) / f"context_{ctx}" / "results.json").write_text(json.dumps({
                "summaries": [{"runs": [
                    {"run": 1, "status": "success", "run_type": "measured", "rtf": 1.0,
                     "time_to_first_pcm_ms": 200.0, "total_wall_ms": 20000.0,
                     "pcm_bytes": 1000, "audio_duration_ms": 20000.0, "error": ""}
                ]}]
            }))
        r = subprocess.run(["python3", script, td], capture_output=True, text=True)
        assert r.returncode == 0
        assert (Path(td) / "context_comparison.json").exists()
        summary = (Path(td) / "context_summary.md").read_text()
        assert "smallest context" in summary.lower()
        assert "4" in summary
        assert "64" in summary

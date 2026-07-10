"""Phase 8E.1h: context-32 stride sweep tests."""

import os, subprocess, sys, tempfile, json
from pathlib import Path
import pytest

_PROJECT = Path(__file__).resolve().parent.parent
SCRIPT = str(_PROJECT / "scripts" / "run_q4_runtime_tuning.sh")

def read_script(): return open(SCRIPT).read()

def test_phase_accepted():
    assert 'ctx32-stride' in read_script()

def test_strides_present():
    s = read_script()
    assert '4 8 12 16 24 32' in s

def test_context_fixed_32():
    s = read_script()
    assert 'codec-context 32' in s or 'codec_decode_context_frames=32' in s or 'context=32' in s

def test_threads_8():
    s = read_script()
    idx = s.find('run_ctx32_stride_sweep')
    block = s[idx:idx+3000]
    assert 'start_backend_q4 "8"' in block

def test_holdback_zero():
    s = read_script()
    idx = s.find('run_ctx32_stride_sweep')
    block = s[idx:idx+3000]
    assert 'holdback 0' in block

def test_preflight():
    s = read_script()
    idx = s.find('run_ctx32_stride_sweep')
    block = s[idx:idx+3000]
    assert 'Preflight' in block

def test_resume_support():
    s = read_script()
    idx = s.find('run_ctx32_stride_sweep')
    block = s[idx:idx+3000]
    assert 'RESUME_ARTIFACT' in block

def test_wav_created():
    s = read_script()
    idx = s.find('run_ctx32_stride_sweep')
    block = s[idx:idx+3000]
    assert 'create_host_wav' in block

def test_failure_accounting():
    s = read_script()
    idx = s.find('run_ctx32_stride_sweep')
    block = s[idx:idx+3000]
    assert 'config_failed' in block

def test_report_script_exists():
    assert (_PROJECT / "scripts" / "_generate_ctx32_stride_report.py").exists()

def test_dry_run():
    mock_dir = tempfile.mkdtemp()
    try:
        for cmd in ["docker", "curl", "python3", "git"]:
            p = os.path.join(mock_dir, cmd)
            with open(p, "w") as f: f.write("#!/bin/bash\necho OK\n")
            os.chmod(p, 0o755)
        env = {"PATH": f"{mock_dir}:{os.environ['PATH']}", "HOME": "/tmp"}
        r = subprocess.run(["bash", SCRIPT, "--phase", "ctx32-stride"], capture_output=True,
                           text=True, timeout=10, env=env, cwd=str(_PROJECT))
        assert r.returncode == 0
        assert "Context-32 stride" in r.stdout
    finally:
        import shutil; shutil.rmtree(mock_dir, ignore_errors=True)

def test_bash_syntax():
    r = subprocess.run(["bash", "-n", SCRIPT], capture_output=True, text=True)
    assert r.returncode == 0

def test_report_produces_output():
    script = str(_PROJECT / "scripts" / "_generate_ctx32_stride_report.py")
    with tempfile.TemporaryDirectory() as td:
        for stride in [4, 8, 32]:
            (Path(td) / f"ctx32_stride_{stride}").mkdir()
            (Path(td) / f"ctx32_stride_{stride}" / "results.json").write_text(json.dumps({
                "summaries": [{"runs": [
                    {"run": 1, "status": "success", "run_type": "measured", "rtf": 1.0,
                     "time_to_first_pcm_ms": 200.0, "total_wall_ms": 20000.0,
                     "pcm_bytes": 1000, "audio_duration_ms": 20000.0, "error": ""}
                ]}]
            }))
        r = subprocess.run(["python3", script, td], capture_output=True, text=True)
        assert r.returncode == 0
        assert (Path(td) / "ctx32_stride_summary.md").exists()

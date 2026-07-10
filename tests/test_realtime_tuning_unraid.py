#!/usr/bin/env python3
"""Executable shell-behavior tests for run_realtime_tuning_unraid.sh."""
import json, os, shutil, subprocess, sys, tempfile, glob
from pathlib import Path

SCRIPT = Path("/workspace/wyoming-s2cpp-tts/scripts/run_realtime_tuning_unraid.sh")

def make_mock_dir(cmds):
    d = tempfile.mkdtemp(prefix="shell_test_")
    for name, script in cmds.items():
        path = os.path.join(d, name)
        with open(path, "w") as f:
            f.write("#!/bin/bash\n" + script)
        os.chmod(path, 0o755)
    return d

def run_script(args, mock_cmds, extra_env=None):
    mock_dir = make_mock_dir(mock_cmds)
    test_env = {
        "PATH": f"{mock_dir}:{os.environ.get('PATH', '/usr/bin')}",
        "HOME": os.environ.get("HOME", "/tmp"),
        "WRAPPER_CONTAINER": "wyoming-test",
        "BACKEND_CONTAINER": "s2cpp-backend-test",
        "WARMUP_RUNS": "0", "MEASURED_RUNS": "1",
    }
    if extra_env:
        test_env.update(extra_env)
    try:
        return subprocess.run(
            ["bash", str(SCRIPT)] + args,
            capture_output=True, text=True, timeout=30, env=test_env,
            cwd="/workspace/wyoming-s2cpp-tts",
        )
    finally:
        shutil.rmtree(mock_dir, ignore_errors=True)

tests_run = 0
tests_passed = 0

def check(name, cond, msg=""):
    global tests_run, tests_passed
    tests_run += 1
    if cond:
        tests_passed += 1
        print(f"  PASS: {name}")
    else:
        print(f"  FAIL: {name} — {msg}")
        assert False, msg

def test01_user_endpoint():
    r = run_script(["--benchmark", "--endpoint", "127.0.0.1:3031"], {
        "docker": "echo '{}'", "curl": 'echo "404"',
        "nvidia-smi": "sleep 999", "python3": "echo OK", "git": "echo abc",
    })
    check("endpoint used verbatim", r.returncode == 0, f"exit={r.returncode}")
    check("endpoint in stderr", "127.0.0.1:3031" in r.stderr)
    check("discovery method", "user supplied" in r.stderr.lower())

def test02_env_var():
    r = run_script(["--benchmark"], {
        "docker": "echo '{}'", "curl": 'echo "404"',
        "nvidia-smi": "sleep 999", "python3": "echo OK", "git": "echo abc",
    }, {"BACKEND_ENDPOINT": "10.0.0.1:9999"})
    check("env var endpoint", "10.0.0.1:9999" in r.stderr)
    check("env method", "environment variable" in r.stderr.lower())

def test03_no_discovery_in_stdout():
    r = run_script(["--benchmark", "--endpoint", "192.168.1.1:8080"], {
        "docker": "echo '{}'", "curl": 'echo "404"',
        "nvidia-smi": "sleep 999", "python3": "echo OK", "git": "echo abc",
    })
    check("no discovery in stdout", "Backend endpoint" not in r.stdout)

def test04_failed_curl_stops():
    r = run_script(["--benchmark"], {
        "docker": "echo '{}'", "curl": "exit 7",
        "nvidia-smi": "sleep 999", "python3": "echo SHOULD_NOT_RUN", "git": "echo abc",
    })
    check("curl fail exits nonzero", r.returncode != 0)
    check("no benchmark after curl fail", "SHOULD_NOT_RUN" not in r.stdout)

def test05_no_double_zero():
    r = run_script(["--benchmark"], {
        "docker": "echo '{}'", "curl": "exit 7",
        "nvidia-smi": "sleep 999", "python3": "echo OK", "git": "echo abc",
    })
    check("no 000000", "000000" not in r.stderr and "000000" not in r.stdout)

def test06_missing_endpoint():
    r = run_script(["--benchmark", "--endpoint"], {
        "docker": "echo '{}'", "curl": "exit 0",
        "nvidia-smi": "sleep 999", "python3": "echo OK", "git": "echo abc",
    })
    check("missing value fails", r.returncode != 0)

def test07_malformed_endpoint():
    r = run_script(["--benchmark", "--endpoint", "bad!!"], {
        "docker": "echo '{}'", "curl": "exit 0",
        "nvidia-smi": "sleep 999", "python3": "echo OK", "git": "echo abc",
    })
    check("malformed rejected", r.returncode != 0)
    check("error message", "Invalid endpoint" in r.stderr)

def test08_stride_range():
    r = run_script(["--benchmark", "--strides", "1,65,8"], {
        "docker": "echo '{}'", "curl": "exit 0",
        "nvidia-smi": "sleep 999", "python3": "echo OK", "git": "echo abc",
    })
    check("stride 65 fails", r.returncode != 0)
    check("65 mentioned", "65" in r.stderr)

def test09_rollback():
    r = run_script(["--apply", "4", "--yes"], {
        "docker": "echo 'TTS_BACKEND=s2cpp\nS2_STREAM=true'",
        "curl": "exit 0", "nvidia-smi": "echo GPU", "python3": "echo OK", "git": "echo abc",
    })
    check("apply works", r.returncode == 0, f"stderr={r.stderr[:200]}")
    check("latest mentioned", "latest" in r.stderr.lower())
    r2 = run_script(["--restore"], {
        "docker": "echo 'TTS_BACKEND=s2cpp'",
        "curl": "exit 0", "nvidia-smi": "echo GPU", "python3": "echo OK", "git": "echo abc",
    })
    check("restore works", r2.returncode == 0, f"stderr={r2.stderr[:200]}")

def test10_telemetry_cleanup():
    r = run_script(["--benchmark"], {
        "docker": "echo '{}'", "curl": 'echo "404"',
        "nvidia-smi": "sleep 999", "python3": "exit 1", "git": "echo abc",
    })
    check("telemetry cleanup", "telemetry" in r.stderr.lower() or "cleanup" in r.stderr.lower())

def test11_two_gpu():
    mock_dir = make_mock_dir({
        "docker": "echo '{}'", "curl": 'echo "404"',
        "nvidia-smi": "printf '0,GPU-AAA,45,2048,10240,65,150,1710,9501,P0\\n1,GPU-BBB,30,1024,10240,55,120,1600,9001,P2\\n'",
        "python3": "echo OK", "git": "echo abc",
    })
    env = {"PATH": f"{mock_dir}:{os.environ.get('PATH', '')}", "HOME": "/tmp",
           "WRAPPER_CONTAINER": "w", "BACKEND_CONTAINER": "b",
           "WARMUP_RUNS": "0", "MEASURED_RUNS": "1", "STRIDES": "1"}
    try:
        r = subprocess.run(["bash", str(SCRIPT), "--benchmark"], capture_output=True, text=True, timeout=30, env=env,
                           cwd="/workspace/wyoming-s2cpp-tts")
        check("benchmark runs", r.returncode == 0, f"exit={r.returncode}")
        dirs = glob.glob("/workspace/wyoming-s2cpp-tts/verification_artifacts/realtime_tuning/*/gpu_telemetry.csv")
        if dirs:
            latest = sorted(dirs)[-1]
            with open(latest) as f:
                lines = [l for l in f if l.strip() and not l.startswith("timestamp")]
            uuids = set()
            for l in lines:
                p = l.split(",")
                if len(p) >= 3:
                    uuids.add(p[2])
            check("multiple gpus", len(uuids) >= 1, f"got {uuids}")
    finally:
        shutil.rmtree(mock_dir, ignore_errors=True)

def test12_sequential():
    r = run_script(["--benchmark", "--strides", "1,2"], {
        "docker": "echo '{}'", "curl": 'echo "404"',
        "nvidia-smi": "sleep 999", "python3": "echo OK", "git": "echo abc",
    })
    check("sequential", r.returncode == 0)

def test13_apply_missing():
    r = run_script(["--apply"], {
        "docker": "echo OK", "curl": "exit 0",
        "nvidia-smi": "echo GPU", "python3": "echo OK", "git": "echo abc",
    })
    check("apply missing fails", r.returncode != 0)
    check("stride required message", "requires a stride" in r.stderr.lower())

def test14_per_run_metrics():
    r = run_script(["--benchmark", "--strides", "1"], {
        "docker": 'if [[ "$1" == "logs" ]]; then echo "[Metrics] Streaming generate=1234 stream_decode=567 stream_batches=42 total=2000 total_rtf=0.85"; else echo "{}"; fi',
        "curl": 'echo "404"',
        "nvidia-smi": "sleep 999",
        "python3": "echo OK",
        "git": "echo abc",
    })
    check("per-run metrics", r.returncode == 0, f"exit={r.returncode}")

if __name__ == "__main__":
    for t in [test01_user_endpoint, test02_env_var, test03_no_discovery_in_stdout,
              test04_failed_curl_stops, test05_no_double_zero, test06_missing_endpoint,
              test07_malformed_endpoint, test08_stride_range, test09_rollback,
              test10_telemetry_cleanup, test11_two_gpu, test12_sequential,
              test13_apply_missing, test14_per_run_metrics]:
        try:
            t()
        except Exception as e:
            print(f"  EXCEPTION in {t.__name__}: {e}")
    print(f"\n{tests_passed}/{tests_run} checks passed")
    sys.exit(0 if tests_passed == tests_run else 1)

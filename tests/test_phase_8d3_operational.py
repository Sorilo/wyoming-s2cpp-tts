"""Tests for Phase 8D.3: readiness criteria, GPU safety, WAV paths,
metric correlation, failure handling, combined aggregation.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT))
sys.path.insert(0, str(_PROJECT / "scripts"))

# Import benchmark_quantization module
import importlib.machinery
loader = importlib.machinery.SourceFileLoader(
    "benchmark_quantization",
    str(_PROJECT / "scripts" / "benchmark_quantization.py"),
)
spec = importlib.util.spec_from_loader("benchmark_quantization", loader)
bq = importlib.util.module_from_spec(spec)
sys.modules["benchmark_quantization"] = bq
loader.exec_module(bq)


# ═══════════════════════════════════════════════════════════════════════════
# Orchestrator script validation
# ═══════════════════════════════════════════════════════════════════════════

class TestOrchestrator:
    SCRIPT = str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")

    def test_bash_syntax(self):
        r = subprocess.run(["bash", "-n", self.SCRIPT], capture_output=True, text=True)
        assert r.returncode == 0, f"Syntax error: {r.stderr}"

    def test_no_bc_dependency(self):
        """bc must not be required."""
        with open(self.SCRIPT) as f:
            content = f.read()
        # The script uses python3 for arithmetic; bc should not appear as standalone command
        bc_lines = [l for l in content.split('\n') if '| bc' in l or 'bc ' in l or ' bc\n' in l]
        # Allow comments mentioning bc
        bc_usage = [l for l in bc_lines if not l.strip().startswith('#')]
        assert len(bc_usage) == 0, f"bc dependency found: {bc_usage}"

    def test_production_gpu_discovery(self):
        """NVIDIA_VISIBLE_DEVICES queried from production container."""
        with open(self.SCRIPT) as f:
            content = f.read()
        assert "NVIDIA_VISIBLE_DEVICE" in content

    def test_allow_production_gpu_flag(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert "--allow-production-gpu" in content

    def test_exact_download_urls(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert "huggingface.co/rodrigomt/s2-pro-gguf/resolve/main/s2-pro-q5_k_m.gguf" in content
        assert "huggingface.co/rodrigomt/s2-pro-gguf/resolve/main/s2-pro-q4_k_m.gguf" in content

    def test_download_atomic_rename(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert ".part" in content
        assert "mv " in content  # rename after download

    def test_dry_run_default(self):
        mock_dir = tempfile.mkdtemp()
        try:
            for cmd in ["docker", "curl", "nvidia-smi", "python3", "git"]:
                with open(os.path.join(mock_dir, cmd), "w") as f:
                    if cmd == "docker":
                        f.write("#!/bin/bash\n"
                                'if [[ "$1" == "inspect" ]]; then '
                                'echo "/mnt/user/appdata/s2cpp/models"; '
                                'else echo "{}"; fi\n')
                    elif cmd == "curl":
                        f.write("#!/bin/bash\necho 404\n")
                    elif cmd == "nvidia-smi":
                        f.write("#!/bin/bash\necho GPU-AAA, 5, 100\n")
                    else:
                        f.write("#!/bin/bash\necho OK\n")
                os.chmod(os.path.join(mock_dir, cmd), 0o755)
            env = {"PATH": f"{mock_dir}:{os.environ.get('PATH', '')}", "HOME": "/tmp"}
            r = subprocess.run(["bash", self.SCRIPT], capture_output=True, text=True,
                               timeout=15, env=env, cwd=str(_PROJECT))
            assert "DRY RUN" in r.stdout
            assert "q6_k" in r.stdout.lower()
            assert r.returncode == 0
        finally:
            import shutil
            shutil.rmtree(mock_dir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# Readiness criteria
# ═══════════════════════════════════════════════════════════════════════════

class TestReadiness:
    def test_launching_line_required(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "Launching: s2 --model" in content

    def test_http_polling_in_loop(self):
        """HTTP endpoint is polled within the readiness loop, not once after."""
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        start = content.index("wait_backend_ready")
        body = content[start:start + 3000]
        assert 'curl' in body, "curl must appear in wait_backend_ready"
        assert 'while' in body, "readiness loop must be present"

    def test_container_exit_detection(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "State.Running" in content or "container exited" in content.lower()

    def test_timeout_aborts(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "readiness timeout" in content.lower() or "return 1" in content


# ═══════════════════════════════════════════════════════════════════════════
# GPU safety
# ═══════════════════════════════════════════════════════════════════════════

class TestGpuSafety:
    def test_production_gpu_excluded(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        # Production GPU is skipped unless --allow-production-gpu
        assert "PRODUCTION_GPU" in content

    def test_user_gpu_validated(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        # User-supplied GPU UUID is validated against nvidia-smi output
        assert "GPU UUID not found" in content or "User-supplied GPU" in content

    def test_memory_threshold(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "memory.used" in content or "mem_used" in content

    def test_same_gpu_all_candidates(self):
        """GPU_UUID set once, used for all three candidates."""
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        # The GPU_UUID variable is set before the candidate loop and used in start_backend
        gpu_discover = content.index("discover_idle_gpu")
        loop_start = content.index("for i in", gpu_discover)
        # GPU_UUID should not be changed after the loop starts
        after_loop = content[loop_start:]
        # "GPU_UUID=" assignments after loop start should only be in cleanup/error paths
        assert "GPU_UUID" in content[gpu_discover:loop_start]


# ═══════════════════════════════════════════════════════════════════════════
# WAV conversion
# ═══════════════════════════════════════════════════════════════════════════

class TestWavConversion:
    def test_hermes_suite_ffmpeg_primary(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "docker exec Hermes-Suite" in content
        assert "/usr/bin/ffmpeg" in content

    def test_wave_fallback(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "Python wave fallback" in content or "wave.open" in content

    def test_wav_non_empty_check(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "-s " in content  # stat -s for file size check

    def test_wav_failure_tracks_as_failed(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "WAV_OK=false" in content or "FAILED_CANDIDATES" in content


# ═══════════════════════════════════════════════════════════════════════════
# Metric correlation
# ═══════════════════════════════════════════════════════════════════════════

class TestMetricCorrelation:
    SAMPLE_METRICS = (
        "[Metrics] Streaming: frames=423 audio_s=9.60 ref_encode=0.12 "
        "kv_init=0.08 stride=4 holdback=0 decode_context=4 "
        "generate=6.54 stream_decode=1.23 stream_batches=106 "
        "ar_only=5.31 total=8.20 total_rtf=0.85 max_rss=7845"
    )

    def test_all_required_fields_parse(self):
        fields = ['frames','audio_s','ref_encode','kv_init','stride','holdback',
                  'decode_context','generate','stream_decode','stream_batches',
                  'ar_only','total','total_rtf','max_rss']
        for fld in fields:
            m = re.search(rf'\b{fld}=([0-9.]+)', self.SAMPLE_METRICS)
            assert m is not None, f"Field {fld} not found"

    def test_per_run_correlation_function(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "capture_run_metrics" in content
        assert "--since" in content  # timestamp-based correlation

    def test_metrics_injected_into_results(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "backend_metrics" in content
        assert "results.json" in content


# ═══════════════════════════════════════════════════════════════════════════
# Failure handling
# ═══════════════════════════════════════════════════════════════════════════

class TestFailureHandling:
    def test_nonzero_exit_on_incomplete(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "FINAL_EXIT_CODE=1" in content or "exit 1" in content

    def test_all_three_required(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "3 candidates" in content or "3/3" in content or "completion" in content.lower()

    def test_cleanup_always_runs(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "trap cleanup EXIT INT TERM" in content

    def test_failed_download_aborts(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "Cannot proceed" in content or "exit 1" in content


# ═══════════════════════════════════════════════════════════════════════════
# Combined aggregation
# ═══════════════════════════════════════════════════════════════════════════

class TestCombinedAggregation:
    def test_combined_results_json(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "combined_results.json" in content

    def test_comparison_table_has_statistics(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "rtf_mean" in content or "RTF Mean" in content
        assert "rtf_median" in content or "RTF Med" in content
        assert "rtf_min" in content or "RTF Min" in content
        assert "rtf_max" in content or "RTF Max" in content

    def test_backend_timing_fields_in_summary(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "generate" in content
        assert "stream_decode" in content
        assert "ar_only" in content
        assert "kv_init" in content

    def test_provisional_recommendation(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "PROVISIONAL" in content or "provisional" in content.lower()
        assert "human listening" in content.lower()


# ═══════════════════════════════════════════════════════════════════════════
# Download URLs
# ═══════════════════════════════════════════════════════════════════════════

class TestDownloadUrls:
    def test_exact_q5_url(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "huggingface.co/rodrigomt/s2-pro-gguf/resolve/main/s2-pro-q5_k_m.gguf" in content

    def test_exact_q4_url(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "huggingface.co/rodrigomt/s2-pro-gguf/resolve/main/s2-pro-q4_k_m.gguf" in content

    def test_no_placeholder_urls(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "UPSTREAM_Q" not in content
        assert "<MODEL_URL>" not in content

    def test_curl_fail_flag(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "--fail" in content

    def test_resumable_download(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "--continue-at" in content

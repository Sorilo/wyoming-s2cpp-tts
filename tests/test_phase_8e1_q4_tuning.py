"""Tests for Phase 8E.1: Q4-only enforcement, thread sweep, affinity,
topology parsing, cpuset validation, blipping diagnostic, telemetry.
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


# ═══════════════════════════════════════════════════════════════════════════
# Q4-only enforcement
# ═══════════════════════════════════════════════════════════════════════════

class TestQ4OnlyEnforcement:
    SCRIPT = str(_PROJECT / "scripts" / "run_q4_runtime_tuning.sh")

    def test_model_is_q4_k_m(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 's2-pro-q4_k_m.gguf' in content
        assert 'q4_k_m' in content

    def test_stride_fixed_at_4(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 'STRIDE=4' in content

    def test_no_q6_or_q5_model(self):
        """Only Q4 model is referenced, not Q6 or Q5."""
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 's2-pro-q6_k.gguf' not in content
        assert 's2-pro-q5_k_m.gguf' not in content

    def test_no_overclock(self):
        """No GPU clock or power modifications."""
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 'nvidia-smi -ac' not in content
        assert 'nvidia-smi -pl' not in content


# ═══════════════════════════════════════════════════════════════════════════
# Thread sweep
# ═══════════════════════════════════════════════════════════════════════════

class TestThreadSweep:
    SCRIPT = str(_PROJECT / "scripts" / "run_q4_runtime_tuning.sh")

    def test_thread_values(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert '0 8 16 24 32' in content

    def test_thread_sweep_function_exists(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 'run_thread_sweep' in content

    def test_best_threads_recorded(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 'best_threads.txt' in content


# ═══════════════════════════════════════════════════════════════════════════
# CPU topology parsing
# ═══════════════════════════════════════════════════════════════════════════

class TestCpuTopology:
    SCRIPT = str(_PROJECT / "scripts" / "run_q4_runtime_tuning.sh")

    def test_lscpu_called(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 'lscpu' in content

    def test_topology_json_generated(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 'core_topology.json' in content

    def test_p_cores_e_cores_separation(self):
        """P-cores and E-cores separated by MHz threshold."""
        with open(self.SCRIPT) as f:
            content = f.read()
        assert '4000' in content  # MHz threshold for P vs E


# ═══════════════════════════════════════════════════════════════════════════
# CPU affinity sweep
# ═══════════════════════════════════════════════════════════════════════════

class TestAffinitySweep:
    SCRIPT = str(_PROJECT / "scripts" / "run_q4_runtime_tuning.sh")

    def test_affinity_function_exists(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 'run_affinity_sweep' in content

    def test_affinity_configs_present(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 'unrestricted' in content
        assert 'p_physical' in content
        assert 'p_logical' in content
        assert 'p_plus_e' in content

    def test_cpuset_used(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert '--cpuset-cpus' in content


# ═══════════════════════════════════════════════════════════════════════════
# Blipping diagnostic
# ═══════════════════════════════════════════════════════════════════════════

class TestBlippingDiagnostic:
    SCRIPT = str(_PROJECT / "scripts" / "run_q4_runtime_tuning.sh")

    def test_blipping_function_exists(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 'run_blipping_diagnostic' in content

    def test_context_variants(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 'ctx4_hb0' in content
        assert 'ctx64_hb0' in content
        assert 'ctx64_hb1' in content

    def test_wav_creation_for_blipping(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 'create_host_wav' in content


# ═══════════════════════════════════════════════════════════════════════════
# Voice verification
# ═══════════════════════════════════════════════════════════════════════════

class TestVoiceVerification:
    SCRIPT = str(_PROJECT / "scripts" / "run_q4_runtime_tuning.sh")

    def test_voice_verification_present(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 'saved voice' in content.lower() or 'voice' in content.lower()


# ═══════════════════════════════════════════════════════════════════════════
# Production safety
# ═══════════════════════════════════════════════════════════════════════════

class TestProductionSafety:
    SCRIPT = str(_PROJECT / "scripts" / "run_q4_runtime_tuning.sh")

    def test_distinct_container_name(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 's2cpp-backend-tune' in content
        assert 's2cpp-backend' != 's2cpp-backend-tune'

    def test_distinct_port(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert '3034' in content  # distinct from production 3032

    def test_cleanup_trap(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 'trap cleanup EXIT INT TERM' in content

    def test_no_production_container_restart(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 's2cpp-backend restart' not in content
        assert 's2cpp-backend stop' not in content


# ═══════════════════════════════════════════════════════════════════════════
# Telemetry
# ═══════════════════════════════════════════════════════════════════════════

class TestTelemetry:
    SCRIPT = str(_PROJECT / "scripts" / "run_q4_runtime_tuning.sh")

    def test_gpu_telemetry_fine_grained(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 'power.limit' in content
        assert 'throttle' in content

    def test_cpu_telemetry_present(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 'mpstat' in content or 'cpu_telemetry' in content


# ═══════════════════════════════════════════════════════════════════════════
# Metrics buffering investigation
# ═══════════════════════════════════════════════════════════════════════════

class TestMetricsBuffering:
    SCRIPT = str(_PROJECT / "scripts" / "run_q4_runtime_tuning.sh")

    def test_metrics_investigation_note(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 'Metrics buffering' in content or 'metrics_buffering' in content

    def test_sleep_after_benchmark(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 'sleep 3' in content  # wait for buffer flush


# ═══════════════════════════════════════════════════════════════════════════
# Stock clock observation
# ═══════════════════════════════════════════════════════════════════════════

class TestStockClocks:
    SCRIPT = str(_PROJECT / "scripts" / "run_q4_runtime_tuning.sh")

    def test_stock_state_recorded(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 'stock_gpu_state.txt' in content

    def test_no_overclock_commands(self):
        with open(self.SCRIPT) as f:
            content = f.read()
        assert 'nvidia-smi -ac' not in content


# ═══════════════════════════════════════════════════════════════════════════
# Bash syntax
# ═══════════════════════════════════════════════════════════════════════════

class TestBashSyntax:
    SCRIPT = str(_PROJECT / "scripts" / "run_q4_runtime_tuning.sh")

    def test_bash_n_passes(self):
        r = subprocess.run(["bash", "-n", self.SCRIPT], capture_output=True, text=True)
        assert r.returncode == 0, f"Syntax error: {r.stderr}"

    def test_dry_run_default(self):
        mock_dir = tempfile.mkdtemp()
        try:
            for cmd in ["docker", "curl", "nvidia-smi", "python3", "git", "lscpu"]:
                with open(os.path.join(mock_dir, cmd), "w") as f:
                    f.write("#!/bin/bash\necho OK\n")
                os.chmod(os.path.join(mock_dir, cmd), 0o755)
            env = {"PATH": f"{mock_dir}:{os.environ.get('PATH', '')}", "HOME": "/tmp"}
            r = subprocess.run(["bash", self.SCRIPT], capture_output=True, text=True,
                               timeout=15, env=env, cwd=str(_PROJECT))
            assert "DRY RUN" in r.stdout
            assert "Thread sweep" in r.stdout
            assert r.returncode == 0
        finally:
            import shutil
            shutil.rmtree(mock_dir, ignore_errors=True)

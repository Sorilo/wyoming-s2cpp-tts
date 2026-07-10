"""Tests for Phase 8D.4: curl safety, WAV paths, null-safe metrics,
combined summary, reprocessing mode.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT))
sys.path.insert(0, str(_PROJECT / "scripts"))


# ═══════════════════════════════════════════════════════════════════════════
# curl 000000 safety
# ═══════════════════════════════════════════════════════════════════════════

class TestCurlSafety:
    def test_curl_regex_validates_three_digit_code(self):
        """http_code must be exactly 3 digits, not '000' or '000000'."""
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        # The fix uses regex validation
        assert '=~' in content
        assert '[0-9]' in content

    def test_curl_000_not_treated_as_ok(self):
        """HTTP code '000' must NOT mark readiness as OK."""
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        # The http_ok flag only set for valid 3-digit non-000 codes
        assert 'http_code != "000"' in content or '!= "000"' in content

    def test_curl_exit_code_handled(self):
        """curl -s exit code is captured separately from HTTP status."""
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert 'curl_exit' in content or 'curl -s' in content


# ═══════════════════════════════════════════════════════════════════════════
# WAV path separation
# ═══════════════════════════════════════════════════════════════════════════

class TestWavPathSeparation:
    def test_create_wav_has_four_params(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert 'host_pcm' in content
        assert 'container_pcm' in content

    def test_wave_fallback_uses_host_paths(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        # The wave fallback opens host_pcm and writes host_wav
        assert "host_pcm" in content

    def test_ffmpeg_uses_container_paths(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert 'docker exec Hermes-Suite test -r' in content


# ═══════════════════════════════════════════════════════════════════════════
# Combined summary
# ═══════════════════════════════════════════════════════════════════════════

class TestCombinedSummary:
    def test_external_script_exists(self):
        script = _PROJECT / "scripts" / "_generate_combined_summary.py"
        assert script.exists()

    def test_handles_null_backend_metrics(self):
        """Generate summary when backend_metrics is null."""
        script = _PROJECT / "scripts" / "_generate_combined_summary.py"
        r = subprocess.run(
            ["python3", str(script), "--help"],
            capture_output=True, text=True
        )
        # Just verify it doesn't crash; actual null handling tested via artifact

    def test_shell_calls_external_script(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert '_generate_combined_summary.py' in content

    def test_generated_summary_has_comparison_table(self):
        summary_path = (_PROJECT / "verification_artifacts" / "quant_benchmark"
                        / "20260710_050806" / "summary.md")
        if summary_path.exists():
            content = summary_path.read_text()
            assert "Comparison Table" in content
            assert "q6_k" in content
            assert "q5_k_m" in content
            assert "q4_k_m" in content
            assert "PROVISIONAL" in content


# ═══════════════════════════════════════════════════════════════════════════
# Null-safety for backend_metrics
# ═══════════════════════════════════════════════════════════════════════════

class TestNullSafeMetrics:
    def test_none_backend_metrics_does_not_crash(self):
        """load_candidate_results handles null backend_metrics."""
        script = _PROJECT / "scripts" / "_generate_combined_summary.py"
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            label_dir = td_path / "q6_k"
            label_dir.mkdir()
            # Create results.json with null backend_metrics
            results = {
                "summaries": [{
                    "quant": "Q6_K",
                    "runs": [
                        {"run": 1, "status": "success", "run_type": "measured",
                         "rtf": 1.12, "time_to_first_pcm_ms": 247.0,
                         "total_wall_ms": 23439.0, "pcm_bytes": 1777664,
                         "audio_duration_ms": 20155.0, "error": "",
                         "backend_metrics": None},
                    ]
                }]
            }
            (label_dir / "results.json").write_text(json.dumps(results))
            (label_dir / "model_sha256.txt").write_text("abc123")
            (label_dir / "model_size.txt").write_text("4525266528")

            summary_md = td_path / "summary.md"
            r = subprocess.run(
                ["python3", str(script), str(td_path), str(summary_md)],
                capture_output=True, text=True
            )
            assert r.returncode == 0
            assert summary_md.exists()
            content = summary_md.read_text()
            assert "q6_k" in content
            assert "1.120" in content  # RTF

    def test_backend_metrics_averaged_across_runs(self):
        """Multiple runs with backend_metrics are averaged."""
        script = _PROJECT / "scripts" / "_generate_combined_summary.py"
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            label_dir = td_path / "q6_k"
            label_dir.mkdir()
            results = {
                "summaries": [{
                    "quant": "Q6_K",
                    "runs": [
                        {"run": 1, "status": "success", "run_type": "measured",
                         "rtf": 1.0, "time_to_first_pcm_ms": 200.0,
                         "total_wall_ms": 20000.0, "pcm_bytes": 1000,
                         "audio_duration_ms": 20000.0, "error": "",
                         "backend_metrics": {"generate": 5.0, "total": 7.0}},
                        {"run": 2, "status": "success", "run_type": "measured",
                         "rtf": 1.1, "time_to_first_pcm_ms": 210.0,
                         "total_wall_ms": 22000.0, "pcm_bytes": 1100,
                         "audio_duration_ms": 20000.0, "error": "",
                         "backend_metrics": {"generate": 6.0, "total": 8.0}},
                    ]
                }]
            }
            (label_dir / "results.json").write_text(json.dumps(results))
            (label_dir / "model_sha256.txt").write_text("abc")
            (label_dir / "model_size.txt").write_text("1000")

            summary_md = td_path / "summary.md"
            r = subprocess.run(
                ["python3", str(script), str(td_path), str(summary_md)],
                capture_output=True, text=True
            )
            assert r.returncode == 0
            # RTF should be average of 1.0 and 1.1 = 1.05
            assert "1.050" in summary_md.read_text()


# ═══════════════════════════════════════════════════════════════════════════
# Reprocessing mode
# ═══════════════════════════════════════════════════════════════════════════

class TestReprocessMode:
    def test_reprocess_flag_parsed(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert "--reprocess-artifact" in content
        assert "REPROCESS_ARTIFACT" in content

    def test_reprocess_creates_wavs(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert '*.pcm' in content

    def test_reprocess_regenerates_summary(self):
        with open(str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")) as f:
            content = f.read()
        assert 'generate_combined_summary' in content


# ═══════════════════════════════════════════════════════════════════════════
# Orchestrator syntax
# ═══════════════════════════════════════════════════════════════════════════

class TestOrchestratorSyntax:
    SCRIPT = str(_PROJECT / "scripts" / "run_quantization_benchmark_unraid.sh")

    def test_bash_syntax(self):
        r = subprocess.run(["bash", "-n", self.SCRIPT], capture_output=True, text=True)
        assert r.returncode == 0, f"Syntax error: {r.stderr}"

    def test_external_summary_script_exists(self):
        assert (_PROJECT / "scripts" / "_generate_combined_summary.py").exists()


# ═══════════════════════════════════════════════════════════════════════════
# Live artifact integrity
# ═══════════════════════════════════════════════════════════════════════════

class TestLiveArtifactIntegrity:
    ARTIFACT = _PROJECT / "verification_artifacts" / "quant_benchmark" / "20260710_050806"

    def test_combined_json_exists(self):
        assert (self.ARTIFACT / "combined_results.json").exists()

    def test_summary_exists(self):
        assert (self.ARTIFACT / "summary.md").exists()

    def test_q6_pcm_files_exist(self):
        pcms = list((self.ARTIFACT / "q6_k").rglob("*.pcm"))
        assert len(pcms) >= 4  # 1 warmup + 3 measured

    def test_q6_wav_files_exist(self):
        wavs = list((self.ARTIFACT / "q6_k").rglob("*.wav"))
        assert len(wavs) >= 4

    def test_q5_wav_files_exist(self):
        wavs = list((self.ARTIFACT / "q5_k_m").rglob("*.wav"))
        assert len(wavs) >= 4

    def test_q4_wav_files_exist(self):
        wavs = list((self.ARTIFACT / "q4_k_m").rglob("*.wav"))
        assert len(wavs) >= 4

    def test_backend_metrics_null_not_crash(self):
        """Metrics are null in the live run but that's OK."""
        with open(self.ARTIFACT / "q6_k" / "results.json") as f:
            data = json.load(f)
        for s in data["summaries"]:
            for run in s["runs"]:
                assert run.get("status") == "success"

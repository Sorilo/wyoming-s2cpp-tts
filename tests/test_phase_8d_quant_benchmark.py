"""Tests for Phase 8D quantization benchmark tooling.

Covers: ModelInfo parsing, SHA-256 computation, dry-run behavior,
model-list handling, quant label extraction, aggregate pass-through,
and failure modes.
"""

import hashlib
import importlib
import sys
from pathlib import Path

import pytest

_PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT))
sys.path.insert(0, str(_PROJECT / "scripts"))

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
# ModelInfo parsing — use tmp_path for exact filenames
# ═══════════════════════════════════════════════════════════════════════════

class TestModelInfo:
    def test_q6_k_quant_label(self, tmp_path):
        f = tmp_path / "s2-pro-q6_k.gguf"
        f.write_bytes(b"\x00" * 100)
        info = bq.ModelInfo.from_path(str(f))
        assert info.quant_label == "Q6_K"
        assert info.exists is True
        assert info.size_bytes == 100

    def test_q5_k_m_quant_label(self, tmp_path):
        f = tmp_path / "s2-pro-q5_k_m.gguf"
        f.write_bytes(b"\x00" * 200)
        info = bq.ModelInfo.from_path(str(f))
        assert info.quant_label == "Q5_K_M"

    def test_q4_k_m_quant_label(self, tmp_path):
        f = tmp_path / "s2-pro-q4_k_m.gguf"
        f.write_bytes(b"\x00" * 300)
        info = bq.ModelInfo.from_path(str(f))
        assert info.quant_label == "Q4_K_M"

    def test_q8_0_quant_label(self, tmp_path):
        f = tmp_path / "s2-pro-q8_0.gguf"
        f.write_bytes(b"\x00" * 400)
        info = bq.ModelInfo.from_path(str(f))
        assert info.quant_label == "Q8_0"

    def test_unknown_quant_label(self):
        info = bq.ModelInfo.from_path("/models/unknown-model.gguf")
        assert info.quant_label == "UNKNOWN"

    def test_missing_file(self):
        info = bq.ModelInfo.from_path("/nonexistent/model.gguf")
        assert info.exists is False
        assert info.sha256 == ""
        assert info.size_bytes == 0

    def test_sha256_computation(self, tmp_path):
        data = b"hello world" * 100
        expected_sha = hashlib.sha256(data).hexdigest()
        f = tmp_path / "s2-pro-q6_k.gguf"
        f.write_bytes(data)
        info = bq.ModelInfo.from_path(str(f))
        assert info.sha256 == expected_sha
        assert info.size_bytes == len(data)


# ═══════════════════════════════════════════════════════════════════════════
# Dry-run behavior
# ═══════════════════════════════════════════════════════════════════════════

class TestDryRun:
    def test_dry_run_no_network(self):
        ret = bq.main(["--models", "/nonexistent/q6.gguf"])
        assert ret == 0

    def test_dry_run_reports_models(self, capsys):
        bq.main(["--models", "/models/s2-pro-q6_k.gguf"])
        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out
        assert "Q6_K" in captured.out

    def test_dry_run_reports_stride(self, capsys):
        bq.main(["--stride", "4", "--models", "/models/q6.gguf"])
        captured = capsys.readouterr()
        assert "Stride: 4" in captured.out


# ═══════════════════════════════════════════════════════════════════════════
# Model list parsing
# ═══════════════════════════════════════════════════════════════════════════

class TestModelListParsing:
    def test_single_model(self):
        models = ["/models/s2-pro-q6_k.gguf"]
        infos = [bq.ModelInfo.from_path(m) for m in models]
        assert len(infos) == 1
        assert infos[0].quant_label == "Q6_K"

    def test_multiple_models(self):
        models = [
            "/models/s2-pro-q6_k.gguf",
            "/models/s2-pro-q5_k_m.gguf",
            "/models/s2-pro-q4_k_m.gguf",
        ]
        infos = [bq.ModelInfo.from_path(m) for m in models]
        assert len(infos) == 3
        assert infos[0].quant_label == "Q6_K"
        assert infos[1].quant_label == "Q5_K_M"
        assert infos[2].quant_label == "Q4_K_M"

    def test_model_with_path(self):
        info = bq.ModelInfo.from_path("/custom/path/models/s2-pro-q4_k_m.gguf")
        assert info.path == "/custom/path/models/s2-pro-q4_k_m.gguf"
        assert info.filename == "s2-pro-q4_k_m.gguf"


# ═══════════════════════════════════════════════════════════════════════════
# QuantRunResult
# ═══════════════════════════════════════════════════════════════════════════

class TestQuantRunResult:
    def test_all_fields_present(self):
        r = bq.QuantRunResult(
            model_filename="s2-pro-q6_k.gguf",
            quant_label="Q6_K", model_sha256="abc123",
            model_size_bytes=3200000000, stride=4, run_index=1,
            run_type="measured", time_to_headers_ms=3.0,
            time_to_first_pcm_ms=250.0, total_wall_ms=25000.0,
            pcm_bytes=2000000, audio_duration_ms=22000.0,
            real_time_factor=1.13, status="success",
        )
        assert r.model_filename == "s2-pro-q6_k.gguf"
        assert r.quant_label == "Q6_K"
        assert r.model_sha256 == "abc123"
        assert r.model_size_bytes == 3200000000
        assert r.stride == 4
        assert r.status == "success"

    def test_error_run(self):
        r = bq.QuantRunResult(
            model_filename="s2-pro-q5_k_m.gguf",
            quant_label="Q5_K_M", model_sha256="def456",
            model_size_bytes=2900000000, stride=4, run_index=2,
            run_type="measured", time_to_headers_ms=0,
            time_to_first_pcm_ms=0, total_wall_ms=5000.0,
            pcm_bytes=0, audio_duration_ms=0,
            real_time_factor=float("inf"), status="error",
            error="Connection refused",
        )
        assert r.status == "error"
        assert r.error == "Connection refused"
        assert r.quant_label == "Q5_K_M"


# ═══════════════════════════════════════════════════════════════════════════
# QuantSummary aggregation
# ═══════════════════════════════════════════════════════════════════════════

class TestQuantSummary:
    def _make_run(self, rtf, status="success", run_type="measured"):
        return bq.QuantRunResult(
            model_filename="test.gguf", quant_label="Q6_K",
            model_sha256="abc", model_size_bytes=1000,
            stride=4, run_index=1, run_type=run_type,
            time_to_headers_ms=3.0, time_to_first_pcm_ms=250.0,
            total_wall_ms=25000.0, pcm_bytes=2000000,
            audio_duration_ms=22000.0, real_time_factor=rtf,
            status=status,
        )

    def test_avg_rtf(self):
        mi = bq.ModelInfo.from_path("/models/test-q6_k.gguf")
        s = bq.QuantSummary(model=mi)
        s.runs = [self._make_run(1.10), self._make_run(1.15), self._make_run(1.12)]
        assert s.avg_rtf == pytest.approx((1.10 + 1.15 + 1.12) / 3)

    def test_min_max_rtf(self):
        mi = bq.ModelInfo.from_path("/models/test-q6_k.gguf")
        s = bq.QuantSummary(model=mi)
        s.runs = [self._make_run(1.10), self._make_run(1.05), self._make_run(1.20)]
        assert s.min_rtf == pytest.approx(1.05)
        assert s.max_rtf == pytest.approx(1.20)

    def test_error_runs_excluded(self):
        mi = bq.ModelInfo.from_path("/models/test-q6_k.gguf")
        s = bq.QuantSummary(model=mi)
        s.runs = [
            self._make_run(1.10),
            self._make_run(0, status="error"),
            self._make_run(1.12),
        ]
        assert len(s.success_runs) == 2
        assert s.avg_rtf == pytest.approx((1.10 + 1.12) / 2)

    def test_no_success_runs(self):
        mi = bq.ModelInfo.from_path("/models/test-q6_k.gguf")
        s = bq.QuantSummary(model=mi)
        s.runs = [self._make_run(0, status="error"), self._make_run(0, status="error")]
        assert s.avg_rtf is None
        assert s.avg_first_pcm_ms is None

    def test_warmup_runs_excluded(self):
        mi = bq.ModelInfo.from_path("/models/test-q6_k.gguf")
        s = bq.QuantSummary(model=mi)
        s.runs = [
            self._make_run(1.50, run_type="warmup"),
            self._make_run(1.10, run_type="measured"),
        ]
        assert len(s.success_runs) == 1
        assert s.avg_rtf == pytest.approx(1.10)


# ═══════════════════════════════════════════════════════════════════════════
# format_quant_summary output
# ═══════════════════════════════════════════════════════════════════════════

class TestFormatQuantSummary:
    def _base_results(self):
        return {
            "endpoint": "127.0.0.1:3032", "text_len": 361, "stride": 4,
            "codec_context": 4, "holdback": 0, "start_buffer_ms": 0,
            "low_latency": True, "sample_rate_hz": 44100,
            "warmup_runs": 1, "measured_runs": 3,
            "candidate_models": [
                {"filename": "s2-pro-q6_k.gguf", "quant_label": "Q6_K",
                 "sha256": "abc123def456", "size_bytes": 3200000000, "exists": True},
            ],
            "summaries": [],
        }

    def test_includes_listening_checklist(self):
        results = self._base_results()
        results["summaries"] = [{
            "quant": "Q6_K", "model_filename": "s2-pro-q6_k.gguf",
            "model_sha256": "abc", "model_size_bytes": 1000,
            "avg_rtf": 1.13, "min_rtf": 1.10, "max_rtf": 1.15,
            "avg_first_pcm_ms": 250.0, "avg_total_ms": 25000.0,
            "runs": [{"run": 1, "run_type": "measured", "status": "success",
                       "rtf": 1.10, "time_to_headers_ms": 3.0,
                       "time_to_first_pcm_ms": 245.0,
                       "total_wall_ms": 24800.0, "pcm_bytes": 2000000,
                       "audio_duration_ms": 22000.0, "error": "",
                       "pcm_path": "/tmp/test.pcm", "backend_metrics": None}],
        }]
        md = bq.format_quant_summary(results)
        assert "Listening Checklist" in md
        assert "Clicks / pops" in md
        assert "Robotic or metallic" in md
        assert "Voice consistency" in md

    def test_pcm_conversion_hint(self):
        results = self._base_results()
        md = bq.format_quant_summary(results)
        assert "ffmpeg" in md.lower()
        assert "-f s16le" in md


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

class TestHelpers:
    def test_sanitize_label(self):
        assert bq._sanitize_label("quant_q6_k_run1") == "quant_q6_k_run1"
        assert bq._sanitize_label("path/traversal") == "path_traversal"
        assert bq._sanitize_label("dots..in.name") == "dots.in.name"
        assert bq._sanitize_label("") == ""

    def test_pcm_duration(self):
        bytes_per_second = 44100 * 2
        dur = bq.pcm_duration_ms(bytes_per_second)
        assert dur == pytest.approx(1000.0, rel=0.01)
        dur = bq.pcm_duration_ms(bytes_per_second * 2)
        assert dur == pytest.approx(2000.0, rel=0.01)
        assert bq.pcm_duration_ms(0) == 0.0

    def test_real_time_factor(self):
        assert bq.real_time_factor(1000.0, 1000.0) == 1.0
        assert bq.real_time_factor(500.0, 1000.0) == 0.5
        assert bq.real_time_factor(2000.0, 1000.0) == 2.0
        assert bq.real_time_factor(1000.0, 0.0) == float("inf")

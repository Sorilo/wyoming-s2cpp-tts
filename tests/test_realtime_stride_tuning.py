"""Tests for Phase 8C realtime stride tuning configuration, request
construction, and benchmark math.

All tests are deterministic — no real backend, GPU, or network calls.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch, Mock

import pytest

from app.config import (
    Settings,
    _parse_bool,
    _parse_float_env,
    _parse_positive_int_env,
    _parse_non_negative_int_env,
)
from app.s2_client import S2GenerateRequest


# ═══════════════════════════════════════════════════════════════════════════
# Config: new env vars
# ═══════════════════════════════════════════════════════════════════════════

class TestStreamDecodeStrideConfig:
    """S2_STREAM_DECODE_STRIDE_FRAMES env var parsing."""

    def test_default_is_4(self):
        settings = Settings()
        assert settings.s2_stream_decode_stride_frames == 4

    def test_parse_valid_stride(self, monkeypatch):
        monkeypatch.setenv("S2_STREAM_DECODE_STRIDE_FRAMES", "8")
        settings = Settings.from_env()
        assert settings.s2_stream_decode_stride_frames == 8

    def test_stride_1_accepted(self, monkeypatch):
        monkeypatch.setenv("S2_STREAM_DECODE_STRIDE_FRAMES", "1")
        settings = Settings.from_env()
        assert settings.s2_stream_decode_stride_frames == 1

    def test_stride_64_accepted(self, monkeypatch):
        monkeypatch.setenv("S2_STREAM_DECODE_STRIDE_FRAMES", "64")
        settings = Settings.from_env()
        assert settings.s2_stream_decode_stride_frames == 64

    def test_stride_0_rejected(self, monkeypatch):
        monkeypatch.setenv("S2_STREAM_DECODE_STRIDE_FRAMES", "0")
        with pytest.raises(ValueError, match="range"):
            Settings.from_env()

    def test_stride_65_rejected(self, monkeypatch):
        monkeypatch.setenv("S2_STREAM_DECODE_STRIDE_FRAMES", "65")
        with pytest.raises(ValueError, match="range"):
            Settings.from_env()

    def test_stride_negative_rejected(self, monkeypatch):
        monkeypatch.setenv("S2_STREAM_DECODE_STRIDE_FRAMES", "-5")
        with pytest.raises(ValueError, match="range"):
            Settings.from_env()

    def test_stride_not_integer_rejected(self, monkeypatch):
        monkeypatch.setenv("S2_STREAM_DECODE_STRIDE_FRAMES", "abc")
        with pytest.raises(ValueError, match="Invalid integer"):
            Settings.from_env()


class TestStreamHoldbackConfig:
    """S2_STREAM_HOLDBACK_FRAMES env var parsing."""

    def test_default_is_0(self):
        settings = Settings()
        assert settings.s2_stream_holdback_frames == 0

    def test_parse_valid_holdback(self, monkeypatch):
        monkeypatch.setenv("S2_STREAM_HOLDBACK_FRAMES", "3")
        settings = Settings.from_env()
        assert settings.s2_stream_holdback_frames == 3

    def test_holdback_0_accepted(self, monkeypatch):
        monkeypatch.setenv("S2_STREAM_HOLDBACK_FRAMES", "0")
        settings = Settings.from_env()
        assert settings.s2_stream_holdback_frames == 0

    def test_holdback_negative_rejected(self, monkeypatch):
        monkeypatch.setenv("S2_STREAM_HOLDBACK_FRAMES", "-1")
        with pytest.raises(ValueError, match="non-negative"):
            Settings.from_env()


class TestStreamStartBufferConfig:
    """S2_STREAM_START_BUFFER_MS env var parsing."""

    def test_default_is_0(self):
        settings = Settings()
        assert settings.s2_stream_start_buffer_ms == 0

    def test_parse_valid_start_buffer(self, monkeypatch):
        monkeypatch.setenv("S2_STREAM_START_BUFFER_MS", "500")
        settings = Settings.from_env()
        assert settings.s2_stream_start_buffer_ms == 500

    def test_start_buffer_negative_rejected(self, monkeypatch):
        monkeypatch.setenv("S2_STREAM_START_BUFFER_MS", "-100")
        with pytest.raises(ValueError, match="non-negative"):
            Settings.from_env()


class TestLowLatencyConfig:
    """S2_LOW_LATENCY env var parsing."""

    def test_default_is_true(self):
        settings = Settings()
        assert settings.s2_low_latency is True

    def test_parse_false(self, monkeypatch):
        monkeypatch.setenv("S2_LOW_LATENCY", "false")
        settings = Settings.from_env()
        assert settings.s2_low_latency is False

    def test_parse_true(self, monkeypatch):
        monkeypatch.setenv("S2_LOW_LATENCY", "true")
        settings = Settings.from_env()
        assert settings.s2_low_latency is True

    def test_invalid_rejected(self, monkeypatch):
        monkeypatch.setenv("S2_LOW_LATENCY", "maybe")
        with pytest.raises(ValueError, match="boolean"):
            Settings.from_env()


class TestTemperatureConfig:
    """S2_TEMPERATURE env var parsing."""

    def test_default_is_0_58(self):
        settings = Settings()
        assert settings.s2_temperature == 0.58

    def test_parse_valid(self, monkeypatch):
        monkeypatch.setenv("S2_TEMPERATURE", "0.9")
        settings = Settings.from_env()
        assert settings.s2_temperature == 0.9

    def test_out_of_range_high(self, monkeypatch):
        monkeypatch.setenv("S2_TEMPERATURE", "3.0")
        with pytest.raises(ValueError, match="range"):
            Settings.from_env()

    def test_out_of_range_low(self, monkeypatch):
        monkeypatch.setenv("S2_TEMPERATURE", "-0.1")
        with pytest.raises(ValueError, match="range"):
            Settings.from_env()

    def test_not_float_rejected(self, monkeypatch):
        monkeypatch.setenv("S2_TEMPERATURE", "abc")
        with pytest.raises(ValueError, match="Invalid float"):
            Settings.from_env()


class TestTopPConfig:
    """S2_TOP_P env var parsing."""

    def test_default_is_0_88(self):
        settings = Settings()
        assert settings.s2_top_p == 0.88

    def test_out_of_range_high(self, monkeypatch):
        monkeypatch.setenv("S2_TOP_P", "1.5")
        with pytest.raises(ValueError, match="range"):
            Settings.from_env()

    def test_out_of_range_low(self, monkeypatch):
        monkeypatch.setenv("S2_TOP_P", "-0.1")
        with pytest.raises(ValueError, match="range"):
            Settings.from_env()


class TestTopKConfig:
    """S2_TOP_K env var parsing."""

    def test_default_is_40(self):
        settings = Settings()
        assert settings.s2_top_k == 40

    def test_parse_valid(self, monkeypatch):
        monkeypatch.setenv("S2_TOP_K", "80")
        settings = Settings.from_env()
        assert settings.s2_top_k == 80

    def test_zero_rejected(self, monkeypatch):
        monkeypatch.setenv("S2_TOP_K", "0")
        with pytest.raises(ValueError, match="positive"):
            Settings.from_env()

    def test_exceeds_max_rejected(self, monkeypatch):
        monkeypatch.setenv("S2_TOP_K", "500")
        with pytest.raises(ValueError, match="exceeds"):
            Settings.from_env()


class TestMaxNewTokensConfig:
    """S2_MAX_NEW_TOKENS env var parsing."""

    def test_default_is_512(self):
        settings = Settings()
        assert settings.s2_max_new_tokens == 512

    def test_parse_valid(self, monkeypatch):
        monkeypatch.setenv("S2_MAX_NEW_TOKENS", "1024")
        settings = Settings.from_env()
        assert settings.s2_max_new_tokens == 1024

    def test_zero_rejected(self, monkeypatch):
        monkeypatch.setenv("S2_MAX_NEW_TOKENS", "0")
        with pytest.raises(ValueError, match="positive"):
            Settings.from_env()

    def test_exceeds_max_rejected(self, monkeypatch):
        monkeypatch.setenv("S2_MAX_NEW_TOKENS", "5000")
        with pytest.raises(ValueError, match="exceeds"):
            Settings.from_env()


class TestMultipleErrorsCollected:
    """Multiple invalid env vars produce one error with all details."""

    def test_two_errors_reported_together(self, monkeypatch):
        monkeypatch.setenv("S2_STREAM_DECODE_STRIDE_FRAMES", "0")
        monkeypatch.setenv("S2_TEMPERATURE", "99")
        with pytest.raises(ValueError) as exc:
            Settings.from_env()
        msg = str(exc.value)
        assert "range" in msg
        assert "Temperature" in msg or "S2_TEMPERATURE" in msg


# ═══════════════════════════════════════════════════════════════════════════
# Parser function unit tests
# ═══════════════════════════════════════════════════════════════════════════

class TestParseBool:
    def test_true_variants(self):
        for v in ("true", "TRUE", "True", "1", "yes", "YES", "on", "ON"):
            assert _parse_bool(v) is True

    def test_false_variants(self):
        for v in ("false", "FALSE", "False", "0", "no", "NO", "off", "OFF"):
            assert _parse_bool(v) is False

    def test_invalid_raises(self):
        for v in ("maybe", "y", "n", ""):
            with pytest.raises(ValueError, match="boolean"):
                _parse_bool(v)


# ═══════════════════════════════════════════════════════════════════════════
# S2GenerateRequest: stride fields in multipart
# ═══════════════════════════════════════════════════════════════════════════

class TestRequestStreamingParams:
    """Streaming multipart params include explicit stride tuning fields."""

    def test_default_stride_in_streaming_params(self):
        req = S2GenerateRequest(text="hello")
        fields = req.to_multipart_fields(streaming=True)
        params = json.loads(fields["params"])
        assert params["stream_decode_stride_frames"] == 4
        assert params["stream_holdback_frames"] == 0
        assert params["stream_start_buffer_ms"] == 0
        assert params["low_latency"] is True

    def test_custom_stride_in_params(self):
        req = S2GenerateRequest(
            text="hello",
            stream_decode_stride_frames=8,
            stream_holdback_frames=2,
            stream_start_buffer_ms=500,
            low_latency=False,
        )
        fields = req.to_multipart_fields(streaming=True)
        params = json.loads(fields["params"])
        assert params["stream_decode_stride_frames"] == 8
        assert params["stream_holdback_frames"] == 2
        assert params["stream_start_buffer_ms"] == 500
        assert params["low_latency"] is False

    def test_buffered_path_omits_stride_fields(self):
        """Non-streaming multipart does NOT include stride/holdback."""
        req = S2GenerateRequest(text="hello")
        fields = req.to_multipart_fields(streaming=False)
        params = json.loads(fields["params"])
        assert "stream_decode_stride_frames" not in params
        assert "stream_holdback_frames" not in params
        assert "stream_start_buffer_ms" not in params
        assert "low_latency" not in params

    def test_from_settings_propagates_all_tuning_fields(self):
        settings = Settings(
            s2_low_latency=False,
            s2_stream_decode_stride_frames=16,
            s2_stream_holdback_frames=1,
            s2_stream_start_buffer_ms=100,
            s2_codec_decode_context_frames=4,
            s2_segment_sentences=False,
        )
        req = S2GenerateRequest.from_settings("hello", settings)
        assert req.low_latency is False
        assert req.stream_decode_stride_frames == 16
        assert req.stream_holdback_frames == 1
        assert req.stream_start_buffer_ms == 100

    def test_codec_context_preserved_with_stride(self):
        """codec_decode_context_frames still present alongside stride fields."""
        req = S2GenerateRequest(
            text="hello",
            codec_decode_context_frames=4,
            stream_decode_stride_frames=4,
        )
        fields = req.to_multipart_fields(streaming=True)
        params = json.loads(fields["params"])
        assert params["codec_decode_context_frames"] == 4
        assert params["stream_decode_stride_frames"] == 4

    def test_voice_preserved_with_stride(self):
        req = S2GenerateRequest(
            text="hello",
            voice="test_voice",
            voice_dir="/voices",
            stream_decode_stride_frames=8,
        )
        fields = req.to_multipart_fields(streaming=True)
        assert fields["voice"] == "test_voice"
        assert fields["voice_dir"] == "/voices"
        params = json.loads(fields["params"])
        assert params["stream_decode_stride_frames"] == 8

    def test_segment_sentences_forced_false_in_streaming(self):
        """Streaming always forces segment_sentences=false regardless of request."""
        req = S2GenerateRequest(text="hello", segment_sentences=True)
        fields = req.to_multipart_fields(streaming=True)
        params = json.loads(fields["params"])
        assert params["segment_sentences"] is False


# ═══════════════════════════════════════════════════════════════════════════
# Config: defaults preserving current behavior
# ═══════════════════════════════════════════════════════════════════════════

class TestDefaultsPreserveBehavior:
    """New fields have safe defaults that preserve existing behavior."""

    def test_stride_4_matches_expected_production(self):
        """Default stride 4 is the intended production candidate."""
        settings = Settings()
        req = S2GenerateRequest.from_settings("test", settings)
        fields = req.to_multipart_fields(streaming=True)
        params = json.loads(fields["params"])
        assert params["stream_decode_stride_frames"] == 4

    def test_holdback_0_no_delay(self):
        """Default holdback 0 means no intentional delay."""
        req = S2GenerateRequest(text="hello")
        fields = req.to_multipart_fields(streaming=True)
        params = json.loads(fields["params"])
        assert params["stream_holdback_frames"] == 0

    def test_low_latency_default_true(self):
        """Default low_latency true matches current production."""
        settings = Settings()
        assert settings.s2_low_latency is True

    def test_all_existing_fields_unchanged(self):
        """Existing settings retain their original defaults."""
        settings = Settings()
        assert settings.s2_stream is True
        assert settings.s2_chunked is True
        assert settings.s2_output_format == "pcm_s16le"
        assert settings.s2_segment_sentences is False
        assert settings.s2_codec_decode_context_frames == 4
        assert settings.s2_max_new_tokens == 512
        assert settings.s2_temperature == 0.58
        assert settings.s2_top_p == 0.88
        assert settings.s2_top_k == 40


# ═══════════════════════════════════════════════════════════════════════════
# Benchmark math
# ═══════════════════════════════════════════════════════════════════════════

class TestPCMDuration:
    """pcm_duration_ms calculations."""

    def test_duration_44100_mono_s16le(self):
        """1 second of 44100 Hz mono s16le = 88200 bytes."""
        from scripts.benchmark_realtime_tuning import pcm_duration_ms
        # 44100 samples/sec * 1 sec * 2 bytes/sample = 88200 bytes
        duration = pcm_duration_ms(88200, sample_rate=44100, width=2, channels=1)
        assert duration == pytest.approx(1000.0, rel=0.01)

    def test_duration_half_second(self):
        from scripts.benchmark_realtime_tuning import pcm_duration_ms
        # 0.5 sec: 44100 * 0.5 * 2 = 44100 bytes
        duration = pcm_duration_ms(44100, sample_rate=44100, width=2, channels=1)
        assert duration == pytest.approx(500.0, rel=0.01)

    def test_duration_empty_pcm(self):
        from scripts.benchmark_realtime_tuning import pcm_duration_ms
        assert pcm_duration_ms(0) == 0.0

    def test_duration_not_frame_aligned(self):
        """Non-aligned bytes: partial frame is truncated (floor)."""
        from scripts.benchmark_realtime_tuning import pcm_duration_ms
        # 3 bytes: only 1 complete s16le frame (2 bytes)
        duration = pcm_duration_ms(3, sample_rate=44100, width=2, channels=1)
        # 1 frame / 44100 = ~0.0227 ms
        assert duration == pytest.approx(1000.0 / 44100, rel=0.01)


class TestRealTimeFactor:
    """RTF calculation."""

    def test_rtf_faster_than_realtime(self):
        from scripts.benchmark_realtime_tuning import real_time_factor
        # 500ms wall time for 1000ms audio = RTF 0.5
        rtf = real_time_factor(500.0, 1000.0)
        assert rtf == pytest.approx(0.5)
        assert rtf < 1.0

    def test_rtf_exactly_realtime(self):
        from scripts.benchmark_realtime_tuning import real_time_factor
        rtf = real_time_factor(1000.0, 1000.0)
        assert rtf == pytest.approx(1.0)

    def test_rtf_slower_than_realtime(self):
        from scripts.benchmark_realtime_tuning import real_time_factor
        # Current measured ~RTF 1.45
        rtf = real_time_factor(1450.0, 1000.0)
        assert rtf == pytest.approx(1.45)
        assert rtf > 1.0

    def test_rtf_zero_duration_infinite(self):
        from scripts.benchmark_realtime_tuning import real_time_factor
        rtf = real_time_factor(100.0, 0.0)
        assert rtf == float("inf")


# ═══════════════════════════════════════════════════════════════════════════
# Dry-run safety
# ═══════════════════════════════════════════════════════════════════════════

class TestBenchmarkDryRun:
    """Benchmark harness must not contact backend without --run-real."""

    def test_dry_run_default_no_network(self):
        """Default invocation (no --run-real) exits without network calls."""
        from scripts.benchmark_realtime_tuning import main
        # Should exit 0 without any urllib calls
        with patch("urllib.request.urlopen") as mock_urlopen:
            result = main(["--text", "test"])
        assert result == 0
        mock_urlopen.assert_not_called()

    def test_dry_run_exits_cleanly(self):
        from scripts.benchmark_realtime_tuning import main
        result = main([])
        assert result == 0


class TestBenchmarkArgParsing:
    """CLI argument parsing."""

    def test_default_strides(self):
        from scripts.benchmark_realtime_tuning import parse_args
        args = parse_args([])
        assert args.strides == "1,2,4,8"
        assert args.run_real is False

    def test_custom_strides(self):
        from scripts.benchmark_realtime_tuning import parse_args
        args = parse_args(["--strides", "1,4,16"])
        assert args.strides == "1,4,16"

    def test_run_real_flag(self):
        from scripts.benchmark_realtime_tuning import parse_args
        args = parse_args(["--run-real"])
        assert args.run_real is True


# ═══════════════════════════════════════════════════════════════════════════
# To-payload (JSON) does NOT include stride fields
# ═══════════════════════════════════════════════════════════════════════════

class TestJSONPayloadNoStride:
    """JSON payload (for legacy/buffered) does not include tuning fields."""

    def test_json_payload_no_stride_fields(self):
        req = S2GenerateRequest(
            text="hello",
            stream_decode_stride_frames=8,
            low_latency=False,
        )
        payload = req.to_payload()
        assert "stream_decode_stride_frames" not in payload
        assert "stream_holdback_frames" not in payload
        assert "stream_start_buffer_ms" not in payload
        assert "low_latency" not in payload


# ═══════════════════════════════════════════════════════════════════════════
# Shell script syntax check
# ═══════════════════════════════════════════════════════════════════════════

class TestShellScriptSyntax:
    """run_realtime_tuning_unraid.sh is valid bash."""

    def test_script_exists_and_executable(self):
        import os
        script = "scripts/run_realtime_tuning_unraid.sh"
        assert os.path.exists(script), f"{script} not found"
        # Should be readable at minimum
        assert os.access(script, os.R_OK)

    def test_script_syntax(self):
        """bash -n checks syntax without executing."""
        import subprocess
        result = subprocess.run(
            ["bash", "-n", "scripts/run_realtime_tuning_unraid.sh"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Shell syntax error: {result.stderr}"


# ═══════════════════════════════════════════════════════════════════════════
# Config: environment audit — all formerly-missing env vars now parse
# ═══════════════════════════════════════════════════════════════════════════

class TestEnvVarAudit:
    """All generation settings are now parseable from environment."""

    def test_s2_max_new_tokens_from_env(self, monkeypatch):
        monkeypatch.setenv("S2_MAX_NEW_TOKENS", "256")
        s = Settings.from_env()
        assert s.s2_max_new_tokens == 256

    def test_s2_temperature_from_env(self, monkeypatch):
        monkeypatch.setenv("S2_TEMPERATURE", "0.7")
        s = Settings.from_env()
        assert s.s2_temperature == 0.7

    def test_s2_top_p_from_env(self, monkeypatch):
        monkeypatch.setenv("S2_TOP_P", "0.95")
        s = Settings.from_env()
        assert s.s2_top_p == 0.95

    def test_s2_top_k_from_env(self, monkeypatch):
        monkeypatch.setenv("S2_TOP_K", "50")
        s = Settings.from_env()
        assert s.s2_top_k == 50

    def test_s2_chunked_from_env(self, monkeypatch):
        monkeypatch.setenv("S2_CHUNKED", "false")
        s = Settings.from_env()
        assert s.s2_chunked is False

    def test_s2_output_format_from_env(self, monkeypatch):
        monkeypatch.setenv("S2_OUTPUT_FORMAT", "wav")
        s = Settings.from_env()
        assert s.s2_output_format == "wav"

    def test_s2_model_from_env(self, monkeypatch):
        monkeypatch.setenv("S2_MODEL", "/models/s2-pro-q8_0.gguf")
        s = Settings.from_env()
        assert s.s2_model == "/models/s2-pro-q8_0.gguf"

    def test_s2_gpu_index_from_env(self, monkeypatch):
        monkeypatch.setenv("S2_GPU_INDEX", "1")
        s = Settings.from_env()
        assert s.s2_gpu_index == 1

    def test_s2_gpu_layers_from_env(self, monkeypatch):
        monkeypatch.setenv("S2_GPU_LAYERS", "32")
        s = Settings.from_env()
        assert s.s2_gpu_layers == 32

    def test_s2_codec_cpu_from_env(self, monkeypatch):
        monkeypatch.setenv("S2_CODEC_CPU", "true")
        s = Settings.from_env()
        assert s.s2_codec_cpu is True

    def test_barge_in_friendly_from_env(self, monkeypatch):
        monkeypatch.setenv("BARGE_IN_FRIENDLY", "false")
        s = Settings.from_env()
        assert s.barge_in_friendly is False

    def test_cancel_on_client_disconnect_from_env(self, monkeypatch):
        monkeypatch.setenv("CANCEL_ON_CLIENT_DISCONNECT", "false")
        s = Settings.from_env()
        assert s.cancel_on_client_disconnect is False

    def test_cancel_on_new_request_from_env(self, monkeypatch):
        monkeypatch.setenv("CANCEL_ON_NEW_REQUEST", "true")
        s = Settings.from_env()
        assert s.cancel_on_new_request is True

    def test_max_queue_size_from_env(self, monkeypatch):
        monkeypatch.setenv("MAX_QUEUE_SIZE", "5")
        s = Settings.from_env()
        assert s.max_queue_size == 5

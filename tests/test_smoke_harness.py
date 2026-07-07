"""Tests for the Phase 5.5 smoke harness (app/smoke_harness.py).

All tests are mocked — no real backend is contacted.  The test suite
covers opt-in gates, WAV validation, PCM frame alignment, streaming
progressive classification, audio-header parsing, error categorisation,
structured output, and the full orchestrator path.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from app.config import Settings
from app.s2_client import S2ClientError
from app.smoke_harness import (
    BufferedMultipartResult,
    LegacyJsonResult,
    SmokeConfig,
    SmokeReport,
    StreamingMultipartResult,
    _categorize_error,
    _classify_progressive,
    _parse_audio_headers,
    _redact_endpoint,
    _validate_pcm_frame_alignment,
    _validate_wav_header,
    format_summary,
    run_smoke_harness,
)


# ============================================================================
# WAV header validation
# ============================================================================

def _wav_header(sample_rate: int = 22050, channels: int = 1, data_size: int = 0) -> bytes:
    """Build a minimal 44-byte PCM WAV header."""
    import struct
    byte_rate = sample_rate * channels * 2
    block_align = channels * 2
    return (
        b"RIFF"
        + struct.pack("<I", 36 + data_size)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", 16)        # subchunk1 size
        + struct.pack("<H", 1)         # PCM format
        + struct.pack("<H", channels)
        + struct.pack("<I", sample_rate)
        + struct.pack("<I", byte_rate)
        + struct.pack("<H", block_align)
        + struct.pack("<H", 16)        # bits per sample
        + b"data"
        + struct.pack("<I", data_size)
    )


class TestValidateWavHeader:
    def test_valid_wav_header(self):
        header = _wav_header()
        assert _validate_wav_header(header) is True

    def test_wav_with_audio_data(self):
        header = _wav_header(data_size=4)
        audio = header + b"\x00\x00\x00\x00"
        assert _validate_wav_header(audio) is True

    def test_non_wav_bytes(self):
        assert _validate_wav_header(b"hello world, this is not a WAV") is False

    def test_empty_bytes(self):
        assert _validate_wav_header(b"") is False

    def test_too_short_for_wav_header(self):
        assert _validate_wav_header(b"RIFF\x00\x00\x00\x00WAVE") is False  # 12 bytes

    def test_missing_riff(self):
        bad = bytearray(_wav_header())
        bad[0:4] = b"XXXX"
        assert _validate_wav_header(bytes(bad)) is False

    def test_missing_wave(self):
        bad = bytearray(_wav_header())
        bad[8:12] = b"XXXX"
        assert _validate_wav_header(bytes(bad)) is False


# ============================================================================
# PCM frame-alignment validation
# ============================================================================

class TestPcmFrameAlignment:
    def test_frame_aligned_mono(self):
        # 2 bytes per sample × 1 channel = 2-byte frames
        assert _validate_pcm_frame_alignment(200, channels=1, width=2) is True

    def test_not_frame_aligned_mono(self):
        assert _validate_pcm_frame_alignment(201, channels=1, width=2) is False

    def test_frame_aligned_stereo(self):
        # 2 bytes × 2 channels = 4-byte frames
        assert _validate_pcm_frame_alignment(400, channels=2, width=2) is True

    def test_not_frame_aligned_stereo(self):
        assert _validate_pcm_frame_alignment(401, channels=2, width=2) is False

    def test_zero_bytes_always_aligned(self):
        assert _validate_pcm_frame_alignment(0, channels=1, width=2) is True

    def test_invalid_frame_size(self):
        assert _validate_pcm_frame_alignment(100, channels=0, width=2) is False


# ============================================================================
# Streaming progressive classification
# ============================================================================

class TestClassifyProgressive:
    def test_verified_progressive_two_reads(self):
        assert _classify_progressive(2, eof_reached=True) == "verified_progressive"

    def test_verified_progressive_many_reads(self):
        assert _classify_progressive(10, eof_reached=True) == "verified_progressive"

    def test_inconclusive_one_read_with_eof(self):
        assert (
            _classify_progressive(1, eof_reached=True)
            == "audio_received_but_progressiveness_inconclusive"
        )

    def test_failed_zero_reads(self):
        assert _classify_progressive(0, eof_reached=True) == "failed"

    def test_failed_one_read_no_eof(self):
        # One read but stream didn't end — something went wrong
        assert _classify_progressive(1, eof_reached=False) == "failed"


# ============================================================================
# Audio header parsing
# ============================================================================

class TestParseAudioHeaders:
    def test_all_headers_present_valid(self):
        headers = {
            "x-audio-sample-rate": "24000",
            "x-audio-channels": "1",
            "x-audio-encoding": "pcm_s16le",
        }
        rate, channels, enc, missing = _parse_audio_headers(headers)
        assert rate == 24000
        assert channels == 1
        assert enc == "pcm_s16le"
        assert missing == []

    def test_missing_all_headers(self):
        rate, channels, enc, missing = _parse_audio_headers({})
        assert rate is None
        assert channels is None
        assert enc is None
        assert "x-audio-sample-rate" in missing
        assert "x-audio-channels" in missing
        assert "x-audio-encoding" in missing

    def test_unparseable_sample_rate(self):
        headers = {"x-audio-sample-rate": "not-a-number"}
        rate, _, _, missing = _parse_audio_headers(headers)
        assert rate is None
        assert "x-audio-sample-rate (unparseable)" in missing

    def test_unparseable_channels(self):
        headers = {"x-audio-channels": "two"}
        _, channels, _, missing = _parse_audio_headers(headers)
        assert channels is None
        assert "x-audio-channels (unparseable)" in missing

    def test_case_insensitive_lookup(self):
        headers = {
            "X-Audio-Sample-Rate": "16000",
            "X-AUDIO-CHANNELS": "1",
            "X-Audio-Encoding": "pcm_s16le",
        }
        rate, channels, enc, missing = _parse_audio_headers(headers)
        assert rate == 16000
        assert channels == 1
        assert enc == "pcm_s16le"
        assert missing == []


# ============================================================================
# Error categorisation
# ============================================================================

class TestCategorizeError:
    def test_connection_refused(self):
        assert _categorize_error(S2ClientError("Connection refused")) == "connection_refused"

    def test_timeout(self):
        assert _categorize_error(S2ClientError("timed out")) == "timeout"

    def test_dns_failure(self):
        assert _categorize_error(S2ClientError("Name or service not known")) == "dns_resolution_failure"

    def test_unknown(self):
        assert _categorize_error(S2ClientError("something weird happened")) == "unknown"


# ============================================================================
# Endpoint redaction
# ============================================================================

class TestRedactEndpoint:
    def test_clean_url_passes_through(self):
        assert _redact_endpoint("http://127.0.0.1:3030/generate") == "http://127.0.0.1:3030/generate"

    def test_query_params_stripped(self):
        assert _redact_endpoint("http://host/generate?token=secret") == "http://host/generate"

    def test_credentials_stripped(self):
        result = _redact_endpoint("http://user:pass@host:3030/generate")
        assert "user" not in result
        assert "pass" not in result
        assert result == "http://host:3030/generate"


# ============================================================================
# SmokeConfig
# ============================================================================

class TestSmokeConfig:
    def test_endpoint_from_settings(self):
        settings = Settings(s2_host="10.0.0.1", s2_port=8080)
        config = SmokeConfig()
        assert config.endpoint(settings) == "http://10.0.0.1:8080/generate"

    def test_endpoint_override(self):
        settings = Settings()  # default 127.0.0.1:3030
        config = SmokeConfig(endpoint_override="192.168.1.45:9090")
        assert config.endpoint(settings) == "http://192.168.1.45:9090/generate"

    def test_endpoint_override_default_port(self):
        settings = Settings()
        config = SmokeConfig(endpoint_override="10.0.0.5")
        assert config.endpoint(settings) == "http://10.0.0.5:3030/generate"


# ============================================================================
# Opt-in gate tests (no backend contact)
# ============================================================================

class TestOptInGate:
    def test_skipped_when_run_real_is_false(self):
        config = SmokeConfig(run_real=False)
        settings = Settings()

        report = run_smoke_harness(
            config, settings, repo_root=Path.cwd(), now_iso="2026-07-07T00:00:00Z",
        )

        assert report.overall_status == "skipped"
        assert report.phase_5_5a_status == "harness_ready_backend_not_tested"
        assert report.phase_5_5b_status == "pending"
        assert report.buffered_multipart is None
        assert report.streaming_multipart is None
        assert report.legacy_json is None

    def test_skipped_report_contains_endpoint(self):
        config = SmokeConfig(run_real=False)
        settings = Settings(s2_host="10.0.0.1", s2_port=9999)

        report = run_smoke_harness(
            config, settings, repo_root=Path.cwd(), now_iso="2026-07-07T00:00:00Z",
        )

        assert "10.0.0.1:9999" in report.configured_endpoint

    def test_no_sensitive_data_in_skipped_json(self):
        config = SmokeConfig(run_real=False)
        settings = Settings(s2_host="10.0.0.1")

        report = run_smoke_harness(
            config, settings, repo_root=Path.cwd(), now_iso="2026-07-07T00:00:00Z",
        )

        j = report.to_json()
        assert "s2_host" not in j.lower() or "10.0.0.1" in j  # endpoint is fine,
        # but ensure no raw Settings fields leak
        assert "s2_port" not in j


# ============================================================================
# Unavailable backend tests
# ============================================================================

class TestUnavailableBackend:
    def test_reports_unavailable_when_tcp_connect_fails(self):
        config = SmokeConfig(run_real=True)
        settings = Settings()

        def _probe_unreachable(_host, _port, timeout):
            return False

        report = run_smoke_harness(
            config, settings,
            repo_root=Path.cwd(),
            now_iso="2026-07-07T00:00:00Z",
            _probe_fn=_probe_unreachable,
        )

        assert report.overall_status == "unavailable"
        assert report.phase_5_5b_status == "pending"
        assert "unreachable" in " ".join(report.warnings).lower()

    def test_require_backend_adds_hard_failure_warning(self):
        config = SmokeConfig(run_real=True, require_backend=True)
        settings = Settings()

        def _probe_unreachable(_host, _port, timeout):
            return False

        report = run_smoke_harness(
            config, settings,
            repo_root=Path.cwd(),
            now_iso="2026-07-07T00:00:00Z",
            _probe_fn=_probe_unreachable,
        )

        assert report.overall_status == "unavailable"
        assert any("require_backend" in w.lower() for w in report.warnings)


# ============================================================================
# Mock helpers for real-backend simulation
# ============================================================================

def _mock_s2client_with_results(buffered_audio, stream_chunks, stream_headers=None):
    """Build a mock S2Client that returns controlled buffered + streaming results."""
    from app.s2_client import S2GenerateResult

    class MockStream:
        def __init__(self):
            self._chunks = list(stream_chunks)
            self._idx = 0
            self._closed = False
            self.status_code = 200
            self.content_type = "audio/L16"
            self.response_headers = (stream_headers or {}).copy()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self._closed = True
            return False

        def __iter__(self):
            return self

        def __next__(self):
            if self._idx >= len(self._chunks):
                raise StopIteration
            chunk = self._chunks[self._idx]
            self._idx += 1
            return chunk

    class MockClient:
        def __init__(self, *args, **kwargs):
            pass

        def generate_multipart(self, request, files=None, boundary=None):
            return S2GenerateResult(audio=buffered_audio, content_type="audio/wav")

        def generate_stream(self, request, files=None, boundary=None):
            return MockStream()

        def generate(self, request):
            raise S2ClientError("HTTP Error 415: Unsupported Media Type")

    return MockClient


# ============================================================================
# Real-backend simulation tests (Phase 5.5B-style but mocked)
# ============================================================================

class TestRealBackendSimulation:
    """Simulate a reachable backend with mocked HTTP responses."""

    def _harness_with_backend(
        self, buffered_audio, stream_chunks, stream_headers=None,
    ):
        config = SmokeConfig(run_real=True, text="test")
        settings = Settings()

        def _probe_ok(_host, _port, timeout):
            return True

        MockClient = _mock_s2client_with_results(
            buffered_audio, stream_chunks, stream_headers
        )

        with patch("app.smoke_harness.S2Client", MockClient):
            return run_smoke_harness(
                config, settings,
                repo_root=Path.cwd(),
                now_iso="2026-07-07T00:00:00Z",
                _probe_fn=_probe_ok,
            )

    def test_buffered_wav_success(self):
        wav = _wav_header(data_size=4) + b"\x00\x00\x00\x00"
        pcm_chunk = b"\x00\x01\x02\x03\x04\x05\x06\x07"

        report = self._harness_with_backend(wav, [pcm_chunk])

        assert report.buffered_multipart is not None
        b = report.buffered_multipart
        assert b.status == "success"
        assert b.wav_header_valid is True
        assert b.audio_non_empty is True
        assert b.response_byte_count > 0

    def test_buffered_non_wav_response(self):
        """Response has audio bytes but no valid WAV header."""
        non_wav = b"some raw bytes, not a wav file at all"
        pcm = b"\x00\x01\x02\x03"

        report = self._harness_with_backend(non_wav, [pcm])

        assert report.buffered_multipart is not None
        b = report.buffered_multipart
        assert b.status == "success"  # HTTP succeeded
        assert b.wav_header_valid is False
        assert b.audio_non_empty is True
        assert any("no valid WAV header" in w for w in report.warnings)

    def test_buffered_empty_response(self):
        """Response with zero audio bytes."""
        pcm = b"\x00\x01\x02\x03"

        report = self._harness_with_backend(b"", [pcm])

        b = report.buffered_multipart
        assert b.audio_non_empty is False
        assert b.wav_header_valid is None  # not checked when empty

    def test_streaming_progressive(self):
        """Multiple non-empty reads → verified_progressive."""
        wav = _wav_header(data_size=4) + b"\x00\x00\x00\x00"
        chunks = [b"\x00\x01", b"\x02\x03", b"\x04\x05", b"\x06\x07"]

        report = self._harness_with_backend(wav, chunks)

        s = report.streaming_multipart
        assert s is not None
        assert s.status == "success"
        assert s.non_empty_read_count == 4
        assert s.total_pcm_bytes == 8
        assert s.progressive_classification == "verified_progressive"
        assert s.eof_reached is True
        assert s.stream_closed_cleanly is True

    def test_streaming_inconclusive_single_read(self):
        """One non-empty read → inconclusive."""
        wav = _wav_header(data_size=2) + b"\x00\x00"
        chunks = [b"\x00\x01\x02\x03\x04\x05\x06\x07"]

        report = self._harness_with_backend(wav, chunks)

        s = report.streaming_multipart
        assert s.progressive_classification == "audio_received_but_progressiveness_inconclusive"
        assert any("inconclusive" in w for w in report.warnings)

    def test_streaming_empty_response(self):
        """No audio chunks received."""
        wav = _wav_header(data_size=0)

        report = self._harness_with_backend(wav, [])

        s = report.streaming_multipart
        assert s.total_pcm_bytes == 0
        assert s.non_empty_read_count == 0
        assert s.progressive_classification == "failed"

    def test_streaming_audio_headers(self):
        """Streaming response headers are parsed and validated."""
        wav = _wav_header(data_size=4) + b"\x00\x00\x00\x00"
        chunks = [b"\x00\x01", b"\x02\x03"]
        headers = {
            "x-audio-sample-rate": "24000",
            "x-audio-channels": "1",
            "x-audio-encoding": "pcm_s16le",
        }

        report = self._harness_with_backend(wav, chunks, stream_headers=headers)

        s = report.streaming_multipart
        assert s.audio_sample_rate == 24000
        assert s.audio_channels == 1
        assert s.audio_encoding == "pcm_s16le"
        assert s.missing_audio_headers == []

    def test_streaming_missing_audio_headers(self):
        """Missing headers are reported in warnings."""
        wav = _wav_header(data_size=4) + b"\x00\x00\x00\x00"
        chunks = [b"\x00\x01", b"\x02\x03"]

        report = self._harness_with_backend(wav, chunks, stream_headers={})

        s = report.streaming_multipart
        assert len(s.missing_audio_headers) > 0
        assert any("missing streaming audio headers" in w for w in report.warnings)

    def test_pcm_frame_aligned_with_valid_headers(self):
        """When channels known, PCM bytes must be frame-aligned."""
        wav = _wav_header(data_size=4) + b"\x00\x00\x00\x00"
        # 8 bytes = 4 frames for mono (2 bytes/frame)
        chunks = [b"\x00\x01\x02\x03\x04\x05\x06\x07"]
        headers = {
            "x-audio-sample-rate": "24000",
            "x-audio-channels": "1",
            "x-audio-encoding": "pcm_s16le",
        }

        report = self._harness_with_backend(wav, chunks, stream_headers=headers)

        s = report.streaming_multipart
        assert s.pcm_frame_aligned is True

    def test_pcm_not_frame_aligned(self):
        """Odd number of bytes with mono → not frame-aligned."""
        wav = _wav_header(data_size=3) + b"\x00\x00\x00"
        # 7 bytes with mono (2-byte frames) = not aligned
        chunks = [b"\x00\x01\x02\x03\x04\x05\x07"]
        headers = {
            "x-audio-sample-rate": "24000",
            "x-audio-channels": "1",
            "x-audio-encoding": "pcm_s16le",
        }

        report = self._harness_with_backend(wav, chunks, stream_headers=headers)

        s = report.streaming_multipart
        assert s.pcm_frame_aligned is False

    def test_pcm_frame_aligned_stereo(self):
        """Stereo (4-byte frames) must be aligned to 4 bytes."""
        wav = _wav_header(data_size=8, channels=2) + b"\x00" * 8
        chunks = [b"\x00\x01\x02\x03\x04\x05\x06\x07"]
        headers = {
            "x-audio-sample-rate": "24000",
            "x-audio-channels": "2",
            "x-audio-encoding": "pcm_s16le",
        }

        report = self._harness_with_backend(wav, chunks, stream_headers=headers)

        s = report.streaming_multipart
        assert s.pcm_frame_aligned is True

    def test_phase_5_5b_verified_on_full_success(self):
        """Both buffered WAV + streaming audio → real_backend_verified."""
        wav = _wav_header(data_size=4) + b"\x00\x00\x00\x00"
        chunks = [b"\x00\x01", b"\x02\x03"]
        headers = {
            "x-audio-sample-rate": "24000",
            "x-audio-channels": "1",
            "x-audio-encoding": "pcm_s16le",
        }

        report = self._harness_with_backend(wav, chunks, stream_headers=headers)

        assert report.phase_5_5b_status == "real_backend_verified"

    def test_phase_5_5b_failed_when_buffered_no_wav(self):
        """No valid WAV → real_backend_failed."""
        non_wav = b"not a wav"
        chunks = [b"\x00\x01", b"\x02\x03"]

        report = self._harness_with_backend(non_wav, chunks)

        assert report.phase_5_5b_status == "real_backend_failed"

    def test_phase_5_5b_failed_when_streaming_empty(self):
        """No streaming audio → real_backend_failed."""
        wav = _wav_header(data_size=4) + b"\x00\x00\x00\x00"

        report = self._harness_with_backend(wav, [])

        assert report.phase_5_5b_status == "real_backend_failed"


# ============================================================================
# Legacy JSON probe tests
# ============================================================================

class TestLegacyJsonProbe:
    def _harness_with_legacy(self, probe=True):
        config = SmokeConfig(run_real=True, probe_legacy_json=probe)
        settings = Settings()

        def _probe_ok(_host, _port, timeout):
            return True

        wav = _wav_header(data_size=4) + b"\x00\x00\x00\x00"
        pcm = [b"\x00\x01", b"\x02\x03"]
        MockClient = _mock_s2client_with_results(wav, pcm)

        with patch("app.smoke_harness.S2Client", MockClient):
            return run_smoke_harness(
                config, settings,
                repo_root=Path.cwd(),
                now_iso="2026-07-07T00:00:00Z",
                _probe_fn=_probe_ok,
            )

    def test_legacy_json_probe_unsupported(self):
        """Mock client raises on JSON → expected unsupported."""
        report = self._harness_with_legacy(probe=True)

        assert report.legacy_json is not None
        assert report.legacy_json.status == "unsupported"

    def test_legacy_json_not_run_by_default(self):
        """Without --probe-legacy-json, no JSON probe."""
        report = self._harness_with_legacy(probe=False)

        assert report.legacy_json is None


# ============================================================================
# Structured output tests
# ============================================================================

class TestStructuredOutput:
    def test_report_to_json_is_valid(self):
        config = SmokeConfig(run_real=False)
        settings = Settings()

        report = run_smoke_harness(
            config, settings, repo_root=Path.cwd(), now_iso="2026-07-07T00:00:00Z",
        )

        j = report.to_json()
        parsed = json.loads(j)

        assert parsed["overall_status"] == "skipped"
        assert parsed["phase_5_5a_status"] == "harness_ready_backend_not_tested"
        assert parsed["phase_5_5b_status"] == "pending"
        assert parsed["buffered_multipart"] is None
        assert parsed["streaming_multipart"] is None

    def test_report_json_excludes_sensitive_data(self):
        config = SmokeConfig(run_real=False)
        settings = Settings(s2_host="secret-host.internal")

        report = run_smoke_harness(
            config, settings, repo_root=Path.cwd(), now_iso="2026-07-07T00:00:00Z",
        )

        j = report.to_json()
        # The endpoint is redacted — should not contain raw credentials
        # (our endpoints don't carry them, but verify no Settings fields leak)
        assert "tts_backend" not in j
        assert "s2_host" not in j
        assert "s2_port" not in j

    def test_report_to_dict_contains_required_fields(self):
        config = SmokeConfig(run_real=False)
        settings = Settings()

        report = run_smoke_harness(
            config, settings, repo_root=Path.cwd(), now_iso="2026-07-07T00:00:00Z",
        )

        d = report.to_dict()
        for field in [
            "overall_status",
            "phase_5_5a_status",
            "phase_5_5b_status",
            "timestamp",
            "git_commit",
            "configured_endpoint",
            "warnings",
        ]:
            assert field in d, f"missing required field: {field}"


# ============================================================================
# format_summary tests
# ============================================================================

class TestFormatSummary:
    def test_skipped_report_summary(self):
        config = SmokeConfig(run_real=False)
        settings = Settings()

        report = run_smoke_harness(
            config, settings, repo_root=Path.cwd(), now_iso="2026-07-07T00:00:00Z",
        )

        summary = format_summary(report)
        assert "skipped" in summary.lower()
        assert "Phase 5.5" in summary

    def test_unavailable_report_summary(self):
        config = SmokeConfig(run_real=True)
        settings = Settings()

        def _probe_unreachable(_host, _port, timeout):
            return False

        report = run_smoke_harness(
            config, settings,
            repo_root=Path.cwd(),
            now_iso="2026-07-07T00:00:00Z",
            _probe_fn=_probe_unreachable,
        )

        summary = format_summary(report)
        assert "unavailable" in summary.lower()

    def test_completed_report_summary(self):
        wav = _wav_header(data_size=4) + b"\x00\x00\x00\x00"
        chunks = [b"\x00\x01", b"\x02\x03"]
        headers = {
            "x-audio-sample-rate": "24000",
            "x-audio-channels": "1",
            "x-audio-encoding": "pcm_s16le",
        }
        MockClient = _mock_s2client_with_results(wav, chunks, headers)

        config = SmokeConfig(run_real=True)
        settings = Settings()

        def _probe_ok(_host, _port, timeout):
            return True

        with patch("app.smoke_harness.S2Client", MockClient):
            report = run_smoke_harness(
                config, settings,
                repo_root=Path.cwd(),
                now_iso="2026-07-07T00:00:00Z",
                _probe_fn=_probe_ok,
            )

        summary = format_summary(report)
        assert "completed" in summary.lower()
        assert "Buffered Multipart" in summary
        assert "Streaming Multipart" in summary


# ============================================================================
# BufferedMultipartResult / StreamingMultipartResult dataclass tests
# ============================================================================

class TestResultDataclasses:
    def test_buffered_result_to_dict(self):
        r = BufferedMultipartResult(
            status="success",
            endpoint="http://127.0.0.1:3030/generate",
            http_status=200,
            content_type="audio/wav",
            response_byte_count=1024,
            audio_non_empty=True,
            wav_header_valid=True,
            duration_ms=150.5,
        )
        d = r.to_dict()
        assert d["status"] == "success"
        assert d["wav_header_valid"] is True

    def test_streaming_result_to_dict(self):
        r = StreamingMultipartResult(
            status="success",
            endpoint="http://127.0.0.1:3030/generate",
            non_empty_read_count=3,
            total_pcm_bytes=8192,
            progressive_classification="verified_progressive",
            pcm_frame_aligned=True,
        )
        d = r.to_dict()
        assert d["progressive_classification"] == "verified_progressive"
        assert d["pcm_frame_aligned"] is True

    def test_legacy_json_result_to_dict(self):
        r = LegacyJsonResult(status="unsupported", error_category="http_error")
        d = r.to_dict()
        assert d["status"] == "unsupported"
        assert d["error_category"] == "http_error"


# ============================================================================
# Timeout / error cleanup test
# ============================================================================

class TestTimeoutErrorCleanup:
    def test_buffered_error_sets_failure_with_category(self):
        """When generate_multipart raises S2ClientError, result is failure."""

        class FailingClient:
            def __init__(self, *args, **kwargs):
                pass

            def generate_multipart(self, request, files=None, boundary=None):
                raise S2ClientError("Connection refused")

            def generate_stream(self, request, files=None, boundary=None):
                raise S2ClientError("Connection refused")

            def generate(self, request):
                raise S2ClientError("HTTP Error 415")

        config = SmokeConfig(run_real=True)
        settings = Settings()

        def _probe_ok(_host, _port, timeout):
            return True

        with patch("app.smoke_harness.S2Client", FailingClient):
            report = run_smoke_harness(
                config, settings,
                repo_root=Path.cwd(),
                now_iso="2026-07-07T00:00:00Z",
                _probe_fn=_probe_ok,
            )

        assert report.buffered_multipart.status == "failure"
        assert report.buffered_multipart.error_category == "connection_refused"
        assert report.streaming_multipart.status == "failure"
        assert report.streaming_multipart.error_category == "connection_refused"


# ============================================================================
# No real backend contacted during ordinary test suite
# ============================================================================

class TestNoRealBackendContact:
    """Verify that the harness never contacts a real backend without opt-in."""

    def test_run_real_false_never_calls_probe(self):
        """When run_real is False, the probe function is never called."""
        config = SmokeConfig(run_real=False)
        settings = Settings()

        probe_called = []

        def _probe(host, port, timeout):
            probe_called.append((host, port))
            return False

        run_smoke_harness(
            config, settings,
            repo_root=Path.cwd(),
            now_iso="2026-07-07T00:00:00Z",
            _probe_fn=_probe,
        )

        assert len(probe_called) == 0, "probe was called without --run-real!"

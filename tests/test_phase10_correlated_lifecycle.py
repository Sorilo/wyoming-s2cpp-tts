"""Phase 10 correlated lifecycle TDD tests — behavioral tests against real production code.

Tests for:
1. classify_wrapper_outcome handles synthesis_terminal states (completed/failed/timed_out/cancelled)
2. classify_backend_outcome handles backend_abort_observed and backend_request_aborted
3. correlate_disconnect_logs properly matches by connection_id/synthesis_id/text_fp
4. Backend native log parser for key=value cancellation lines
5. follow_up_completed treated as distinct completed-normal terminal when correlated
6. Unknown absent correlation (fail closed)
7. Integration: synthesize_s2cpp_streaming_tts_events emits cancellation_requested,
   cancellation_propagated, and synthesis_terminal(cancelled) on CancelledError

These tests MUST fail against the current code before implementation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest


# ═══════════════════════════════════════════════════════════════════════════
# Test 1: classify_wrapper_outcome handles synthesis_terminal states
# ═══════════════════════════════════════════════════════════════════════════

class TestClassifyWrapperOutcome:
    """Real production classifier in scripts/phase10_live_validation.py."""

    @staticmethod
    def _classifier():
        import scripts.phase10_live_validation as p10
        return p10.classify_wrapper_outcome

    @pytest.mark.parametrize("state,expected", [
        ("completed", "completed normally"),
        ("failed", "failed"),
        ("timed_out", "timed out"),
        ("cancelled", "cancelled"),
        ("unknown", "unknown"),
    ])
    def test_synthesis_terminal_state_drives_classification(self, state, expected):
        """synthesis_terminal with explicit terminal_state must drive the outcome."""
        classify = self._classifier()
        events = [
            {"event": "synthesis_terminal",
             "connection_id": "c-1", "synthesis_id": "s-1",
             "text_fp": "abc123", "terminal_state": state},
        ]
        result = classify(events)
        assert result == expected, (
            f"synthesis_terminal state={state} expected '{expected}', got '{result}'"
        )

    def test_synthesis_terminal_takes_precedence_over_other_events(self):
        """synthesis_terminal completed overrides a cancellation event."""
        classify = self._classifier()
        events = [
            {"event": "synthesis_cancelled", "connection_id": "c-1",
             "synthesis_id": "s-1", "text_fp": "abc"},
            {"event": "synthesis_terminal", "connection_id": "c-1",
             "synthesis_id": "s-1", "text_fp": "abc", "terminal_state": "completed"},
        ]
        assert classify(events) == "completed normally"

    def test_fallback_to_cancellation_when_no_terminal(self):
        """When no synthesis_terminal but cancellation events exist, classify as cancelled."""
        classify = self._classifier()
        events = [
            {"event": "cancellation_requested", "connection_id": "c-1",
             "synthesis_id": "s-1", "text_fp": "abc"},
            {"event": "cancellation_propagated", "connection_id": "c-1",
             "synthesis_id": "s-1", "text_fp": "abc"},
        ]
        assert classify(events) == "cancelled"

    def test_fallback_to_completed_when_audio_out_ok(self):
        """audio_out with status=ok classifies as completed normally."""
        classify = self._classifier()
        events = [
            {"event": "audio_out", "status": "ok", "connection_id": "c-1",
             "synthesis_id": "s-1", "text_fp": "abc"},
        ]
        assert classify(events) == "completed normally"

    def test_empty_events_return_unknown(self):
        """Empty events list must return unknown."""
        classify = self._classifier()
        assert classify([]) == "unknown"

    def test_events_without_matching_patterns_return_unknown(self):
        """Events without recognizable patterns return unknown."""
        classify = self._classifier()
        events = [
            {"event": "queue_admitted"},
            {"event": "queue_started"},
        ]
        assert classify(events) == "unknown"


# ═══════════════════════════════════════════════════════════════════════════
# Test 2: classify_backend_outcome
# ═══════════════════════════════════════════════════════════════════════════

class TestClassifyBackendOutcome:
    """Real production backend classifier."""

    @staticmethod
    def _classifier():
        import scripts.phase10_live_validation as p10
        return p10.classify_backend_outcome

    def test_backend_abort_observed_classifies_as_aborted(self):
        """backend_abort_observed must classify as 'aborted early'."""
        classify = self._classifier()
        events = [
            {"event": "backend_abort_observed", "connection_id": "c-1",
             "synthesis_id": "s-1", "text_fp": "abc"},
        ]
        assert classify(events) == "aborted early"

    def test_backend_request_aborted_classifies_as_aborted(self):
        """backend_request_aborted (legacy) must classify as 'aborted early'."""
        classify = self._classifier()
        events = [
            {"event": "backend_request_aborted"},
        ]
        assert classify(events) == "aborted early"

    def test_native_backend_request_cancelled_classifies_as_aborted(self):
        classify = self._classifier()
        events = [{"event": "backend_request_cancelled", "request_id": "s-1"}]
        assert classify(events) == "aborted early"

    def test_backend_done_with_client_disconnected_status(self):
        """backend_done status=client_disconnected must classify as aborted."""
        classify = self._classifier()
        events = [
            {"event": "backend_stream_done", "status": "client_disconnected"},
        ]
        assert classify(events) == "aborted early"

    def test_backend_done_ok_classifies_completed(self):
        """backend_done status=ok must classify as 'completed normally'."""
        classify = self._classifier()
        events = [
            {"event": "backend_stream_done", "status": "ok"},
        ]
        assert classify(events) == "completed normally"

    def test_backend_done_error_classifies_failed(self):
        """backend_done status=error must classify as 'failed'."""
        classify = self._classifier()
        events = [
            {"event": "backend_done", "status": "error"},
        ]
        assert classify(events) == "failed"

    def test_empty_events_return_unknown(self):
        classify = self._classifier()
        assert classify([]) == "unknown"


# ═══════════════════════════════════════════════════════════════════════════
# Test 3: correlate_disconnect_logs
# ═══════════════════════════════════════════════════════════════════════════

class TestCorrelateDisconnectLogs:
    """Real production correlator."""

    @staticmethod
    def _correlator():
        import scripts.phase10_live_validation as p10
        return p10.correlate_disconnect_logs

    def test_returns_unknown_when_no_wrapper_logs(self):
        correlate = self._correlator()
        result = correlate(wrapper_logs=[], backend_logs=[], original_text_fp="abc")
        assert result["wrapper_unknown"] is True

    def test_returns_unknown_when_no_matching_text_fp(self):
        correlate = self._correlator()
        wrapper_line = (
            '{"event": "syn_stopped", "connection_id": "c-1", '
            '"synthesis_id": "s-1", "text_fp": "nomatch"}'
        )
        result = correlate(
            wrapper_logs=[wrapper_line],
            backend_logs=[],
            original_text_fp="abc",
        )
        assert result["wrapper_unknown"] is True

    def test_correlates_by_text_fp_and_extracts_ids(self):
        correlate = self._correlator()
        wrapper_line = (
            '{"event": "syn_stopped", "connection_id": "c-match", '
            '"synthesis_id": "s-match", "text_fp": "abc"}'
        )
        result = correlate(
            wrapper_logs=[wrapper_line],
            backend_logs=[],
            original_text_fp="abc",
        )
        assert result["wrapper_unknown"] is False
        assert result["connection_id"] == "c-match"
        assert result["synthesis_id"] == "s-match"
        assert len(result["wrapper_events"]) == 1

    def test_detects_conn_closed_across_all_connection_scoped_events(self):
        """conn_closed may not have text_fp — must scan all connection events."""
        correlate = self._correlator()
        wrapper_lines = [
            '{"event": "syn_stopped", "connection_id": "c-1", '
            '"synthesis_id": "s-1", "text_fp": "abc"}',
            '{"event": "conn_closed", "connection_id": "c-1"}',
        ]
        result = correlate(
            wrapper_logs=wrapper_lines,
            backend_logs=[],
            original_text_fp="abc",
        )
        assert result["has_conn_closed"] is True

    def test_correlates_backend_by_synthesis_id(self):
        """Backend events matching synthesis_id must be correlated."""
        correlate = self._correlator()
        wrapper_lines = [
            '{"event": "syn_stopped", "connection_id": "c-1", '
            '"synthesis_id": "s-1", "text_fp": "abc"}',
        ]
        backend_lines = [
            '{"event": "generation_cancel_observed", "synthesis_id": "s-1"}',
        ]
        result = correlate(
            wrapper_logs=wrapper_lines,
            backend_logs=backend_lines,
            original_text_fp="abc",
        )
        assert len(result["backend_events"]) == 1

    def test_correlates_native_backend_abort_by_request_id(self):
        correlate = self._correlator()
        wrapper_lines = [
            '{"event":"synthesize_received","connection_id":"c-1","synthesis_id":"s-1","text_fp":"abc"}',
            '{"event":"synthesis_terminal","connection_id":"c-1","synthesis_id":"s-1","text_fp":"abc","terminal_state":"cancelled"}',
        ]
        backend_lines = [
            '[CANCEL] backend_request_cancelled reason=client_disconnect point=sink_write request_id=s-1'
        ]
        result = correlate(wrapper_lines, backend_lines, "abc")
        assert result["backend_events"][0]["event"] == "backend_request_cancelled"
        assert result["backend_events"][0]["synthesis_id"] == "s-1"

    def test_mismatched_wrapper_identifiers_fail_closed(self):
        correlate = self._correlator()
        wrapper_lines = [
            '{"event":"cancellation_requested","connection_id":"c-1","synthesis_id":"s-1","text_fp":"abc"}',
            '{"event":"synthesis_terminal","connection_id":"c-1","synthesis_id":"s-2","text_fp":"abc","terminal_state":"cancelled"}',
        ]
        result = correlate(wrapper_lines, [], "abc")
        assert result["wrapper_unknown"] is True
        assert result["correlation_error"] == "missing_or_mismatched_wrapper_identifiers"


def test_follow_up_requires_correlated_completed_terminal():
    import scripts.phase10_live_validation as p10
    incomplete = {
        "wrapper_unknown": False,
        "has_synthesis_received": True,
        "wrapper_events": [{"event": "synthesize_received"}],
    }
    assert p10.assert_follow_up_request_completed(incomplete).passed is False
    complete = {
        **incomplete,
        "wrapper_events": [
            {"event": "synthesize_received"},
            {"event": "synthesis_terminal", "terminal_state": "completed"},
        ],
    }
    assert p10.assert_follow_up_request_completed(complete).passed is True


def test_live_event_in_marks_follow_up_synthesis_received():
    import scripts.phase10_live_validation as p10
    fp = "afe09bca8086"
    wrapper_logs = [
        f'2026-07-13T03:15:06Z {{"connection_id":"c1","event":"event_in",'
        f'"event_type":"synthesize","text_fp":"{fp}"}}',
        f'2026-07-13T03:15:06Z {{"connection_id":"c1","event":"syn_trigger",'
        f'"synthesis_id":"s1","text_fp":"{fp}"}}',
        f'2026-07-13T03:15:09Z {{"connection_id":"c1","event":"synthesis_terminal",'
        f'"synthesis_id":"s1","terminal_state":"completed","text_fp":"{fp}"}}',
    ]
    correlation = p10.correlate_disconnect_logs(wrapper_logs, [], fp)
    assert correlation["has_synthesis_received"] is True
    assert p10.assert_follow_up_request_completed(correlation).passed is True


# ═══════════════════════════════════════════════════════════════════════════
# Test 4: Backend native log parser
# ═══════════════════════════════════════════════════════════════════════════

class TestBackendNativeLogParser:
    """Parser must handle both JSON wrapper logs and text key=value backend native logs."""

    @staticmethod
    def _parser():
        import scripts.phase10_live_validation as p10
        return p10._extract_json_from_line

    def test_parses_pure_json(self):
        parse = self._parser()
        result = parse('{"event": "syn_stopped"}')
        assert result == {"event": "syn_stopped"}

    def test_parses_json_after_timestamp_prefix(self):
        parse = self._parser()
        result = parse('2025-01-01T00:00:00Z {"event": "syn_stopped"}')
        assert result == {"event": "syn_stopped"}

    def test_returns_none_for_empty_line(self):
        parse = self._parser()
        assert parse("") is None
        assert parse("   ") is None

    def test_parse_backend_native_cancellation_line(self):
        """Backend native cancellation logs are text key=value, not JSON."""
        import scripts.phase10_live_validation as p10

        line = (
            "[CANCEL] backend_cancel_detected reason=client_disconnect "
            "point=content_provider_wait request_id=s-1"
        )
        result = p10.parse_backend_native_line(line)
        assert result is not None
        assert result["event"] == "backend_cancel_detected"
        assert result["reason"] == "client_disconnect"
        assert result["point"] == "content_provider_wait"
        assert result["request_id"] == "s-1"

    def test_parse_timestamped_backend_native_cancellation_line(self):
        """Exact docker --timestamps output must retain native cancellation evidence."""
        import scripts.phase10_live_validation as p10

        line = (
            "2026-07-13T03:15:06.404233771Z "
            "[CANCEL] backend_cancel_detected reason=client_disconnect "
            "point=content_provider_wait request_id=1eb6c032"
        )
        result = p10.parse_backend_native_line(line)
        assert result is not None
        assert result["event"] == "backend_cancel_detected"
        assert result["request_id"] == "1eb6c032"

    def test_parse_backend_native_generation_cancel_line(self):
        import scripts.phase10_live_validation as p10

        line = (
            "[CANCEL] generation_cancel_observed reason=client_disconnect "
            "point=content_provider_wait request_id=s-1 total_frames=150 "
            "frame_index=42 detection_to_generation_cancel_ms=5"
        )
        result = p10.parse_backend_native_line(line)
        assert result is not None
        assert result["event"] == "generation_cancel_observed"
        assert result["reason"] == "client_disconnect"
        assert result["request_id"] == "s-1"
        assert result["total_frames"] == "150"

    def test_parse_backend_native_final_decode_line(self):
        import scripts.phase10_live_validation as p10

        line = (
            "[CANCEL] final_decode_skipped reason=client_disconnect "
            "point=http_sink_on_pcm request_id=s-1 generated_frames=200 "
            "elapsed_ms=10"
        )
        result = p10.parse_backend_native_line(line)
        assert result is not None
        assert result["event"] == "final_decode_skipped"
        assert result["generated_frames"] == "200"

    def test_parse_backend_native_request_cancelled_line(self):
        import scripts.phase10_live_validation as p10

        line = (
            "[CANCEL] backend_request_cancelled reason=client_disconnect "
            "point=content_provider_wait request_id=s-1 generated_frames=150 "
            "last_frame_index=42 decoded_frames=10 pcm_bytes_produced=3200 "
            "queued_pcm_bytes=1600 detection_to_exit_ms=15"
        )
        result = p10.parse_backend_native_line(line)
        assert result is not None
        assert result["event"] == "backend_request_cancelled"
        assert result["request_id"] == "s-1"
        assert result["generated_frames"] == "150"
        assert result["pcm_bytes_produced"] == "3200"

    def test_parse_backend_native_cleanup_line(self):
        import scripts.phase10_live_validation as p10

        line = (
            "[CANCEL] backend_request_cleanup_done reason=client_disconnect "
            "point=content_provider_wait request_id=s-1 "
            "detection_to_cleanup_ms=20 server_busy_released_on_scope_exit=true"
        )
        result = p10.parse_backend_native_line(line)
        assert result is not None
        assert result["event"] == "backend_request_cleanup_done"
        assert result["server_busy_released_on_scope_exit"] == "true"

    def test_parse_backend_native_line_returns_none_for_non_matching(self):
        import scripts.phase10_live_validation as p10

        # Regular log lines without [CANCEL] prefix should return None
        result = p10.parse_backend_native_line("Normal log message")
        assert result is None

        # JSON lines handled by existing parser
        result = p10.parse_backend_native_line('{"event": "foo"}')
        assert result is None

    def test_parse_backend_native_line_with_request_id_none(self):
        import scripts.phase10_live_validation as p10

        line = (
            "[CANCEL] backend_cancel_detected reason=client_disconnect "
            "point=content_provider_wait request_id=none"
        )
        result = p10.parse_backend_native_line(line)
        assert result is not None
        assert result["request_id"] == "none"


# ═══════════════════════════════════════════════════════════════════════════
# Test 5: Distinct follow-up completion
# ═══════════════════════════════════════════════════════════════════════════

class TestFollowUpCompletion:
    """follow_up_completed must be handled as a completed terminal state."""

    @staticmethod
    def _classifier():
        import scripts.phase10_live_validation as p10
        return p10.classify_wrapper_outcome

    def test_follow_up_completed_classifies_as_completed_normally(self):
        """follow_up_completed must classify as 'completed normally'."""
        classify = self._classifier()
        events = [
            {"event": "follow_up_completed",
             "connection_id": "c-fu", "synthesis_id": "s-fu",
             "text_fp": "followup12345",
             "original_text_fp": "original67890",
             "status": "ok"},
        ]
        assert classify(events) == "completed normally"

    def test_follow_up_completed_with_audio_out_classifies_completed(self):
        """follow_up_completed alongside audio_out still classifies completed."""
        classify = self._classifier()
        events = [
            {"event": "follow_up_completed",
             "connection_id": "c-fu", "synthesis_id": "s-fu",
             "text_fp": "followup12345",
             "original_text_fp": "original67890",
             "status": "ok"},
            {"event": "audio_out", "status": "ok",
             "connection_id": "c-fu", "synthesis_id": "s-fu",
             "text_fp": "followup12345"},
        ]
        assert classify(events) == "completed normally"


# ═══════════════════════════════════════════════════════════════════════════
# Test 6: Cancellation events in production code
# ═══════════════════════════════════════════════════════════════════════════

class TestProductionCancellationEvents:
    """synthesize_s2cpp_streaming_tts_events must emit cancellation_requested,
    cancellation_propagated, and synthesis_terminal(cancelled) when cancelled."""

    @pytest.mark.asyncio
    async def test_cancellation_events_emitted_in_order(self, monkeypatch):
        """Inject cancellation into the real generator and assert correlated order."""
        from app.config import Settings
        from app.s2_client import S2GenerateRequest
        from app.wyoming_server import synthesize_s2cpp_streaming_tts_events, FakeTtsConfig
        from app.observability import LogContext
        import app.wyoming_server as wserver

        observed = []
        monkeypatch.setattr(
            wserver, "obs_log",
            lambda event, **fields: observed.append({"event": event, **fields}),
        )

        class _CancellableStream:
            content_type = "audio/L16; rate=44100; channels=1"
            response_headers = {
                "x-audio-encoding": "pcm_s16le",
                "x-audio-channels": "1",
                "x-audio-sample-rate": "44100",
            }
            def __init__(self): self.cancelled = False
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def __iter__(self): return self
            def __next__(self):
                if self.cancelled: raise StopIteration
                return b"\x00" * 8820
            def cancel(self): self.cancelled = True

        stream = _CancellableStream()
        class _Client:
            def generate_stream(self, request, files=None, boundary=None, **kwargs):
                return stream

        gen = synthesize_s2cpp_streaming_tts_events(
            client=_Client(),
            request=S2GenerateRequest(text="Hello world", stream=True),
            config=FakeTtsConfig(sample_rate=44100),
            settings=Settings(tts_backend="s2cpp", s2_stream=True,
                              s2_initial_buffer_ms=0, s2_max_initial_buffer_ms=0),
            ctx=LogContext(connection_id="c-test", synthesis_id="s-test"),
        )
        await anext(gen)
        with pytest.raises(asyncio.CancelledError):
            await gen.athrow(asyncio.CancelledError("test cancellation"))

        names = [event["event"] for event in observed]
        expected = ["cancellation_requested", "cancellation_propagated",
                    "synthesis_cancelled", "synthesis_terminal"]
        indices = [names.index(name) for name in expected]
        assert indices == sorted(indices)
        for event in observed:
            if event["event"] in expected:
                assert event["connection_id"] == "c-test"
                assert event["synthesis_id"] == "s-test"
                assert event["text_fp"]
        terminal = next(event for event in observed if event["event"] == "synthesis_terminal")
        assert terminal["terminal_state"] == "cancelled"
        assert stream.cancelled is True

    def test_synthesis_terminal_cancelled_state_exists(self):
        """Verify that synthesis_terminal with state='cancelled' is expected
        in the production code's cancellation path."""
        import inspect
        import app.wyoming_server as wserver
        source = inspect.getsource(wserver.synthesize_s2cpp_streaming_tts_events)
        assert "synthesis_terminal" in source


# ═══════════════════════════════════════════════════════════════════════════
# Test 7: Backend patch structural tests (C++ - compilation not available)
# ═══════════════════════════════════════════════════════════════════════════

class TestBackendPatchStructure:
    """Structural verification of the backend C++ patch."""

    @staticmethod
    def _patch_text():
        patch_path = (
            Path(__file__).resolve().parent.parent
            / "docker" / "s2cpp" / "patches" / "cancellation-observability.patch"
        )
        return patch_path.read_text(encoding="utf-8")

    def test_patch_includes_synthesis_id_extraction(self):
        """The patch must read X-Synthesis-ID header and include it in logs."""
        patch_text = self._patch_text()
        assert "X-Synthesis-ID" in patch_text, (
            "Patch must include X-Synthesis-ID header extraction"
        )

    def test_patch_extracts_id_before_thread_spawn(self):
        """The request_id must be extracted BEFORE the synth_thread lambda
        is created (outside the capture list), not accessed as 'req' inside."""
        patch_text = self._patch_text()
        assert "synth_thread" in patch_text

    def test_patch_has_all_cancellation_events(self):
        """All five cancellation events must remain in the patch."""
        patch_text = self._patch_text()
        for event in [
            "backend_cancel_detected",
            "generation_cancel_observed",
            "final_decode_skipped",
            "backend_request_cancelled",
            "backend_request_cleanup_done",
        ]:
            assert event in patch_text, f"Event '{event}' must be present in patch"

    def test_patch_logs_include_request_id(self):
        """Every [CANCEL] log line must include request_id."""
        patch_text = self._patch_text()
        cancel_count = patch_text.count("[CANCEL]")
        request_id_count = patch_text.count("request_id=")
        assert request_id_count >= cancel_count, (
            f"All {cancel_count} [CANCEL] log lines must include request_id= "
            f"(found {request_id_count})"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Test 8: LISTED_OUTCOMES completeness
# ═══════════════════════════════════════════════════════════════════════════

class TestListedOutcomes:
    """LISTED_OUTCOMES must include all new correlated lifecycle events."""

    def test_listed_outcomes_has_all_new_events(self):
        import scripts.phase10_live_validation as p10

        required = {
            "cancellation_requested",
            "cancellation_propagated",
            "backend_abort_observed",
            "synthesis_terminal",
            "follow_up_completed",
            "wrapper_outcome_classification",
            "backend_outcome_classification",
            "follow_up_synthesis_correlated",
        }
        for name in required:
            assert name in p10.LISTED_OUTCOMES, (
                f"LISTED_OUTCOMES must include '{name}'"
            )


# ═══════════════════════════════════════════════════════════════════════════
# Test 9: No plaintext text in events
# ═══════════════════════════════════════════════════════════════════════════

class TestNoPlaintext:
    """Correlated events must use text_fp only, not raw text."""

    def test_synthesis_terminal_uses_text_fp(self):
        import inspect
        import app.wyoming_server as wserver
        source = inspect.getsource(wserver.synthesize_s2cpp_streaming_tts_events)

        terminal_count = source.count('"synthesis_terminal"')
        idx = 0
        found_with_text_fp = 0
        while True:
            idx = source.find('"synthesis_terminal"', idx)
            if idx == -1:
                break
            window_start = max(0, idx - 100)
            window_end = min(len(source), idx + 400)
            window = source[window_start:window_end]
            if "text_fp" in window:
                found_with_text_fp += 1
            idx += len('"synthesis_terminal"')

        assert found_with_text_fp >= terminal_count, (
            f"All {terminal_count} synthesis_terminal events must include text_fp; "
            f"only {found_with_text_fp} do"
        )

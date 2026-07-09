"""Static tests for the backend cancellation observability patch.

The repository builds the backend by applying
``docker/s2cpp/patches/cancellation-observability.patch`` to the pinned upstream
s2.cpp source. These tests validate the patch-level contract without requiring a
CUDA backend build in unit tests.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PATCH = PROJECT_ROOT / "docker" / "s2cpp" / "patches" / "cancellation-observability.patch"


def _patch() -> str:
    return PATCH.read_text(encoding="utf-8")


def _added_patch() -> str:
    """Return only added lines from the unified diff, excluding file headers."""
    lines = []
    for line in _patch().splitlines():
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            lines.append(line[1:])
    return "\n".join(lines)


def test_patch_centralizes_once_only_cancellation_recording() -> None:
    text = _added_patch()
    assert "mark_cancelled" in text
    assert "compare_exchange" in text
    assert "backend_cancel_detected" in text
    assert "cancel_detection_point" in text
    assert "cancel_cv" in text or "notify_all" in text


def test_all_http_disconnect_paths_call_mark_cancelled() -> None:
    text = _added_patch()
    for point in [
        "content_provider_wait",
        "startup_buffer",
        "sink_write",
        "content_provider_complete",
        "http_sink_on_pcm_after_cancel",
    ]:
        assert point in text
    assert text.count("mark_cancelled(") >= 5
    assert "ctx->cancelled.store(true)" not in text


def test_terminal_cancellation_events_and_timings_are_structured() -> None:
    text = _added_patch()
    for event in [
        "backend_cancel_detected",
        "generation_cancel_observed",
        "final_decode_skipped",
        "backend_request_cancelled",
        "backend_request_cleanup_done",
    ]:
        assert event in text
    for field in [
        "detection_to_generation_cancel_ms",
        "detection_to_exit_ms",
        "detection_to_cleanup_ms",
    ]:
        assert field in text
    assert "if (!cancel_recorded.load())" in text, (
        "Durations must guard against unset cancellation time"
    )


def test_generation_counters_threaded_to_terminal_event() -> None:
    text = _added_patch()
    for field in [
        "generated_frames",
        "last_frame_index",
        "decoded_frames",
        "pcm_bytes_produced",
        "queued_pcm_bytes",
    ]:
        assert field in text
    assert "generated_frames=0" not in text
    assert "on_generation_cancelled" in text
    assert "on_final_decode_skipped" in text
    assert "on_stream_decode" in text
    assert "sink.on_stream_decode(total_frames, emit_end_samples - emit_begin_samples);" in text
    assert "sink.on_stream_decode(total_frames, delta_count);" not in text

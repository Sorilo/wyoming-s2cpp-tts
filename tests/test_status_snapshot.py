"""Phase 9C Slice 4: Status snapshot sanitization tests.

Tests that build_status_snapshot:
- Contains only safe fields
- No plaintext text, raw audio, secrets, env dumps, or task objects
- Scheduler depth/pending and active presence are represented correctly
- Lifecycle state and readiness are included
- Snapshot is stable across repeated calls
"""

import json

from app.lifecycle import ServiceLifecycle
from app.config import Settings
from app.admin_http import build_status_snapshot, build_metrics_snapshot


def _sched_snap(active_sid=None, depth=0, pending=0, waiting=0):
    return {
        "active_synthesis_id": active_sid,
        "active_connection_id": "c1" if active_sid else None,
        "depth": depth,
        "pending": pending,
        "max_size": 3,
        "waiting_count": waiting,
    }


def test_snapshot_contains_only_safe_fields():
    """Snapshot must not contain plaintext, raw audio, secrets, tokens, env dumps."""
    lifecycle = ServiceLifecycle()
    settings = Settings()

    snap = build_status_snapshot(lifecycle, settings)

    # Must contain expected keys
    expected_keys = {
        "state", "ready", "uptime_sec", "version",
        "max_queue_size", "admin_http_enabled", "active_connections",
    }
    for key in expected_keys:
        assert key in snap, f"Missing key: {key}"

    # Must NOT contain sensitive data
    forbidden = {
        "text", "plaintext", "secret", "token",
        "audio", "raw", "env", "password",
        "synthesis_id", "connection_id", "task",
        "stack", "trace",
    }
    for key in forbidden:
        assert key not in snap, f"Forbidden key present: {key}"

    # All values must be safe scalars
    for key, val in snap.items():
        assert isinstance(val, (str, int, float, bool, type(None))), \
            f"Non-scalar value for {key}: {type(val)}"


def test_snapshot_with_scheduler_data():
    """Scheduler depth/pending/waiting are included when available."""
    lifecycle = ServiceLifecycle()
    lifecycle.transition_to_running()
    settings = Settings()

    sched = _sched_snap(active_sid="s1", depth=3, pending=3, waiting=2)
    snap = build_status_snapshot(
        lifecycle, settings, scheduler_snapshot=sched
    )

    assert snap["scheduler_depth"] == 3
    assert snap["scheduler_pending"] == 3
    assert snap["scheduler_waiting"] == 2
    assert snap["has_active_synthesis"] is True
    assert snap["state"] == "RUNNING"
    assert snap["ready"] is True


def test_snapshot_without_active_synthesis():
    """When no active synthesis, has_active_synthesis is False."""
    lifecycle = ServiceLifecycle()
    settings = Settings()

    sched = _sched_snap(active_sid=None, depth=0, pending=0, waiting=0)
    snap = build_status_snapshot(
        lifecycle, settings, scheduler_snapshot=sched
    )

    assert snap["has_active_synthesis"] is False
    assert snap["scheduler_depth"] == 0
    assert snap["scheduler_pending"] == 0
    assert snap["scheduler_waiting"] == 0


def test_snapshot_with_no_scheduler():
    """When scheduler snapshot is None, scheduler keys are not added."""
    lifecycle = ServiceLifecycle()
    settings = Settings()

    snap = build_status_snapshot(
        lifecycle, settings, scheduler_snapshot=None
    )

    assert "scheduler_depth" not in snap
    assert "scheduler_pending" not in snap
    assert "scheduler_waiting" not in snap
    assert "has_active_synthesis" not in snap


def test_snapshot_lifecycle_draining_state():
    """During draining, ready is False."""
    lifecycle = ServiceLifecycle()
    lifecycle.transition_to_running()
    lifecycle.start_draining()
    settings = Settings()

    snap = build_status_snapshot(lifecycle, settings)

    assert snap["state"] == "DRAINING"
    assert snap["ready"] is False


def test_snapshot_lifecycle_starting_state():
    """During starting, ready is False."""
    lifecycle = ServiceLifecycle()
    settings = Settings()

    snap = build_status_snapshot(lifecycle, settings)

    assert snap["state"] == "STARTING"
    assert snap["ready"] is False


def test_snapshot_stable_across_repeated_calls():
    """Repeated calls produce consistent results (state-dependent only)."""
    lifecycle = ServiceLifecycle()
    settings = Settings()

    snap1 = build_status_snapshot(lifecycle, settings)
    snap2 = build_status_snapshot(lifecycle, settings)

    assert snap1["state"] == snap2["state"]
    assert snap1["ready"] == snap2["ready"]
    assert snap1["max_queue_size"] == snap2["max_queue_size"]


def test_snapshot_includes_active_connections():
    """Active connection count is included."""
    lifecycle = ServiceLifecycle()
    settings = Settings()

    snap = build_status_snapshot(
        lifecycle, settings, active_connection_count=5
    )

    assert snap["active_connections"] == 5


def test_snapshot_includes_version():
    """Version string is included."""
    lifecycle = ServiceLifecycle()
    settings = Settings()

    snap = build_status_snapshot(
        lifecycle, settings, version="1.2.3"
    )

    assert snap["version"] == "1.2.3"


def test_snapshot_excludes_scheduler_ids_from_status():
    """The status snapshot must NOT expose active_synthesis_id or active_connection_id."""
    lifecycle = ServiceLifecycle()
    settings = Settings()

    sched = _sched_snap(active_sid="secret-s1", depth=1, pending=1, waiting=0)
    snap = build_status_snapshot(
        lifecycle, settings, scheduler_snapshot=sched
    )

    assert "active_synthesis_id" not in snap
    assert "active_connection_id" not in snap
    # The boolean is safe
    assert snap["has_active_synthesis"] is True


def test_snapshot_excludes_text_from_scheduler():
    """The scheduler snapshot has no text field, and we don't add one."""
    lifecycle = ServiceLifecycle()
    settings = Settings()

    sched = {"depth": 0, "pending": 0, "text": "hello"}
    snap = build_status_snapshot(
        lifecycle, settings, scheduler_snapshot=sched
    )

    # 'text' key is not mapped by build_status_snapshot
    assert "text" not in snap


def test_snapshot_json_serializable():
    """Snapshot must be JSON-serializable."""
    lifecycle = ServiceLifecycle()
    settings = Settings()
    sched = _sched_snap(active_sid="s1", depth=1, pending=1, waiting=0)

    snap = build_status_snapshot(
        lifecycle, settings, scheduler_snapshot=sched, active_connection_count=2
    )

    # Must not raise
    encoded = json.dumps(snap, sort_keys=True)
    decoded = json.loads(encoded)
    assert decoded["state"] == snap["state"]
    assert decoded["ready"] == snap["ready"]


# ── Metrics snapshot tests ──────────────────────────────────────────────────


def test_metrics_snapshot_independent_schema():
    """build_metrics_snapshot has an independent schema from build_status_snapshot."""
    lifecycle = ServiceLifecycle()
    lifecycle.transition_to_running()
    settings = Settings()

    snap = build_metrics_snapshot(lifecycle, settings, version="1.0")

    # Metrics does NOT include status-specific fields
    assert "max_queue_size" not in snap
    assert "admin_http_enabled" not in snap

    # Metrics has its own fields
    assert "state" in snap
    assert "ready" in snap
    assert "uptime_sec" in snap
    assert "version" in snap
    assert "active_connections" in snap


def test_metrics_snapshot_with_scheduler():
    """Metrics snapshot includes scheduler data when available."""
    lifecycle = ServiceLifecycle()
    settings = Settings()

    sched = _sched_snap(active_sid="s1", depth=2, pending=1, waiting=1)
    snap = build_metrics_snapshot(
        lifecycle, settings, scheduler_snapshot=sched, active_connection_count=3
    )

    assert snap["scheduler_depth"] == 2
    assert snap["scheduler_pending"] == 1
    assert snap["scheduler_waiting"] == 1
    assert snap["has_active_synthesis"] is True
    assert snap["active_connections"] == 3


def test_metrics_snapshot_no_scheduler():
    """Metrics snapshot omits scheduler keys when None."""
    lifecycle = ServiceLifecycle()
    settings = Settings()

    snap = build_metrics_snapshot(lifecycle, settings, scheduler_snapshot=None)

    assert "scheduler_depth" not in snap
    assert "scheduler_pending" not in snap
    assert "scheduler_waiting" not in snap
    assert "has_active_synthesis" not in snap


def test_metrics_snapshot_json_serializable():
    """Metrics snapshot is JSON-serializable."""
    lifecycle = ServiceLifecycle()
    settings = Settings()
    sched = _sched_snap(active_sid="s1", depth=1, pending=1, waiting=0)

    snap = build_metrics_snapshot(
        lifecycle, settings, scheduler_snapshot=sched, active_connection_count=2
    )

    encoded = json.dumps(snap, sort_keys=True)
    decoded = json.loads(encoded)
    assert decoded["state"] == snap["state"]


def test_metrics_snapshot_no_ids_exposed():
    """Metrics snapshot never exposes synthesis/connection IDs."""
    lifecycle = ServiceLifecycle()
    settings = Settings()

    sched = _sched_snap(active_sid="secret-id", depth=1, pending=1)
    snap = build_metrics_snapshot(
        lifecycle, settings, scheduler_snapshot=sched
    )

    assert "active_synthesis_id" not in snap
    assert "active_connection_id" not in snap
    assert snap["has_active_synthesis"] is True

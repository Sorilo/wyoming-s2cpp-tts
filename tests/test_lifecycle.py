"""Phase 9C Slice 1: Lifecycle state machine and snapshot tests."""

import pytest


# --- LifecycleState enum tests (unit) ---

def test_lifecycle_state_values():
    from app.lifecycle import LifecycleState
    assert LifecycleState.STARTING.value == "STARTING"
    assert LifecycleState.RUNNING.value == "RUNNING"
    assert LifecycleState.DRAINING.value == "DRAINING"
    assert LifecycleState.STOPPING.value == "STOPPING"
    assert LifecycleState.STOPPED.value == "STOPPED"
    assert LifecycleState.FAILED.value == "FAILED"


def test_lifecycle_terminal_states():
    from app.lifecycle import LifecycleState
    assert LifecycleState.STOPPED.is_terminal()
    assert LifecycleState.FAILED.is_terminal()
    assert not LifecycleState.STARTING.is_terminal()
    assert not LifecycleState.RUNNING.is_terminal()
    assert not LifecycleState.DRAINING.is_terminal()
    assert not LifecycleState.STOPPING.is_terminal()


def test_lifecycle_readiness():
    from app.lifecycle import LifecycleState
    assert not LifecycleState.STARTING.is_ready()
    assert LifecycleState.RUNNING.is_ready()
    assert not LifecycleState.DRAINING.is_ready()
    assert not LifecycleState.STOPPING.is_ready()
    assert not LifecycleState.STOPPED.is_ready()
    assert not LifecycleState.FAILED.is_ready()


def test_lifecycle_can_accept_work():
    from app.lifecycle import LifecycleState
    assert not LifecycleState.STARTING.accepts_new_work()
    assert LifecycleState.RUNNING.accepts_new_work()
    assert not LifecycleState.DRAINING.accepts_new_work()
    assert not LifecycleState.STOPPING.accepts_new_work()
    assert not LifecycleState.STOPPED.accepts_new_work()
    assert not LifecycleState.FAILED.accepts_new_work()


# --- LifecycleSnapshot tests ---

def test_snapshot_defaults():
    from app.lifecycle import LifecycleSnapshot, LifecycleState
    snap = LifecycleSnapshot()
    assert snap.state == LifecycleState.STARTING
    assert snap.ready is False
    assert snap.uptime_sec >= 0
    assert snap.pending_count == 0
    assert snap.depth == 0
    assert snap.max_queue_size == 0
    assert snap.active_connections == 0


def test_snapshot_immutable():
    from app.lifecycle import LifecycleSnapshot
    snap = LifecycleSnapshot(state="RUNNING", ready=True)
    with pytest.raises(Exception):
        snap.state = "STOPPED"


def test_snapshot_excludes_sensitive():
    from app.lifecycle import LifecycleSnapshot
    snap = LifecycleSnapshot()
    d = snap.to_dict()
    assert "state" in d
    assert "ready" in d
    assert "uptime_sec" in d
    assert "text" not in d
    assert "plaintext" not in d
    assert "secret" not in d
    assert "audio" not in d
    assert "token" not in d
    assert "synthesis_id" not in d
    assert "connection_id" not in d
    for key, val in d.items():
        assert not isinstance(val, (list, dict, set, bytearray))
        assert not callable(val)


def test_snapshot_to_dict_is_immutable_copy():
    from app.lifecycle import LifecycleSnapshot
    snap = LifecycleSnapshot(
        state="RUNNING", ready=True, depth=3, pending_count=1,
        active_connections=2,
    )
    d = snap.to_dict()
    assert d == {
        "state": "RUNNING",
        "ready": True,
        "uptime_sec": snap.uptime_sec,
        "pending_count": 1,
        "depth": 3,
        "max_queue_size": 0,
        "active_connections": 2,
    }
    d["state"] = "STOPPED"
    assert snap.state == "RUNNING"

# --- ServiceLifecycle tests ---

def test_lifecycle_initial_state():
    from app.lifecycle import ServiceLifecycle, LifecycleState
    owner = ServiceLifecycle()
    assert owner.state == LifecycleState.STARTING
    assert owner.ready is False


def test_lifecycle_transitions_to_running():
    from app.lifecycle import ServiceLifecycle, LifecycleState
    owner = ServiceLifecycle()
    owner.transition_to_running()
    assert owner.state == LifecycleState.RUNNING
    assert owner.ready is True


def test_lifecycle_transition_to_running_only_from_starting():
    from app.lifecycle import ServiceLifecycle
    owner = ServiceLifecycle()
    owner.transition_to_running()
    with pytest.raises(RuntimeError, match="transition"):
        owner.transition_to_running()


def test_lifecycle_transition_to_failed_from_starting():
    from app.lifecycle import ServiceLifecycle, LifecycleState
    owner = ServiceLifecycle()
    owner.transition_to_failed()
    assert owner.state == LifecycleState.FAILED
    assert owner.ready is False


def test_lifecycle_transition_to_failed_from_running():
    from app.lifecycle import ServiceLifecycle, LifecycleState
    owner = ServiceLifecycle()
    owner.transition_to_running()
    owner.transition_to_failed()
    assert owner.state == LifecycleState.FAILED


def test_lifecycle_shutdown_once():
    from app.lifecycle import ServiceLifecycle, LifecycleState
    owner = ServiceLifecycle()
    owner.transition_to_running()
    assert owner.state == LifecycleState.RUNNING
    assert owner.ready is True
    owner.start_draining()
    assert owner.state == LifecycleState.DRAINING
    assert owner.ready is False
    owner.transition_to_stopping()
    assert owner.state == LifecycleState.STOPPING
    owner.transition_to_stopped()
    assert owner.state == LifecycleState.STOPPED

def test_lifecycle_duplicate_shutdown_idempotent():
    from app.lifecycle import ServiceLifecycle, LifecycleState
    owner = ServiceLifecycle()
    owner.transition_to_running()
    owner.start_draining()
    assert owner.state == LifecycleState.DRAINING
    owner.start_draining()
    assert owner.state == LifecycleState.DRAINING
    owner.transition_to_stopping()
    assert owner.state == LifecycleState.STOPPING
    owner.start_draining()
    assert owner.state == LifecycleState.STOPPING
    owner.transition_to_stopped()
    assert owner.state == LifecycleState.STOPPED
    owner.start_draining()
    assert owner.state == LifecycleState.STOPPED


def test_lifecycle_ready_false_immediately_on_shutdown():
    from app.lifecycle import ServiceLifecycle
    owner = ServiceLifecycle()
    owner.transition_to_running()
    assert owner.ready is True
    owner.start_draining()
    assert owner.ready is False


def test_lifecycle_shutdown_idempotent_on_duplicate_signals():
    from app.lifecycle import ServiceLifecycle, LifecycleState
    owner = ServiceLifecycle()
    owner.transition_to_running()
    result1 = owner.start_draining()
    assert result1 is True
    assert owner.state == LifecycleState.DRAINING
    result2 = owner.start_draining()
    assert result2 is False
    result3 = owner.start_draining()
    assert result3 is False


def test_lifecycle_uptime_tracks_monotonic():
    from app.lifecycle import ServiceLifecycle
    owner = ServiceLifecycle()
    t0 = owner.uptime_sec
    assert t0 >= 0
    assert t0 < 5.0


def test_lifecycle_shutdown_timeout_is_enforced():
    from app.lifecycle import ServiceLifecycle
    owner = ServiceLifecycle(shutdown_grace_timeout_sec=10.0)
    assert owner.shutdown_grace_timeout_sec == 10.0
    owner_default = ServiceLifecycle()
    assert owner_default.shutdown_grace_timeout_sec > 0
    assert owner_default.shutdown_grace_timeout_sec <= 300

# --- Shutdown config validation ---

def test_shutdown_grace_timeout_default():
    from app.config import Settings
    s = Settings()
    assert s.shutdown_grace_timeout_sec > 0
    assert s.shutdown_grace_timeout_sec <= 60
    assert s.shutdown_grace_timeout_sec < float("inf")


def test_shutdown_grace_timeout_env_override(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("SHUTDOWN_GRACE_TIMEOUT_SEC", "15.0")
    s = Settings.from_env()
    assert s.shutdown_grace_timeout_sec == 15.0


def test_shutdown_grace_timeout_rejects_negative(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("SHUTDOWN_GRACE_TIMEOUT_SEC", "-5")
    with pytest.raises(ValueError, match="SHUTDOWN_GRACE_TIMEOUT_SEC"):
        Settings.from_env()


def test_shutdown_grace_timeout_rejects_zero(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("SHUTDOWN_GRACE_TIMEOUT_SEC", "0")
    with pytest.raises(ValueError, match="SHUTDOWN_GRACE_TIMEOUT_SEC"):
        Settings.from_env()


def test_shutdown_grace_timeout_rejects_excessive(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("SHUTDOWN_GRACE_TIMEOUT_SEC", "9999")
    with pytest.raises(ValueError, match="SHUTDOWN_GRACE_TIMEOUT_SEC"):
        Settings.from_env()

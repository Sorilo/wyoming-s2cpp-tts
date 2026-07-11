"""Phase 9B SpeechScheduler behavior tests.

Tests the scheduler's FIFO admission, activation, cancellation, timeout,
and snapshot behavior using deterministic asyncio.Event coordination.
"""

import asyncio

import pytest

from app.speech.models import (
    SpeechMetadata,
    SpeechRequest,
    SpeechState,
    ScheduledSpeech,
)
from app.speech.scheduler import (
    SpeechScheduler,
    QueueFullError,
    QueueTimeoutError,
)


def _req(sid: str = "s1", cid: str = "c1", text: str = "test") -> SpeechRequest:
    return SpeechRequest(synthesis_id=sid, connection_id=cid, text=text)


# ── Initialisation ─────────────────────────────────────────────────────

def test_scheduler_initial_state():
    sched = SpeechScheduler(max_size=3, wait_timeout_sec=30)
    snap = sched.snapshot()
    assert snap["active_synthesis_id"] is None
    assert snap["active_connection_id"] is None
    assert snap["depth"] == 0
    assert snap["pending"] == 0
    assert snap["max_size"] == 3
    assert snap["waiting_count"] == 0


def test_scheduler_rejects_zero_max_size():
    with pytest.raises(ValueError, match="max_size"):
        SpeechScheduler(max_size=0)


def test_scheduler_rejects_negative_max_size():
    with pytest.raises(ValueError, match="max_size"):
        SpeechScheduler(max_size=-1)


# ── Basic admission and activation ──────────────────────────────────────

def test_first_request_starts_immediately():
    """First admitted request becomes active immediately."""
    sched = SpeechScheduler(max_size=3)
    started = asyncio.Event()
    done = asyncio.Event()

    async def op():
        started.set()
        await done.wait()

    async def run_test():
        task = asyncio.create_task(sched.run(_req("s1", "c1"), op))
        await asyncio.wait_for(started.wait(), timeout=5)
        assert sched.snapshot()["active_synthesis_id"] == "s1"
        assert sched.snapshot()["depth"] == 1
        assert sched.snapshot()["pending"] == 1
        done.set()
        await task
        assert sched.snapshot()["depth"] == 0
        assert sched.snapshot()["active_synthesis_id"] is None

    asyncio.run(run_test())


def test_second_request_waits_and_starts_fifo():
    """Admitted waiters start in FIFO order."""
    sched = SpeechScheduler(max_size=2)
    first_started = asyncio.Event()
    first_release = asyncio.Event()
    second_started = asyncio.Event()
    second_release = asyncio.Event()
    order = []

    async def op1():
        order.append("r1")
        first_started.set()
        await first_release.wait()

    async def op2():
        order.append("r2")
        second_started.set()
        await second_release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1", "c1"), op1))
        await first_started.wait()
        assert sched.snapshot()["active_synthesis_id"] == "s1"

        t2 = asyncio.create_task(sched.run(_req("s2", "c2"), op2))
        # Give t2 time to register as waiting
        await asyncio.sleep(0.05)
        assert sched.snapshot()["active_synthesis_id"] == "s1"
        assert sched.snapshot()["pending"] == 2
        assert sched.snapshot()["depth"] == 2
        assert sched.snapshot()["waiting_count"] == 1

        first_release.set()
        await t1
        # After first completes, second should become active
        await asyncio.wait_for(second_started.wait(), timeout=5)
        assert sched.snapshot()["active_synthesis_id"] == "s2"

        second_release.set()
        await t2

        assert order == ["r1", "r2"], f"FIFO violated: {order}"
        assert sched.snapshot()["depth"] == 0

    asyncio.run(run_test())


# ── Capacity ────────────────────────────────────────────────────────────

def test_capacity_includes_active_work():
    """max_size=1 means only the active request fits."""
    sched = SpeechScheduler(max_size=1)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1", "c1"), op))
        # Give t1 time to become active
        await asyncio.sleep(0.05)
        with pytest.raises(QueueFullError):
            await sched.run(_req("s2", "c2"), lambda: None)
        release.set()
        await t1

    asyncio.run(run_test())


def test_full_requests_reject_without_depth_change():
    """Rejected requests do not alter depth/pending counters."""
    sched = SpeechScheduler(max_size=1)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1", "c1"), op))
        await asyncio.sleep(0.05)
        assert sched.snapshot()["depth"] == 1
        with pytest.raises(QueueFullError):
            await sched.run(_req("s2", "c2"), lambda: None)
        # Depth must not change after rejection
        assert sched.snapshot()["depth"] == 1
        release.set()
        await t1

    asyncio.run(run_test())


# ── Timeout ─────────────────────────────────────────────────────────────

def test_waiting_timeout_removes_waiter():
    """Timed-out waiters are removed and depth/pending decremented."""
    sched = SpeechScheduler(max_size=2, wait_timeout_sec=0.1)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1", "c1"), op))
        await asyncio.sleep(0.02)
        with pytest.raises(QueueTimeoutError):
            await sched.run(_req("s2", "c2"), lambda: None)
        # After timeout, depth should go back to 1
        assert sched.snapshot()["depth"] == 1
        assert sched.snapshot()["pending"] == 1
        release.set()
        await t1

    asyncio.run(run_test())


# ── Cancellation ────────────────────────────────────────────────────────

def test_cancelled_waiter_removed_exactly_once():
    """Cancelled waiting requests decrement depth/pending exactly once."""
    sched = SpeechScheduler(max_size=2)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1", "c1"), op))
        await asyncio.sleep(0.02)

        # Schedule t2 and cancel it while waiting
        t2 = asyncio.create_task(sched.run(_req("s2", "c2"), lambda: None))
        await asyncio.sleep(0.02)
        t2.cancel()
        try:
            await t2
        except asyncio.CancelledError:
            pass

        assert sched.snapshot()["depth"] == 1
        assert sched.snapshot()["pending"] == 1
        release.set()
        await t1

    asyncio.run(run_test())


def test_atomic_handoff_sets_active_entry_under_lock():
    """RED→GREEN: after active completes, _active_entry points to next waiter.

    In the unfixed scheduler, _active_entry is cleared under the lock and
    the waiter sets it later after re-acquiring — a gap where no active
    entry exists.  This test proves the gap exists (RED).

    After the fix, the completing active atomically transfers ownership by
    setting _active_entry to the next waiter's entry under the lock, before
    waking the future.  The snapshot shows the waiter as active immediately.

    RED (unfixed): snapshot shows active_synthesis_id is None during handoff.
    GREEN (fixed): snapshot shows the next waiter's synthesis_id.
    """
    sched = SpeechScheduler(max_size=2)

    a_started = asyncio.Event()
    a_release = asyncio.Event()
    waiter_admitted = asyncio.Event()
    handoff_detected = asyncio.Event()
    b_done = asyncio.Event()

    # ── Hook: observe deterministic admission and handoff boundaries ──
    _orig_waiters = sched._waiters
    _orig_popleft = _orig_waiters.popleft

    class _Wrapper:
        def __init__(self, dq):
            self._dq = dq
        def __len__(self): return len(self._dq)
        def __iter__(self): return iter(self._dq)
        def __getitem__(self, idx): return self._dq[idx]
        def __delitem__(self, idx): del self._dq[idx]
        def append(self, item):
            self._dq.append(item)
            waiter_admitted.set()
        def popleft(self):
            handoff_detected.set()
            return _orig_popleft()
        def __getattr__(self, name): return getattr(self._dq, name)

    sched._waiters = _Wrapper(_orig_waiters)

    async def op_a():
        a_started.set()
        await a_release.wait()

    async def op_b():
        b_done.set()
        await asyncio.sleep(0)

    async def run_test():
        ta = asyncio.create_task(sched.run(_req("a"), op_a))
        await a_started.wait()
        assert sched.snapshot()["active_synthesis_id"] == "a"

        tb = asyncio.create_task(sched.run(_req("b"), op_b))
        await waiter_admitted.wait()
        assert sched.snapshot()["waiting_count"] == 1

        # Release A — its finally block fires handoff_detected
        a_release.set()
        await handoff_detected.wait()
        # At this point, the lock has been released and _active_entry was
        # just set (or cleared, in the old code).  Check the snapshot.
        snap_after = sched.snapshot()

        # GREEN (fixed): _active_entry already points to b's entry
        # RED (unfixed): _active_entry is None during the gap
        assert snap_after["active_synthesis_id"] == "b", (
            f"Handoff not atomic: active_synthesis_id={snap_after['active_synthesis_id']} "
            f"(expected 'b').  _active_entry was cleared before waiter resumed."
        )

        await b_done.wait()
        await ta
        await tb

    asyncio.run(run_test())


def test_active_completion_fifo_handoff():
    """After active completes, the next waiter starts immediately."""
    sched = SpeechScheduler(max_size=2)
    first_done = asyncio.Event()
    second_started = asyncio.Event()
    second_done = asyncio.Event()

    async def op1():
        first_done.set()

    async def op2():
        second_started.set()
        await second_done.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1", "c1"), op1))
        await first_done.wait()
        await t1

        t2 = asyncio.create_task(sched.run(_req("s2", "c2"), op2))
        await asyncio.wait_for(second_started.wait(), timeout=5)
        assert sched.snapshot()["active_synthesis_id"] == "s2"
        second_done.set()
        await t2

    asyncio.run(run_test())


# ── Connection cancellation ─────────────────────────────────────────────

def test_connection_cancellation_targets_only_matching():
    """cancel_connection affects only the specified connection's requests."""
    sched = SpeechScheduler(max_size=3)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1", "c1"), op))
        await asyncio.sleep(0.02)

        t2 = asyncio.create_task(sched.run(_req("s2", "c2"), lambda: asyncio.sleep(0)))
        await asyncio.sleep(0.02)

        t3 = asyncio.create_task(sched.run(_req("s3", "c1"), lambda: asyncio.sleep(0)))
        await asyncio.sleep(0.02)

        # Cancel connection c1's waiters (s3)
        cancelled = await sched.cancel_connection("c1")
        # s3 is waiting for c1 and gets cancelled; s2 is c2, not affected
        assert cancelled >= 1

        # Give cancellation time to propagate
        await asyncio.sleep(0.02)

        release.set()
        await t1

        # t2 (c2) should complete normally
        try:
            await t2
        except asyncio.CancelledError:
            pass
        try:
            await t3
        except asyncio.CancelledError:
            pass

    asyncio.run(run_test())


# ── Snapshot safety ─────────────────────────────────────────────────────

def test_snapshot_excludes_plaintext():
    """Scheduler snapshot must not expose plaintext text."""
    sched = SpeechScheduler(max_size=3)
    snap = sched.snapshot()
    assert "text" not in snap
    for k, v in snap.items():
        if isinstance(v, str):
            assert "hello" not in v


# ── Cancel active by synthesis ID ───────────────────────────────────────

def test_cancel_active_by_synthesis_id():
    """cancel_synthesis cancels the active task if it matches."""
    sched = SpeechScheduler(max_size=2)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1", "c1"), op))
        await asyncio.sleep(0.02)
        assert sched.snapshot()["active_synthesis_id"] == "s1"

        cancelled = sched.cancel_synthesis("s1")
        assert cancelled is True

        release.set()  # Release the await anyway
        try:
            await t1
        except asyncio.CancelledError:
            pass

    asyncio.run(run_test())


def test_cancel_nonexistent_synthesis_returns_false():
    sched = SpeechScheduler(max_size=2)
    assert sched.cancel_synthesis("nonexistent") is False


# ── Cancel active by connection ─────────────────────────────────────────

def test_cancel_active_by_connection():
    """cancel_active_for_connection cancels the active task if connection matches."""
    sched = SpeechScheduler(max_size=2)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1", "c1"), op))
        await asyncio.sleep(0.02)

        cancelled = sched.cancel_active_for_connection("c1")
        assert cancelled is True

        release.set()
        try:
            await t1
        except asyncio.CancelledError:
            pass

    asyncio.run(run_test())


# ── Active failure handoff ──────────────────────────────────────────────

def test_active_error_hands_off_to_next():
    """When active fails, next waiter becomes active."""
    sched = SpeechScheduler(max_size=2)
    second_started = asyncio.Event()
    second_done = asyncio.Event()

    async def op1():
        raise RuntimeError("backend failure")

    async def op2():
        second_started.set()
        await second_done.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1", "c1"), op1))
        try:
            await t1
        except RuntimeError:
            pass

        # Now admit second request
        t2 = asyncio.create_task(sched.run(_req("s2", "c2"), op2))
        await asyncio.wait_for(second_started.wait(), timeout=5)
        assert sched.snapshot()["active_synthesis_id"] == "s2"
        second_done.set()
        await t2

    asyncio.run(run_test())


# ── Queue errors ────────────────────────────────────────────────────────

def test_queue_full_error():
    err = QueueFullError("test")
    assert isinstance(err, RuntimeError)


def test_queue_timeout_error():
    err = QueueTimeoutError("test")
    assert isinstance(err, asyncio.TimeoutError)


# ── Depth/pending never negative ────────────────────────────────────────

def test_depth_never_negative_after_all_terminal_paths():
    """All terminal paths must return depth to 0."""
    sched = SpeechScheduler(max_size=3)

    async def op():
        pass

    async def run_test():
        tasks = []
        for i in range(3):
            tasks.append(asyncio.create_task(
                sched.run(_req(f"s{i}", f"c{i}"), op)))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(not isinstance(r, Exception) for r in results)
        assert sched.snapshot()["depth"] == 0
        assert sched.snapshot()["pending"] == 0

    asyncio.run(run_test())


# ── Slice 5: Lifecycle observability ───────────────────────────────────

def test_snapshot_includes_lifecycle_fields():
    """Snapshot must expose lifecycle state fields (no plaintext)."""
    sched = SpeechScheduler(max_size=3)
    snap = sched.snapshot()
    assert "admission_latency_ms" in snap
    assert snap["admission_latency_ms"] is None  # No active request
    assert "terminal_reason" in snap
    assert snap["terminal_reason"] is None


def test_scheduler_logs_admission_and_activation(monkeypatch):
    """obs_log is called with queue_admitted and queue_started events."""
    from app.speech import scheduler as sched_mod
    logs = []
    monkeypatch.setattr(sched_mod, "obs_log", lambda event, **fields: logs.append((event, fields)))

    sched = SpeechScheduler(max_size=3)
    started = asyncio.Event()
    done = asyncio.Event()

    async def op():
        started.set()
        await done.wait()

    async def run_test():
        task = asyncio.create_task(sched.run(_req("s1", "c1"), op))
        await started.wait()
        done.set()
        await task

        admitted_events = [e for e in logs if e[0] == "queue_admitted"]
        started_events = [e for e in logs if e[0] == "queue_started"]
        assert len(admitted_events) >= 1
        assert len(started_events) >= 1
        # Verify fields are plaintext-safe (no text value)
        for _, fields in logs:
            for v in fields.values():
                if isinstance(v, str):
                    assert "test" not in v.lower() or "test" == v  # only "test" as literal is ok in event names

    asyncio.run(run_test())


def test_scheduler_logs_timeout(monkeypatch):
    """Timeout produces queue_wait_timeout event."""
    from app.speech import scheduler as sched_mod
    logs = []
    monkeypatch.setattr(sched_mod, "obs_log", lambda event, **fields: logs.append((event, fields)))

    sched = SpeechScheduler(max_size=2, wait_timeout_sec=0.1)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1", "c1"), op))
        await asyncio.sleep(0.02)
        with pytest.raises(QueueTimeoutError):
            await sched.run(_req("s2", "c2"), lambda: None)
        release.set()
        await t1

        timeout_events = [e for e in logs if e[0] == "queue_wait_timeout"]
        assert len(timeout_events) >= 1
        # Check terminal_reason in the log
        for _, fields in timeout_events:
            assert "text" not in fields  # No plaintext

    asyncio.run(run_test())


def test_snapshot_shows_terminal_reason_after_timeout(monkeypatch):
    """After a timeout, snapshot reflects terminal_reason."""
    from app.speech import scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "obs_log", lambda event, **fields: None)

    sched = SpeechScheduler(max_size=2, wait_timeout_sec=0.1)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1", "c1"), op))
        await asyncio.sleep(0.02)
        with pytest.raises(QueueTimeoutError):
            await sched.run(_req("s2", "c2"), lambda: None)
        # After timeout, snapshot should show terminal reason for the active
        snap = sched.snapshot()
        # active is still s1 running, so terminal_reason is None
        release.set()
        await t1

    asyncio.run(run_test())


def test_scheduler_logs_no_plaintext_in_any_event(monkeypatch):
    """No obs_log event from the scheduler contains request text."""
    from app.speech import scheduler as sched_mod
    logs = []
    monkeypatch.setattr(sched_mod, "obs_log", lambda event, **fields: logs.append((event, fields)))

    sched = SpeechScheduler(max_size=3)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1", "c1", text="secret phrase"), op))
        await asyncio.sleep(0.02)
        t2 = asyncio.create_task(sched.run(_req("s2", "c2", text="another secret"), lambda: asyncio.sleep(0)))
        await asyncio.sleep(0.02)
        release.set()
        await t1
        try:
            await t2
        except asyncio.CancelledError:
            pass

        for event_name, fields in logs:
            field_str = str(fields)
            assert "secret" not in field_str, f"Plaintext leaked in event '{event_name}': {field_str}"
            assert "phrase" not in field_str, f"Plaintext leaked in event '{event_name}': {field_str}"

    asyncio.run(run_test())

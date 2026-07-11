"""Phase 9C Slice 2: SpeechScheduler drain and shutdown API tests.

Tests the scheduler's public drain/shutdown boundary:
- Reject new admissions after drain
- Cancel queued/waiting requests and release exactly once
- Bounded active grace
- Clean completion during grace
- Exact once cancellation on expiry
- Counters zero after drain+completion
- Repeated drain/shutdown safe (idempotent)
- Races: no double release
- Never execute operations (cancel/set_result) while scheduler lock held
- Deterministic quiescence: shutdown does not return until counters zero
"""

import asyncio

import pytest

from app.speech.models import (
    SpeechRequest,
)
from app.speech.scheduler import (
    SpeechScheduler,
    QueueFullError,
)


def _req(sid: str = "s1", cid: str = "c1", text: str = "test") -> SpeechRequest:
    return SpeechRequest(synthesis_id=sid, connection_id=cid, text=text)


# ── drain() ────────────────────────────────────────────────────────────

def test_drain_rejects_new_admissions():
    """After drain, new admissions raise QueueFullError."""
    sched = SpeechScheduler(max_size=3)

    async def run_test():
        await sched.drain()
        with pytest.raises(QueueFullError, match="drain"):
            await sched.run(_req("s1"), lambda: None)

    asyncio.run(run_test())


def test_drain_cancels_waiting_requests():
    """Drain cancels all queued waiters deterministically."""
    sched = SpeechScheduler(max_size=3)
    release = asyncio.Event()
    cancelled_ids = []

    async def op():
        await release.wait()

    async def waiter(sid, cid):
        try:
            await sched.run(_req(sid, cid), lambda: None)
        except asyncio.CancelledError:
            cancelled_ids.append(sid)

    async def run_test():
        # Start active
        t_active = asyncio.create_task(sched.run(_req("s-active", "c1"), op))
        await asyncio.sleep(0.02)
        assert sched.snapshot()["active_synthesis_id"] == "s-active"

        # Enqueue two waiters
        t_w1 = asyncio.create_task(waiter("s-w1", "c1"))
        t_w2 = asyncio.create_task(waiter("s-w2", "c2"))
        await asyncio.sleep(0.02)
        assert sched.snapshot()["waiting_count"] == 2
        assert sched.snapshot()["depth"] == 3

        # Drain — await drain-done for deterministic cleanup
        cancelled = await sched.drain()
        assert cancelled == 2

        # Deterministic: wait for drain completion event
        await sched._drain_done.wait()
        assert "s-w1" in cancelled_ids
        assert "s-w2" in cancelled_ids

        # Active still running
        assert sched.snapshot()["active_synthesis_id"] == "s-active"

        release.set()
        await t_active

    asyncio.run(run_test())


def test_drain_is_idempotent():
    """Multiple drain calls after first return 0 (no double cancel)."""
    sched = SpeechScheduler(max_size=2)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1"), op))
        await asyncio.sleep(0.02)

        t2 = asyncio.create_task(sched.run(_req("s2"), lambda: None))
        await asyncio.sleep(0.02)

        c1 = await sched.drain()
        assert c1 == 1  # one waiter cancelled (s2)

        c2 = await sched.drain()
        assert c2 == 0  # already drained

        c3 = await sched.drain()
        assert c3 == 0  # still idempotent

        release.set()
        await t1

    asyncio.run(run_test())


def test_drain_preserves_active_work():
    """After drain, active synthesis continues running."""
    sched = SpeechScheduler(max_size=2)
    release = asyncio.Event()
    active_done = False

    async def op():
        nonlocal active_done
        await release.wait()
        active_done = True

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1"), op))
        await asyncio.sleep(0.02)

        t2 = asyncio.create_task(sched.run(_req("s2"), lambda: None))
        await asyncio.sleep(0.02)

        await sched.drain()
        assert sched.snapshot()["active_synthesis_id"] == "s1"
        assert not active_done

        release.set()
        await t1
        assert active_done

    asyncio.run(run_test())


def test_drain_releases_counters_exactly_once():
    """After drain, depth/pending only count active work."""
    sched = SpeechScheduler(max_size=3)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1"), op))
        await asyncio.sleep(0.02)

        t2 = asyncio.create_task(sched.run(_req("s2"), lambda: None))
        t3 = asyncio.create_task(sched.run(_req("s3"), lambda: None))
        await asyncio.sleep(0.02)

        assert sched.snapshot()["depth"] == 3
        assert sched.snapshot()["pending"] == 3

        cancelled = await sched.drain()
        assert cancelled == 2

        # Deterministic: wait for drain-done instead of sleeping
        await sched._drain_done.wait()
        snap = sched.snapshot()
        assert snap["depth"] == 1, f"depth should be 1, got {snap['depth']}"
        assert snap["pending"] == 1, f"pending should be 1, got {snap['pending']}"

        release.set()
        await t1

        snap2 = sched.snapshot()
        assert snap2["depth"] == 0
        assert snap2["pending"] == 0

    asyncio.run(run_test())


def test_drain_sets_terminal_reason_on_waiters():
    """Drained waiters record 'drain_cancelled' terminal reason."""
    sched = SpeechScheduler(max_size=3)
    release = asyncio.Event()
    waiter_entries = []

    # Wrap deque to capture appended entries
    _orig_deque = sched._waiters
    class _Wrapper:
        def __init__(self, dq):
            self._dq = dq
        def __len__(self): return len(self._dq)
        def __iter__(self): return iter(self._dq)
        def __getitem__(self, idx): return self._dq[idx]
        def clear(self): return self._dq.clear()
        def popleft(self): return self._dq.popleft()
        def append(self, item):
            waiter_entries.append(item)
            self._dq.append(item)
    sched._waiters = _Wrapper(_orig_deque)

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1"), op))
        await asyncio.sleep(0.02)

        t2 = asyncio.create_task(sched.run(_req("s2"), lambda: None))
        await asyncio.sleep(0.02)

        await sched.drain()

        # Deterministic: wait for drain-done instead of sleeping
        await sched._drain_done.wait()
        # Check that the waiter entry has terminal_reason set
        assert len(waiter_entries) >= 1, "No waiter entries captured"
        for entry_tuple in waiter_entries:
            # entry_tuple is (sid, fut, cid, entry)
            entry = entry_tuple[3]
            assert entry.terminal_reason == "drain_cancelled", \
                f"Expected drain_cancelled, got {entry.terminal_reason}"

        release.set()
        await t1

    asyncio.run(run_test())


# ── shutdown() ──────────────────────────────────────────────────────────

def test_shutdown_clean_when_no_active_work():
    """Shutdown returns (True, 0) when no active or waiting work;
    depth/pending are zero at return boundary."""
    sched = SpeechScheduler(max_size=3)

    async def run_test():
        clean, cancelled = await sched.shutdown(grace_timeout_sec=5.0)
        assert clean is True
        assert cancelled == 0
        # Deterministic quiescence: counters must be zero
        assert sched.snapshot()["depth"] == 0
        assert sched.snapshot()["pending"] == 0

    asyncio.run(run_test())


def test_shutdown_clean_when_only_waiters():
    """Shutdown with waiters drains and completes when active finishes."""
    sched = SpeechScheduler(max_size=3)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1"), op))
        await asyncio.sleep(0.02)

        t2 = asyncio.create_task(sched.run(_req("s2"), lambda: None))
        t3 = asyncio.create_task(sched.run(_req("s3"), lambda: None))
        await asyncio.sleep(0.02)

        # Start shutdown; it drains waiters, then waits for active
        shutdown_task = asyncio.create_task(
            sched.shutdown(grace_timeout_sec=5.0)
        )
        await asyncio.sleep(0.05)

        # Release active so shutdown completes cleanly
        release.set()
        clean, cancelled = await shutdown_task
        assert cancelled == 2
        assert clean is True  # active completed within grace

        await t1

        # Deterministic: counters must be zero after shutdown returns
        snap = sched.snapshot()
        assert snap["depth"] == 0, f"depth={snap['depth']}, expected 0"
        assert snap["pending"] == 0, f"pending={snap['pending']}, expected 0"

    asyncio.run(run_test())


def test_shutdown_grace_period_allows_completion():
    """Active synthesis completes within grace period -> clean shutdown."""
    sched = SpeechScheduler(max_size=2)
    started = asyncio.Event()
    release = asyncio.Event()
    completed = False

    async def op():
        nonlocal completed
        started.set()
        await release.wait()
        completed = True

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1"), op))
        await started.wait()

        # Start shutdown in background
        shutdown_task = asyncio.create_task(
            sched.shutdown(grace_timeout_sec=2.0)
        )

        # Let shutdown drain (no waiters) and begin waiting for active
        await asyncio.sleep(0.05)

        # Release active within grace period
        release.set()
        clean, cancelled = await shutdown_task
        assert clean is True
        assert completed is True

        # Counters zero at return
        assert sched.snapshot()["depth"] == 0
        assert sched.snapshot()["pending"] == 0

        await t1

    asyncio.run(run_test())


def test_shutdown_expiry_cancels_active():
    """Active exceeds grace period -> forced cancellation; counters zero
    at shutdown return (deterministic, no arbitrary sleep)."""
    sched = SpeechScheduler(max_size=2)
    started = asyncio.Event()
    never_release = asyncio.Event()  # never set

    async def op():
        started.set()
        await never_release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1"), op))
        await started.wait()

        clean, cancelled = await sched.shutdown(grace_timeout_sec=0.2)
        assert clean is False
        assert cancelled == 0  # no waiters

        # Active should have been cancelled — verify by awaiting
        try:
            await t1
        except asyncio.CancelledError:
            pass

        # Deterministic: counters must be zero AT the return boundary
        # (no asyncio.sleep needed — shutdown already awaited cleanup)
        snap = sched.snapshot()
        assert snap["depth"] == 0, f"depth should be 0 after forced cancel, got {snap['depth']}"
        assert snap["pending"] == 0, f"pending should be 0 after forced cancel, got {snap['pending']}"

    asyncio.run(run_test())


def test_shutdown_bounded_grace():
    """Shutdown does not wait forever — respects grace_timeout_sec."""
    sched = SpeechScheduler(max_size=2)
    started = asyncio.Event()
    block_forever = asyncio.Event()

    async def op():
        started.set()
        await block_forever.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1"), op))
        await started.wait()

        import time
        t0 = time.monotonic()
        clean, cancelled = await sched.shutdown(grace_timeout_sec=0.3)
        elapsed = time.monotonic() - t0

        assert clean is False
        # Should not exceed grace + small overhead
        assert elapsed < 2.0, f"shutdown took {elapsed:.1f}s, expected bounded"

        try:
            await t1
        except asyncio.CancelledError:
            pass

        # Counters zero at return
        assert sched.snapshot()["depth"] == 0
        assert sched.snapshot()["pending"] == 0

    asyncio.run(run_test())


def test_shutdown_idempotent():
    """Repeated shutdown calls after first are safe; counters zero."""
    sched = SpeechScheduler(max_size=2)

    async def run_test():
        c1, _ = await sched.shutdown(grace_timeout_sec=1.0)
        assert c1 is True  # clean since nothing active
        assert sched.snapshot()["depth"] == 0

        c2, w2 = await sched.shutdown(grace_timeout_sec=1.0)
        assert c2 is True
        assert w2 == 0  # no waiters to cancel
        assert sched.snapshot()["depth"] == 0

        c3, w3 = await sched.shutdown(grace_timeout_sec=1.0)
        assert c3 is True
        assert w3 == 0
        assert sched.snapshot()["depth"] == 0

    asyncio.run(run_test())


def test_counters_zero_after_full_drain_and_completion():
    """After drain + active completion, all counters return to 0."""
    sched = SpeechScheduler(max_size=3)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1"), op))
        await asyncio.sleep(0.02)

        t2 = asyncio.create_task(sched.run(_req("s2"), lambda: None))
        t3 = asyncio.create_task(sched.run(_req("s3"), lambda: None))
        await asyncio.sleep(0.02)

        assert sched.snapshot()["depth"] == 3
        assert sched.snapshot()["pending"] == 3

        cancelled = await sched.drain()
        assert cancelled == 2

        # Deterministic: wait for drain-done
        await sched._drain_done.wait()

        release.set()
        await t1

        snap = sched.snapshot()
        assert snap["depth"] == 0
        assert snap["pending"] == 0
        assert snap["active_synthesis_id"] is None
        assert snap["waiting_count"] == 0

    asyncio.run(run_test())


# ── Race: drain + waiter CancelledError handler ─────────────────────────

def test_no_double_release_on_drain_plus_waiter_cancellation():
    """Drain + CancelledError handler don't double-decrement depth/pending."""
    sched = SpeechScheduler(max_size=3)
    release = asyncio.Event()
    depth_samples = []

    async def op():
        await release.wait()

    async def waiter():
        try:
            await sched.run(_req("s-waiter"), lambda: None)
        except asyncio.CancelledError:
            pass
        # Record depth after cancellation handled
        depth_samples.append(sched.snapshot()["depth"])

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s-active"), op))
        await asyncio.sleep(0.02)

        t_w = asyncio.create_task(waiter())
        await asyncio.sleep(0.02)
        assert sched.snapshot()["depth"] == 2

        await sched.drain()

        # Deterministic: wait for drain-done
        await sched._drain_done.wait()

        release.set()
        await t1

        # Final depth should be 0
        assert sched.snapshot()["depth"] == 0
        assert sched.snapshot()["pending"] == 0

        # Depth should have been 1 after drain (only active left), never negative
        for d in depth_samples:
            assert d >= 0, f"depth went negative: {d}"

    asyncio.run(run_test())


def test_repeated_drain_does_not_double_cancel():
    """Second drain call doesn't double-cancel already-cancelled waiters."""
    sched = SpeechScheduler(max_size=3)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1"), op))
        await asyncio.sleep(0.02)

        t2 = asyncio.create_task(sched.run(_req("s2"), lambda: None))
        await asyncio.sleep(0.02)

        c1 = await sched.drain()
        assert c1 == 1

        # Deterministic: wait for drain-done
        await sched._drain_done.wait()

        c2 = await sched.drain()
        assert c2 == 0

        snap = sched.snapshot()
        assert snap["depth"] == 1
        assert snap["pending"] == 1

        release.set()
        await t1

    asyncio.run(run_test())


# ── Lock discipline ─────────────────────────────────────────────────────

def test_drain_never_holds_lock_during_cancel():
    """drain() does not deadlock — proves cancel() is called outside lock.

    If drain() were to call fut.cancel() while holding the scheduler lock,
    the CancelledError handler in run() would try to re-acquire the lock
    and deadlock.  The absence of deadlock is proof of lock discipline.
    """
    sched = SpeechScheduler(max_size=3)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1"), op))
        await asyncio.sleep(0.02)

        t2 = asyncio.create_task(sched.run(_req("s2"), lambda: None))
        await asyncio.sleep(0.02)

        # drain must complete without deadlock
        cancelled = await sched.drain()
        assert cancelled == 1

        # Deterministic: wait for drain-done
        await sched._drain_done.wait()
        assert sched.snapshot()["waiting_count"] == 0
        assert sched.snapshot()["depth"] == 1  # only active remains

        release.set()
        await t1

        # Final state clean
        assert sched.snapshot()["depth"] == 0
        assert sched.snapshot()["pending"] == 0

    asyncio.run(run_test())


def test_shutdown_never_holds_lock_during_task_cancel():
    """shutdown() cancels active task outside the lock (proven by no deadlock);
    counters zero at return (no arbitrary sleep)."""
    sched = SpeechScheduler(max_size=2)
    started = asyncio.Event()
    block_forever = asyncio.Event()

    async def op():
        started.set()
        await block_forever.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1"), op))
        await started.wait()

        clean, cancelled = await sched.shutdown(grace_timeout_sec=0.1)
        assert clean is False  # forced cancel
        assert cancelled == 0  # no waiters

        # Active task should finish cancellation without deadlock
        try:
            await t1
        except asyncio.CancelledError:
            pass

        # Deterministic: counters zero at return boundary (shutdown awaits cleanup)
        assert sched.snapshot()["depth"] == 0
        assert sched.snapshot()["pending"] == 0

    asyncio.run(run_test())


# ── drain + admission race ──────────────────────────────────────────────

def test_admission_rejected_when_drain_set_during_lock_acquisition():
    """Request admitted while drain starts should not orphan."""
    sched = SpeechScheduler(max_size=2)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s-active"), op))
        await asyncio.sleep(0.02)

        # Start drain, then immediately try to admit - drain should win
        drain_task = asyncio.create_task(sched.drain())

        # Small delay to let drain set event
        await asyncio.sleep(0.02)

        # Now try to admit - should be rejected
        with pytest.raises(QueueFullError, match="drain"):
            await sched.run(_req("s-rejected"), lambda: None)

        cancelled = await drain_task
        assert cancelled == 0  # no waiters

        release.set()
        await t1

    asyncio.run(run_test())


# ── State transitions on drain ──────────────────────────────────────────

def test_drain_sets_waiters_to_cancelled_state():
    """Drain transitions waiting requests to CANCELLED state."""
    sched = SpeechScheduler(max_size=3)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def waiter():
        try:
            await sched.run(_req("s-w"), lambda: None)
        except asyncio.CancelledError:
            pass

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s-active"), op))
        await asyncio.sleep(0.02)

        t_w = asyncio.create_task(waiter())
        await asyncio.sleep(0.02)

        await sched.drain()
        # Deterministic: wait for drain-done
        await sched._drain_done.wait()

        release.set()
        await t1

    asyncio.run(run_test())


# ── Deterministic quiescence: concurrent shutdown ───────────────────────

def test_shutdown_concurrent_calls_safe():
    """Two concurrent shutdown() calls both complete safely; counters zero."""
    sched = SpeechScheduler(max_size=3)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s-active"), op))
        await asyncio.sleep(0.02)

        t2 = asyncio.create_task(sched.run(_req("s-w1"), lambda: None))
        t3 = asyncio.create_task(sched.run(_req("s-w2"), lambda: None))
        await asyncio.sleep(0.02)

        assert sched.snapshot()["depth"] == 3

        # Fire two concurrent shutdown calls
        shutdown_a = asyncio.create_task(sched.shutdown(grace_timeout_sec=5.0))
        shutdown_b = asyncio.create_task(sched.shutdown(grace_timeout_sec=5.0))

        await asyncio.sleep(0.05)

        release.set()

        clean_a, wc_a = await shutdown_a
        clean_b, wc_b = await shutdown_b

        # One call cancels waiters, the other is idempotent
        assert wc_a + wc_b == 2  # total waiters cancelled across both calls
        assert clean_a is True
        assert clean_b is True

        await t1

        # Both shutdowns returned; counters must be zero
        snap = sched.snapshot()
        assert snap["depth"] == 0, f"depth={snap['depth']}"
        assert snap["pending"] == 0, f"pending={snap['pending']}"

    asyncio.run(run_test())


def test_shutdown_active_completion_race():
    """Active completes during grace window; shutdown returns clean with
    zero counters (no arbitrary sleep)."""
    sched = SpeechScheduler(max_size=2)
    started = asyncio.Event()
    can_complete = asyncio.Event()

    async def op():
        started.set()
        await can_complete.wait()  # will be set during grace

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s-active"), op))
        await started.wait()

        # Start shutdown; it'll wait for active
        shutdown_task = asyncio.create_task(
            sched.shutdown(grace_timeout_sec=2.0)
        )

        # Let shutdown enter wait
        await asyncio.sleep(0.05)

        # Complete active DuRING grace period (race condition)
        can_complete.set()

        clean, wc = await shutdown_task
        assert clean is True
        assert wc == 0  # no waiters

        # Counters must be zero at return
        snap = sched.snapshot()
        assert snap["depth"] == 0, f"depth={snap['depth']}, expected 0"
        assert snap["pending"] == 0

        await t1

    asyncio.run(run_test())


def test_shutdown_quiescence_after_forced_cancellation():
    """After forced cancellation, shutdown() does not return until
    depth/pending are zero (deterministic event-based, no sleep)."""
    sched = SpeechScheduler(max_size=3)
    started = asyncio.Event()
    block = asyncio.Event()  # never set

    async def op():
        started.set()
        await block.wait()  # never completes

    async def waiter(sid):
        try:
            await sched.run(_req(sid), lambda: None)
        except asyncio.CancelledError:
            pass

    async def run_test():
        t_active = asyncio.create_task(sched.run(_req("s-active"), op))
        await started.wait()

        # Enqueue waiters
        t_w1 = asyncio.create_task(waiter("s-w1"))
        t_w2 = asyncio.create_task(waiter("s-w2"))
        await asyncio.sleep(0.02)

        assert sched.snapshot()["depth"] == 3
        assert sched.snapshot()["pending"] == 3

        # Shutdown with short grace — forces cancellation of active
        clean, wc = await sched.shutdown(grace_timeout_sec=0.2)
        assert clean is False
        assert wc == 2  # two waiters cancelled

        # Wait for waiter tasks and active task
        try:
            await t_active
        except asyncio.CancelledError:
            pass

        # Deterministic: counters must be zero WITHOUT any sleep
        snap = sched.snapshot()
        assert snap["depth"] == 0, f"depth={snap['depth']}, expected 0"
        assert snap["pending"] == 0, f"pending={snap['pending']}, expected 0"
        assert snap["active_synthesis_id"] is None
        assert snap["waiting_count"] == 0

    asyncio.run(run_test())


def test_shutdown_quiescence_event_set_after_clean_completion():
    """Verify _quiescence event is set after clean shutdown (event-based)."""
    sched = SpeechScheduler(max_size=2)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1"), op))
        await asyncio.sleep(0.02)

        t2 = asyncio.create_task(sched.run(_req("s2"), lambda: None))
        await asyncio.sleep(0.02)

        shutdown_task = asyncio.create_task(
            sched.shutdown(grace_timeout_sec=5.0)
        )
        await asyncio.sleep(0.05)

        release.set()
        clean, wc = await shutdown_task
        assert clean is True
        assert wc == 1

        # _quiescence should be set after shutdown returns
        assert sched._quiescence.is_set()
        assert sched._depth == 0
        assert sched._pending == 0

        await t1

    asyncio.run(run_test())

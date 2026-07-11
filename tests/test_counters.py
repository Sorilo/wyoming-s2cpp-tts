"""Phase 9C Slice 5: Cumulative counters tests.

Tests the CumulativeCounters class and its integration with:
- SpeechScheduler (admission, terminal outcomes)
- Admin HTTP /status and /metrics endpoints
- build_status_snapshot and build_metrics_snapshot helpers

Verifies:
- Counters are monotonic
- All terminal outcomes map correctly
- Repeated shutdown does not increment counters
- No IDs, text, secrets, or mutable objects in snapshots
- Snapshot is JSON-serializable
- Integration with scheduler: admission, rejection, success, cancel, timeout, fail
- Counters exposed through admin HTTP endpoints
- Concurrent/deterministic race safety
- Exactly-once terminal accounting (terminal_counted guard)
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from app.counters import CumulativeCounters, build_counters_snapshot
from app.speech.scheduler import (
    SpeechScheduler,
    QueueFullError,
    QueueTimeoutError,
    _record_terminal_once,
)
from app.speech.models import SpeechRequest, ScheduledSpeech
from app.admin_http import build_status_snapshot, build_metrics_snapshot
from app.lifecycle import ServiceLifecycle
from app.config import Settings


def _req(sid: str = "s1", cid: str = "c1", text: str = "test") -> SpeechRequest:
    return SpeechRequest(synthesis_id=sid, connection_id=cid, text=text)


# ── CumulativeCounters unit tests ─────────────────────────────────────────

def test_counters_initial_state():
    """All counters start at 0."""
    c = CumulativeCounters()
    snap = c.snapshot()
    assert snap["admitted"] == 0
    assert snap["admission_rejected"] == 0
    assert snap["completed_success"] == 0
    assert snap["cancelled_queued"] == 0
    assert snap["cancelled_active"] == 0
    assert snap["timed_out"] == 0
    assert snap["failed"] == 0
    assert snap["backend_busy_retries"] == 0


def test_counters_snapshot_is_immutable_dict():
    """Snapshot returns a plain dict, not the internal state."""
    c = CumulativeCounters()
    snap1 = c.snapshot()
    snap2 = c.snapshot()
    assert snap1 == snap2
    assert snap1 is not snap2  # different dict objects each time


def test_counters_record_methods_are_monotonic():
    """Each record method only increases its counter (synchronous, lock-protected)."""
    c = CumulativeCounters()

    c.record_admitted()
    c.record_rejected()

    snap = c.snapshot()
    assert snap["admitted"] == 1
    assert snap["admission_rejected"] == 1
    # Others still zero
    assert snap["completed_success"] == 0
    assert snap["cancelled_queued"] == 0
    assert snap["cancelled_active"] == 0
    assert snap["timed_out"] == 0
    assert snap["failed"] == 0


def test_counters_record_terminal_maps_correctly():
    """record_terminal maps terminal_reason strings to correct counters."""
    c = CumulativeCounters()

    # completed → completed_success
    c.record_terminal("completed")
    assert c.snapshot()["completed_success"] == 1

    # cancelled_while_waiting → cancelled_queued
    c.record_terminal("cancelled_while_waiting")
    assert c.snapshot()["cancelled_queued"] == 1

    # drain_cancelled → cancelled_queued
    c.record_terminal("drain_cancelled")
    assert c.snapshot()["cancelled_queued"] == 2

    # cancelled_while_active → cancelled_active
    c.record_terminal("cancelled_while_active")
    assert c.snapshot()["cancelled_active"] == 1

    # queue_wait_timeout → timed_out
    c.record_terminal("queue_wait_timeout")
    assert c.snapshot()["timed_out"] == 1

    # synthesis_timeout → timed_out
    c.record_terminal("synthesis_timeout")
    assert c.snapshot()["timed_out"] == 2

    # operation_failed → failed
    c.record_terminal("operation_failed")
    assert c.snapshot()["failed"] == 1

    # Unknown reason is silently ignored
    c.record_terminal("unknown_reason")
    assert c.snapshot()["failed"] == 1  # unchanged

    # None terminal reason is silently ignored
    c.record_terminal(None)  # type: ignore[arg-type]
    assert c.snapshot()["failed"] == 1  # unchanged


def test_counters_no_double_count():
    """record_terminal on same entry only counts once (caller responsibility).
    The counter itself doesn't deduplicate — scheduler enforces via terminal_counted."""
    c = CumulativeCounters()

    c.record_terminal("completed")
    c.record_terminal("completed")
    # Called twice, counter adds 1 each time — no dedup in counters.
    assert c.snapshot()["completed_success"] == 2


def test_counters_backend_busy_retries():
    """Backend busy retries counter increments (synchronous)."""
    c = CumulativeCounters()
    c.record_backend_busy_retries(3)
    assert c.snapshot()["backend_busy_retries"] == 3


def test_counters_backend_busy_retries_validation():
    """record_backend_busy_retries rejects count <= 0."""
    c = CumulativeCounters()
    with pytest.raises(ValueError, match="must be positive"):
        c.record_backend_busy_retries(0)
    with pytest.raises(ValueError, match="must be positive"):
        c.record_backend_busy_retries(-1)


def test_counters_snapshot_is_json_serializable():
    """Snapshot must be JSON-serializable (safe scalars only)."""
    c = CumulativeCounters()
    c.record_terminal("completed")
    c.record_terminal("cancelled_while_waiting")

    snap = c.snapshot()
    encoded = json.dumps(snap, sort_keys=True)
    decoded = json.loads(encoded)
    assert decoded["completed_success"] == 1
    assert decoded["cancelled_queued"] == 1


def test_counters_no_sensitive_keys():
    """Snapshot never contains IDs, text, secrets, or mutable objects."""
    c = CumulativeCounters()
    snap = c.snapshot()

    forbidden = {
        "synthesis_id", "connection_id", "text", "audio",
        "secret", "token", "password", "env", "stack", "trace",
        "task", "future", "request",
    }
    for key in forbidden:
        assert key not in snap, f"Forbidden key present: {key}"


def test_build_counters_snapshot_none():
    """build_counters_snapshot with None returns empty dict."""
    result = build_counters_snapshot(None)
    assert result == {}


def test_build_counters_snapshot_with_counters():
    """build_counters_snapshot delegates to CumulativeCounters.snapshot()."""
    c = CumulativeCounters()
    c.record_terminal("completed")
    result = build_counters_snapshot(c)
    assert result["completed_success"] == 1


# ── Scheduler integration tests ────────────────────────────────────────────


def test_scheduler_admitted_increments_counter():
    """Admission increments the admitted counter."""
    counters = CumulativeCounters()
    sched = SpeechScheduler(max_size=3, counters=counters)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        task = asyncio.create_task(sched.run(_req("s1"), op))
        await asyncio.sleep(0)
        assert counters.snapshot()["admitted"] == 1
        release.set()
        await task

    asyncio.run(run_test())


def test_scheduler_completed_increments_success():
    """Successful completion increments completed_success."""
    counters = CumulativeCounters()
    sched = SpeechScheduler(max_size=3, counters=counters)

    async def op():
        pass

    async def run_test():
        await sched.run(_req("s1"), op)
        assert counters.snapshot()["admitted"] == 1
        assert counters.snapshot()["completed_success"] == 1

    asyncio.run(run_test())


def test_scheduler_rejection_increments_counter():
    """QueueFullError increments admission_rejected."""
    counters = CumulativeCounters()
    sched = SpeechScheduler(max_size=1, counters=counters)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1"), op))
        await asyncio.sleep(0)

        with pytest.raises(QueueFullError):
            await sched.run(_req("s2"), op)

        assert counters.snapshot()["admitted"] == 1
        assert counters.snapshot()["admission_rejected"] == 1

        release.set()
        await t1

    asyncio.run(run_test())


def test_scheduler_drain_rejection_increments_counter():
    """Drain rejection increments admission_rejected."""
    counters = CumulativeCounters()
    sched = SpeechScheduler(max_size=3, counters=counters)

    async def run_test():
        await sched.drain()
        with pytest.raises(QueueFullError):
            await sched.run(_req("s1"), lambda: None)
        assert counters.snapshot()["admission_rejected"] == 1
        assert counters.snapshot()["admitted"] == 0

    asyncio.run(run_test())


def test_scheduler_cancelled_while_waiting():
    """Cancelled waiter increments cancelled_queued."""
    counters = CumulativeCounters()
    sched = SpeechScheduler(max_size=2, counters=counters)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1", "c1"), op))
        await asyncio.sleep(0)

        t2 = asyncio.create_task(sched.run(_req("s2", "c2"), op))
        await asyncio.sleep(0)

        cancelled = await sched.cancel_connection("c2")
        assert cancelled == 1

        try:
            await t2
        except asyncio.CancelledError:
            pass

        assert counters.snapshot()["admitted"] == 2
        assert counters.snapshot()["cancelled_queued"] == 1

        release.set()
        await t1

    asyncio.run(run_test())


def test_scheduler_cancelled_while_active():
    """Cancelled active synthesis increments cancelled_active."""
    counters = CumulativeCounters()
    sched = SpeechScheduler(max_size=3, counters=counters)
    started = asyncio.Event()
    blocker = asyncio.Event()

    async def op():
        started.set()
        await blocker.wait()

    async def run_test():
        task = asyncio.create_task(sched.run(_req("s1"), op))
        await started.wait()

        assert sched.cancel_synthesis("s1") is True

        try:
            await task
        except asyncio.CancelledError:
            pass

        assert counters.snapshot()["admitted"] == 1
        assert counters.snapshot()["cancelled_active"] == 1

    asyncio.run(run_test())


def test_scheduler_wait_timeout_increments_timed_out():
    """Queue wait timeout increments timed_out."""
    counters = CumulativeCounters()
    sched = SpeechScheduler(max_size=2, wait_timeout_sec=0.05, counters=counters)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1"), op))
        await asyncio.sleep(0)

        with pytest.raises(QueueTimeoutError):
            await sched.run(_req("s2"), op)

        assert counters.snapshot()["admitted"] == 2
        assert counters.snapshot()["timed_out"] == 1

        release.set()
        await t1

    asyncio.run(run_test())


def test_scheduler_operation_failed_increments_failed():
    """Unhandled exception in operation increments failed."""
    counters = CumulativeCounters()
    sched = SpeechScheduler(max_size=3, counters=counters)

    async def op():
        raise ValueError("simulated failure")

    async def run_test():
        with pytest.raises(ValueError):
            await sched.run(_req("s1"), op)

        assert counters.snapshot()["admitted"] == 1
        assert counters.snapshot()["failed"] == 1

    asyncio.run(run_test())


def test_scheduler_synthesis_timeout_increments_timed_out():
    """Operation timeout increments timed_out (synthesis_timeout)."""
    counters = CumulativeCounters()
    sched = SpeechScheduler(max_size=3, counters=counters)
    blocker = asyncio.Event()

    async def op():
        try:
            await asyncio.wait_for(blocker.wait(), timeout=0.01)
        except asyncio.TimeoutError:
            raise

    async def run_test():
        with pytest.raises(asyncio.TimeoutError):
            await sched.run(_req("s1"), op)

        assert counters.snapshot()["admitted"] == 1
        assert counters.snapshot()["timed_out"] == 1

    asyncio.run(run_test())


def test_scheduler_drain_cancelled_increments_cancelled_queued():
    """Drain-cancelled waiters increment cancelled_queued."""
    counters = CumulativeCounters()
    sched = SpeechScheduler(max_size=2, counters=counters)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1"), op))
        await asyncio.sleep(0)

        t2 = asyncio.create_task(sched.run(_req("s2"), op))
        await asyncio.sleep(0)

        cancelled = await sched.drain()
        assert cancelled == 1

        try:
            await t2
        except asyncio.CancelledError:
            pass

        assert counters.snapshot()["admitted"] == 2
        assert counters.snapshot()["cancelled_queued"] == 1

        release.set()
        await t1

    asyncio.run(run_test())


def test_scheduler_multiple_admissions():
    """Multiple admissions are counted correctly."""
    counters = CumulativeCounters()
    sched = SpeechScheduler(max_size=5, counters=counters)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        tasks = []
        for i in range(3):
            t = asyncio.create_task(
                sched.run(_req(f"s{i}", f"c{i}"), op)
            )
            tasks.append(t)
            await asyncio.sleep(0)

        assert counters.snapshot()["admitted"] == 3

        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)

        assert counters.snapshot()["completed_success"] == 3

    asyncio.run(run_test())


def test_scheduler_terminal_outcomes_not_double_counted():
    """Each request's terminal outcome is counted exactly once."""
    counters = CumulativeCounters()
    sched = SpeechScheduler(max_size=5, counters=counters)

    async def op_success():
        pass

    async def op_fail():
        raise ValueError("fail")

    async def run_test():
        await sched.run(_req("s1"), op_success)
        with pytest.raises(ValueError):
            await sched.run(_req("s2"), op_fail)

        snap = counters.snapshot()
        assert snap["admitted"] == 2
        assert snap["completed_success"] == 1
        assert snap["failed"] == 1
        assert snap["completed_success"] + snap["failed"] == 2

    asyncio.run(run_test())


# ── Repeated shutdown does not increment counters ──────────────────────────


def test_repeated_shutdown_no_counter_increment():
    """Repeated shutdown calls do not increment outcome counters."""
    counters = CumulativeCounters()
    sched = SpeechScheduler(max_size=3, counters=counters)

    async def run_test():
        await sched.drain()
        snap1 = counters.snapshot()

        await sched.drain()
        snap2 = counters.snapshot()
        assert snap2 == snap1

        clean, cancelled = await sched.shutdown(1.0)
        assert clean is True
        assert cancelled == 0

        snap3 = counters.snapshot()
        assert snap3 == snap1

        clean2, cancelled2 = await sched.shutdown(1.0)
        assert clean2 is True
        assert cancelled2 == 0

        snap4 = counters.snapshot()
        assert snap4 == snap1

    asyncio.run(run_test())


# ── Snapshot helpers with counters ─────────────────────────────────────────


def test_status_snapshot_includes_counters():
    """build_status_snapshot includes counters when provided."""
    lifecycle = ServiceLifecycle()
    settings = Settings()
    counters_snap = {
        "admitted": 5, "admission_rejected": 2,
        "completed_success": 3, "cancelled_queued": 1,
        "cancelled_active": 0, "timed_out": 1,
        "failed": 0, "backend_busy_retries": 0,
    }

    snap = build_status_snapshot(
        lifecycle, settings, counters_snapshot=counters_snap,
    )

    assert "counters" in snap
    assert snap["counters"]["admitted"] == 5
    assert snap["counters"]["admission_rejected"] == 2
    assert snap["counters"]["completed_success"] == 3


def test_status_snapshot_without_counters():
    """build_status_snapshot without counters does not include the key."""
    lifecycle = ServiceLifecycle()
    settings = Settings()

    snap = build_status_snapshot(lifecycle, settings)
    assert "counters" not in snap


def test_metrics_snapshot_includes_counters():
    """build_metrics_snapshot includes counters and schema_version."""
    lifecycle = ServiceLifecycle()
    settings = Settings()
    counters_snap = {"admitted": 10, "completed_success": 8}

    snap = build_metrics_snapshot(
        lifecycle, settings, counters_snapshot=counters_snap,
    )

    assert snap["schema_version"] == "1.0"
    assert "counters" in snap
    assert snap["counters"]["admitted"] == 10
    assert snap["counters"]["completed_success"] == 8


def test_metrics_snapshot_without_counters():
    """build_metrics_snapshot without counters does not include the key."""
    lifecycle = ServiceLifecycle()
    settings = Settings()

    snap = build_metrics_snapshot(lifecycle, settings)
    assert "counters" not in snap
    assert snap["schema_version"] == "1.0"


# ── Admin HTTP integration (counters in endpoints) ──────────────────────────


def _settings_admin(**overrides):
    kwargs = {
        "admin_http_enabled": True,
        "admin_http_host": "127.0.0.1",
        "admin_http_port": 0,
        "admin_http_read_timeout_sec": 5.0,
        "admin_http_max_header_size": 8192,
        "admin_http_max_body_size": 65536,
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


async def _http_get(host: str, port: int, path: str, timeout: float = 5.0) -> tuple[int, dict | str]:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=timeout,
    )
    try:
        request = f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n"
        writer.write(request.encode())
        await writer.drain()

        response = b""
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            response += chunk
            if b"\r\n\r\n" in response:
                header_end = response.index(b"\r\n\r\n") + 4
                headers_part = response[:header_end].decode("utf-8", errors="replace")
                cl_match = None
                for line in headers_part.split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        cl_match = int(line.split(":", 1)[1].strip())
                        break
                if cl_match is not None:
                    body = response[header_end:]
                    if len(body) >= cl_match:
                        break
                else:
                    break
    finally:
        writer.close()
        await writer.wait_closed()

    resp_str = response.decode("utf-8", errors="replace")
    lines = resp_str.split("\r\n")
    status_line = lines[0] if lines else ""
    parts = status_line.split(" ", 2)
    status_code = int(parts[1]) if len(parts) >= 2 else 0

    body_start = resp_str.index("\r\n\r\n") + 4 if "\r\n\r\n" in resp_str else len(resp_str)
    body = resp_str[body_start:]

    try:
        return status_code, json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return status_code, body


@pytest.mark.asyncio
async def test_admin_status_includes_counters():
    """GET /status includes counters when admin server has them wired."""
    from app.admin_http import AdminHttpServer

    counters = CumulativeCounters()
    counters.record_terminal("completed")
    counters.record_terminal("completed")

    s = _settings_admin()
    lifecycle = ServiceLifecycle()
    lifecycle.transition_to_running()

    server = AdminHttpServer(
        settings=s,
        lifecycle=lifecycle,
        get_scheduler_snapshot=lambda: {"active_synthesis_id": None, "depth": 0, "pending": 0, "max_size": 3, "waiting_count": 0},
        get_counters_snapshot=lambda: counters.snapshot(),
    )
    port = await server.start()
    try:
        status, body = await _http_get("127.0.0.1", port, "/status")
        assert status == 200
        assert "counters" in body
        assert body["counters"]["completed_success"] == 2
        assert body["counters"]["admitted"] == 0
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_admin_metrics_includes_counters_and_schema_version():
    """GET /metrics includes counters and schema_version."""
    from app.admin_http import AdminHttpServer

    counters = CumulativeCounters()
    counters.record_terminal("cancelled_while_waiting")

    s = _settings_admin()
    lifecycle = ServiceLifecycle()
    lifecycle.transition_to_running()

    server = AdminHttpServer(
        settings=s,
        lifecycle=lifecycle,
        get_counters_snapshot=lambda: counters.snapshot(),
    )
    port = await server.start()
    try:
        status, body = await _http_get("127.0.0.1", port, "/metrics")
        assert status == 200
        assert body["schema_version"] == "1.0"
        assert "counters" in body
        assert body["counters"]["cancelled_queued"] == 1
    finally:
        await server.stop()


# ── Concurrent race safety ──────────────────────────────────────────────────


def test_concurrent_admissions_all_counted():
    """Concurrent admissions from multiple tasks are all counted."""
    counters = CumulativeCounters()
    sched = SpeechScheduler(max_size=10, counters=counters)
    release = asyncio.Event()
    started_count = 0

    async def op():
        nonlocal started_count
        started_count += 1
        if started_count >= 5:
            release.set()
        await release.wait()

    async def run_test():
        tasks = []
        for i in range(5):
            t = asyncio.create_task(
                sched.run(_req(f"s{i}", f"c{i}"), op)
            )
            tasks.append(t)
            await asyncio.sleep(0)

        assert counters.snapshot()["admitted"] == 5

        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)

        assert counters.snapshot()["completed_success"] == 5

    asyncio.run(run_test())


def test_scheduler_without_counters_still_works():
    """Scheduler without counters does not crash (backward compat)."""
    sched = SpeechScheduler(max_size=3)

    async def op():
        pass

    async def run_test():
        await sched.run(_req("s1"), op)
        snap = sched.snapshot()
        assert snap["depth"] == 0

    asyncio.run(run_test())


def test_counters_immutable_across_snapshots():
    """Snapshot values are not affected by subsequent increments."""
    c = CumulativeCounters()

    c.record_admitted()
    snap1 = c.snapshot()
    c.record_admitted()
    snap2 = c.snapshot()

    assert snap1["admitted"] == 1
    assert snap2["admitted"] == 2
    assert snap1["admitted"] == 1  # unchanged


# ── Exactly-once terminal accounting tests ──────────────────────────────────


def test_terminal_counted_guard_prevents_double_count():
    """_record_terminal_once only records the first call per entry."""
    counters = CumulativeCounters()
    entry = ScheduledSpeech(request=_req("s1"))
    entry.terminal_reason = "completed"

    # We need a scheduler with counters to test the guard
    sched = SpeechScheduler(max_size=3, counters=counters)

    # First call records
    _record_terminal_once(sched, entry)
    assert counters.snapshot()["completed_success"] == 1
    assert entry.terminal_counted is True

    # Second call is no-op
    _record_terminal_once(sched, entry)
    assert counters.snapshot()["completed_success"] == 1  # still 1

    # Third call also no-op
    _record_terminal_once(sched, entry)
    assert counters.snapshot()["completed_success"] == 1  # still 1


def test_terminal_counted_guard_with_none_reason():
    """_record_terminal_once marks counted=True but skips when reason is None."""
    counters = CumulativeCounters()
    entry = ScheduledSpeech(request=_req("s1"))
    entry.terminal_reason = None

    sched = SpeechScheduler(max_size=3, counters=counters)

    _record_terminal_once(sched, entry)
    assert entry.terminal_counted is True
    assert counters.snapshot()["completed_success"] == 0  # nothing recorded

    # Even if reason is set later, second call is still no-op
    entry.terminal_reason = "completed"
    _record_terminal_once(sched, entry)
    assert counters.snapshot()["completed_success"] == 0  # still nothing


def test_terminal_counted_guard_without_counters():
    """_record_terminal_once is safe when counters is None."""
    entry = ScheduledSpeech(request=_req("s1"))
    entry.terminal_reason = "completed"

    sched = SpeechScheduler(max_size=3)  # no counters

    # Should not raise
    _record_terminal_once(sched, entry)
    assert entry.terminal_counted is True


def test_terminal_counted_race_regression():
    """Repeated accounting via helper cannot double-count, even when
    intentionally called multiple times simulating evolved code paths."""
    counters = CumulativeCounters()
    sched = SpeechScheduler(max_size=3, counters=counters)
    entry = ScheduledSpeech(request=_req("s1"))
    entry.terminal_reason = "completed"

    # Simulate what would happen if both the finalizer and a timeout
    # handler called _record_terminal_once — only the first wins.
    _record_terminal_once(sched, entry)  # "finalizer"
    _record_terminal_once(sched, entry)  # "timeout handler" — no-op
    _record_terminal_once(sched, entry)  # "cancel handler" — no-op

    assert counters.snapshot()["completed_success"] == 1


def test_scheduler_admitted_only_after_capacity_checks():
    """Admitted counter incremented only after capacity/drain checks pass
    and depth/pending are incremented."""
    counters = CumulativeCounters()
    sched = SpeechScheduler(max_size=1, counters=counters)
    release = asyncio.Event()

    async def op():
        await release.wait()

    async def run_test():
        t1 = asyncio.create_task(sched.run(_req("s1"), op))
        await asyncio.sleep(0)

        # First request admitted
        assert counters.snapshot()["admitted"] == 1

        # Second request rejected — no new admit
        with pytest.raises(QueueFullError):
            await sched.run(_req("s2"), op)

        # Still only 1 admitted
        assert counters.snapshot()["admitted"] == 1
        assert counters.snapshot()["admission_rejected"] == 1

        release.set()
        await t1

    asyncio.run(run_test())


def test_thread_safety_concurrent_snapshots():
    """Snapshot is thread-safe when called concurrently (threading.Lock)."""
    c = CumulativeCounters()
    c.record_admitted()
    c.record_terminal("completed")

    errors = []

    def snapshot_worker():
        try:
            snap = c.snapshot()
            assert snap["admitted"] >= 1
            assert snap["completed_success"] >= 1
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=snapshot_worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Thread safety errors: {errors}"


def test_thread_safety_concurrent_mutations():
    """Concurrent record_terminal calls are thread-safe."""
    c = CumulativeCounters()

    def mutator():
        for _ in range(100):
            c.record_terminal("completed")
            c.record_terminal("cancelled_while_waiting")

    threads = [threading.Thread(target=mutator) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = c.snapshot()
    expected = 10 * 100
    assert snap["completed_success"] == expected
    assert snap["cancelled_queued"] == expected

"""Phase 9C Slice 5: Bounded cumulative process-lifetime metrics counters.

Thread-safe monotonic counters exposed through sanitized
/status and /metrics endpoints.  No labels, cardinality, IDs, plaintext,
secrets, request bodies, audio, environment variables, or tasks ever
appear in snapshots.

Design:
- Single ``CumulativeCounters`` owner shared across scheduler,
  coordinator, and admin HTTP server.
- All mutation protected by a ``threading.Lock`` — synchronous methods
  safe to call from any thread or event-loop task without external
  locking.  No async/await needed, so callers inside asyncio lock
  regions do not risk re-entrant deadlocks.
- ``snapshot()`` returns an immutable dict of counter values suitable
  for JSON serialisation (acquires lock, copies values, releases).
- Counters are monotonic by construction — ``record_*`` methods only
  increment; there is no decrement or reset.
- Repeated shutdown does not increment outcome counters (enforced at
  the scheduler level via ``terminal_counted`` guard, not here).
"""

from __future__ import annotations

import threading
from typing import Any


class CumulativeCounters:
    """Monotonic process-lifetime cumulative counters.

    All counters start at 0 and only increase.  Gauges (depth, pending,
    active, connections, readiness) remain scheduler/lifecycle snapshots
    — this class owns only cumulative terminal counters.

    Thread safety: all mutation is protected by a ``threading.Lock``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # ── Admission ───────────────────────────────────────────────
        self._admitted: int = 0
        self._admission_rejected: int = 0

        # ── Terminal outcomes ───────────────────────────────────────
        self._completed_success: int = 0
        self._cancelled_queued: int = 0    # cancelled while waiting (drain / timeout / connection)
        self._cancelled_active: int = 0    # cancelled while actively synthesising
        self._timed_out: int = 0           # queue wait timeout or synthesis timeout
        self._failed: int = 0              # operation_failed (unhandled exception)

        # ── Backend busy retries ────────────────────────────────────
        # Not currently wired to any production hook — the backend-busy
        # retry loop in wyoming_server.py does not call this counter.
        # The field exists for future instrumentation and remains zero
        # in production.  Do not fabricate increments with mock hooks.
        self._backend_busy_retries: int = 0

    # ── Synchronous public API (thread-safe via internal Lock) ──────

    def record_admitted(self) -> None:
        """Increment admitted counter.  Call after capacity/drain checks
        pass and depth/pending have been incremented."""
        with self._lock:
            self._admitted += 1

    def record_rejected(self) -> None:
        """Increment admission_rejected counter.  Call once per failed
        run() invocation (drain rejection or queue-full rejection)."""
        with self._lock:
            self._admission_rejected += 1

    def record_terminal(self, terminal_reason: str) -> None:
        """Record one terminal outcome from a scheduler terminal_reason.

        Maps terminal_reason strings to cumulative counters.  Callers
        MUST ensure each scheduler entry is counted exactly once
        (the scheduler enforces this via ``terminal_counted`` guard).

        Unknown / None terminal reasons are silently ignored.
        """
        with self._lock:
            if terminal_reason == "completed":
                self._completed_success += 1
            elif terminal_reason in ("cancelled_while_waiting", "drain_cancelled"):
                self._cancelled_queued += 1
            elif terminal_reason == "cancelled_while_active":
                self._cancelled_active += 1
            elif terminal_reason in ("queue_wait_timeout", "synthesis_timeout"):
                self._timed_out += 1
            elif terminal_reason == "operation_failed":
                self._failed += 1

    def record_backend_busy_retries(self, count: int) -> None:
        """Increment backend_busy_retries by *count*.

        Raises ValueError if count <= 0.  Not wired to any production
        hook — remains zero until a retry path is instrumented.
        """
        if count <= 0:
            raise ValueError(f"count must be positive, got {count}")
        with self._lock:
            self._backend_busy_retries += count

    # ── Snapshot ────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """Return an immutable dict of cumulative counter values.

        Acquires the lock, copies all values into a new dict, releases.
        Safe for JSON serialisation.  Never exposes IDs, text, audio,
        secrets, or mutable objects.
        """
        with self._lock:
            return {
                "admitted": self._admitted,
                "admission_rejected": self._admission_rejected,
                "completed_success": self._completed_success,
                "cancelled_queued": self._cancelled_queued,
                "cancelled_active": self._cancelled_active,
                "timed_out": self._timed_out,
                "failed": self._failed,
                "backend_busy_retries": self._backend_busy_retries,
            }


def build_counters_snapshot(
    counters: CumulativeCounters | None,
) -> dict[str, Any]:
    """Helper that safely snapshots counters for admin endpoints.

    Returns an empty dict when *counters* is None.
    """
    if counters is None:
        return {}
    return counters.snapshot()

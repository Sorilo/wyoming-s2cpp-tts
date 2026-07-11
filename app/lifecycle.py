"""Phase 9C: Service lifecycle state machine and coordinator.

Provides:
- LifecycleState: closed enum for service state transitions
- LifecycleSnapshot: immutable sanitized operational snapshot
- ServiceLifecycle: idempotent lifecycle owner with bounded shutdown
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any


class LifecycleState(Enum):
    """Closed lifecycle state for the Wyoming TTS service.

    Transition table:
        STARTING -> RUNNING
        STARTING -> FAILED
        RUNNING  -> DRAINING
        RUNNING  -> FAILED
        DRAINING -> STOPPING
        DRAINING -> FAILED
        STOPPING -> STOPPED
        STOPPING -> FAILED
        STOPPED  (terminal)
        FAILED   (terminal)
    """

    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    def is_terminal(self) -> bool:
        """Return True if this is a terminal state (STOPPED or FAILED)."""
        return self in (LifecycleState.STOPPED, LifecycleState.FAILED)

    def is_ready(self) -> bool:
        """Return True if the service is ready to accept traffic (RUNNING)."""
        return self == LifecycleState.RUNNING

    def accepts_new_work(self) -> bool:
        """Return True if new synthesis work can be admitted."""
        return self == LifecycleState.RUNNING


@dataclass(frozen=True)
class LifecycleSnapshot:
    """Immutable sanitized operational snapshot.

    Never exposes plaintext text, raw audio, secrets, tokens,
    environment dumps, or mutable async objects.
    """

    state: LifecycleState = LifecycleState.STARTING
    ready: bool = False
    uptime_sec: float = 0.0
    pending_count: int = 0
    depth: int = 0
    max_queue_size: int = 0
    active_connections: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict copy with only safe scalar values."""
        return {
            "state": self.state.value if isinstance(self.state, LifecycleState) else str(self.state),
            "ready": self.ready,
            "uptime_sec": self.uptime_sec,
            "pending_count": self.pending_count,
            "depth": self.depth,
            "max_queue_size": self.max_queue_size,
            "active_connections": self.active_connections,
        }

DEFAULT_SHUTDOWN_GRACE_TIMEOUT_SEC = 30.0


class ServiceLifecycle:
    """Single lifecycle owner coordinating startup readiness,
    shutdown/drain sequencing, signal handling, and terminal state.

    Shutdown is idempotent: repeated calls to start_draining() after
    the first are no-ops and return False.
    """

    def __init__(self, shutdown_grace_timeout_sec: float = DEFAULT_SHUTDOWN_GRACE_TIMEOUT_SEC) -> None:
        if shutdown_grace_timeout_sec <= 0:
            raise ValueError("shutdown_grace_timeout_sec must be positive")
        self._state = LifecycleState.STARTING
        self._started_monotonic = time.monotonic()
        self.shutdown_grace_timeout_sec = shutdown_grace_timeout_sec

    # -- Properties --

    @property
    def state(self) -> LifecycleState:
        return self._state

    @property
    def ready(self) -> bool:
        return self._state.is_ready()

    @property
    def uptime_sec(self) -> float:
        return time.monotonic() - self._started_monotonic
    # -- Transitions --

    def transition_to_running(self) -> None:
        """Transition from STARTING to RUNNING.

        Raises RuntimeError if not in STARTING.
        """
        if self._state != LifecycleState.STARTING:
            raise RuntimeError(
                f"Cannot transition to RUNNING from {self._state.value}"
            )
        self._state = LifecycleState.RUNNING

    def transition_to_failed(self) -> None:
        """Transition to FAILED from any non-terminal state."""
        if self._state.is_terminal():
            return
        self._state = LifecycleState.FAILED

    def start_draining(self) -> bool:
        """Initiate drain. Returns True if this call started draining,
        False if already draining/stopping/stopped/failed.
        """
        if self._state in (
            LifecycleState.DRAINING,
            LifecycleState.STOPPING,
            LifecycleState.STOPPED,
            LifecycleState.FAILED,
        ):
            return False
        if self._state not in (LifecycleState.RUNNING, LifecycleState.STARTING):
            return False
        self._state = LifecycleState.DRAINING
        return True

    def transition_to_stopping(self) -> None:
        """Transition from DRAINING to STOPPING.

        Raises RuntimeError if not in DRAINING.
        """
        if self._state != LifecycleState.DRAINING:
            raise RuntimeError(
                f"Cannot transition to STOPPING from {self._state.value}"
            )
        self._state = LifecycleState.STOPPING

    def transition_to_stopped(self) -> None:
        """Transition from STOPPING to STOPPED.

        Raises RuntimeError if not in STOPPING.
        """
        if self._state != LifecycleState.STOPPING:
            raise RuntimeError(
                f"Cannot transition to STOPPED from {self._state.value}"
            )
        self._state = LifecycleState.STOPPED

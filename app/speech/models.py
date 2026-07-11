"""Phase 9B domain value objects and lifecycle model.

Immutable SpeechMetadata and SpeechRequest, closed SpeechState lifecycle
enum, and scheduler-owned ScheduledSpeech.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


class SpeechState(Enum):
    """Closed lifecycle enum for speech synthesis scheduling.

    Transition table:
        CREATED -> REJECTED
        CREATED -> WAITING
        WAITING -> ACTIVE
        WAITING -> CANCELLED
        WAITING -> TIMED_OUT
        ACTIVE -> COMPLETED
        ACTIVE -> CANCELLED
        ACTIVE -> TIMED_OUT
        ACTIVE -> FAILED

    Terminal states (COMPLETED, CANCELLED, TIMED_OUT, FAILED, REJECTED)
    are idempotent — transitioning to the same terminal state is allowed.
    """

    CREATED = auto()
    WAITING = auto()
    ACTIVE = auto()
    COMPLETED = auto()
    CANCELLED = auto()
    TIMED_OUT = auto()
    FAILED = auto()
    REJECTED = auto()


# ── State transition table (module-level, avoids enum metaclass issues) ──
_STATE_TRANSITIONS: dict[SpeechState, frozenset[SpeechState]] = {
    SpeechState.CREATED: frozenset({SpeechState.WAITING, SpeechState.REJECTED}),
    SpeechState.WAITING: frozenset(
        {SpeechState.ACTIVE, SpeechState.CANCELLED, SpeechState.TIMED_OUT}
    ),
    SpeechState.ACTIVE: frozenset(
        {
            SpeechState.COMPLETED,
            SpeechState.CANCELLED,
            SpeechState.TIMED_OUT,
            SpeechState.FAILED,
        }
    ),
    # Terminal states allow idempotent self-transition
    SpeechState.COMPLETED: frozenset({SpeechState.COMPLETED}),
    SpeechState.CANCELLED: frozenset({SpeechState.CANCELLED}),
    SpeechState.TIMED_OUT: frozenset({SpeechState.TIMED_OUT}),
    SpeechState.FAILED: frozenset({SpeechState.FAILED}),
    SpeechState.REJECTED: frozenset({SpeechState.REJECTED}),
}

_STATE_TERMINALS = frozenset(
    {
        SpeechState.COMPLETED,
        SpeechState.CANCELLED,
        SpeechState.TIMED_OUT,
        SpeechState.FAILED,
        SpeechState.REJECTED,
    }
)


def _can_transition(from_state: SpeechState, to_state: SpeechState) -> bool:
    """Return True if *from_state* can legally transition to *to_state*."""
    allowed = _STATE_TRANSITIONS.get(from_state)
    if allowed is None:
        return False
    return to_state in allowed


def _is_terminal(state: SpeechState) -> bool:
    """Return True if *state* is a terminal state."""
    return state in _STATE_TERMINALS


# Attach helper methods to SpeechState for ergonomic access
SpeechState.can_transition = staticmethod(_can_transition)  # type: ignore[attr-defined]
SpeechState.is_terminal = staticmethod(_is_terminal)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class SpeechMetadata:
    """Immutable carrier for optional descriptive metadata.

    Reserved semantic fields are stored for future compatibility but
    never consulted by Phase 9B scheduling (FIFO ordering is unaffected).
    """

    voice: str | None = None
    trigger: str = "legacy"
    text_fingerprint: str | None = None
    semantic_priority: int | None = None
    replacement_key: str | None = None


@dataclass(frozen=True)
class SpeechRequest:
    """Immutable representation of one admitted unit of speech.

    Plaintext text must never appear in repr, snapshots, structured logs,
    log-facing exceptions, debug output, or lifecycle observability.

    Raises ValueError on empty or whitespace-only synthesis_id or
    connection_id.
    """

    synthesis_id: str
    connection_id: str
    text: str = field(repr=False)
    metadata: SpeechMetadata = field(default_factory=SpeechMetadata)
    created_monotonic: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if not self.synthesis_id.strip():
            raise ValueError("synthesis_id must be a non-empty string")
        if not self.connection_id.strip():
            raise ValueError("connection_id must be a non-empty string")


@dataclass
class ScheduledSpeech:
    """Scheduler-owned mutable record tracking one speech through lifecycle.

    Mutable fields are only modified under the scheduler lock.  Public
    snapshots are immutable dicts that never expose plaintext text, request
    objects containing text, task/future objects, or other mutable
    scheduler-owned internal state.
    """

    request: SpeechRequest
    state: SpeechState = SpeechState.CREATED
    admitted_monotonic: float = field(default_factory=time.monotonic)
    started_monotonic: float | None = None
    completed_monotonic: float | None = None
    terminal_reason: str | None = None

    def snapshot(self) -> dict[str, Any]:
        """Return an immutable plaintext-safe summary dict."""
        return {
            "synthesis_id": self.request.synthesis_id,
            "connection_id": self.request.connection_id,
            "state": self.state.name,
            "admitted_monotonic": self.admitted_monotonic,
            "started_monotonic": self.started_monotonic,
            "completed_monotonic": self.completed_monotonic,
            "terminal_reason": self.terminal_reason,
        }

"""Characterization and domain tests for Phase 9B speech models."""

import pytest
from dataclasses import FrozenInstanceError

from app.speech.models import (
    SpeechMetadata,
    SpeechRequest,
    SpeechState,
    ScheduledSpeech,
)


# ── SpeechMetadata ──────────────────────────────────────────────────────

def test_metadata_default_is_inert():
    """Default SpeechMetadata has all optional fields as None."""
    m = SpeechMetadata()
    assert m.voice is None
    assert m.trigger == "legacy"
    assert m.text_fingerprint is None
    assert m.semantic_priority is None
    assert m.replacement_key is None


def test_metadata_accepts_valid_fields():
    m = SpeechMetadata(
        voice="voice_a",
        trigger="streaming",
        text_fingerprint="abc123",
        semantic_priority=5,
        replacement_key="key1",
    )
    assert m.voice == "voice_a"
    assert m.trigger == "streaming"
    assert m.text_fingerprint == "abc123"
    assert m.semantic_priority == 5
    assert m.replacement_key == "key1"


def test_metadata_is_immutable():
    m = SpeechMetadata(voice="test")
    with pytest.raises(FrozenInstanceError):
        m.voice = "other"  # type: ignore[misc]


# ── SpeechRequest ───────────────────────────────────────────────────────

def test_request_rejects_empty_synthesis_id():
    with pytest.raises(ValueError, match="synthesis_id"):
        SpeechRequest(synthesis_id="", connection_id="c1", text="hello")


def test_request_rejects_empty_connection_id():
    with pytest.raises(ValueError, match="connection_id"):
        SpeechRequest(synthesis_id="s1", connection_id="", text="hello")


def test_request_rejects_whitespace_ids():
    with pytest.raises(ValueError, match="synthesis_id"):
        SpeechRequest(synthesis_id="   ", connection_id="c1", text="hello")
    with pytest.raises(ValueError, match="connection_id"):
        SpeechRequest(synthesis_id="s1", connection_id="  ", text="hello")


def test_request_text_excluded_from_repr():
    req = SpeechRequest(
        synthesis_id="abc123",
        connection_id="conn1",
        text="super secret text that must not leak",
        metadata=SpeechMetadata(voice="v1"),
    )
    r = repr(req)
    assert "abc123" in r
    assert "conn1" in r
    assert "secret" not in r
    assert "super" not in r
    assert "SpeechRequest" in r


def test_request_repr_includes_safe_fields():
    req = SpeechRequest(
        synthesis_id="syn1",
        connection_id="conn1",
        text="hello world",
        metadata=SpeechMetadata(voice="v1", trigger="streaming"),
    )
    r = repr(req)
    assert "syn1" in r
    assert "conn1" in r
    assert "v1" in r
    assert "streaming" in r
    # text must not appear as a key=value pair
    assert "text=" not in r


def test_request_is_immutable():
    req = SpeechRequest(synthesis_id="s1", connection_id="c1", text="hello")
    with pytest.raises(FrozenInstanceError):
        req.synthesis_id = "other"  # type: ignore[misc]


def test_request_created_monotonic_default():
    req = SpeechRequest(synthesis_id="s1", connection_id="c1", text="hello")
    assert isinstance(req.created_monotonic, float)
    assert req.created_monotonic > 0


def test_request_created_monotonic_can_be_set():
    req = SpeechRequest(
        synthesis_id="s1", connection_id="c1", text="hello", created_monotonic=100.5
    )
    assert req.created_monotonic == 100.5


# ── SpeechState ──────────────────────────────────────────────────────────

def test_state_transition_table_allows_legal_transitions():
    """Legal transitions from the plan must be permitted."""
    legal = [
        (SpeechState.CREATED, SpeechState.REJECTED),
        (SpeechState.CREATED, SpeechState.WAITING),
        (SpeechState.WAITING, SpeechState.ACTIVE),
        (SpeechState.WAITING, SpeechState.CANCELLED),
        (SpeechState.WAITING, SpeechState.TIMED_OUT),
        (SpeechState.ACTIVE, SpeechState.COMPLETED),
        (SpeechState.ACTIVE, SpeechState.CANCELLED),
        (SpeechState.ACTIVE, SpeechState.TIMED_OUT),
        (SpeechState.ACTIVE, SpeechState.FAILED),
    ]
    for from_state, to_state in legal:
        assert SpeechState.can_transition(from_state, to_state), (
            f"Expected {from_state} -> {to_state} to be legal"
        )


def test_state_transition_table_rejects_illegal_transitions():
    """Illegal transitions must be rejected."""
    illegal = [
        (SpeechState.CREATED, SpeechState.ACTIVE),  # must go through WAITING
        (SpeechState.CREATED, SpeechState.COMPLETED),
        (SpeechState.CREATED, SpeechState.FAILED),
        (SpeechState.WAITING, SpeechState.COMPLETED),
        (SpeechState.WAITING, SpeechState.FAILED),
        (SpeechState.WAITING, SpeechState.CREATED),  # no backward
        (SpeechState.ACTIVE, SpeechState.CREATED),
        (SpeechState.ACTIVE, SpeechState.WAITING),
        (SpeechState.ACTIVE, SpeechState.REJECTED),
        (SpeechState.COMPLETED, SpeechState.ACTIVE),
        (SpeechState.COMPLETED, SpeechState.CREATED),
        (SpeechState.CANCELLED, SpeechState.ACTIVE),
        (SpeechState.TIMED_OUT, SpeechState.ACTIVE),
        (SpeechState.FAILED, SpeechState.ACTIVE),
    ]
    for from_state, to_state in illegal:
        assert not SpeechState.can_transition(from_state, to_state), (
            f"Expected {from_state} -> {to_state} to be illegal"
        )


def test_terminal_transitions_are_idempotent():
    """Transitioning from a terminal state to the same terminal state is ok."""
    terminals = [
        SpeechState.COMPLETED,
        SpeechState.CANCELLED,
        SpeechState.TIMED_OUT,
        SpeechState.FAILED,
        SpeechState.REJECTED,
    ]
    for state in terminals:
        assert SpeechState.can_transition(state, state), (
            f"Expected {state} -> {state} to be allowed (idempotent)"
        )


def test_terminal_transitions_cannot_change():
    """Terminal states cannot transition to different terminal states."""
    terminals = [
        SpeechState.COMPLETED,
        SpeechState.CANCELLED,
        SpeechState.TIMED_OUT,
        SpeechState.FAILED,
        SpeechState.REJECTED,
    ]
    for a in terminals:
        for b in terminals:
            if a is not b:
                assert not SpeechState.can_transition(a, b), (
                    f"Expected {a} -> {b} to be illegal"
                )


def test_is_terminal():
    terminals = {
        SpeechState.COMPLETED,
        SpeechState.CANCELLED,
        SpeechState.TIMED_OUT,
        SpeechState.FAILED,
        SpeechState.REJECTED,
    }
    non_terminals = {
        SpeechState.CREATED,
        SpeechState.WAITING,
        SpeechState.ACTIVE,
    }
    for s in terminals:
        assert SpeechState.is_terminal(s)
    for s in non_terminals:
        assert not SpeechState.is_terminal(s)


def test_active_failure_transitions_to_failed():
    """Active non-timeout failure transitions to FAILED."""
    assert SpeechState.can_transition(SpeechState.ACTIVE, SpeechState.FAILED)
    assert not SpeechState.can_transition(SpeechState.WAITING, SpeechState.FAILED)
    assert SpeechState.is_terminal(SpeechState.FAILED)


# ── ScheduledSpeech ─────────────────────────────────────────────────────

def test_scheduled_speech_creation():
    req = SpeechRequest(synthesis_id="s1", connection_id="c1", text="hello")
    ss = ScheduledSpeech(request=req, state=SpeechState.CREATED, admitted_monotonic=1.0)
    assert ss.request is req
    assert ss.state == SpeechState.CREATED
    assert ss.admitted_monotonic == 1.0
    assert ss.started_monotonic is None
    assert ss.completed_monotonic is None
    assert ss.terminal_reason is None


def test_scheduled_speech_snapshot_is_immutable_and_safe():
    """Snapshot returns a dict without futures/tasks or plaintext text."""
    req = SpeechRequest(synthesis_id="s1", connection_id="c1", text="hello")
    ss = ScheduledSpeech(request=req, state=SpeechState.CREATED, admitted_monotonic=1.0)
    snap = ss.snapshot()
    assert isinstance(snap, dict)
    # Must not expose text
    assert "text" not in snap
    assert "hello" not in str(snap.values())
    # Must expose safe fields
    assert snap["synthesis_id"] == "s1"
    assert snap["connection_id"] == "c1"
    assert snap["state"] == "CREATED"
    assert snap["admitted_monotonic"] == 1.0
    # No internal task/future objects
    for v in snap.values():
        assert not hasattr(v, "set_result"), f"Snapshot leaked future/task: {v}"


def test_scheduled_speech_waiting_default_state_is_created():
    req = SpeechRequest(synthesis_id="s1", connection_id="c1", text="hello")
    ss = ScheduledSpeech(request=req, admitted_monotonic=1.0)
    assert ss.state == SpeechState.CREATED

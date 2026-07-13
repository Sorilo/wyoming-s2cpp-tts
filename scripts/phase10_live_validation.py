#!/usr/bin/env python3
"""Phase 10 live-validation harness — safe, mode-specific, dry-run by default.

Usage (dry-run — zero network/Docker):
    python scripts/phase10_live_validation.py --mode health

Usage (live — requires typed confirmation):
    export HA_TOKEN="your-token"
    python scripts/phase10_live_validation.py --mode normal --run-live

Modes:
    health          — Container health, Wyoming port, admin endpoint checks (read-only)
    normal          — Single TTS synthesis, verify normal flow
    media-stop      — HA media_player.media_stop during playback
    direct-disconnect — Direct Wyoming client disconnect during active stream
    overlap-recovery — Overlapping requests, verify cleanup and recovery
    vpe-barge-in    — VPE voice barge-in full cycle

Safety:
    - Default dry-run (no HA mutations, no Docker exec writes)
    - Double opt-in: type CONFIRMATION_PHRASE to proceed with live actions
    - HA_TOKEN from env var or gitignored env file, never hardcoded
    - Docker read-only: only ps/logs, no exec mutations
    - Artifacts in artifacts/phase10/<UTC>/
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import subprocess
import json
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

# ── Constants ──────────────────────────────────────────────────────

CONFIRMATION_PHRASE = "I-UNDERSTAND-THIS-IS-LIVE"
DOCKER_CONTAINERS = ("wyoming-s2cpp-tts", "s2cpp-backend")
DOCKER_INSPECT_FORMAT = (
    '{"id":{{json .Id}},"name":{{json .Name}},'
    '"image_name":{{json .Config.Image}},"image_id":{{json .Image}},'
    '"running":{{json .State.Running}},"state":{{json .State.Status}}}'
)
_SSH_HOST_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_SSH_USER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SINCE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+-]+Z?$")
# RFC 3339 prefix pattern for local log filtering (SSH aliases may ignore --since)
_RFC3339_PREFIX_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))\s"
)

LISTED_OUTCOMES = frozenset({
    "local_playback_stopped_only",
    "replacement_assist_run_created",
    "wrapper_synthesis_cancelled",
    "backend_request_aborted",
    "old_synthesis_completed_normally",
    "scheduler_queue_recovered",
    "second_request_succeeded",
    "cancel_before_output",
    "cancel_after_first_phrase",
    "cancel_then_replace",
    "pending_phrase_clearing",
    "barge_in_detected",
    "barge_in_not_detected",
    "playback_stopped",
    "playback_continued",
    "recovery_successful",
    "recovery_failed",
    "client_disconnected",
    "wrapper_cancel_requested",
    "wrapper_cancel_observed",
    "backend_abort_observed",
    "follow_up_synthesis_succeeded",
    "wrapper_outcome_classification",
    "backend_outcome_classification",
    "follow_up_synthesis_correlated",
    "original_fp",
    "follow_up_fp",
    "original_connection_id",
    "original_synthesis_id",
    "original_conn_closed",
    "follow_up_connection_id",
    "follow_up_synthesis_id",
    "cancellation_requested",
    "cancellation_propagated",
    "synthesis_terminal",
    "follow_up_completed",
})

REQUIRED_ARTIFACTS = (
    "report.md",
    "report.json",
    "timeline.json",
    "assertions.json",
    "wrapper.log",
    "backend.log",
    "ha_states.json",
    "wrapper_status_before.json",
    "wrapper_status_during.json",
    "wrapper_status_after.json",
)

PRODUCTION_CONTAINERS = ("wyoming-s2cpp-tts", "s2cpp-backend")

_ADMIN_DEFAULT_PORT = 10201


# ── Enums ──────────────────────────────────────────────────────────

class ValidationMode(Enum):
    """Validation operation modes."""
    HEALTH = "health"
    NORMAL = "normal"
    MEDIA_STOP = "media-stop"
    DIRECT_DISCONNECT = "direct-disconnect"
    OVERLAP_RECOVERY = "overlap-recovery"
    VPE_BARGE_IN = "vpe-barge-in"


# ── Configuration ──────────────────────────────────────────────────

@dataclass
class ValidationConfig:
    """Immutable configuration for a validation run."""

    mode: ValidationMode = ValidationMode.HEALTH
    dry_run: bool = True
    ha_url: str = ""
    ha_token: str = ""
    ha_token_env_var: str | None = "HA_TOKEN"
    wyoming_host: str = "127.0.0.1"
    wyoming_port: int = 10200
    admin_url: str = "http://127.0.0.1:10201"
    admin_port: int = _ADMIN_DEFAULT_PORT
    container_names: tuple[str, ...] = PRODUCTION_CONTAINERS
    artifact_base: Path = Path("artifacts/phase10")
    timeout_sec: float = 30.0
    allow_ha_actions: bool = False
    ha_ws_url: str = ""
    tts_text: str = "Hello, this is a Phase 10 validation test."
    tts_voice: str = "cmu_bdl_male_us"
    vpe_media_player: str = "media_player.home_assistant_voice_0acbe7_media_player"
    vpe_assist_satellite: str = "assist_satellite.home_assistant_voice_0acbe7_assist_satellite"
    disconnect_marker: str | None = None

    def __post_init__(self) -> None:
        if self.ha_token_env_var:
            token = os.getenv(self.ha_token_env_var, "")
            if token:
                object.__setattr__(self, "ha_token", token)
        if not self.ha_ws_url and self.ha_url:
            ws = self.ha_url.replace("http://", "ws://").replace("https://", "wss://")
            if not ws.startswith("ws"):
                ws = "ws://" + ws
            object.__setattr__(self, "ha_ws_url", ws + "/api/websocket")


# ── Result types ───────────────────────────────────────────────────

@dataclass
class AssertionResult:
    """Single assertion outcome."""
    name: str
    passed: bool
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationReport:
    """Complete validation report."""
    mode: str
    utc_timestamp: str
    dry_run: bool
    assertions: list[AssertionResult] = field(default_factory=list)
    outcomes: dict[str, str] = field(default_factory=dict)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ha_states: dict[str, Any] = field(default_factory=dict)
    status_snapshots: list[dict[str, Any]] = field(default_factory=list)
    wrapper_logs: list[str] = field(default_factory=list)
    backend_logs: list[str] = field(default_factory=list)
    duration_sec: float = 0.0

    @property
    def assertions_passed(self) -> int:
        return sum(1 for a in self.assertions if a.passed)

    @property
    def assertions_failed(self) -> int:
        return sum(1 for a in self.assertions if not a.passed)


# ── HA REST Client ─────────────────────────────────────────────────

class HaRestClient:
    """Minimal Home Assistant REST API client using stdlib urllib.

    Supports dependency injection via _request_fn for testing.
    """

    def __init__(self, ha_url: str, token: str):
        self.ha_url = ha_url.rstrip("/")
        self.token = token

    async def get_json(self, path: str) -> dict[str, Any]:
        """GET JSON from HA REST API."""
        import urllib.request
        url = f"{self.ha_url}{path}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            return {"error": str(exc)}

    async def post_json(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        """POST JSON to HA REST API."""
        import urllib.request
        url = f"{self.ha_url}{path}"
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            return {"error": str(exc)}


# ── Safety: confirmation ───────────────────────────────────────────

READ_ONLY_MODES = frozenset({"health"})

def requires_confirmation(cfg: ValidationConfig) -> bool:
    """Return True if the user must confirm before proceeding.

    Health mode (read-only) never requires confirmation.
    """
    if cfg.mode.value in READ_ONLY_MODES:
        return False
    return not cfg.dry_run


def confirm_live_action() -> bool:
    """Prompt user to type CONFIRMATION_PHRASE. Returns True if confirmed."""
    print(f"\n!! LIVE ACTION REQUESTED")
    print(f"   Type exactly: {CONFIRMATION_PHRASE}")
    print(f"   to confirm you understand this will perform real actions.")
    print()
    try:
        user_input = input("Confirmation: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return False
    if user_input == CONFIRMATION_PHRASE:
        print("OK Confirmed. Proceeding with live validation.\n")
        return True
    else:
        print("XX Confirmation failed. Aborting.\n")
        return False


# ── Token handling ─────────────────────────────────────────────────

def read_token_file(path: str) -> str:
    """Read HA token from a file, stripped of whitespace."""
    try:
        return Path(path).read_text().strip()
    except Exception:
        return ""


# ── Artifact helpers ───────────────────────────────────────────────

def make_artifact_dir(base: Path, utc_timestamp: str | None = None) -> Path:
    """Create and return artifact directory: <base>/<UTC>/."""
    if utc_timestamp is None:
        utc_timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    art_dir = base / utc_timestamp
    art_dir.mkdir(parents=True, exist_ok=True)
    return art_dir


def sanitize_for_artifacts(data: dict[str, Any] | list[Any] | Any) -> Any:
    """Remove or redact sensitive fields before writing to artifacts.

    Handles recursive dicts, lists, and header-style Authorization fields.
    """
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return [sanitize_for_artifacts(item) if isinstance(item, (dict, list)) else item
                for item in data]
    if not isinstance(data, dict):
        return data
    sensitive_keys = {
        "ha_token", "token", "password", "secret", "api_key",
        "access_token", "authorization", "ha_token_env_var",
    }
    cleaned: dict[str, Any] = {}
    for k, v in data.items():
        k_lower = k.lower()
        if k_lower in sensitive_keys:
            cleaned[k] = "[REDACTED]"
        elif isinstance(v, dict):
            cleaned[k] = sanitize_for_artifacts(v)
        elif isinstance(v, list):
            cleaned[k] = [
                sanitize_for_artifacts(item) if isinstance(item, (dict, list)) else item
                for item in v
            ]
        else:
            cleaned[k] = v
    return cleaned


# ── Log parsing ────────────────────────────────────────────────────

def _extract_json_from_line(line: str) -> dict[str, Any] | None:
    """Extract JSON object from a line that may have a timestamp prefix."""
    stripped = line.strip()
    if not stripped:
        return None
    # Try pure JSON first
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Try to find JSON object after a timestamp/prefix
    # Look for first '{'
    brace_idx = stripped.find("{")
    if brace_idx >= 0:
        try:
            return json.loads(stripped[brace_idx:])
        except json.JSONDecodeError:
            pass
    return None


# ── Backend native log parsing (text key=value format) ──────────────

_CANCEL_PREFIX = "[CANCEL] "
_CANCEL_EVENT_RE = re.compile(
    r"^\[CANCEL\]\s+"
    r"(?P<event>backend_cancel_detected|generation_cancel_observed|"
    r"final_decode_skipped|backend_request_cancelled|"
    r"backend_request_cleanup_done)"
    r"(?:\s+(?P<fields>.*))?$"
)


def parse_backend_native_line(line: str) -> dict[str, Any] | None:
    """Parse backend native cancellation log line (text key=value format).

    Backend native logs use::

        [CANCEL] backend_cancel_detected reason=client_disconnect \
            point=content_provider_wait request_id=s-1

    Docker --timestamps output may prefix the native line with an RFC 3339
    timestamp, so we strip that prefix before parsing.

    Returns a dict with ``event`` and parsed key=value pairs, or *None*
    if the line does not match the known backend native format.
    """
    stripped = line.strip()
    if not stripped:
        return None

    ts_prefix = _parse_rfc3339_prefix(stripped)
    if ts_prefix is not None:
        stripped = stripped[len(ts_prefix):].lstrip()

    # Only handle [CANCEL] prefixed lines; JSON goes through _extract_json_from_line
    if not stripped.startswith(_CANCEL_PREFIX):
        return None

    m = _CANCEL_EVENT_RE.match(stripped)
    if m is None:
        return None

    event_name = m.group("event")
    result: dict[str, Any] = {"event": event_name}
    fields_str = m.group("fields") or ""

    # Parse key=value pairs, handling both unquoted and quoted values
    pos = 0
    while pos < len(fields_str):
        # Skip whitespace
        while pos < len(fields_str) and fields_str[pos].isspace():
            pos += 1
        if pos >= len(fields_str):
            break
        # Find key
        eq_pos = fields_str.find("=", pos)
        if eq_pos == -1:
            break
        key = fields_str[pos:eq_pos].strip()
        if not key:
            break
        pos = eq_pos + 1
        # Find value
        if pos < len(fields_str) and fields_str[pos] == '"':
            # Quoted value
            pos += 1
            quote_end = fields_str.find('"', pos)
            if quote_end == -1:
                break
            value = fields_str[pos:quote_end]
            pos = quote_end + 1
        else:
            # Unquoted value — go until whitespace or end
            space_pos = len(fields_str)
            for i in range(pos, len(fields_str)):
                if fields_str[i].isspace():
                    space_pos = i
                    break
            value = fields_str[pos:space_pos]
            pos = space_pos
        if key:
            result[key] = value

    return result


def _parse_rfc3339_prefix(line: str) -> str | None:
    """Extract RFC 3339 timestamp prefix from a log line, if present."""
    m = _RFC3339_PREFIX_RE.match(line)
    return m.group("ts") if m else None


def _post_filter_logs_since(lines: list[str], since_utc: str) -> list[str]:
    """Filter log lines >= since_utc using local RFC 3339 prefix parsing.

    SSH aliases return full tails despite --since, so we parse prefixes
    locally and filter. Lines without a parseable timestamp are included
    (they may be continuation lines or errors).
    """
    if not since_utc:
        return list(lines)
    try:
        since_dt = datetime.fromisoformat(since_utc.replace("Z", "+00:00"))
    except ValueError:
        return list(lines)
    filtered: list[str] = []
    for line in lines:
        ts = _parse_rfc3339_prefix(line)
        if ts is None:
            filtered.append(line)
        else:
            try:
                line_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                filtered.append(line)
            else:
                if line_dt >= since_dt:
                    filtered.append(line)
    return filtered


def parse_wrapper_logs(lines: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Parse wrapper JSON log lines, grouped by connection_id."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for line in lines:
        event = _extract_json_from_line(line)
        if event is None:
            continue
        cid = event.get("connection_id")
        if cid:
            grouped.setdefault(cid, []).append(event)
    return grouped


def parse_wrapper_logs_by_synthesis(
    lines: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Parse wrapper JSON log lines, grouped by synthesis_id."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for line in lines:
        event = _extract_json_from_line(line)
        if event is None:
            continue
        sid = event.get("synthesis_id")
        if sid:
            grouped.setdefault(sid, []).append(event)
    return grouped


def _compute_text_fp(text: str) -> str:
    """Compute a stable text fingerprint matching app.observability.text_fingerprint.

    Uses SHA-256 first 12 hex chars, matching the production function.
    """
    if not text:
        return "<empty>"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def parse_wrapper_logs_by_text_fp(
    lines: list[str], text: str | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Parse wrapper JSON log lines, grouped by text_fp."""
    target_fp = _compute_text_fp(text) if text else None
    grouped: dict[str, list[dict[str, Any]]] = {}
    for line in lines:
        event = _extract_json_from_line(line)
        if event is None:
            continue
        fp = event.get("text_fp")
        if fp:
            if target_fp is None or fp == target_fp:
                grouped.setdefault(fp, []).append(event)
    return grouped


# ── Assertions ─────────────────────────────────────────────────────

def assert_scheduler_quiescent(
    scheduler_snapshot: dict[str, Any],
) -> AssertionResult:
    """Assert the scheduler is quiescent using production /status keys:
    scheduler_depth, scheduler_pending, scheduler_waiting, has_active_synthesis."""
    depth = scheduler_snapshot.get("scheduler_depth", 0)
    pending = scheduler_snapshot.get("scheduler_pending", 0)
    waiting = scheduler_snapshot.get("scheduler_waiting", 0)
    has_active = scheduler_snapshot.get("has_active_synthesis", False)
    passed = (depth == 0 and pending == 0 and waiting == 0 and not has_active)
    return AssertionResult(
        name="scheduler_quiescent",
        passed=passed,
        detail=f"depth={depth}, pending={pending}, waiting={waiting}, active={has_active}",
        evidence={"scheduler_snapshot": scheduler_snapshot},
    )


def assert_no_active_synthesis(
    scheduler_snapshot: dict[str, Any],
) -> AssertionResult:
    """Assert no synthesis is actively running (uses has_active_synthesis)."""
    has_active = scheduler_snapshot.get("has_active_synthesis", False)
    return AssertionResult(
        name="no_active_synthesis",
        passed=not has_active,
        detail=f"has_active_synthesis={has_active}",
        evidence={"has_active_synthesis": has_active},
    )


def assert_queue_pending_zero(
    scheduler_snapshot: dict[str, Any],
) -> AssertionResult:
    """Assert queue pending is zero and no active synthesis."""
    pending = scheduler_snapshot.get("scheduler_pending", 0)
    depth = scheduler_snapshot.get("scheduler_depth", 0)
    has_active = scheduler_snapshot.get("has_active_synthesis", False)
    passed = (pending == 0 and depth == 0 and not has_active)
    return AssertionResult(
        name="queue_pending_zero",
        passed=passed,
        detail=f"pending={pending}, depth={depth}, active={has_active}",
        evidence=scheduler_snapshot,
    )


def assert_no_persistent_busy(
    scheduler_snapshot: dict[str, Any],
) -> AssertionResult:
    """Assert no persistent 503/busy state — scheduler recovers."""
    has_active = scheduler_snapshot.get("has_active_synthesis", False)
    pending = scheduler_snapshot.get("scheduler_pending", 0)
    passed = not has_active and pending == 0
    return AssertionResult(
        name="no_persistent_busy",
        passed=passed,
        detail=f"active={has_active}, pending={pending}",
        evidence=scheduler_snapshot,
    )


def assert_stale_pending_cleared(
    log_events: list[dict[str, Any]],
) -> AssertionResult:
    """Assert no stale pending phrases remain after operations."""
    has_pending_clear = any(
        e.get("event") in ("pending_phrase_cleared", "queue_drained")
        for e in log_events
    )
    has_stale = any(
        e.get("event") == "phrase_timeout" for e in log_events
    )
    passed = has_pending_clear and not has_stale
    return AssertionResult(
        name="stale_pending_cleared",
        passed=passed,
        detail=f"pending_clear={has_pending_clear}, stale_timeout={has_stale}",
        evidence={"events_checked": len(log_events)},
    )


def classify_cancellation_outcome(
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Classify the lifecycle outcome from wrapper log events."""
    result: dict[str, Any] = {
        "cancellation_detected": False,
        "terminal_reason": None,
        "outcome": "UNKNOWN",
    }
    for event in events:
        if event.get("event") in ("queue_cancelled", "synthesis_cancelled"):
            result["cancellation_detected"] = True
        reason = event.get("terminal_reason") or event.get("reason")
        if reason and not result["terminal_reason"]:
            result["terminal_reason"] = reason
    if result["terminal_reason"]:
        result["outcome"] = distinguish_outcome(result["terminal_reason"])
    elif result["cancellation_detected"]:
        result["outcome"] = "CANCELLATION"
    return result


def distinguish_outcome(terminal_reason: str) -> str:
    """Map a terminal_reason to a high-level outcome classification."""
    cancelled_reasons = {
        "cancelled_while_active",
        "cancelled_while_waiting",
        "drain_cancelled",
        "cancelled",
    }
    failed_reasons = {
        "operation_failed",
        "synthesis_timeout",
        "queue_wait_timeout",
        "backend_error",
    }
    completed_reasons = {"completed", "success"}
    reason_lower = terminal_reason.lower()
    if reason_lower in cancelled_reasons:
        return "CANCELLATION"
    elif reason_lower in failed_reasons:
        return "FAILURE"
    elif reason_lower in completed_reasons:
        return "NORMAL_COMPLETION"
    else:
        return "UNKNOWN"



# ── Phase 10 Direct-Disconnect assertions ──────────────────────────

def assert_client_received_audio_before_disconnect(
    disc_result: dict[str, Any],
) -> AssertionResult:
    """Assert the client received at least one audio chunk before disconnecting."""
    got_chunk = disc_result.get("got_chunk", False)
    event_count = disc_result.get("event_count", 0)
    passed = got_chunk and event_count > 0
    return AssertionResult(
        name="client_received_audio_before_disconnect",
        passed=passed,
        detail=f"got_chunk={got_chunk}, event_count={event_count}",
        evidence={"got_chunk": got_chunk, "event_count": event_count},
    )


def assert_client_disconnected(
    disc_result: dict[str, Any],
) -> AssertionResult:
    """Assert the client disconnect outcome was recorded."""
    outcome = disc_result.get("outcome", "error")
    passed = outcome == "client_disconnected"
    return AssertionResult(
        name="client_disconnected",
        passed=passed,
        detail=f"outcome={outcome}",
        evidence={"outcome": outcome},
    )


def assert_active_state_observed_before_disconnect_or_cleanup(
    snap_during: dict[str, Any],
) -> AssertionResult:
    """Assert active synthesis state was captured during disconnect."""
    is_active = snap_during.get("is_active", False)
    has_active = snap_during.get("has_active_synthesis", False)
    passed = is_active or has_active
    return AssertionResult(
        name="active_state_observed_before_disconnect_or_cleanup",
        passed=passed,
        detail=f"is_active={is_active}, has_active_synthesis={has_active}",
        evidence={"is_active": is_active, "has_active_synthesis": has_active},
    )


def assert_original_request_terminal_state_known(
    wrapper_outcome: str,
) -> AssertionResult:
    """Assert the original request's terminal state is known (not unknown)."""
    passed = wrapper_outcome != "unknown"
    return AssertionResult(
        name="original_request_terminal_state_known",
        passed=passed,
        detail=f"wrapper_outcome={wrapper_outcome}",
        evidence={"wrapper_outcome": wrapper_outcome},
    )


def assert_wrapper_cancel_observed(
    wrapper_outcome: str,
) -> AssertionResult:
    """Assert that wrapper cancellation was observed.  Mandatory: fails if
    cancellation was expected but unobserved.
    """
    passed = wrapper_outcome == "cancelled"
    return AssertionResult(
        name="wrapper_cancel_observed",
        passed=passed,
        detail=f"wrapper_outcome={wrapper_outcome} (expected=cancelled)",
        evidence={"wrapper_outcome": wrapper_outcome},
    )


def assert_original_completed_normally(
    wrapper_outcome: str,
) -> AssertionResult:
    """Assert the original synthesis completed normally (PARTIAL finding)."""
    passed = wrapper_outcome == "completed normally"
    return AssertionResult(
        name="original_completed_normally",
        passed=passed,
        detail=f"wrapper_outcome={wrapper_outcome}",
        evidence={"wrapper_outcome": wrapper_outcome},
    )


def assert_backend_terminal_state_known(
    backend_outcome: str,
) -> AssertionResult:
    """Assert the backend's terminal state is known (not unknown)."""
    passed = backend_outcome != "unknown"
    return AssertionResult(
        name="backend_terminal_state_known",
        passed=passed,
        detail=f"backend_outcome={backend_outcome}",
        evidence={"backend_outcome": backend_outcome},
    )


def assert_scheduler_returned_to_zero(
    status: dict[str, Any],
) -> AssertionResult:
    """Assert scheduler_depth returned to zero."""
    depth = status.get("scheduler_depth", -1)
    passed = depth == 0
    return AssertionResult(
        name="scheduler_returned_to_zero",
        passed=passed,
        detail=f"scheduler_depth={depth}",
        evidence={"scheduler_depth": depth},
    )


def assert_pending_returned_to_zero(
    status: dict[str, Any],
) -> AssertionResult:
    """Assert scheduler_pending returned to zero."""
    pending = status.get("scheduler_pending", -1)
    passed = pending == 0
    return AssertionResult(
        name="pending_returned_to_zero",
        passed=passed,
        detail=f"scheduler_pending={pending}",
        evidence={"scheduler_pending": pending},
    )


def assert_waiting_returned_to_zero(
    status: dict[str, Any],
) -> AssertionResult:
    """Assert scheduler_waiting returned to zero."""
    waiting = status.get("scheduler_waiting", -1)
    passed = waiting == 0
    return AssertionResult(
        name="waiting_returned_to_zero",
        passed=passed,
        detail=f"scheduler_waiting={waiting}",
        evidence={"scheduler_waiting": waiting},
    )


def assert_active_synthesis_false(
    status: dict[str, Any],
) -> AssertionResult:
    """Assert has_active_synthesis is False."""
    has_active = status.get("has_active_synthesis", True)
    passed = not has_active
    return AssertionResult(
        name="active_synthesis_false",
        passed=passed,
        detail=f"has_active_synthesis={has_active}",
        evidence={"has_active_synthesis": has_active},
    )


def assert_wrapper_ready(
    status: dict[str, Any],
) -> AssertionResult:
    """Assert wrapper ready flag is True."""
    ready = status.get("ready", False)
    passed = ready is True
    return AssertionResult(
        name="wrapper_ready",
        passed=passed,
        detail=f"ready={ready}",
        evidence={"ready": ready},
    )


def assert_no_busy_503_latch(
    status: dict[str, Any],
) -> AssertionResult:
    """Assert no persistent 503/busy latch remains."""
    busy = status.get("busy", False)
    has_active = status.get("has_active_synthesis", False)
    pending = status.get("scheduler_pending", 0)
    passed = not busy and not has_active and pending == 0
    return AssertionResult(
        name="no_busy_503_latch",
        passed=passed,
        detail=f"busy={busy}, active={has_active}, pending={pending}",
        evidence={"busy": busy, "has_active_synthesis": has_active, "scheduler_pending": pending},
    )


def assert_follow_up_request_protocol_valid(
    follow_up_result: dict[str, Any],
) -> AssertionResult:
    """Assert follow-up request protocol_valid is True."""
    protocol_valid = follow_up_result.get("protocol_valid", False)
    passed = protocol_valid is True
    return AssertionResult(
        name="follow_up_request_protocol_valid",
        passed=passed,
        detail=f"protocol_valid={protocol_valid}",
        evidence={"protocol_valid": protocol_valid},
    )


def assert_follow_up_request_pcm_bytes_gt_zero(
    follow_up_result: dict[str, Any],
) -> AssertionResult:
    """Assert follow-up request returned PCM audio bytes > 0."""
    pcm_bytes = follow_up_result.get("pcm_bytes", 0)
    passed = pcm_bytes > 0
    return AssertionResult(
        name="follow_up_request_pcm_bytes_gt_zero",
        passed=passed,
        detail=f"pcm_bytes={pcm_bytes}",
        evidence={"pcm_bytes": pcm_bytes},
    )


def assert_follow_up_request_completed(
    followup_correlation: dict[str, Any],
) -> AssertionResult:
    """Assert follow-up request was completed (requires correlated terminal event)."""
    has_events = not followup_correlation.get("wrapper_unknown", True)
    has_synthesis = followup_correlation.get("has_synthesis_received", False)
    terminal_completed = any(
        event.get("event") == "synthesis_terminal"
        and event.get("terminal_state") == "completed"
        for event in followup_correlation.get("wrapper_events", [])
    )
    passed = has_events and has_synthesis and terminal_completed
    return AssertionResult(
        name="follow_up_request_completed",
        passed=passed,
        detail=(f"correlated={has_events}, synthesis_received={has_synthesis}, "
                f"terminal_completed={terminal_completed}"),
        evidence={"wrapper_unknown": followup_correlation.get("wrapper_unknown", True),
                  "has_synthesis_received": has_synthesis,
                  "terminal_completed": terminal_completed},
    )


def assert_follow_up_scheduler_recovered(
    status: dict[str, Any],
) -> AssertionResult:
    """Assert scheduler recovered after follow-up request."""
    depth = status.get("scheduler_depth", -1)
    pending = status.get("scheduler_pending", -1)
    has_active = status.get("has_active_synthesis", True)
    passed = depth == 0 and pending == 0 and not has_active
    return AssertionResult(
        name="follow_up_scheduler_recovered",
        passed=passed,
        detail=f"depth={depth}, pending={pending}, active={has_active}",
        evidence={"scheduler_depth": depth, "scheduler_pending": pending,
                  "has_active_synthesis": has_active},
    )


def assert_idle_timeout_not_reached(
    idle_status: dict[str, Any] | None,
) -> AssertionResult:
    """Assert the idle wait did NOT timeout — terminal idle was reached."""
    passed = idle_status is not None and "error" not in idle_status
    return AssertionResult(
        name="idle_timeout_not_reached",
        passed=passed,
        detail=f"idle_status={'reached' if passed else 'timeout'}",
        evidence={"idle_reached": passed},
    )


# ── Phase 10 Direct-Disconnect classification ──────────────────────

def classify_wrapper_outcome(events: list[dict[str, Any]]) -> str:
    """Classify wrapper synthesis outcome: cancelled / completed normally
    / failed / timed out / client disconnected / unknown.

    Uses production event contracts from app/wyoming_server.py:
      - synthesis_cancelled          -> cancelled
      - synthesis_cancel_requested   -> cancelled
      - cancellation_requested       -> cancelled (structured lifecycle)
      - cancellation_propagated      -> cancelled (structured lifecycle)
      - client_disconnected          -> client disconnected (peer disconnect)
      - syn_stopped                  -> completed normally
      - audio_out with status="ok"   -> completed normally
      - synthesis_terminal           -> use terminal_state field
      - synthesis_error              -> failed
      - synthesis_timeout            -> timed out

    Precedence:
      1. synthesis_terminal explicit state (highest authority)
      2. Cancellation -> cancelled
      3. Client disconnect -> client disconnected
      4. Timeout -> timed out
      5. Error -> failed
      6. Normal completion -> completed normally
      7. Otherwise -> unknown
    """
    if not events:
        return "unknown"

    def _has(*names: str) -> bool:
        return any(e.get("event") in names for e in events)

    # 1. Check synthesis_terminal explicit state first (highest authority)
    for e in events:
        if e.get("event") == "synthesis_terminal":
            state = e.get("terminal_state", "unknown")
            if state == "cancelled":
                return "cancelled"
            elif state == "completed":
                return "completed normally"
            elif state == "failed":
                return "failed"
            elif state == "timed_out":
                return "timed out"
            else:
                return "unknown"

    has_cancellation = _has(
        "synthesis_cancelled", "synthesis_cancel_requested",
        "cancellation_requested", "cancellation_propagated",
    )
    has_client_disconnect = _has("client_disconnected")
    has_timeout = _has("synthesis_timeout")
    has_error = _has("synthesis_error")
    has_completed = (
        _has("syn_stopped", "follow_up_completed")
        or any(e.get("event") == "audio_out" and e.get("status") == "ok" for e in events)
    )

    if has_cancellation:
        return "cancelled"
    if has_client_disconnect:
        return "client disconnected"
    if has_timeout:
        return "timed out"
    if has_error:
        return "failed"
    if has_completed:
        return "completed normally"
    return "unknown"


def classify_backend_outcome(events: list[dict[str, Any]]) -> str:
    """Classify backend request outcome: aborted early / completed normally
    / failed / unknown.

    Uses production event contracts:
      - backend_stream_done status=client_disconnected -> aborted early
      - backend_done status=client_disconnected         -> aborted early
      - backend_stream_done status=ok                   -> completed normally
      - backend_done status=ok                          -> completed normally
      - backend_stream_done status=error                -> failed
      - backend_done status=error                       -> failed
      - backend_request_aborted (legacy)                -> aborted early
      - backend_abort_observed                          -> aborted early (structured)

    Precedence: abort > error > completed > unknown.
    """
    if not events:
        return "unknown"

    has_abort = any(
        (e.get("event") in ("backend_stream_done", "backend_done")
         and e.get("status") == "client_disconnected")
        or e.get("event") in (
            "backend_request_aborted", "backend_request_cancelled",
            "backend_cancel_detected", "generation_cancel_observed",
            "final_decode_skipped", "backend_abort_observed",
        )
        for e in events
    )
    has_error = any(
        (e.get("event") in ("backend_stream_done", "backend_done")
         and e.get("status") == "error")
        or e.get("event") == "backend_error"
        for e in events
    )
    has_completed = any(
        (e.get("event") in ("backend_stream_done", "backend_done")
         and e.get("status") == "ok")
        or e.get("event") == "backend_completed"
        for e in events
    )

    if has_abort:
        return "aborted early"
    if has_error:
        return "failed"
    if has_completed:
        return "completed normally"
    return "unknown"


def correlate_disconnect_logs(
    wrapper_logs: list[str],
    backend_logs: list[str],
    original_text_fp: str,
) -> dict[str, Any]:
    """Correlate wrapper and backend logs by text_fp, connection_id, synthesis_id.

    Returns a dict with:
      - connection_id: str | None
      - synthesis_id: str | None
      - wrapper_events: list of matched wrapper events
      - backend_events: list of matched backend events
      - backend_events_count: int
      - has_conn_closed: bool
      - has_synthesis_received: bool
      - wrapper_unknown: bool (True if no matching events found)
    """
    result: dict[str, Any] = {
        "connection_id": None,
        "synthesis_id": None,
        "wrapper_events": [],
        "backend_events": [],
        "backend_events_count": 0,
        "has_conn_closed": False,
        "has_synthesis_received": False,
        "wrapper_unknown": True,
        "correlation_error": None,
    }

    if not wrapper_logs:
        return result

    # Parse all wrapper events matching text_fp
    by_fp = parse_wrapper_logs_by_text_fp(wrapper_logs)
    wrapper_events = by_fp.get(original_text_fp, [])
    if not wrapper_events:
        return result

    correlated_events = [
        ev for ev in wrapper_events
        if ev.get("connection_id") and ev.get("synthesis_id") and ev.get("text_fp")
    ]
    connection_ids = {ev["connection_id"] for ev in correlated_events}
    synthesis_ids = {ev["synthesis_id"] for ev in correlated_events}
    if not correlated_events or len(connection_ids) != 1 or len(synthesis_ids) != 1:
        result["correlation_error"] = "missing_or_mismatched_wrapper_identifiers"
        return result

    result["wrapper_unknown"] = False
    result["wrapper_events"] = wrapper_events
    result["connection_id"] = next(iter(connection_ids))
    result["synthesis_id"] = next(iter(synthesis_ids))

    for ev in wrapper_events:
        if ev.get("event") == "conn_closed":
            result["has_conn_closed"] = True
        if ev.get("event") == "synthesize_received" or (
            ev.get("event") == "event_in" and ev.get("event_type") == "synthesize"
        ) or ev.get("event") == "syn_trigger":
            result["has_synthesis_received"] = True

    # Also scan ALL wrapper events (not just text_fp-matched) for
    # connection-scoped events like conn_closed that may lack text_fp.
    conn_id = result["connection_id"]
    if conn_id:
        all_events = []
        for line in wrapper_logs:
            ev = _extract_json_from_line(line)
            if ev is not None:
                all_events.append(ev)
        for ev in all_events:
            if ev.get("connection_id") == conn_id:
                if ev.get("event") == "conn_closed":
                    result["has_conn_closed"] = True
                if ev.get("event") == "synthesize_received" or (
                    ev.get("event") == "event_in" and ev.get("event_type") == "synthesize"
                ) or ev.get("event") == "syn_trigger":
                    result["has_synthesis_received"] = True

    # Correlate backend events by synthesis_id
    synthesis_id = result["synthesis_id"]
    if synthesis_id and backend_logs:
        backend_parsed = []
        for line in backend_logs:
            ev = _extract_json_from_line(line) or parse_backend_native_line(line)
            event_synthesis_id = ev.get("synthesis_id") if ev else None
            if ev and event_synthesis_id is None:
                event_synthesis_id = ev.get("request_id")
            if ev and event_synthesis_id == synthesis_id:
                ev = dict(ev)
                ev.setdefault("synthesis_id", synthesis_id)
                backend_parsed.append(ev)
        result["backend_events"] = backend_parsed
        result["backend_events_count"] = len(backend_parsed)

    return result


def verify_follow_up_synthesis(result: dict[str, Any]) -> AssertionResult:
    """Verify a follow-up synthesis result is valid."""
    protocol_valid = result.get("protocol_valid", False)
    pcm_bytes = result.get("pcm_bytes", 0)
    duration_s = result.get("duration_s", 0.0)
    errors = result.get("errors", [])

    passed = protocol_valid and pcm_bytes > 0 and len(errors) == 0
    return AssertionResult(
        name="follow_up_synthesis_succeeded",
        passed=passed,
        detail=f"valid={protocol_valid}, pcm={pcm_bytes}B, dur={duration_s}s, errs={len(errors)}",
        evidence={"protocol_valid": protocol_valid, "pcm_bytes": pcm_bytes,
                  "duration_s": duration_s, "errors": errors},
    )


def get_disconnect_texts(cfg: ValidationConfig, *, marker: str | None = None) -> dict[str, Any]:
    """Return original and follow-up text markers with fingerprints.

    Each call generates a unique per-call nonce (secrets.token_hex(8)) embedded
    in both texts.  An injectable marker parameter allows deterministic test
    markers.  The original and follow-up texts are always distinct and unique.

    Plaintext markers are NOT intentionally included in the report — only
    fingerprints and correlation IDs are recorded.
    """
    if marker is None:
        import secrets
        marker = secrets.token_hex(8)

    original = f"{cfg.tts_text} [disconnect-validation-{marker}]"
    follow_up = f"{cfg.tts_text} [recovery-validation-{marker}]"
    if not original.endswith("."):
        original = original + "."
    if not follow_up.endswith("."):
        follow_up = follow_up + "."

    return {
        "original": original,
        "original_fp": _compute_text_fp(original),
        "follow_up": follow_up,
        "follow_up_fp": _compute_text_fp(follow_up),
        "marker": marker,
    }


def build_direct_disconnect_status_snapshot(
    phase: str,
    is_active: bool,
    admin_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a flat status snapshot compatible with admin status file output.

    Snapshots must only be marked as 'during' (is_active=True) after
    audio chunk/activity has been confirmed.

    Returns a flat dict (not nested phase-only) — the admin_status data
    is merged at the top level so writers that expect flat keys
    (scheduler_depth, has_active_synthesis, etc.) remain compatible.
    """
    snap: dict[str, Any] = {
        "phase": phase,
        "is_active": is_active,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if admin_status is not None:
        # Merge admin_status fields flat so status files are compatible
        snap.update(admin_status)
    return snap

# ── Timeline / report generation ───────────────────────────────────

def generate_timeline(
    events: list[tuple[float, str, str]],
) -> list[dict[str, Any]]:
    """Generate a sorted timeline from (timestamp, event_name, entity_id)."""
    sorted_events = sorted(events, key=lambda e: e[0])
    first_ts = sorted_events[0][0] if sorted_events else 0.0
    return [
        {
            "timestamp": ts,
            "event": name,
            "entity_id": eid,
            "offset_ms": round((ts - first_ts) * 1000, 2),
        }
        for ts, name, eid in sorted_events
    ]


def generate_validation_report(
    mode: str,
    assertions_passed: int,
    assertions_failed: int,
    outcomes: dict[str, str],
    timeline: list[dict[str, Any]],
    errors: list[str] | None = None,
    utc_timestamp: str | None = None,
) -> dict[str, Any]:
    """Generate a summary validation report dict."""
    if utc_timestamp is None:
        utc_timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return {
        "mode": mode,
        "generated_utc": utc_timestamp,
        "assertions_passed": assertions_passed,
        "assertions_failed": assertions_failed,
        "assertions_total": assertions_passed + assertions_failed,
        "passed": assertions_failed == 0,
        "outcomes": outcomes,
        "timeline": timeline,
        "errors": errors or [],
        "schema_version": "1.0",
    }


def _generate_report_md(report: ValidationReport) -> str:
    """Generate a markdown report string."""
    lines = [
        "# Phase 10 Validation Report",
        "",
        f"**Mode:** {report.mode}",
        f"**UTC:** {report.utc_timestamp}",
        f"**Dry-Run:** {report.dry_run}",
        f"**Duration:** {report.duration_sec}s",
        "",
        "## Assertions",
        f"- Passed: {report.assertions_passed}",
        f"- Failed: {report.assertions_failed}",
        "",
    ]
    for a in report.assertions:
        status = "\u2705" if a.passed else "\u274c"
        lines.append(f"- {status} **{a.name}**: {a.detail}")
    lines.append("")
    if report.outcomes:
        lines.append("## Outcomes")
        for k, v in report.outcomes.items():
            lines.append(f"- **{k}**: {v}")
        lines.append("")
    if report.errors:
        lines.append("## Errors")
        for e in report.errors:
            lines.append(f"- {e}")
        lines.append("")
    if report.timeline:
        lines.append("## Timeline")
        for t in report.timeline[:20]:
            lines.append(f"- {t.get('timestamp', '')}: {t.get('event', '')}")
        lines.append("")
    return "\n".join(lines)


# ── Infra operations ───────────────────────────────────────────────

async def capture_admin_status(
    admin_url: str = "http://127.0.0.1:10201",
    http_client_factory: Any = None,
    subprocess_runner: Any = None,
    container_name: str = "wyoming-s2cpp-tts",
) -> dict[str, Any]:
    """Capture /status from the admin HTTP server.

    Strategy:
      1. If http_client_factory is provided (injected for testing/live),
         use it directly.
      2. If dry/live with no factory: try direct admin URL via urllib.
      3. If direct URL fails, fall back to safe read-only docker exec:
         ``docker exec wyoming-s2cpp-tts python -c "..."``
         This command is read-only, exact container, no shell, no token.
      4. If all fail, return error dict.
    """
    status = await _try_capture_admin_direct(admin_url, http_client_factory)
    if "error" not in status:
        return status

    # Fallback: docker exec (C)
    if subprocess_runner is not None:
        try:
            import_cmd = (
                "import urllib.request, json; "
                "resp = urllib.request.urlopen('http://127.0.0.1:10201/status'); "
                "print(json.dumps(json.loads(resp.read().decode())))"
            )
            result = await subprocess_runner.run(
                ["docker", "exec", container_name, "python", "-c", import_cmd],
                capture_output=True,
                timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                return json.loads(result.stdout.strip())
        except Exception as exc:
            return {"error": f"docker exec fallback failed: {exc}"}

    return {"error": "admin status unreachable via all methods"}


async def _try_capture_admin_direct(
    admin_url: str,
    http_client_factory: Any = None,
) -> dict[str, Any]:
    """Try to capture admin status via direct HTTP."""
    if http_client_factory is not None:
        client = http_client_factory(admin_url, "")
        try:
            return await client.get_json("/status")
        except Exception:
            return {"error": "failed to fetch /status via factory"}

    import urllib.request
    try:
        url = f"{admin_url}/status"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        return {"error": str(exc)}


async def collect_docker_state(
    container_names: tuple[str, ...],
    subprocess_runner: Any = None,
) -> dict[str, Any]:
    """Collect exact identity, image provenance, and running state."""
    state: dict[str, Any] = {}
    if subprocess_runner is None:
        return {name: {"found": False, "state": "dry-run"} for name in container_names}

    for name in container_names:
        container_state: dict[str, Any] = {"found": False, "state": "unknown"}
        try:
            result = await subprocess_runner.run(
                ["docker", "inspect", "--type", "container", "--format",
                 DOCKER_INSPECT_FORMAT, name],
                capture_output=True, timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                info = json.loads(result.stdout.decode().strip())
                info["name"] = str(info.get("name", "")).removeprefix("/")
                if info["name"] != name:
                    raise ValueError("container identity mismatch")
                container_state = {"found": True, **info}
            elif result.stderr:
                container_state["error"] = result.stderr.decode(errors="replace").strip()
        except Exception as exc:
            container_state["error"] = str(exc)
        state[name] = container_state
    return state


def assert_container_ids_unchanged(
    before: dict[str, Any], after: dict[str, Any],
) -> AssertionResult:
    """Fail closed when a scenario crosses a container recreation boundary."""
    changed: dict[str, dict[str, Any]] = {}
    for name in sorted(set(before) | set(after)):
        before_id = before.get(name, {}).get("id")
        after_id = after.get(name, {}).get("id")
        if not before_id or not after_id or before_id != after_id:
            changed[name] = {"before": before_id, "after": after_id}
    return AssertionResult(
        name="container_ids_unchanged_during_scenario",
        passed=not changed,
        detail="container identities stable" if not changed else "container recreation detected",
        evidence={"changed": changed},
    )


async def collect_docker_logs(
    container_names: tuple[str, ...],
    subprocess_runner: Any = None,
    tail: int = 200,
    since: str | None = None,
) -> dict[str, list[str]]:
    """Collect bounded timestamped logs for the exact containers."""
    logs: dict[str, list[str]] = {}
    if subprocess_runner is None:
        return {name: [] for name in container_names}

    for name in container_names:
        try:
            cmd = ["docker", "logs", "--timestamps", "--tail", str(tail)]
            if since is not None:
                cmd.extend(["--since", since])
            cmd.append(name)
            result = await subprocess_runner.run(cmd, capture_output=True, timeout=15)
            if result.returncode == 0:
                stdout = result.stdout.decode(errors="replace").strip()
                stderr = result.stderr.decode(errors="replace").strip()
                decoded = "\n".join(part for part in (stdout, stderr) if part)
                lines = decoded.split("\n") if decoded else []
                if not lines:
                    lines = ["ERROR: docker logs succeeded but returned no stdout or stderr"]
                # Post-filter locally because SSH aliases may ignore --since
                if since is not None:
                    lines = _post_filter_logs_since(lines, since)
                logs[name] = lines
            else:
                err = result.stderr.decode(errors="replace").strip()
                logs[name] = [f"ERROR: docker logs failed: {err}"]
        except Exception as exc:
            logs[name] = [f"ERROR: {exc}"]
    return logs


def preserve_latest_docker_logs(
    report: ValidationReport,
    latest_logs: dict[str, list[str]],
) -> None:
    """Update report log payloads with the latest collected Docker logs."""
    wrapper_logs = latest_logs.get("wyoming-s2cpp-tts")
    if wrapper_logs is not None:
        report.wrapper_logs = list(wrapper_logs)

    backend_logs = latest_logs.get("s2cpp-backend")
    if backend_logs is not None:
        report.backend_logs = list(backend_logs)


async def check_wyoming_port(
    host: str = "127.0.0.1",
    port: int = 10200,
    timeout: float = 5.0,
    connector_factory: Any = None,
) -> dict[str, Any]:
    """Check if the Wyoming port is reachable."""
    if connector_factory is not None:
        try:
            async with connector_factory(host, port):
                pass
            return {"reachable": True, "host": host, "port": port, "error": None}
        except Exception as exc:
            return {"reachable": False, "host": host, "port": port, "error": str(exc)}

    # (A) Absolute requirement: NO real socket I/O when factory is None.
    # This is the dry-run path — always return skipped.
    return {"reachable": None, "host": host, "port": port,
            "skipped": True, "error": "dry-run (no connector)"}


async def fetch_admin_status(
    admin_url: str = "http://127.0.0.1:9876",
    http_client_factory: Any = None,
) -> dict[str, Any]:
    """Fetch /status from the admin HTTP server. Legacy name, prefers capture_admin_status."""
    return await capture_admin_status(admin_url=admin_url, http_client_factory=http_client_factory)


async def _try_capture_admin_metrics_direct(
    admin_url: str,
    http_client_factory: Any = None,
) -> dict[str, Any]:
    """Try to capture admin /metrics via direct HTTP."""
    if http_client_factory is not None:
        client = http_client_factory(admin_url, "")
        try:
            return await client.get_json("/metrics")
        except Exception:
            return {"error": "failed to fetch /metrics via factory"}
    import urllib.request
    try:
        with urllib.request.urlopen(f"{admin_url}/metrics", timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        return {"error": str(exc)}


async def fetch_admin_metrics(
    admin_url: str = "http://127.0.0.1:9876",
    http_client_factory: Any = None,
    subprocess_runner: Any = None,
    container_name: str = "wyoming-s2cpp-tts",
) -> dict[str, Any]:
    """Fetch /metrics from the admin HTTP server, with docker exec fallback."""
    metrics = await _try_capture_admin_metrics_direct(admin_url, http_client_factory)
    if "error" not in metrics:
        return metrics

    if subprocess_runner is not None:
        try:
            import_cmd = (
                "import urllib.request, json; "
                "resp = urllib.request.urlopen('http://127.0.0.1:10201/metrics'); "
                "print(json.dumps(json.loads(resp.read().decode())))"
            )
            result = await subprocess_runner.run(
                ["docker", "exec", container_name, "python", "-c", import_cmd],
                capture_output=True,
                timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                return json.loads(result.stdout.strip())
        except Exception as exc:
            return {"error": f"docker exec fallback failed: {exc}"}

    return {"error": "admin metrics unreachable via all methods"}


# ── HA operations ──────────────────────────────────────────────────

async def ha_media_stop(
    entity_id: str,
    http_client: Any = None,
    ha_url: str = "",
    ha_token: str = "",
) -> bool:
    """Call POST /api/services/media_player/media_stop only after observing 'playing' state.

    Returns True if the stop was called, False if state is not playing or on error.
    """
    if http_client is None:
        if not ha_url or not ha_token:
            return False
        http_client = HaRestClient(ha_url, ha_token)

    # Check entity state first
    state_path = f"/api/states/{entity_id}"
    state = await http_client.get_json(state_path)
    if state.get("state") != "playing":
        return False

    # Call media_stop
    result = await http_client.post_json(
        "/api/services/media_player/media_stop",
        {"entity_id": entity_id},
    )
    return not result.get("error") and result.get("success", False) is not False


async def trigger_assist_pipeline(
    http_client: Any = None,
    ha_url: str = "",
    ha_token: str = "",
    text: str = "What time is it?",
    entity_id: str = "",
) -> dict[str, Any]:
    """Trigger an Assist pipeline run via HA conversation/process API.

    Returns the response from HA or error dict.
    """
    if http_client is None:
        if not ha_url or not ha_token:
            return {"error": "no http client or credentials"}
        http_client = HaRestClient(ha_url, ha_token)

    result = await http_client.post_json(
        "/api/conversation/process",
        {
            "text": text,
            "agent_id": entity_id if entity_id else None,
        },
    )
    return result


# ── Wyoming operations ─────────────────────────────────────────────

async def wyoming_synthesize(
    text: str,
    host: str = "127.0.0.1",
    port: int = 10200,
    voice: str = "cmu_bdl_male_us",
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Perform a single Wyoming TTS synthesis and return result dict.

    In dry-run mode (host/port=None), returns a simulated result.
    """
    if host is None or port is None:
        return {
            "text": text,
            "dry_run": True,
            "pcm_bytes": 0,
            "duration_s": 0.0,
            "protocol_valid": True,
            "errors": [],
        }
    try:
        from wyoming.audio import AudioChunk, AudioStart, AudioStop
        from wyoming.client import AsyncTcpClient
        from wyoming.tts import Synthesize, SynthesizeVoice

        pcm = bytearray()
        rate = width = channels = 0
        events = []
        start_time = time.monotonic()
        errors = []

        async with AsyncTcpClient(host, port) as tcp:
            await tcp.write_event(Synthesize(
                text=text, voice=SynthesizeVoice(name=voice)).event())
            while True:
                try:
                    ev = await asyncio.wait_for(tcp.read_event(), timeout=timeout)
                except asyncio.TimeoutError:
                    errors.append("timeout")
                    break
                if ev is None:
                    break
                events.append(ev.type)
                if AudioStart.is_type(ev.type):
                    s = AudioStart.from_event(ev)
                    rate, width, channels = s.rate, s.width, s.channels
                elif AudioChunk.is_type(ev.type):
                    c = AudioChunk.from_event(ev)
                    pcm.extend(c.audio)
                elif AudioStop.is_type(ev.type):
                    break

        duration = round(time.monotonic() - start_time, 3)
        return {
            "text": text,
            "pcm_bytes": len(pcm),
            "duration_s": duration,
            "rate": rate,
            "width": width,
            "channels": channels,
            "protocol_valid": len(errors) == 0 and rate > 0 and len(pcm) > 0,
            "errors": errors,
        }
    except ImportError:
        return {
            "text": text,
            "dry_run": False,
            "wyoming_unavailable": True,
            "pcm_bytes": 0,
            "duration_s": 0.0,
            "protocol_valid": False,
            "errors": ["wyoming library not available"],
        }
    except Exception as exc:
        return {
            "text": text,
            "pcm_bytes": 0,
            "duration_s": 0.0,
            "protocol_valid": False,
            "errors": [str(exc)],
        }


async def wyoming_disconnect_during_stream(
    text: str,
    host: str = "127.0.0.1",
    port: int = 10200,
    voice: str = "cmu_bdl_male_us",
    timeout: float = 30.0,
    on_first_audio: Any = None,
) -> dict[str, Any]:
    """Connect to Wyoming, start synthesis, then disconnect during active stream.

    If *on_first_audio* is provided, it is an async callable invoked
    immediately after the first non-empty audio chunk is received but
    BEFORE the writer is closed — allowing capture_admin_status while
    the disconnect request is still in flight.

    Returns dict with outcome classification.
    """
    if host is None or port is None:
        return {"dry_run": True, "outcome": "dry-run", "events": []}
    try:
        from wyoming.audio import AudioChunk, AudioStart
        from wyoming.client import AsyncTcpClient
        from wyoming.tts import Synthesize, SynthesizeVoice

        events = []
        got_chunk = False
        async with AsyncTcpClient(host, port) as tcp:
            await tcp.write_event(Synthesize(
                text=text, voice=SynthesizeVoice(name=voice)).event())
            while not got_chunk:
                try:
                    ev = await asyncio.wait_for(tcp.read_event(), timeout=timeout)
                except asyncio.TimeoutError:
                    break
                if ev is None:
                    break
                events.append(ev.type)
                if AudioStart.is_type(ev.type):
                    pass
                elif AudioChunk.is_type(ev.type):
                    chunk = AudioChunk.from_event(ev)
                    if chunk.audio:
                        got_chunk = True
                        # Invoke callback while disconnect is still in flight
                        if on_first_audio is not None:
                            await on_first_audio()
            # Client is now disconnected by context manager exit

        return {
            "outcome": "client_disconnected",
            "got_chunk": got_chunk,
            "events": events,
            "event_count": len(events),
        }
    except ImportError:
        return {"outcome": "wyoming_unavailable", "events": []}
    except Exception as exc:
        return {"outcome": "error", "error": str(exc), "events": []}


# ── Artifact writing ───────────────────────────────────────────────

def _write_all_artifacts(report: ValidationReport, artifact_dir: Path) -> None:
    """Write all required artifacts as flat files in artifact_dir."""
    try:
        _write_all_artifacts_inner(report, artifact_dir)
    except Exception as exc:
        # (M) Always write something, even on failure
        err_path = artifact_dir / "errors.txt"
        existing = err_path.read_text() if err_path.exists() else ""
        err_path.write_text(existing + f"\nArtifact write error: {exc}")


def _write_all_artifacts_inner(report: ValidationReport, artifact_dir: Path) -> None:
    """Write all required artifacts as flat files in artifact_dir."""
    # report.md
    md = _generate_report_md(report)
    (artifact_dir / "report.md").write_text(md)

    # report.json
    report_data = generate_validation_report(
        mode=report.mode,
        assertions_passed=report.assertions_passed,
        assertions_failed=report.assertions_failed,
        outcomes=report.outcomes,
        timeline=report.timeline,
        errors=report.errors,
        utc_timestamp=report.utc_timestamp,
    )
    (artifact_dir / "report.json").write_text(json.dumps(
        sanitize_for_artifacts(report_data), indent=2, sort_keys=True))

    # timeline.json
    (artifact_dir / "timeline.json").write_text(json.dumps(
        report.timeline, indent=2))

    # assertions.json
    (artifact_dir / "assertions.json").write_text(json.dumps(
        [{"name": a.name, "passed": a.passed, "detail": a.detail,
          "evidence": sanitize_for_artifacts(a.evidence)}
         for a in report.assertions],
        indent=2, sort_keys=True))

    # wrapper.log
    (artifact_dir / "wrapper.log").write_text(
        "\n".join(report.wrapper_logs) if report.wrapper_logs else "")

    # backend.log
    (artifact_dir / "backend.log").write_text(
        "\n".join(report.backend_logs) if report.backend_logs else "")

    # ha_states.json
    (artifact_dir / "ha_states.json").write_text(json.dumps(
        sanitize_for_artifacts(report.ha_states), indent=2))

    # wrapper_status snapshots
    for idx, snap in enumerate(report.status_snapshots):
        label = ["before", "during", "after"][idx] if idx < 3 else f"snapshot_{idx}"
        fname = f"wrapper_status_{label}.json"
        (artifact_dir / fname).write_text(json.dumps(
            sanitize_for_artifacts(snap) if isinstance(snap, dict) else {},
            indent=2, sort_keys=True))

    # Ensure all required names exist (even if empty)
    for name in REQUIRED_ARTIFACTS:
        fpath = artifact_dir / name
        if not fpath.exists():
            if name.endswith(".json"):
                fpath.write_text("{}")
            elif name.endswith(".md"):
                fpath.write_text("# Phase 10 Validation Report\n\nNo data.\n")
            elif name.endswith(".log"):
                fpath.write_text("")


# ── Mode-specific orchestration ────────────────────────────────────

async def _run_health_mode(
    cfg: ValidationConfig,
    report: ValidationReport,
    artifact_dir: Path,
    *,
    subprocess_runner: Any = None,
    http_client_factory: Any = None,
    connector_factory: Any = None,
    ha_event_watcher: Any = None,
) -> ValidationReport:
    """Health mode: read-only container, port, and admin endpoint checks."""
    if cfg.dry_run:
        skipped = {"skipped": True, "reason": "zero-I/O dry-run"}
        for name in (
            "wyoming_port_reachable",
            "admin_status_accessible",
            "admin_metrics_accessible",
        ):
            report.assertions.append(AssertionResult(
                name=name, passed=True, detail="SKIPPED: zero-I/O dry-run", evidence=skipped,
            ))
        report.status_snapshots.extend([
            {"phase": "before", **skipped},
            {"phase": "during", **skipped},
            {"phase": "after", **skipped},
        ])
        return report

    # Wyoming port check
    port_result = await check_wyoming_port(
        cfg.wyoming_host, cfg.wyoming_port,
        connector_factory=connector_factory,
    )
    report.assertions.append(AssertionResult(
        name="wyoming_port_reachable",
        passed=port_result["reachable"],
        detail=f"{cfg.wyoming_host}:{cfg.wyoming_port}",
        evidence=port_result,
    ))

    # Docker state (only if subprocess_runner provided)
    if subprocess_runner is not None:
        docker_state = await collect_docker_state(cfg.container_names, subprocess_runner)
        for cname, cstate in docker_state.items():
            report.assertions.append(AssertionResult(
                name=f"container_found_{cname}",
                passed=cstate.get("found", False),
                detail=cstate.get("state", "unknown"),
                evidence={"container": cname, "state": cstate},
            ))

    # Admin status before
    admin_url = cfg.admin_url
    used_factory = http_client_factory if subprocess_runner is not None else None
    status = await capture_admin_status(
        admin_url=admin_url,
        http_client_factory=used_factory,
        subprocess_runner=subprocess_runner,
    )
    report.status_snapshots.append(status)
    if "error" not in status:
        report.assertions.append(AssertionResult(
            name="admin_status_accessible",
            passed=True,
            detail="status fetched",
            evidence=status,
        ))
        report.assertions.append(assert_scheduler_quiescent(status))
    else:
        report.assertions.append(AssertionResult(
            name="admin_status_accessible",
            passed=False,
            detail=status.get("error", "unknown"),
        ))

    # Admin metrics
    metrics = await fetch_admin_metrics(
        admin_url=admin_url,
        http_client_factory=used_factory,
        subprocess_runner=subprocess_runner,
    )
    if "error" not in metrics:
        report.assertions.append(AssertionResult(
            name="admin_metrics_accessible",
            passed=True,
            detail="metrics fetched",
            evidence=metrics,
        ))
    else:
        report.assertions.append(AssertionResult(
            name="admin_metrics_accessible",
            passed=False,
            detail=metrics.get("error", "unknown"),
        ))

    return report


async def _run_normal_mode(
    cfg: ValidationConfig,
    report: ValidationReport,
    artifact_dir: Path,
    *,
    subprocess_runner: Any = None,
    http_client_factory: Any = None,
    connector_factory: Any = None,
    ha_event_watcher: Any = None,
) -> ValidationReport:
    """Normal mode: single synthesis, verify normal flow."""
    # Health baseline first
    report = await _run_health_mode(cfg, report, artifact_dir,
                                     subprocess_runner=subprocess_runner,
                                     http_client_factory=http_client_factory,
                                     connector_factory=connector_factory,
                                     ha_event_watcher=ha_event_watcher)

    # Admin status before
    status_before = await capture_admin_status(
        admin_url=cfg.admin_url,
        http_client_factory=http_client_factory if not cfg.dry_run else None,
    )
    report.status_snapshots.append(status_before)

    # Perform synthesis
    synth_result = await wyoming_synthesize(
        text=cfg.tts_text,
        host=cfg.wyoming_host if not cfg.dry_run else None,
        port=cfg.wyoming_port if not cfg.dry_run else None,
        voice=cfg.tts_voice,
    )
    report.timeline.append({
        "timestamp": time.monotonic(),
        "event": "synthesis_executed",
        "result": {k: v for k, v in synth_result.items() if k in
                   ("pcm_bytes", "duration_s", "protocol_valid", "errors")},
    })
    if synth_result.get("protocol_valid", False):
        report.assertions.append(AssertionResult(
            name="normal_synthesis_valid",
            passed=True,
            detail=f"pcm={synth_result.get('pcm_bytes', 0)}B, "
                   f"dur={synth_result.get('duration_s', 0)}s",
        ))

    # Admin status after
    status_after = await capture_admin_status(
        admin_url=cfg.admin_url,
        http_client_factory=http_client_factory if not cfg.dry_run else None,
    )
    report.status_snapshots.append(status_after)

    return report


async def _run_media_stop_mode(
    cfg: ValidationConfig,
    report: ValidationReport,
    artifact_dir: Path,
    *,
    subprocess_runner: Any = None,
    http_client_factory: Any = None,
    connector_factory: Any = None,
    ha_event_watcher: Any = None,
) -> ValidationReport:
    """Media-stop mode: call media_player.media_stop during playback."""
    report = await _run_health_mode(cfg, report, artifact_dir,
                                     subprocess_runner=subprocess_runner,
                                     http_client_factory=http_client_factory,
                                     connector_factory=connector_factory,
                                     ha_event_watcher=ha_event_watcher)

    status_before = await capture_admin_status(admin_url=cfg.admin_url,
                                                http_client_factory=http_client_factory if not cfg.dry_run else None)
    report.status_snapshots.append(status_before)

    # In dry-run, emit placeholder outcome
    if cfg.dry_run:
        report.outcomes["playback_stopped"] = "dry-run"
        report.timeline.append({"timestamp": time.monotonic(), "event": "media_stop_dry_run"})
    else:
        result = await ha_media_stop(
            entity_id=cfg.vpe_media_player,
            ha_url=cfg.ha_url,
            ha_token=cfg.ha_token,
        )
        report.outcomes["playback_stopped"] = str(result)
        report.timeline.append({
            "timestamp": time.monotonic(),
            "event": "media_stop_executed",
            "result": result,
        })

    status_after = await capture_admin_status(admin_url=cfg.admin_url,
                                               http_client_factory=http_client_factory if not cfg.dry_run else None)
    report.status_snapshots.append(status_after)

    return report


async def _run_direct_disconnect_mode(
    cfg: ValidationConfig,
    report: ValidationReport,
    artifact_dir: Path,
    *,
    subprocess_runner: Any = None,
    http_client_factory: Any = None,
    connector_factory: Any = None,
    ha_event_watcher: Any = None,
) -> ValidationReport:
    """Direct-disconnect mode: close Wyoming client during active stream.

    Produces 17+ explicit assertions with unique names (no broad old assertions
    duplicated).  Classification PARTIAL when original normal completion +
    cleanup/recovery.  report.passed is False when cancellation expected but
    unobserved.  Correlation IDs/fps recorded without plaintext.
    """
    # 1. Health baseline
    report = await _run_health_mode(cfg, report, artifact_dir,
                                     subprocess_runner=subprocess_runner,
                                     http_client_factory=http_client_factory,
                                     connector_factory=connector_factory,
                                     ha_event_watcher=ha_event_watcher)

    # 2. Establish UTC baseline for log collection timestamps
    baseline_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # 3. Get unique text markers (per-call nonce)
    texts = get_disconnect_texts(cfg, marker=cfg.disconnect_marker)
    original_text = texts["original"]
    original_fp = texts["original_fp"]
    follow_up_text = texts["follow_up"]
    follow_up_fp = texts["follow_up_fp"]
    marker = texts.get("marker", "unknown")

    # Record fingerprints (no plaintext) in outcomes
    report.outcomes["original_fp"] = original_fp
    report.outcomes["follow_up_fp"] = follow_up_fp

    # 4. Take BEFORE snapshot (not active)
    status_before = await capture_admin_status(
        admin_url=cfg.admin_url,
        http_client_factory=http_client_factory if not cfg.dry_run else None,
        subprocess_runner=subprocess_runner if not cfg.dry_run else None,
    )
    snap_before = build_direct_disconnect_status_snapshot(
        phase="before", is_active=False, admin_status=status_before,
    )
    report.status_snapshots.append(snap_before)

    if cfg.dry_run:
        report.outcomes["client_disconnected"] = "dry-run"
        report.outcomes["follow_up_synthesis_succeeded"] = "dry-run"
        report.outcomes["wrapper_cancel_observed"] = "dry-run"
        report.outcomes["original_fp"] = "dry-run"
        report.outcomes["follow_up_fp"] = "dry-run"
        snap_during = build_direct_disconnect_status_snapshot(
            phase="during", is_active=True, admin_status={"skipped": True},
        )
        report.status_snapshots.append(snap_during)
        snap_after = build_direct_disconnect_status_snapshot(
            phase="after", is_active=False, admin_status={"skipped": True},
        )
        report.status_snapshots.append(snap_after)
        return report

    # ── LIVE PATH ────────────────────────────────────────────────
    identity_before = await collect_docker_state(
        cfg.container_names, subprocess_runner,
    )

    # Capture DURING snapshot WHILE disconnect is in flight via callback
    during_captured: dict[str, Any] | None = None

    async def _capture_during_disconnect() -> None:
        """Callback: capture admin status after first audio chunk but before close."""
        nonlocal during_captured
        during_captured = await capture_admin_status(
            admin_url=cfg.admin_url,
            http_client_factory=http_client_factory,
            subprocess_runner=subprocess_runner,
        )

    # 5. Execute Wyoming disconnect during active stream with callback
    disc_result = await wyoming_disconnect_during_stream(
        text=original_text,
        host=cfg.wyoming_host,
        port=cfg.wyoming_port,
        voice=cfg.tts_voice,
        on_first_audio=_capture_during_disconnect,
    )
    report.timeline.append({
        "timestamp": time.monotonic(),
        "event": "direct_disconnect_executed",
        "result": {k: v for k, v in disc_result.items()
                    if k in ("outcome", "got_chunk", "event_count")},
    })

    # Record client disconnect outcome
    client_outcome = disc_result.get("outcome", "error")
    report.outcomes["client_disconnected"] = client_outcome

    # ── Explicit disconnect assertions ──
    report.assertions.append(assert_client_received_audio_before_disconnect(disc_result))
    report.assertions.append(assert_client_disconnected(disc_result))

    # 6. Take DURING snapshot (active — only proceed after audio chunk confirmed)
    status_during = during_captured or await capture_admin_status(
        admin_url=cfg.admin_url,
        http_client_factory=http_client_factory,
        subprocess_runner=subprocess_runner,
    )
    snap_during = build_direct_disconnect_status_snapshot(
        phase="during",
        is_active=disc_result.get("got_chunk", False),
        admin_status=status_during,
    )
    report.status_snapshots.append(snap_during)
    report.assertions.append(assert_active_state_observed_before_disconnect_or_cleanup(snap_during))

    # 7. Wait for terminal + idle state
    idle_status = await _wait_for_terminal_idle(cfg, http_client_factory, subprocess_runner, timeout=15.0)
    # Idle timeout explicit failing assertion
    report.assertions.append(assert_idle_timeout_not_reached(idle_status))

    # 8. Verify container continuity, then collect logs since baseline
    identity_after = await collect_docker_state(
        cfg.container_names, subprocess_runner,
    )
    identity_gate = assert_container_ids_unchanged(identity_before, identity_after)
    report.assertions.append(identity_gate)
    if not identity_gate.passed:
        report.errors.append(
            "Container identity changed during direct-disconnect; log correlation aborted"
        )
        return report
    all_logs = await collect_docker_logs(
        container_names=cfg.container_names,
        subprocess_runner=subprocess_runner,
        since=baseline_utc,
    )
    wrapper_log_lines = all_logs.get("wyoming-s2cpp-tts", [])
    backend_log_lines = all_logs.get("s2cpp-backend", [])
    report.wrapper_logs = wrapper_log_lines
    report.backend_logs = backend_log_lines

    # 9. Correlate logs and classify outcomes
    correlation = correlate_disconnect_logs(
        wrapper_logs=wrapper_log_lines,
        backend_logs=backend_log_lines,
        original_text_fp=original_fp,
    )

    # Record correlation IDs (no plaintext) in outcomes
    conn_id = correlation.get("connection_id")
    synth_id = correlation.get("synthesis_id")
    if conn_id:
        report.outcomes["original_connection_id"] = conn_id
    if synth_id:
        report.outcomes["original_synthesis_id"] = synth_id
    report.outcomes["original_conn_closed"] = str(correlation.get("has_conn_closed", False))

    # Classify wrapper outcome
    wrapper_events = correlation.get("wrapper_events", [])
    wrapper_outcome = classify_wrapper_outcome(wrapper_events)
    report.outcomes["cancellation_requested"] = str(any(
        event.get("event") == "cancellation_requested" for event in wrapper_events
    )).lower()
    report.outcomes["cancellation_propagated"] = str(any(
        event.get("event") == "cancellation_propagated" for event in wrapper_events
    )).lower()
    terminal_events = [
        event for event in wrapper_events if event.get("event") == "synthesis_terminal"
    ]
    report.outcomes["synthesis_terminal"] = (
        str(terminal_events[-1].get("terminal_state", "unknown"))
        if terminal_events else "unknown"
    )

    # ── Explicit wrapper assertions ──
    report.assertions.append(assert_original_request_terminal_state_known(wrapper_outcome))

    if wrapper_outcome == "cancelled":
        report.assertions.append(assert_wrapper_cancel_observed(wrapper_outcome))
        report.outcomes["wrapper_cancel_observed"] = "true"
        report.outcomes["wrapper_cancel_requested"] = "true"
    elif wrapper_outcome == "client disconnected":
        report.assertions.append(assert_wrapper_cancel_observed(wrapper_outcome))
        report.outcomes["wrapper_cancel_observed"] = "client_disconnected"
        report.outcomes["wrapper_cancel_requested"] = "false"
    elif wrapper_outcome == "completed normally":
        # PARTIAL classification: normal completion + cleanup/recovery
        report.assertions.append(assert_wrapper_cancel_observed(wrapper_outcome))
        report.assertions.append(assert_original_completed_normally(wrapper_outcome))
        report.outcomes["old_synthesis_completed_normally"] = "true"
        report.outcomes["wrapper_cancel_requested"] = "PARTIAL (normal completion + recovery)"
        report.outcomes["wrapper_cancel_observed"] = "PARTIAL (normal completion + recovery)"
    else:
        report.assertions.append(assert_wrapper_cancel_observed(wrapper_outcome))
        report.outcomes["wrapper_cancel_requested"] = "false"
        if correlation.get("wrapper_unknown", True):
            report.outcomes["wrapper_cancel_observed"] = "unknown (incomplete logs)"

    # Classify backend outcome
    backend_events = correlation.get("backend_events", [])
    backend_outcome = classify_backend_outcome(backend_events)
    report.assertions.append(assert_backend_terminal_state_known(backend_outcome))
    report.outcomes["backend_abort_observed"] = (
        "true" if backend_outcome == "aborted early" else "false"
    )
    if not backend_events:
        report.outcomes["backend_abort_observed"] = "unknown (no backend events correlated)"

    # Record classification details
    report.outcomes["wrapper_outcome_classification"] = wrapper_outcome
    report.outcomes["backend_outcome_classification"] = backend_outcome

    # ── Scheduler recovery assertions (narrow, non-duplicated) ──
    if idle_status is not None and "error" not in idle_status:
        report.assertions.append(assert_scheduler_returned_to_zero(idle_status))
        report.assertions.append(assert_pending_returned_to_zero(idle_status))
        report.assertions.append(assert_waiting_returned_to_zero(idle_status))
        report.assertions.append(assert_active_synthesis_false(idle_status))
        report.assertions.append(assert_wrapper_ready(idle_status))
        report.assertions.append(assert_no_busy_503_latch(idle_status))

    # 10. Execute distinct follow-up Wyoming synthesis through AudioStop
    follow_up_result = await wyoming_synthesize(
        text=follow_up_text,
        host=cfg.wyoming_host,
        port=cfg.wyoming_port,
        voice=cfg.tts_voice,
    )
    report.timeline.append({
        "timestamp": time.monotonic(),
        "event": "follow_up_synthesis_executed",
        "result": {k: v for k, v in follow_up_result.items()
                    if k in ("pcm_bytes", "duration_s", "protocol_valid", "errors")},
    })

    # ── Explicit follow-up assertions ──
    report.assertions.append(assert_follow_up_request_protocol_valid(follow_up_result))
    report.assertions.append(assert_follow_up_request_pcm_bytes_gt_zero(follow_up_result))
    report.assertions.append(verify_follow_up_synthesis(follow_up_result))
    report.outcomes["follow_up_synthesis_succeeded"] = str(
        follow_up_result.get("protocol_valid", False)
    )

    # 10b. Collect logs again after follow-up and correlate separately
    followup_logs = await collect_docker_logs(
        container_names=cfg.container_names,
        subprocess_runner=subprocess_runner,
        since=baseline_utc,
    )
    preserve_latest_docker_logs(report, followup_logs)
    followup_wrapper = followup_logs.get("wyoming-s2cpp-tts", [])
    followup_correlation = correlate_disconnect_logs(
        wrapper_logs=followup_wrapper,
        backend_logs=followup_logs.get("s2cpp-backend", []),
        original_text_fp=follow_up_fp,
    )

    # Record follow-up correlation IDs
    fw_conn = followup_correlation.get("connection_id")
    fw_synth = followup_correlation.get("synthesis_id")
    if fw_conn:
        report.outcomes["follow_up_connection_id"] = fw_conn
    if fw_synth:
        report.outcomes["follow_up_synthesis_id"] = fw_synth

    follow_up_completed = assert_follow_up_request_completed(followup_correlation)
    report.assertions.append(follow_up_completed)
    report.outcomes["follow_up_completed"] = str(follow_up_completed.passed).lower()
    report.outcomes["follow_up_synthesis_correlated"] = str(
        not followup_correlation.get("wrapper_unknown", True)
    )

    # 11. Take AFTER snapshot (not active)
    status_after = await capture_admin_status(
        admin_url=cfg.admin_url,
        http_client_factory=http_client_factory,
        subprocess_runner=subprocess_runner,
    )
    snap_after = build_direct_disconnect_status_snapshot(
        phase="after", is_active=False, admin_status=status_after,
    )
    report.status_snapshots.append(snap_after)

    # ── Final recovery assertion ──
    if "error" not in status_after:
        report.assertions.append(assert_follow_up_scheduler_recovered(status_after))

    return report

async def _wait_for_terminal_idle(
    cfg: ValidationConfig,
    http_client_factory: Any,
    subprocess_runner: Any,
    timeout: float = 15.0,
) -> dict[str, Any] | None:
    """Wait for the scheduler to reach terminal idle state (quiescent).

    Checks: has_active_synthesis, scheduler_depth, scheduler_pending,
    scheduler_waiting, ready, busy. Uses retry delta/latch to detect
    progress. Returns the final status snapshot or None on timeout.
    """
    deadline = time.monotonic() + timeout
    last_pending: int | None = None
    stall_count = 0
    while time.monotonic() < deadline:
        status = await capture_admin_status(
            admin_url=cfg.admin_url,
            http_client_factory=http_client_factory,
            subprocess_runner=subprocess_runner,
        )
        if "error" in status:
            await asyncio.sleep(0.5)
            continue

        has_active = status.get("has_active_synthesis", False)
        pending = status.get("scheduler_pending", 0)
        depth = status.get("scheduler_depth", 0)
        waiting = status.get("scheduler_waiting", 0)
        ready = status.get("ready", True)
        busy = status.get("busy", False)

        # Core idle check: no active, zero depth/pending, not busy
        if not has_active and pending == 0 and depth == 0 and not busy:
            return status

        # Retry delta/latch: detect stall
        if last_pending is not None and pending == last_pending and depth > 0:
            stall_count += 1
        else:
            stall_count = 0
        last_pending = pending

        # If stalled for > 5 iterations, proceed with what we have
        if stall_count >= 5:
            return status

        await asyncio.sleep(0.5)
    # Timeout — return None, caller proceeds with what we have
    return None


async def _run_overlap_recovery_mode(
    cfg: ValidationConfig,
    report: ValidationReport,
    artifact_dir: Path,
    *,
    subprocess_runner: Any = None,
    http_client_factory: Any = None,
    connector_factory: Any = None,
    ha_event_watcher: Any = None,
) -> ValidationReport:
    """Overlap recovery mode: overlapping requests, verify cleanup."""
    report = await _run_health_mode(cfg, report, artifact_dir,
                                     subprocess_runner=subprocess_runner,
                                     http_client_factory=http_client_factory,
                                     connector_factory=connector_factory,
                                     ha_event_watcher=ha_event_watcher)

    status_before = await capture_admin_status(admin_url=cfg.admin_url,
                                                http_client_factory=http_client_factory if not cfg.dry_run else None)
    report.status_snapshots.append(status_before)

    if cfg.dry_run:
        report.outcomes["scheduler_queue_recovered"] = "dry-run"
    else:
        # Submit two overlapping synthesis requests
        task1 = asyncio.create_task(wyoming_synthesize(
            text=cfg.tts_text, host=cfg.wyoming_host, port=cfg.wyoming_port,
            voice=cfg.tts_voice))
        task2 = asyncio.create_task(wyoming_synthesize(
            text=cfg.tts_text + " (second)", host=cfg.wyoming_host,
            port=cfg.wyoming_port, voice=cfg.tts_voice))
        results = await asyncio.gather(task1, task2)
        report.outcomes["overlap_results"] = [
            {"pcm": r.get("pcm_bytes", 0), "valid": r.get("protocol_valid", False)}
            for r in results
        ]

    status_after = await capture_admin_status(admin_url=cfg.admin_url,
                                               http_client_factory=http_client_factory if not cfg.dry_run else None)
    report.status_snapshots.append(status_after)
    if "error" not in status_after:
        report.assertions.append(assert_queue_pending_zero(status_after))
        report.assertions.append(assert_no_active_synthesis(status_after))
        report.outcomes["scheduler_queue_recovered"] = str(
            status_after.get("scheduler_pending", -1) == 0
        )

    return report


async def _run_vpe_barge_in_mode(
    cfg: ValidationConfig,
    report: ValidationReport,
    artifact_dir: Path,
    *,
    subprocess_runner: Any = None,
    http_client_factory: Any = None,
    connector_factory: Any = None,
    ha_event_watcher: Any = None,
) -> ValidationReport:
    """VPE barge-in mode: prompt operator, observe states/new Assist run."""
    report = await _run_health_mode(cfg, report, artifact_dir,
                                     subprocess_runner=subprocess_runner,
                                     http_client_factory=http_client_factory,
                                     connector_factory=connector_factory,
                                     ha_event_watcher=ha_event_watcher)

    status_before = await capture_admin_status(admin_url=cfg.admin_url,
                                                http_client_factory=http_client_factory if not cfg.dry_run else None)
    report.status_snapshots.append(status_before)

    if cfg.dry_run:
        report.outcomes["barge_in_detected"] = "dry-run"
        report.outcomes["barge_in_not_detected"] = "dry-run"
        report.outcomes["replacement_assist_run_created"] = "dry-run"
    else:
        # Trigger long assist via HA API
        assist_result = await trigger_assist_pipeline(
            ha_url=cfg.ha_url, ha_token=cfg.ha_token,
            text="What is the weather forecast for tomorrow with extended details?",
            entity_id=cfg.vpe_assist_satellite,
        )
        report.outcomes["replacement_assist_run_created"] = (
            "error" not in assist_result
        )

    status_after = await capture_admin_status(admin_url=cfg.admin_url,
                                               http_client_factory=http_client_factory if not cfg.dry_run else None)
    report.status_snapshots.append(status_after)

    return report


# ── Mode dispatcher ────────────────────────────────────────────────

_MODE_HANDLERS = {
    ValidationMode.HEALTH: _run_health_mode,
    ValidationMode.NORMAL: _run_normal_mode,
    ValidationMode.MEDIA_STOP: _run_media_stop_mode,
    ValidationMode.DIRECT_DISCONNECT: _run_direct_disconnect_mode,
    ValidationMode.OVERLAP_RECOVERY: _run_overlap_recovery_mode,
    ValidationMode.VPE_BARGE_IN: _run_vpe_barge_in_mode,
}


async def _dispatch_mode(
    cfg: ValidationConfig,
    report: ValidationReport,
    artifact_dir: Path,
    *,
    subprocess_runner: Any = None,
    http_client_factory: Any = None,
    connector_factory: Any = None,
    ha_event_watcher: Any = None,
) -> ValidationReport:
    """Dispatch to the appropriate mode handler."""
    handler = _MODE_HANDLERS.get(cfg.mode, _run_health_mode)
    return await handler(cfg, report, artifact_dir,
                         subprocess_runner=subprocess_runner,
                         http_client_factory=http_client_factory,
                         connector_factory=connector_factory,
                         ha_event_watcher=ha_event_watcher)


# ── Main orchestration ─────────────────────────────────────────────

async def run_validation(
    cfg: ValidationConfig,
    *,
    subprocess_runner: Any = None,
    http_client_factory: Any = None,
    connector_factory: Any = None,
    _mode_dispatcher: Any = None,
) -> ValidationReport:
    """Run the full validation according to *cfg*, dispatching to mode-specific handler.

    (M) Always writes artifacts even on failure; sets duration before writing.
    """
    start = time.monotonic()
    report = ValidationReport(
        mode=cfg.mode.value,
        utc_timestamp=time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        dry_run=cfg.dry_run,
    )

    try:
        # Pre-flight: confirmation (B: health bypasses)
        if requires_confirmation(cfg):
            if not confirm_live_action():
                report.errors.append("Live confirmation denied")
                report.duration_sec = round(time.monotonic() - start, 3)
                artifact_dir = make_artifact_dir(cfg.artifact_base, report.utc_timestamp)
                _write_all_artifacts(report, artifact_dir)
                return report
            object.__setattr__(cfg, "allow_ha_actions", True)

        # Artifact directory
        artifact_dir = make_artifact_dir(cfg.artifact_base, report.utc_timestamp)

        # Dispatch to mode-specific handler
        # In dry-run mode, never pass subprocess_runner/connector_factory
        # so mode handlers cannot accidentally do I/O
        dispatcher = _mode_dispatcher if _mode_dispatcher is not None else _dispatch_mode
        report = await dispatcher(
            cfg, report, artifact_dir,
            subprocess_runner=subprocess_runner if not cfg.dry_run else None,
            http_client_factory=http_client_factory if not cfg.dry_run else None,
            connector_factory=connector_factory if not cfg.dry_run else None,
        )

        # (M) Set duration before writing artifacts
        report.duration_sec = round(time.monotonic() - start, 3)

        # Write artifacts
        _write_all_artifacts(report, artifact_dir)

    except Exception as exc:
        # (M) Catch, sanitize, always write artifacts
        report.errors.append(f"Fatal error: {exc}")
        report.duration_sec = round(time.monotonic() - start, 3)
        artifact_dir = make_artifact_dir(cfg.artifact_base, report.utc_timestamp)
        _write_all_artifacts(report, artifact_dir)

    return report


# ── CLI ────────────────────────────────────────────────────────────

def _add_cli_arguments(parser: argparse.ArgumentParser) -> None:
    """Add CLI arguments to an ArgumentParser."""
    parser.add_argument(
        "--mode", type=str, default="health",
        choices=[m.value for m in ValidationMode],
        help="Validation mode (default: health)",
    )
    live_group = parser.add_mutually_exclusive_group()
    live_group.add_argument(
        "--run-live", action="store_true", default=False,
        help="Enable live actions (requires typed confirmation for active modes)",
    )
    live_group.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Dry-run mode (default; no real I/O)",
    )
    parser.add_argument(
        "--ha-url", type=str,
        default=os.getenv("HA_URL", ""),
        help="Home Assistant URL",
    )
    parser.add_argument(
        "--ha-token-file", type=str,
        default=os.getenv("HA_TOKEN_FILE", ""),
        help="File containing HA long-lived access token",
    )
    parser.add_argument(
        "--wyoming-host", type=str, default="127.0.0.1",
        help="Wyoming TTS host",
    )
    parser.add_argument(
        "--wyoming-port", type=int, default=10200,
        help="Wyoming TTS port",
    )
    parser.add_argument(
        "--admin-url", type=str, default="http://127.0.0.1:10201",
        help="Admin HTTP server URL",
    )
    parser.add_argument(
        "--artifact-dir", type=str, default="artifacts/phase10",
        help="Base artifact directory",
    )


def build_config_from_args(args: argparse.Namespace) -> ValidationConfig:
    """Build a ValidationConfig from parsed CLI args."""
    ha_token = ""
    if args.ha_token_file:
        try:
            ha_token = Path(args.ha_token_file).read_text().strip()
        except Exception as exc:
            print(f"Warning: Could not read HA token file: {exc}", file=sys.stderr)

    # --run-live overrides --dry-run default
    dry_run = not args.run_live

    return ValidationConfig(
        mode=ValidationMode(args.mode),
        dry_run=dry_run,
        ha_url=args.ha_url,
        ha_token=ha_token,
        ha_token_env_var=None if ha_token else "HA_TOKEN",
        wyoming_host=args.wyoming_host,
        wyoming_port=args.wyoming_port,
        admin_url=args.admin_url,
        admin_port=_ADMIN_DEFAULT_PORT,
        artifact_base=Path(args.artifact_dir),
    )


# ── Live CLI adapters ──────────────────────────────────────────────

@asynccontextmanager
async def real_tcp_connector(host: str, port: int):
    """Open and deterministically close one real TCP connection."""
    reader, writer = await asyncio.open_connection(host, port)
    try:
        yield reader, writer
    finally:
        writer.close()
        await writer.wait_closed()


def validate_read_only_docker_command(cmd: list[str]) -> None:
    """Reject every command outside the exact Phase 10 Docker whitelist."""
    if not isinstance(cmd, list) or not all(isinstance(v, str) for v in cmd):
        raise ValueError("command must be a string argument list")
    if len(cmd) == 7 and cmd[:5] == ["docker", "inspect", "--type", "container", "--format"]:
        if cmd[5] != DOCKER_INSPECT_FORMAT or cmd[6] not in DOCKER_CONTAINERS:
            raise ValueError("docker inspect command not whitelisted")
        return
    if len(cmd) in (6, 8) and cmd[:4] == ["docker", "logs", "--timestamps", "--tail"]:
        try:
            tail = int(cmd[4])
        except ValueError as exc:
            raise ValueError("log tail must be an integer") from exc
        if not 1 <= tail <= 5000:
            raise ValueError("log tail outside whitelist bounds")
        if len(cmd) == 6:
            container = cmd[5]
        else:
            if cmd[5] != "--since" or not _SINCE_RE.fullmatch(cmd[6]):
                raise ValueError("log timestamp not whitelisted")
            container = cmd[7]
        if container not in DOCKER_CONTAINERS:
            raise ValueError("container not whitelisted")
        return
    raise ValueError("Docker command not whitelisted")


class SshReadOnlyDockerRunner:
    """Execute only the explicit read-only Docker whitelist over SSH."""

    def __init__(self, host: str, user: str, key_file: str) -> None:
        key = Path(key_file).expanduser()
        if not host or not _SSH_HOST_RE.fullmatch(host):
            raise ValueError("invalid UNRAID_SSH_HOST")
        if not user or not _SSH_USER_RE.fullmatch(user):
            raise ValueError("invalid UNRAID_SSH_USER")
        if not key.is_file():
            raise ValueError("UNRAID_SSH_KEY_FILE is not a file")
        self.host, self.user, self.key_file = host, user, key
        self.commands: list[list[str]] = []

    async def run(self, cmd: list[str], *, capture_output: bool = True,
                  timeout: float = 30) -> subprocess.CompletedProcess[bytes]:
        validate_read_only_docker_command(cmd)
        self.commands.append(list(cmd))
        container = cmd[-1]
        prefix = "wrapper" if container == "wyoming-s2cpp-tts" else "backend"
        alias = f"{prefix}-inspect" if cmd[1] == "inspect" else f"{prefix}-logs"
        ssh_cmd = [
            "ssh", "-i", str(self.key_file), "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
            f"{self.user}@{self.host}", alias,
        ]
        def _run() -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(ssh_cmd, capture_output=capture_output,
                                  text=False, timeout=timeout, shell=False)
        return await asyncio.get_running_loop().run_in_executor(None, _run)


def build_live_subprocess_runner() -> Any:
    """Choose SSH Docker collection only when all three env vars are set."""
    values = [os.getenv("UNRAID_SSH_HOST", ""), os.getenv("UNRAID_SSH_USER", ""),
              os.getenv("UNRAID_SSH_KEY_FILE", "")]
    if any(values) and not all(values):
        raise ValueError("UNRAID_SSH_HOST, UNRAID_SSH_USER, and UNRAID_SSH_KEY_FILE must all be set")
    if all(values):
        return SshReadOnlyDockerRunner(*values)
    return AsyncSubprocessRunner()


class AsyncSubprocessRunner:
    """Real async subprocess adapter for CLI use.

    Runs subprocess in a thread to avoid blocking the event loop.
    """

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    async def run(
        self,
        cmd: list[str],
        *,
        capture_output: bool = True,
        timeout: float = 30,
    ) -> subprocess.CompletedProcess[str]:
        """Run *cmd* asynchronously via asyncio.to_thread."""
        self.commands.append(cmd)

        def _run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                cmd,
                capture_output=capture_output,
                text=False,
                timeout=timeout,
            )

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _run)


def main() -> int:
    """CLI entry point.

    (D) Passes real AsyncSubprocessRunner for live modes.
    """
    parser = argparse.ArgumentParser(
        description="Phase 10 live-validation harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    _add_cli_arguments(parser)
    args = parser.parse_args()

    cfg = build_config_from_args(args)

    print(f"Phase 10 Live Validation — "
          f"mode={cfg.mode.value} dry_run={cfg.dry_run}")
    print(f"  HA URL: {cfg.ha_url or '(not set)'}")
    print(f"  Wyoming: {cfg.wyoming_host}:{cfg.wyoming_port}")
    print(f"  Admin:   {cfg.admin_url}")
    print(f"  Artifacts: {cfg.artifact_base.absolute()}")
    if cfg.mode.value in READ_ONLY_MODES:
        print(f"  Health mode: read-only, no confirmation required")
    print()

    # (D) Create real async subprocess runner for live modes
    subprocess_runner = None if cfg.dry_run else build_live_subprocess_runner()

    report = asyncio.run(run_validation(
        cfg,
        subprocess_runner=subprocess_runner,
        connector_factory=real_tcp_connector if not cfg.dry_run else None,
    ))

    print(f"\nValidation complete ({report.duration_sec}s)")
    print(f"  Passed: {report.assertions_passed}")
    print(f"  Failed: {report.assertions_failed}")
    for a in report.assertions:
        status = "PASS" if a.passed else "FAIL"
        print(f"  [{status}] {a.name}: {a.detail}")
    if report.errors:
        print(f"\nErrors:")
        for e in report.errors:
            print(f"  FAIL: {e}")

    return 0 if report.assertions_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

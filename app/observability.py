"""Structured request-level observability for the Wyoming TTS wrapper.

Generates correlation identifiers, fingerprints text without storing it,
and provides a single ``obs_log`` helper that emits structured JSON log
lines.  All public functions are side-effect-free except ``obs_log``.

No sensitive content (full text, filesystem paths, model data, or
multipart bodies) is ever logged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("wyoming-s2cpp-tts.obs")

# ── ID generators ──────────────────────────────────────────────────────

def new_connection_id() -> str:
    """Short random hex identifier for one Wyoming TCP connection."""
    return secrets.token_hex(4)


def new_synthesis_id() -> str:
    """Short random hex identifier for one logical synthesis request."""
    return secrets.token_hex(4)


# ── Text fingerprinting ────────────────────────────────────────────────

def text_fingerprint(text: str) -> str:
    """Return the first 12 hex chars of the SHA-256 digest of *text*.

    The fingerprint is stable for identical text but does not reveal the
    original content.  An empty string returns ``"<empty>"``.
    """
    if not text:
        return "<empty>"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# ── Structured log helper ──────────────────────────────────────────────

def _serialise(obj: Any) -> Any:
    """Convert non-serialisable values to strings for JSON output."""
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    return str(obj)


def obs_log(event: str, **fields: Any) -> None:
    """Emit a structured JSON log line at INFO level.

    Args:
        event: A short snake_case event name (e.g. ``"conn_open"``,
               ``"event_in"``, ``"syn_trigger"``, ``"backend_call"``).
        **fields: Arbitrary keyword arguments serialised as JSON keys.
    """
    payload = {"event": event}
    payload.update({k: _serialise(v) for k, v in fields.items()})
    logger.info(json.dumps(payload, sort_keys=True))


# ── Log context for propagating identifiers ────────────────────────────

@dataclass
class LogContext:
    """Lightweight context bag carried through one synthesis request.

    ``connection_id`` is set once when the TCP connection opens.
    ``synthesis_id`` is regenerated for each logical synthesis.
    """

    connection_id: str = ""
    synthesis_id: str = ""

"""Health endpoint scaffold.

TODO: expose a lightweight HTTP health/debug service on port 8088 in a future
phase. It should report wrapper status, backend reachability, and queue state.
"""

from __future__ import annotations


def health_payload() -> dict[str, str]:
    """Return a static scaffold health payload."""
    return {"status": "scaffold", "service": "wyoming-s2cpp-tts"}

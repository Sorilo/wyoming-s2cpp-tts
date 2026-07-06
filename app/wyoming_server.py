"""Wyoming server scaffold.

TODO Phase 1: implement a minimal Wyoming Protocol server that returns fake PCM
so Home Assistant/client compatibility can be tested before real model work.
"""

from __future__ import annotations


class WyomingServerNotImplemented(RuntimeError):
    """Raised if code tries to start the scaffold-only server."""


def describe_planned_server() -> str:
    """Return a human-readable description of the planned Wyoming endpoint."""
    return "planned Wyoming TTS server on tcp://0.0.0.0:10200"


def run_server() -> None:
    """Placeholder for the future Wyoming TCP server."""
    raise WyomingServerNotImplemented("Phase 1 will implement the Wyoming server")

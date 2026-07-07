"""Application entrypoint for the Wyoming TTS server.

By default this starts deterministic fake/test PCM. With `TTS_BACKEND=s2cpp`, it
uses the opt-in non-streaming bridge to an already-running s2.cpp HTTP server.
Later phases will supervise s2.cpp and add real streaming/cancellation behavior.
"""

from __future__ import annotations

from app.config import Settings
from app.wyoming_server import run_server


def main() -> int:
    """Run the configured Wyoming TTS server."""
    run_server(Settings.from_env())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

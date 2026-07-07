"""Application entrypoint for the Phase 1 fake Wyoming TTS server.

This starts deterministic fake/test PCM only. Later phases will supervise s2.cpp
and route real Fish Speech audio through this Wyoming boundary.
"""

from __future__ import annotations

from app.config import Settings
from app.wyoming_server import run_server


def main() -> int:
    """Run the Phase 1 fake Wyoming TTS server."""
    run_server(Settings())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Application entrypoint placeholder.

Future phases will start the Wyoming TCP server, health endpoint, and s2.cpp
process supervision from here. Phase 0 intentionally does not run a service.
"""

from __future__ import annotations

from app.config import Settings


def main() -> int:
    """Print a safe scaffold-only message and exit."""
    settings = Settings()
    print("wyoming-s2cpp-tts scaffold only")
    print(f"planned Wyoming URI: {settings.wyoming_uri}")
    print(f"planned s2.cpp endpoint: http://{settings.s2_host}:{settings.s2_port}")
    print(f"planned model: {settings.s2_model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

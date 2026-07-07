#!/usr/bin/env python3
"""Optional direct smoke test for an already-running s2.cpp HTTP `/generate`.

Usage:
    TTS_BACKEND=s2cpp S2_HOST=192.168.1.45 S2_PORT=3030 \
      python scripts/smoke_s2cpp_generate.py --text "hello"

If TTS_BACKEND is not `s2cpp`, the script exits successfully with a skipped
message. If the backend is unavailable, it exits successfully with an unavailable
message so normal tests/CI do not require model infrastructure.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import Settings
from app.smoke_s2cpp import run_smoke


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Optional direct smoke test for an already-running s2.cpp /generate endpoint."
    )
    parser.add_argument(
        "--text",
        default="Hello from wyoming-s2cpp-tts direct smoke test.",
        help="Text to send to the external s2.cpp /generate endpoint.",
    )
    args = parser.parse_args()

    result = run_smoke(settings=Settings.from_env(), text=args.text)
    print(f"status={result.status}")
    print(f"endpoint={result.endpoint}")
    if result.content_type:
        print(f"content_type={result.content_type}")
    print(f"bytes_received={result.bytes_received}")
    print(result.message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

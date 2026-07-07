#!/usr/bin/env python3
"""Phase 5.5 smoke-test CLI for an already-running s2.cpp HTTP ``/generate`` backend.

This script is deliberately opt-in and backend-client-only. It never starts,
builds, downloads, packages, or supervises s2.cpp.

Usage (harmless skip — validates config only):
    python scripts/smoke_s2cpp_generate.py

Usage (real test with explicit opt-in):
    python scripts/smoke_s2cpp_generate.py --run-real

Usage (with explicit endpoint override):
    python scripts/smoke_s2cpp_generate.py --run-real --endpoint 192.168.1.45:3030

Usage (hard-fail when backend is unavailable):
    python scripts/smoke_s2cpp_generate.py --run-real --require-backend

Usage (save diagnostic audio output):
    python scripts/smoke_s2cpp_generate.py --run-real --output-dir /tmp/smoke-out

Environment variables respected (via Settings.from_env()):
    TTS_BACKEND, S2_HOST, S2_PORT

Without ``--run-real`` the script prints ``status=skipped`` and exits 0.
With ``--run-real`` but unreachable backend it prints ``status=unavailable``
(exit 0 unless ``--require-backend`` is also set).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import Settings
from app.smoke_harness import SmokeConfig, SmokeReport, format_summary, run_smoke_harness


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Phase 5.5 s2.cpp smoke test — opt-in real backend validation."
    )
    parser.add_argument(
        "--run-real",
        action="store_true",
        help="Send real synthesis requests to a running backend (REQUIRED for any backend contact).",
    )
    parser.add_argument(
        "--require-backend",
        action="store_true",
        help="Exit nonzero when the backend is unavailable (only meaningful with --run-real).",
    )
    parser.add_argument(
        "--endpoint",
        default=None,
        help="Explicit host:port override for the s2.cpp backend (e.g. 192.168.1.45:3030).",
    )
    parser.add_argument(
        "--text",
        default="Hello from wyoming-s2cpp-tts smoke test.",
        help="Short bounded text to send for synthesis.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Per-request timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--probe-legacy-json",
        action="store_true",
        help="Also probe the legacy JSON /generate path (expected unsupported).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Optional directory to save diagnostic WAV/PCM output files.",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Print the report as JSON instead of the human-readable summary.",
    )
    args = parser.parse_args()

    settings = Settings.from_env()
    config = SmokeConfig(
        run_real=args.run_real,
        require_backend=args.require_backend,
        endpoint_override=args.endpoint,
        probe_legacy_json=args.probe_legacy_json,
        output_dir=args.output_dir,
        text=args.text,
        timeout_seconds=args.timeout,
    )

    report: SmokeReport = run_smoke_harness(config, settings, repo_root=REPO_ROOT)

    if args.json_output:
        print(report.to_json())
    else:
        print(format_summary(report))

    # Exit status
    if config.require_backend and report.overall_status == "unavailable":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

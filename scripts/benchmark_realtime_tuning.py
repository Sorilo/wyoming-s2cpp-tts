#!/usr/bin/env python3
"""Real-time stride tuning benchmark harness for s2.cpp backend (Phase 8C).

Sends streaming multipart requests to an s2.cpp backend with varying
stream_decode_stride_frames and measures real-time factor (RTF),
time-to-first-audio, and other performance metrics.

SAFETY: Defaults to dry-run mode.  Requires ``--run-real`` to contact
a real backend.  This harness contacts the s2.cpp backend DIRECTLY (bypassing
the Wyoming wrapper).  **No wrapper rebuild is required** to run this benchmark.
However, for Home Assistant / Wyoming to use new stride tuning settings, a
new wrapper image containing these code changes must be built and deployed.

IMPORTANT: This script uses only Python standard library modules.  No virtual
environment or pip packages are required.  Run with ``python3`` from the
repository root.  Requires `--run-real` to contact
a real backend.  Without it, the harness prints what it would do and exits.

Usage:
    # Dry-run (safe default)
    python scripts/benchmark_realtime_tuning.py

    # Real benchmark against local backend
    python scripts/benchmark_realtime_tuning.py --run-real \\
        --endpoint 127.0.0.1:3031 \\
        --text "Hello, this is a performance benchmark test."

    # Custom stride sweep
    python scripts/benchmark_realtime_tuning.py --run-real \\
        --strides 1,2,4,8,16 \\
        --warmup-runs 1 --measured-runs 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Project-local imports ────────────────────────────────────────────────
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
# Support both PYTHONPATH env var and direct path insertion
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.s2_client import (
    S2Client,
    S2Endpoint,
    S2GenerateRequest,
    S2StreamResult,
    S2ClientError,
)
from app.audio import PCM_WIDTH_BYTES, PCM_CHANNELS


# ── Constants ────────────────────────────────────────────────────────────
DEFAULT_ENDPOINT = "127.0.0.1:3030"
DEFAULT_STRIDES = [1, 2, 4, 8]
DEFAULT_CODEC_CONTEXT = 4
DEFAULT_WARMUP_RUNS = 1
DEFAULT_MEASURED_RUNS = 3
DEFAULT_TIMEOUT = 120.0
DEFAULT_SAMPLE_RATE = 44100
DEFAULT_TEXT = (
    "The quick brown fox jumps over the lazy dog. "
    "This is a benchmark test for real time speech synthesis performance."
)


# ── Data structures ──────────────────────────────────────────────────────

@dataclass
class RunResult:
    """One measured benchmark run."""
    stride: int
    run_index: int
    time_to_headers_ms: float
    time_to_first_pcm_ms: float
    total_wall_ms: float
    pcm_bytes: int
    audio_duration_ms: float
    real_time_factor: float
    status: str  # "success" | "error"
    error: str = ""
    response_headers: dict[str, str] = field(default_factory=dict)
    pcm_saved_path: str = ""


@dataclass
class StrideSummary:
    """Aggregated results for one stride value."""
    stride: int
    runs: list[RunResult] = field(default_factory=list)

    @property
    def success_runs(self) -> list[RunResult]:
        return [r for r in self.runs if r.status == "success"]

    @property
    def avg_rtf(self) -> float | None:
        ok = self.success_runs
        if not ok:
            return None
        return sum(r.real_time_factor for r in ok) / len(ok)

    @property
    def avg_first_pcm_ms(self) -> float | None:
        ok = self.success_runs
        if not ok:
            return None
        return sum(r.time_to_first_pcm_ms for r in ok) / len(ok)

    @property
    def avg_total_ms(self) -> float | None:
        ok = self.success_runs
        if not ok:
            return None
        return sum(r.total_wall_ms for r in ok) / len(ok)


# ── Helpers ──────────────────────────────────────────────────────────────

def pcm_duration_ms(pcm_bytes: int, sample_rate: int = DEFAULT_SAMPLE_RATE,
                    width: int = PCM_WIDTH_BYTES, channels: int = PCM_CHANNELS) -> float:
    """Calculate audio duration in milliseconds from raw PCM bytes."""
    frame_size = width * channels
    if frame_size <= 0 or sample_rate <= 0:
        return 0.0
    samples = pcm_bytes // frame_size
    return (samples / sample_rate) * 1000.0


def real_time_factor(total_wall_ms: float, audio_duration_ms: float) -> float:
    """Calculate RTF: wall time / audio duration.

    RTF < 1.0: faster than real time (can keep up with playback)
    RTF = 1.0: exactly real time
    RTF > 1.0: slower than playback (will stutter)
    """
    if audio_duration_ms <= 0:
        return float("inf")
    return total_wall_ms / audio_duration_ms


def _read_all_chunks(stream: S2StreamResult) -> tuple[bytes, float, float]:
    """Read all chunks from stream; return (pcm, time_to_headers, time_to_first_pcm)."""
    start = time.monotonic()
    headers_time = time.monotonic() - start  # after context enter
    first_pcm_time: float | None = None
    pcm_parts: list[bytes] = []

    for chunk in stream:
        if first_pcm_time is None and chunk:
            first_pcm_time = (time.monotonic() - start) * 1000.0
        pcm_parts.append(chunk)

    if first_pcm_time is None:
        first_pcm_time = (time.monotonic() - start) * 1000.0

    pcm = b"".join(pcm_parts)
    return pcm, headers_time, first_pcm_time


# ── Core benchmark logic ─────────────────────────────────────────────────

def run_one_benchmark(
    client: S2Client,
    request: S2GenerateRequest,
    stride: int,
    run_index: int,
    output_dir: Path,
    timeout: float = DEFAULT_TIMEOUT,
) -> RunResult:
    """Run one synthesis and measure performance."""
    # Clone request with this stride
    req = S2GenerateRequest(
        text=request.text,
        voice=request.voice,
        model=request.model,
        stream=True,
        chunked=True,
        output_format="pcm_s16le",
        segment_sentences=False,
        max_new_tokens=request.max_new_tokens,
        temperature=request.temperature,
        top_p=request.top_p,
        top_k=request.top_k,
        prompt_text=request.prompt_text,
        voice_dir=request.voice_dir,
        codec_decode_context_frames=request.codec_decode_context_frames,
        low_latency=request.low_latency,
        stream_decode_stride_frames=stride,
        stream_holdback_frames=request.stream_holdback_frames,
        stream_start_buffer_ms=request.stream_start_buffer_ms,
    )

    start = time.monotonic()
    try:
        with client.generate_stream(req) as stream:
            headers_time_ms = (time.monotonic() - start) * 1000.0

            response_headers = stream.response_headers
            first_pcm_time_ms: float | None = None
            pcm_parts: list[bytes] = []

            for chunk in stream:
                if first_pcm_time_ms is None and chunk:
                    first_pcm_time_ms = (time.monotonic() - start) * 1000.0
                pcm_parts.append(chunk)

            if first_pcm_time_ms is None:
                first_pcm_time_ms = (time.monotonic() - start) * 1000.0

            pcm = b"".join(pcm_parts)
            total_ms = (time.monotonic() - start) * 1000.0
            duration_ms = pcm_duration_ms(len(pcm))
            rtf = real_time_factor(total_ms, duration_ms)

            # Save PCM artifact
            pcm_path = output_dir / f"stride{stride}_run{run_index}.pcm"
            pcm_path.write_bytes(pcm)

            return RunResult(
                stride=stride,
                run_index=run_index,
                time_to_headers_ms=headers_time_ms,
                time_to_first_pcm_ms=first_pcm_time_ms,
                total_wall_ms=total_ms,
                pcm_bytes=len(pcm),
                audio_duration_ms=duration_ms,
                real_time_factor=rtf,
                status="success",
                response_headers=response_headers,
                pcm_saved_path=str(pcm_path),
            )

    except (S2ClientError, urllib.error.URLError, OSError) as exc:
        total_ms = (time.monotonic() - start) * 1000.0
        return RunResult(
            stride=stride,
            run_index=run_index,
            time_to_headers_ms=0,
            time_to_first_pcm_ms=0,
            total_wall_ms=total_ms,
            pcm_bytes=0,
            audio_duration_ms=0,
            real_time_factor=float("inf"),
            status="error",
            error=str(exc),
        )


def run_stride_sweep(
    endpoint: str,
    text: str,
    strides: list[int],
    codec_context: int,
    holdback: int,
    start_buffer_ms: int,
    low_latency: bool,
    warmup_runs: int,
    measured_runs: int,
    output_dir: Path,
    voice: str = "",
    voice_dir: str = "",
    model: str = "/models/s2-pro-q6_k.gguf",
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run a complete stride sweep and return results."""
    host, port_str = endpoint.rsplit(":", 1)
    port = int(port_str)
    ep = S2Endpoint(host=host, port=port)
    client = S2Client(ep, timeout_seconds=timeout)

    base_request = S2GenerateRequest(
        text=text,
        voice=voice,
        model=model,
        stream=True,
        chunked=True,
        output_format="pcm_s16le",
        segment_sentences=False,
        codec_decode_context_frames=codec_context,
        low_latency=low_latency,
        stream_holdback_frames=holdback,
        stream_start_buffer_ms=start_buffer_ms,
    )

    summaries: list[StrideSummary] = []

    for stride in strides:
        print(f"\n{'='*60}")
        print(f"Stride {stride}: {measured_runs} measured runs (warmup={warmup_runs})")
        print(f"{'='*60}")

        summary = StrideSummary(stride=stride)

        # Warm-up (discard results)
        for i in range(warmup_runs):
            print(f"  Warm-up {i+1}/{warmup_runs}...")
            try:
                run_one_benchmark(client, base_request, stride, 999, output_dir, timeout)
            except Exception as exc:
                print(f"  Warm-up failed: {exc}")

        # Measured runs
        for i in range(measured_runs):
            print(f"  Run {i+1}/{measured_runs}...", end=" ", flush=True)
            result = run_one_benchmark(client, base_request, stride, i + 1, output_dir, timeout)
            summary.runs.append(result)
            if result.status == "success":
                print(f"RTF={result.real_time_factor:.2f}, "
                      f"first_pcm={result.time_to_first_pcm_ms:.0f}ms, "
                      f"total={result.total_wall_ms:.0f}ms, "
                      f"pcm={result.pcm_bytes}B, "
                      f"duration={result.audio_duration_ms:.0f}ms")
            else:
                print(f"ERROR: {result.error}")

        summaries.append(summary)

    return {
        "endpoint": endpoint,
        "text": text,
        "text_len": len(text),
        "codec_context": codec_context,
        "holdback": holdback,
        "start_buffer_ms": start_buffer_ms,
        "low_latency": low_latency,
        "strides": strides,
        "warmup_runs": warmup_runs,
        "measured_runs": measured_runs,
        "sample_rate_hz": DEFAULT_SAMPLE_RATE,
        "summaries": [
            {
                "stride": s.stride,
                "avg_rtf": s.avg_rtf,
                "avg_first_pcm_ms": s.avg_first_pcm_ms,
                "avg_total_ms": s.avg_total_ms,
                "runs": [
                    {
                        "run": r.run_index,
                        "status": r.status,
                        "rtf": r.real_time_factor,
                        "time_to_headers_ms": r.time_to_headers_ms,
                        "time_to_first_pcm_ms": r.time_to_first_pcm_ms,
                        "total_wall_ms": r.total_wall_ms,
                        "pcm_bytes": r.pcm_bytes,
                        "audio_duration_ms": r.audio_duration_ms,
                        "error": r.error,
                        "pcm_path": r.pcm_saved_path,
                    }
                    for r in s.runs
                ],
            }
            for s in summaries
        ],
    }


def format_summary(results: dict[str, Any]) -> str:
    """Produce a readable Markdown summary of benchmark results."""
    lines = [
        "# Real-Time Stride Tuning Benchmark Results",
        "",
        f"- **Endpoint**: `{results['endpoint']}`",
        f"- **Text length**: {results['text_len']} chars",
        f"- **Codec context**: {results['codec_context']}",
        f"- **Holdback**: {results['holdback']}",
        f"- **Start buffer**: {results['start_buffer_ms']} ms",
        f"- **Low latency**: {results['low_latency']}",
        f"- **Sample rate**: {results['sample_rate_hz']} Hz (mono s16le)",
        "",
        "## Results by Stride",
        "",
        "| Stride | Avg RTF | Avg First PCM (ms) | Avg Total (ms) | Success |",
        "|--------|---------|---------------------|----------------|---------|",
    ]

    for s in results["summaries"]:
        success_count = sum(1 for r in s["runs"] if r["status"] == "success")
        total_count = len(s["runs"])
        avg_rtf = f"{s['avg_rtf']:.2f}" if s["avg_rtf"] is not None else "N/A"
        avg_first = f"{s['avg_first_pcm_ms']:.0f}" if s["avg_first_pcm_ms"] is not None else "N/A"
        avg_total = f"{s['avg_total_ms']:.0f}" if s["avg_total_ms"] is not None else "N/A"
        lines.append(
            f"| {s['stride']} | {avg_rtf} | {avg_first} | {avg_total} | {success_count}/{total_count} |"
        )

    lines.append("")
    lines.append("## Recommended Candidate")
    lines.append("")
    lines.append(
        "**⚠️ Quality unverified**: The recommended candidate below is based "
        "on RTF and latency metrics only. **Audio quality has not been assessed**. "
        "You MUST listen to the generated PCM files before applying any settings."
    )
    lines.append("")

    # Find best stride (lowest RTF with success)
    best_stride = None
    best_rtf = float("inf")
    for s in results["summaries"]:
        if s["avg_rtf"] is not None and s["avg_rtf"] < best_rtf:
            best_rtf = s["avg_rtf"]
            best_stride = s["stride"]

    if best_stride is not None:
        lines.append(f"- **Fastest stride**: {best_stride} (RTF={best_rtf:.2f})")
        if best_rtf < 1.0:
            lines.append("- **Status**: Faster than real time ✅")
        elif best_rtf == 1.0:
            lines.append("- **Status**: Exactly real time ⚠️")
        else:
            lines.append("- **Status**: Slower than real time ❌")
        lines.append("")
        lines.append("### Suggested Unraid environment variables")
        lines.append("")
        lines.append("```bash")
        lines.append(f"S2_STREAM_DECODE_STRIDE_FRAMES={best_stride}")
        lines.append(f"S2_STREAM_HOLDBACK_FRAMES={results['holdback']}")
        lines.append(f"S2_STREAM_START_BUFFER_MS={results['start_buffer_ms']}")
        lines.append(f"S2_LOW_LATENCY={'true' if results['low_latency'] else 'false'}")
        lines.append("```")
    else:
        lines.append("- **No successful runs** — all strides failed.")
        lines.append("- Check backend logs and connectivity.")

    lines.append("")
    lines.append("## PCM Artifacts")
    lines.append("")
    lines.append("To listen to generated PCM (convert to WAV):")
    lines.append("```bash")
    lines.append("# Convert PCM to WAV")
    lines.append(
        "ffmpeg -f s16le -ar 44100 -ac 1 -i stride4_run1.pcm stride4_run1.wav"
    )
    lines.append("```")

    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Real-time stride tuning benchmark for s2.cpp backend"
    )
    parser.add_argument(
        "--run-real",
        action="store_true",
        help="Actually contact the backend. Without this flag, the harness "
             "prints a dry-run message and exits.",
    )
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help=f"Backend host:port (default: {DEFAULT_ENDPOINT})",
    )
    parser.add_argument(
        "--text",
        default=DEFAULT_TEXT,
        help="Benchmark text to synthesize",
    )
    parser.add_argument(
        "--strides",
        default=",".join(str(s) for s in DEFAULT_STRIDES),
        help="Comma-separated stride values to sweep (default: 1,2,4,8)",
    )
    parser.add_argument(
        "--codec-context",
        type=int,
        default=DEFAULT_CODEC_CONTEXT,
        help=f"Codec decode context frames (default: {DEFAULT_CODEC_CONTEXT})",
    )
    parser.add_argument(
        "--holdback",
        type=int,
        default=0,
        help="Stream holdback frames (default: 0)",
    )
    parser.add_argument(
        "--start-buffer-ms",
        type=int,
        default=0,
        help="Stream start buffer ms (default: 0)",
    )
    parser.add_argument(
        "--no-low-latency",
        action="store_true",
        help="Disable low_latency mode",
    )
    parser.add_argument(
        "--warmup-runs",
        type=int,
        default=DEFAULT_WARMUP_RUNS,
        help=f"Warm-up runs per stride (default: {DEFAULT_WARMUP_RUNS})",
    )
    parser.add_argument(
        "--measured-runs",
        type=int,
        default=DEFAULT_MEASURED_RUNS,
        help=f"Measured runs per stride (default: {DEFAULT_MEASURED_RUNS})",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory for artifacts (default: timestamped dir under "
             "verification_artifacts/realtime_tuning/)",
    )
    parser.add_argument(
        "--voice",
        default="",
        help="Voice profile ID to use",
    )
    parser.add_argument(
        "--voice-dir",
        default="",
        help="Voice directory path",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON results to stdout",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Request timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.run_real:
        print("DRY RUN — no backend will be contacted.")
        print(f"Would benchmark endpoint: {args.endpoint}")
        print(f"Text: {args.text[:80]}...")
        print(f"Strides: {args.strides}")
        print(f"Warm-up runs: {args.warmup_runs}")
        print(f"Measured runs: {args.measured_runs}")
        print()
        print("Add --run-real to execute the benchmark against a live backend.")
        return 0

    strides = [int(s.strip()) for s in args.strides.split(",")]
    output_dir = Path(args.output_dir) if args.output_dir else (
        Path(_PROJECT_ROOT) / "verification_artifacts" / "realtime_tuning"
        / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Benchmark endpoint: {args.endpoint}")
    print(f"Text length: {len(args.text)} chars")
    print(f"Strides: {strides}")
    print(f"Codec context: {args.codec_context}")
    print(f"Output dir: {output_dir}")
    print()

    results = run_stride_sweep(
        endpoint=args.endpoint,
        text=args.text,
        strides=strides,
        codec_context=args.codec_context,
        holdback=args.holdback,
        start_buffer_ms=args.start_buffer_ms,
        low_latency=not args.no_low_latency,
        warmup_runs=args.warmup_runs,
        measured_runs=args.measured_runs,
        output_dir=output_dir,
        voice=args.voice,
        voice_dir=args.voice_dir,
        timeout=args.timeout,
    )

    # Save JSON
    json_path = output_dir / "results.json"
    json_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nJSON results: {json_path}")

    # Save Markdown summary
    md_path = output_dir / "summary.md"
    md_path.write_text(format_summary(results))
    print(f"Markdown summary: {md_path}")

    # Print summary
    print()
    print(format_summary(results))

    if args.json:
        print(json.dumps(results, indent=2, default=str))

    return 0


if __name__ == "__main__":
    sys.exit(main())

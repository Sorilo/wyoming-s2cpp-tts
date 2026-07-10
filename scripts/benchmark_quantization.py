#!/usr/bin/env python3
"""Phase 8D: Controlled quantization benchmark for S2 Pro GGUF models.

Benchmarks candidate GGUF models (Q6_K, Q5_K_M, Q4_K_M) against the
s2.cpp backend at FIXED stride 4 under identical conditions.  Produces
per-run PCM artifacts, JSON/Markdown summaries, and model provenance.

SAFETY: Defaults to dry-run mode.  Requires --run-real to contact a
real backend.  Contacts the backend DIRECTLY (bypasses Wyoming wrapper).

Usage:
    python scripts/benchmark_quantization.py --run-real \\
        --endpoint 127.0.0.1:3032 \\
        --models /models/s2-pro-q6_k.gguf,/models/s2-pro-q5_k_m.gguf
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.s2_client import (
    S2Client, S2Endpoint, S2GenerateRequest, S2StreamResult, S2ClientError,
)
from app.audio import PCM_WIDTH_BYTES, PCM_CHANNELS

DEFAULT_ENDPOINT = "127.0.0.1:3030"
DEFAULT_CODEC_CONTEXT = 4
DEFAULT_STRIDE = 4
DEFAULT_WARMUP_RUNS = 1
DEFAULT_MEASURED_RUNS = 3
DEFAULT_TIMEOUT = 120.0
DEFAULT_SAMPLE_RATE = 44100
DEFAULT_TEXT = (
    "The morning sun cast long shadows across the quiet neighborhood "
    "as residents began their daily routines. A gentle breeze carried "
    "the scent of fresh coffee from the corner cafe, where early "
    "customers sat reading newspapers and checking their phones. "
    "Children hurried past with backpacks slung over their shoulders, "
    "their laughter echoing off the brick buildings."
)


def pcm_duration_ms(pcm_bytes, sample_rate=DEFAULT_SAMPLE_RATE,
                    width=PCM_WIDTH_BYTES, channels=PCM_CHANNELS):
    frame_size = width * channels
    if frame_size <= 0 or sample_rate <= 0:
        return 0.0
    return (pcm_bytes // frame_size) / sample_rate * 1000.0


def real_time_factor(total_ms, audio_ms):
    if audio_ms <= 0:
        return float("inf")
    return total_ms / audio_ms


def _sanitize_label(label):
    if not label:
        return ""
    safe = re.sub(r'[^a-zA-Z0-9._-]', '_', label)
    safe = re.sub(r'\.{2,}', '.', safe)
    return safe.strip('._-')[:64]


def compute_sha256(filepath):
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class ModelInfo:
    path: str
    filename: str
    quant_label: str
    sha256: str = ""
    size_bytes: int = 0
    exists: bool = False

    @classmethod
    def from_path(cls, p):
        p = Path(p)
        filename = p.name
        quant_match = re.search(r'q([a-z0-9_]+)', filename)
        quant_label = quant_match.group(0).upper() if quant_match else "UNKNOWN"
        info = cls(path=str(p), filename=filename, quant_label=quant_label)
        if p.exists():
            info.exists = True
            info.size_bytes = p.stat().st_size
            info.sha256 = compute_sha256(p)
        return info


@dataclass
class QuantRunResult:
    model_filename: str
    quant_label: str
    model_sha256: str
    model_size_bytes: int
    stride: int
    run_index: int
    run_type: str
    time_to_headers_ms: float
    time_to_first_pcm_ms: float
    total_wall_ms: float
    pcm_bytes: int
    audio_duration_ms: float
    real_time_factor: float
    status: str
    error: str = ""
    response_headers: dict = field(default_factory=dict)
    pcm_saved_path: str = ""
    backend_metrics: dict | None = None


@dataclass
class QuantSummary:
    model: ModelInfo
    runs: list = field(default_factory=list)

    @property
    def success_runs(self):
        return [r for r in self.runs if r.status == "success" and r.run_type == "measured"]

    @property
    def all_success_runs(self):
        return [r for r in self.runs if r.status == "success"]

    @property
    def avg_rtf(self):
        ok = self.success_runs
        return sum(r.real_time_factor for r in ok) / len(ok) if ok else None

    @property
    def min_rtf(self):
        ok = self.success_runs
        return min(r.real_time_factor for r in ok) if ok else None

    @property
    def max_rtf(self):
        ok = self.success_runs
        return max(r.real_time_factor for r in ok) if ok else None

    @property
    def avg_first_pcm_ms(self):
        ok = self.success_runs
        return sum(r.time_to_first_pcm_ms for r in ok) / len(ok) if ok else None

    @property
    def avg_total_ms(self):
        ok = self.success_runs
        return sum(r.total_wall_ms for r in ok) / len(ok) if ok else None


def run_one_quant_benchmark(client, request, model_info, stride, run_index,
                             output_dir, run_label="", run_type="measured",
                             timeout=DEFAULT_TIMEOUT):
    start = time.monotonic()
    try:
        with client.generate_stream(request) as stream:
            headers_ms = (time.monotonic() - start) * 1000.0
            resp_headers = stream.response_headers
            first_pcm_ms = None
            pcm_parts = []
            for chunk in stream:
                if first_pcm_ms is None and chunk:
                    first_pcm_ms = (time.monotonic() - start) * 1000.0
                pcm_parts.append(chunk)
            if first_pcm_ms is None:
                first_pcm_ms = (time.monotonic() - start) * 1000.0
            pcm = b"".join(pcm_parts)
            total_ms = (time.monotonic() - start) * 1000.0
            duration_ms = pcm_duration_ms(len(pcm))
            rtf_val = real_time_factor(total_ms, duration_ms)
            safe_label = _sanitize_label(run_label)
            pcm_dir = output_dir / safe_label if safe_label else output_dir
            pcm_dir.mkdir(parents=True, exist_ok=True)
            pcm_name = f"{safe_label}.pcm" if safe_label else f"{model_info.quant_label}_run{run_index}.pcm"
            pcm_path = pcm_dir / pcm_name
            pcm_path.write_bytes(pcm)
            return QuantRunResult(
                model_filename=model_info.filename,
                quant_label=model_info.quant_label,
                model_sha256=model_info.sha256,
                model_size_bytes=model_info.size_bytes,
                stride=stride, run_index=run_index, run_type=run_type,
                time_to_headers_ms=headers_ms,
                time_to_first_pcm_ms=first_pcm_ms,
                total_wall_ms=total_ms, pcm_bytes=len(pcm),
                audio_duration_ms=duration_ms,
                real_time_factor=rtf_val, status="success",
                response_headers=resp_headers,
                pcm_saved_path=str(pcm_path),
            )
    except (S2ClientError, urllib.error.URLError, OSError) as exc:
        total_ms = (time.monotonic() - start) * 1000.0
        return QuantRunResult(
            model_filename=model_info.filename,
            quant_label=model_info.quant_label,
            model_sha256=model_info.sha256,
            model_size_bytes=model_info.size_bytes,
            stride=stride, run_index=run_index, run_type=run_type,
            time_to_headers_ms=0, time_to_first_pcm_ms=0,
            total_wall_ms=total_ms, pcm_bytes=0,
            audio_duration_ms=0, real_time_factor=float("inf"),
            status="error", error=str(exc),
        )


def run_quant_sweep(endpoint, text, models, stride, codec_context, holdback,
                     start_buffer_ms, low_latency, warmup_runs, measured_runs,
                     output_dir, voice="", voice_dir="", timeout=DEFAULT_TIMEOUT):
    host, port_str = endpoint.rsplit(":", 1)
    port = int(port_str)
    client = S2Client(S2Endpoint(host=host, port=port), timeout_seconds=timeout)
    model_infos = [ModelInfo.from_path(m) for m in models]
    missing = [m for m in model_infos if not m.exists]
    if missing:
        print("WARNING: Missing model files:")
        for m in missing:
            print(f"  - {m.path} ({m.quant_label})")
        print()
    summaries = []
    for m_info in model_infos:
        quant_tag = m_info.quant_label.lower().replace("_", "")
        request = S2GenerateRequest(
            text=text, voice=voice, model=m_info.path,
            stream=True, chunked=True, output_format="pcm_s16le",
            segment_sentences=False,
            codec_decode_context_frames=codec_context,
            low_latency=low_latency,
            stream_decode_stride_frames=stride,
            stream_holdback_frames=holdback,
            stream_start_buffer_ms=start_buffer_ms,
        )
        print(f"\n{'='*70}")
        print(f"Quant: {m_info.quant_label} ({m_info.filename})")
        if m_info.exists:
            print(f"  SHA-256: {m_info.sha256[:16]}...")
            print(f"  Size: {m_info.size_bytes:,} bytes ({m_info.size_bytes/1e9:.2f} GB)")
        else:
            print("  FILE NOT FOUND — skipping")
            continue
        print(f"{'='*70}")
        summary = QuantSummary(model=m_info)
        for i in range(warmup_runs):
            w_label = f"quant_{quant_tag}_warmup{i+1}"
            print(f"  Warm-up {i+1}/{warmup_runs}...", end=" ", flush=True)
            r = run_one_quant_benchmark(client, request, m_info, stride, i+1,
                                         output_dir, run_label=w_label,
                                         run_type="warmup", timeout=timeout)
            print(f"RTF={r.real_time_factor:.3f}" if r.status == "success" else f"ERROR: {r.error}")
        for i in range(measured_runs):
            m_label = f"quant_{quant_tag}_run{i+1}"
            print(f"  Run {i+1}/{measured_runs}...", end=" ", flush=True)
            r = run_one_quant_benchmark(client, request, m_info, stride, i+1,
                                         output_dir, run_label=m_label,
                                         run_type="measured", timeout=timeout)
            summary.runs.append(r)
            if r.status == "success":
                print(f"RTF={r.real_time_factor:.3f}, first_pcm={r.time_to_first_pcm_ms:.0f}ms, total={r.total_wall_ms:.0f}ms")
            else:
                print(f"ERROR: {r.error}")
        summaries.append(summary)
    return {
        "endpoint": endpoint, "text_len": len(text), "stride": stride,
        "codec_context": codec_context, "holdback": holdback,
        "start_buffer_ms": start_buffer_ms, "low_latency": low_latency,
        "sample_rate_hz": DEFAULT_SAMPLE_RATE,
        "warmup_runs": warmup_runs, "measured_runs": measured_runs,
        "candidate_models": [
            {"filename": m.filename, "quant_label": m.quant_label,
             "sha256": m.sha256, "size_bytes": m.size_bytes, "exists": m.exists}
            for m in model_infos
        ],
        "summaries": [
            {
                "quant": s.model.quant_label,
                "model_filename": s.model.filename,
                "model_sha256": s.model.sha256,
                "model_size_bytes": s.model.size_bytes,
                "avg_rtf": s.avg_rtf, "min_rtf": s.min_rtf,
                "max_rtf": s.max_rtf,
                "avg_first_pcm_ms": s.avg_first_pcm_ms,
                "avg_total_ms": s.avg_total_ms,
                "runs": [
                    {
                        "run": r.run_index, "run_type": r.run_type,
                        "status": r.status, "rtf": r.real_time_factor,
                        "time_to_headers_ms": r.time_to_headers_ms,
                        "time_to_first_pcm_ms": r.time_to_first_pcm_ms,
                        "total_wall_ms": r.total_wall_ms,
                        "pcm_bytes": r.pcm_bytes,
                        "audio_duration_ms": r.audio_duration_ms,
                        "error": r.error, "pcm_path": r.pcm_saved_path,
                        "backend_metrics": r.backend_metrics,
                    }
                    for r in s.runs
                ],
            }
            for s in summaries
        ],
    }


def format_quant_summary(results):
    lines = [
        "# S2 Pro Quantization Benchmark Results", "",
        f"- **Endpoint**: `{results['endpoint']}`",
        f"- **Text length**: {results['text_len']} chars",
        f"- **Stride**: {results['stride']} (fixed)",
        f"- **Codec context**: {results['codec_context']}",
        f"- **Holdback**: {results['holdback']}",
        f"- **Start buffer**: {results['start_buffer_ms']} ms",
        f"- **Low latency**: {results['low_latency']}",
        f"- **Sample rate**: {results['sample_rate_hz']} Hz (mono s16le)",
        "", "## Candidate Models", "",
        "| Quant | Filename | SHA-256 | Size (GB) | Exists |",
        "|-------|----------|---------|-----------|--------|",
    ]
    for m in results["candidate_models"]:
        sha_short = m["sha256"][:16] + "..." if m["sha256"] else "N/A"
        size_gb = f"{m['size_bytes']/1e9:.2f}" if m["size_bytes"] else "N/A"
        lines.append(f"| {m['quant_label']} | {m['filename']} | {sha_short} | {size_gb} | {'YES' if m['exists'] else 'NO'} |")
    lines += [
        "", "## Results by Quantization", "",
        "| Quant | Avg RTF | Min RTF | Max RTF | Avg First PCM (ms) | Avg Total (ms) | Success |",
        "|-------|---------|---------|---------|---------------------|----------------|---------|",
    ]
    for s in results["summaries"]:
        sc = sum(1 for r in s["runs"] if r["status"] == "success")
        a = f"{s['avg_rtf']:.3f}" if s['avg_rtf'] is not None else "N/A"
        mn = f"{s['min_rtf']:.3f}" if s['min_rtf'] is not None else "N/A"
        mx = f"{s['max_rtf']:.3f}" if s['max_rtf'] is not None else "N/A"
        b = f"{s['avg_first_pcm_ms']:.0f}" if s['avg_first_pcm_ms'] is not None else "N/A"
        c = f"{s['avg_total_ms']:.0f}" if s['avg_total_ms'] is not None else "N/A"
        lines.append(f"| {s['quant']} | {a} | {mn} | {mx} | {b} | {c} | {sc}/{len(s['runs'])} |")
    lines += [
        "", "## Recommendation", "",
        "**⚠️ Quality unverified**: Based on RTF and latency only.",
        "**Audio quality has not been assessed**. Listen to PCM files before selecting.", "",
    ]
    best_quant = None
    best_rtf = float("inf")
    for s in results["summaries"]:
        if s["avg_rtf"] is not None and s["avg_rtf"] < best_rtf:
            best_rtf = s["avg_rtf"]
            best_quant = s
    if best_quant:
        lines.append(f"- **Recommended quant**: {best_quant['quant']} (RTF={best_rtf:.3f})")
        if best_rtf < 0.95:
            lines.append("- **Status**: Safe real-time with operating margin ✅")
        elif best_rtf < 1.0:
            lines.append("- **Status**: Real-time achievable ⚠️ (tight margin)")
        else:
            lines.append("- **Status**: Slower than real time ❌ — consider Phase 8E")
        lines.append("")
        lines.append("### Suggested backend environment")
        lines.append("```bash")
        lines.append(f"# Model: {best_quant['model_filename']}")
        lines.append(f"S2_STREAM_DECODE_STRIDE_FRAMES={results['stride']}")
        lines.append("```")
    lines += [
        "", "## Listening Checklist", "",
        "- [ ] Clicks / pops", "- [ ] Missing or repeated syllables",
        "- [ ] Word stretching or unnatural pacing",
        "- [ ] Robotic or metallic artifacts",
        "- [ ] Voice consistency across runs", "- [ ] Natural prosody and intonation",
        "- [ ] Appropriate pauses", "- [ ] Clipped word endings",
        "- [ ] Overall preference ranking", "",
        "## PCM Artifacts", "",
        "Convert PCM to WAV for listening:",
        "```bash",
        "ffmpeg -f s16le -ar 44100 -ac 1 -i <file>.pcm <file>.wav",
        "# ffmpeg available at /usr/bin/ffmpeg on Hermes Suite",
        "```",
    ]
    return "\n".join(lines)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Controlled quant benchmark for S2 Pro GGUF")
    p.add_argument("--run-real", action="store_true")
    p.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    p.add_argument("--text", default=DEFAULT_TEXT)
    p.add_argument("--models", default="",
                   help="Comma-separated model paths")
    p.add_argument("--stride", type=int, default=DEFAULT_STRIDE)
    p.add_argument("--codec-context", type=int, default=DEFAULT_CODEC_CONTEXT)
    p.add_argument("--holdback", type=int, default=0)
    p.add_argument("--start-buffer-ms", type=int, default=0)
    p.add_argument("--no-low-latency", action="store_true")
    p.add_argument("--warmup-runs", type=int, default=DEFAULT_WARMUP_RUNS)
    p.add_argument("--measured-runs", type=int, default=DEFAULT_MEASURED_RUNS)
    p.add_argument("--output-dir", default=None)
    p.add_argument("--voice", default="")
    p.add_argument("--voice-dir", default="")
    p.add_argument("--candidate-dir", default="",
                   help="Per-candidate subdirectory name (e.g. q6_k, q5_k_m)")
    p.add_argument("--expected-model-file", default="",
                   help="Expected model filename in startup logs (for verification)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    models = [m.strip() for m in args.models.split(",")] if args.models else ["/models/s2-pro-q6_k.gguf"]

    if not args.run_real:
        print("DRY RUN — no backend will be contacted.")
        print(f"Would benchmark endpoint: {args.endpoint}")
        print(f"Stride: {args.stride}")
        print("Models:")
        for m in models:
            mi = ModelInfo.from_path(m)
            print(f"  - {mi.filename} ({mi.quant_label}): {'EXISTS' if mi.exists else 'MISSING'}")
        print(f"Warm-up: {args.warmup_runs}, Measured: {args.measured_runs}")
        print("\nAdd --run-real to execute against a live backend.")
        return 0

    # Live mode: model selection happens at backend startup via S2_MODEL env var.
    # The HTTP request cannot switch models — each backend process loads exactly
    # one GGUF file.  Accept exactly one model path for provenance recording.
    if len(models) != 1:
        print("ERROR: --run-real requires exactly one model path per invocation.", file=sys.stderr)
        print("       The s2.cpp backend loads ONE GGUF at startup (S2_MODEL env var).", file=sys.stderr)
        print("       Use the shell orchestrator to restart the backend per candidate.", file=sys.stderr)
        print(f"       Received {len(models)} model paths: {models}", file=sys.stderr)
        print("", file=sys.stderr)
        print("       Example (one invocation per candidate):", file=sys.stderr)
        print("         python3 scripts/benchmark_quantization.py --run-real \\", file=sys.stderr)
        print("           --models /models/s2-pro-q5_k_m.gguf --endpoint 127.0.0.1:3033", file=sys.stderr)
        return 1

    output_dir = Path(args.output_dir) if args.output_dir else (
        Path(_PROJECT_ROOT) / "verification_artifacts" / "quant_benchmark"
        / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    )
    # Nest into candidate-dir if provided (shell orchestrator per-model layout)
    if getattr(args, 'candidate_dir', '') or getattr(args, 'candidate-dir', ''):
        cd = getattr(args, 'candidate_dir', '') or getattr(args, 'candidate-dir', '')
        output_dir = output_dir / cd
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Quant Benchmark: endpoint={args.endpoint}, stride={args.stride}, models={models}")
    print(f"Output: {output_dir}\n")

    results = run_quant_sweep(
        endpoint=args.endpoint, text=args.text, models=models,
        stride=args.stride, codec_context=args.codec_context,
        holdback=args.holdback, start_buffer_ms=args.start_buffer_ms,
        low_latency=not args.no_low_latency,
        warmup_runs=args.warmup_runs, measured_runs=args.measured_runs,
        output_dir=output_dir, voice=args.voice, voice_dir=args.voice_dir,
        timeout=args.timeout,
    )

    json_path = output_dir / "results.json"
    json_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nJSON: {json_path}")

    md_path = output_dir / "summary.md"
    summary_text = format_quant_summary(results)
    md_path.write_text(summary_text)
    print(f"Summary: {md_path}\n")
    print(summary_text)

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

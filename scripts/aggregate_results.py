#!/usr/bin/env python3
"""Aggregate per-run benchmark results into canonical results.json and summary.md.

Filters by explicit run_type field — only measured runs in averages.
Validates PCM paths exist. Merges backend metrics from per_run_metrics.json.
"""

import json, sys, hashlib, os
from pathlib import Path
from typing import Any


def load_backend_metrics(artifact_dir: Path) -> dict[str, dict]:
    """Load per_run_metrics.json and index by run_label."""
    pm = artifact_dir / "per_run_metrics.json"
    if not pm.exists():
        return {}
    try:
        raw = json.loads(pm.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    indexed = {}
    for entry in raw:
        label = entry.get("run_label", "")
        if label:
            indexed[label] = {
                "raw": entry.get("raw", None),
                "generate": entry.get("generate", None),
                "stream_decode": entry.get("stream_decode", None),
                "stream_batches": entry.get("stream_batches", None),
                "ar_only": entry.get("ar_only", None),
                "total": entry.get("total", None),
                "total_rtf": entry.get("total_rtf", None),
            }
    return indexed


def aggregate_results(artifact_dir: Path) -> dict[str, Any]:
    """Collect per-run results, filter by run_type, produce aggregate."""
    metrics_index = load_backend_metrics(artifact_dir)
    all_measured = []
    all_warmups = []
    stride_map: dict[int, list[dict]] = {}
    text, endpoint, codec_context, holdback, start_buffer, low_latency = "", "", 0, 0, 0, True
    model, voice, temperature, top_p, top_k, max_tokens = "", "", 0.58, 0.88, 40, 512

    for subdir in sorted(artifact_dir.iterdir()):
        if not subdir.is_dir():
            continue
        rj = subdir / "results.json"
        if not rj.exists():
            continue
        try:
            data = json.loads(rj.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        for s in data.get("summaries", []):
            stride = s["stride"]
            for run in s.get("runs", []):
                run_type = run.get("run_type", "measured")
                run_label = run.get("run_label", subdir.name)

                # Build enriched run entry
                entry = {
                    "run_label": run_label,
                    "run_type": run_type,
                    "stride": stride,
                    "run_index": run.get("run", 0),
                    "model_file": run.get("model_file", ""),
                    "model_sha256": run.get("model_sha256", ""),
                    "model_size_bytes": run.get("model_size_bytes", 0),
                    "status": run.get("status", "error"),
                    "time_to_headers_ms": run.get("time_to_headers_ms", 0),
                    "time_to_first_pcm_ms": run.get("time_to_first_pcm_ms", 0),
                    "total_wall_ms": run.get("total_wall_ms", 0),
                    "pcm_bytes": run.get("pcm_bytes", 0),
                    "audio_duration_ms": run.get("audio_duration_ms", 0),
                    "rtf": run.get("rtf", None),
                    "error": run.get("error", ""),
                    "pcm_path": run.get("pcm_path", str(subdir / f"{run_label}.pcm")),
                    "backend_metrics": metrics_index.get(run_label, None),
                }

                # Validate PCM path
                pcm_p = Path(entry["pcm_path"])
                if entry["status"] == "success" and not pcm_p.exists():
                    # Try relative to artifact_dir/run_label/
                    alt = subdir / f"{run_label}.pcm"
                    if alt.exists():
                        entry["pcm_path"] = str(alt)
                    else:
                        entry["pcm_path"] += " (MISSING)"

                if run_type == "warmup":
                    all_warmups.append(entry)
                else:
                    all_measured.append(entry)
                    if stride not in stride_map:
                        stride_map[stride] = []
                    stride_map[stride].append(entry)

        # Collect metadata from first result
        text = data.get("text", text)
        endpoint = data.get("endpoint", endpoint)
        codec_context = data.get("codec_context", codec_context)
        holdback = data.get("holdback", holdback)
        start_buffer = data.get("start_buffer_ms", start_buffer)
        low_latency = data.get("low_latency", low_latency)

    # Build stride summaries (measured only)
    summaries = []
    for stride in sorted(stride_map.keys()):
        runs = stride_map[stride]
        success_runs = [r for r in runs if r["status"] == "success"]
        avg_rtf = sum(r["rtf"] for r in success_runs if r["rtf"] is not None) / len(success_runs) if success_runs else None
        avg_first = sum(r["time_to_first_pcm_ms"] for r in success_runs) / len(success_runs) if success_runs else None
        avg_total = sum(r["total_wall_ms"] for r in success_runs) / len(success_runs) if success_runs else None
        summaries.append({
            "stride": stride, "avg_rtf": avg_rtf, "avg_first_pcm_ms": avg_first,
            "avg_total_ms": avg_total, "runs": runs,
        })

    return {
        "endpoint": endpoint,
        "text_len": len(text),
        "text_sha256": hashlib.sha256(text.encode()).hexdigest()[:16] if text else "N/A",
        "codec_context": codec_context, "holdback": holdback,
        "start_buffer_ms": start_buffer, "low_latency": low_latency,
        "sample_rate_hz": 44100,
        "strides": sorted(stride_map.keys()),
        "total_warmup_runs": len(all_warmups),
        "total_measured_runs": len(all_measured),
        "warmups": all_warmups,
        "summaries": summaries,
    }


def format_summary(results):
    lines = ["# Real-Time Stride Tuning Benchmark Results", "",
             f"- **Endpoint**: `{results.get('endpoint','N/A')}`",
             f"- **Text hash**: `{results.get('text_sha256','N/A')}`",
             f"- **Codec context**: {results.get('codec_context','N/A')}",
             f"- **Warmup runs**: {results.get('total_warmup_runs',0)}",
             f"- **Measured runs**: {results.get('total_measured_runs',0)}",
             "", "## Results by Stride", "",
             "| Stride | Avg RTF | Avg First PCM (ms) | Avg Total (ms) | Success |",
             "|--------|---------|---------------------|----------------|---------|"]
    for s in results.get("summaries", []):
        sc = sum(1 for r in s["runs"] if r["status"]=="success")
        a = f"{s['avg_rtf']:.2f}" if s.get('avg_rtf') is not None else "N/A"
        b = f"{s['avg_first_pcm_ms']:.0f}" if s.get('avg_first_pcm_ms') is not None else "N/A"
        c = f"{s['avg_total_ms']:.0f}" if s.get('avg_total_ms') is not None else "N/A"
        lines.append(f"| {s['stride']} | {a} | {b} | {c} | {sc}/{len(s['runs'])} |")
    lines += ["", "**Warning: Quality unverified. Listen to PCM before applying.**", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    d = Path(sys.argv[1])
    r = aggregate_results(d)
    (d/"results.json").write_text(json.dumps(r, indent=2, default=str))
    (d/"summary.md").write_text(format_summary(r))
    print(f"Aggregated {r['total_measured_runs']} measured + {r['total_warmup_runs']} warmup runs across {r['strides']} strides")

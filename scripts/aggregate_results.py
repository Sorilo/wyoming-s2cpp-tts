#!/usr/bin/env python3
"""Aggregate per-run benchmark results into canonical results.json and summary.md."""
import json, sys, hashlib
from pathlib import Path
from typing import Any

def aggregate_results(artifact_dir: Path) -> dict[str, Any]:
    all_runs = []
    stride_map: dict[int, list[dict]] = {}
    text = ""; endpoint = ""; codec_context = 0; holdback = 0; start_buffer = 0; low_latency = True

    for subdir in sorted(artifact_dir.iterdir()):
        if not subdir.is_dir(): continue
        rj = subdir / "results.json"
        if not rj.exists(): continue
        try:
            data = json.loads(rj.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for s in data.get("summaries", []):
            stride = s["stride"]
            if stride not in stride_map: stride_map[stride] = []
            for run in s.get("runs", []):
                run["run_label"] = subdir.name
                run["pcm_path"] = str(subdir / f"{subdir.name}.pcm")
                stride_map[stride].append(run); all_runs.append(run)
        text = data.get("text", text)
        endpoint = data.get("endpoint", endpoint)
        codec_context = data.get("codec_context", codec_context)
        holdback = data.get("holdback", holdback)
        start_buffer = data.get("start_buffer_ms", start_buffer)
        low_latency = data.get("low_latency", low_latency)

    summaries = []
    for stride in sorted(stride_map.keys()):
        runs = stride_map[stride]
        success = [r for r in runs if r["status"] == "success"]
        avg_rtf = sum(r["rtf"] for r in success) / len(success) if success else None
        avg_first = sum(r["time_to_first_pcm_ms"] for r in success) / len(success) if success else None
        avg_total = sum(r["total_wall_ms"] for r in success) / len(success) if success else None
        summaries.append({"stride": stride, "avg_rtf": avg_rtf, "avg_first_pcm_ms": avg_first, "avg_total_ms": avg_total, "runs": runs})

    return {"endpoint": endpoint, "text_len": len(text), "text_sha256": hashlib.sha256(text.encode()).hexdigest()[:16] if text else "N/A",
            "codec_context": codec_context, "holdback": holdback, "start_buffer_ms": start_buffer, "low_latency": low_latency,
            "sample_rate_hz": 44100, "strides": sorted(stride_map.keys()), "total_measured_runs": len(all_runs), "summaries": summaries}

def format_summary(results): 
    lines = ["# Real-Time Stride Tuning Benchmark Results", "",
             f"- **Endpoint**: `{results.get('endpoint','N/A')}`", f"- **Text hash**: `{results.get('text_sha256','N/A')}`",
             f"- **Codec context**: {results.get('codec_context','N/A')}", f"- **Total measured runs**: {results.get('total_measured_runs',0)}",
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
    print(f"Aggregated {r['total_measured_runs']} runs across {r['strides']} strides")

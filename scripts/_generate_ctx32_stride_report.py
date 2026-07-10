#!/usr/bin/env python3
"""Generate context-32 stride sweep comparison report."""

import json, sys
from pathlib import Path


def load(artifact_dir, label):
    rj = artifact_dir / label / "results.json"
    if not rj.exists(): return None
    with open(rj) as f:
        data = json.load(f)
    runs = []
    for s in data.get("summaries", []):
        for r in s.get("runs", []):
            if r.get("status") == "success" and r.get("run_type") == "measured":
                runs.append(r)
    if not runs: return None
    rtfs = sorted(r["rtf"] for r in runs if r.get("rtf") is not None)
    firsts = sorted(r["time_to_first_pcm_ms"] for r in runs)
    totals = sorted(r["total_wall_ms"] for r in runs)
    return {
        "label": label, "success": len(runs),
        "rtf_mean": sum(rtfs) / len(rtfs) if rtfs else None,
        "rtf_min": rtfs[0] if rtfs else None,
        "rtf_max": rtfs[-1] if rtfs else None,
        "first_pcm_mean": sum(firsts) / len(firsts) if firsts else None,
        "total_wall_mean": sum(totals) / len(totals) if totals else None,
    }


def main():
    artifact_dir = Path(sys.argv[1])
    results = {}
    strides = [4, 8, 12, 16, 24, 32]
    for stride in strides:
        label = f"ctx32_stride_{stride}"
        r = load(artifact_dir, label)
        if r: results[str(stride)] = r

    with open(artifact_dir / "ctx32_stride_comparison.json", "w") as f:
        json.dump({
            "strides": results,
            "config": {"context": 32, "threads": 8, "model": "Q4_K_M"},
            "goal": "smallest stride with context-32 quality and RTF < 1.0"
        }, f, indent=2)

    lines = [
        "# Context-32 Stride Sweep",
        "",
        "**Goal**: smallest stride providing acceptable context-32 audio and real-time throughput.",
        "Fixed: Q4_K_M, threads=8, context=32, holdback=0, low_latency=true.",
        "",
        "## Results",
        "",
        "| Stride | RTF Mean | RTF Range | 1st PCM (ms) | Total (ms) | Status |",
        "|--------|----------|-----------|--------------|------------|--------|",
    ]
    for stride in strides:
        r = results.get(str(stride))
        if not r:
            lines.append(f"| {stride} | — | — | — | — | — |")
            continue
        def f(v, fmt=".2f"): return f"{v:{fmt}}" if v is not None else "—"
        status = "✅ RTF < 1.0" if r["rtf_mean"] and r["rtf_mean"] < 1.0 else "⚠️ RTF ≥ 1.0"
        lines.append(
            f"| {stride} | {f(r['rtf_mean'], '.3f')} | "
            f"{f(r['rtf_min'], '.3f')}–{f(r['rtf_max'], '.3f')} | "
            f"{f(r['first_pcm_mean'], '.0f')} | {f(r['total_wall_mean'], '.0f')} | {status} |"
        )

    lines += [
        "",
        "## Decision",
        "",
        "1. Prefer smallest stride with RTF < 1.0",
        "2. If no stride achieves RTF < 1.0, prefer RTF < 1.0 from thread/affinity sweep",
        "3. Do NOT automatically select the largest stride",
        "",
        "## Listening Files",
        f"WAV per stride: `{artifact_dir}/ctx32_stride_*/quant_q4k_run1/*.wav`",
    ]

    with open(artifact_dir / "ctx32_stride_summary.md", "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Stride comparison: {artifact_dir}/ctx32_stride_summary.md")


if __name__ == "__main__":
    main()

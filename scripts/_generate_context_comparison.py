#!/usr/bin/env python3
"""Generate context screening comparison report for Q4_K_M."""

import json, sys
from pathlib import Path


def median(lst):
    n = len(lst)
    if n == 0: return None
    m = n // 2
    return (lst[m] + lst[~m]) / 2 if n % 2 == 0 else lst[m]


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
        "rtf_median": median(rtfs),
        "rtf_min": rtfs[0] if rtfs else None,
        "rtf_max": rtfs[-1] if rtfs else None,
        "first_pcm_mean": sum(firsts) / len(firsts) if firsts else None,
        "total_wall_mean": sum(totals) / len(totals) if totals else None,
    }


def main():
    artifact_dir = Path(sys.argv[1])
    results = {}
    contexts = [4, 8, 12, 16, 24, 32, 48, 64]
    for ctx in contexts:
        label = f"context_{ctx}"
        r = load(artifact_dir, label)
        if r:
            results[str(ctx)] = r

    with open(artifact_dir / "context_comparison.json", "w") as f:
        json.dump({"contexts": results, "goal": "smallest context without audible tapping"}, f, indent=2)

    lines = [
        "# Q4_K_M Codec Context Screening",
        "",
        "**Goal**: find the smallest codec context without audible tapping/blipping.",
        "Fixed: Q4_K_M, threads=8, stride=4, holdback=0, low_latency=true.",
        "",
        "## Results",
        "",
        "| Context | RTF Mean | RTF Med | RTF Range | 1st PCM (ms) | Total (ms) |",
        "|---------|----------|---------|-----------|--------------|------------|",
    ]
    for ctx in contexts:
        r = results.get(str(ctx))
        if not r:
            lines.append(f"| {ctx} | — | — | — | — | — |")
            continue
        def f(v, fmt=".2f"): return f"{v:{fmt}}" if v is not None else "—"
        lines.append(
            f"| {ctx} | {f(r['rtf_mean'], '.3f')} | {f(r['rtf_median'], '.3f')} | "
            f"{f(r['rtf_min'], '.3f')}–{f(r['rtf_max'], '.3f')} | "
            f"{f(r['first_pcm_mean'], '.0f')} | {f(r['total_wall_mean'], '.0f')} |"
        )

    lines += [
        "",
        "## Listening Guidance",
        "",
        "For each context, listen to the WAV file and assess:",
        "- Tapping/blipping at word boundaries",
        "- Overall voice quality",
        "- Artifacts not present at context 4",
        "",
        "**Select the smallest context that eliminates audible tapping.**",
        "Context 64 is for reference — do NOT automatically select it.",
    ]

    with open(artifact_dir / "context_summary.md", "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Context comparison: {artifact_dir}/context_summary.md")


if __name__ == "__main__":
    main()

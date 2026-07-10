#!/usr/bin/env python3
"""Generate combined quantization benchmark summary from per-candidate results.

Usage: python3 _generate_combined_summary.py <artifact_dir> <summary_md>
"""

import json
import sys
from pathlib import Path


def median(lst):
    n = len(lst)
    if n == 0:
        return None
    m = n // 2
    return (lst[m] + lst[~m]) / 2 if n % 2 == 0 else lst[m]


def avg(lst):
    return sum(lst) / len(lst) if lst else None


def load_candidate_results(artifact_dir, label):
    rj = artifact_dir / label / "results.json"
    if not rj.exists():
        return None
    with open(rj) as f:
        data = json.load(f)

    runs = []
    backend_metrics_list = []
    for s in data.get("summaries", []):
        for run in s.get("runs", []):
            if run.get("status") == "success" and run.get("run_type") == "measured":
                runs.append(run)
                bm = run.get("backend_metrics")
                if bm and isinstance(bm, dict):
                    backend_metrics_list.append(bm)

    if not runs:
        return None

    rtfs = sorted(r.get("rtf") for r in runs if r.get("rtf") is not None)
    firsts = sorted(r.get("time_to_first_pcm_ms") for r in runs)
    totals = sorted(r.get("total_wall_ms") for r in runs)

    # Average backend metrics across all measured runs
    avg_bm = {}
    if backend_metrics_list:
        keys = ["generate", "stream_decode", "stream_batches", "ar_only",
                "total", "total_rtf", "kv_init", "ref_encode", "max_rss",
                "frames", "audio_s"]
        for k in keys:
            vals = [bm[k] for bm in backend_metrics_list if k in bm and bm[k] is not None]
            if vals:
                avg_bm[k + "_mean"] = sum(vals) / len(vals)

    # Model provenance
    sha_file = artifact_dir / label / "model_sha256.txt"
    size_file = artifact_dir / label / "model_size.txt"
    model_sha = sha_file.read_text().strip() if sha_file.exists() else ""
    model_size = size_file.read_text().strip() if size_file.exists() else ""

    return {
        "label": label,
        "success": len(runs),
        "rtf_mean": avg(rtfs),
        "rtf_median": median(rtfs),
        "rtf_min": rtfs[0] if rtfs else None,
        "rtf_max": rtfs[-1] if rtfs else None,
        "first_pcm_mean": avg(firsts),
        "first_pcm_median": median(firsts),
        "total_wall_mean": avg(totals),
        "model_sha": model_sha,
        "model_size": model_size,
        "backend_metrics_available": len(backend_metrics_list) > 0,
        "backend_metrics": avg_bm,
    }


def main():
    artifact_dir = Path(sys.argv[1])
    summary_md = sys.argv[2]

    results = {}
    for label in ["q6_k", "q5_k_m", "q4_k_m"]:
        r = load_candidate_results(artifact_dir, label)
        if r:
            results[label] = r

    # Write combined JSON
    combined_json = artifact_dir / "combined_results.json"
    with open(combined_json, "w") as f:
        json.dump(results, f, indent=2, default=str)

    # Build Markdown summary
    lines = [
        "# Quantization Benchmark Results",
        "",
        "**Status: Provisional — human listening required before model selection.**",
        "",
        "## Comparison Table",
        "",
        "| Quant | Success | RTF Mean | RTF Med | RTF Min | RTF Max | "
        "1st PCM Mean (ms) | Total Mean (ms) | Gen (s) | SD (s) | AR (s) | "
        "KV (s) | VRAM (MiB) |",
        "|-------|---------|----------|---------|---------|---------|"
        "--------------------|-----------------|---------|--------|--------|"
        "--------|-------------|",
    ]

    for label in ["q6_k", "q5_k_m", "q4_k_m"]:
        r = results.get(label)
        if not r:
            lines.append(f"| {label} | — | — | — | — | — | — | — | — | — | — | — | — |")
            continue

        def fmt(v, f=".2f"):
            return f"{v:{f}}" if v is not None else "—"

        bm = r.get("backend_metrics", {})
        lines.append(
            f"| {label} | {r['success']}/3 | {fmt(r['rtf_mean'], '.3f')} | "
            f"{fmt(r['rtf_median'], '.3f')} | {fmt(r['rtf_min'], '.3f')} | "
            f"{fmt(r['rtf_max'], '.3f')} | {fmt(r['first_pcm_mean'], '.0f')} | "
            f"{fmt(r['total_wall_mean'], '.0f')} | "
            f"{fmt(bm.get('generate_mean'))} | {fmt(bm.get('stream_decode_mean'))} | "
            f"{fmt(bm.get('ar_only_mean'))} | {fmt(bm.get('kv_init_mean'))} | "
            f"{fmt(bm.get('max_rss_mean'), '.0f')} |"
        )

    lines += [
        "",
        "## Recommendation",
        "",
        "⚠️ **PROVISIONAL**: Based on RTF and latency metrics only.",
        "**Human listening is REQUIRED before model selection.**",
        "",
        "### Decision Rule",
        "",
        "- RTF ≤ 0.95: safe real-time with margin ✅",
        "- 0.95 < RTF < 1.0: real-time achievable, tight margin ⚠️",
        "- RTF ≥ 1.0: slower than real-time ❌",
        "",
        "### First Live Benchmark Results (2026-07-10)",
        "",
    ]

    for label in ["q6_k", "q5_k_m", "q4_k_m"]:
        r = results.get(label)
        if r:
            lines.append(
                f"- **{label}**: RTF {fmt(r['rtf_mean'], '.3f')}, "
                f"first PCM {fmt(r['first_pcm_mean'], '.0f')} ms, "
                f"{r['success']}/3 measured"
            )

    lines += [
        "",
        "### Model SHA-256",
        "",
    ]
    for label in ["q6_k", "q5_k_m", "q4_k_m"]:
        r = results.get(label)
        if r and r["model_sha"]:
            lines.append(f"- **{label}**: `{r['model_sha']}`")

    with open(summary_md, "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Combined summary: {summary_md}")
    print(f"Combined JSON: {combined_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

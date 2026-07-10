#!/usr/bin/env python3
"""Generate combined Q4 runtime tuning report from per-configuration results."""

import json, sys
from pathlib import Path


def median(lst):
    n = len(lst)
    if n == 0: return None
    m = n // 2
    return (lst[m] + lst[~m]) / 2 if n % 2 == 0 else lst[m]


def avg(lst): return sum(lst) / len(lst) if lst else None


def load_config(artifact_dir, label):
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
    return {
        "label": label, "success": len(runs),
        "rtf_mean": avg(rtfs), "rtf_median": median(rtfs),
        "rtf_min": rtfs[0] if rtfs else None, "rtf_max": rtfs[-1] if rtfs else None,
        "first_pcm_mean": avg(firsts),
    }


def main():
    artifact_dir = Path(sys.argv[1])
    sections = {"threads": [], "affinity": [], "blipping": []}
    for subdir in sorted(artifact_dir.iterdir()):
        if not subdir.is_dir(): continue
        name = subdir.name
        cfg = load_config(artifact_dir, name)
        if not cfg: continue
        if name.startswith("threads_"): sections["threads"].append(cfg)
        elif name.startswith("affinity_"): sections["affinity"].append(cfg)
        elif name.startswith("blip_"): sections["blipping"].append(cfg)

    all_configs = sections["threads"] + sections["affinity"] + sections["blipping"]
    with open(artifact_dir / "combined_results.json", "w") as f:
        json.dump({"sections": {k: v for k, v in sections.items() if v}}, f, indent=2)

    lines = ["# Q4 Runtime Tuning Results", "", "**Q4_K_M, stride 4 fixed**", ""]
    for sec_name, sec_label in [("threads", "Thread Sweep"), ("affinity", "Affinity Sweep"), ("blipping", "Blipping Diagnostic")]:
        cfgs = sections[sec_name]
        if not cfgs: continue
        lines += [f"## {sec_label}", "",
                  "| Config | Success | RTF Mean | RTF Med | RTF Min | RTF Max | 1st PCM (ms) |",
                  "|--------|---------|----------|---------|---------|---------|--------------|"]
        for c in cfgs:
            def f(v, fmt=".2f"): return f"{v:{fmt}}" if v is not None else "—"
            lines.append(f"| {c['label']} | {c['success']}/3 | {f(c['rtf_mean'], '.3f')} | "
                         f"{f(c['rtf_median'], '.3f')} | {f(c['rtf_min'], '.3f')} | "
                         f"{f(c['rtf_max'], '.3f')} | {f(c['first_pcm_mean'], '.0f')} |")
        lines.append("")

    lines += ["## Recommendation", "",
              "⚠️ **PROVISIONAL** — human listening of blipping WAVs required.", "",
              "- Speed winner: lowest RTF configuration from thread/affinity sweeps",
              "- Quality: evaluate blipping diagnostic WAVs before final selection",
              "- Do NOT promote automatically"]

    with open(artifact_dir / "summary.md", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Combined report: {artifact_dir}/summary.md")


if __name__ == "__main__":
    main()

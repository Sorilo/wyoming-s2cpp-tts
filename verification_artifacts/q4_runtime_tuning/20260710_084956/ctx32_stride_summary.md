# Context-32 Stride Sweep

**Goal**: smallest stride providing acceptable context-32 audio and real-time throughput.
Fixed: Q4_K_M, threads=8, context=32, holdback=0, low_latency=true.

## Results

| Stride | RTF Mean | RTF Range | 1st PCM (ms) | Total (ms) | Status |
|--------|----------|-----------|--------------|------------|--------|
| 4 | 1.371 | 1.371–1.371 | 256 | 30819 | ⚠️ RTF ≥ 1.0 |
| 8 | 1.153 | 1.153–1.153 | 398 | 25326 | ⚠️ RTF ≥ 1.0 |
| 12 | 1.083 | 1.083–1.083 | 566 | 23732 | ⚠️ RTF ≥ 1.0 |
| 16 | 1.065 | 1.065–1.065 | 816 | 24080 | ⚠️ RTF ≥ 1.0 |
| 24 | 1.015 | 1.015–1.015 | 1047 | 23291 | ⚠️ RTF ≥ 1.0 |
| 32 | 0.987 | 0.987–0.987 | 1355 | 21917 | ✅ RTF < 1.0 |

## Decision

1. Prefer smallest stride with RTF < 1.0
2. If no stride achieves RTF < 1.0, prefer RTF < 1.0 from thread/affinity sweep
3. Do NOT automatically select the largest stride

## Listening Files
WAV per stride: `/mnt/user/appdata/hermes-agent/webui-workspace/wyoming-s2cpp-tts/verification_artifacts/q4_runtime_tuning/20260710_084956/ctx32_stride_*/quant_q4k_run1/*.wav`

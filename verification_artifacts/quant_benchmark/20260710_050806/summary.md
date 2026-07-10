# Quantization Benchmark Results

**Status: Provisional — human listening required before model selection.**

## Comparison Table

| Quant | Success | RTF Mean | RTF Med | RTF Min | RTF Max | 1st PCM Mean (ms) | Total Mean (ms) | Gen (s) | SD (s) | AR (s) | KV (s) | VRAM (MiB) |
|-------|---------|----------|---------|---------|---------|--------------------|-----------------|---------|--------|--------|--------|-------------|
| q6_k | 3/3 | 1.121 | 1.119 | 1.117 | 1.126 | 247 | 23439 | — | — | — | — | — |
| q5_k_m | 3/3 | 1.072 | 1.075 | 1.066 | 1.076 | 234 | 23755 | — | — | — | — | — |
| q4_k_m | 3/3 | 1.015 | 1.015 | 1.013 | 1.016 | 219 | 22305 | — | — | — | — | — |

## Recommendation

⚠️ **PROVISIONAL**: Based on RTF and latency metrics only.
**Human listening is REQUIRED before model selection.**

### Decision Rule

- RTF ≤ 0.95: safe real-time with margin ✅
- 0.95 < RTF < 1.0: real-time achievable, tight margin ⚠️
- RTF ≥ 1.0: slower than real-time ❌

### First Live Benchmark Results (2026-07-10)

- **q6_k**: RTF 1.121, first PCM 247 ms, 3/3 measured
- **q5_k_m**: RTF 1.072, first PCM 234 ms, 3/3 measured
- **q4_k_m**: RTF 1.015, first PCM 219 ms, 3/3 measured

### Model SHA-256

- **q6_k**: `84ac904172a2cadb84e8f7f14ea3f1acef0584987635e85f7207fd254eafa235`
- **q5_k_m**: `e445b0c8f32ed0ff584b906098f0fe53a67c0691249bfcccde569544f7d72cb9`
- **q4_k_m**: `83963e1b7cec980b41eb2163d617e2b6241bfd1564dd880e5b43fc4834807bd9`

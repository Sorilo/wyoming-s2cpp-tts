# Performance Tuning Results

> **Phase 8C–8E.1** | Live RTX 3080 benchmarks | Q4_K_M selected

## Executive Summary

The s2.cpp TTS backend on RTX 3080 was tuned through six controlled benchmark phases. Starting from Q6_K at stride 4 (RTF 1.121), we reduced to Q4_K_M (RTF 1.015), optimized threads to 8 (RTF 0.954 at context 4), and established context 32 as the quality floor. At context 32, only stride 32 achieves RTF below 1.0 (0.987), with backend first PCM ~1.35s. The provisional baseline is **Q4_K_M, threads=8, context=32, stride=32, P-cores 0-15**. Tuning is paused for end-to-end Home Assistant validation.

## Original Problem

Q6_K at stride 1 had RTF ~1.34 — 34% slower than real-time playback, causing underrun on long responses. The goal was RTF < 1.0 (preferably ≤ 0.95) while preserving audio quality.

## Hardware / Software

| Component | Value |
|---|---|
| GPU | NVIDIA RTX 3080 (GPU-65b9a886) |
| CPU | Intel i9-13900K (8P+16E, 32 logical) |
| Backend image | `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd` |
| Backend digest | `sha256:c29e41e59b470d58bf4b88c11c9ec753e00fa74a3bffbb003bc257fb9c6e46d9` |
| s2.cpp revision | `edf89bd7` |
| Wrapper (production) | `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-9c134cc` |
| Wrapper (closure) | `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-<commit>` (published by this phase) |
| Audio | 44100 Hz mono s16le PCM |
| Benchmark text | 361 chars (standard neighborhood passage) |
| Voice | `cmu_bdl_male_us` (.s2voice profile) |

## Artifact Inventory

| Artifact | Contents |
|---|---|
| `verification_artifacts/realtime_tuning/20260710_021915/` | Stride sweep 1–8 (Q6_K baseline) |
| `verification_artifacts/quant_benchmark/20260710_050806/` | Q6/Q5/Q4 quant comparison |
| `verification_artifacts/q4_runtime_tuning/20260710_064041/` | Q4 thread-count sweep 0–32 |
| `verification_artifacts/q4_runtime_tuning/20260710_065510/` | Q4 codec context/holdback diagnostic |
| `verification_artifacts/q4_runtime_tuning/20260710_074118/` | Q4 context screen 4–64 |
| `verification_artifacts/q4_runtime_tuning/20260710_084956/` | Q4 context-32 stride sweep 4–32 |

## 1. Quantization Results

**Fixed**: stride 4, threads=0, codec context 4, holdback 0, low_latency=true

| Quant | RTF | First PCM | Size | Status |
|---|---|---|---|---|
| Q6_K | 1.121 | 247 ms | 4.53 GB | Baseline |
| Q5_K_M | 1.072 | 234 ms | 4.03 GB | Quality fallback |
| **Q4_K_M** | **1.015** | 219 ms | 3.57 GB | **Selected** |

**Decision**: Q4_K_M selected as performance model. Q5_K_M retained as quality fallback. Q6_K retired.

## 2. Thread-Count Results

**Fixed**: Q4_K_M, stride 4, context 4, holdback 0, low_latency=true. 3 measured runs.

| Threads | RTF Mean | RTF Range | First PCM | Status |
|---|---|---|---|---|
| 0 (auto) | 1.004 | 0.995–1.012 | 226 ms | — |
| **8** | **0.954** | 0.947–0.959 | 208 ms | **Best** |
| 16 | 0.968 | 0.957–0.976 | 216 ms | — |
| 24 | 0.988 | 0.974–0.999 | 219 ms | — |
| 32 | 1.006 | 0.994–1.019 | 226 ms | — |

**Decision**: Threads=8 is the clear winner. On i9-13900K, auto (0) is NOT optimal.

## 3. Codec Context / Holdback Diagnostic

**Fixed**: Q4_K_M, threads 8, stride 4, low_latency=true. 1+1 runs.

| Context | Holdback | RTF | First PCM | Audio |
|---|---|---|---|---|
| 4 | 0 | 0.954 | 211 ms | Tapping/blipping |
| 64 | 0 | 1.711 | 211 ms | Cleaner |
| 64 | 1 | 1.728 | 210 ms | No benefit over hb=0 |

**Decision**: Context > 4 eliminates artifacts but at substantial RTF cost. Holdback provides no benefit.

## 4. Context Screen (stride 4)

**Fixed**: Q4_K_M, threads 8, stride 4, holdback 0, low_latency=true. 1+1 runs.

| Context | RTF | First PCM | Audio Quality |
|---|---|---|---|
| 4 | 1.008 | 247 ms | ❌ Tapping |
| 8 | 1.062 | 239 ms | ❌ |
| 12 | 1.108 | 246 ms | ❌ |
| 16 | 1.166 | 252 ms | ❌ |
| 24 | 1.266 | 253 ms | ⚠️ Shaky/borderline |
| **32** | **1.374** | 257 ms | ✅ **First solid** |
| 48 | 1.573 | 239 ms | ✅ (slower) |
| 64 | 1.760 | 245 ms | ✅ (reference) |

**Decision**: Context 32 is the quality floor. Context 64 is reference only.

## 5. Context-32 Stride Sweep

**Fixed**: Q4_K_M, threads 8, context 32, holdback 0, low_latency=true. 1+1 runs.

| Stride | RTF | First PCM | Status |
|---|---|---|---|
| 4 | 1.371 | 256 ms | ❌ RTF > 1.0 |
| 8 | 1.153 | 398 ms | ❌ RTF > 1.0 |
| 12 | 1.083 | 566 ms | ❌ RTF > 1.0 |
| 16 | 1.065 | 816 ms | ❌ RTF > 1.0 |
| 24 | 1.015 | 1047 ms | ❌ RTF > 1.0 |
| **32** | **0.987** | 1355 ms | ✅ **Only sub-1.0** |

**Decision**: Stride 32 is the only context-32 configuration below RTF 1.0. Narrow margin (0.987). Backend first PCM ~1.35s. This is the provisional baseline — NOT final.

## TTFA vs RTF Tradeoff

| Metric | Best (ctx4/str4/th8) | Provisional (ctx32/str32/th8) |
|---|---|---|
| RTF | 0.954 | 0.987 |
| First PCM | 208 ms | 1355 ms |
| Audio quality | ❌ Tapping | ✅ Clean |

The provisional baseline sacrifices ~1.1s of first-PCM latency for clean audio. Future optimization may return to stride 8/12/16 if end-to-end traces show the real bottleneck is elsewhere.

## Human Listening Findings

*User-supplied judgments, not automated measurements:*

- Q5_K_M sounds slightly better than Q4_K_M
- Q4_K_M is acceptable and preferred as performance candidate
- Context < 24: consistently problematic
- Context 24: shaky/borderline
- Context 32: first consistently solid quality
- Context 64: good but too slow
- Holdback 1: no meaningful benefit over holdback 0
- Context-32 stride sweep WAVs: NOT yet comparatively evaluated
- Stride 32 audio quality: NOT fully human-validated

## Failed / Resumed Runs

- Stride 16 in context-32 sweep: initial measured run received HTTP 503 (transient). Successfully resumed. Final artifact reflects resumed result.
- Context 8, 12, 16, 24, 32, 48 in context screen: initially rejected by enum {4,64,160}. Code fixed in Phase 8E.1g. Resumed with `--resume-artifact`.

## Selected Provisional Baseline

### Backend
```
Model: /models/s2-pro-q4_k_m.gguf
S2_GPU_LAYERS=-1
S2_CODEC_CPU=false
S2_THREADS=8
--cpuset-cpus=0-15  (i9-13900K P-cores; remove on different hardware)
GPU: production RTX 3080
Image: ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd
```

### Wrapper / Request
```
S2_CODEC_CONTEXT_FRAMES=32
S2_STREAM_DECODE_STRIDE_FRAMES=32
S2_STREAM_HOLDBACK_FRAMES=0
S2_STREAM_START_BUFFER_MS=0
S2_INITIAL_BUFFER_MS=0
S2_LOW_LATENCY=true
S2_SEGMENT_SENTENCES=false
S2_DEFAULT_VOICE=cmu_bdl_male_us
S2_VOICE_DIR=/voices
```

## Rejected Alternatives

| Alternative | Why Rejected |
|---|---|
| Q6_K | RTF 1.121 at stride 4 — too slow |
| Q5_K_M | RTF 1.072 — slight quality advantage, Q4 preferred for speed |
| Context 4 | Audible tapping at all tested strides |
| Context 24 | Shaky/borderline quality |
| Context 64 | RTF 1.760 — much too slow |
| Threads > 8 | Degraded RTF on i9-13900K |
| Stride < 32 at ctx32 | RTF > 1.0 |

## Remaining Risks

1. **Stride 32 narrow margin**: RTF 0.987 leaves only 1.3% headroom. Concurrent HA pipeline activity could push it above 1.0
2. **First PCM latency**: 1.35s backend first PCM is measured as first HTTP chunk arrival, NOT end-to-end audible latency
3. **End-to-end latency unknown**: Satellite VAD + STT + LLM + TTS + playback path not yet measured
4. **P-core affinity not performance-proven**: Sensible allocation based on topology; not benchmarked against alternatives
5. **Context-32 stride listening incomplete**: Comparative WAV evaluation of strides 8/12/16/24/32 not yet done

## Deferred Optimization (Phase 8E.2)

- CUDA kernel selection (MMQ vs CUBLAS)
- GGML_NATIVE + LTO build
- GPU tuning (power limit, clocks)
- CPU affinity benchmarking
- Adaptive stride (initial vs steady-state)
- Benchmark-matrix automation

## End-to-End Latency Goals

| Target | Value |
|---|---|
| Goal | 1.5–2.0 seconds |
| Acceptable initial range | 2.0–3.0 seconds |

Backend first PCM of ~1.35s at stride 32 consumes most of the acceptable range before satellite/network/playback overhead.

## Next Steps

1. Manual deployment to Home Assistant staging
2. One short listening test through HA satellite
3. Capture full end-to-end trace (VAD→STT→LLM→TTS→playback)
4. Decision: acceptable latency → keep baseline; unacceptable → return to stride 8/12/16 optimization
5. Phase 9: queue, busy handling, timeout policy
6. Phase 9.5: progressive LLM-to-TTS phrase pipeline

# Streaming Decode Stride and Quantization Benchmarks

> **Last updated**: 2026-07-10
> **Hardware**: NVIDIA RTX 3080 (GPU-65b9a886-d157-27fa-09d1-8894bc5cc135)
> **Backend**: ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd
> **Model (baseline)**: s2-pro-q6_k.gguf (36 transformer layers on CUDA, Fast-AR)

## Table of Contents

1. [What Decode Stride Controls](#what-decode-stride-controls)
2. [Live Stride Benchmark Results](#live-stride-benchmark-results)
3. [TTFA/RTF Tradeoff Analysis](#ttfa-rtf-tradeoff-analysis)
4. [Why Stride 4 Is Currently Preferred](#why-stride-4-is-currently-preferred)
5. [Diminishing Returns at Higher Strides](#diminishing-returns-at-higher-strides)
6. [Human Listening Observations](#human-listening-observations)
7. [Quantization Comparison Methodology](#quantization-comparison-methodology)
8. [Which Tensors Remain F16](#which-tensors-remain-f16)
9. [Q8_0 Quality Ceiling (Optional)](#q8_0-quality-ceiling-optional)
10. [Raw Artifact Locations](#raw-artifact-locations)
11. [Hardware and Software Provenance](#hardware-and-software-provenance)
12. [Benchmark Limitations](#benchmark-limitations)
13. [Recommended Next Action](#recommended-next-action)

---

## What Decode Stride Controls

The `stream_decode_stride_frames` parameter controls how many audio frames the
s2.cpp backend decodes per streaming step during generation.

- **stride = 1**: decode one frame at a time.  Finest streaming granularity,
  lowest TTFA, but highest overhead (more HTTP chunk boundaries and AR/stream
  coordination).
- **stride > 1**: decode multiple frames per step.  Coarser streaming, higher
  TTFA, lower overhead.

The tradeoff is between **time-to-first-audio (TTFA)** and **total synthesis
throughput (RTF)**.  Larger strides batch more frames per decode step,
amortizing the per-step overhead (Python/C++ boundary, VRAM access patterns,
stream-decode loop) at the cost of making the first audio chunk larger.

The backend's streaming path uses these settings on every request:

```
S2_STREAM_DECODE_STRIDE_FRAMES = 4
S2_STREAM_HOLDBACK_FRAMES      = 0
S2_STREAM_START_BUFFER_MS      = 0
S2_LOW_LATENCY                 = true
S2_CODEC_CONTEXT_FRAMES        = 4
S2_SEGMENT_SENTENCES           = false
```

---

## Live Stride Benchmark Results

### Primary benchmark: `20260710_021915`

Tested on RTX 3080, Q6_K model, 361-char text, 1 warmup + 3 measured runs.

| Stride | Avg RTF | Avg First PCM (ms) | Avg Total (ms) | Success |
|--------|---------|---------------------|----------------|---------|
| 1      | 1.34    | 105                 | 28,787         | 3/3     |
| 2      | 1.19    | 150                 | 24,761         | 3/3     |
| 4      | 1.13    | 251                 | 25,127         | 3/3     |
| 8      | 1.08    | 419                 | 22,513         | 3/3     |

### Higher-stride testing: `20260710_024627`

Additional testing for diminishing-returns analysis.

| Stride | Avg RTF | Avg First PCM (ms) | Avg Total (ms) | Success |
|--------|---------|---------------------|----------------|---------|
| 8      | 1.10    | 491                 | 24,020         | 3/3     |
| 12     | 1.08    | 669                 | 22,652         | 3/3     |
| 16     | 1.08    | 837                 | 23,338         | 3/3     |
| 24     | 1.07    | 1,209               | 22,589         | 3/3     |

---

## TTFA/RTF Tradeoff Analysis

```
Stride    RTF        First PCM     TTFA penalty    Throughput gain
─────     ───        ─────────     ─────────────   ───────────────
1 → 2:    1.34→1.19  105→150 ms    +45 ms          +0.15 RTF
2 → 4:    1.19→1.13  150→251 ms    +101 ms         +0.06 RTF
4 → 8:    1.13→1.08  251→419 ms    +168 ms         +0.05 RTF
8 → 16:   1.10→1.08  491→837 ms    +346 ms         +0.02 RTF
16 → 24:  1.08→1.07  837→1209 ms   +372 ms         +0.01 RTF
```

The steepest TTFA increase and largest RTF improvement come from stride
1→2.  Beyond stride 4, throughput gains rapidly diminish while first-PCM
latency continues to increase linearly.

---

## Why Stride 4 Is Currently Preferred

Stride 4 is the current preferred candidate because it provides the best
compromise:

1. **TTFA ~251 ms**: perceptually responsive for voice assistant use.
2. **RTF ~1.13**: significantly better than stride 1 (1.34), though still
   above real-time.
3. **Diminishing returns after 4**: stride 8 only improves RTF by 0.05
   (1.13→1.08) while doubling first-PCM latency (251→419 ms).
4. **Human listening**: all strides sound broadly similar; no quality
   regression detected at stride 4.

Stride 4 is the pragmatic choice: enough batching to reduce overhead,
but not so much that it degrades the interactive feel.

---

## Diminishing Returns at Higher Strides

Beyond stride 8, RTF improvements flatten:

- Stride 16→24: RTF drops only 0.01 (1.08→1.07)
- First-PCM increases by ~372 ms per step

The total wall-clock improvement from stride 8 to 24 is only ~1,431 ms (5.9%)
for a 22s audio clip — not worth the 790 ms TTFA penalty.

**Key insight**: The Q6_K model on RTX 3080 cannot reach RTF < 1.0 at any
stride (best: 1.07 at stride 24).  The bottleneck is in AR generation, not
stream-decode batching.  This is why quantization comparison (Phase 8D) is
the logical next step.

---

## Human Listening Observations

Informal listening across stride 1 through 8:

- All strides produced **broadly similar** audio quality.
- All had **minor artifacts** but were **mostly acceptable** for voice
  assistant use.
- No stride-specific quality regression was audible.
- Higher strides (12–24) were not subjectively evaluated (PCM artifacts
  preserved for later review).

**Listening is required** before making any production change — RTF metrics
alone do not capture perceptual quality.

---

## Quantization Comparison Methodology

### Candidate Models

| Quant   | Filename                | Expected Size | Status        |
|---------|-------------------------|---------------|---------------|
| Q6_K    | s2-pro-q6_k.gguf       | ~4.5 GB       | Baseline ✅   |
| Q5_K_M  | s2-pro-q5_k_m.gguf     | ~4.0 GB       | To download   |
| Q4_K_M  | s2-pro-q4_k_m.gguf     | ~3.6 GB       | To download   |
| Q8_0    | s2-pro-q8_0.gguf       | ~5.2 GB       | Optional      |

### Fixed Variables

All quant comparisons hold these constant:

```
stream_decode_stride_frames = 4
codec_decode_context_frames = 4
stream_holdback_frames      = 0
stream_start_buffer_ms      = 0
low_latency                 = true
threads                     = 0
voice                       = (same saved voice)
text                        = (same benchmark text)
warmup runs                 = 1
measured runs               = 3
```

### Test Sequence

1. Spin up a **temporary benchmark backend container** with:
   - Distinct name: `s2cpp-backend-bench`
   - Distinct host port (not 3032)
   - Same image: `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd`
   - Same GPU UUID
   - Read-only model mounts
2. For each candidate model:
   - Restart the benchmark container with the model path
   - Wait for backend readiness
   - Run 1 warmup + 3 measured syntheses
   - Capture per-run [Metrics] output
   - Save raw PCM
3. Aggregate results
4. Convert PCM to WAV (one per quant)
5. Human listening evaluation

### Production Safety

- The benchmark container is **temporary** — it does not affect the running
  production `s2cpp-backend` container.
- No production wrapper, Home Assistant, or backend image modifications.
- The winning model is NOT promoted automatically — the user must explicitly
  apply the change.

### If Benchmark Cannot Run From Hermes

See the **Host-Side Live Benchmark Runbook** below for the complete step-by-step
sequence.  All commands use the existing ``scripts/benchmark_quantization.py``
harness directly — no separate orchestration script is needed.

```bash
cd /mnt/user/appdata/hermes-agent/webui-workspace/wyoming-s2cpp-tts
# Start at Step 1 of the runbook below
```

---

## Which Tensors Remain F16

In the Q6_K, Q5_K_M, and Q4_K_M quants:

- **Attention weights** (Q, K, V projections): quantized to Q6_K / Q5_K_M / Q4_K_M
- **Feed-forward weights** (MLP layers): quantized
- **Output projection**: quantized
- **Embedding tables**: remain F16 (quantized embedding tables degrade quality
  disproportionately; they stay on CPU in hybrid CUDA path)
- **Codec tensors**: remain F16 (never quantized — codec quality is critical)
- **Layer norms, biases, RMS norms**: remain F32 (small, don't affect model size)

The codec tensors are explicitly excluded from quantization per the s2.cpp
GGUF format conventions.

---

## Q8_0 Quality Ceiling (Optional)

Q8_0 is an 8-bit quantization with near-lossless quality — effectively the
quality ceiling for quantized models.  It may be benchmarked as an optional
reference point if:

- Storage is sufficient (~5.2 GB vs 3.2 GB for Q6_K)
- The file is verified as compatible with s2.cpp backend
- Adding it does not delay the primary Q6/Q5/Q4 comparison

Q8_0's larger size and higher memory bandwidth requirements may actually
make it SLOWER than Q6_K despite using more bits — quantization benchmarks
are needed to confirm.

---

## Raw Artifact Locations

| Artifact Set | Path |
|---|---|
| Stride sweep (1, 2, 4, 8) | `verification_artifacts/realtime_tuning/20260710_021915/` |
| Higher-stride testing (8–24) | `verification_artifacts/realtime_tuning/20260710_024627/` |
| Quant benchmark (future) | `verification_artifacts/quant_benchmark/<timestamp>/` |

Each artifact directory contains:
- `results.json` — full per-run timing data
- `summary.md` — Markdown summary table
- `gpu_telemetry.csv` — 1 Hz GPU samples (utilization, memory, temp, power, clocks)
- `per_run_metrics.json` — correlated backend [Metrics] per run
- `stride*_run*_metrics.log` — raw `docker logs` per run
- `stride*_run*/` — per-run subdirectories with raw PCM

---

## Hardware and Software Provenance

| Component | Value |
|---|---|
| **GPU** | NVIDIA RTX 3080 (UUID: GPU-65b9a886-d157-27fa-09d1-8894bc5cc135) |
| **GPU driver** | Running container's CUDA driver |
| **Backend image** | `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd` |
| **Backend digest** | `sha256:c29e41e59b470d58bf4b88c11c9ec753e00fa74a3bffbb003bc257fb9c6e46d9` |
| **s2.cpp revision** | edf89bd7c5554769bb36cbd049b6fbb98bcb9d41 |
| **Repository commit** | 97eb49c (perf/realtime-stride-tuning) |
| **Wrapper (production)** | `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-9c134cc` |
| **Container network** | sorilonet |
| **Backend port** | 3032 (host) → 3030 (container) |
| **Unraid host** | 192.168.1.45 |
| **Home Assistant** | 192.168.1.233 → 192.168.1.45:10200 |

---

## Benchmark Limitations

1. **Single GPU**: all results are on one RTX 3080.  Different GPUs will have
   different RTF characteristics.
2. **Single text sample**: the 361-char benchmark text may not represent all
   synthesis workloads (short commands, long narratives).
3. **No production load simulation**: benchmarks run sequentially with idle GPU
   between runs.  Concurrent TTS requests or HA pipeline activity may affect
   real-world performance.
4. **Three measured runs**: not statistically definitive.  Variability across
   runs is reported but confidence intervals are not computed.
5. **Audio quality**: assessed informally by one listener.  Perceptual quality
   claims require controlled listening tests.
6. **Model loading**: each quant comparison requires container restart, which
   includes model loading time (~10–30s).  This is excluded from measurement
   but documented.
7. **Backend metrics correlation**: the [Metrics] Streaming line is polled
   from `docker logs --since` with a 30s bounded timeout.  Concurrent requests
   to the same backend could cause metric misattribution (the benchmark
   container is dedicated, mitigating this).

---

## Host-Side Live Benchmark Runbook

> **Architecture**: The s2.cpp server loads ONE GGUF at process startup via the
> `S2_MODEL` environment variable.  HTTP requests cannot switch models mid-process.
> Each candidate requires a fresh backend container start/stop cycle.

### Step 1: Pull the latest commit

```bash
cd /mnt/user/appdata/hermes-agent/webui-workspace/wyoming-s2cpp-tts
git fetch origin
git checkout perf/realtime-stride-tuning
git pull origin perf/realtime-stride-tuning
git log --oneline -1
```

### Step 2: Discover the `/models` host mount

```bash
docker inspect s2cpp-backend \
  --format '{{range .Mounts}}{{if eq .Destination "/models"}}{{.Source}}{{end}}{{end}}'
```

Use the printed host source path for all model file operations below.

### Step 3: List existing GGUF files and calculate required storage

```bash
HOST_MODELS="/mnt/user/appdata/s2cpp/models"  # ← REPLACE with discovered path
ls -lh "$HOST_MODELS"/s2-pro-q*.gguf 2>/dev/null
df -h "$HOST_MODELS"
```

### Step 4: Download missing candidate models

| Model          | Filename                | ~Size  | Status     |
|----------------|-------------------------|--------|------------|
| Q6_K (baseline)| s2-pro-q6_k.gguf       | 4.5 GB | Existing   |
| Q5_K_M         | s2-pro-q5_k_m.gguf     | 4.0 GB | Download   |
| Q4_K_M         | s2-pro-q4_k_m.gguf     | 3.6 GB | Download   |

Download using resumable curl with atomic rename:

```bash
HOST_MODELS="/mnt/user/appdata/s2cpp/models"  # ← REPLACE with discovered path

# Q5_K_M (~4.0 GB)
curl --continue-at - --fail --location --retry 3 \
  -o "$HOST_MODELS/s2-pro-q5_k_m.gguf.part" \
  "<UPSTREAM_Q5_URL>" \
  && mv "$HOST_MODELS/s2-pro-q5_k_m.gguf.part" "$HOST_MODELS/s2-pro-q5_k_m.gguf"

# Q4_K_M (~3.6 GB)
curl --continue-at - --fail --location --retry 3 \
  -o "$HOST_MODELS/s2-pro-q4_k_m.gguf.part" \
  "<UPSTREAM_Q4_URL>" \
  && mv "$HOST_MODELS/s2-pro-q4_k_m.gguf.part" "$HOST_MODELS/s2-pro-q4_k_m.gguf"
```

> **Replace `<UPSTREAM_*_URL>` with the verified upstream S2 Pro GGUF download URLs.**

Record checksums:

```bash
sha256sum "$HOST_MODELS"/s2-pro-q*.gguf | tee model_sha256.txt
```

### Step 5: Dry-run the orchestrator (safe, no containers)

```bash
cd /mnt/user/appdata/hermes-agent/webui-workspace/wyoming-s2cpp-tts
bash scripts/run_quantization_benchmark_unraid.sh
```

### Step 6: Run the real benchmark

```bash
bash scripts/run_quantization_benchmark_unraid.sh --run-real
```

This orchestration sequence:
1. Discovers idle GPU (avoids the production TTS GPU if busy)
2. Checks available storage
3. For each candidate (Q6 → Q5 → Q4):
   - Starts temporary `s2cpp-backend-bench` container with `S2_MODEL=/models/<file>`
   - Waits for `Launching: s2 --model /models/<file>` in startup logs
   - Verifies HTTP endpoint reachability
   - Runs `benchmark_quantization.py --run-real` with exactly one model
   - Captures container inspect, startup logs, backend metrics, GPU telemetry
   - Stops and removes the temporary container
4. Produces combined results in `verification_artifacts/quant_benchmark/<timestamp>/`

### Step 7: Identify the artifact directory

```bash
ls -d verification_artifacts/quant_benchmark/*/ | tail -1
```

### Step 8: Convert PCM to WAV for listening

**Primary path: Hermes-Suite ffmpeg** (Unraid host lacks ffmpeg):

```bash
ARTIFACT_DIR="verification_artifacts/quant_benchmark/<timestamp>"  # ← fill in

for label in q6_k q5_k_m q4_k_m; do
  docker exec Hermes-Suite ffmpeg -y -f s16le -ar 44100 -ac 1 \
    -i "/workspace/wyoming-s2cpp-tts/$ARTIFACT_DIR/$label/quant_${label}_run1.pcm" \
    "/workspace/wyoming-s2cpp-tts/$ARTIFACT_DIR/$label/quant_${label}_run1.wav"
done
```

**Fallback: Python wave module** (no transcoding, adds WAV header):

```bash
ARTIFACT_DIR="verification_artifacts/quant_benchmark/<timestamp>"  # ← fill in

python3 -c "
import wave, sys
for label in ['q6_k', 'q5_k_m', 'q4_k_m']:
    pcm_path = f'$ARTIFACT_DIR/{label}/quant_{label}_run1.pcm'
    wav_path = f'$ARTIFACT_DIR/{label}/quant_{label}_run1.wav'
    with open(pcm_path, 'rb') as pf:
        pcm = pf.read()
    with wave.open(wav_path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(44100)
        wf.writeframes(pcm)
    print(f'  {wav_path}')
"
```

### Step 9: View the aggregate report

```bash
cat "$ARTIFACT_DIR/summary.md"
python3 -m json.tool "$ARTIFACT_DIR/combined_results.json" | head -80
```

### Step 10: Human listening evaluation

Use the Listening Checklist in this document to evaluate each quant's WAV file.

### Step 11: Cleanup

The orchestrator handles cleanup automatically (trap on EXIT/INT/TERM).
Verify no benchmark containers remain:

```bash
docker ps -a | grep s2cpp-backend-bench || echo "Clean"
```

### Step 12: Selection decision

- If a lower quant achieves **RTF < 0.95** with **no quality regression** →
  recommend as production model.
- If no quant reaches RTF < 1.0 → **no production change**; proceed to Phase 8E.
- **DO NOT automatically promote** — human listening required first.

---

## Hardware and Software Provenance

| Component | Value |
|---|---|
| **GPU** | NVIDIA RTX 3080 (UUID: GPU-65b9a886-d157-27fa-09d1-8894bc5cc135) |
| **GPU driver** | Running container's CUDA driver |
| **Backend image** | `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd` |
| **Backend digest** | `sha256:c29e41e59b470d58bf4b88c11c9ec753e00fa74a3bffbb003bc257fb9c6e46d9` |
| **s2.cpp revision** | edf89bd7c5554769bb36cbd049b6fbb98bcb9d41 |
| **Repository commit** | 97eb49c (perf/realtime-stride-tuning) |
| **Wrapper (production)** | `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-9c134cc` |
| **Container network** | sorilonet |
| **Backend port** | 3032 (host) → 3030 (container) |
| **Unraid host** | 192.168.1.45 |
| **Home Assistant** | 192.168.1.233 → 192.168.1.45:10200 |

---

## Benchmark Limitations

1. **Single GPU**: all results are on one RTX 3080.  Different GPUs will have
   different RTF characteristics.
2. **Single text sample**: the 361-char benchmark text may not represent all
   synthesis workloads (short commands, long narratives).
3. **No production load simulation**: benchmarks run sequentially with idle GPU
   between runs.  Concurrent TTS requests or HA pipeline activity may affect
   real-world performance.
4. **Three measured runs**: not statistically definitive.  Variability across
   runs is reported but confidence intervals are not computed.
5. **Audio quality**: assessed informally by one listener.  Perceptual quality
   claims require controlled listening tests.
6. **Model loading**: each quant comparison requires container restart, which
   includes model loading time (~10–30s).  This is excluded from measurement
   but documented.
7. **Backend metrics correlation**: the [Metrics] Streaming line is polled
   from `docker logs --since` with a 30s bounded timeout.  Concurrent requests
   to the same backend could cause metric misattribution (the benchmark
   container is dedicated, mitigating this).

---

## Host-Side Live Benchmark Runbook

> **Important**: `/models` is the path *inside* the backend container, not on the
> Unraid host.  You must discover the host source directory mapped to `/models`
> before downloading or inspecting model files.

### Step 1: Pull the latest commit

```bash
cd /mnt/user/appdata/hermes-agent/webui-workspace/wyoming-s2cpp-tts
git fetch origin
git checkout perf/realtime-stride-tuning
git pull origin perf/realtime-stride-tuning
git log --oneline -1  # should show the Phase 8D.1 cleanup commit
```

### Step 2: Discover the `/models` host mount

```bash
docker inspect s2cpp-backend   --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}'
```

This prints the host source path mapped to each container destination.  Look for
the line ending in `-> /models`.  Use that host source path for all model file
operations below.  The examples use `HOST_MODELS` as a placeholder — replace it
with the actual discovered path.

### Step 3: List existing GGUF files and calculate required storage

```bash
HOST_MODELS="/mnt/user/appdata/s2cpp/models"  # ← REPLACE with discovered path
ls -lh "$HOST_MODELS"/s2-pro-q*.gguf 2>/dev/null || echo "No existing GGUF files found"
```

### Step 4: Download missing candidate models

Required models (from verified upstream S2 Pro GGUF source):

| Model          | Filename                | ~Size  | Status   |
|----------------|-------------------------|--------|----------|
| Q6_K (baseline)| s2-pro-q6_k.gguf       | 3.2 GB | Existing |
| Q5_K_M         | s2-pro-q5_k_m.gguf     | 2.9 GB | Download |
| Q4_K_M         | s2-pro-q4_k_m.gguf     | 2.6 GB | Download |

Download using resumable downloads (curl -C -).  Example:

```bash
# Replace MODEL_URL with the actual upstream download URL for each quant
curl -C - -L -o "$HOST_MODELS/s2-pro-q5_k_m.gguf" "MODEL_URL"
curl -C - -L -o "$HOST_MODELS/s2-pro-q4_k_m.gguf" "MODEL_URL"
```

After download, record checksums:

```bash
sha256sum "$HOST_MODELS"/s2-pro-q*.gguf
```

### Step 5: Dry-run the quant benchmark (safe, no network)

```bash
cd /mnt/user/appdata/hermes-agent/webui-workspace/wyoming-s2cpp-tts
python3 scripts/benchmark_quantization.py   --models "$HOST_MODELS/s2-pro-q6_k.gguf,$HOST_MODELS/s2-pro-q5_k_m.gguf,$HOST_MODELS/s2-pro-q4_k_m.gguf"   --stride 4
```

This prints what models exist and what would be benchmarked without contacting
the backend.

### Step 6: Spin up a temporary benchmark backend container

```bash
# Stop any previous benchmark container
docker rm -f s2cpp-backend-bench 2>/dev/null || true

# Start a temporary backend with a distinct port and read-only model mount
docker run -d --name s2cpp-backend-bench   --gpus '"device=GPU-65b9a886-d157-27fa-09d1-8894bc5cc135"'   --network sorilonet   -p 3033:3030   -v "$HOST_MODELS:/models:ro"   ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd

# Wait for backend readiness
sleep 15
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3033/
# Expect: 404 (normal — /generate is the working endpoint)
```

### Step 7: Run the real quant benchmark

```bash
python3 scripts/benchmark_quantization.py --run-real   --endpoint 127.0.0.1:3033   --models "$HOST_MODELS/s2-pro-q6_k.gguf,$HOST_MODELS/s2-pro-q5_k_m.gguf,$HOST_MODELS/s2-pro-q4_k_m.gguf"   --stride 4 --warmup-runs 1 --measured-runs 3   --voice ""  # Use default voice
```

Benchmark runs sequentially: Q6_K → Q5_K_M → Q4_K_M.  Each gets 1 warmup + 3
measured runs.  Raw PCM artifacts are saved in
`verification_artifacts/quant_benchmark/<timestamp>/`.

### Step 8: Identify the timestamped artifact directory

```bash
ls -d verification_artifacts/quant_benchmark/*/ | tail -1
```

### Step 9: Convert representative PCM to WAV for listening

Use ffmpeg from Hermes Suite (`/usr/bin/ffmpeg`):

```bash
ARTIFACT_DIR="verification_artifacts/quant_benchmark/<timestamp>"  # ← fill in
for quant in q6_k q5_k_m q4_k_m; do
  ffmpeg -f s16le -ar 44100 -ac 1     -i "$ARTIFACT_DIR/quant_${quant}_run1/quant_${quant}_run1.pcm"     "$ARTIFACT_DIR/quant_${quant}_run1.wav"
done
```

### Step 10: View the aggregate report

```bash
cat "$ARTIFACT_DIR/summary.md"
cat "$ARTIFACT_DIR/results.json" | python3 -m json.tool | head -80
```

### Step 11: Human listening evaluation

Use the Listening Checklist in this document to evaluate each quant's WAV file.
Rank quants by perceived quality and note any artifacts.

### Step 12: Clean up the benchmark container

```bash
docker rm -f s2cpp-backend-bench
```

### Step 13: Selection decision

- If a lower quant achieves **RTF < 0.95** with **no quality regression** →
  recommend as production model.
- If no quant reaches RTF < 1.0 → **no production change**; proceed to Phase 8E.
- If a quant has **minor quality regression** but RTF < 0.95 → user judgment
  call on quality vs. performance.

**DO NOT automatically promote** a winning model to production.  The user
must explicitly apply the change by updating the production backend container's
model path after listening evaluation.

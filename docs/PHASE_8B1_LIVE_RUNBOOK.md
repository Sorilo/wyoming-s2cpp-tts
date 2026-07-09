# Phase 8B1 Live Verification Runbook

## Prerequisites

- Unraid host with RTX 3080 (10 GB VRAM)
- Production backend running: `s2cpp-backend` (sha-741d06b)
- Production wrapper running: `wyoming-s2cpp-tts` (sha-9c134cc)
- `S2_SEGMENT_SENTENCES=false`, `S2_CODEC_CONTEXT_FRAMES=4`

## Option A: Diagnostic Backend on Second GPU

If you have a second GPU, run the diagnostic backend alongside production:

```bash
# Verify production backend GPU
docker inspect s2cpp-backend --format '{{.HostConfig.Devices}}'

# Deploy diagnostic on SECOND GPU
docker run -d \
  --name s2cpp-backend-diag \
  --network sorilonet \
  --gpus device=<SECOND_GPU_UUID> \
  -v /mnt/user/appdata/s2cpp/models:/models:ro \
  -v /mnt/user/appdata/s2cpp/voices:/voices:ro \
  -p 3031:3030 \
  -e S2_MODEL=/models/s2-pro-q6_k.gguf \
  -e S2_HOST=0.0.0.0 -e S2_PORT=3030 \
  -e S2_GPU_LAYERS=-1 -e S2_LOG_LEVEL=info \
  ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-29a5a2c

# Point wrapper to diagnostic backend
docker update --env S2_PORT=3031 wyoming-s2cpp-tts
# OR: docker stop wyoming-s2cpp-tts && docker run ... -e S2_HOST=s2cpp-backend-diag -e S2_PORT=3030 ...
```

## Option B: Temporary Replacement (same GPU)

> ⚠️ **WARNING:** Loading two backend containers on the same 10 GB GPU
> will exhaust VRAM. Stop production before starting diagnostic.

```bash
# 1. Stop production backend
docker stop s2cpp-backend

# 2. Start diagnostic on same GPU
docker run -d \
  --name s2cpp-backend-diag \
  --network sorilonet \
  --gpus all \
  -v /mnt/user/appdata/s2cpp/models:/models:ro \
  -v /mnt/user/appdata/s2cpp/voices:/voices:ro \
  -p 3030:3030 \
  -e S2_MODEL=/models/s2-pro-q6_k.gguf \
  -e S2_HOST=0.0.0.0 -e S2_PORT=3030 \
  -e S2_GPU_LAYERS=-1 -e S2_LOG_LEVEL=info \
  ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-29a5a2c

# Wrapper continues to use s2cpp-backend:3030 → now goes to diagnostic
```

## Verify Deployment

```bash
# Confirm wrapper image
docker inspect wyoming-s2cpp-tts --format '{{.Config.Image}}'
# Expected: ghcr.io/sorilo/wyoming-s2cpp-tts:sha-9c134cc

# Confirm backend image
docker inspect s2cpp-backend-diag --format '{{.Config.Image}}'
# Expected: ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-29a5a2c

# Confirm wrapper settings
docker exec wyoming-s2cpp-tts env | grep S2_
# Expected: S2_STREAM=true, S2_SEGMENT_SENTENCES=false, S2_CODEC_CONTEXT_FRAMES=4

# Verify backend is serving
curl http://<diagnostic-ip>:3030/ 2>&1 | head -1
# Expected: HTTP/1.1 404 Not Found (server is alive)

# Verify Home Assistant can still do normal synthesis
# (Use Try Voice in HA settings)
```

## Run Live Verification

The log capture helper is safe for unattended/background use.  Use
`--duration` so the capture exits on its own instead of waiting for keyboard
input.

```bash
cd /workspace/wyoming-s2cpp-tts

# Start unattended log capture. 180 seconds is intentionally longer than the
# five-cycle verification run so backend cancellation logs are not truncated.
./scripts/capture_phase_8b1_logs.sh \
  --duration 180 \
  wyoming-s2cpp-tts s2cpp-backend-diag &
CAPTURE_PID=$!

# Run the corrected client harness.
PYTHONPATH=. .venv/bin/python scripts/live_verify_phase_8b1.py \
  --host 192.168.1.45 --port 10200 --runs 5 \
  --chunks-before-disconnect 3 --timeout 30 --recovery-delay 1.0

# Wait for capture metadata/logs to flush.
wait "$CAPTURE_PID"
```

Expected corrected console shape:

```text
Disconnect: 650 ms | Audio: PASS | Protocol: PASS | First audio: 4 ms | Complete: 3400 ms | PCM: 229376 bytes
```

For the exact recovery request type used by the harness (`Synthesize`, not a
streaming `SynthesizeStart`/`SynthesizeStop` session), the correct terminal
sequence is `AudioStart` → `AudioChunk`* → `AudioStop`.  `synthesize-stopped` is
not required for standalone legacy `Synthesize`; it remains required for a
Wyoming streaming-text session.

## Analyze Results

```bash
cd verification_artifacts/phase_8b1

# Client results
cat client-results.json | python3 -c "
import json, sys
data = json.load(sys.stdin)
for r in data:
    print(f'Cycle {r[\"cycle\"]}: dc={r.get(\"cancel_disconnect_ms\")}ms '
          f'recovery={\"OK\" if r.get(\"recovery_success\") else \"FAIL\"} '
          f'pcm={r.get(\"recovery_pcm_bytes\",0)}B')"

# Backend cancellation events
echo "=== Backend Cancellation Events ==="
grep -n "CANCEL" backend-live.log

echo "=== Wrapper Disconnect Events ==="
grep -n "disconnect\|cancel" wrapper-live.log

# Final batch decode skipped?
grep -c "stream_aborted\|final.*decode.*skip" backend-live.log

# GPU utilization during test
echo "=== GPU Utilization ==="
grep -v "^===" nvidia-smi.log | head -20
```

## Correlate Timestamps

```bash
# Extract client disconnect times (ms from cycle start)
python3 -c "
import json
with open('verification_artifacts/phase_8b1/client-results.json') as f:
    data = json.load(f)
for r in data:
    print(f'Cycle {r[\"cycle\"]}: client_dc={r.get(\"cancel_disconnect_ms\")}ms')"

# Find matching backend events (look for CANCEL lines near those timestamps)
# Manual correlation: compare client dc_ms with backend log timestamps
```

## Rollback (Option B only)

```bash
# Stop diagnostic
docker stop s2cpp-backend-diag
docker rm s2cpp-backend-diag

# Restart production
docker start s2cpp-backend

# Verify Home Assistant Try Voice works normally
```

## Promotion Criteria

Check ALL before promoting:

- [ ] `backend_cancel_detected` appears for every cancelled request
- [ ] `generation_cancel_observed` appears after detection
- [ ] `backend_request_cancelled` appears with timing
- [ ] Final batch decode was skipped (no decode after cancel)
- [ ] Every recovery request succeeded (HTTP 200, valid PCM, correct voice)
- [ ] No crashes, deadlocks, or GPU memory leaks
- [ ] No regression in normal synthesis latency or quality


## Current Phase 8B1 Status Note

The first live attempt produced five non-empty, frame-aligned recovery WAVs, but
old harness logic marked all five failed because it incorrectly waited for
`synthesize-stopped` after standalone legacy `Synthesize` recovery requests.
The corrected harness separates:

- `audio_recovery_success`
- `protocol_terminal_success`
- `pcm_valid`
- `first_audio_ms`
- `completion_ms`
- `pcm_bytes`
- `exact_failure_reason`

Phase 8B1 is **not complete** until a rerun captures diagnostic-backend
cancellation logs (`backend_cancel_detected`, `generation_cancel_observed`, and
`backend_request_cancelled`) plus successful corrected recovery evidence.

## Long-Form Quality Follow-Up

After the corrected cancellation rerun, use
[`docs/PHASE_8B1_LONG_FORM_COMPARISON.md`](PHASE_8B1_LONG_FORM_COMPARISON.md)
for the controlled context 4 vs 64 vs auto/160 long-form listening and timing
comparison. Do not change production defaults based on a single subjective report
without that comparison.

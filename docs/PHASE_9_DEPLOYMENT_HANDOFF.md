# Phase 9 Deployment Handoff

## Source

- **Commit:** `12f3bf8`
- **Branch:** `phase/phase-9-queue-busy-timeouts`

## New Environment Variables

Add to wrapper container:

| Variable | Default | Description |
|----------|---------|-------------|
| `S2_BACKEND_BUSY_MAX_RETRIES` | 3 | Additional 503 retries (4 total attempts) |
| `S2_BACKEND_BUSY_RETRY_DELAY_MS` | 200 | Milliseconds between retries |
| `S2_QUEUE_WAIT_TIMEOUT_SEC` | 30 | Max seconds waiting in queue |
| `S2_SYNTHESIS_TIMEOUT_SEC` | 120 | Max seconds for backend synthesis |

All values validated at startup with strict range checks.

## Image

### Wrapper (NEW)
```
ghcr.io/sorilo/wyoming-s2cpp-tts:sha-12f3bf8

**Canonical RepoDigest:** ghcr.io/sorilo/wyoming-s2cpp-tts@sha256:1954a448a52cf6ebbbd4c09c231fb416b045d8d421d25b1c3e11acf82be28d9b
**Local ImageID:** sha256:97bc8bf14202b8c0c4ce4e1c7a0857a3b5ff40c37232fae8636d22f529635fbd
**Verified:** Independently pulled and inspected on target Unraid server

**Workflow Run:** 29129753396
**Job:** 86482658019
**Source SHA:** 12f3bf8a489e76b13c62bd62c52fd443d1b07d82
```
**Workflow:** https://github.com/Sorilo/wyoming-s2cpp-tts/actions/runs/29129753396
**Build:** `docker build -t ghcr.io/sorilo/wyoming-s2cpp-tts:sha-12f3bf8 .`
**Push:** `docker push ghcr.io/sorilo/wyoming-s2cpp-tts:sha-12f3bf8`

### Backend (UNCHANGED)
```
ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd
sha256:c29e41e59b470d58bf4b88c11c9ec753e00fa74a3bffbb003bc257fb9c6e46d9
```

### Rollback Wrapper
```
ghcr.io/sorilo/wyoming-s2cpp-tts:sha-22db725
```

## Production Configuration (Unchanged)
```
S2_MODEL=/models/s2-pro-q4_k_m.gguf
S2_THREADS=8
S2_GPU_LAYERS=-1
S2_CODEC_CPU=false
S2_STREAM=true
S2_SEGMENT_SENTENCES=false
S2_CODEC_CONTEXT_FRAMES=32
S2_STREAM_DECODE_STRIDE_FRAMES=32
S2_STREAM_HOLDBACK_FRAMES=0
S2_STREAM_START_BUFFER_MS=0
S2_INITIAL_BUFFER_MS=0
S2_LONG_FORM_BUFFER_MS=0
S2_MAX_INITIAL_BUFFER_MS=0
S2_LOW_LATENCY=true
S2_DEFAULT_VOICE=cmu_bdl_male_us
S2_VOICE_DIR=/voices
MAX_QUEUE_SIZE=3
CANCEL_ON_NEW_REQUEST=false
CANCEL_ON_CLIENT_DISCONNECT=true
```

## Verification

1. Deploy wrapper image with new env vars (defaults OK)
2. Send two simultaneous TTS requests — verify serialization
3. Send request, disconnect client mid-stream — verify cleanup
4. Check logs for `queue_request_received`, `queue_started`, `queue_depth_changed`
5. Verify Q4_K_M/context32/stride32 baseline unchanged

## Rollback

```bash
docker pull ghcr.io/sorilo/wyoming-s2cpp-tts:sha-22db725
# Update container to use sha-22db725
# Backend unchanged
```

## Blocker Criteria

- Queue counters do not reach zero after requests
- Two backend calls overlap (check backend metrics)
- Home Assistant TTS hangs >30s after disconnect
- Existing audio quality regresses

## First Unraid Live Validation — 2026-07-11

### Image tested
`ghcr.io/sorilo/wyoming-s2cpp-tts:sha-12f3bf8`

### PASS
- Short synthesis: valid PCM, one AudioStart/Stop
- Long synthesis: multiple AudioChunks, RTF ~0.972
- FIFO ordering and serialization verified
- Production containers unchanged
- Shadow cleanup successful

### FAIL
- **Backend-busy retry crashed with UnboundLocalError** — `audio_start_emitted` not initialized before retry loop; raised when `stream.__enter__()` returned 503
- Recovery requests after disconnect produced no audio (same crash)
- **Unhandled BrokenPipeError task warning** — Wyoming handler task exception not retrieved after client disconnect

### UNPROVEN
- Queue-full rejection: helper-mode log polling inaccessible from container

### Fixes applied (commits 94ebd76, 1b3ee17)
- Initialize `audio_start_emitted=False` before retry loop
- Catch BrokenPipeError/ConnectionResetError as normal disconnect
- Suppress asyncio.TimeoutError re-raise (already logged by generator)
- 4 regression tests for busy-before-enter paths

### New image
**Source commit:** 1b3ee176dc65aa7e0a00775257e84c81d74debe8
**Tag:** ghcr.io/sorilo/wyoming-s2cpp-tts:sha-1b3ee17
**Workflow:** https://github.com/Sorilo/wyoming-s2cpp-tts/actions/runs/29134187755


## 2026-07-11 Persistent Busy-Latch Repair

Live evidence in `verification_artifacts/phase_9_live_smoke/20260711_020713/`
showed immediate HTTP 503 responses for more than 80 minutes while backend CPU
and GPU utilization remained idle. The exact source mechanism was reconstructed
from backend image commit `edf89bd7c5554769bb36cbd049b6fbb98bcb9d41`
and upstream s2.cpp revision `2c33261938da1a41d713768b1b391b4d368d7d2c`:

1. The HTTP content provider holds `StreamContext::mtx` while checking
   `sink.is_writable()`.
2. On downstream disconnect it called `mark_cancelled()`, which attempted to
   lock the same non-recursive mutex.
3. The provider deadlocked before publishing cancellation; the synthesis
   worker later blocked on the same mutex and never reached the manual
   `server_busy->store(false)` cleanup.
4. Every later `/generate` admission therefore observed `server_busy=true` and
   returned HTTP 503 immediately.

The backend patch now avoids recursive locking and owns the busy flag with an
RAII guard, including exception paths. The wrapper emits the official Wyoming
`error` event for expected operational terminal failures and does not let those
failures escape as unobserved handler-task exceptions. Unexpected programming
errors remain visible.

Destructive disconnect/queue validation no longer shares `s2cpp-backend`.
`scripts/validate_phase_9_live.sh` requires an explicit backend image, canonical
digest, and non-production `PHASE9_TEST_GPU_UUID`; it creates a uniquely named
temporary backend, points the shadow wrapper only to that backend, performs a
valid-audio readiness preflight, preserves backend logs/inspect evidence, and
removes only containers created by that run. It fails closed rather than
falling back to the production backend or production GPU.

### Validation Inputs

```bash
PHASE9_TEST_IMAGE="<new-wrapper-image>" \
PHASE9_EXPECTED_DIGEST="<new-wrapper-sha256-digest>" \
PHASE9_BACKEND_IMAGE="<new-backend-image>" \
PHASE9_BACKEND_DIGEST="<new-backend-sha256-digest>" \
PHASE9_TEST_GPU_UUID="GPU-fcd97b9d-0c2b-3db7-6002-81a1e2c785ea" \
  bash scripts/validate_phase_9_live.sh
```

The production wrapper and backend must remain running and unchanged throughout.
The new images are validation candidates only until isolated Unraid validation
passes; do not deploy them to production and do not merge PR #2 beforehand.

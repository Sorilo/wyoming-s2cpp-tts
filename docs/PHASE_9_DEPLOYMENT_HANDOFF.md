# Phase 9 Deployment Handoff

## Source

- **Commit:** `476685f`
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
ghcr.io/sorilo/wyoming-s2cpp-tts:sha-476685f
```
**Build:** `docker build -t ghcr.io/sorilo/wyoming-s2cpp-tts:sha-476685f .`
**Push:** `docker push ghcr.io/sorilo/wyoming-s2cpp-tts:sha-476685f`

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

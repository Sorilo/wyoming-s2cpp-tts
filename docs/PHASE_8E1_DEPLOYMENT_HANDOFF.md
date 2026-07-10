# Phase 8E.1 Deployment Handoff

> **Do NOT deploy yet.** This is the manual procedure for when ready.

## Provisional Baseline

| Setting | Value |
|---|---|
| Model | `/models/s2-pro-q4_k_m.gguf` |
| Threads | `S2_THREADS=8` |
| Codec context | `S2_CODEC_CONTEXT_FRAMES=32` |
| Decode stride | `S2_STREAM_DECODE_STRIDE_FRAMES=32` |
| Holdback | `S2_STREAM_HOLDBACK_FRAMES=0` |
| Start buffer | `S2_STREAM_START_BUFFER_MS=0` |
| Initial buffer | `S2_INITIAL_BUFFER_MS=0` |
| Low latency | `S2_LOW_LATENCY=true` |
| Segment sentences | `S2_SEGMENT_SENTENCES=false` |
| Voice | `S2_DEFAULT_VOICE=cmu_bdl_male_us` |
| Voice dir | `S2_VOICE_DIR=/voices` |
| GPU layers | `S2_GPU_LAYERS=-1` |
| Codec on CPU | `S2_CODEC_CPU=false` |
| CPU pin | `--cpuset-cpus=0-15` (i9-13900K P-cores; remove on different HW) |

## Image Provenance

| Image | Tag | Digest |
|---|---|---|
| Backend | `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd` | `sha256:c29e41e59b470d58bf4b88c11c9ec753e00fa74a3bffbb003bc257fb9c6e46d9` |
| Wrapper (new) | `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-22db725` | Built from commit 22db725. Pull and verify with `docker inspect`. |
| Wrapper (rollback) | `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-9c134cc` | |

## Backend Changes (Unraid → Docker → s2cpp-backend → Edit)

1. Change `S2_MODEL` to `/models/s2-pro-q4_k_m.gguf`
2. Change `S2_THREADS` to `8`
3. Add `--cpuset-cpus=0-15` in Extra Parameters (i9-13900K specific)
4. Verify `S2_GPU_LAYERS=-1`, `S2_CODEC_CPU=false`
5. Verify model exists: `ls -lh /mnt/user/appdata/s2cpp/models/s2-pro-q4_k_m.gguf`

## Wrapper Changes (Unraid → Docker → wyoming-s2cpp-tts → Edit)

1. Update Repository to `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-22db725`
2. Set environment variables per baseline table above
3. Verify `/voices` mount: `/mnt/user/appdata/s2cpp/voices:/voices:ro`


## Restart Order

1. Stop `wyoming-s2cpp-tts`
2. Stop `s2cpp-backend`
3. Start `s2cpp-backend` — verify logs show `Launching: s2 --model /models/s2-pro-q4_k_m.gguf`
4. Start `wyoming-s2cpp-tts` — verify logs show `backend_start` with context=32, stride=32
5. In Home Assistant: Settings → Devices → Wyoming → Reload (three-dot menu)

## Verification

```bash
# Verify running images
docker inspect s2cpp-backend --format "{{.Config.Image}} {{.Image}}"
docker inspect wyoming-s2cpp-tts --format "{{.Config.Image}} {{.Image}}"

# Verify environment
docker inspect wyoming-s2cpp-tts --format "{{range .Config.Env}}{{println .}}{{end}}" | grep -E "S2_CODEC|S2_STRIDE|S2_LOW|S2_VOICE"
```

```bash
# Backend model loaded
docker logs s2cpp-backend 2>&1 | grep "Launching:"

# Wrapper backend_start
docker logs wyoming-s2cpp-tts 2>&1 | grep "backend_start"

# Wyoming discovery
# TCP connectivity check (Wyoming is not HTTP — use docker inspect instead)
docker inspect wyoming-s2cpp-tts --format "{{.Config.Image}} {{.Image}}"
```

## Listening Test

1. In Home Assistant: Settings → Voice assistants → Test TTS
2. Say: "Hello, this is a voice assistant quality test."
3. Listen for: tapping/blipping, word stretching, metallic artifacts, prosody
4. Note: backend first PCM ~1.35s is expected at stride 32 — not a blocker

## Rollback Procedure

```bash
# Backend
docker stop s2cpp-backend
# In Unraid: change model back to /models/s2-pro-q6_k.gguf, threads=0
docker start s2cpp-backend

# Wrapper
docker stop wyoming-s2cpp-tts
# In Unraid: change image to ghcr.io/sorilo/wyoming-s2cpp-tts:sha-9c134cc
docker start wyoming-s2cpp-tts
```

## Blocker Criteria (Stop and Roll Back)

- Wrapper fails to start
- Home Assistant cannot discover Wyoming TTS
- Voice `cmu_bdl_male_us` is missing
- Q4 model fails to load
- Context or stride not reflected in `backend_start` logs
- No progressive audio arrives
- Audible corruption, clipping, repeated syllables, severe cadence problems, dropouts
- Backend stays busy after completion
- Cancellation/recovery regresses
- Image provenance cannot be verified

## Before Starting Phase 9

- [ ] Baseline deployed and verified
- [ ] One successful HA satellite listening test
- [ ] End-to-end trace captured (VAD→STT→LLM→TTS→playback)
- [ ] Latency acceptable (2.0–3.0s initial range)
- [ ] No blocker criteria triggered

# Architecture

## Goal

A Home Assistant-compatible Wyoming Protocol TTS service backed by Fish Speech S2 Pro
through `s2.cpp` GGUF models running on an NVIDIA RTX 3080, deployed as two Docker
containers on the Unraid `sorilonet` network.

## Deployed service flow (verified)

```
Home Assistant (192.168.1.233)
  └─ Wyoming Protocol TCP → 192.168.1.45:10200
       └─ wyoming-s2cpp-tts wrapper container (CPU-only, sorilonet)
            ├─ Wyoming TCP server on tcp://0.0.0.0:10200
            ├─ Streaming TTS state machine:
            │    synthesize-start → synthesize-chunk(s) → synthesize-stop
            │    → AudioStart → AudioChunk(s) → AudioStop → synthesize-stopped
            └─ HTTP multipart/form-data → http://s2cpp-backend:3030/generate
                 └─ s2cpp-backend container (CUDA, sorilonet)
                      ├─ s2.cpp HTTP server on 0.0.0.0:3030
                      ├─ Fish Speech S2 Pro GGUF model
                      └─ NVIDIA RTX 3080 GPU
```

## Container design

The architecture uses **two separate containers** on the `sorilonet` Docker network:

### s2cpp-backend (CUDA)
- **Image:** `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b`
- Requires NVIDIA runtime, CUDA, and GPU access
- Runs `s2.cpp` in HTTP server mode on port 3030
- Mounts: `/models` (GGUF files), `/voices` (voice profiles)
- GPU: RTX 3080 with model offloading
- Generates `audio/L16; rate=44100; channels=1` raw PCM via multipart/form-data

### wyoming-s2cpp-tts (CPU-only wrapper)
- **Image:** `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-89ed2dc`
- Does NOT require NVIDIA runtime, CUDA, or GPU
- Runs the Python Wyoming TCP server on port 10200
- Translates Wyoming TTS requests into HTTP calls to the backend
- Implements the full Wyoming streaming TTS lifecycle:
  - Legacy `synthesize` (classic single request)
  - Streaming `synthesize-start` / `synthesize-chunk` / `synthesize-stop`
  - Always emits `synthesize-stopped` after final audio
- Exposes port 10200 to the host (LAN) for Home Assistant

## Home Assistant / Wyoming role

Home Assistant discovers the service at `192.168.1.45:10200` via the Wyoming Protocol
integration. It should not need to know about the s2.cpp backend, Fish Speech, GGUF
files, or CUDA.

The service advertises:
- Program: `wyoming-s2cpp-tts`
- Voice: `s2-pro` (en, zh)
- Streaming: `true`
- Audio: 44100 Hz, mono, s16le

## Queue and worker model

The current implementation uses single-active-synthesis with a bounded queue (default
max 3). Only one synthesis runs at a time to keep RTX 3080 VRAM predictable.

- `BARGE_IN_FRIENDLY=true`
- `CANCEL_ON_CLIENT_DISCONNECT=true` (placeholder, not runtime-verified)
- `CANCEL_ON_NEW_REQUEST=false`

Client-disconnect cancellation, backend request cancellation, and queue-busy/timeout
policies are **not yet runtime-verified** against the real backend.

## Latency measurement ownership

This repository can directly measure:
- TTS request receipt
- Backend first data (buffered: post-response; streaming: first non-empty chunk)
- First Wyoming AudioChunk (produced, not transmitted)
- Emitted bytes and chunk count
- Request duration

STT, LLM, VAD, and actual playback timestamps require Home Assistant or
satellite-side instrumentation.

## Cancellation and barge-in

The service is designed to be barge-in friendly (streaming chunks, configurable
cancellation flags), but true barge-in depends on the full Home Assistant Assist
stack: wake word, VAD, satellite behavior, and playback device interrupt support.

Real cancellation and barge-in testing with the HA satellite/player path is
pending — see Phase 6F on the roadmap.

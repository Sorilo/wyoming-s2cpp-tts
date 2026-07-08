# Architecture

## Goal

A Home Assistant-compatible Wyoming Protocol TTS service backed by Fish Speech S2 Pro through `s2.cpp` GGUF models running on an NVIDIA RTX 3080, deployed as two Docker containers on the Unraid `sorilonet` network.

## Deployed service flow (verified)

```text
Home Assistant (192.168.1.233)
  └─ Wyoming Protocol TCP → 192.168.1.45:10200
       └─ wyoming-s2cpp-tts wrapper container (CPU-only, sorilonet)
            ├─ Wyoming TCP server on tcp://0.0.0.0:10200
            ├─ Wyoming streaming TTS lifecycle:
            │    synthesize-start → synthesize-chunk(s) → synthesize-stop
            │    → AudioStart → AudioChunk(s) → AudioStop → synthesize-stopped
            └─ HTTP multipart/form-data → http://s2cpp-backend:3030/generate
                 └─ s2cpp-backend container (CUDA, sorilonet)
                      ├─ s2.cpp HTTP server on 0.0.0.0:3030
                      ├─ Fish Speech S2 Pro GGUF model
                      ├─ /voices persistent voice-profile mount
                      └─ NVIDIA RTX 3080 GPU
```

## Container design

The architecture uses **two separate containers** on the `sorilonet` Docker network.

### s2cpp-backend (CUDA)

- **Image:** `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b`
- Requires NVIDIA runtime, CUDA, and GPU access
- Runs `s2.cpp` in HTTP server mode on port 3030
- Mounts `/models` for GGUF/tokenizer assets and `/voices` for saved `.s2voice` profiles
- GPU: RTX 3080 with model offloading
- Generates `audio/L16; rate=44100; channels=1` raw PCM via `multipart/form-data`
- Verified backend endpoint: `POST /generate`

### wyoming-s2cpp-tts (CPU-only wrapper)

- **Image:** `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-89ed2dc`
- Does **not** require NVIDIA runtime, CUDA, or GPU
- Runs the Python Wyoming TCP server on port 10200
- Translates Wyoming TTS requests into HTTP multipart calls to the backend
- Implements the Home Assistant/Wyoming streaming-text lifecycle:
  - Legacy `synthesize` (classic single request)
  - Streaming `synthesize-start` / `synthesize-chunk` / `synthesize-stop`
  - Emits `synthesize-stopped` after successful streaming-session audio
- Exposes port 10200 to the host LAN for Home Assistant

## Home Assistant / Wyoming role

Home Assistant discovers the service at `192.168.1.45:10200` via the Wyoming Protocol integration. It does not need to know about the s2.cpp backend, Fish Speech, GGUF files, or CUDA.

The service currently advertises:

- Program: `wyoming-s2cpp-tts`
- Voice: `s2-pro` (en, zh)
- Streaming: `true`
- Audio: 44100 Hz, mono, s16le

## Streaming distinction

Wyoming protocol streaming is implemented and verified: the wrapper handles `synthesize-start`, `synthesize-chunk`, and `synthesize-stop`, then emits `AudioStart`, `AudioChunk`, `AudioStop`, and `synthesize-stopped` for Home Assistant.

Progressive backend-audio streaming is not currently used by the production handler: although `S2_STREAM` is parsed and `synthesize_s2cpp_streaming_tts_events()` / `generate_stream()` exist, the live handler still calls buffered `synthesize_s2cpp_tts_events()` via `generate_multipart()`, then sends Wyoming audio events.

Phase 7.5 is the planned work to make `S2_STREAM=true` select the progressive backend HTTP stream in the production Wyoming event handler. Until then, `S2_STREAM=true` must not be documented as sending backend bytes to Home Assistant as they are generated.

## Voice profile boundary

The pinned s2.cpp behavior to plan against is:

- `POST /generate`
- Reference audio plus exact reference transcript
- Saved voice selection through `voice` and `voice_dir`
- CLI voice profile creation with `--prompt-audio`, `--prompt-text`, `--voice`, `--save-voice`, and `--voice-dir`
- CLI voice listing with `--list-voices`

Do not claim an HTTP voice-management endpoint such as `/v1/voices` unless source inspection proves one exists.

## Queue and worker model

The current implementation uses single-active-synthesis with a bounded queue (default max 3). Only one synthesis runs at a time to keep RTX 3080 VRAM predictable.

- `BARGE_IN_FRIENDLY=true`
- `CANCEL_ON_CLIENT_DISCONNECT=true` (configured placeholder; not runtime-verified)
- `CANCEL_ON_NEW_REQUEST=false`

Client-disconnect cancellation, backend request cancellation, queue-busy/timeout policies, and controlled Wyoming failure behavior are **not yet runtime-verified** against the real backend. These are Phase 8 and Phase 9 responsibilities.

## Latency measurement ownership

This repository can directly measure:

- TTS request receipt
- Backend first data observed by the wrapper path being used
- First Wyoming `AudioChunk` produced by the wrapper
- Emitted bytes and chunk count
- Request duration

STT, LLM, VAD, and actual playback timestamps require Home Assistant or satellite-side instrumentation.

## Cancellation and barge-in

The service is designed to be barge-in friendly, but true barge-in depends on the full Home Assistant Assist stack: wake word, VAD, satellite behavior, and playback device interrupt support.

Real cancellation cleanup is planned for Phase 8. End-to-end barge-in testing with an actual Home Assistant satellite/player path is planned for Phase 10.

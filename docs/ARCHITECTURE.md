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

- **Image:** `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-974e220`
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
- Default voice: `s2-pro` (en, zh) — always present
- Discovered voices: each `.s2voice` profile in `/voices` as a selectable voice
- Streaming: `true`
- Audio: 44100 Hz, mono, s16le

Voice discovery scans `/voices` on every Describe and before validating synthesis
requests. New `.s2voice` files are discoverable without container rebuild or restart.
Home Assistant may cache Describe results and require a Wyoming integration reload
to see newly dropped-in voices.

## Streaming distinction

Wyoming protocol streaming is implemented and verified: the wrapper handles `synthesize-start`, `synthesize-chunk`, and `synthesize-stop`, then emits `AudioStart`, `AudioChunk`, `AudioStop`, and `synthesize-stopped` for Home Assistant.

Progressive backend-audio streaming is now wired (Phase 7.5A). When `S2_STREAM=true`, the production handler uses `synthesize_s2cpp_streaming_tts_events()` / `generate_stream()` to yield Wyoming audio events progressively as backend transport chunks arrive — ``AudioStart`` is emitted only after backend metadata is validated, ``AudioChunk`` events are emitted as bytes arrive, and ``AudioStop`` follows clean stream completion. When `S2_STREAM=false`, the existing buffered `generate_multipart()` path is preserved.

Time-to-first-audio with the real backend was previously observed at ~3.8 seconds (both first-audio and total request). Phase 7.5A does not guarantee a major latency reduction; measure live latency after deployment (Phase 7.5B).

## Voice profile boundary

The pinned s2.cpp behavior to plan against is:

- `POST /generate`
- Reference audio plus exact reference transcript
- Saved voice selection through `voice` and `voice_dir`
- CLI voice profile creation with `--prompt-audio`, `--prompt-text`, `--voice`, `--save-voice`, and `--voice-dir`
- CLI voice listing with `--list-voices`

Do not claim an HTTP voice-management endpoint such as `/v1/voices` unless source inspection proves one exists.

The wrapper mounts `/voices` read-only and discovers `.s2voice` profiles on every
Describe and synthesis request.  Voice selection follows this priority:

1. Client-requested voice (Wyoming Synthesize ``voice.name``).
2. ``S2_DEFAULT_VOICE`` (when configured and discovered).
3. Generic ``s2-pro`` fallback (no custom voice fields sent).

Unknown or unsafe voice IDs are rejected with a clear error.

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

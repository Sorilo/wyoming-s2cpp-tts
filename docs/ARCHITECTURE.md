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

- **Image:** `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd`
- Requires NVIDIA runtime, CUDA, and GPU access
- Runs `s2.cpp` in HTTP server mode on port 3030
- Mounts `/models` for GGUF/tokenizer assets and `/voices` for saved `.s2voice` profiles
- GPU: RTX 3080 with model offloading
- Generates `audio/L16; rate=44100; channels=1` raw PCM via `multipart/form-data`
- Verified backend endpoint: `POST /generate`

### wyoming-s2cpp-tts (CPU-only wrapper)

- **Image:** `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-9c134cc`
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


## Streaming decode stride tuning (Phase 8C)

The s2.cpp backend interprets ``low_latency=true`` as approximately
``stream_decode_stride_frames=1`` and ``stream_holdback_frames=0``.
With ``codec_decode_context_frames=4``, stride 1 may cause excessive
repeated codec decoding and CUDA launch overhead on the RTX 3080.

### Tuning parameters

| Parameter | Env var | Default | Range | Description |
|---|---|---|---|---|
| Decode stride | ``S2_STREAM_DECODE_STRIDE_FRAMES`` | 4 | 1--64 | Frames decoded per streaming step |
| Holdback | ``S2_STREAM_HOLDBACK_FRAMES`` | 0 | ≥0 | Frames held before first chunk |
| Start buffer | ``S2_STREAM_START_BUFFER_MS`` | 0 | ≥0 | Initial buffer before streaming begins |
| Low latency | ``S2_LOW_LATENCY`` | true | bool | Backend low-latency streaming mode |

### Difference from wrapper initial buffer

- **Codec context** (``codec_decode_context_frames``): how many prior frames
  the codec re-decodes for continuity during streaming generation.
- **Decode stride** (``stream_decode_stride_frames``): how many new frames
  are decoded per step; higher stride reduces CUDA launch overhead.
- **Holdback** (``stream_holdback_frames``): backend-side frame holdback
  before first emission.
- **Backend start buffer** (``stream_start_buffer_ms``): backend-side
  initial accumulation before streaming begins.
- **Wrapper initial buffer** (``S2_INITIAL_BUFFER_MS`` et al.): wrapper-side
  PCM buffering before emitting ``AudioStart``.

### Why stride 1 may be inefficient

With ``low_latency=true``, the backend defaults to stride 1 — one frame per
CUDA kernel launch.  At 44100 Hz with codec context 4, this means ~11,025
CUDA launches per second of audio, each re-decoding 4 context frames.
Stride 4 reduces this to ~2,756 launches (4x reduction) with the same
context re-decode cost per stride step.

⚠️ Stride 4 is a **candidate only** — real RTX 3080 benchmarks are required
to confirm actual RTF improvement.  Streaming s2.cpp repeatedly re-decodes
codec context and is not yet fully stateful/incremental.

### Benchmarking

The benchmark harness contacts the s2.cpp backend **directly** — no wrapper
rebuild is required.  The running backend container is all you need.

```bash
# On Unraid host (safe, no container changes):
bash scripts/run_realtime_tuning_unraid.sh --benchmark
```

### Deploying to Home Assistant / Wyoming

**A new wrapper image must be built and deployed** before Home Assistant can
use the stride tuning settings.  The current production wrapper
(``ghcr.io/sorilo/wyoming-s2cpp-tts:sha-9c134cc``) does not understand
``S2_STREAM_DECODE_STRIDE_FRAMES`` or the other new environment variables.

After a new wrapper image is published:

```bash
# See what settings to apply (informational only):
bash scripts/run_realtime_tuning_unraid.sh --apply 4 --yes
```

### RTF interpretation

| RTF | Meaning |
|---|---|
| < 1.0 | Faster than real time — can keep up with playback |
| = 1.0 | Exactly real time — marginal |
| > 1.0 | Slower than playback — will stutter |

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
- `CANCEL_ON_CLIENT_DISCONNECT=true` (runtime-verified through Phase 8B2 backend cancellation)
- `CANCEL_ON_NEW_REQUEST=false`

Client-disconnect and backend request cancellation are runtime-verified through Phase 8B2: abandoned requests are recorded once, generation exits promptly, final decode is skipped, and `server_busy` is released. Queue-busy/timeout policies and controlled Wyoming failure behavior remain Phase 9 responsibilities.

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

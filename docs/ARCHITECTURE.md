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

- **Image:** `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-75936bc`
- Requires NVIDIA runtime, CUDA, and GPU access
- Runs `s2.cpp` in HTTP server mode on port 3030
- Mounts `/models` for GGUF/tokenizer assets and `/voices` for saved `.s2voice` profiles
- GPU: RTX 3080 with model offloading
- Generates `audio/L16; rate=44100; channels=1` raw PCM via `multipart/form-data`
- Verified backend endpoint: `POST /generate`

### wyoming-s2cpp-tts (CPU-only wrapper)

- **Image:** `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-75936bc`
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

✅ Real RTX 3080 stride benchmarks completed (strides 1-24, Q6_K model).
Stride 4 is the preferred Q6_K compromise (RTF ~1.13, first PCM ~251 ms).
Quant comparison (Q5_K_M, Q4_K_M) and human listening still pending.
Streaming s2.cpp repeatedly re-decodes
codec context and is not yet fully stateful/incremental.

### Benchmarking

The benchmark harness contacts the s2.cpp backend **directly** — no wrapper
rebuild is required.  The running backend container is all you need.

```bash
# On Unraid host (safe, no container changes):
bash scripts/run_realtime_tuning_unraid.sh --benchmark
```

### Deploying to Home Assistant / Wyoming

The production wrapper
(``ghcr.io/sorilo/wyoming-s2cpp-tts:sha-75936bc``) supports the stride tuning
environment variables. Preserve the validated production values when editing
the Unraid template.

For controlled future tuning:

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

Client-disconnect and backend request cancellation are runtime-verified: abandoned requests are recorded once, generation exits promptly, final decode is skipped, and `server_busy` is released. Phase 9 added deterministic bounded FIFO admission, backend-busy retries, queue/synthesis deadlines, and controlled Wyoming failures while preserving one active synthesis.

### Speech domain model (Phase 9B)

Phase 9B extracted the queue and scheduling logic into explicit domain objects in `app/speech/`:

- **`SpeechMetadata`**: immutable dataclass carrying optional descriptive metadata (voice, trigger, text fingerprint) plus reserved inert fields for future semantic priority and replacement.
- **`SpeechRequest`**: immutable dataclass representing one admitted unit of speech with synthesis/connection IDs, text (plaintext excluded from repr/snapshots/logs), and metadata.
- **`SpeechScheduler`**: sole owner of admission, FIFO activation, queue depth, active task identity, cancellation, and release. Wyoming handlers are protocol adapters that create `SpeechRequest` objects and submit operations to the scheduler.
- **`SpeechState`**: closed lifecycle enum (`CREATED → WAITING → ACTIVE → COMPLETED/CANCELLED/TIMED_OUT/FAILED`) with idempotent terminal transitions.
- **`SynthesisSession`**: per-request protocol state tracking `AudioStart`/`AudioStop` emission, streaming eligibility, disconnect state, and resource cleanup.

Observable behavior is unchanged from Phase 9. No image is published; production remains on wrapper `sha-7db26b7` and backend `sha-6e629d0`.


## Progressive phrase synthesis (Phase 9.5)

Phase 9.5 enables progressive TTS synthesis from streaming LLM text input:
each complete phrase begins backend synthesis as soon as its terminal
punctuation arrives, without waiting for the full response.

### Components

- **PhraseAccumulator** (``app/speech/phrases.py``): Purely functional
  streaming text parser.  Incoming chunks are buffered and split at
  deterministically confirmed sentence boundaries.  Terminal set:
  ``.!?。！？``.  Decimal periods (digits on both sides), known
  abbreviations (case‑insensitive), and ellipsis runs are protected.
  Configurable bounds: soft fallback 160, maximum phrase 320, retained
  buffer 640 characters.  Chunking‑invariant: identical output regardless
  of where chunk boundaries fall.

- **AudioEnvelope** (``app/speech/envelope.py``): Logical Wyoming audio
  normaliser.  Emits ``AudioStart`` exactly once (first encountered
  format locked), suppresses internal phrase ``AudioStop`` events,
  rebuilds chunk timestamps from cumulative emitted PCM frames, validates
  frame alignment, and closes with one terminal event (``AudioStop`` on
  success; ``AudioStop`` then Wyoming ``Error`` on failure after partial
  audio).

- **StreamingCoordinator** (``app/speech/stream_coordinator.py``):
  Connection‑owned coordinator.  A background synthesis task consumes
  completed phrases from the accumulator, submits each through
  ``SpeechScheduler.run()`` one at a time (serial FIFO, no overlapping
  backend calls), and delivers output events through a bounded capacity‑1
  async queue.  Supports both progressive text feeding (``feed_text`` /
  ``feed_done``) and buffered legacy compatibility (``stream()``).

### Handler integration

- ``SynthesizeStart`` → creates coordinator, starts background consumer
  task.
- ``SynthesizeChunk`` → feeds text to accumulator immediately; tracks
  non‑whitespace chunks for compatibility deduplication.
- ``SynthesizeStop`` → feeds deferred compat text only if no streaming
  chunks arrived, flushes residual, awaits consumer, emits
  ``SynthesizeStopped`` if client still connected.
- Disconnect/cancellation → cancels coordinator, drains pending phrases,
  closes generators.

### Timeout and counter semantics

- Queue‑wait and synthesis deadlines apply **per phrase** (inherited from
  ``SpeechScheduler``).  No new total‑stream timeout is added.
- Cumulative counters (``admitted``, ``completed``, etc.) count individual
  **phrase operations**, not logical Wyoming requests.  This is intentional
  and documented — counters previously counted scheduler operations per
  logical request; progressive streaming creates multiple scheduler
  operations per request.

### Compatibility deduplication

- Streaming is authoritative once any non‑whitespace chunk is accepted.
- If no non‑whitespace chunk arrived, the deferred compatibility text is
  used once on stop.
- No overlap comparison, prefix matching, or plaintext history.
- Duplicate ``SynthesizeStart`` returns ``stream_already_active`` error.
- Orphan chunk/stop events are no‑ops.
- Legacy standalone ``Synthesize`` path is unchanged.

### Cancellation and drain

- ``cancel()`` atomically marks the session closed, clears accumulator and
  pending handoff state, cancels the active scheduler connection, cancels
  and awaits the background task, and puts a sentinel on the output queue
  to unblock consumers.
- ``drain()`` prevents later phrases from starting; the currently active
  phrase (if any) receives the Phase 9C grace behaviour.
- ``S2_STREAM=false`` selects buffered per‑phrase backend transport; it
  does **not** disable progressive phrase coordination.


## Graceful shutdown (Phase 9C)

The service has an explicit ServiceCoordinator lifecycle owner with a
LifecycleState machine: STARTING, RUNNING, DRAINING, STOPPING, STOPPED, FAILED.
SIGTERM/SIGINT trigger shutdown exactly once (idempotent).  Readiness flips
false immediately.  Scheduler drains: cancels queued work, allows active
synthesis a grace period (SHUTDOWN_GRACE_TIMEOUT_SEC, default 30s, range
(0, 300]), force-cancels after expiry.  All tracked handlers are closed.

## Optional admin HTTP server (Phase 9C)

Disabled by default, loopback-bound at 127.0.0.1:10201.  Read-only endpoints:
GET /livez (200 while alive), GET /readyz (200 only RUNNING, 503 otherwise),
GET /status (sanitized JSON: state, readiness, uptime, scheduler depth/pending/
active, connection count, cumulative counters), GET /metrics (sanitized JSON
with independent schema and cumulative process-lifetime counters: admitted,
rejected, completed, cancelled-queued, cancelled-active, timed-out, failed,
backend-busy-retries).

Config: ADMIN_HTTP_ENABLED=false, ADMIN_HTTP_HOST=127.0.0.1,
ADMIN_HTTP_PORT=10201, ADMIN_HTTP_READ_TIMEOUT_SEC=5.0,
ADMIN_HTTP_MAX_HEADER_SIZE=8192, ADMIN_HTTP_MAX_BODY_SIZE=65536.

Safety: No mutating endpoints (GET-only, 405 with Allow: GET for others).
No plaintext, audio, secrets, tokens, IDs, or mutable objects.  Bounded
cumulative time/size HTTP parsing.  Bind failure is non-fatal.  Do not
expose admin port broadly without network controls.

Docker guidance: set ADMIN_HTTP_ENABLED=true and publish port 10201 only
if needed.  Use loopback binding or firewall rules for security.

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

Client-disconnect and backend cancellation cleanup were implemented through Phase 8B2,
strengthened in Phases 9–9.5, and validated end to end in Phase 10: a Wyoming
disconnect cancels wrapper and native backend work, releases scheduler ownership,
and permits a correlated follow-up request. Stock HA 2026.7.2 with Voice PE 26.6.0
does not provide full one-wake barge-in: generic media stop targets the normal
media pipeline while Assist uses the announcement pipeline, and HA keeps the TTS
producer alive. Full physical interruption plus producer cancellation is deferred
to an announcement-aware upstream lifecycle or Cortex-Satellite. See
`docs/validation/PHASE_10_CLOSURE.md`.


## Operations documentation (Phase 11)

The following operational documents define the v0.1.0 deployment, security,
and lifecycle posture:

- **`docs/SECURITY.md`** — security model: private backend network,
  no-secrets-in-images policy, admin HTTP safety, image pinning.
- **`docs/UPGRADE_ROLLBACK.md`** — upgrade paths, backup procedures,
  immutable image pins, supported version transitions.
- **`docs/RELEASE.md`** — release checklist, versioning, tagging,
  image publication, rollback criteria.
- **`docs/INSTALL.md`** — fresh install instructions (Compose-first).
- **`docs/UNRAID_INSTALL.md`** — Unraid-specific notes (Compose-first,
  sanitized placeholders, backup/rollback links).
- **`docs/HOME_ASSISTANT_SETUP.md`** — Home Assistant integration with
  voice selection, streaming status, and the documented stock HA
  2026.7.2 + Voice PE 26.6.0 one-wake NOT PASS limitation.

# wyoming-s2cpp-tts

`wyoming-s2cpp-tts` is a local Home Assistant Wyoming Protocol TTS service that runs Fish Speech S2 Pro through `s2.cpp` GGUF models on a home Unraid server.

The current deployed baseline is a two-container system:

```text
Home Assistant (192.168.1.233)
  -> Wyoming Protocol TCP at 192.168.1.45:10200
  -> CPU-only wyoming-s2cpp-tts wrapper container
  -> HTTP multipart/form-data at http://s2cpp-backend:3030/generate
  -> CUDA s2cpp-backend container
  -> Fish Speech S2 Pro GGUF model on NVIDIA RTX 3080
```

Real Home Assistant TTS playback has been deployed and verified: Home Assistant discovers the Wyoming service, shows the `s2-pro` voice, completes the streaming TTS lifecycle, and audibly plays real speech.

## Target hardware and model

- Server: Unraid home server (`192.168.1.45`)
- Home Assistant VM: `192.168.1.233`
- Docker network: `sorilonet`
- GPU target: NVIDIA RTX 3080 10 GB
- CPU: Intel i9-13900K
- RAM: 96 GB DDR4
- Persistent appdata root: `/mnt/user/appdata`
- Model path inside backend container: `/models/s2-pro-q4_k_m.gguf`
- Host voices directory: `/mnt/user/appdata/s2cpp/voices`
- Backend voices mount: `/voices`

The current verified RTX 3080 runtime baseline is `/models/s2-pro-q4_k_m.gguf` with codec context 32, decode stride 32, and 8 threads. Hardware-upgrade benchmarking is post-v0.1 work.


## Real-time stride tuning (Phase 8C)

The wrapper code now supports configurable streaming decode stride for
RTX 3080 performance optimisation. The benchmark harness contacts the s2.cpp
backend directly — no wrapper rebuild is required to run the stride sweep.
Phase 9 validated these settings through an isolated wrapper/backend candidate pair; that validation did not deploy the candidates to production. The s2.cpp backend with ``low_latency=true`` defaults to stride 1 (one frame per CUDA kernel launch), which may cause excessive overhead with ``codec_decode_context_frames=4``.

### Quick benchmark (on Unraid host)

```bash
# Safe: runs benchmark only, no container changes
bash scripts/run_realtime_tuning_unraid.sh --benchmark
```

This sweeps stride values (1, 2, 4, 8) against the live backend, measures real-time factor (RTF), and produces a recommendation. **RTF alone does not guarantee audio quality** — listen to the generated PCM files before applying any settings.

### New configuration variables

| Variable | Default | Range | Description |
|---|---|---|---|
| ``S2_STREAM_DECODE_STRIDE_FRAMES`` | 4 | 1--64 | Frames decoded per streaming step |
| ``S2_STREAM_HOLDBACK_FRAMES`` | 0 | ≥0 | Frame holdback before first chunk |
| ``S2_STREAM_START_BUFFER_MS`` | 0 | ≥0 | Initial backend buffer (ms) |
| ``S2_LOW_LATENCY`` | true | bool | Backend low-latency mode |

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#streaming-decode-stride-tuning-phase-11) for the full explanation of stride vs. context vs. holdback vs. buffer.

## Current verified deployment

| Component | Value |
| --- | --- |
| Backend container | `s2cpp-backend` |
| Backend image | `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-6e629d0` |
| Backend endpoint | `http://s2cpp-backend:3030/generate` |
| Backend contract | `multipart/form-data` only; raw `audio/L16; rate=44100; channels=1` |
| Wrapper container | `wyoming-s2cpp-tts` |
| Wrapper image | `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-7db26b7` |
| Wyoming endpoint | `tcp://0.0.0.0:10200` inside container; `192.168.1.45:10200` from Home Assistant |
| Home Assistant result | Discovery succeeds; `s2-pro` is visible; real speech is audible |
| Test baseline | Phase 9B: 940 passed, 0 failed, 0 skipped in the standard suite; 14 Unraid shell-behavior tests remain a separate environment-specific invocation |

## Current architecture

Phase 9B extracted speech identity, lifecycle, FIFO admission, cancellation, and session cleanup into the focused `app/speech/` package. `SpeechScheduler` owns scheduling state through a public API, while Wyoming handlers remain protocol adapters. This was a source-only refactor: externally observable behavior and the deployed Phase 9 images remain unchanged.


The production deployment intentionally separates CPU-only Wyoming protocol handling from GPU inference:

- The **wrapper** runs the Python Wyoming TCP server and does not require CUDA, NVIDIA runtime, GGUF files, or GPU access.
- The **backend** runs `s2.cpp` in HTTP server mode with CUDA and the mounted model/tokenizer assets.
- Home Assistant only talks to the wrapper on TCP port 10200.
- The wrapper talks to the backend through the Docker network at `http://s2cpp-backend:3030/generate`.
- The backend expects `multipart/form-data`; JSON requests are not valid for the deployed backend.

## Streaming status

Wyoming protocol streaming is implemented and verified: the wrapper handles `synthesize-start`, `synthesize-chunk`, and `synthesize-stop`, then emits `AudioStart`, `AudioChunk`, `AudioStop`, and `synthesize-stopped` for Home Assistant.

Progressive backend-audio streaming is now wired (Phase 7.5A). When `S2_STREAM=true`, the production handler uses `synthesize_s2cpp_streaming_tts_events()` / `generate_stream()` to yield Wyoming audio events progressively as backend transport chunks arrive. When `S2_STREAM=false`, the existing buffered `generate_multipart()` path is preserved unchanged.

Phase 9 validation passed three deliberate disconnect/recovery cycles with valid recovery audio, released busy state, no persistent HTTP 503 latch, and no unobserved task-exception or disconnect-cleanup warning. The validated images are deployed. Final short and long direct Wyoming requests and the Home Assistant VM smoke passed with audible intelligible speech, zero restarts, queue depth zero, active GPU inference, and clean logs.

## Graceful shutdown (Phase 9C)

The service handles SIGTERM/SIGINT with a bounded graceful shutdown sequence:
drain queued work, allow active synthesis a grace period (default 30s,
configurable via SHUTDOWN_GRACE_TIMEOUT_SEC), then force-cancel and exit
cleanly.  Repeated signals are idempotent.

## Optional admin HTTP server (Phase 9C)

An optional read-only admin HTTP listener provides operational visibility.
It is **disabled by default** and loopback-bound (127.0.0.1:10201).

### Endpoints

| Endpoint | Purpose | Status |
|---|---|---|
| GET /livez | Liveness — process alive | 200 |
| GET /readyz | Readiness — accepting traffic | 200 only while RUNNING; 503 otherwise |
| GET /status | Sanitized JSON operational snapshot | 200 |
| GET /metrics | Sanitized JSON cumulative metrics | 200 |

### Enabling

Set `ADMIN_HTTP_ENABLED=true`. The defaults are `ADMIN_HTTP_HOST=127.0.0.1` and `ADMIN_HTTP_PORT=10201`; optional parser controls are `ADMIN_HTTP_READ_TIMEOUT_SEC=5.0`, `ADMIN_HTTP_MAX_HEADER_SIZE=8192`, and `ADMIN_HTTP_MAX_BODY_SIZE=65536`.

No mutating endpoints exist.  No plaintext, audio, secrets, tokens, or IDs
are exposed.  Bind failure does not prevent service startup.

### Docker / Unraid

Set ADMIN_HTTP_ENABLED=true and publish port 10201 only if needed.
Use loopback binding or firewall rules to avoid exposing admin endpoints
broadly.

## Running locally for development

The repository default remains the safe fake backend:

```text
TTS_BACKEND=fake
WYOMING_URI=tcp://0.0.0.0:10200
```

Start the development server with:

```bash
python -m app.main
```

Expected startup message for the default fake path:

```text
Wyoming TTS server listening on tcp://0.0.0.0:10200 with backend=fake
```

To point a local wrapper process at an already-running s2.cpp backend, set:

```bash
export TTS_BACKEND=s2cpp
export S2_HOST=s2cpp-backend
export S2_PORT=3030
export S2_VOICE_DIR=/voices
export S2_DEFAULT_VOICE=cmu_bdl_male_us
python -m app.main
```

In the verified deployment, the production wrapper container sets `TTS_BACKEND=s2cpp`; with `S2_STREAM=true` it streams backend PCM progressively and sends Wyoming audio events as chunks arrive.

## Direct backend smoke testing

Use the smoke harness only when a compatible backend is already running:

```bash
.venv/bin/python scripts/smoke_s2cpp_generate.py \
  --run-real \
  --require-backend \
  --endpoint s2cpp-backend:3030 \
  --json
```

The verified real backend contract is raw `audio/L16; rate=44100; channels=1` with `X-Audio-Encoding=pcm_s16le`, `X-Audio-Channels=1`, and `X-Audio-Sample-Rate=44100`.

## Testing

Current Phase 9C application-suite baseline: **1112 passed, 0 failed, 0 skipped**, excluding the 14 tests in `tests/test_realtime_tuning_unraid.py`. Those environment-specific shell-behavior checks—including fake-`nvidia-smi` cases—remain a separate invocation. The historical Phase 9 acceptance baseline was 876 passed.

Useful focused checks:

```bash
python -m pytest tests/test_s2_client.py tests/test_wyoming_s2cpp_backend.py -q
python -m pytest tests/test_streaming_protocol.py -q
python -m pytest tests/test_dockerfile_cuda.py tests/test_dockerfile_wrapper.py -q
```

No ordinary test should contact a real backend unless explicitly opted in through the smoke harness.

## Current limitations and remaining work

- Six custom `.s2voice` voice profiles are available from Phase 7A (CMU ARCTIC). Voice discovery and selection through Home Assistant is implemented in Phase 7B.
- Saved voice selection uses `voice` and `voice_dir` multipart fields; CLI voice creation uses `--prompt-audio`, `--prompt-text`, `--voice`, `--save-voice`, `--voice-dir`; CLI voice listing uses `--list-voices`. There is no HTTP voice-management API.
- Drop-in discovery: new `.s2voice` files placed in `/voices` are discoverable without rebuilding or restarting the wrapper. Home Assistant may require a Wyoming integration reload to see new voices.
- ~~True progressive backend HTTP audio streaming in the production handler is future Phase 7.5 work.~~ ✅ Phase 7.5A complete. Live latency verification is Phase 7.5B.
- Phase 8 client disconnect cleanup, HTTP stream closure, and backend cancellation work is complete.
- Phase 9 queue admission, HTTP 503 busy retry, queue/synthesis timeouts, controlled Wyoming failures, and disconnect recovery are implemented, merged, validated, deployed, and production-smoke verified. Phase 9 is closed.
- Phase 9C adds graceful shutdown (SIGTERM/SIGINT, bounded grace, scheduler drain) and an optional read-only admin HTTP server (/livez, /readyz, /status, /metrics). Source-only — no image published or deployed.
- End-to-end barge-in with a real Home Assistant satellite/player path is future Phase 10 work.
- STT, LLM, VAD, and actual playback timestamps require Home Assistant/upstream/client instrumentation or a correlated end-to-end test harness.

Do not claim end-to-end latency, cancellation, barge-in, custom voice management, or production release readiness until those phases have been implemented and verified.

## Historical implementation notes

Earlier phases remain useful implementation history, but they are no longer the current deployment baseline:

- Phase 0-4: repository scaffold, fake Wyoming server, s2.cpp client, wrapper container scaffold, and CUDA/Unraid planning.
- Phase 5A-5D: multipart client support, streaming client interfaces, streamed audio-to-Wyoming helpers, and TTS-side metrics.
- Phase 5.5A/5.5B: opt-in real-backend smoke harness and real backend contract verification.
- Phase 6A: CUDA backend image built, published, and deployed.
- Phase 6B0: CPU-only wrapper image and GHCR workflow.
- Phase 6B1: production wrapper changed from JSON to multipart/form-data.
- Phase 6C: Wyoming streaming TTS state machine fixed Home Assistant preview hangs.
- Phase 6D: live Home Assistant deployment verified real audible speech.

See [`docs/ROADMAP.md`](docs/ROADMAP.md), [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), and [`docs/NEXT_GOAL_PROMPTS.md`](docs/NEXT_GOAL_PROMPTS.md) for the governing forward plan.

## GitHub remote

The project remote is expected to be GitHub. Do not force-push, and do not publish images or change running containers unless the current phase explicitly authorizes it.

# wyoming-s2cpp-tts

`wyoming-s2cpp-tts` is a local Home Assistant Wyoming Protocol TTS service that runs Fish Speech S2 Pro through `s2.cpp` GGUF models on a home Unraid server.

The current deployed baseline is a two-container system:

```text
Home Assistant (192.168.x.x)
  -> Wyoming Protocol TCP at <host>:10200
  -> CPU-only wyoming-s2cpp-tts wrapper container
  -> HTTP multipart/form-data at http://s2cpp-backend:3030/generate
  -> CUDA s2cpp-backend container
  -> Fish Speech S2 Pro GGUF model on NVIDIA RTX 3080
```

Real Home Assistant TTS playback has been deployed and verified: Home Assistant discovers the Wyoming service, shows the `s2-pro` voice, completes the streaming TTS lifecycle, and audibly plays real speech.

## Quick start with Docker Compose

```bash
# 1. Copy and edit environment
cp .env.example .env
# Edit .env to set your host paths and preferences

# 2. Start both containers
docker compose up -d

# 3. Home Assistant -> Wyoming Protocol integration -> <your-host>:10200
```

See [compose.yaml](compose.yaml) and [.env.example](.env.example) for the full configuration.

## Target hardware and model

- Server: Unraid home server
- Home Assistant VM
- Docker network: `s2cpp-net` (shared bridge; backend port 3030 is not host-published)
- GPU target: NVIDIA RTX 3080 10 GB
- CPU: Intel i9-13900K
- RAM: 96 GB DDR4
- Persistent appdata root: `/mnt/user/appdata`
- Model path inside backend container: `/models/s2-pro-q4_k_m.gguf`
- Host voices directory: `/mnt/user/appdata/s2cpp/voices`
- Backend voices mount: `/voices`

The current verified RTX 3080 runtime baseline is `/models/s2-pro-q4_k_m.gguf` with codec context 32, decode stride 32, and 8 threads. Hardware-upgrade benchmarking is post-v0.1 work.

## Current verified deployment

| Component | Value |
| --- | --- |
| Backend container | `s2cpp-backend` |
| Backend image | `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-6e629d0` |
| Backend endpoint | `http://s2cpp-backend:3030/generate` |
| Backend contract | `multipart/form-data` only; raw `audio/L16; rate=44100; channels=1` |
| Wrapper container | `wyoming-s2cpp-tts` |
| Wrapper image | `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-7db26b7` |
| Wyoming endpoint | `tcp://0.0.0.0:10200` inside container; `<host>:10200` from Home Assistant |
| Home Assistant result | Discovery succeeds; `s2-pro` is visible; real speech is audible |
| Test baseline | 1505 pass, 0 fail, 7 skip (LiveArtifactIntegrity opt-in) |

## Current architecture

The production deployment intentionally separates CPU-only Wyoming protocol handling from GPU inference:

- The **wrapper** runs the Python Wyoming TCP server and does not require CUDA, NVIDIA runtime, GGUF files, or GPU access.
- The **backend** runs `s2.cpp` in HTTP server mode with CUDA and the mounted model/tokenizer assets.
- Home Assistant only talks to the wrapper on TCP port 10200.
- The wrapper talks to the backend through the Docker network at `http://s2cpp-backend:3030/generate`.
- The backend expects `multipart/form-data`; JSON requests are not valid for the deployed backend.

Phase 9B extracted speech identity, lifecycle, FIFO admission, cancellation, and session cleanup into the focused `app/speech/` package. `SpeechScheduler` owns scheduling state through a public API, while Wyoming handlers remain protocol adapters.

## Streaming status

Wyoming protocol streaming is implemented and verified: the wrapper handles `synthesize-start`, `synthesize-chunk`, and `synthesize-stop`, then emits `AudioStart`, `AudioChunk`, `AudioStop`, and `synthesize-stopped` for Home Assistant.

Progressive backend-audio streaming is wired (Phase 7.5A). When `S2_STREAM=true`, the production handler uses `synthesize_s2cpp_streaming_tts_events()` / `generate_stream()` to yield Wyoming audio events progressively as backend transport chunks arrive.

## Graceful shutdown (Phase 9C)

The service handles SIGTERM/SIGINT with a bounded graceful shutdown sequence:
drain queued work, allow active synthesis a grace period (default 30s,
configurable via SHUTDOWN_GRACE_TIMEOUT_SEC), then force-cancel and exit
cleanly. Repeated signals are idempotent.

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

No mutating endpoints exist. No plaintext, audio, secrets, tokens, or IDs
are exposed. Bind failure does not prevent service startup.

## Phase 10: End-to-end barge-in (VALIDATED)

Repository-owned disconnect cancellation, native backend abort, scheduler
cleanup, follow-up recovery, and overlap recovery all passed against deployed
images. However, **stock Home Assistant 2026.7.2 with Voice PE 26.6.0 does NOT
pass full one-wake barge-in**: generic `media_player.media_stop` targets the
normal media pipeline while Assist uses the announcement pipeline, and HA keeps
the TTS producer alive. Full physical interruption plus producer cancellation
is deferred to an announcement-aware upstream lifecycle or Cortex-Satellite.

See [docs/validation/PHASE_10_CLOSURE.md](docs/validation/PHASE_10_CLOSURE.md).

## Progressive phrase synthesis (Phase 9.5)

The wrapper synthesises streaming LLM text **progressively** — each complete
phrase begins backend synthesis as soon as its terminal punctuation arrives,
without waiting for the full LLM response. Audio continuity (one `AudioStart`,
continuous timestamps, one `AudioStop`) and scheduler serialisation are preserved.

## Running locally for development

```text
TTS_BACKEND=fake
WYOMING_URI=tcp://0.0.0.0:10200
```

Start the development server with:

```bash
python -m app.main
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

In the verified deployment, the production wrapper container sets `TTS_BACKEND=s2cpp`; with `S2_STREAM=true` it streams backend PCM progressively.

## Testing

Useful focused checks:

```bash
python -m pytest tests/test_s2_client.py tests/test_wyoming_s2cpp_backend.py -q
python -m pytest tests/test_streaming_protocol.py -q
python -m pytest tests/test_dockerfile_cuda.py tests/test_dockerfile_wrapper.py -q
```

No ordinary test should contact a real backend unless explicitly opted in through the smoke harness.

## Current limitations and remaining work

- Six custom `.s2voice` voice profiles are available from Phase 7A (CMU ARCTIC).
- Voice discovery and selection through Home Assistant is implemented (Phase 7B).
- Drop-in discovery: new `.s2voice` files placed in `/voices` are discoverable without rebuilding or restarting.
- ✅ Client disconnect, cleanup, and backend cancellation (Phase 8–8B2).
- ✅ Queue admission, HTTP 503 retry, timeouts, disconnect recovery (Phase 9).
- ✅ Progressive phrase synthesis (Phase 9.5).
- ✅ Graceful shutdown and admin HTTP server (Phase 9C).
- ✅ Repository barge-in contracts validated (Phase 10).
- **NOT PASSED**: Stock HA 2026.7.2 + Voice PE 26.6.0 one-wake barge-in (announcement pipeline limitation).
- Phase 11: Faster-Whisper/full Assist pipeline integration and latency measurement.
- Phase 12: Comprehensive reliability tests and troubleshooting.
- Phase 13: v0.1 release checklist, tagging, and rollback criteria.
- Phase 14: Final Unraid templates, persistence, restart, update, and backup testing.

Do not claim end-to-end latency, full barge-in, custom voice management, or production release readiness until those phases have been implemented and verified.

## Historical implementation notes

- Phase 0-4: repository scaffold, fake Wyoming server, s2.cpp client, container scaffold, CUDA/Unraid planning.
- Phase 5A-5D: multipart client support, streaming client interfaces, streamed audio-to-Wyoming helpers, TTS-side metrics.
- Phase 5.5A/5.5B: opt-in real-backend smoke harness and real backend contract verification.
- Phase 6A-6D: CUDA backend image, CPU-only wrapper, Wyoming streaming fix, HA deployment verification.
- Phase 7A-7B: CMU ARCTIC voice profiles, wrapper voice discovery, HA voice selection.
- Phase 7.5A-7.5B: progressive backend HTTP audio streaming, live latency verification.
- Phase 8-8E.1: disconnect cleanup, stride tuning, quant benchmark, runtime tuning.
- Phase 9-9C: queue/busy/timeout policy, SpeechRequest domain model, graceful shutdown.
- Phase 9.5: progressive phrase synthesis.
- Phase 10: end-to-end barge-in validation (stock HA limitation documented).

See [`docs/ROADMAP.md`](docs/ROADMAP.md), [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), and [`docs/NEXT_GOAL_PROMPTS.md`](docs/NEXT_GOAL_PROMPTS.md) for the governing forward plan.

## Operations docs (v0.1.0)

| Document | Purpose |
|----------|---------|
| [`docs/INSTALL.md`](docs/INSTALL.md) | Fresh install instructions |
| [`docs/UNRAID_INSTALL.md`](docs/UNRAID_INSTALL.md) | Unraid-specific deployment notes |
| [`docs/HOME_ASSISTANT_SETUP.md`](docs/HOME_ASSISTANT_SETUP.md) | Home Assistant integration and voice selection |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Security posture and network model |
| [`docs/UPGRADE_ROLLBACK.md`](docs/UPGRADE_ROLLBACK.md) | Upgrade paths, backup, and rollback |
| [`docs/RELEASE.md`](docs/RELEASE.md) | Release checklist and tagging |

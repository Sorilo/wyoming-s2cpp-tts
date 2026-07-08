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
- Model path inside backend container: `/models/s2-pro-q6_k.gguf`
- Host voices directory: `/mnt/user/appdata/s2cpp/voices`
- Backend voices mount: `/voices`

The verified first model target is:

```text
/models/s2-pro-q6_k.gguf
```

This `q6_k` target is the current RTX 3080 baseline. Future model choices may include `s2-pro-q8_0.gguf` for quality if VRAM allows, or `s2-pro-q4_k_m.gguf` as a lower-VRAM fallback. Hardware-upgrade benchmarking is post-v0.1 work.

## Current verified deployment

| Component | Value |
| --- | --- |
| Backend container | `s2cpp-backend` |
| Backend image | `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b` |
| Backend endpoint | `http://s2cpp-backend:3030/generate` |
| Backend contract | `multipart/form-data` only; raw `audio/L16; rate=44100; channels=1` |
| Wrapper container | `wyoming-s2cpp-tts` |
| Wrapper image | `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-89ed2dc` |
| Wyoming endpoint | `tcp://0.0.0.0:10200` inside container; `192.168.1.45:10200` from Home Assistant |
| Home Assistant result | Discovery succeeds; `s2-pro` is visible; real speech is audible |
| Test baseline | 323 tests passing after Phase 7B |

## Current architecture

The production deployment intentionally separates CPU-only Wyoming protocol handling from GPU inference:

- The **wrapper** runs the Python Wyoming TCP server and does not require CUDA, NVIDIA runtime, GGUF files, or GPU access.
- The **backend** runs `s2.cpp` in HTTP server mode with CUDA and the mounted model/tokenizer assets.
- Home Assistant only talks to the wrapper on TCP port 10200.
- The wrapper talks to the backend through the Docker network at `http://s2cpp-backend:3030/generate`.
- The backend expects `multipart/form-data`; JSON requests are not valid for the deployed backend.

## Streaming status

Wyoming protocol streaming is implemented and verified: the wrapper handles `synthesize-start`, `synthesize-chunk`, and `synthesize-stop`, then emits `AudioStart`, `AudioChunk`, `AudioStop`, and `synthesize-stopped` for Home Assistant.

Progressive backend-audio streaming is not currently used by the production handler: although `S2_STREAM` is parsed and `synthesize_s2cpp_streaming_tts_events()` / `generate_stream()` exist, the live handler still calls buffered `synthesize_s2cpp_tts_events()` via `generate_multipart()`, then sends Wyoming audio events.

This means `S2_STREAM=true` is a parsed/configured setting, but Phase 7.5 is still required to wire true progressive backend HTTP audio streaming into the production Wyoming event handler.

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

In the verified deployment, the production wrapper container sets `TTS_BACKEND=s2cpp` and sends buffered multipart requests to `/generate`.

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

Current full-suite baseline before Phase 6E: 287 passing tests.

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
- True progressive backend HTTP audio streaming in the production handler is future Phase 7.5 work.
- Client disconnect cleanup, open HTTP stream closure, and backend cancellation limitations are future Phase 8 work.
- Queue-busy behavior, HTTP 503 handling, queue wait timeout, synthesis timeout, and controlled Wyoming failure behavior are future Phase 9 work.
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

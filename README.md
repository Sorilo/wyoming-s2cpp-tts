# wyoming-s2cpp-tts

`wyoming-s2cpp-tts` is planned as a local Home Assistant Wyoming Protocol TTS service for running Fish Speech S2 Pro through `s2.cpp` GGUF models on a home server.

This repository currently contains an early phased implementation through Phase 5D — including lightweight structured TTS metrics and tracing across all three synthesis paths (fake, buffered s2.cpp, streaming s2.cpp). It includes a minimal fake-audio Wyoming server, a small client for an already-running `s2.cpp` HTTP `/generate` endpoint, JSON and multipart/form-data request construction for that client, an opt-in non-streaming `s2cpp` backend mode, a Phase 3 container/process scaffold that runs the Python wrapper while leaving hooks for a future supervised s2.cpp process, and a Phase 4 CUDA/Unraid planning document. It does **not** yet build `s2.cpp`, download models, progressively stream backend audio, measure real latency, or implement final cancellation/barge-in behavior.

## Target hardware for the first real version

- Server: Unraid home server
- GPU target: NVIDIA RTX 3080 10 GB
- CPU: Intel i9-13900K
- RAM: 96 GB DDR4
- Persistent appdata root: `/mnt/user/appdata`

The first model target is:

```text
/models/s2-pro-q6_k.gguf
```

This `q6_k` target is intended as a realistic starting point for a single 10 GB RTX 3080. Future model choices may include `s2-pro-q8_0.gguf` for quality if VRAM allows, or `s2-pro-q4_k_m.gguf` as a lower-VRAM fallback. A possible later TTS hardware upgrade is an NVIDIA RTX 5080 16 GB, but hardware-upgrade benchmarking is post-v0.1 work.

## Planned final architecture

```text
Home Assistant Assist pipeline
  -> Wyoming Protocol TCP TTS server on port 10200
  -> Python wrapper / adapter
  -> local s2.cpp HTTP server on port 3030
  -> Fish Speech S2 Pro GGUF model
  -> NVIDIA RTX 3080
```

The Python wrapper is responsible for translating Home Assistant/Wyoming TTS requests into s2.cpp HTTP requests, then returning audio to the Wyoming client. The final design should stream PCM chunks where possible, avoid unnecessary full-audio buffering, and cancel synthesis when the client disconnects.

## Latency objective

The aspirational end-to-end target is under 2 seconds from detected end-of-speech through first audible playback for short, warm-path requests, including VAD endpointing. This repo can directly measure TTS-side timestamps such as request receipt, backend first byte, Wyoming first audio chunk, emitted bytes/chunks, cancellation, and request duration. STT, LLM, VAD, and actual playback timestamps require Home Assistant/upstream/client instrumentation or a correlated end-to-end test harness.

Do not treat placeholder buffering values such as `1000 ms` or `4000 ms` as validated production defaults, and do not claim end-to-end latency until it is actually measured.

## Current status

Phase 5D is now implemented:

- Repository structure exists with 128 passing tests.
- Docs describe the intended architecture and deployment path.
- The Python package includes a Wyoming TCP TTS server.
- The default `TTS_BACKEND=fake` path handles Wyoming `Describe` and `Synthesize` events with deterministic local PCM test-tone audio.
- `app/s2_client.py` can POST JSON, multipart/form-data, or create streaming iterators for an already-running external `s2.cpp` HTTP `/generate` endpoint.
- Optional `TTS_BACKEND=s2cpp` routes one buffered s2.cpp client result back through Wyoming `AudioStart`/`AudioChunk`/`AudioStop` events.
- `app/wyoming_server.py` has a streaming async generator (`synthesize_s2cpp_streaming_tts_events()`) that yields progressive Wyoming audio events with PCM frame-aligned rechunking.
- `app/audio.py` has `StreamingPCMRechunker` for bounded frame-aligned PCM rechunking across arbitrary HTTP transport boundaries.
- `app/metrics.py` provides `SynthesisMetrics` (frozen dataclass) and `MetricsCollector` (mutable per-request collector with DI clock) wired into all three synthesis paths — request start, first backend data, first Wyoming chunk, emitted bytes/chunks, terminal status, and monotonic duration.
- `scripts/smoke_s2cpp_generate.py` provides an optional direct `/generate` smoke test.
- `Dockerfile` installs Python requirements, exposes Wyoming/health ports, creates `/models`, `/voices`, and `/config`, and starts `entrypoint.sh`.
- `entrypoint.sh` runs `python -m app.main` and includes TODO hooks for future internal s2.cpp supervision on `127.0.0.1:3030`.
- `docs/CUDA_S2CPP_PLAN.md` documents the untested future CUDA/s2.cpp build plan.
- `scripts/check_gpu_visibility.sh` provides a safe future `nvidia-smi` validation hook.
- No s2.cpp build, CUDA setup, GGUF model download, Home Assistant integration, real latency measurement, or final cancellation/barge-in behavior is implemented yet.

Implementation continues in small phases. The exact next implementation phase is Phase 5B: streaming async iterator over s2.cpp response bytes with mocked chunked responses. See [`docs/ROADMAP.md`](docs/ROADMAP.md) and [`docs/NEXT_GOAL_PROMPTS.md`](docs/NEXT_GOAL_PROMPTS.md).

## Manual Phase 1 test

Install the small Python requirements, then start the fake Wyoming server:

```bash
python -m pip install -r requirements.txt
python -m app.main
```

Expected startup message:

```text
Wyoming TTS server listening on tcp://0.0.0.0:10200 with backend=fake
```

In Home Assistant, add a Wyoming Protocol integration pointing at the host running this service on port `10200`, then select it as a TTS engine in an Assist pipeline. A synthesis request should return a deterministic test tone, not real speech.

For a local automated protocol check, run:

```bash
python -m pytest tests/test_wyoming_server.py -q
```

## Phase 2/2.5 external s2.cpp backend configuration

Phase 2 added client code for an already-running external `s2.cpp` HTTP server. Phase 2.5 adds an opt-in non-streaming Wyoming backend mode that can call that client and convert one buffered PCM response into Wyoming audio events. The service does not start or supervise s2.cpp yet.

Default backend settings in `app/config.py` are:

```text
TTS_BACKEND=fake
S2_HOST=127.0.0.1
S2_PORT=3030
S2_MODEL=/models/s2-pro-q6_k.gguf
```

For a future external server on another host, set the corresponding environment variables before running the service or client tools:

```bash
export TTS_BACKEND=s2cpp
export S2_HOST=192.168.1.45
export S2_PORT=3030
python -m app.main
```

In this mode, Home Assistant still connects to Wyoming on port `10200`, while the Python wrapper makes a buffered HTTP request to `http://$S2_HOST:$S2_PORT/generate` for each synthesis request.

You can also load settings from the environment when creating the client directly:

```python
from app.config import Settings
from app.s2_client import S2Client, S2GenerateRequest

settings = Settings.from_env()
client = S2Client.from_settings(settings)
result = client.generate(S2GenerateRequest.from_settings("hello", settings))
print(result.content_type, len(result.audio))
```

`TTS_BACKEND=s2cpp` is intentionally non-streaming in Phase 2.5: it buffers one backend result, assumes raw PCM s16le matching the configured audio format, then emits Wyoming `AudioStart`/`AudioChunk`/`AudioStop`. Progressive streaming, WAV-header handling, cancellation, and barge-in behavior are later phases.

For the mocked Phase 2/2.5 client and backend-route tests, run:

```bash
python -m pytest tests/test_s2_client.py tests/test_wyoming_s2cpp_backend.py -q
```

## Phase 5A multipart/form-data client compatibility

Phase 5A adds an additive multipart/form-data request path in `app/s2_client.py`:

- `S2Client.generate(...)` still sends JSON and remains the existing buffered default path.
- `S2Client.generate_multipart(...)` sends multipart/form-data and still buffers the response.
- `encode_multipart_form_data(...)` supports scalar fields and in-memory file parts for future reference audio/file experiments.

Current multipart field names intentionally mirror the JSON payload keys: `text`, `model`, `stream`, `chunked`, `output_format`, `segment_sentences`, `max_new_tokens`, `temperature`, `top_p`, `top_k`, and optional `voice`. These names are **unverified upstream assumptions** until tested against a real compatible s2.cpp backend. File part names such as `reference_audio` are supported by the encoder but are also unverified assumptions.

Phase 5A does not implement streaming, Home Assistant/Wyoming behavior changes, Docker/CUDA work, real backend validation, audio-quality validation, or latency measurement.

For mocked multipart compatibility tests, run:

```bash
python -m pytest tests/test_s2_client.py -q
```

## Phase 2.75 optional direct s2.cpp smoke test

Use this only when an external s2.cpp HTTP server is already running. The script does not start s2.cpp, build CUDA code, download models, or require model infrastructure for normal tests/CI.

Default harmless skip mode:

```bash
python scripts/smoke_s2cpp_generate.py --text "hello"
```

Expected output includes:

```text
status=skipped
endpoint=http://127.0.0.1:3030/generate
bytes_received=0
```

Opt in when a backend is available:

```bash
export TTS_BACKEND=s2cpp
export S2_HOST=192.168.1.45
export S2_PORT=3030
python scripts/smoke_s2cpp_generate.py --text "Hello from direct s2.cpp smoke test."
```

Expected success output includes:

```text
status=ok
endpoint=http://192.168.1.45:3030/generate
content_type=<backend content type>
bytes_received=<non-zero byte count>
```

If `TTS_BACKEND=s2cpp` is set but the backend is unavailable, the script reports `status=unavailable` and exits successfully so it is safe to run during local setup checks.

Limitations:

- This is a direct backend-client smoke test, not a Home Assistant/Wyoming integration test.
- It buffers one response and prints metadata only.
- It assumes the backend `/generate` endpoint is already running and compatible with the current buffered JSON payload used by `S2Client.generate(...)`; multipart compatibility is mocked separately in Phase 5A.
- It does not validate audio quality, realtime factor, VRAM use, streaming, cancellation, or barge-in behavior.

## Phase 3 container/process scaffold

The Phase 3 Dockerfile is now a runnable Python-wrapper container scaffold:

- Installs `requirements.txt`.
- Copies `app/`, `scripts/`, and `entrypoint.sh`.
- Creates `/models`, `/voices`, and `/config` for Unraid appdata mappings.
- Exposes `10200/tcp` for Wyoming and `8088/tcp` for a future health/debug endpoint.
- Starts `entrypoint.sh`, which runs `python -m app.main`.

Default container behavior is still safe fake audio:

```text
TTS_BACKEND=fake
WYOMING_URI=tcp://0.0.0.0:10200
```

Future s2.cpp process supervision is intentionally a hook only:

```text
S2CPP_ENABLE_INTERNAL_SERVER=false
```

Setting `S2CPP_ENABLE_INTERNAL_SERVER=true` currently prints TODO messages and continues; it does not start s2.cpp yet. This Phase 3 container does not build s2.cpp, compile CUDA code, download models, or include the future s2.cpp binary. Phase 8A is the planned phase for actually building and testing the CUDA-enabled s2.cpp Docker image.

## Phase 4 CUDA/s2.cpp and Unraid GPU plan

See [`docs/CUDA_S2CPP_PLAN.md`](docs/CUDA_S2CPP_PLAN.md) for the current future build/runtime plan. Phase 4 added documentation and validation hooks only:

- A future multi-stage CUDA Dockerfile shape is documented but not enabled.
- The relevant external s2.cpp reference found was `sinfisum/s2pro-gguf`; its Linux/Unraid build has not been tested here.
- The planned server flags include `--server`, `--model`, `--tokenizer`, `--ngl 36`, and `--cuda 0`, subject to Linux verification.
- Future Unraid/NVIDIA variables include `NVIDIA_VISIBLE_DEVICES` and `NVIDIA_DRIVER_CAPABILITIES=compute,utility`.
- `scripts/check_gpu_visibility.sh` can be run inside a future GPU-enabled container to check `nvidia-smi` safely.

No CUDA/s2.cpp build success is claimed by this repo yet.

## GitHub remote

No remote is required for this scaffold. If this repository does not already have a remote, add one later with:

```bash
git remote add origin git@github.com:<your-user-or-org>/wyoming-s2cpp-tts.git
git push -u origin main
```

Do not force-push, and do not push from automation unless the remote and credentials are confirmed safe.

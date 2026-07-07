# wyoming-s2cpp-tts

`wyoming-s2cpp-tts` is planned as a local Home Assistant Wyoming Protocol TTS service for running Fish Speech S2 Pro through `s2.cpp` GGUF models on a home server.

This repository currently contains an early phased implementation. It includes a minimal fake-audio Wyoming server, a small client for an already-running `s2.cpp` HTTP `/generate` endpoint, and an opt-in non-streaming `s2cpp` backend mode that can route one buffered backend response through Wyoming audio events. It does **not** yet build `s2.cpp`, download models, package the final container, progressively stream backend audio, or implement final cancellation/barge-in behavior.

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

This `q6_k` target is intended as a realistic starting point for a single 10 GB RTX 3080. Future model choices may include `s2-pro-q8_0.gguf` for quality if VRAM allows, or `s2-pro-q4_k_m.gguf` as a lower-VRAM fallback.

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

## Current status

Phase 2.5 is now implemented at an opt-in non-streaming integration level:

- Repository structure exists.
- Docs describe the intended architecture and deployment path.
- The Python package includes a Wyoming TCP TTS server.
- The default `TTS_BACKEND=fake` path handles Wyoming `Describe` and `Synthesize` events with deterministic local PCM test-tone audio.
- `app/s2_client.py` can POST a request to an already-running external `s2.cpp` HTTP `/generate` endpoint and return raw audio bytes.
- Optional `TTS_BACKEND=s2cpp` routes one buffered s2.cpp client result back through Wyoming `AudioStart`/`AudioChunk`/`AudioStop` events.
- The s2.cpp client and opt-in Wyoming backend path are covered with mocked tests.
- No s2.cpp build, CUDA setup, GGUF model download, Docker build, progressive streaming, or final cancellation/barge-in behavior is implemented yet.

Implementation continues in small phases. See [`docs/ROADMAP.md`](docs/ROADMAP.md) and [`docs/NEXT_GOAL_PROMPTS.md`](docs/NEXT_GOAL_PROMPTS.md).

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

## GitHub remote

No remote is required for this scaffold. If this repository does not already have a remote, add one later with:

```bash
git remote add origin git@github.com:<your-user-or-org>/wyoming-s2cpp-tts.git
git push -u origin main
```

Do not force-push, and do not push from automation unless the remote and credentials are confirmed safe.

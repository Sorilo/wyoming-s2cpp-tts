# Next Hermes `/goal` prompts

Run phases one at a time. This file must be regenerated from the actual repository state after every `/goal` run. Do not copy stale assumptions forward.

The current exact next incomplete phase is **Phase 5D: TTS-side metrics and structured tracing**.

## Completed intermediate phases

### Phase 5A.1 (completed 2026-07-07)

Verified the s2.cpp multipart/form-data structure against the sinfisum/s2pro-gguf reference client. Established that generation settings belong inside a single `params` JSON string, not as flattened multipart fields.

### Phase 5A.2 (completed 2026-07-07)

Corrected outgoing multipart field names to the canonical `rodrigomatta/s2.cpp` OpenAPI spec (`openapi/s2-openapi.yaml`). Canonical fields: `text`, `params`, `reference` (file part), `reference_text`, `voice`, `voice_dir`. Aliases (`prompt_audio`, `prompt_text`, etc.) are normalised to canonical names. Saved voice profiles (`.s2voice`) supported via `voice`/`voice_dir`.

### Phase 5C (completed 2026-07-07)

Implemented streamed audio-to-Wyoming-events conversion. Added ``StreamingPCMRechunker`` in ``app/audio.py`` for bounded frame-aligned PCM rechunking across arbitrary HTTP transport boundaries with frame-derived timestamps. Added ``synthesize_s2cpp_streaming_tts_events()`` async generator in ``app/wyoming_server.py`` that consumes ``S2StreamResult`` via ``asyncio.to_thread`` (blocking reads offloaded from event loop), feeds the rechunker, and emits ``AudioStart`` → progressive ``AudioChunk`` → ``AudioStop``. Added ``_STREAM_EOF`` sentinel pattern for safe ``StopIteration`` transport through ``run_in_executor``. Error semantics: backend errors propagate without ``AudioStop``; final incomplete PCM frames raise ``ValueError``; stream cleanup on normal/error/early-exit paths. 28 new mocked tests (90 total) cover frame alignment, carry-over, combining/splitting, timestamps, incomplete-frame rejection, event ordering, PCM preservation, error propagation, stream lifecycle, thread offloading, no buffering. Existing buffered/fake behavior preserved; ``TTS_BACKEND=fake`` remains default. No real s2.cpp streaming, Home Assistant, cancellation, or latency success claimed.

### Phase 5B (completed 2026-07-07)

Implemented the streaming client interface: ``S2StreamResult`` context manager / iterator yields audio chunks progressively via ``response.read(4096)`` without buffering the full response; ``S2Client.generate_stream()`` builds canonical multipart with ``stream``/``chunked``/``low_latency``/``pcm_s16le`` in ``params`` JSON. 15 new mocked streaming tests (62 total) cover progressive yielding, deterministic first-chunk-before-full-response proof, cleanup on normal/break/error paths, streaming params verification, canonical field preservation. All 47 existing tests pass. ``TTS_BACKEND=fake`` remains default.


## Prompt-generation guidance

Every future generated prompt must:

- name the exact next incomplete phase or a justified narrowly scoped intermediate phase;
- include `/workspace/wyoming-s2cpp-tts` as the project path;
- include quota/risk protections appropriate to the phase;
- require inspection of repository areas touched by the phase, not just one file;
- define exact scope, exclusions, acceptance criteria, and tests;
- state which claims remain unverified;
- require one focused commit;
- require status/documentation updates;
- require the final response to include the following phase's complete ready-to-paste prompt.

If an intermediate phase is proposed, it must state why it is required, which approved phase it blocks, exact scope, acceptance criteria, and whether it changes the approved architecture.

## Next immediate prompt: Phase 5D

```text
/goal
You are Hermes, acting as a senior Python/Home Assistant Wyoming Protocol engineer.

Project:
/workspace/wyoming-s2cpp-tts

Goal:
Implement Phase 5D only: add TTS-side metrics and structured tracing for the Wyoming service — request start, first backend byte, first Wyoming audio chunk, total emitted bytes, emitted chunk count, request/stream duration, and trace/request identifiers where practical. Preserve the existing fake backend, buffered JSON/multipart paths, Phase 5B streaming client, Phase 5C streaming-to-Wyoming path, and all existing Wyoming behavior.

Current repository state (post Phase 5C):
- 90 tests pass (4 JSON, 17 multipart/encoder, 15 streaming-client, 10 rechunker, 18 streaming-Wyoming, 4 Wyoming, 3 smoke, 19 other).
- ``app/wyoming_server.py``: ``synthesize_fake_tts_events()``, ``synthesize_s2cpp_tts_events()`` (buffered), ``synthesize_s2cpp_streaming_tts_events()`` (async generator, streaming), ``FakeTtsEventHandler``, ``SingleWorkerSynthesisQueue``.
- ``app/audio.py``: ``StreamingPCMRechunker`` for progressive PCM frame alignment across transport boundaries.
- ``app/s2_client.py``: ``S2Client.generate()``, ``generate_multipart()``, ``generate_stream()``, ``S2StreamResult``.
- ``TTS_BACKEND=fake`` remains the default.

Quota protection:

* Keep this run small and focused.
* Do not download GGUF models, tokenizers, or voices.
* Do not build, vendor, clone, or run s2.cpp.
* Do not build Docker or CUDA.
* Do not run GPU tests.
* Do not contact or test a real s2.cpp backend.
* Do not implement cancellation, barge-in, Home Assistant integration, Docker/Unraid deployment behavior, audio-quality validation, or real latency measurement.
* Do not change multipart field names or flatten params.
* Do not perform unrelated refactoring.
* Make one focused implementation commit.

Repository inspection requirement:

Inspect the current repository state before editing, including at minimum:

* ``git status`` and recent Git history
* ``app/wyoming_server.py`` (note: ``FakeTtsEventHandler``, ``SingleWorkerSynthesisQueue``, all three synthesis functions)
* ``app/audio.py`` (note: ``StreamingPCMRechunker``)
* ``app/s2_client.py``
* ``app/config.py``
* Existing test structure for patterns to follow

Scope:

* Add TTS-side metrics/structured tracing for:
  - request start timestamp
  - first backend byte timestamp
  - first Wyoming audio chunk timestamp
  - total emitted audio bytes
  - emitted chunk count
  - request/stream duration
  - trace/request identifier where practical

* This repository can directly measure TTS request receipt, backend first byte, Wyoming first audio chunk, emitted bytes/chunks, cancellation, and request duration. STT, LLM, VAD, and actual playback timestamps require external instrumentation — clearly distinguish locally measurable timestamps from those that require Home Assistant or upstream instrumentation.

* The metrics implementation should work for both the buffered and streaming s2cpp paths as well as the fake backend path.

* Keep metrics collection lightweight and non-blocking.

* Tests must be mocked/fake — no real backend, no real Home Assistant.

* Document which timestamps are locally measurable and which require external instrumentation.

* Preserve ``TTS_BACKEND=fake`` as the default.

* Preserve all existing behavior and tests.

Acceptance criteria:

* Existing fake, buffered, and streaming tests still pass (90 total).
* New mocked tests prove metrics are collected for fake, buffered s2cpp, and streaming s2cpp paths.
* Metrics/timestamps are correct: request start < first backend byte < first Wyoming chunk.
* Total bytes and chunk counts are accurate.
* This repository clearly distinguishes locally measurable TTS timestamps from STT/LLM/VAD/playback timestamps that require external instrumentation.
* No end-to-end latency claims are made without an actual end-to-end harness.
* No real s2.cpp, CUDA, GPU, Docker, Home Assistant, cancellation, or latency success is claimed.
* ROADMAP.md, TODO.md, NEXT_GOAL_PROMPTS.md, and CHANGELOG.md reflect Phase 5D status.
* The final response includes the complete ready-to-paste /goal prompt for Phase 5.5 or a justified intermediate phase.

Checks:

* Run focused Phase 5D tests first.
* Run all existing inexpensive tests.
* Review the final diff for accidental runtime-handler replacement, Phase 6 cancellation/queue work, Docker/CUDA work, Home Assistant integration, or unrelated refactoring.
* Make one focused implementation commit.
* Leave the working tree clean.
```

## Approved later phase skeletons

### Phase 5C ✅ (completed 2026-07-07)

Implemented. Pipe streamed backend audio into Wyoming `AudioStart`, `AudioChunk`, and `AudioStop` events with mocked streaming tests. Include WAV-header handling if required by mocked/backend format.

### Phase 5D

Add TTS-side metrics and structured tracing for request start, first backend byte, first Wyoming audio chunk, emitted bytes/chunks, duration, and trace/request identifiers where practical.

### Phase 5.5

Run a real external s2.cpp smoke test outside the final Docker image only when an already-running backend and required files are available.

### Phase 6A

Handle Wyoming client disconnects and backend cancellation where supported.

### Phase 6B

Implement queue cancellation, backend busy handling, timeout policy, and policy for a new request arriving during active speech.

### Phase 6C

Test barge-in-friendly behavior using Home Assistant when available or simulated disconnect/cancellation tests otherwise.

### Phase 7A

Add comprehensive protocol, queue, error, cancellation, integration tests, and troubleshooting documentation.

### Phase 7B

Create v0.1 release checklist and tagging criteria. Do not tag v0.1 unless required behavior has actually been verified.

### Phase 8A

Build and test the CUDA-enabled s2.cpp Docker image.

### Phase 8B

Finalize the Unraid WebUI template/documentation and validate GPU passthrough, ports, mounts, permissions, startup, process supervision, health checks, shutdown, restart behavior, and persistence.

### Phase 8C

Run the final Home Assistant end-to-end test including Assist pipeline connection, real STT-to-conversation-to-TTS operation, streamed playback, audio correctness, cancellation/barge-in behavior where supported, and latency measurements where measurable.

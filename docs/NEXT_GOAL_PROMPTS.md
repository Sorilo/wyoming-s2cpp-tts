# Next Hermes `/goal` prompts

Run phases one at a time. This file must be regenerated from the actual repository state after every `/goal` run. Do not copy stale assumptions forward.

The current exact next incomplete phase is **Phase 5C: streamed audio to Wyoming events**.

## Completed intermediate phases

### Phase 5A.1 (completed 2026-07-07)

Verified the s2.cpp multipart/form-data structure against the sinfisum/s2pro-gguf reference client. Established that generation settings belong inside a single `params` JSON string, not as flattened multipart fields.

### Phase 5A.2 (completed 2026-07-07)

Corrected outgoing multipart field names to the canonical `rodrigomatta/s2.cpp` OpenAPI spec (`openapi/s2-openapi.yaml`). Canonical fields: `text`, `params`, `reference` (file part), `reference_text`, `voice`, `voice_dir`. Aliases (`prompt_audio`, `prompt_text`, etc.) are normalised to canonical names. Saved voice profiles (`.s2voice`) supported via `voice`/`voice_dir`.

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

## Next immediate prompt: Phase 5C

```text
/goal
You are Hermes, acting as a senior Python/Home Assistant Wyoming Protocol engineer.

Project:
/workspace/wyoming-s2cpp-tts

Goal:
Implement Phase 5C only: pipe streamed backend audio from the Phase 5B ``S2StreamResult`` iterator into Wyoming ``AudioStart``, ``AudioChunk``, and ``AudioStop`` events with mocked streaming tests, while preserving the existing fake backend, buffered JSON/multipart paths, and Wyoming behavior.

Current repository state (post Phase 5B):
- ``app/s2_client.py``: ``S2Client.generate()`` (JSON buffered), ``S2Client.generate_multipart()`` (canonical multipart buffered), ``S2Client.generate_stream()`` (canonical multipart streaming via ``S2StreamResult`` iterator), ``encode_multipart_form_data()``.
- ``S2StreamResult`` is a context manager / iterator yielding chunks via ``response.read(4096)``; resources close on normal completion, error, or early break.
- ``S2GenerateRequest.to_multipart_fields(streaming=True)`` injects ``stream``/``chunked``/``low_latency``/``pcm_s16le`` into ``params`` JSON.
- 62 tests pass (4 JSON, 17 multipart/encoder, 15 streaming, 4 Wyoming, 3 smoke, 19 other).
- ``TTS_BACKEND=fake`` remains default.
- Wyoming server (``app/wyoming_server.py``) currently only supports buffered audio via ``synthesize_s2cpp_tts_events()`` and ``synthesize_fake_tts_events()``.

Quota protection:

* Keep this run small.
* Do not download GGUF models, tokenizers, or voices.
* Do not build, vendor, clone, or run s2.cpp.
* Do not build Docker.
* Do not build CUDA.
* Do not run GPU tests.
* Do not implement metrics/tracing, cancellation, Home Assistant integration, Docker/Unraid deployment behavior, or real latency measurement in this phase.
* Use mocked HTTP/client tests only unless an already-running backend is explicitly provided.
* Make one focused commit.
* Do not change multipart field names or flatten params — the Phase 5A.2 canonical format is verified.

Repository inspection requirement:

Inspect the current repository state before editing, including at minimum:

* git status and recent git history
* app/s2_client.py (note: ``S2StreamResult``, ``generate_stream()``, ``to_multipart_fields(streaming=...)``)
* app/wyoming_server.py (note: ``synthesize_s2cpp_tts_events()``, ``synthesize_fake_tts_events()``, ``_pcm_to_audio_events()``, ``FakeTtsEventHandler``)
* app/audio.py (PCM chunking helpers)
* tests/test_s2_client.py (15 streaming tests)
* tests/test_wyoming_s2cpp_backend.py
* tests/test_wyoming_server.py
* docs/ROADMAP.md
* docs/NEXT_GOAL_PROMPTS.md
* TODO.md
* CHANGELOG.md

Scope:

* Add a function (e.g., ``synthesize_s2cpp_streaming_tts_events()``) that consumes the ``S2StreamResult`` iterator and emits Wyoming ``AudioStart``, progressive ``AudioChunk``, and ``AudioStop`` events.
* Use the existing ``_pcm_to_audio_events()`` pattern or a new streaming variant that emits one ``AudioChunk`` per yielded audio chunk, with correct timestamps, rate, width, and channels.
* Keep backend HTTP/chunking details in ``app/s2_client.py``.
* Preserve ``TTS_BACKEND=fake`` as the default.
* Preserve existing buffered ``synthesize_s2cpp_tts_events()`` and ``synthesize_fake_tts_events()``.
* Preserve existing ``FakeTtsEventHandler`` behavior for the fake backend path.
* Do NOT modify the Wyoming ``FakeTtsEventHandler`` to use streaming in this phase — the streaming path is a new, separate function tested independently.
* Use mocked ``S2StreamResult`` (or patched ``S2Client``) to prove Wyoming events are emitted progressively.
* Document that real Wyoming streaming behavior remains unverified until tested against a real s2.cpp backend and Home Assistant pipeline.
* Update roadmap/status docs only as needed for Phase 5C.

Acceptance criteria:

* Existing fake Wyoming tests still pass.
* Existing buffered JSON, canonical multipart, and Phase 5B streaming s2cpp mocked tests still pass.
* New mocked streaming-to-Wyoming tests prove that audio chunks from ``S2StreamResult`` are converted to progressive ``AudioChunk`` events.
* A test proves that ``AudioStart`` is emitted before the first ``AudioChunk``, and ``AudioStop`` after the last.
* Timestamps in ``AudioChunk`` events increment logically across chunks.
* Tests prove event emission stops cleanly on stream exhaustion, stream error, and early consumer exit where practical.
* No real s2.cpp, CUDA, GPU, Docker, Home Assistant Wyoming streaming, cancellation, audio-quality, or latency success is claimed.
* Runtime behavior remains default-safe with ``TTS_BACKEND=fake``.
* ROADMAP.md, TODO.md, NEXT_GOAL_PROMPTS.md, and CHANGELOG.md reflect Phase 5C status after the change.
* The final response includes files changed, tests run, git status, commit hash/message, unresolved unknowns, and the complete ready-to-paste ``/goal`` prompt for Phase 5D or a justified intermediate phase.
```
## Approved later phase skeletons

### Phase 5C

Pipe streamed backend audio into Wyoming `AudioStart`, `AudioChunk`, and `AudioStop` events with mocked streaming tests. Include WAV-header handling if required by mocked/backend format.

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

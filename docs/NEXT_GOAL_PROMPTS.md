# Next Hermes `/goal` prompts

Run phases one at a time. This file must be regenerated from the actual repository state after every `/goal` run. Do not copy stale assumptions forward.

The current exact next incomplete phase is **Phase 5B: streaming async iterator over s2.cpp response bytes**.

## Completed intermediate phases

### Phase 5A.1 (completed 2026-07-07)

Verified the s2.cpp multipart/form-data structure against the sinfisum/s2pro-gguf reference client. Established that generation settings belong inside a single `params` JSON string, not as flattened multipart fields.

### Phase 5A.2 (completed 2026-07-07)

Corrected outgoing multipart field names to the canonical `rodrigomatta/s2.cpp` OpenAPI spec (`openapi/s2-openapi.yaml`). Canonical fields: `text`, `params`, `reference` (file part), `reference_text`, `voice`, `voice_dir`. Aliases (`prompt_audio`, `prompt_text`, etc.) are normalised to canonical names. Saved voice profiles (`.s2voice`) supported via `voice`/`voice_dir`.

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

## Next immediate prompt: Phase 5B

```text
/goal
You are Hermes, acting as a senior Python/Home Assistant Wyoming Protocol engineer.

Project:
/workspace/wyoming-s2cpp-tts

Goal:
Implement Phase 5B only: add a streaming client interface that exposes s2.cpp HTTP response bytes progressively using mocked chunked responses, while preserving the existing fake backend, JSON buffered client path, Phase 5A.2 canonical multipart buffered client path, and Wyoming behavior.

Current repository state (post Phase 5A.2):
- app/s2_client.py: S2Client.generate() (JSON), S2Client.generate_multipart() (canonical multipart with text+params+reference_text/reference+voice+voice_dir), encode_multipart_form_data().
- Canonical multipart fields match rodrigomatta/s2.cpp OpenAPI spec: reference (file), reference_text, voice, voice_dir.
- Alias normalisation: prompt_audio/prompt_text/reference_audio/ref_audio/ref_text → canonical reference/reference_text.
- S2GenerateRequest has internal prompt_text (backward compat), voice, and voice_dir fields.
- S2GenerateRequest.to_payload() preserves the JSON format unchanged.
- S2GenerateRequest.to_multipart_fields() emits canonical field names only.
- Input validation: reference (or alias) without reference_text/prompt_text raises ValueError.
- voice and reference can coexist; voice works independently.
- Buffered multipart does not silently add stream/chunked/low_latency to params.
- 47 tests pass (21 s2_client, 4 Wyoming, 3 smoke, etc.).
- TTS_BACKEND=fake remains default.

Quota protection:

* Keep this run small.
* Do not download GGUF models, tokenizers, or voices.
* Do not build, vendor, clone, or run s2.cpp.
* Do not build Docker.
* Do not build CUDA.
* Do not run GPU tests.
* Do not pipe streamed audio into Wyoming events yet; that is Phase 5C.
* Do not implement metrics/tracing, cancellation, Home Assistant integration, Docker/Unraid deployment behavior, or real latency measurement in this phase.
* Use mocked HTTP/client tests only unless an already-running backend is explicitly provided.
* Make one focused commit.
* Do not change multipart field names or flatten params — the Phase 5A.2 canonical format is verified.

Repository inspection requirement:

Inspect the current repository state before editing, including at minimum:

* git status and recent git history
* app/config.py
* app/s2_client.py (note: canonical multipart format from Phase 5A.2)
* app/wyoming_server.py
* tests/test_s2_client.py
* tests/test_wyoming_s2cpp_backend.py
* scripts/smoke_s2cpp_generate.py
* README.md
* docs/ROADMAP.md
* docs/NEXT_GOAL_PROMPTS.md
* TODO.md
* CHANGELOG.md

Scope:

* Add a streaming response interface to the s2.cpp client layer, likely in app/s2_client.py.
* The streaming method should add `stream=true`, `chunked=true`, `output_format="pcm_s16le"`, and `low_latency=true` to the multipart `params` JSON string when streaming is requested, without duplicating multipart-building logic.
* Keep backend HTTP details in app/s2_client.py.
* Preserve `TTS_BACKEND=fake` as the default.
* Preserve existing `S2Client.generate(...)` JSON buffered behavior.
* Preserve existing `S2Client.generate_multipart(...)` canonical multipart buffered behavior (text + params JSON string, reference/reference_text, voice/voice_dir).
* Use mocked chunked HTTP responses to prove bytes can be consumed progressively.
* Represent stream errors and partial-stream cleanup clearly enough for later Phase 5C/6 work.
* Do not change Wyoming event emission in this phase.
* Document that real backend streaming remains unverified until a real s2.cpp backend is tested.
* Update roadmap/status docs only as needed for Phase 5B.

Streaming lifecycle requirements:

* Implement the streaming result as an async iterator or equivalent resource-safe interface.
* Do not call a full-response read method before yielding chunks -- no implementation path may reconstruct the complete streamed body before exposing the first chunk.
* The HTTP response and client resources must remain open while chunks are consumed and close after normal completion, backend error, or early consumer exit.
* Preserve chunk order and ignore empty transport chunks where appropriate.
* Do not implement broader cancellation or queue policy in this phase, but structure cleanup so Phase 6 can add cancellation safely.

Acceptance criteria:

* Existing fake Wyoming tests still pass.
* Existing buffered JSON and canonical multipart s2cpp mocked tests still pass.
* New mocked streaming tests prove the client can yield chunks progressively without reading the full response first.
* A deterministic mocked test proves that the first chunk is yielded before the complete response or later chunks become available.
* Tests prove response cleanup after normal completion, streaming failure, and early iterator close/break where practical.
* Stream error/cleanup behavior is covered by mocked tests where practical.
* No real s2.cpp, CUDA, GPU, Docker, Home Assistant, Wyoming streaming, cancellation, audio-quality, or latency success is claimed.
* Runtime behavior remains default-safe with `TTS_BACKEND=fake`.
* ROADMAP.md, TODO.md, NEXT_GOAL_PROMPTS.md, and CHANGELOG.md reflect Phase 5B status after the change.
* The final response includes files changed, tests run, git status, commit hash/message, unresolved unknowns, and the complete ready-to-paste `/goal` prompt for Phase 5C or a justified intermediate phase.
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

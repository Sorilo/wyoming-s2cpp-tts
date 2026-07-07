# Next Hermes `/goal` prompts

Run phases one at a time. This file must be regenerated from the actual repository state after every `/goal` run. Do not copy stale assumptions forward.

The current exact next incomplete phase is **Phase 5.5: real external s2.cpp smoke test outside the final Docker image**.

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

## Next immediate prompt: Phase 5.5A ✅ (harness completed 2026-07-07)

Phase 5.5A implemented: ``app/smoke_harness.py`` with full smoke harness,
rewritten ``scripts/smoke_s2cpp_generate.py`` CLI, 65 new mocked tests,
193 total pass. Phase 5.5B is pending — requires an already-running
``rodrigomatta/s2.cpp`` backend.

### Phase 5.5B continuation prompt (if no real backend was tested)

```text
/goal
You are Hermes, acting as a senior Python/s2.cpp/smoke-test engineer.

Project:
/workspace/wyoming-s2cpp-tts

Goal:
Execute Phase 5.5B only: run the real-backend smoke tests against an
already-running rodrigomatta/s2.cpp server and document the actual results.

Context:
Phase 5.5A is complete — the harness is implemented and tested (193 tests
pass). The CLI accepts --run-real, --require-backend, --endpoint, --json,
--output-dir, --probe-legacy-json.

If no backend is reachable, report status=unavailable and exit 0 (unless
--require-backend is set).

Quota/safety: do not build s2.cpp, download models, or do GPU work.

---

### Phase 6A: Wyoming client disconnect and backend cancellation (prompt follows)


```text
/goal
You are Hermes, acting as a senior Python/Home Assistant Wyoming Protocol engineer.

Project:
/workspace/wyoming-s2cpp-tts

Goal:
Implement Phase 5.5 only: run a real external s2.cpp smoke test outside the final Docker image. Verify that an already-running s2.cpp backend with required model/tokenizer files can actually produce audio through the client code, and document the results.

Current repository state (post Phase 5D):
- 128 tests pass (90 existing + 38 Phase 5D metrics tests).
- ``app/metrics.py``: ``SynthesisMetrics`` (frozen dataclass) + ``MetricsCollector`` (mutable per-request collector with DI clock).
- ``app/wyoming_server.py``: ``synthesize_fake_tts_events()``, ``synthesize_s2cpp_tts_events()``, ``synthesize_s2cpp_streaming_tts_events()`` — all accept optional ``MetricsCollector``.
- ``app/audio.py``: ``StreamingPCMRechunker``.
- ``app/s2_client.py``: ``S2Client.generate()``, ``generate_multipart()``, ``generate_stream()``, ``S2StreamResult``.
- ``TTS_BACKEND=fake`` remains the default.
- Existing smoke-test script: ``scripts/smoke_s2cpp_generate.py``.

Quota protection:

* This is an opt-in smoke test — skip harmlessly when no backend is available.
* Do not download GGUF models, tokenizers, or voices.
* Do not build, vendor, clone, or run s2.cpp.
* Do not build Docker or CUDA.
* Do not run GPU tests beyond what the backend already uses.
* Do not implement cancellation, barge-in, Home Assistant integration, or latency benchmarking.
* Do not change multipart field names, params structure, or audio semantics.
* Do not perform unrelated refactoring.
* Do not change ``TTS_BACKEND=fake`` default behavior.
* Make one focused implementation commit.

Repository inspection requirement:

Inspect the current repository state before editing, including at minimum:

* ``git status`` and recent Git history
* ``scripts/smoke_s2cpp_generate.py``
* ``app/s2_client.py``
* ``app/config.py``

Scope:

* Extend the existing ``scripts/smoke_s2cpp_generate.py`` (or create a new companion script) to test the real s2.cpp backend when available.

* If a real backend is available (``TTS_BACKEND=s2cpp`` and reachable endpoint):
  - Send a buffered JSON request
  - Send a buffered multipart request
  - Send a streaming multipart request and consume chunks
  - For each: record content type, byte count, and whether audio was received
  - Report results as structured output (JSON or human-readable)

* If no backend is available:
  - Exit successfully with ``status=skipped`` or ``status=unavailable``
  - Do not fail CI or local test suites

* Document which parts of the result were actually verified (audio received vs. playable vs. correct-sounding).

* This is a direct backend-client smoke test, not a Home Assistant/Wyoming integration test. Do not attempt to play audio, validate speech quality, measure latency, or connect to HA.

* Do not change the existing ``smoke_s2cpp_generate.py`` behavior for the JSON path unless it's a bug fix.

Acceptance criteria:

* Opt-in smoke test: skips harmlessly when no backend is available.
* Results document exact backend endpoint, payload mode, content type, byte count, and whether audio bytes were received.
* Streaming test proves chunks are received progressively (not all at end).
* No Docker/CUDA build success is inferred from this smoke test.
* No real-time playability, audio quality, or latency claims unless actually verified.
* Existing 128 tests still pass.
* Existing ``TTS_BACKEND=fake`` behavior unchanged.
* ``ROADMAP.md``, ``TODO.md``, ``NEXT_GOAL_PROMPTS.md``, and ``CHANGELOG.md`` reflect Phase 5.5 status.
* The final response includes the complete ready-to-paste ``/goal`` prompt for Phase 6A or a justified intermediate phase.

Checks:

* Run only existing inexpensive tests (no new test required for the smoke script unless adding cleanup/regression coverage).
* Review the final diff for accidental runtime-handler replacement, Phase 6 work, Docker/CUDA work, Home Assistant integration, or unrelated refactoring.
* Make one focused implementation commit.
* Leave the working tree clean.
```

## Approved later phase skeletons

### Phase 5C ✅ (completed 2026-07-07)

Implemented. Pipe streamed backend audio into Wyoming `AudioStart`, `AudioChunk`, and `AudioStop` events with mocked streaming tests. Include WAV-header handling if required by mocked/backend format.

### Phase 5D ✅ (completed 2026-07-07)

Implemented. Added ``app/metrics.py`` with ``SynthesisMetrics`` / ``MetricsCollector``, wired into all three synthesis paths. 38 new tests; 128 total pass.

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

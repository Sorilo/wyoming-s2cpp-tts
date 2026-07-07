# Roadmap

This roadmap is the governing implementation sequence for `wyoming-s2cpp-tts`. Work only on the phase explicitly named in the current `/goal`; do not silently implement later phases.

## Approved architecture baseline

- Home Assistant connects to a Wyoming Protocol TTS endpoint on TCP port `10200`.
- The Python Wyoming adapter owns request validation, bounded queueing, backend communication, audio handling, progressive streaming, cancellation, and observability.
- `s2.cpp` is the Fish Speech S2 Pro inference runtime.
- The planned internal s2.cpp HTTP endpoint is `127.0.0.1:3030`.
- The optional health/debug HTTP endpoint is port `8088`.
- `TTS_BACKEND=fake` remains the safe default until an explicitly approved release phase changes that behavior.
- The first production baseline assumes one active s2.cpp synthesis at a time.
- The queue remains bounded, currently with planned/default maximum `3` unless implementation/testing establishes a better documented value.
- Models, voices, and configuration are mounted under `/models`, `/voices`, and `/config`.
- The final deployment target is an Unraid WebUI-friendly NVIDIA GPU Docker container.
- Initial hardware baseline: NVIDIA RTX 3080 10 GB.
- Possible later TTS hardware upgrade: NVIDIA RTX 5080 16 GB.
- Multi-worker, multi-model, and multi-GPU scheduling are post-v0.1 topics unless a future approved phase explicitly revises the baseline.

## Latency objective and measurement ownership

Optimize for low time-to-first-audio. The aspirational end-to-end target is under 2 seconds from detected end-of-speech through first audible playback for short, warm-path requests, including VAD endpointing.

This is an optimization target, not permission to make unsupported performance claims, weaken protocol correctness, hide buffering, omit error handling, or claim measurements that were not actually collected.

Desired end-to-end timestamps:

- `stt_done_at`
- `llm_request_start_at`
- `llm_first_token_at`
- `llm_first_sentence_at`
- `tts_request_start_at`
- `tts_first_backend_byte_at`
- `wyoming_first_audio_chunk_at`
- `ha_first_playback_at`

This repository can directly measure TTS request receipt, backend first byte, Wyoming first audio chunk, emitted bytes/chunks, cancellation, and request duration. STT, LLM, VAD, and actual playback timestamps require Home Assistant instrumentation, upstream service instrumentation, a correlated trace identifier, or a final end-to-end test harness. `ha_first_playback_at` may require satellite, player, Home Assistant, or client-side instrumentation and may not always be precisely measurable.

## Completed phases

### Phase 0: scaffold and docs

Implemented. Created the repository scaffold, architecture docs, roadmap, Docker placeholders, Python package skeleton, minimal tests, and initial commit.

### Phase 1: minimal Wyoming server with fake PCM/test audio

Implemented. The service can run a minimal Wyoming TCP fake TTS server that handles `Describe` and `Synthesize`, returning deterministic local PCM test-tone audio without touching s2.cpp or model inference.

### Phase 2: connect wrapper to existing s2.cpp HTTP `/generate`

Implemented at the backend-client level. `app/s2_client.py` can POST JSON to an already-running external `/generate` endpoint and return raw audio bytes. This is mocked in tests; no real s2.cpp success is claimed.

### Phase 2.5: opt-in non-streaming s2.cpp backend mode

Implemented. `TTS_BACKEND=fake` remains the default. `TTS_BACKEND=s2cpp` calls the tested s2.cpp HTTP client and converts one buffered raw PCM response into Wyoming `AudioStart`/`AudioChunk`/`AudioStop` events. Progressive streaming, WAV-header handling, and cancellation remain later phases.

### Phase 2.75: optional direct external s2.cpp smoke test

Implemented. `scripts/smoke_s2cpp_generate.py` uses `Settings.from_env()`, `TTS_BACKEND=s2cpp`, `S2_HOST`, and `S2_PORT` to send one direct `/generate` request when an external backend is already available. It skips harmlessly by default and reports unavailable without failing when no backend is running.

### Phase 3: container/process scaffold

Implemented at the container/process-structure level. The Dockerfile installs Python requirements, copies runtime code, creates `/models`, `/voices`, and `/config`, exposes `10200`/`8088`, and runs `entrypoint.sh`. The entrypoint starts `python -m app.main` and includes TODO hooks for future internal s2.cpp supervision on `127.0.0.1:3030`; it does not build or start s2.cpp yet.

### Phase 4: CUDA/s2.cpp and Unraid GPU runtime plan

Implemented as a documentation/static-validation phase. [`CUDA_S2CPP_PLAN.md`](CUDA_S2CPP_PLAN.md) records the untested future CUDA/s2.cpp build plan, relevant `sinfisum/s2pro-gguf` server flags, NVIDIA/Unraid runtime variables, and explicit non-claims. `scripts/check_gpu_visibility.sh` provides a safe `nvidia-smi` check that exits successfully when GPU tooling is unavailable.

## Approved next implementation phases

### Phase 5A: multipart/form-data s2.cpp client compatibility

Implemented. `app/s2_client.py` now has additive multipart/form-data request construction through `encode_multipart_form_data(...)` and `S2Client.generate_multipart(...)` while preserving the existing JSON `S2Client.generate(...)` buffered path. **Phase 5A.1** verified the multipart field structure against the sinfisum/s2pro-gguf reference client. **Phase 5A.2** corrected the emitted field names to the canonical `rodrigomatta/s2.cpp` OpenAPI spec: `reference` (file part, was `prompt_audio`), `reference_text` (was `prompt_text`), plus `voice` and `voice_dir`. Aliases (`prompt_audio`, `prompt_text`, `reference_audio`, `ref_audio`, `ref_text`) are normalised internally.

Acceptance criteria status:

- Existing fake backend and buffered s2cpp tests still pass.
- Multipart request construction is tested without a real backend.
- Required fields/files are documented, including unresolved upstream assumptions.
- No real s2.cpp, CUDA, GPU, or latency success is claimed.

### Phase 5A.1: verify and correct s2.cpp multipart request shape

Implemented. Verified the s2.cpp POST /generate multipart/form-data structure against the `sinfisum/s2pro-gguf` reference client (`s2_test_client.py`, commit `0cd2864`, retrieved 2026-07-07). Established that generation settings belong inside a single `params` JSON string, not as flattened multipart fields.

**Phase 5A.2 corrected the emitted field names** to match the actual project target `rodrigomatta/s2.cpp`.

---

### Phase 5A.2: align multipart fields with rodrigomatta/s2.cpp canonical spec

Implemented. Corrected the outgoing multipart field names to match the official target `rodrigomatta/s2.cpp` (`openapi/s2-openapi.yaml`). The canonical multipart fields are:

- `text` — required top-level string field
- `params` — one JSON-encoded string containing generation settings
- `reference` — optional file part (filename, bytes, media type); reference audio for voice cloning
- `reference_text` — optional string field; required when `reference` is present
- `voice` — optional top-level string field; saved `.s2voice` profile id or path
- `voice_dir` — optional top-level string field; directory used when `voice` is an id

Accepted upstream aliases (`prompt_audio`, `reference_audio`, `ref_audio` for `reference`; `ref_text`, `prompt_text` for `reference_text`; `voice_id`, `voice_profile` for `voice`) are normalised to canonical names on outgoing requests. The internal `prompt_text` field on `S2GenerateRequest` is preserved as a backward-compatible property.

Generation settings (`model`, `stream`, `chunked`, `output_format`, `temperature`, `top_p`, `top_k`, `max_new_tokens`, `segment_sentences`) are NOT top-level multipart fields — they belong inside the `params` JSON string.

Input validation rejects `reference` (or its aliases) without `reference_text`/`prompt_text`. `voice` can be used independently without reference audio, and both can be provided together.

Buffered multipart requests do not silently enable `stream`, `chunked`, `low_latency`, or raw PCM output.

Compatibility is validated against the official `rodrigomatta/s2.cpp` OpenAPI spec (`openapi/s2-openapi.yaml`) and mocked tests only. Real s2.cpp compatibility remains unverified until Phase 5.5.

Acceptance criteria status:

- Canonical emitted fields match `rodrigomatta/s2.cpp` OpenAPI spec.
- Alias normalisation: `prompt_audio` → `reference`, `prompt_text` → `reference_text`.
- `voice` and `voice_dir` emitted as canonical top-level fields when configured.
- `reference` without `reference_text` raises `ValueError`.
- `voice` without reference audio is supported.
- `voice` and `reference` can be provided together (no client-side conflict).
- Empty optional fields are omitted.
- Buffered requests do not add `stream`/`chunked`/`low_latency` to `params`.
- Existing JSON-buffered and fake Wyoming tests still pass.
- 21 s2_client tests pass (4 JSON-buffered + 17 multipart/encoder).
- Full 47-test suite passes.
- No real s2.cpp, CUDA, GPU, Docker, or latency success claimed.

### Phase 5A.1: verify and correct s2.cpp multipart request shape

Implemented. Verified the s2.cpp POST /generate multipart/form-data structure against the `sinfisum/s2pro-gguf` reference client (`s2_test_client.py`, commit `0cd2864`, retrieved 2026-07-07). Established that generation settings belong inside a single `params` JSON string, not as flattened multipart fields.

**Phase 5A.2 corrected the emitted field names** to match the actual project target `rodrigomatta/s2.cpp`.

---

### Phase 5A.2: align multipart fields with rodrigomatta/s2.cpp canonical spec

Implemented. Corrected the outgoing multipart field names to match the official target `rodrigomatta/s2.cpp` (`openapi/s2-openapi.yaml`). The canonical multipart fields are:

- `text` — required top-level string field
- `params` — one JSON-encoded string containing generation settings
- `reference` — optional file part (filename, bytes, media type); reference audio for voice cloning
- `reference_text` — optional string field; required when `reference` is present
- `voice` — optional top-level string field; saved `.s2voice` profile id or path
- `voice_dir` — optional top-level string field; directory used when `voice` is an id

Accepted upstream aliases (`prompt_audio`, `reference_audio`, `ref_audio` for `reference`; `ref_text`, `prompt_text` for `reference_text`; `voice_id`, `voice_profile` for `voice`) are normalised to canonical names on outgoing requests. The internal `prompt_text` field on `S2GenerateRequest` is preserved as a backward-compatible property.

Generation settings (`model`, `stream`, `chunked`, `output_format`, `temperature`, `top_p`, `top_k`, `max_new_tokens`, `segment_sentences`) are NOT top-level multipart fields — they belong inside the `params` JSON string.

Input validation rejects `reference` (or its aliases) without `reference_text`/`prompt_text`. `voice` can be used independently without reference audio, and both can be provided together.

Buffered multipart requests do not silently enable `stream`, `chunked`, `low_latency`, or raw PCM output.

Compatibility is validated against the official `rodrigomatta/s2.cpp` OpenAPI spec (`openapi/s2-openapi.yaml`) and mocked tests only. Real s2.cpp compatibility remains unverified until Phase 5.5.

Acceptance criteria status:

- Canonical emitted fields match `rodrigomatta/s2.cpp` OpenAPI spec.
- Alias normalisation: `prompt_audio` → `reference`, `prompt_text` → `reference_text`.
- `voice` and `voice_dir` emitted as canonical top-level fields when configured.
- `reference` without `reference_text` raises `ValueError`.
- `voice` without reference audio is supported.
- `voice` and `reference` can be provided together (no client-side conflict).
- Empty optional fields are omitted.
- Buffered requests do not add `stream`/`chunked`/`low_latency` to `params`.
- Existing JSON-buffered and fake Wyoming tests still pass.
- 21 s2_client tests pass (4 JSON-buffered + 17 multipart/encoder).
- Full 47-test suite passes.
- No real s2.cpp, CUDA, GPU, Docker, or latency success claimed.


### Phase 5B: streaming async iterator over s2.cpp response bytes

Implemented. ``app/s2_client.py`` now has ``S2StreamResult`` (a resource-safe context manager / iterator that yields audio chunks via ``response.read(4096)`` without buffering the entire response) and ``S2Client.generate_stream()`` (builds canonical multipart with ``stream=true``, ``chunked=true``, ``output_format="pcm_s16le"``, ``low_latency=true`` in ``params`` JSON). ``S2GenerateRequest.to_multipart_fields(streaming=True)`` injects streaming flags without duplicating multipart-building logic.

Acceptance criteria status:

- Mocked backend chunks can be consumed by an iterator that does not read the full response first. ✅ (15 new tests)
- Errors and partial streams are represented clearly: HTTP errors raise ``S2ClientError`` before iteration; read errors mid-stream raise ``S2ClientError`` and close the response; early consumer exit (``break``) closes the response via ``__exit__``. ✅
- No Wyoming streaming behavior is changed: ``app/wyoming_server.py`` is untouched. ``TTS_BACKEND=fake`` remains default. ✅
- All 47 existing tests (buffered JSON, canonical multipart, fake Wyoming) still pass; 62 total tests pass. ✅
- No real streaming success is claimed without a tested backend. ✅

### Phase 5C: streamed audio to Wyoming events

Pipe streamed backend audio into Wyoming `AudioStart`, `AudioChunk`, and `AudioStop` events with mocked streaming tests.

Acceptance criteria:

- Fake backend remains default.
- Mocked streamed raw PCM is emitted progressively as Wyoming chunks.
- WAV-header handling is implemented if mocked/backend format requires it; otherwise raw PCM assumptions are clearly documented.
- Existing buffered behavior is preserved or safely migrated with tests.

### Phase 5D: TTS-side metrics and structured tracing

Add TTS-side metrics and structured tracing for:

- request start
- first backend byte
- first Wyoming audio chunk
- total emitted bytes
- emitted chunk count
- request/stream duration
- trace/request identifier where practical

Acceptance criteria:

- Metrics/traces are tested with fake or mocked backend paths.
- This repository clearly distinguishes locally measurable TTS timestamps from STT/LLM/VAD/playback timestamps that require external instrumentation.
- No end-to-end latency claims are made without an actual end-to-end harness.

### Phase 5.5: real external s2.cpp smoke test outside final Docker image

Run a real external s2.cpp smoke test only when an already-running backend and required model/tokenizer files are available outside the final Docker image.

Acceptance criteria:

- The smoke test is opt-in and harmless when unavailable.
- Results document exact backend endpoint, payload mode, content type, byte count, and whether audio was playable if checked.
- No Docker/CUDA build success is inferred from this smoke test.

### Phase 6A: Wyoming client disconnect and backend cancellation

Handle Wyoming client disconnects and backend cancellation where supported.

Acceptance criteria:

- Simulated disconnect tests cover cleanup.
- Backend cancellation is implemented only where the backend interface supports it; otherwise limitations are documented.
- Partial streams do not leave queue state stuck.

### Phase 6B: queue cancellation, backend busy handling, and timeout policy

Implement queue cancellation, backend busy handling, timeout policy, and policy for a new request arriving during active speech.

Acceptance criteria:

- The single-active-synthesis baseline remains clear.
- Queue max size and busy behavior are documented and tested.
- New-request policy is explicit; default remains no cancellation on new request unless changed by tested policy.

### Phase 6C: barge-in-friendly behavior tests

Test barge-in-friendly behavior using Home Assistant when available or simulated disconnect/cancellation tests otherwise.

Acceptance criteria:

- Claims distinguish TTS-side cancellation from full Home Assistant/satellite barge-in.
- Tests or manual evidence are documented.
- Unsupported player/satellite behavior is not hidden.

### Phase 7A: comprehensive tests and troubleshooting docs

Add comprehensive protocol, queue, error, cancellation, and integration tests plus troubleshooting documentation.

Acceptance criteria:

- Troubleshooting covers ports, GPU visibility, models, voices, audio format, backend reachability, queue/busy behavior, and Wyoming/Home Assistant connection issues.
- Tests remain cheap by default and do not require GPU/model infrastructure unless explicitly opted in.

### Phase 7B: v0.1 release checklist and tagging criteria

Create the v0.1 release checklist and tagging criteria. Do not tag v0.1 unless required behavior has actually been verified.

Acceptance criteria:

- Release checklist distinguishes mocked, static, smoke-tested, and fully verified behavior.
- Required verification evidence is listed.
- No tag is created unless the current goal explicitly authorizes it and the evidence exists.

### Phase 8A: CUDA-enabled s2.cpp Docker image

Build and test the CUDA-enabled s2.cpp Docker image.

Acceptance criteria:

- Docker/CUDA build commands are actually run before success is claimed.
- s2.cpp source/binary provenance is documented.
- Model/tokenizer downloads remain explicit and are not hidden in normal tests.

### Phase 8B: Unraid WebUI template and deployment validation

Finalize the Unraid WebUI template/documentation and validate NVIDIA GPU passthrough, ports, mounts, permissions, startup, process supervision, health checks, shutdown, restart behavior, and persistence.

Acceptance criteria:

- Validation is performed on the intended Unraid/NVIDIA environment or clearly marked as unverified.
- Port `3030` remains internal unless intentionally exposed for debug.

### Phase 8C: final Home Assistant end-to-end test

Run the final Home Assistant end-to-end test, including Assist pipeline connection, real STT-to-conversation-to-TTS operation, streamed playback, audio correctness, cancellation/barge-in behavior where supported, and latency measurements where measurable.

Acceptance criteria:

- Measured timestamps and ownership are documented.
- Claims are limited to what was actually observed.
- Any missing HA/satellite/player instrumentation is called out.

## Post-v0.1 future work

- Multiple model profiles.
- Multiple voice/reference profiles.
- Higher-quality quantizations.
- Multi-worker scheduling.
- Multi-GPU routing.
- Advanced local-versus-cloud LLM routing.
- Hardware-upgrade benchmarking, including possible RTX 5080 16 GB evaluation.
- Broader monitoring and dashboard integration.

## Roadmap governance rules

1. Work only on the phase explicitly named in the current `/goal`.
2. Do not silently implement later phases.
3. Preserve completed behavior and keep existing tests passing.
4. Keep `TTS_BACKEND=fake` as the default until an explicitly approved phase changes release behavior.
5. Never claim real s2.cpp, CUDA, GPU, Home Assistant, streaming, cancellation, audio-quality, or latency success unless actually tested.
6. If an unexpected prerequisite is discovered, recommend a narrowly scoped intermediate phase such as Phase 5A.1 or Phase 6B.1.
7. Every proposed intermediate phase must state why it is required, which approved phase it blocks, exact scope, acceptance criteria, and whether it changes the approved architecture.
8. Do not automatically expand the current run into an intermediate or later phase unless the work is inseparable, small, low risk, and necessary to complete the current phase correctly.
9. If newly discovered work is substantial, stop at a safe state and generate it as the next recommended `/goal`.
10. Prefer one focused commit per phase or intermediate phase.
11. Update `ROADMAP.md`, `TODO.md`, `NEXT_GOAL_PROMPTS.md`, and `CHANGELOG.md` when phase status changes.
12. Preserve accurate historical descriptions of completed Phases 0 through 4.
13. Static inspection, mocks, or documentation do not count as real GPU, CUDA, s2.cpp, streaming, Home Assistant, or latency validation.
14. Startup-buffer values must remain configurable and benchmark-driven. Do not treat existing placeholder values such as `1000 ms` or `4000 ms` as validated production defaults.
15. Avoid unrelated cleanup or refactoring unless it directly prevents completion of the current phase.
16. Base every next-phase prompt on the actual repository state, completed tests, remaining TODOs, and discoveries from the current run.

## Mandatory next-prompt handoff policy

At the end of every `/goal` run, Hermes must output a complete ready-to-paste `/goal` prompt for the next required phase or justified intermediate phase.

The generated next prompt must:

- be based on repository state after the current changes;
- name the exact next incomplete phase or justified intermediate phase;
- include the project path;
- include appropriate quota/risk protections;
- state what files and repository areas should be inspected;
- define exact scope and exclusions;
- define measurable acceptance criteria;
- define required mocked or real tests;
- state which claims remain unverified;
- require one focused commit;
- require documentation/status updates;
- require Hermes to generate the following phase’s complete prompt at the end;
- avoid copying stale assumptions when the current implementation shows otherwise.

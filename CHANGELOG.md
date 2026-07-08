# Changelog

## Unreleased

- Phase 6A: wired the verified real `s2.cpp` raw-PCM response contract into the
  Wyoming wrapper runtime path without rebuilding Docker images or modifying the
  external backend container.
- Added shared declared-PCM validation for `audio/L16` / `pcm_s16le` responses:
  required encoding, sample-rate/channel metadata, conflict detection between
  `Content-Type` parameters and `X-Audio-*` headers, non-empty buffered audio,
  and 16-bit frame alignment.
- Buffered `TTS_BACKEND=s2cpp` Wyoming synthesis now derives `AudioStart` and
  `AudioChunk` metadata from validated backend response headers instead of the
  fake-audio defaults.
- Streaming Wyoming synthesis validates response metadata before `AudioStart`
  when stream metadata is available, then emits progressive frame-aligned chunks
  with the validated backend sample rate/channels.
- Added runtime contract tests for real-contract buffered and streaming PCM,
  missing/contradictory metadata rejection, unaligned PCM errors, and preserved
  fake/default mocked behavior (238 total tests pass).

- Phase 5.5B: verified the smoke harness against a real external
  `rodrigomatta/s2.cpp` backend at `s2cpp-backend:3030` without rebuilding the
  image or modifying the Unraid container.
- Added validated buffered raw-PCM acceptance for declared `pcm_s16le` /
  `audio/L16` responses with non-empty audio, valid sample-rate/channel
  metadata, no contradictory `Content-Type` vs `X-Audio-*` metadata, and
  frame-aligned 16-bit PCM bytes; declared `audio/wav` responses still require
  a valid RIFF/WAVE header.
- Added buffered smoke-result fields for audio format, validity, PCM frame
  alignment, sample rate, channels, audio duration, and validation error.
- Tightened Phase 5.5B success criteria: buffered audio must be valid WAV or
  valid declared PCM; streaming must have valid metadata, frame-aligned PCM,
  and `verified_progressive` delivery.
- Captured buffered response headers in `S2GenerateResult` so the harness can
  validate real backend `X-Audio-*` metadata.
- Added 8 mocked buffered-PCM compatibility tests (73 smoke-harness tests; 226
  total tests pass).
- Real backend verification command returned `phase_5_5b_status =
  real_backend_verified` with no warnings; see
  `docs/PHASE_5_5B_REAL_BACKEND_VERIFICATION.md`.

- Phase 5.5A: implemented opt-in real-backend smoke-test harness.
- Added ``app/smoke_harness.py`` with ``SmokeConfig``, ``BufferedMultipartResult``,
  ``StreamingMultipartResult``, ``LegacyJsonResult``, ``SmokeReport``, WAV header
  validation, PCM frame-alignment validation, audio response-header parsing,
  streaming progressive-delivery classification, and ``run_smoke_harness()``
  orchestrator with reachability probe.
- Added ``status_code`` and ``response_headers`` properties to ``S2StreamResult``
  for smoke-test audio-metadata validation.
- Rewrote ``scripts/smoke_s2cpp_generate.py`` as the Phase 5.5 CLI with
  ``--run-real`` (explicit opt-in), ``--require-backend`` (nonzero exit),
  ``--endpoint`` override, ``--probe-legacy-json``, ``--output-dir``, and
  ``--json`` machine-readable output.
- Added 65 mocked smoke-harness tests (193 total pass): cover opt-in gates,
  WAV/PCM validation, streaming progressive/inconclusive classification,
  audio-header parsing, error categorisation, structured output, timeout/error
  cleanup, and the full orchestrator path. No real backend contacted during
  the ordinary test suite.
- ``TTS_BACKEND=fake`` remains default; all existing 128 tests continue to pass.
- Phase 5.5B (real backend verification) is pending — requires an already-running
  ``rodrigomatta/s2.cpp`` backend.
- No real s2.cpp, CUDA, GPU, Docker, Home Assistant, cancellation, or latency
  success claimed from this harness implementation alone.


- Phase 5D: implemented lightweight structured TTS metrics and tracing.
- Added ``app/metrics.py`` with ``SynthesisMetrics`` frozen dataclass (request_id,
  trace_id, backend_type, synthesis_mode, monotonic timestamps for request start,
  first backend data, first AudioChunk, terminal, emitted bytes/chunks, terminal
  status, error type, duration) and ``MetricsCollector`` mutable per-request
  collector with dependency-injectable clock for deterministic tests.
- Wired metrics into ``synthesize_fake_tts_events()`` (fake path),
  ``synthesize_s2cpp_tts_events()`` (buffered s2.cpp), and
  ``synthesize_s2cpp_streaming_tts_events()`` (streaming s2.cpp). All three
  accept an optional ``MetricsCollector``; when none is supplied, one is created
  internally and metrics are logged as structured info messages.
- Metrics finalize exactly once on success, backend/PCM exception, early
  consumer close (``GeneratorExit`` / ``asyncio.CancelledError``), and observed
  coroutine cancellation. Original exceptions and cancellation propagate — never
  swallowed.
- Timestamp semantics: monotonic nanoseconds (``time.monotonic_ns``) for all
  durations. Buffered ``first_backend_data_ns`` is documented as post-buffer
  availability, not network first byte. Streaming ``first_backend_data_ns`` is
  first non-empty chunk observed by this process. ``first_audio_chunk_ns`` means
  produced by this repository — not transmitted over Wyoming, received by HA,
  decoded by satellite, or played through speaker.
- Privacy: log output and metrics snapshots exclude request text, raw audio,
  reference-audio paths, and credentials.
- 38 new tests (12 collector unit, 6 fake metrics, 5 buffered metrics, 10
  streaming metrics, 5 lifecycle/cancellation); 128 total tests pass.
- ``TTS_BACKEND=fake`` remains default; runtime handler unchanged; no real
  s2.cpp, CUDA, GPU, Docker, Home Assistant, cancellation, or latency success
  claimed.

- Phase 5C: implemented streamed audio to Wyoming events with mocked streaming tests.
- Added ``StreamingPCMRechunker`` in ``app/audio.py`` — bounded streaming rechunker that
  handles partial PCM frames across HTTP transport boundaries, combines/splits transport
  chunks into Wyoming-sized ``AudioChunk`` payloads, computes timestamps from cumulative
  emitted PCM frames, and raises ``ValueError`` on final incomplete frames.
- Added ``synthesize_s2cpp_streaming_tts_events()`` in ``app/wyoming_server.py`` — async
  generator that drives ``S2StreamResult`` via ``asyncio.to_thread`` (blocking HTTP reads
  never run on the event loop), feeds the rechunker, and emits ``AudioStart`` →
  progressive ``AudioChunk`` → ``AudioStop`` events.
- Added ``_read_stream_chunk`` / ``_STREAM_EOF`` sentinel pattern to safely transport
  ``StopIteration`` through ``run_in_executor`` in Python 3.13.
- Error semantics: backend failures propagate ``S2ClientError`` without emitting
  ``AudioStop``; final incomplete PCM frames raise ``ValueError``; stream cleanup
  on normal completion, error, and early consumer exit (async generator ``aclose()``).
- 28 new mocked tests (90 total): rechunker frame alignment, carry-over, combining,
  splitting, timestamps, incomplete-frame rejection; Wyoming event ordering,
  progressive emission, PCM preservation, error propagation, stream lifecycle,
  thread offloading, no complete buffering.
- Existing buffered ``synthesize_s2cpp_tts_events()``, ``synthesize_fake_tts_events()``,
  and ``FakeTtsEventHandler`` preserved; ``TTS_BACKEND=fake`` remains default.
- No real s2.cpp, CUDA, GPU, Docker, Home Assistant, cancellation, or latency success
  claimed — Phase 5C is validated with mocked streams only.

- Phase 5B: implemented streaming client interface for progressive s2.cpp audio delivery.
- Added ``S2StreamResult`` class — a resource-safe context manager / iterator that yields
  backend response bytes one chunk at a time via ``response.read(4096)`` without buffering
  the entire response.
- Added ``S2Client.generate_stream()`` method — builds a canonical multipart request with
  ``stream=true``, ``chunked=true``, ``output_format="pcm_s16le"``, and ``low_latency=true``
  in the ``params`` JSON string, then returns a ``S2StreamResult``.
- Added ``streaming`` parameter to ``S2GenerateRequest.to_multipart_fields()`` for
  injecting streaming flags without duplicating multipart-building logic.
- Streaming lifecycle: HTTP connection stays open during chunk consumption; closes on
  normal completion, backend error, or early consumer exit (``break`` from loop).
- 15 new mocked streaming tests (62 total): progressive chunk yielding, deterministic
  first-chunk-before-full-response proof, cleanup on normal/break/error paths, streaming
  params verification, canonical field preservation, content_type access, input validation.
- No real s2.cpp, CUDA, GPU, Docker, Wyoming streaming, or latency success claimed.
- ``TTS_BACKEND=fake`` remains default; all existing 47 buffered/fake tests still pass.

- Phase 5A.2: corrected multipart field names to match the canonical `rodrigomatta/s2.cpp` OpenAPI spec (`openapi/s2-openapi.yaml`).
- Canonical emitted fields: `reference` (file part, was `prompt_audio`), `reference_text` (was `prompt_text`), `voice`, `voice_dir`.
- `prompt_audio`, `prompt_text`, `reference_audio`, `ref_audio`, `ref_text` are accepted upstream aliases; the client normalises them to canonical names on outgoing requests.
- Added `voice` as a canonical top-level multipart field for saved `.s2voice` profiles.
- Added `voice_dir` as a canonical top-level multipart field.
- `voice` and `voice_dir` are also wired through `S2GenerateRequest.from_settings()` from `config.py`.
- Input validation: `reference` (or alias) without `reference_text`/`prompt_text` raises `ValueError`.
- Buffered multipart requests do not silently enable `stream`, `chunked`, or `low_latency`.
- 21 s2_client tests, full 47-test suite passes.
- Phase 5A.1: verified and corrected the s2.cpp multipart/form-data request shape against the upstream reference client (sinfisum/s2pro-gguf s2_test_client.py).
- Canonical multipart fields are now: `text` (required string), `params` (one JSON string with generation settings), optional `prompt_text`, and optional `prompt_audio` file part.
- Generation settings are no longer flattened into individual multipart fields (`model`, `voice`, `stream`, `chunked` etc. are NOT top-level).
- Added `prompt_text` field to `S2GenerateRequest` and input validation: `prompt_audio` without `prompt_text` raises `ValueError`.
- Added comprehensive canonical-format multipart tests (16 total s2_client tests, all passing).
- Compatibility validated against upstream documentation/source and mocked tests only; real s2.cpp compatibility remains unverified until Phase 5.5.
- Preserved `TTS_BACKEND=fake` default, all existing JSON-buffered and fake Wyoming behavior.
- Documented upstream API source, multipart fields, and unresolved assumptions.
- Implemented Phase 5A multipart/form-data s2.cpp client compatibility with mocked request-construction tests.
- Added additive `S2Client.generate_multipart(...)` and `encode_multipart_form_data(...)` while preserving existing JSON buffered behavior.
- Documented unresolved upstream assumptions about exact multipart field/file names.
- Aligned roadmap governance docs to the approved Phase 5A-8C implementation sequence.
- Replaced broad future Phase 5/6/7/8 prompts with narrowly scoped phase prompts and mandatory next-prompt handoff policy.
- Documented latency objective, TTS-side measurement ownership, and external instrumentation boundaries.
- Reconciled TODOs with the v0.1 roadmap and moved multi-worker/multi-model/multi-GPU/hardware-upgrade work to post-v0.1 future work.
- Corrected stale Home Assistant/Unraid status language without changing runtime behavior.
- Implemented Phase 4 CUDA/s2.cpp and Unraid NVIDIA runtime plan without building or downloading models.
- Added `docs/CUDA_S2CPP_PLAN.md` with untested build assumptions, s2.cpp server flag references, and future Dockerfile shape.
- Added safe `scripts/check_gpu_visibility.sh` for future in-container `nvidia-smi` validation.
- Added Dockerfile Phase 4 TODO placeholders for future CUDA/s2.cpp multi-stage build.
- Implemented Phase 3 Dockerfile/entrypoint process scaffold for running the Python Wyoming wrapper in a container.
- Added future `S2CPP_ENABLE_INTERNAL_SERVER` hook/TODOs for supervised s2.cpp startup on `127.0.0.1:3030`.
- Documented Phase 3 Unraid path, port, and environment variable expectations.
- Added static tests for Dockerfile, entrypoint, and container capability docs.
- Implemented Phase 2.75 optional direct s2.cpp `/generate` smoke-test script that skips harmlessly unless opted in.
- Added mocked tests for the smoke helper success, skip, and unavailable outcomes.
- Documented direct smoke-test inputs, outputs, and limitations.
- Implemented Phase 2.5 opt-in `TTS_BACKEND=s2cpp` Wyoming route for one buffered backend response.
- Kept `TTS_BACKEND=fake` as the default fake PCM/Home Assistant test mode.
- Added mocked tests for converting s2.cpp client results into Wyoming `AudioStart`/`AudioChunk`/`AudioStop` events.
- Implemented Phase 2 s2.cpp HTTP `/generate` client for an already-running backend.
- Added minimal `S2_HOST`/`S2_PORT` environment loading for external backend targeting.
- Added mocked tests for backend request payloads, endpoint selection, omitted empty voices, and HTTP failure handling.
- Documented how to point the client at an external s2.cpp test server.
- Implemented Phase 1 minimal Wyoming TCP fake TTS server.
- Added deterministic PCM test-tone generation for fake synthesis.
- Added Wyoming `Describe` metadata and `Synthesize` -> `AudioStart`/`AudioChunk`/`AudioStop` handling.
- Added bounded single-worker queue scaffolding for initial single-active-synthesis policy.
- Added tests for fake audio, queue capacity, and Wyoming TCP roundtrip.
- Added architecture, roadmap, Unraid, and Home Assistant setup drafts.
- Added placeholder Dockerfile, entrypoint, Python package, and tests.

# Changelog

## Unreleased

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

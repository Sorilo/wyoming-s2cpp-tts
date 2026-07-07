# Changelog

## Unreleased

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

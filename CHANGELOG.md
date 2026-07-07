# Changelog

## Unreleased

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

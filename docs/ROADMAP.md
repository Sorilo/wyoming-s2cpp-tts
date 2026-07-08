# Roadmap

This roadmap is the governing implementation sequence for `wyoming-s2cpp-tts`.
Work only on the phase explicitly named in the current `/goal`; do not silently
implement later phases.

## Approved architecture baseline

- **Two-container design:** CPU-only Wyoming wrapper + separate CUDA s2.cpp backend,
  both on the `sorilonet` Docker network.
- Wrapper translates Wyoming TTS requests into HTTP multipart/form-data calls to
  `http://s2cpp-backend:3030/generate`.
- Home Assistant connects at `192.168.1.45:10200` (Wyoming Protocol).
- Full Wyoming streaming TTS lifecycle is verified: `synthesize-start`,
  `synthesize-chunk`, `synthesize-stop`, `AudioStart`, `AudioChunk`, `AudioStop`,
  and `synthesize-stopped`, plus legacy `synthesize`.
- Backend generates `audio/L16; rate=44100; channels=1` raw s16le PCM.
- `TTS_BACKEND=s2cpp` is the production Docker default.
- Single active synthesis at a time, bounded queue (max 3).
- `S2_STREAM` is parsed/configured and a progressive HTTP streaming helper exists,
  but the production handler still uses buffered `generate_multipart()` until Phase 7.5.

## Completed phases

### Phase 0-4: scaffold, Wyoming server, s2.cpp client, container scaffold, CUDA plan \u2705

### Phase 5A-5D: multipart client, streaming helpers, Wyoming events, metrics \u2705

### Phase 5.5A: smoke-test harness \u2705

### Phase 5.5B: real backend verification \u2705
Backend contract verified: `audio/L16; rate=44100; channels=1` via multipart/form-data.

### Phase 6A: backend CUDA image \u2705
Built and deployed `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b`.

### Phase 6B0: wrapper Docker image \u2705
Built and deployed the CPU-only wrapper. Added GitHub Actions workflow for GHCR publication.

### Phase 6B1: multipart fix + dynamic Describe \u2705
Fixed `generate()` \u2192 `generate_multipart()`. Describe returns real metadata.

### Phase 6C: Wyoming streaming TTS state machine \u2705
HA preview hang fixed. `synthesize-stopped` emitted. 287 tests pass.

### Phase 6D: Home Assistant deployment verification \u2705
Real speech audible through Home Assistant. Wyoming streaming lifecycle verified.

### Phase 6E: deployment safety corrections and forward-plan refinement \u2705
Pinned Unraid templates to verified immutable images, corrected stale README/deployment documentation, documented the `S2_STREAM` production-handler caveat, and split the remaining roadmap.

### Phase 7A: CMU ARCTIC voice profile creation \u2705
Six `.s2voice` profiles created from CMU ARCTIC reference recordings (bdl, rms, jmk, slt, clb, eey) under `/mnt/user/appdata/s2cpp/voices`. All six visible via `s2 --list-voices`. Direct backend multipart synthesis: 6/6 passed (valid RIFF/WAVE). Human listening: acceptable temporary voices, somewhat robotic, no downstream defect; personal recording planned. Caveats: FestVox HTTPS unreachable (HTTP fallback); `--list-voices` requires GPU runtime. Wrapper, images, and HA unchanged.

## Remaining phases

### Phase 7B: wrapper voice discovery and Home Assistant voice selection ✅
Wrapper discovers `.s2voice` profiles, exposes them through Describe, supports client voice selection, `S2_DEFAULT_VOICE`, drop-in discovery, and generic `s2-pro` fallback. 38 new tests, 323/325 passing. Wrapper image published.

### Phase 7.5A: true progressive backend HTTP audio streaming ✅
When `S2_STREAM=true`, the production handler uses `synthesize_s2cpp_streaming_tts_events()` / `generate_stream()` to yield Wyoming audio events progressively as backend transport chunks arrive. When `S2_STREAM=false`, the existing buffered `generate_multipart()` path is preserved. Wyoming streaming-text state machine, compatibility synthesize deferral, voice propagation, and fake backend all preserved. Structured observability extended with streaming-specific timing fields. 13 new tests, 367/368 passing. Phase 7.5B (live latency verification) remains.

### Phase 8: disconnect cleanup and backend cancellation limitations
Detect client disconnect/write failure, cancel active async synthesis, close any open backend stream/HTTP response, stop forwarding chunks, and document that closing the HTTP client connection may not stop all GPU work if upstream lacks an active cancellation API.

### Phase 9: queue, busy handling, and timeout policy
Define queue capacity behavior, busy responses, backend HTTP 503 handling, queue wait timeout, synthesis timeout, and controlled Wyoming failure behavior.

### Phase 10: end-to-end barge-in testing with HA satellite/player
Test with an actual Home Assistant satellite/player path including VAD, wake word, playback interruption, and new-request behavior.

### Phase 11: Faster-Whisper/full Assist pipeline integration and latency measurement
Integrate or measure the broader Assist path and correlate STT, LLM, VAD, TTS, and playback timings.

### Phase 12: comprehensive reliability tests and troubleshooting
Add reliability coverage, operational troubleshooting, and failure-mode documentation.

### Phase 13: v0.1 release checklist, tagging, and rollback criteria
Define and execute the v0.1 release checklist, tags, image pins, rollback criteria, and known limitations.

### Phase 14: final Unraid templates, persistence, restart, update, and backup testing
Finalize templates after real restart/update/persistence/backup validation.

## Post-v0.1

- Multiple model profiles / quantizations
- Multi-worker / multi-GPU routing
- Hardware upgrade benchmarking

## Governance

1. Work only on the phase named in the current `/goal`.
2. Preserve completed behavior and tests.
3. Never claim real behavior unless actually tested.
4. One focused commit per phase.
5. Update `ROADMAP.md`, `TODO.md`, `NEXT_GOAL_PROMPTS.md`, and `CHANGELOG.md` when status changes.

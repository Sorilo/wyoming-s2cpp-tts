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

### Phase 0-4: scaffold, Wyoming server, s2.cpp client, container scaffold, CUDA plan ✅

### Phase 5A-5D: multipart client, streaming helpers, Wyoming events, metrics ✅

### Phase 5.5A: smoke-test harness ✅

### Phase 5.5B: real backend verification ✅
Backend contract verified: `audio/L16; rate=44100; channels=1` via multipart/form-data.

### Phase 6A: backend CUDA image ✅
Built and deployed `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b`.

### Phase 6B0: wrapper Docker image ✅
Built and deployed the CPU-only wrapper. Added GitHub Actions workflow for GHCR publication.

### Phase 6B1: multipart fix + dynamic Describe ✅
Fixed `generate()` → `generate_multipart()`. Describe returns real metadata.

### Phase 6C: Wyoming streaming TTS state machine ✅
HA preview hang fixed. `synthesize-stopped` emitted. 287 tests pass.

### Phase 6D: Home Assistant deployment verification ✅
Real speech audible through Home Assistant. Wyoming streaming lifecycle verified.

### Phase 6E: deployment safety corrections and forward-plan refinement ✅
Pinned Unraid templates to verified immutable images, corrected stale README/deployment documentation, documented the `S2_STREAM` production-handler caveat, and split the remaining roadmap.

## Remaining phases

### Phase 7A: one-time custom `.s2voice` profile creation and direct backend verification (NEXT)
Create one custom voice profile using a user-supplied, consented 5-30 second clean recording plus exact transcript. Use the s2 CLI with `--prompt-audio`, `--prompt-text`, `--voice`, `--save-voice`, and `--voice-dir`; write the `.s2voice` file to the persistent `/voices` host mount; verify direct backend synthesis with `voice=<profile id>`. Do not modify the wrapper in Phase 7A.

### Phase 7B: wrapper voice discovery and Home Assistant voice selection
Mount `/voices` read-only into the wrapper or explicitly justify an alternative. Safely enumerate `.s2voice` files, sanitize profile IDs, expose selectable voices through Wyoming Describe, read requested Wyoming voice selection, pass `voice` and `voice_dir` in multipart requests, support `S2_DEFAULT_VOICE`, preserve generic `s2-pro` fallback, verify Home Assistant selection, and publish one immutable wrapper image after tests pass.

### Phase 7.5: true progressive backend HTTP audio streaming
Tests first. When `S2_STREAM=true`, route production synthesis through `synthesize_s2cpp_streaming_tts_events()` without building a complete list of audio events before writing. Preserve `S2_STREAM=false` as buffered fallback and preserve Wyoming streaming-text behavior.

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

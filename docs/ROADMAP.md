# Roadmap

This roadmap is the governing implementation sequence for `wyoming-s2cpp-tts`.
Work only on the phase explicitly named in the current `/goal`; do not silently
implement later phases.

## Approved architecture baseline (revised)

- **Two-container design:** CPU-only Wyoming wrapper + separate CUDA s2.cpp backend,
  both on the `sorilonet` Docker network.
- Wrapper translates Wyoming TTS requests into HTTP multipart/form-data calls to
  `http://s2cpp-backend:3030/generate`.
- Home Assistant connects at `192.168.1.45:10200` (Wyoming Protocol).
- Full Wyoming streaming TTS lifecycle: synthesize-start, synthesize-chunk,
  synthesize-stop, synthesize-stopped + legacy synthesize.
- Backend generates `audio/L16; rate=44100; channels=1` raw s16le PCM.
- `TTS_BACKEND=s2cpp` is the production Docker default.
- Single active synthesis at a time, bounded queue (max 3).

## Completed phases

### Phase 0-4: scaffold, Wyoming server, s2.cpp client, container scaffold, CUDA plan ✅

### Phase 5A-5D: multipart client, streaming, Wyoming events, metrics ✅

### Phase 5.5A: smoke-test harness ✅

### Phase 5.5B: real backend verification ✅
Backend contract verified: audio/L16; rate=44100; channels=1.

### Phase 6A: backend CUDA image ✅
Built and deployed `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b`.

### Phase 6B0: wrapper Docker image ✅
Built and deployed. GitHub Actions workflow for GHCR publication.

### Phase 6B1: multipart fix + dynamic Describe ✅
Fixed generate() -> generate_multipart(). Describe returns real metadata.

### Phase 6C: streaming TTS state machine ✅
HA preview hang fixed. synthesize-stopped emitted. 287 tests pass.

### Phase 6D: Home Assistant deployment verification ✅
Real speech audible. Streaming lifecycle verified.

## Next phases (renumbered)

### Phase 7: custom voice profiles (NEXT)
Create/persist/select voice profiles. Promoted from post-v0.1 per user requirement.

### Phase 8: disconnect cleanup and backend cancellation

### Phase 9: queue, busy handling, timeout policy

### Phase 10: barge-in testing with HA satellite/player

### Phase 11: full Assist pipeline and latency measurement

### Phase 12: comprehensive tests and troubleshooting

### Phase 13: v0.1 release checklist

### Phase 14: Unraid template finalization

## Post-v0.1

- Multiple model profiles / quantizations
- Multi-worker / multi-GPU routing
- Hardware upgrade benchmarking

## Governance

1. Work only on the phase named in the current /goal.
2. Preserve completed behavior and tests.
3. Never claim real behavior unless actually tested.
4. One focused commit per phase.
5. Update ROADMAP.md, TODO.md, NEXT_GOAL_PROMPTS.md, CHANGELOG.md when status changes.

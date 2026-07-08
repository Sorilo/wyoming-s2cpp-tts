# TODO

## Completed through Phase 6E (2026-07-08)

1. Scaffold, minimal Wyoming server, config loading, queue ✅
2. s2.cpp HTTP client with mocked tests ✅
3. Opt-in non-streaming s2.cpp backend mode ✅
4. Container startup flow ✅
5. Unraid WebUI docs ✅
6. CUDA/s2.cpp build and GPU runtime plan ✅
7. Phase 5A: multipart/form-data client compatibility ✅
8. Phase 5A.1/5A.2: verify and correct multipart request shape ✅
9. Phase 5B: streaming async iterator over s2.cpp response bytes ✅
10. Phase 5C: streamed audio to Wyoming events helper ✅
11. Phase 5D: TTS-side metrics and structured tracing ✅
12. Phase 5.5A: smoke-test harness ✅
13. Phase 5.5B: real backend smoke verification ✅
14. Phase 6A: CUDA s2.cpp backend Docker image built, published, deployed ✅
15. Phase 6B0: CPU-only wrapper Docker image, GHCR workflow, Unraid template ✅
16. Phase 6B1: Wyoming protocol verification — multipart fix, dynamic Describe ✅
17. Phase 6C: streaming TTS state machine — HA preview hang fix ✅
18. Phase 6D: Home Assistant deployment verified — real speech playback ✅
19. Phase 6E: deployment safety documentation and immutable Unraid template correction ✅

## Current verified deployment

- Backend: `s2cpp-backend` (`ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b`)
- Wrapper: `wyoming-s2cpp-tts` (`ghcr.io/sorilo/wyoming-s2cpp-tts:sha-89ed2dc`)
- Network: `sorilonet`
- HA: `192.168.1.233` → `192.168.1.45:10200`
- Audio: 44100 Hz mono s16le real speech via Wyoming protocol streaming lifecycle
- Tests baseline: 287/287 pass before Phase 6E
- Runtime caveat: true progressive backend HTTP audio streaming is not wired into the production event handler yet; the live handler still uses buffered `generate_multipart()`.

## Approved remaining v0.1 phases

20. Phase 7A: one-time custom `.s2voice` profile creation and direct backend verification
21. Phase 7B: wrapper voice discovery, voice selection, default voice configuration, Wyoming Describe exposure, and Home Assistant selection
22. Phase 7.5: wire true progressive backend HTTP audio streaming into the production Wyoming event handler when `S2_STREAM=true`
23. Phase 8: client disconnect cleanup, open HTTP stream closure, cancellation behavior, and documented backend cancellation limitations
24. Phase 9: queue capacity, busy handling, backend HTTP 503 handling, queue wait timeout, synthesis timeout, and controlled Wyoming failure behavior
25. Phase 10: end-to-end barge-in testing with an actual Home Assistant satellite/player, VAD, wake word, playback interruption, and new-request behavior
26. Phase 11: Faster-Whisper/full Assist pipeline integration and correlated latency measurement
27. Phase 12: comprehensive reliability tests and troubleshooting docs
28. Phase 13: v0.1 release checklist, tagging, and rollback criteria
29. Phase 14: final Unraid templates, persistence, restart, update, and backup testing

## Post-v0.1

- Multiple model profiles, higher-quality quantizations
- Multi-worker / multi-GPU scheduling
- Hardware upgrade benchmarking
- Broader monitoring and dashboard integration

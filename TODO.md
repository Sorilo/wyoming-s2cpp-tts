# TODO

## Completed through Phase 6D (2026-07-08)

1. Scaffold, minimal Wyoming server, config loading, queue ✅
2. s2.cpp HTTP client with mocked tests ✅
3. Opt-in non-streaming s2.cpp backend mode ✅
4. Container startup flow ✅
5. Unraid WebUI docs ✅
6. CUDA/s2.cpp build and GPU runtime plan ✅
7. Phase 5A: multipart/form-data client compatibility ✅
8. Phase 5A.1/5A.2: verify and correct multipart request shape ✅
9. Phase 5B: streaming async iterator over s2.cpp response bytes ✅
10. Phase 5C: streamed audio to Wyoming events ✅
11. Phase 5D: TTS-side metrics and structured tracing ✅
12. Phase 5.5A: smoke-test harness ✅
13. Phase 5.5B: real backend smoke verification ✅
14. Phase 6A: CUDA s2.cpp backend Docker image built, published, deployed ✅
15. Phase 6B0: CPU-only wrapper Docker image, GHCR workflow, Unraid template ✅
16. Phase 6B1: Wyoming protocol verification — multipart fix, dynamic Describe ✅
17. Phase 6C: streaming TTS state machine — HA preview hang fix ✅
18. Phase 6D: Home Assistant deployment verified — real speech playback ✅

## Current verified deployment

- Backend: s2cpp-backend (ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b)
- Wrapper: wyoming-s2cpp-tts (ghcr.io/sorilo/wyoming-s2cpp-tts:sha-89ed2dc)
- Network: sorilonet
- HA: 192.168.1.233 → 192.168.1.45:10200
- Audio: 44100Hz mono s16le real speech via Wyoming streaming protocol
- Tests: 287/287 pass

## Approved remaining v0.1 phases

19. Phase 7: custom voice profile creation, persistence, selection, HA exposure
20. Phase 8: disconnect cleanup and backend cancellation limitations
21. Phase 9: bounded queue, busy handling, timeout policy
22. Phase 10: barge-in testing with HA satellite/player path
23. Phase 11: Faster-Whisper/full Assist pipeline integration and latency
24. Phase 12: comprehensive tests and troubleshooting docs
25. Phase 13: v0.1 release checklist and tagging criteria
26. Phase 14: Unraid template finalization, restart/persistence testing

## Post-v0.1

- Multiple model profiles, higher-quality quantizations
- Multi-worker / multi-GPU scheduling
- Hardware upgrade benchmarking
- Broader monitoring and dashboard integration

# TODO

## Completed through Phase 4

1. Create scaffold and initial commit. ✅
2. Implement minimal Wyoming server with fake/test PCM. ✅
3. Add basic config loading from environment variables. ✅
4. Add single-worker bounded queue. ✅
5. Add direct s2.cpp HTTP client with mocked tests. ✅
6. Prove one optional external s2.cpp request path before packaging. ✅ skip-safe direct smoke path added
7. Add opt-in non-streaming s2.cpp backend mode. ✅
8. Add container startup flow for Python wrapper plus future s2.cpp process hook. ✅ Python wrapper container flow + future s2.cpp hook
9. Document Unraid WebUI Add Container settings. ✅ draft updated through Phase 4
10. Add CUDA/s2.cpp build and Unraid GPU runtime plan. ✅ docs/static plan only; no build claimed

## Approved v0.1 roadmap TODOs

11. Phase 5A: implement multipart/form-data s2.cpp client compatibility with mocked tests. ✅
11a. Phase 5A.1: verify and correct the exact s2.cpp multipart request shape against upstream reference client; use canonical `text` + `params` (JSON string) + `prompt_text`/`prompt_audio` fields. ✅
12. Phase 5B: implement a streaming async iterator over s2.cpp response bytes with mocked chunked responses. ✅
13. Phase 5C: pipe streamed audio into Wyoming `AudioStart`/`AudioChunk`/`AudioStop` events with mocked streaming tests. ✅
14. Phase 5D: add TTS-side metrics and structured tracing for request start, first backend byte, first Wyoming audio chunk, emitted bytes/chunks, request duration, and trace/request identifiers where practical.
15. Phase 5.5: run an opt-in real external s2.cpp smoke test outside the final Docker image when backend/model/tokenizer prerequisites are actually available.
16. Phase 6A: handle Wyoming client disconnects and backend cancellation where supported.
17. Phase 6B: implement queue cancellation, backend busy handling, timeout policy, and policy for a new request arriving during active speech.
18. Phase 6C: test barge-in-friendly behavior using Home Assistant when available or simulated disconnect/cancellation tests otherwise.
19. Phase 7A: add comprehensive protocol, queue, error, cancellation, integration tests, and troubleshooting documentation for ports, GPU visibility, models, voices, audio format, backend reachability, queue/busy behavior, and Wyoming/Home Assistant connection issues.
20. Phase 7B: create the v0.1 release checklist and tagging criteria; do not tag v0.1 unless required behavior has actually been verified.
21. Phase 8A: build and test the CUDA-enabled s2.cpp Docker image.
22. Phase 8B: finalize the Unraid WebUI template/documentation and validate NVIDIA GPU passthrough, ports, mounts, permissions, startup, process supervision, health checks, shutdown, restart behavior, and persistence.
23. Phase 8C: run the final Home Assistant end-to-end test with real Assist pipeline operation, streamed playback, audio correctness, cancellation/barge-in behavior where supported, and latency measurements where measurable.

## Post-v0.1 future work

- Multiple model profiles.
- Multiple voice/reference profiles.
- Higher-quality quantizations.
- Multi-worker scheduling.
- Multi-GPU routing.
- Advanced local-versus-cloud LLM routing.
- Hardware-upgrade benchmarking, including possible RTX 5080 16 GB evaluation.
- Broader monitoring and dashboard integration.

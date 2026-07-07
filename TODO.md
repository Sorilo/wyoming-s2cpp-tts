# TODO

## Must have for v0.1

1. Create scaffold and initial commit. ✅
2. Implement minimal Wyoming server with fake/test PCM. ✅
3. Add basic config loading from environment variables. ✅
4. Add single-worker bounded queue. ✅
5. Add direct s2.cpp HTTP client with mocked tests. ✅
6. Prove one external s2.cpp request path before packaging. ✅ optional direct smoke path added
7. Add opt-in non-streaming s2.cpp backend mode. ✅
8. Add container startup flow for Python wrapper plus s2.cpp process. ✅ Python wrapper container flow + future s2.cpp hook
9. Document Unraid WebUI Add Container settings. ✅ draft updated for Phase 3
10. Verify Home Assistant can connect to the Wyoming endpoint.
11. Add troubleshooting for ports, GPU visibility, models, voices, and audio format.

## Future

- CUDA-capable s2.cpp image build.
- Fish Speech S2 Pro GGUF model profiles.
- `s2-pro-q8_0.gguf` quality profile.
- `s2-pro-q4_k_m.gguf` low-VRAM fallback profile.
- True progressive streaming from backend to Wyoming.
- Cancellation on client disconnect.
- Optional cancellation of old synthesis when a new request arrives.
- Latency benchmarking and realtime-factor logging.
- Multiple voices/reference IDs.
- Multi-GPU or multi-worker scheduling after the RTX 3080 baseline is stable.

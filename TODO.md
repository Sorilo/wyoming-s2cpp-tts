# TODO

## Completed through Phase 7A (2026-07-08)

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
20. Phase 7A: CMU ARCTIC voice profile creation — 6 profiles, 6/6 direct synthesis ✅

## Phase 7A results

- Six one-time `.s2voice` profiles created from CMU ARCTIC reference recordings:

  | Profile ID | Gender | Accent | Size |
  |---|---|---|---|
  | `cmu_bdl_male_us` | male | US English | ~5.0 KB |
  | `cmu_rms_male_us` | male | US English | ~5.9 KB |
  | `cmu_jmk_male_canadian` | male | Canadian English | ~5.1 KB |
  | `cmu_slt_female_us` | female | US English | ~4.7 KB |
  | `cmu_clb_female_us` | female | US English | ~5.5 KB |
  | `cmu_eey_female_us` | female | US English | ~5.2 KB |

- Persistent profile directory: `/mnt/user/appdata/s2cpp/voices`
- All six profiles visible via `s2 --list-voices` with GPU-backed execution (libcuda.so.1 linked even for listing)
- Direct backend multipart synthesis: **6/6 passed** (all profiles produce valid RIFF/WAVE audio)
- Human listening: acceptable as temporary assistant voices; sound somewhat robotic; no downstream defect confirmed; personal clean recording expected to be a better long-term quality test
- Operational caveats: FestVox HTTPS endpoint unreachable from Unraid host (HTTP fallback used); `--list-voices` requires GPU runtime due to CUDA library linkage
- Comparison WAVs saved: `/mnt/user/appdata/s2cpp/verification_artifacts/phase_7a/`

## Current verified deployment

- Backend: `s2cpp-backend` (`ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b`)
- Wrapper: `wyoming-s2cpp-tts` (`ghcr.io/sorilo/wyoming-s2cpp-tts:sha-974e220`)
- Network: `sorilonet`
- HA: `192.168.1.233` → `192.168.1.45:10200`
- Audio: 44100 Hz mono s16le real speech via Wyoming protocol streaming lifecycle
- Tests baseline: 287/287 pass before Phase 7A
- Runtime caveat: true progressive backend HTTP audio streaming is not wired into the production event handler yet; the live handler still uses buffered `generate_multipart()`.

## Phase 7B results

- Wrapper voice discovery from `/voices` directory.
- Safe `.s2voice` filename sanitisation (path traversal, hidden files, and symlinks rejected).
- Six CMU ARCTIC profiles plus future drop-in profiles discoverable without rebuild/restart.
- `S2_VOICE_DIR` and `S2_DEFAULT_VOICE` environment variables supported.
- Wyoming Describe advertises all discovered voices plus the generic `s2-pro` fallback.
- Synthesis: selected/default voice propagated as `voice` and `voice_dir` multipart fields.
- Generic `s2-pro` fallback omits custom voice fields.
- Unknown/unsafe voice IDs rejected with clear errors.
- Both buffered and streaming Wyoming paths propagate voice consistently.
- Home Assistant may require a Wyoming integration reload to see newly dropped-in voices.
- 38 new tests (20 discovery + 18 voice selection/Describe/synthesis).
- Full suite: 323 passing (2 pre-existing stale doc test failures unchanged).
- Wrapper image published: see CHANGELOG for tags.

## Phase 7.5A results

- Wired S2_STREAM routing: when ``S2_STREAM=true`` and ``TTS_BACKEND=s2cpp``, the
  production handler uses ``synthesize_s2cpp_streaming_tts_events()`` /
  ``generate_stream()`` to yield Wyoming audio events progressively.
- When ``S2_STREAM=false``, the existing buffered ``generate_multipart()`` path is
  preserved unchanged.
- Fake backend behavior remains unchanged.
- Wyoming text-streaming state machine preserved (start→chunk→stop→AudioStart→AudioChunk→AudioStop→synthesize-stopped).
- Compatibility synthesize event deferral (Phase 7B.3 fix) preserved.
- Backend response validation before ``AudioStart``: ``Content-Type``,
  ``X-Audio-*`` metadata, sample rate, channels, frame alignment, empty output,
  incomplete frames.
- Resource safety: stream closed on normal completion, validation failure,
  generator exception, mid-stream error.
- Voice propagation preserved through streaming path.
- Structured observability extended: ``backend_stream_headers``,
  ``backend_stream_first_audio``, ``first_wyoming_audio``,
  ``backend_stream_done`` with timing fields.
- 13 new streaming-specific tests. Full suite: 367/368 passing.
- Backend image, voices, live containers, and Home Assistant untouched.


## Phase 7.5B results

- Live deployment verified: one-request/one-audio lifecycle confirmed through progressive streaming path.
- Progressive window measured at ~5 ms — backend generation dominates latency (2,932 ms to first audio).
- Two metric-only double-counting bugs found and fixed in ``backend_stream_done`` and ``audio_out`` observability lines.
  Flush-carry chunk bytes and chunk counts were double-counted. No actual audio bytes were affected.
- ``first_wyoming_audio`` enhanced with ``elapsed_ms``, ``time_to_first_backend_audio_ms``, ``wrapper_first_audio_forwarding_overhead_ms``.
- ``backend_stream_done.total_elapsed_ms`` → ``total_backend_stream_ms``.
- ``syn_stopped`` now includes ``total_synthesis_ms``.
- 5 new deterministic PCM byte-counting tests. Full suite: 374/374 passing.
- Backend image, voices, live containers, and Home Assistant untouched.
- Wrapper image to be published with corrected observability.

## Approved remaining v0.1 phases

21. ~~Phase 7.5: wire true progressive backend HTTP audio streaming into the production Wyoming event handler when `S2_STREAM=true`~~ ✅ Phase 7.5A complete
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

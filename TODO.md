# TODO

## Completed through Phase 9 (2026-07-11)

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

- Backend: `s2cpp-backend` (pin the approved paired `sha-<commit>` image)
- Wrapper: `wyoming-s2cpp-tts` (pin the same paired `sha-<commit>` image)
- Network: `s2cpp-net` (or an operator-selected `NETWORK_NAME`)
- Home Assistant: `<ha-host>` → `<docker-host>:10200`
- Audio: 44100 Hz mono s16le real speech via Wyoming protocol streaming lifecycle
- Phase 9 historical test baseline: 876 passed, 0 failed, 0 skipped
- Phase 9B standard-suite baseline: 940 collected, 940 passed, 0 failed, 0 skipped; 14 Unraid shell-behavior tests remain a separate environment-specific invocation
- Phase 9 production deployment and final smoke passed: short/long direct Wyoming, audible Home Assistant VM speech, zero restarts, queue depth zero, active GPU inference, and clean logs. Phase 9 is closed.

## Phase 11 operations/docs status (2026-07-13)

- **Generic Compose + .env.example**: ✅ compose.yaml + .env.example created for v0.1.0.
- **Operations docs**: ✅ `docs/SECURITY.md`, `docs/UPGRADE_ROLLBACK.md`, `docs/RELEASE.md` created.
- **Install docs**: ✅ `docs/UNRAID_INSTALL.md` and `docs/HOME_ASSISTANT_SETUP.md` updated with sanitized placeholders, v0.1.0 guidance, backup/rollback links, host-unpublished backend port, and stock HA 2026.7.2 + Voice PE 26.6.0 one-wake NOT PASS limitation.
- **Contract tests**: ✅ 37 documentation/security contract tests pass.
- **Remaining Phase 11 work**: Faster-Whisper/full Assist pipeline integration and latency measurement.
- **Remaining v0.1 phases**: Phase 12 (reliability), Phase 13 (release checklist/tagging), Phase 14 (Unraid template finalization).

## Phase 10 results

- Final status: **Phase 10 implementation validation complete with documented external stock-platform limitation.**
- Correlated direct-disconnect: **25/25 passed**; wrapper cancellation, native backend abort, scheduler cleanup, and follow-up recovery proved.
- Overlap-recovery: **8/8 passed** with no persistent queue or busy latch.
- Generic HA media-stop: **7/9**; stock HA 2026.7.2 / Voice PE 26.6.0 / ESPHome 2026.6.0 stops the normal media pipeline rather than the active announcement and keeps the TTS producer alive.
- Stock one-wake barge-in is **not passed**; remaining acceptance moves to an announcement-aware upstream integration or Cortex-Satellite.
- Full suite: **1512 passed**, excluding only `tests/test_realtime_tuning_unraid.py`.
- Closure: `docs/validation/PHASE_10_CLOSURE.md`.
- Production remains wrapper/backend `sha-75936bc`.

## Phase 9C results

- Graceful shutdown lifecycle owner with explicit state machine: ``STARTING`` → ``RUNNING`` → ``DRAINING`` → ``STOPPING`` → ``STOPPED`` / ``FAILED``.
- SIGTERM/SIGINT initiate shutdown exactly once; repeated signals are idempotent.
- Shutdown bounded by ``SHUTDOWN_GRACE_TIMEOUT_SEC`` (default 30, validated (0, 300]).
- Scheduler drain: cancels queued work, allows active synthesis a grace period, force-cancels after expiry.
- Readiness false immediately on drain; no new Wyoming connections or synthesis admissions accepted.
- Optional admin HTTP server (disabled by default, loopback ``127.0.0.1:10201``):
  * ``GET /livez`` — liveness (200 while alive)
  * ``GET /readyz`` — traffic readiness (200 only RUNNING, 503 otherwise)
  * ``GET /status`` — sanitized JSON snapshot (state, readiness, uptime, scheduler, counters)
  * ``GET /metrics`` — sanitized JSON metrics with cumulative counters
- No plaintext, audio, secrets, tokens, IDs, or mutable objects in admin responses.
- All admin HTTP parsing uses bounded time/size limits; bind failure non-fatal.
- No mutating admin endpoints.
- ``CumulativeCounters``: thread-safe monotonic process-lifetime counters wired through scheduler and admin.
- Service coordinator owns lifecycle, Wyoming listener start/stop, connection tracking, signal dispatch, admin lifecycle.
- 183 new Phase 9C tests (lifecycle, coordinator, shutdown behavior, admin HTTP, counters, status/metrics snapshots).
- Full standard suite: **1112 passed, 0 failed, 0 skipped** (excluding the 14 environment-specific tests in `tests/test_realtime_tuning_unraid.py`, including its fake-`nvidia-smi` cases).
- Source-only implementation — no image published or deployed. Production remains on Phase 9 images (wrapper ``sha-7db26b7``, backend ``sha-6e629d0``).

## Phase 9.5 results

- Progressive phrase synthesis implemented: ``PhraseAccumulator`` for bounded
  deterministic phrase parsing, ``AudioEnvelope`` for logical Wyoming audio
  normalisation, and ``StreamingCoordinator`` for connection-owned progressive
  synthesis pipeline.
- Text streaming chunks are parsed at deterministic terminal-punctuation
  boundaries (``.!?。！？``) with decimal, abbreviation, and ellipsis
  protection plus bounded fallback (default 160/320/640 character limits).
- ``AudioEnvelope`` emits exactly one ``AudioStart`` per logical Wyoming
  response, suppresses internal phrase ``AudioStop`` events, rebuilds chunk
  timestamps from cumulative emitted PCM frames, and closes with one terminal
  event (``AudioStop`` on success; ``AudioStop`` then Error on failure).
- ``StreamingCoordinator`` runs a background synthesis task that submits
  completed phrases through ``SpeechScheduler`` one at a time — no backend
  calls overlap.  Output events arrive through a bounded capacity‑1 queue
  with backpressure.
- Cancellation clears pending phrases, cancels the active scheduler
  connection, and unblocks waiting consumers.
- Timeout and deadline budgets apply per‑phrase (inherited from
  ``SpeechScheduler``).  Counters count individual phrase operations.
- Legacy ``Synthesize`` path unchanged outside a streaming session.
  Compatibility deduplication: streaming authoritative once any non‑whitespace
  chunk arrives.
- Backend‑busy, queue‑full, queue‑timeout, and synthesis‑failure handling
  preserved from Phase 9/9B/9C.
- Focused/adjacent integration gate: **218 passed**. Coordinator/cancellation
  coverage gate: **33 passed**.
- Full suite: **1250 passed, 0 failed, 0 skipped** (excluding 14
  environment‑specific tests in ``tests/test_realtime_tuning_unraid.py``).
- Source‑only implementation — no image published or deployed. Production
  remains on Phase 9 images (wrapper ``sha-7db26b7``, backend ``sha-6e629d0``).
- Draft branch: ``phase/phase-9-5-progressive-phrase-synthesis``; not merged, released, or deployed.


## Phase 9B results

- Source-only domain refactor extracting `SpeechRequest`, `SpeechMetadata`, `SpeechScheduler`, `SpeechState`, `ScheduledSpeech`, and `SynthesisSession` into explicit `app/speech/` domain objects.
- `SpeechScheduler` exclusively owns admission, FIFO activation, queue state, cancellation, and release. Handlers create `SpeechRequest` objects and submit operations; they do not read or mutate scheduler-private fields.
- `SingleWorkerSynthesisQueue` compatibility wrapper removed; all code uses `SpeechScheduler` directly.
- Lifecycle state machine: `CREATED → WAITING → ACTIVE → COMPLETED/CANCELLED/TIMED_OUT/FAILED` with idempotent terminal transitions and `TIMED_OUT` vs `FAILED` distinction.
- Reserved semantic metadata fields present but inert (do not affect FIFO order).
- Plaintext excluded from reprs, snapshots, structured logs, and lifecycle observability.
- Observable behavior (FIFO, capacity, single-worker, busy retries, deadlines, cancellation, event ordering) unchanged from Phase 9.
- No image published or deployed. Production remains on Phase 9 images (wrapper `sha-7db26b7`, backend `sha-6e629d0`).


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


## Phase 7.5D2 results

- Wrapper now sends ``segment_sentences=false`` + ``low_latency=true`` + optionally ``codec_decode_context_frames=4``.
- First backend PCM arrives at approximately 150 ms (was gated behind sentence completion).
- Added ``S2_SEGMENT_SENTENCES`` and ``S2_CODEC_CONTEXT_FRAMES`` env vars.
- Context validation: only 4, 64, 160 accepted; others rejected with clear error.
- 23 new tests. Full suite: 397/397 passing.
- No backend image change required.
- Wrapper image: ghcr.io/sorilo/wyoming-s2cpp-tts:sha-4c23aa8

## Phase 8A results

- Client disconnect and task cancellation now promptly close the backend HTTP stream.
- ``S2StreamResult.cancel()`` unblocks blocked ``read()`` calls in worker threads.
- Write failures (``BrokenPipeError``, ``ConnectionResetError``, ``TypeError`` from closed transports) are caught and trigger graceful cleanup.
- 4 new tests. Full suite: 401/401 passing.
- Diagnostic test: backend GPU inference continues after HTTP close (no s2.cpp cancellation API).
- Wrapper image to be published.


## Phase 8B0 results

- Backend ALREADY has cooperative cancellation mechanism: StreamContext::cancelled checked at frame boundaries.
- Disconnect detected by content provider (is_writable/sink.write failure) → cancelled=true.
- Synthesizer's on_frame checks is_cancelled() → aborts generate() within ~45ms.
- Skip final batch decode when generation was aborted (avoids expensive codec work).
- Added observability events: backend_cancel_detected, generation_cancel_observed, backend_request_cancelled.
- Diagnostic backend image: ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-29a5a2c
- Expected disconnect-to-stop latency: ~145ms (100ms cv wait + 45ms frame).
- Wrapper tests: 407/407 passing. Backend unchanged except instrumentation patch.


## Phase 8B1 tooling status

- Corrected the live verification harness classification for the exact recovery
  request type used by the harness: standalone legacy `Synthesize` terminates at
  `AudioStop` and does not require `synthesize-stopped`.
- Fixed `capture_phase_8b1_logs.sh` for unattended `--duration` capture plus
  post-run bounded wrapper/backend log snapshots.
- Added `scripts/live_compare_long_form_contexts.py` plus a runbook for long-form
  context 4 vs 64 vs auto/160 comparison, but long-form audio-quality work has
  not begun.
- Phase 8B1.1 retry artifacts captured complete backend cancellation logs and
  successful corrected recovery evidence. Phase 8B1/8B2 is complete.



## Phase 8B1/8B2 results

- Final live retry artifacts: `verification_artifacts/phase_8b1_1_retry/`.
- Tested diagnostic backend `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-b8e54f9` passed 5/5 cancellation/recovery cycles.
- Backend cancellation events appeared exactly once per cancelled request and in order: `backend_cancel_detected`, `generation_cancel_observed`, `final_decode_skipped`, `backend_request_cancelled`, `backend_request_cleanup_done`.
- Cancellation fields were consistent: `reason=client_disconnect`, `point=content_provider_complete`, valid 41-45 ms monotonic timings, accurate generated/decode/PCM counters, `queued_pcm_bytes=0`, `server_busy=false`.
- Immediate recovery passed 5/5 for audio, protocol terminal event, and valid non-empty PCM; no server-busy response, timeout, exception, crash, deadlock, restart, or GPU accumulation.
- Production backend image published: `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd` (`sha256:c29e41e59b470d58bf4b88c11c9ec753e00fa74a3bffbb003bc257fb9c6e46d9`).
- Rollback backend remains `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b`.
- Wrapper remains `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-9c134cc`; BrokenPipe task-exception noise on deliberate disconnect is a separate logging issue and did not block cleanup.


## Phase 8D results: controlled quantized-model performance and quality benchmark

- Fixed benchmark-tool issues: false "No live RTX 3080" claim removed, metric correlation hardened with bounded polling, WAV conversion guidance updated for Hermes-Suite ffmpeg path.
- Benchmark harness now records model SHA-256 and file size per run.
- Added ``--model`` CLI argument for explicit model path recording.
- Benchmark scripts ready for controlled Q6_K/Q5_K_M/Q4_K_M comparison at fixed stride 4.

**Status**: Phase 8D complete. Phase 8E.1 complete. See docs/PERFORMANCE_TUNING_RESULTS.md. Single-container-per-model orchestrator (`scripts/run_quantization_benchmark_unraid.sh`). Live Q5_K_M/Q4_K_M quant benchmark and human listening still pending.
- Full suite: 590/590 passing (25 Phase 8D + 16 Phase 8D.2 tests included).

## Phase 8C results: realtime stride tuning infrastructure

- Four new wrapper env vars with strict validation: S2_STREAM_DECODE_STRIDE_FRAMES (1-64),
  S2_STREAM_HOLDBACK_FRAMES (non-negative), S2_STREAM_START_BUFFER_MS (non-negative),
  S2_LOW_LATENCY (bool).
- Environment audit: S2_MAX_NEW_TOKENS, S2_TEMPERATURE, S2_TOP_P, S2_TOP_K,
  S2_CHUNKED, S2_OUTPUT_FORMAT, S2_MODEL, S2_GPU_INDEX, S2_GPU_LAYERS,
  S2_CODEC_CPU, BARGE_IN_FRIENDLY, CANCEL_ON_CLIENT_DISCONNECT,
  CANCEL_ON_NEW_REQUEST, MAX_QUEUE_SIZE now parseable with strict validation.
- S2GenerateRequest.to_multipart_fields(streaming=True) now explicitly sends:
  low_latency, stream_decode_stride_frames, stream_holdback_frames,
  stream_start_buffer_ms alongside existing params.
- Backend_start observability extended with all tuning parameters.
- scripts/benchmark_realtime_tuning.py: dry-run-safe Python stride-sweep harness
  (--run-real to contact backend; measures RTF, first-PCM, total synthesis).
- scripts/run_realtime_tuning_unraid.sh: one-command Unraid host orchestration.
- Unraid wrapper template updated with Phase 8C config vars.
- 80 new tests. Full suite: 540/540 passing.
- No backend image change. Live RTX 3080 benchmarks completed (strides 1-24).
- Stride 4 is a candidate only; real benchmarking pending on Unraid host.

## Phase 8E.1 results: Q4_K_M non-fork runtime tuning

- Thread sweep: threads=8 is optimal on i9-13900K (RTF 0.954 at context 4)
- Context screen: context 32 is quality floor (first without tapping/blipping)
- Context-32 stride sweep: stride 32 only sub-1.0 configuration (RTF 0.987)
- Provisional baseline: Q4_K_M, threads=8, context=32, stride=32, P-cores 0-15
- Wrapper image published: ghcr.io/sorilo/wyoming-s2cpp-tts:sha-22db725
- Deployment handoff: docs/PHASE_8E1_DEPLOYMENT_HANDOFF.md
- Tuning paused for end-to-end HA validation

## Approved remaining v0.1 phases

Phase 9C: graceful shutdown and optional admin HTTP visibility ✅
22. ~~Phase 7.5: wire true progressive backend HTTP audio streaming into the production Wyoming event handler when `S2_STREAM=true`~~ ✅ Phase 7.5A complete
23. Phase 8: client disconnect cleanup, open HTTP stream closure, cancellation behavior, and documented backend cancellation limitations
24. Phase 9: queue capacity, busy handling, backend HTTP 503 handling, queue wait timeout, synthesis timeout, and controlled Wyoming failure behavior
25. ~~Phase 10: end-to-end barge-in implementation validation~~ ✅ Repository-owned disconnect cancellation/recovery passed; stock Voice PE one-wake behavior is a documented external limitation carried to Cortex-Satellite
26. Phase 11: Faster-Whisper/full Assist pipeline integration and correlated latency measurement
27. Phase 12: comprehensive reliability tests and troubleshooting docs
28. Phase 13: v0.1 release checklist, tagging, and rollback criteria
29. Phase 14: final Unraid templates, persistence, restart, update, and backup testing

## Post-v0.1

- Multiple model profiles, higher-quality quantizations
- Multi-worker / multi-GPU scheduling
- Hardware upgrade benchmarking
- Broader monitoring and dashboard integration

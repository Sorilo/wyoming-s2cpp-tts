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
- `S2_STREAM=true` uses the production progressive HTTP streaming path; the
  buffered `generate_multipart()` path remains available when disabled.

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

### Phase 7.5B: live verification + streaming-metrics audit \u2705
Confirmed progressive streaming in production. Fixed metric-only double-counting bugs.
Enhanced observability with unified timing fields. 374/374 tests pass.

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


### Phase 8B1/8B2: backend cancellation verification and production promotion ✅
Phase 8B1.1 final retry artifacts under `verification_artifacts/phase_8b1_1_retry/`
proved 5/5 deliberate disconnect/recovery cycles.  Each cancelled backend request
recorded `backend_cancel_detected`, `generation_cancel_observed`,
`final_decode_skipped`, `backend_request_cancelled`, and
`backend_request_cleanup_done` exactly once, in order, with
`reason=client_disconnect`, `point=content_provider_complete`, valid monotonic
timings, accurate frame/decode/PCM counters, `queued_pcm_bytes=0`, and
`server_busy=false`.  All five immediate recovery syntheses passed audio,
protocol terminal, and PCM validity checks.  GPU utilization returned to idle,
containers stayed running, and no restart was required.  Wrapper BrokenPipe
task-exception noise remains a separate narrow logging issue and did not block
backend cleanup or recovery.

Production backend image: `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd` (`sha256:c29e41e59b470d58bf4b88c11c9ec753e00fa74a3bffbb003bc257fb9c6e46d9`).
Rollback backend image: `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b`.
Wrapper unchanged: `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-9c134cc`.

### Phase 8C: realtime stride tuning infrastructure ✅
Implemented configurable streaming decode stride, holdback, start-buffer,
and low-latency settings with strict validation, explicit multipart
request parameters, enhanced observability, an opt-in Python benchmark
harness, and a one-command Unraid orchestration script. 80 new tests,
540/540 passing. No backend image change; live RTX 3080 benchmarks completed (strides 1-24, see verification_artifacts/realtime_tuning/).
Stride 4 is the current preferred Q6_K latency/throughput compromise (RTF 1.13, first PCM ~251 ms). See ``CHANGELOG.md`` and
``scripts/benchmark_realtime_tuning.py`` for the benchmark workflow.

### Phase 8D: controlled quantized-model performance and quality benchmark ✅

Select a single preferred runtime quantization by benchmarking candidate Q6_K, Q5_K_M, and Q4_K_M GGUF models against the RTX 3080 at fixed stride 4, holding all other variables constant.  The phase delivers: (a) hardened benchmark tooling with reliable metric correlation, port discovery, and WAV conversion; (b) a controlled quant comparison under identical conditions (same GPU, backend build, container environment, voice, and text); (c) human listening evaluation of audio quality across quants; and (d) a single recommended runtime model.

**Status**: Complete. First live quant inference benchmark + human listening finished.
Q4_K_M selected as performance candidate (RTF 1.015). Q5_K_M retained as quality fallback.
Phase 8E.1 (Q4 runtime tuning) in progress.

This phase does NOT implement dynamic model switching, multi-worker routing, or multi-GPU scheduling.  Those remain in post-v0.1.  A conditional Phase 8E placeholder exists for non-fork runtime tuning if no quant achieves safe real-time performance.

Candidate models: s2-pro-q6_k.gguf (baseline), s2-pro-q5_k_m.gguf, s2-pro-q4_k_m.gguf.  Q8_0 is an optional quality ceiling if storage permits.  Models are acquired from the verified upstream S2 Pro GGUF source with SHA-256 verification and resumable downloads.

See ``docs/STREAMING_STRIDE_AND_QUANT_BENCHMARKS.md`` for the comprehensive stride and quantization benchmark documentation.

### Phase 8E: conditional non-fork runtime performance tuning (placeholder)

Only triggered if no acceptable quant reaches safe real time.  Investigate generation-profile-specific decode-stride tuning, codec-context variants, and AR-batch sizing adjustments without forking s2.cpp.

### Phase 8E.1: Q4_K_M non-fork runtime tuning ✅

Find the best non-fork runtime configuration for Q4_K_M at fixed stride 4.
Thread-count sweep (0,8,16,24,32), CPU-affinity sweep (P-core physical/logical,
P+E), and blipping diagnostic (codec context 4 vs 64, holdback 0 vs 1).
GPU telemetry, stock-clock verification, and saved-voice verification included.

**Status**: Complete. Q4_K_M selected, threads=8, context=32, stride=32 baseline frozen. See `docs/PERFORMANCE_TUNING_RESULTS.md`.

### Phase 8E.2: build-level and stride tuning ⏸️ (paused)

**Status**: Paused. Tuning deferred pending end-to-end HA measurements. Conditional future phase: CUDA kernel selection (MMQ vs CUBLAS), GGML_NATIVE+LTO
build comparison, mild GPU overclock testing, final stride 5/6/8 comparison.
Requires separate goal and controlled backend-image builds.

### Phase 9: queue, busy handling, and timeout policy ✅
Implemented deterministic bounded FIFO admission, backend HTTP 503 retry, queue-wait and synthesis deadlines, controlled Wyoming failure behavior, and disconnect recovery. PR #2 merged as `1a0b93f`; 876 tests and isolated Unraid validation passed. Validated images are deployed. Short and long direct Wyoming smoke and audible Home Assistant VM smoke passed with zero restarts, queue depth zero, active GPU inference, and clean logs. Phase 9 is closed.

### Phase 9.5: progressive LLM text-to-TTS phrase pipeline ✅

Implemented source-only progressive phrase synthesis for Wyoming
synthesize-start/chunk/stop input. Complete phrases enter the existing FIFO
scheduler before synthesize-stop, while one logical AudioStart/AudioStop
envelope preserves continuous timestamps across backend phrase operations.
Compatibility authority, bounded buffering, cancellation, timeout, drain,
and cleanup behavior are covered by deterministic tests. No image was built
or deployed; production remains on the recorded Phase 9 image pins.

### Phase 10: end-to-end barge-in testing with HA satellite/player — implementation validation complete
Repository-owned disconnect cancellation, native backend abort, scheduler cleanup,
follow-up recovery, and overlap recovery passed against the deployed `sha-75936bc`
images. Stock HA 2026.7.2 with Voice PE 26.6.0 / ESPHome 2026.6.0 has a
documented external limitation: generic `media_player.media_stop` targets the
normal media pipeline, not the active Assist announcement, and does not cancel
the HA TTS producer or close Wyoming. Stock one-wake barge-in is **not passed**;
it is deferred to an announcement-aware upstream lifecycle or Cortex-Satellite.
See `docs/validation/PHASE_10_CLOSURE.md`.

### Phase 11: Faster-Whisper/full Assist pipeline integration and latency measurement
Integrate or measure the broader Assist path and correlate STT, LLM, VAD, TTS, and playback timings.

### Phase 12: comprehensive reliability tests and troubleshooting
Add reliability coverage, operational troubleshooting, and failure-mode documentation.

### Phase 13: v0.1 release checklist, tagging, and rollback criteria
Define and execute the v0.1 release checklist, tags, image pins, rollback criteria, and known limitations.

### Phase 14: final Unraid templates, persistence, restart, update, and backup testing
Finalize templates after real restart/update/persistence/backup validation.

## Post-v0.1

- Dynamic model switching and simultaneously served quantization profiles
- Multi-worker / multi-GPU routing and scheduling
- Hardware upgrade benchmarking

## Governance

1. Work only on the phase named in the current `/goal`.
2. Preserve completed behavior and tests.
3. Never claim real behavior unless actually tested.
4. One focused commit per phase.
5. Update `ROADMAP.md`, `TODO.md`, `NEXT_GOAL_PROMPTS.md`, and `CHANGELOG.md` when status changes.


## Phase 9B: SpeechRequest Domain Model ✅
- SpeechRequest domain model, scheduler-owned lifecycle state machine
- SpeechScheduler behavior-preserving refactor, SynthesisSession foundation
- inactive reserved semantic metadata, scheduler admission latency metric
- Status: Complete. Source-only refactor; no image published/deployed. Production remains on Phase 9 images (wrapper sha-7db26b7, backend sha-6e629d0).

## Phase 9C: Graceful Shutdown & Admin ✅

- ``ServiceCoordinator`` lifecycle owner with explicit ``LifecycleState`` machine (``STARTING`` → ``RUNNING`` → ``DRAINING`` → ``STOPPING`` → ``STOPPED`` / ``FAILED``).
- SIGTERM/SIGINT initiate shutdown exactly once; idempotent repeated calls.
- Bounded by ``SHUTDOWN_GRACE_TIMEOUT_SEC`` (default 30, range (0, 300]).
- Scheduler drain: cancel queued, allow active grace period, force-cancel after expiry.
- Readiness false immediately on shutdown; no new admissions.
- Optional admin HTTP server (disabled, ``127.0.0.1:10201`` by default): ``GET /livez`` (200), ``GET /readyz`` (200/503), ``GET /status`` (sanitized JSON), ``GET /metrics`` (sanitized JSON counters).
- No plaintext, audio, secrets, tokens, or mutating endpoints.
- ``CumulativeCounters``: thread-safe monotonic counters (admitted, rejected, completed, cancelled-queued, cancelled-active, timed-out, failed, backend-busy-retries).
- Bind failure non-fatal. Source-only; no image published/deployed. Production remains on Phase 9 images.
- Full standard suite: **1112 passed, 0 failed, 0 skipped**.

## Phase 9.5: Progressive Phrase Synthesis — Complete

Implemented per-request bounded deterministic phrase accumulation, continuous Wyoming audio streaming, and progressive phrase-by-phrase synthesis through the SpeechScheduler.

**Architecture**:
- ``PhraseAccumulator`` (``app/speech/phrases.py``): Bounded streaming text parser with deterministic terminal-punctuation boundaries (`.!?。！？`), decimal protection, abbreviation awareness, ellipsis handling, and fallback splitting at configurable soft/phrase/retained limits (default 160/320/640 characters).
- ``AudioEnvelope`` (``app/speech/envelope.py``): Logical audio normalizer that emits AudioStart exactly once per Wyoming response, validates format consistency across phrases, suppresses internal AudioStop events, rebuilds AudioChunk timestamps from cumulative emitted PCM frames, and closes with exactly one terminal event (AudioStop on success, AudioStop then Error on failure).
- ``StreamingCoordinator`` (``app/speech/stream_coordinator.py``): Connection-owned coordinator that feeds text chunks to the accumulator, submits completed phrases through SpeechScheduler one at a time (FIFO fairness), and delivers output events via a bounded capacity-1 async queue (backpressure). Supports both progressive feeding and buffered legacy compatibility mode.

**Handler integration** (``app/wyoming_server.py``):
- ``SynthesizeStart`` creates a ``StreamingCoordinator`` and starts a background consumer task that writes audio events progressively.
- ``SynthesizeChunk`` feeds text to the accumulator immediately; tracks whether non-whitespace chunks arrived for correct compatibility-synthesize deduplication.
- ``SynthesizeStop`` feeds deferred compat text only when no streaming chunks arrived, flushes remaining text, and awaits the consumer task. SynthesizeStopped is emitted only if the client is still connected (prevents post-disconnect leak).
- Disconnect/cancellation cancels the coordinator, drains pending phrases, and closes the consumer task. Generator cleanup (aclose) is verified.

**Test baseline**: 1250 passed, 0 failed, 0 skipped (excluding 14 environment-specific Unraid tests). Full suite includes 218 focused tests across synthesis session, streaming protocol, compatibility, Wyoming streaming, coordinator/envelope, scheduler/drain, shutdown/lifecycle, and backend.

**Commits** (on branch ``phase/phase-9-5-progressive-phrase-synthesis``):
1. ``d12dc27`` — docs: add reviewed progressive synthesis plan
2. ``680a042`` — feat: add bounded deterministic phrase accumulator
3. ``3f3a24b`` — fix: preserve phrase closers and decimal boundaries
4. ``86b4ca0`` — feat: add logical audio-envelope normalizer with frame accounting
5. ``ce70297`` — feat: add explicit streaming coordinator with progressive phrase synthesis
6. ``8e7c160`` — fix: redesign StreamingCoordinator to be truly progressive
7. ``61b0e63`` — fix: avoid terminal audio for empty streams
8. ``e0b8dbc`` — feat: integrate progressive Wyoming synthesis
9. ``799952f`` — fix: harden progressive stream cleanup
10. ``feaec8c`` — fix: gate terminal success after disconnect
11. ``be4d1c0`` — fix: unblock coordinator consumers on cancellation
12. ``fa8ac46`` — docs: document progressive synthesis draft
13. ``51226af`` — docs: describe progressive synthesis architecture

**Known limitations**:
- Timeout and deadline budgets apply per-phrase (inherited from SpeechScheduler), not to the entire logical streaming request.
- Counters (admitted, completed, etc.) count individual phrase operations, not logical requests — this is intentional and documented.
- No backend image changes; wrapper-only phase. Production deployment deferred pending Phase 10 (barge-in) integration testing.
- Phrase-level progressive coordination activates for every Wyoming streaming session. ``S2_STREAM=false`` selects buffered per-phrase backend transport; it does not disable phrase-level coordination. The standalone legacy ``Synthesize`` path remains unchanged outside a streaming session.

## Phase 10: Barge-In
- service cancellation contract, physical playback interruption, wake word detection

## Phase 11: Home Assistant Pipeline
- complete Assist pipeline, correlated STT/LLM/TTS/playback latency tracing

## Future: Hermes Orchestrator (separate repo)
- Job Manager, workflow engine, tools, progress, retries, confirmations
- notifications, memory, LLM routing, Home Assistant orchestration
- This TTS repo owns: speech admission, scheduling, synthesis, audio, cancellation, metrics
- The orchestrator owns: why speech is generated, jobs, tools, assistant logic

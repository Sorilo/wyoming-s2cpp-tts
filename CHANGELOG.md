# Changelog

## Unreleased

- Phase 9 closure: bounded queue admission, backend-busy retry, queue wait and synthesis timeouts, controlled Wyoming errors, and disconnect cleanup are complete. Isolated Unraid validation passed short and long synthesis (RTF ~0.961), FIFO ordering, queue-full rejection/recovery, and three disconnect/recovery cycles without a persistent 503 latch or unobserved task exception. Full suite: **876 passed, 0 failed, 0 skipped**. PR #2 merged as `1a0b93f`. Wrapper `sha-7db26b7` and backend `sha-6e629d0` are deployed and passed per-container production verification; the compact direct/HA smoke remains. See `docs/PHASE_9_DEPLOYMENT_HANDOFF.md`.

- Phase 8E.1 closure: Q4_K_M runtime tuning complete.  After six benchmark
  phases, the provisional baseline is Q4_K_M at context=32, stride=32,
  threads=8.  Backend first PCM ~1.35s, RTF 0.987 (narrow margin).
  Wrapper image published: ``ghcr.io/sorilo/wyoming-s2cpp-tts:sha-22db725``.
  See ``docs/PERFORMANCE_TUNING_RESULTS.md`` for full evidence and
  ``docs/PHASE_8E1_DEPLOYMENT_HANDOFF.md`` for deployment procedure.
  Phase 9.5 (progressive LLM-to-TTS phrase pipeline) added to roadmap.
  Tuning paused pending end-to-end Home Assistant measurements.

- Phase 8D.2: corrected live quantization benchmark architecture.
  **Critical fix**: the s2.cpp server loads ONE GGUF at startup via the
  ``S2_MODEL`` environment variable; HTTP requests cannot switch models.
  The previous runbook (single container, multi-model Python invocation)
  would have benchmarked the same model repeatedly with different labels.
  Changes:
  * ``benchmark_quantization.py`` now rejects multiple ``--models`` in
    ``--run-real`` mode (exit 1 with clear error).  Dry-run multi-model
    inspection preserved.
  * New orchestrator: ``scripts/run_quantization_benchmark_unraid.sh``
    (495 lines) — default dry-run, ``--run-real`` for live.  Starts one
    backend container per candidate (Q6→Q5→Q4), each with ``S2_MODEL``
    pointing to the correct GGUF.  Waits for ``Launching: s2 --model``
    confirmation in startup logs (bounded 120s timeout, not blind sleep).
    Captures per-candidate container inspect, startup logs, backend
    metrics, GPU telemetry.  Cleans up on EXIT/INT/TERM.
  * Model size estimates corrected (Q6≈4.5 GB, Q5_K_M≈4.0 GB,
    Q4_K_M≈3.6 GB) from verified upstream information.
  * WAV conversion path updated: primary = ``docker exec Hermes-Suite
    ffmpeg``; fallback = Python ``wave`` module (header-only, no transcode).
  * 16 new Phase 8D.2 tests (multi-model rejection, candidate-dir nesting,
    orchestrator syntax, metric parsing, WAV fallback, GPU-busy detection,
    Hermes-Suite ffmpeg path).
  Full suite: **590/590 passing** (all tests, excluding standalone shell
  behavior tests).

- Phase 8D: controlled quantized-model performance and quality benchmark.
  Fixed benchmark-tool issues: removed false "No live RTX 3080 benchmark"
  claim from all scripts and documentation (live benchmarks completed for
  strides 1–24 on Q6_K model).  Hardened backend metric correlation with
  bounded polling (30s max wait for completed ``[Metrics] Streaming`` line)
  replacing naive one-shot ``docker logs`` capture.  Updated WAV conversion
  guidance to reference Hermes-Suite ``ffmpeg`` path (``/usr/bin/ffmpeg``).
  Benchmark harness now records model SHA-256 and file size per run via
  ``--model`` CLI argument.  Added model metadata fields to ``RunResult``
  and aggregate output.  Inserted Phase 8D into roadmap between Phase 8C
  and Phase 9: controlled comparison of Q6_K, Q5_K_M, and Q4_K_M GGUF
  models at fixed stride 4 under identical conditions (same GPU, backend
  build, container, voice, text, and settings).  Added conditional Phase 8E
  placeholder for non-fork runtime tuning as fallback.  Created comprehensive
  ``docs/STREAMING_STRIDE_AND_QUANT_BENCHMARKS.md`` documenting stride
  tuning principles, live RTX 3080 results, quantization methodology,
  and benchmark limitations.  Clarified post-v0.1 roadmap items (dynamic
  model switching, multi-GPU scheduling).  **Status**: Tooling complete. Live Q5_K_M/Q4_K_M quant benchmark and
  human listening still pending. See roadmap and benchmark doc for details.

- Phase 8C: real-time stride tuning infrastructure for RTX 3080 performance
  optimisation.  Added four new environment-backed wrapper settings:
  ``S2_STREAM_DECODE_STRIDE_FRAMES`` (1--64, default 4),
  ``S2_STREAM_HOLDBACK_FRAMES`` (non-negative, default 0),
  ``S2_STREAM_START_BUFFER_MS`` (non-negative ms, default 0), and
  ``S2_LOW_LATENCY`` (bool, default true).  All are strictly validated at
  startup — invalid values raise clear errors rather than falling back to
  unsafe defaults.  The ``S2GenerateRequest`` streaming multipart params
  now explicitly include ``low_latency``, ``stream_decode_stride_frames``,
  ``stream_holdback_frames``, and ``stream_start_buffer_ms``.  The
  buffered generation path is unchanged.
  Audited ``Settings.from_env()``: ``S2_MAX_NEW_TOKENS``, ``S2_TEMPERATURE``,
  ``S2_TOP_P``, ``S2_TOP_K``, ``S2_CHUNKED``, ``S2_OUTPUT_FORMAT``,
  ``S2_MODEL``, ``S2_GPU_INDEX``, ``S2_GPU_LAYERS``, ``S2_CODEC_CPU``,
  ``BARGE_IN_FRIENDLY``, ``CANCEL_ON_CLIENT_DISCONNECT``,
  ``CANCEL_ON_NEW_REQUEST``, and ``MAX_QUEUE_SIZE`` are now parseable
  from environment variables with strict validation.
  Extended streaming ``backend_start`` observability with all tuning
  parameters: ``low_latency``, ``stream_decode_stride_frames``,
  ``stream_holdback_frames``, ``stream_start_buffer_ms``,
  ``codec_decode_context_frames``, ``segment_sentences``, and ``model``.
  Added ``scripts/benchmark_realtime_tuning.py`` — an opt-in,
  dry-run-safe Python benchmark harness that sweeps stride values
  against a real s2.cpp backend, measures RTF/time-to-first-PCM/total
  synthesis, saves PCM artifacts, and produces JSON + Markdown summaries.
  Added ``scripts/run_realtime_tuning_unraid.sh`` — a one-command Unraid
  host orchestration script (default: safe benchmark-only; ``--apply``
  requires ``--yes`` with rollback support).
  Updated Unraid wrapper template with all new tuning variables and
  clear descriptions.  80 new tests (config validation, request contract,
  benchmark math, dry-run safety, env audit, shell syntax).  Full suite:
  **540/540 passing**.  No backend image change required.  The benchmark harness contacts
  the s2.cpp backend directly and works immediately against the running
  backend container — no wrapper rebuild is needed for benchmarking.
  However, for Home Assistant / Wyoming to use the new stride tuning
  environment variables, a NEW WRAPPER IMAGE must be built and deployed;
  the current production wrapper (sha-9c134cc) does not support them.
  Live RTX 3080 benchmarks completed (strides 1-24) — Q6_K model RTF 1.13 at stride 4;
  stride 4 is the preferred Q6_K latency/throughput compromise. Quant comparison pending.


- Phase 8B2 production backend promotion: promoted the Phase 8B1.1-proven
  backend cancellation patch from diagnostic image `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-b8e54f9`
  into the production backend build.  Final live retry artifacts under
  `verification_artifacts/phase_8b1_1_retry/` show 5/5 cancellation/recovery
  cycles passing: `backend_cancel_detected`, `generation_cancel_observed`,
  `final_decode_skipped`, `backend_request_cancelled`, and
  `backend_request_cleanup_done` each appeared exactly once per cancelled
  request, in order, with `reason=client_disconnect`,
  `point=content_provider_complete`, valid monotonic timings, accurate
  frame/decode/PCM counters, `queued_pcm_bytes=0`, and `server_busy=false`.
  Recovery synthesis passed 5/5 for audio, protocol terminal event, and valid
  non-empty PCM.  GPU utilization returned to idle and the backend/wrapper
  containers remained running.  Wrapper BrokenPipe task-exception noise remains
  a narrow disconnect logging issue but did not block cleanup or recovery.
  Published production backend image `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd` from commit `edf89bd7c5554769bb36cbd049b6fbb98bcb9d41` with digest
  `sha256:c29e41e59b470d58bf4b88c11c9ec753e00fa74a3bffbb003bc257fb9c6e46d9`.  Rollback backend remains `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b`.  Wrapper image unchanged: `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-9c134cc`.

- Phase 8B1 tooling correction: fixed the live verification harness recovery
  classifier for standalone legacy `Synthesize` requests.  The correct terminal
  sequence for the harness recovery request is `AudioStart` → `AudioChunk`* →
  `AudioStop`; `synthesize-stopped` is required only for Wyoming streaming-text
  sessions.  The harness now reports `audio_recovery_success`,
  `protocol_terminal_success`, `pcm_valid`, `first_audio_ms`, `completion_ms`,
  `pcm_bytes`, and `exact_failure_reason` separately.  Reclassified the first
  Phase 8B1 client artifact as 5/5 audio/protocol/PCM recovery success, while
  keeping Phase 8B1 incomplete because diagnostic backend cancellation logs were
  not captured.  Reworked `capture_phase_8b1_logs.sh` for unattended
  `--duration` capture with metadata, image IDs, status/health, wrapper/backend
  logs, GPU samples, and timestamps.  Added a long-form context comparison probe
  and runbook for contexts 4, 64, and auto/160.  No runtime wrapper/backend code
  changed and no image publication is required.

- Phase 8A: implemented Wyoming client disconnect and backend-stream cleanup.
  Added ``cancel()`` method to ``S2StreamResult`` that closes the underlying
  HTTP response, unblocking any ``asyncio.to_thread(read)`` worker threads.
  Wrapped all ``write_event()`` calls in ``send_streaming_audio`` and
  ``send_audio`` with ``try/except Exception`` to catch client disconnects
  (including ``TypeError`` from already-closed asyncio transports).  On
  disconnect, the ``async for`` loop breaks, triggering ``GeneratorExit`` in
  the async generator, which calls ``stream.cancel()`` and ``__exit__`` to
  close the backend HTTP stream.  Added ``client_disconnected``,
  ``synthesis_cancelled``, and ``synthesis_cancel_requested`` observability
  events with connection/synthesis IDs, reason, elapsed ms, PCM bytes/chunks,
  and AudioStart-emitted flag.  4 new tests (3 unit + 1 TCP integration).
  Full suite: 401/401 passing.  Diagnostic test confirms backend GPU inference
  continues after HTTP close — Phase 8B required for backend cancellation.

- Phase 7.5D2: enabled genuine progressive backend streaming in the production
  wrapper.  Changed ``segment_sentences`` default from ``True`` to ``False`` in
  ``S2GenerateRequest`` (``app/s2_client.py``).  When ``S2_STREAM=true``, the
  wrapper now sends ``segment_sentences=false``, ``low_latency=true``, and
  optionally ``codec_decode_context_frames=4`` to the backend, activating the
  raw frame-level progressive synthesis path instead of the sentence-buffered
  path.  First backend PCM now arrives at approximately 150 ms (previously
  gated behind sentence completion at ~6500 ms).  Added ``S2_SEGMENT_SENTENCES``
  and ``S2_CODEC_CONTEXT_FRAMES`` environment variables with validation (only
  values 4, 64, 160 or auto accepted for context).  Added ``segment_sentences``
  and ``codec_decode_context_frames`` observability fields to ``backend_start``
  log event.  Updated Unraid wrapper template with new configuration variables.
  23 new tests (config defaults, context validation, request contract).  Full
  suite: 397/397 passing.  No backend image change required.

- Phase 7.5B: live deployment verification and streaming-metrics audit.
  Confirmed one-request/one-audio lifecycle through progressive streaming path
  (compatibility_synthesize_deferred → syn_trigger → backend_start →
  backend_stream_headers → backend_stream_first_audio → first_wyoming_audio →
  backend_stream_done → audio_out → syn_stopped).  Discovered and fixed two
  metric-only double-counting bugs in ``backend_stream_done`` and ``audio_out``
  observability lines that double-counted flush-carry chunk bytes and chunk
  counts (``+ len(flush_chunks)`` and ``+ sum(flush_chunks)``).  No actual audio
  bytes were affected — the 2,080-byte live discrepancy was purely in the JSON
  log fields.  Renamed ``total_elapsed_ms`` → ``total_backend_stream_ms`` in
  ``backend_stream_done``.  Enhanced ``first_wyoming_audio`` with ``elapsed_ms``,
  ``time_to_first_backend_audio_ms``, and
  ``wrapper_first_audio_forwarding_overhead_ms`` timing fields using one
  consistent monotonic start point.  Added ``total_synthesis_ms`` to
  ``syn_stopped``.  5 new deterministic PCM byte-counting tests prove: every
  backend byte is counted exactly once, every emitted byte is counted exactly
  once, clean aligned streams match, first backend chunk is included in totals,
  and realistic 222,580-byte/44100 Hz scenario produces exact accounting (26
  chunks, not 27).  Live progressive window measured at ~5 ms — wrapper
  streaming works but backend generation dominates latency (2,932 ms to first
  audio).  Full suite: 374/374 passing.

- Phase 7.5A: wired true progressive backend HTTP PCM streaming into the
  production Wyoming wrapper. When ``S2_STREAM=true``, the handler now uses
  ``synthesize_s2cpp_streaming_tts_events()`` / ``generate_stream()`` to
  yield Wyoming ``AudioStart`` / ``AudioChunk`` / ``AudioStop`` events
  progressively as backend transport chunks arrive, instead of buffering
  the complete response. When ``S2_STREAM=false``, the existing buffered
  ``generate_multipart()`` path is preserved unchanged.  Added structured
  observability fields: ``backend_stream_headers``, ``backend_stream_first_audio``,
  ``first_wyoming_audio``, ``backend_stream_done`` with timing (``elapsed_ms``,
  ``total_elapsed_ms``), chunk count, and PCM byte totals.  Extended
  ``synthesize_s2cpp_streaming_tts_events()`` to accept ``LogContext`` for
  correlation.  Added ``_synthesize_text_streaming()`` async generator on
  ``FakeTtsEventHandler``.  13 new streaming-specific tests + existing suite
  adjustments.  Full suite: 367/368 passing (1 pre-existing Unraid template SHA
  test unchanged).

- Phase 7B.3: fixed duplicate synthesis for Wyoming compatibility events. 355 tests.

- Phase 7B.1: added structured request-level observability.
  New app/observability.py module generates per-connection and per-synthesis
  correlation IDs, fingerprints text with SHA-256 (never logs full text), and
  emits structured JSON log lines at INFO level through the wyoming-s2cpp-tts.obs
  logger.  Instrumented connection open/close, every incoming Wyoming event type,
  synthesis trigger (legacy vs streaming), backend request lifecycle
  (start/done/elapsed/status/bytes), and outgoing Wyoming audio lifecycle
  (AudioStart, chunk count, PCM bytes, AudioStop, synthesize-stopped).
  Correlation IDs propagate through the full handler, queue, synthesis, and
  audio-event paths. 17 new tests + 325 existing = 342 total.

- Phase 7B: implemented wrapper voice discovery and Home Assistant voice selection.
  Added `app/voice_discovery.py` for safe `.s2voice` file enumeration from the
  `/voices` directory. Updated `build_info_event()` to advertise discovered
  voices through Wyoming Describe alongside the generic `s2-pro` fallback.
  Wired voice selection through the Wyoming handler: client-requested voice,
  `S2_DEFAULT_VOICE`, and generic fallback priority. Selected/default voice
  is forwarded as `voice` and `voice_dir` multipart fields to the backend.
  Unknown and unsafe voice IDs are rejected. Drop-in discovery: new `.s2voice`
  files are discoverable without rebuild/restart. Home Assistant may require a
  Wyoming integration reload to see newly dropped-in voices. Both buffered
  and streaming Wyoming paths propagate voice consistently. Added
  `S2_VOICE_DIR` and `S2_DEFAULT_VOICE` env vars. Updated wrapper Dockerfile,
  entrypoint, and Unraid template with read-only `/voices` mount. 38 new tests
  (20 discovery + 18 voice selection/Describe/synthesis). Full suite: 323/325
  passing (2 pre-existing stale doc tests unchanged).

- Phase 7A: created six CMU ARCTIC voice profiles and verified direct backend synthesis.
  Created `.s2voice` profiles for bdl, rms, jmk, slt, clb, and eey under
  `/mnt/user/appdata/s2cpp/voices`. All six profiles are visible via `s2 --list-voices`
  and pass direct multipart synthesis (6/6, valid RIFF/WAVE output). Human listening:
  acceptable temporary voices, somewhat robotic, no downstream defect confirmed;
  personal clean recording planned for later quality test. Operational caveats
  documented: FestVox HTTPS unreachable (HTTP fallback used), `--list-voices` requires
  GPU runtime due to CUDA library linkage. Wrapper behavior, images, and Home
  Assistant settings unchanged.

- Phase 6E: corrected deployment safety baseline and forward plan.
  Updated Unraid templates to pin verified immutable images, rewrote stale
  README deployment/status claims, clarified that Wyoming protocol streaming is
  verified while true progressive backend HTTP audio streaming is still pending,
  and split the remaining roadmap into Phase 7A, 7B, 7.5, and later reliability
  phases.

- Phase 6D: verified Home Assistant deployment end-to-end.
  HA discovers service at 192.168.1.45:10200, s2-pro voice visible,
  preview generates and audibly plays real speech through the Wyoming
  streaming TTS lifecycle.

- Phase 6C: implemented full Wyoming streaming TTS state machine.
  Added synthesize-start, synthesize-chunk, synthesize-stop, and
  synthesize-stopped event handling. Fixed HA preview spinner hang.
  Legacy synthesize still works. 10 new protocol tests; 287/287 pass.

- Phase 6B1: fixed deployed wrapper Synthesize crash.
  Changed synthesize_s2cpp_tts_events() from client.generate() (JSON)
  to client.generate_multipart() (multipart/form-data). Updated
  build_info_event() for real backend metadata.

- Phase 6B0: built CPU-only Wyoming wrapper Docker image.
  Added publish-wrapper.yml workflow for GHCR with sha-* and edge tags.

- Phase 6A: built CUDA s2.cpp backend Docker image.
  Real CUDA/model/codec loading verified on RTX 3080.

- Phase 5.5B: verified smoke harness against real s2.cpp backend.
  Real contract: audio/L16; rate=44100; channels=1.

- Phase 5.5A: implemented opt-in real-backend smoke-test harness.

- Phase 5D: implemented structured TTS metrics and tracing.

- Phase 5C: implemented streamed audio to Wyoming events helper.

- Phase 5B: implemented streaming client interface.

- Phase 5A.2: corrected multipart fields to rodrigomatta/s2.cpp spec.

- Earlier phases: Wyoming TCP server, fake PCM, s2.cpp JSON client,
  container scaffold, CUDA plan. Full history in git log.

# Changelog

## Unreleased

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

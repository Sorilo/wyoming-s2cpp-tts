# Next Hermes `/goal` prompts

Run phases one at a time. This file is regenerated from the actual repository
state after every `/goal` run. Do not copy stale assumptions forward.

## Current state after Phase 9

- Repository: `main`; PR #2 merged as `1a0b93f`.
- Runtime commit `7db26b7` and documentation commit `105121b` are ancestors of `main`.
- Full test baseline: **876 passed, 0 failed, 0 skipped**.
- Isolated Unraid validation: **PASS** for short/long synthesis, FIFO,
  queue-full recovery, and three disconnect/recovery cycles.
- Production backend: `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-6e629d0`.
- Production wrapper: `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-7db26b7`.
- Production retries: `S2_BACKEND_BUSY_MAX_RETRIES=10`,
  `S2_BACKEND_BUSY_RETRY_DELAY_MS=500`; timeouts remain `30` and `120`.
- Per-container production startup/wiring checks passed; compact direct/HA smoke
  remains. Rollback is backend `sha-edf89bd`, wrapper `sha-12f3bf8`, retries
  `3` and `200`.

## Next official phase: Phase 9B

Create a behavior-preserving scheduler/domain refactor around `SpeechRequest`,
`SpeechMetadata`, `ScheduledSpeech`, `SpeechScheduler`, and `SynthesisSession`.
Preserve FIFO, queue limits, retry/deadline semantics, cancellation, disconnect
recovery, and Wyoming event ordering. Semantic priority, replacement,
interrupt-policy behavior, progressive phrase queues, barge-in, playback
interruption, and admin HTTP endpoints remain deferred.

## Historical prompt: Phase 9 queue, busy handling, and timeout policy

```text
/goal

Proceed with Phase 9 only: queue capacity, busy handling, backend HTTP 503
handling, queue wait timeout, synthesis timeout, and controlled Wyoming failure
behavior. Do not begin Phase 10 or long-form audio-quality work.

Project:
/workspace/wyoming-s2cpp-tts

Current verified backend/wrapper baseline:
- Backend: ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd
- Backend digest: sha256:c29e41e59b470d58bf4b88c11c9ec753e00fa74a3bffbb003bc257fb9c6e46d9
- Backend rollback: ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b
- Wrapper: ghcr.io/sorilo/wyoming-s2cpp-tts:sha-9c134cc
- Defaults to preserve: S2_SEGMENT_SENTENCES=false, S2_CODEC_CONTEXT_FRAMES=4

Required work:
1. Inspect git status and current queue/synthesis handling in app code and tests.
2. Define deterministic behavior for max queue depth, already-busy backend, backend HTTP 503, queue wait timeout, synthesis timeout, and client-visible Wyoming error/termination behavior.
3. Add tests first for each behavior.
4. Implement the smallest production changes needed to pass those tests.
5. Preserve Phase 8B2 cancellation behavior and observability.
6. Do not modify backend model, quantization, voice profiles, or Home Assistant settings.
7. Run focused tests and the full suite with zero failures.
8. Update CHANGELOG.md, TODO.md, docs/ROADMAP.md, docs/NEXT_GOAL_PROMPTS.md, and relevant setup/troubleshooting docs.
9. Publish new image(s) only if runtime code/template changes require them; report exact provenance.

Acceptance criteria:
- Queue and busy/timeout behavior is deterministic, tested, and documented.
- Controlled failures cleanly terminate Wyoming requests without hanging Home Assistant.
- Existing synthesis, streaming, voice selection, and cancellation tests remain green.
```

## Current state after Phase 7.5C

- Repository branch: `main`.
- Backend image: `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b`
- s2.cpp revision: `rodrigomatta/s2.cpp` @ `2c33261938da1a41d713768b1b391b4d368d7d2c`
- Wrapper image: `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-974e220`
- Test baseline: 374/374 passing.
- **Root cause confirmed**: cpp-httplib's `DataSink.write()` buffers all
  intermediate writes. Transfer-Encoding: chunked headers arrive at 0-1ms,
  but zero audio data reaches the client until `sink.done()` is called at
  synthesis completion. All 7 tested configurations (stride=1 through 16,
  low_latency on/off, start_buffer 0/3000ms) produce identical non-progressive
  behavior.
- Architecture: Type E — transport-chunked but NOT inference-progressive.
- The pipeline codec already decodes incrementally during generation. The
  bottleneck is purely in the HTTP framework's output buffering.

## Next phase: Phase 7.5D — Backend httplib Flush Implementation

Goal: Modify the s2.cpp backend's chunked content provider to flush HTTP
writes progressively, so audio data reaches the client during generation
rather than only at completion.

### Required work

1. Inspect the bundled `third_party/httplib.h` at the pinned revision
   (`2c33261938da1a41d713768b1b391b4d368d7d2c`) for `DataSink` flush support:
   - Check for `DataSink::flush()`, `DataSink::write_and_flush()`, or socket
     access methods
   - Check for write-buffer-size configuration on `httplib::Server` or
     `httplib::Response`

2. If flush support exists:
   - Add a `sink.flush()` call (or equivalent) after every `sink.write()` in
     the chunked content provider lambda at `src/s2_server.cpp:840-847`
   - Or add flush after a byte threshold (e.g., every 4096 bytes accumulated)

3. If no flush support exists:
   - Implement Option C: call `sink.done()` periodically to force chunk
     finalization, then re-initialize chunked streaming
   - Or implement Option D: bypass the chunked content provider and write
     chunked-framed data directly to the socket with `fflush()`

4. Rebuild the backend Docker image with the modified source.

5. Benchmark the rebuilt image against the live backend:
   - Verify progressive transport reads arrive within 500ms of generation start
   - Measure time-to-first-audio for short/medium/long text
   - Compare against Phase 7.5C baseline

6. Publish the rebuilt backend image as
   `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-<new-commit>`.

7. Update documentation: CHANGELOG, TODO, ROADMAP, NEXT_GOAL_PROMPTS.

Target: first audio within 500ms for medium text (currently 6500ms).

Do not modify wrapper code, voice profiles, or Home Assistant during this phase.


- Repository branch: `main`.
- Wrapper image: to be published by CI workflow.
- Full test baseline: 374 passing (zero failures).
- Live progressive streaming verified. Progressive window ~5 ms — backend dominates latency.
- Streaming observability corrected and enhanced:
  - ``first_wyoming_audio``: ``elapsed_ms``, ``time_to_first_backend_audio_ms``, ``wrapper_first_audio_forwarding_overhead_ms``
  - ``backend_stream_done``: ``total_backend_stream_ms``, ``total_pcm_bytes``, ``chunk_count``
  - ``syn_stopped``: ``total_synthesis_ms``
  - No more double-counting of flush-carry bytes/chunks.
- 5 new deterministic PCM byte-counting tests.
- Backend image, voices, live containers, and Home Assistant untouched.

## Next phase: S2 Backend Generation & Flush Investigation

Since first backend audio arrived at ~2,932 ms and the stream completed at
~2,937 ms (only ~5 ms progressive window), the recommended next latency phase
should investigate s2.cpp backend generation and flush behavior before assuming
further wrapper optimization will help.

### Phase 7.5C: Backend Early-Audio Investigation

Goal: benchmark the s2.cpp backend directly to determine where time-to-first-audio
latency comes from, and whether backend ``low_latency`` / streaming params can
cause the backend to emit audio sooner.

Required work:

1. Benchmark direct backend /generate calls:
   - Variation A: ``low_latency=true``, ``chunked=true``, ``stream=true`` (current)
   - Variation B: ``low_latency=false``, ``chunked=false``, ``stream=false`` (buffered)
   - Variation C: different text lengths (short, medium, long)
   - Variation D: different ``max_new_tokens`` values
   - Variation E: different ``temperature`` values
   - Measure time-to-first-byte and total time for each

2. For the streaming path, measure:
   - Time from POST to first response byte (headers)
   - Time from POST to first audio byte
   - Time from POST to last audio byte
   - Compare with wrapper-side measurements

3. Check s2.cpp source code / documentation for:
   - Streaming buffer settings
   - ``low_latency`` mode behavior
   - ``chunked`` output behavior
   - Any configurable flush policies

4. Determine whether:
   - The backend holds audio until generation completes despite streaming flags
   - The backend emits progressively but in large chunks
   - The ``low_latency`` param is being correctly parsed and applied
   - Model architecture prevents early emission

5. Do not:
   - Modify the wrapper code
   - Modify voice profiles
   - Change Home Assistant settings
   - Implement Phase 8 cancellation

The goal is to find whether backend configuration changes can reduce the ~2.9s
time-to-first-audio before investing in wrapper-side streaming optimization.
2026-07-08


- Repository branch: `main`.
- Wrapper image: to be published by CI workflow.
- Full test baseline: 367 passing (368 total, 1 pre-existing Unraid template SHA test unchanged).
- Streaming routing wired: ``S2_STREAM=true`` uses ``generate_stream()`` / progressive event emission; ``S2_STREAM=false`` preserves buffered ``generate_multipart()``.
- Streaming observability: ``backend_stream_headers``, ``backend_stream_first_audio``, ``first_wyoming_audio``, ``backend_stream_done`` with timing fields.
- Voice discovery, compatibility synthesize deferral, Wyoming text-streaming state machine, fake backend — all preserved.
- 13 new streaming-specific tests.
- Phase 7.5B (deployment + live latency measurement) remains.

## Previous state after Phase 7A

- Repository branch: `main`.
- Deployment reconciliation baseline commit: `ea72838`.
- Full test baseline before Phase 7A: 287 tests passing. No application Python
  files were changed in Phase 7A.
- Two-container deployment verified on Unraid:
  - Backend: `s2cpp-backend`, image `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b`
  - Wrapper: `wyoming-s2cpp-tts`, image `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-89ed2dc`
  - Network: `sorilonet`
  - Backend endpoint from wrapper: `http://s2cpp-backend:3030/generate`
  - Home Assistant endpoint: `192.168.1.45:10200`
  - Home Assistant VM: `192.168.1.233`
- Home Assistant preview produces real audible speech.
- Wyoming protocol streaming is implemented and verified; progressive
  backend-audio streaming is not yet wired (Phase 7.5).
- Six CMU ARCTIC `.s2voice` profiles created in Phase 7A:
  `cmu_bdl_male_us`, `cmu_rms_male_us`, `cmu_jmk_male_canadian`,
  `cmu_slt_female_us`, `cmu_clb_female_us`, `cmu_eey_female_us`.
  Persistent directory: `/mnt/user/appdata/s2cpp/voices`.
  All six visible via `s2 --list-voices` (GPU-backed, libcuda.so.1 linked).
  Direct multipart synthesis: 6/6 passed (valid RIFF/WAVE).
- Human listening: acceptable temporary voices, somewhat robotic, no downstream
  defect; personal clean recording planned for later quality test.
- Operational caveats: FestVox HTTPS unreachable from Unraid (HTTP fallback
  used); `--list-voices` requires GPU runtime.
- Wrapper does not yet discover or expose voice profiles through Wyoming
  Describe. Voice selection in Home Assistant is not yet wired. These are
  Phase 7B.
- Do not assume an HTTP voice-management API. The pinned behavior is
  `POST /generate`, voice/voice_dir multipart fields, CLI voice creation with
  `--prompt-audio`/`--prompt-text`/`--voice`/`--save-voice`/`--voice-dir`, and
  CLI voice listing with `--list-voices`.

## Phase 7A prompt — one-time custom `.s2voice` profile creation and direct backend verification (COMPLETED)

Phase 7A is complete. Six CMU ARCTIC voice profiles were created and verified
via direct backend synthesis (6/6 passed). See `docs/PHASE_7A_VERIFICATION.md`
for full results. Wrapper behavior, images, and Home Assistant settings were not
changed.

## Phase 7B prompt — wrapper voice discovery, selection, default voice, Wyoming Describe, Home Assistant selection, and drop-in discovery

```text
/goal

Proceed with Phase 7B only: wrapper voice discovery, voice selection, default
voice configuration, Wyoming Describe exposure, Home Assistant selection, and
drop-in discovery for later personal voice profiles.

Project:
/workspace/wyoming-s2cpp-tts

Current verified deployment:
- Backend: s2cpp-backend at http://s2cpp-backend:3030/generate
- Backend image: ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b
- Wrapper image before this phase: ghcr.io/sorilo/wyoming-s2cpp-tts:sha-89ed2dc
- Docker network: sorilonet
- Home Assistant endpoint: 192.168.1.45:10200
- Backend voice directory inside backend: /voices
- Host voices directory: /mnt/user/appdata/s2cpp/voices
- Six .s2voice profiles already exist from Phase 7A:
  cmu_bdl_male_us, cmu_rms_male_us, cmu_jmk_male_canadian,
  cmu_slt_female_us, cmu_clb_female_us, cmu_eey_female_us
- All six verified via direct backend synthesis (6/6 passed)
- A personal clean voice recording will be added later; the wrapper must support
  drop-in discovery of new .s2voice files without rebuild

Important constraints:
- Do not create voice profiles in Phase 7B; Phase 7A already created six.
- Do not change backend image or model unless explicitly required and approved.
- Do not implement true progressive backend HTTP streaming; that is Phase 7.5.
- Do not implement cancellation or barge-in; those are later phases.

Required work:
1. Inspect git status, recent commits, app/config.py, app/s2_client.py,
   app/wyoming_server.py, tests, docker/wrapper/Dockerfile,
   docker/wrapper/entrypoint.sh, unraid/my-wyoming-wrapper.xml,
   docs/ARCHITECTURE.md, docs/HOME_ASSISTANT_SETUP.md, docs/ROADMAP.md,
   TODO.md, CHANGELOG.md, and docs/NEXT_GOAL_PROMPTS.md.
2. Add a read-only /voices mount to the wrapper template and image/runtime
   documentation, or explicitly justify an alternative that still lets the
   wrapper discover profiles safely.
3. Implement automatic .s2voice discovery: enumerate valid .s2voice files from
   the /voices directory at startup (and optionally on Describe events).
4. Sanitize profile IDs and prevent path traversal; reject names containing path
   separators, parent-directory traversal, unexpected suffixes, or unsafe
   characters.
5. Add S2_DEFAULT_VOICE environment/config support.
6. Preserve generic s2-pro/default fallback when no voice is configured or
   requested.
7. Expose all discovered voice profiles through Wyoming Describe so Home
   Assistant can list and select them.
8. Read the requested Wyoming voice selection from Home Assistant/Wyoming
   events.
9. Pass voice and voice_dir in the multipart request to the backend for each
   synthesis.
10. Support drop-in discovery: new .s2voice files placed in /voices (e.g. a
    future personal profile) should be discoverable without rebuilding or
    restarting the wrapper container (e.g. periodic re-scan or event-driven).
11. Add deterministic tests for voice enumeration, sanitization, Describe
    exposure, selected voice propagation, default voice config, fallback
    behavior, and drop-in discovery.
12. Update wrapper Docker/Unraid docs and templates for the /voices read-only
    mount and new environment variables.
13. Run focused tests first, then the full Python suite.
14. Build and publish one immutable wrapper image only after tests pass.
15. Deploy the new wrapper image to Unraid and verify Home Assistant can select
    each of the six CMU ARCTIC voices and produce speech.
16. Update TODO.md, CHANGELOG.md, docs/ROADMAP.md, docs/HOME_ASSISTANT_SETUP.md,
    docs/ARCHITECTURE.md, README.md if needed, and docs/NEXT_GOAL_PROMPTS.md.
17. Make one focused commit and push it.

Acceptance criteria:
- Wrapper discovers all existing .s2voice files through a read-only /voices
  mount or documented safer equivalent.
- New .s2voice files dropped into /voices are discoverable without rebuild.
- Unsafe voice IDs cannot escape the voices directory.
- Wyoming Describe advertises all discovered selectable voices.
- Home Assistant displays and can select each of the six custom voices.
- Selected voice and voice_dir are sent in multipart /generate requests.
- S2_DEFAULT_VOICE works and default s2-pro fallback remains available.
- Tests pass, including full Python suite.
- One new immutable wrapper image is published and deployed.
- Working tree is clean after commit and push.

Suggested commit:
feat: expose saved s2 voices through Wyoming with drop-in discovery
```

## Phase 7.5A prompt — true progressive backend HTTP audio streaming (COMPLETED ✅)

Completed 2026-07-08.  ``S2_STREAM=true`` now routes production synthesis through
``synthesize_s2cpp_streaming_tts_events()`` / ``generate_stream()`` instead of
buffered ``generate_multipart()``.  13 new tests (routing, success, voice,
failure, compat, progressive proof).  367/368 tests pass.  Structured
observability extended with streaming-specific timing fields.  See
``CHANGELOG.md`` and ``TODO.md`` for full results.

```text
/goal

Proceed with Phase 7.5 only: wire true progressive backend HTTP audio streaming into the production Wyoming event handler when S2_STREAM=true.

Project:
/workspace/wyoming-s2cpp-tts

Current verified distinction:
- Wyoming protocol streaming is implemented and verified: the wrapper handles synthesize-start, synthesize-chunk, and synthesize-stop, then emits AudioStart, AudioChunk, AudioStop, and synthesize-stopped for Home Assistant.
- Progressive backend-audio streaming is not currently used by the production handler: although S2_STREAM is parsed and synthesize_s2cpp_streaming_tts_events() / generate_stream() exist, the live handler still calls buffered synthesize_s2cpp_tts_events() via generate_multipart(), then sends Wyoming audio events.

Required work:
1. Inspect git status, recent commits, app/config.py, app/s2_client.py, app/wyoming_server.py, app/audio.py, app/metrics.py, tests/test_streaming_protocol.py, tests/test_wyoming_streaming.py, tests/test_wyoming_s2cpp_backend.py, docs/ARCHITECTURE.md, docs/ROADMAP.md, TODO.md, CHANGELOG.md, and docs/NEXT_GOAL_PROMPTS.md.
2. Write tests first that fail against the current production handler because S2_STREAM=true does not progressively forward backend stream events.
3. Preserve legacy synthesize behavior and Home Assistant streaming-text Wyoming protocol behavior.
4. When S2_STREAM=true, progressively forward events from synthesize_s2cpp_streaming_tts_events() in the production event handler.
5. Do not build a complete list of audio events before writing in the streaming path.
6. Send AudioStart only after backend response metadata is validated.
7. Preserve PCM frame alignment across arbitrary HTTP chunks.
8. Ensure AudioStop and synthesize-stopped ordering on successful streaming sessions.
9. Close the backend stream on normal completion, backend error, and early consumer exit.
10. Preserve S2_STREAM=false as the buffered generate_multipart() fallback.
11. Measure time to first Wyoming audio before and after with the available TTS-side metrics or a deterministic local harness; clearly label what is and is not measured.
12. Run focused streaming tests, then the full Python suite.
13. Publish and deploy one immutable wrapper image only after tests pass.
14. Update README.md, docs/ARCHITECTURE.md, docs/HOME_ASSISTANT_SETUP.md, docs/ROADMAP.md, TODO.md, CHANGELOG.md, and docs/NEXT_GOAL_PROMPTS.md.
15. Make one focused commit and push it.

Do not:
- Implement disconnect/cancellation beyond cleanup needed for normal streaming resource safety.
- Implement queue policy changes, barge-in, Faster-Whisper, VAD, wake word, or release tasks.
- Change backend image or model unless explicitly required and approved.

Acceptance criteria:
- Tests prove S2_STREAM=true production handler progressively writes backend stream events.
- S2_STREAM=false still uses buffered generate_multipart() fallback.
- Legacy synthesize and streaming-text protocol behavior remain compatible with Home Assistant.
- Backend stream closes on completion, error, and early consumer exit.
- Full Python suite passes.
- One immutable wrapper image is published and deployed only after tests pass.
- Documentation clearly reflects the new streaming behavior and remaining limitations.
- Working tree is clean after commit and push.

Suggested commit:
feat: wire progressive backend streaming into Wyoming handler
```

## Phase 8 prompt — disconnect cleanup and backend cancellation limitations

```text
/goal

Proceed with Phase 8 only: client disconnect cleanup, open HTTP stream closure, cancellation behavior, and documented backend cancellation limitations.

Project:
/workspace/wyoming-s2cpp-tts

Current prerequisite:
- Phase 7.5 should already have wired true progressive backend HTTP audio streaming into the production handler when S2_STREAM=true.
- If Phase 7.5 is not complete, stop and update the plan instead of implementing Phase 8 out of order.

Required work:
1. Inspect git status, recent commits, app/wyoming_server.py, app/s2_client.py, app/audio.py, app/metrics.py, tests, docs/ARCHITECTURE.md, docs/ROADMAP.md, TODO.md, CHANGELOG.md, and docs/NEXT_GOAL_PROMPTS.md.
2. Add deterministic lifecycle and resource-cleanup tests first.
3. Detect client disconnect/write failure while sending Wyoming audio events.
4. Cancel the active async synthesis task after disconnect/write failure.
5. Close an open S2StreamResult/HTTP response on normal completion, backend error, cancellation, and early consumer exit.
6. Stop forwarding chunks after disconnect/cancellation.
7. Do not emit successful AudioStop or synthesize-stopped after a failed or cancelled session unless required by the installed Wyoming protocol and explicitly justified in code comments and docs.
8. Document that closing the HTTP client connection may not stop all GPU work if the upstream backend lacks an active cancellation API.
9. Preserve successful synthesis behavior, S2_STREAM=false fallback, and Home Assistant streaming-text compatibility.
10. Run focused lifecycle tests, then the full Python suite.
11. Publish/deploy an immutable wrapper image only if runtime code changed and tests pass.
12. Update README.md, docs/ARCHITECTURE.md, docs/HOME_ASSISTANT_SETUP.md, docs/ROADMAP.md, TODO.md, CHANGELOG.md, and docs/NEXT_GOAL_PROMPTS.md.
13. Make one focused commit and push it.

Do not:
- Add a fake upstream cancellation API.
- Claim GPU work stops immediately unless actually proven.
- Implement queue/busy/timeout policy beyond what is needed for disconnect cleanup; that is Phase 9.
- Implement barge-in testing; that is Phase 10.

Acceptance criteria:
- Client disconnect/write failure is detected.
- Active async synthesis is cancelled.
- Open backend stream/HTTP response is closed on all tested lifecycle paths.
- Chunks stop forwarding after cancellation.
- Success terminal events are not emitted after failed/cancelled sessions unless protocol-required and justified.
- Backend cancellation limitations are documented.
- Full Python suite passes.
- Working tree is clean after commit and push.

Suggested commit:
fix: clean up synthesis streams on client disconnect
```

## Prompt-generation guidance

Every future generated prompt must:

- name the exact next incomplete phase
- include `/workspace/wyoming-s2cpp-tts` as the project path
- include deployment/image/network context
- include quota/risk protections
- require inspection of repository areas touched by the phase
- define exact scope, exclusions, acceptance criteria, and tests
- state which claims remain unverified
- require one focused commit
- require status/documentation updates
- require the final response to include the following phase's prompt

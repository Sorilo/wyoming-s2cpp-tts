# Next Hermes `/goal` prompts

Run phases one at a time. This file is regenerated from the actual repository
state after every `/goal` run. Do not copy stale assumptions forward.

## Current state after Phase 7B

- Repository branch: `main`.
- Wrapper image: `sha-b5cbee1` (to be filled after publish).
- Full test baseline: 323 passing (2 pre-existing stale doc test failures unchanged).
- Voice discovery implemented: wrapper scans `/voices` for `.s2voice` profiles.
- Wyoming Describe advertises `s2-pro` plus all discovered voices.
- Client-requested voice, `S2_DEFAULT_VOICE`, and generic fallback all work.
- Design constraints from Phase 7A still apply: see prior state below.

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

## Phase 7.5 prompt — true progressive backend HTTP audio streaming

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

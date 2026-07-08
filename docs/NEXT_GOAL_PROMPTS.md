# Next Hermes `/goal` prompts

Run phases one at a time. This file is regenerated from the actual repository
state after every `/goal` run. Do not copy stale assumptions forward.

## Current state after Phase 6E

- Repository branch: `main`.
- Deployment reconciliation baseline commit before Phase 6E: `b97e4ea6ec041cfb0b750b0b05c1b99d35909b29`.
- Full test baseline before Phase 6E: 287 tests passing.
- Two-container deployment verified on Unraid:
  - Backend: `s2cpp-backend`, image `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b`
  - Wrapper: `wyoming-s2cpp-tts`, image `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-89ed2dc`
  - Network: `sorilonet`
  - Backend endpoint from wrapper: `http://s2cpp-backend:3030/generate`
  - Home Assistant endpoint: `192.168.1.45:10200`
  - Home Assistant VM: `192.168.1.233`
- Home Assistant preview produces real audible speech.
- Wyoming protocol streaming is implemented and verified: the wrapper handles `synthesize-start`, `synthesize-chunk`, and `synthesize-stop`, then emits `AudioStart`, `AudioChunk`, `AudioStop`, and `synthesize-stopped` for Home Assistant.
- Progressive backend-audio streaming is not currently used by the production handler: although `S2_STREAM` is parsed and `synthesize_s2cpp_streaming_tts_events()` / `generate_stream()` exist, the live handler still calls buffered `synthesize_s2cpp_tts_events()` via `generate_multipart()`, then sends Wyoming audio events.
- Custom `.s2voice` profile creation and wrapper voice selection are not implemented.
- Do not assume an HTTP voice-management API. The pinned behavior to plan against is `POST /generate`, reference audio plus exact reference transcript, saved voice selection through `voice` and `voice_dir`, CLI voice profile creation with `--prompt-audio`, `--prompt-text`, `--voice`, `--save-voice`, and `--voice-dir`, and CLI voice listing with `--list-voices`.

## Phase 7A prompt — one-time custom `.s2voice` profile creation and direct backend verification

```text
/goal

Proceed with Phase 7A only: one-time custom .s2voice profile creation and direct backend verification.

Project:
/workspace/wyoming-s2cpp-tts

Current verified deployment:
- Backend container: s2cpp-backend
- Backend image: ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b
- Backend internal endpoint: http://s2cpp-backend:3030/generate
- Backend GPU: NVIDIA RTX 3080
- Backend model: /models/s2-pro-q6_k.gguf
- Backend persistent voices directory inside container: /voices
- Host voices directory: /mnt/user/appdata/s2cpp/voices
- Wrapper container: wyoming-s2cpp-tts
- Wrapper image: ghcr.io/sorilo/wyoming-s2cpp-tts:sha-89ed2dc
- Wyoming endpoint: 192.168.1.45:10200
- Home Assistant VM: 192.168.1.233
- Docker network: sorilonet
- Home Assistant discovery succeeds, s2-pro is visible, and real speech is audible
- Tests before Phase 7A: 287 passing

Important constraints:
- Do not modify wrapper behavior during Phase 7A.
- Do not rebuild or publish Docker images.
- Do not modify Home Assistant settings.
- Do not implement wrapper voice discovery or Home Assistant voice selection; that is Phase 7B.
- Do not implement true progressive backend streaming; that is Phase 7.5.
- Do not assume an HTTP voice-management endpoint such as /v1/voices unless source inspection proves it exists.

Real upstream behavior to plan against:
- Backend supports POST /generate.
- Voice creation uses the s2 CLI, not a proven HTTP management API.
- User supplies a consented 5-30 second clean recording.
- User supplies the exact transcript for that recording.
- CLI voice profile creation uses flags like:
  --prompt-audio
  --prompt-text
  --voice
  --save-voice
  --voice-dir
- CLI voice listing uses:
  --list-voices
- Saved voice synthesis uses multipart fields:
  voice
  voice_dir

Required work:
1. Inspect git status, recent commits, docs/ROADMAP.md, TODO.md, CHANGELOG.md, docs/ARCHITECTURE.md, docs/HOME_ASSISTANT_SETUP.md, docs/NEXT_GOAL_PROMPTS.md, docker/s2cpp/entrypoint.sh, unraid/my-s2cpp-backend.xml, and the available pinned s2.cpp documentation/source.
2. Confirm the installed backend image/container has the expected s2 CLI capabilities for --prompt-audio, --prompt-text, --voice, --save-voice, --voice-dir, and --list-voices, using safe read-only commands first.
3. Ask the user for the approved voice profile ID, the path to the consented 5-30 second clean recording, and the exact transcript if they are not already provided.
4. Prefer a one-off command/container execution for voice creation rather than adding another permanent service.
5. Avoid simultaneous backend and one-off model loading on the 10 GB RTX 3080. If the one-off command would load the model separately, stop the backend temporarily to release GPU memory, and restart it after profile creation.
6. Create the .s2voice profile under the persistent /voices directory mounted from /mnt/user/appdata/s2cpp/voices.
7. Verify the resulting .s2voice file exists, has sane permissions, and is visible through the backend's voice-listing command.
8. Restart the backend if it was stopped.
9. Perform a direct backend synthesis test using voice=<profile id> and voice_dir=/voices through POST /generate multipart/form-data.
10. Save any generated verification audio only under verification_artifacts/ or another explicitly temporary/artifact path; do not commit private voice samples or generated voice audio unless the user explicitly asks.
11. Update TODO.md, CHANGELOG.md, docs/ROADMAP.md, docs/HOME_ASSISTANT_SETUP.md, and docs/NEXT_GOAL_PROMPTS.md to reflect Phase 7A results and keep Phase 7B as the next wrapper work.
12. Run focused checks for any docs/templates changed. Run the full Python suite only if application Python files were touched accidentally.
13. Make one focused commit and push it.

Acceptance criteria:
- A user-approved custom .s2voice profile exists in /mnt/user/appdata/s2cpp/voices and /voices.
- The profile is listed by the s2 CLI voice-list command.
- Direct backend synthesis with voice=<profile id> succeeds via multipart POST /generate.
- Backend is running again after any temporary stop.
- Wrapper behavior and image are unchanged.
- Home Assistant settings are unchanged.
- No model files, private source audio, or generated voice audio are committed.
- Documentation accurately states what was verified and what remains for Phase 7B.
- Working tree is clean after commit.
- Commit is pushed to origin/main.

Suggested commit:
docs: verify custom s2voice profile creation

Final report must include:
1. Commit hash.
2. Voice profile ID and verified .s2voice path.
3. Exact command shape used for profile creation, with private paths/transcripts redacted if needed.
4. Direct backend synthesis verification result.
5. Whether the backend was temporarily stopped and restarted.
6. Confirmation that wrapper behavior, images, Home Assistant settings, model files, and runtime Python behavior were not changed.
7. The next ready-to-paste Phase 7B prompt.
```

## Phase 7B prompt — wrapper voice discovery, selection, and Home Assistant exposure

```text
/goal

Proceed with Phase 7B only: wrapper voice discovery, voice selection, default voice configuration, Wyoming Describe exposure, and Home Assistant selection.

Project:
/workspace/wyoming-s2cpp-tts

Current verified deployment:
- Backend: s2cpp-backend at http://s2cpp-backend:3030/generate
- Backend image: ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b or newer explicitly verified immutable tag from Phase 7A
- Wrapper image before this phase: ghcr.io/sorilo/wyoming-s2cpp-tts:sha-89ed2dc
- Docker network: sorilonet
- Home Assistant endpoint: 192.168.1.45:10200
- Backend voice directory inside backend: /voices
- Host voices directory: /mnt/user/appdata/s2cpp/voices
- A custom .s2voice profile should already exist from Phase 7A

Important constraints:
- Do not create voice profiles in Phase 7B; Phase 7A handles profile creation.
- Do not change backend image or model unless explicitly required and approved.
- Do not implement true progressive backend HTTP streaming; that is Phase 7.5.
- Do not implement cancellation or barge-in; those are later phases.

Required work:
1. Inspect git status, recent commits, app/config.py, app/s2_client.py, app/wyoming_server.py, tests, docker/wrapper/Dockerfile, docker/wrapper/entrypoint.sh, unraid/my-wyoming-wrapper.xml, docs/ARCHITECTURE.md, docs/HOME_ASSISTANT_SETUP.md, docs/ROADMAP.md, TODO.md, CHANGELOG.md, and docs/NEXT_GOAL_PROMPTS.md.
2. Add a read-only /voices mount to the wrapper template and image/runtime documentation, or explicitly justify an alternative that still lets the wrapper discover profiles safely.
3. Implement safe enumeration of valid .s2voice files.
4. Sanitize profile IDs and prevent path traversal; reject names containing path separators, parent-directory traversal, unexpected suffixes, or unsafe characters.
5. Add S2_DEFAULT_VOICE environment/config support.
6. Preserve generic s2-pro/default fallback when no voice is configured or requested.
7. Expose selectable voices through Wyoming Describe.
8. Read the requested Wyoming voice selection from Home Assistant/Wyoming events.
9. Pass voice and voice_dir in the multipart request to the backend.
10. Add deterministic tests for voice enumeration, sanitization, Describe exposure, selected voice propagation, default voice config, and fallback behavior.
11. Update wrapper Docker/Unraid docs and templates for the /voices read-only mount and new environment variables.
12. Run focused tests first, then the full Python suite.
13. Build and publish one immutable wrapper image only after tests pass.
14. Deploy the new wrapper image to Unraid and verify Home Assistant can select the custom voice and produce speech.
15. Update TODO.md, CHANGELOG.md, docs/ROADMAP.md, docs/HOME_ASSISTANT_SETUP.md, docs/ARCHITECTURE.md, README.md if needed, and docs/NEXT_GOAL_PROMPTS.md.
16. Make one focused commit and push it.

Acceptance criteria:
- Wrapper sees .s2voice files through a read-only /voices mount or documented safer equivalent.
- Unsafe voice IDs cannot escape the voices directory.
- Wyoming Describe advertises selectable voices.
- Home Assistant displays/selects the custom voice.
- Selected voice and voice_dir are sent in multipart /generate requests.
- S2_DEFAULT_VOICE works and default s2-pro fallback remains available.
- Tests pass, including full Python suite.
- One new immutable wrapper image is published and deployed.
- Working tree is clean after commit and push.

Suggested commit:
feat: expose saved s2 voices through Wyoming
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

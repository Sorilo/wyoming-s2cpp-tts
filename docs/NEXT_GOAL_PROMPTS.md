# Next Hermes `/goal` prompts

Run phases one at a time. This file is regenerated from the actual repository
state after every `/goal` run. Do not copy stale assumptions forward.

## Current state

- 287 tests pass.
- Two-container deployment verified on Unraid:
  - Backend: `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b`
  - Wrapper: `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-89ed2dc`
- Home Assistant preview produces real audible speech.
- Wyoming streaming protocol lifecycle fully implemented.
- Custom voice profiles NOT yet implemented.

## Next immediate prompt: Phase 7 — custom voice profiles

```
/goal

Proceed with Phase 7 only: custom voice profile creation, persistence,
selection, and Home Assistant exposure.

Project:
/workspace/wyoming-s2cpp-tts

Current verified deployment:
- Backend: s2cpp-backend (ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b)
- Wrapper: wyoming-s2cpp-tts (ghcr.io/sorilo/wyoming-s2cpp-tts:sha-89ed2dc)
- Network: sorilonet (wrapper reaches backend at http://s2cpp-backend:3030)
- HA: 192.168.1.233 to 192.168.1.45:10200
- Audio: 44100Hz mono s16le real speech via Wyoming streaming protocol
- Tests: 287/287 pass

Goal:
Enable custom voice profiles so the user can create, persist, select, and
use different TTS voices through Home Assistant.

Scope:

1. Inspect the s2.cpp backend's voice-related API endpoints (e.g. /v1/voices)
   if available. Document what the backend supports.

2. If the backend has a voice creation API:
   - Test creating a voice profile programmatically
   - Persist profiles under /voices (host-mounted from Unraid appdata)
   - Allow the wrapper to reference a voice by name or ID

3. Wire voice selection through the Wyoming wrapper:
   - Accept a voice parameter from config or environment (S2_DEFAULT_VOICE,
     S2_VOICE_DIR)
   - Pass the selected voice in multipart `voice` and `voice_dir` fields
   - Update Wyoming Describe metadata to list available voices

4. Home Assistant integration:
   - The Describe response should list installed voices
   - HA should show voice options in the TTS settings
   - Fall back to default (no-voice) synthesis when no voice is configured

Do not:
- Download voice samples, clone voices, or generate .s2voice files unless
  the backend supports it and user provides reference audio
- Change the backend image or model
- Modify Unraid containers or Home Assistant
- Rebuild or publish Docker images in this run
- Implement disconnect/cancellation, queue policy, or barge-in

Acceptance criteria:
- Voice profiles can be created and persisted under /voices
- Wrapper passes selected voice to backend in multipart fields
- Describe response reflects available voices when configured
- Fall back to default synthesis when no voice is set
- Existing 287 tests still pass; new voice-selection tests added
- Documentation updated for voice profile setup

Commit: one focused commit (feat: custom voice profile support)
```

## Prompt-generation guidance

Every future generated prompt must:
- name the exact next incomplete phase
- include /workspace/wyoming-s2cpp-tts as the project path
- include quota/risk protections
- require inspection of repository areas touched by the phase
- define exact scope, exclusions, acceptance criteria, and tests
- state which claims remain unverified
- require one focused commit
- require status/documentation updates
- require the final response to include the following phase's prompt

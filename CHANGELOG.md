# Changelog

## Unreleased

- Phase 6D: verified Home Assistant deployment end-to-end.
  HA discovers service at 192.168.1.45:10200, s2-pro voice visible,
  preview generates and audibly plays real speech through the streaming
  TTS lifecycle.

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

- Phase 5C: implemented streamed audio to Wyoming events.

- Phase 5B: implemented streaming client interface.

- Phase 5A.2: corrected multipart fields to rodrigomatta/s2.cpp spec.

- Earlier phases: Wyoming TCP server, fake PCM, s2.cpp JSON client,
  container scaffold, CUDA plan. Full history in git log.

# wyoming-s2cpp-tts

`wyoming-s2cpp-tts` is planned as a local Home Assistant Wyoming Protocol TTS service for running Fish Speech S2 Pro through `s2.cpp` GGUF models on a home server.

This repository currently contains a **scaffold only**: architecture notes, install planning docs, configuration placeholders, and minimal starter Python modules. It does **not** yet build `s2.cpp`, download models, expose a working Wyoming server, or synthesize real speech.

## Target hardware for the first real version

- Server: Unraid home server
- GPU target: NVIDIA RTX 3080 10 GB
- CPU: Intel i9-13900K
- RAM: 96 GB DDR4
- Persistent appdata root: `/mnt/user/appdata`

The first model target is:

```text
/models/s2-pro-q6_k.gguf
```

This `q6_k` target is intended as a realistic starting point for a single 10 GB RTX 3080. Future model choices may include `s2-pro-q8_0.gguf` for quality if VRAM allows, or `s2-pro-q4_k_m.gguf` as a lower-VRAM fallback.

## Planned final architecture

```text
Home Assistant Assist pipeline
  -> Wyoming Protocol TCP TTS server on port 10200
  -> Python wrapper / adapter
  -> local s2.cpp HTTP server on port 3030
  -> Fish Speech S2 Pro GGUF model
  -> NVIDIA RTX 3080
```

The Python wrapper is responsible for translating Home Assistant/Wyoming TTS requests into s2.cpp HTTP requests, then returning audio to the Wyoming client. The final design should stream PCM chunks where possible, avoid unnecessary full-audio buffering, and cancel synthesis when the client disconnects.

## Current status

Phase 0 is complete when this scaffold is committed:

- Repository structure exists.
- Docs describe the intended architecture and deployment path.
- Python package skeleton exists.
- Dockerfile and entrypoint are placeholders only.
- Tests are basic placeholders for future behavior.

Implementation comes in small phases. See [`docs/ROADMAP.md`](docs/ROADMAP.md) and [`docs/NEXT_GOAL_PROMPTS.md`](docs/NEXT_GOAL_PROMPTS.md).

## GitHub remote

No remote is required for this scaffold. If this repository does not already have a remote, add one later with:

```bash
git remote add origin git@github.com:<your-user-or-org>/wyoming-s2cpp-tts.git
git push -u origin main
```

Do not force-push, and do not push from automation unless the remote and credentials are confirmed safe.

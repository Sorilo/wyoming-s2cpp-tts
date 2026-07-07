# Next Hermes `/goal` prompts

Run these phases one at a time. Keep each run small. Do **not** start the CUDA/s2.cpp build until the Wyoming server works with fake/test audio, the backend client path is proven, and the container/process structure is in place. Phase 2.75 is complete; the next immediate goal is Phase 3 container structure/supervision placeholders.

## Next immediate prompt: Phase 3

```text
/goal
You are Hermes, acting as a senior Python/Home Assistant Wyoming Protocol engineer.

Project: /workspace/wyoming-s2cpp-tts

Quota protection: Keep this run small. Do not build s2.cpp, do not download models, do not compile CUDA, and do not implement final streaming/cancellation yet.

Goal: Implement Phase 3 only: turn the placeholder Dockerfile and entrypoint into a container/process structure that can run the Python Wyoming wrapper and leave clear supervised-process hooks for a future s2.cpp server.

Requirements:
- Inspect the current Phase 2.75 implementation first.
- Keep fake backend as default and keep all tests passing.
- Do not build, compile, download, or vendor s2.cpp.
- Do not run a Docker build unless it is extremely cheap and clearly safe; prefer static/smoke checks.
- Update Dockerfile and entrypoint.sh to install Python requirements and run `python -m app.main`.
- Add TODO/supervision hooks for future s2.cpp startup on 127.0.0.1:3030.
- Keep Unraid WebUI Add Container compatibility in mind: env vars, ports 10200/8088, paths /models /voices /config.
- Add lightweight tests or script checks for entrypoint behavior where possible without Docker/GPU/model infrastructure.
- Document what the container can and cannot do yet.
- Run the cheapest relevant tests available.
- Make one git commit with a clear message.

Final response: summarize files changed, tests run, git status, and the next recommended prompt.
```

## Phase 4 prompt

```text
/goal
Implement Phase 4 for /workspace/wyoming-s2cpp-tts: research and add the tested CUDA/s2.cpp build path and Unraid NVIDIA runtime instructions. Only build if explicitly safe in this run and no large model downloads are required. Verify GPU visibility with nvidia-smi where available. Keep documentation precise about what was actually tested. Commit the change.
```

## Phase 5 prompt

```text
/goal
Implement Phase 5 for /workspace/wyoming-s2cpp-tts: add streaming TTS plumbing from s2.cpp HTTP output to Wyoming audio chunks. Focus on low time-to-first-audio, WAV-header handling if needed, chunk sizing, and measurement hooks. Use mocks if the real backend is unavailable. Commit the change.
```

## Phase 6 prompt

```text
/goal
Implement Phase 6 for /workspace/wyoming-s2cpp-tts: add cancellation and barge-in-friendly behavior. Handle Wyoming client disconnects, backend cancellation/timeouts when possible, queue cleanup, and tests for cancelled requests. Document Home Assistant/satellite limits. Commit the change.
```

## Phase 7 prompt

```text
/goal
Implement Phase 7 for /workspace/wyoming-s2cpp-tts: harden tests, troubleshooting docs, release notes, and v0.1 tagging criteria. Do not tag a release unless the working behavior has been verified. Commit the change.
```

## Phase 8 prompt

```text
/goal
Plan Phase 8 for /workspace/wyoming-s2cpp-tts: future hardware upgrades, multi-worker scheduling, multi-model profiles, q8/q4 fallback policy, and benchmarking methodology. Prefer docs and tests over implementation unless the v0.1 baseline is already stable. Commit any doc updates.
```

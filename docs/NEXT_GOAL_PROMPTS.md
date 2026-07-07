# Next Hermes `/goal` prompts

Run these phases one at a time. Keep each run small. Do **not** start the CUDA/s2.cpp build until the Wyoming server works with fake/test audio and the backend client path is proven. Phase 2 is complete at the backend-client level; the next immediate goal is a small Phase 2.5 integration step.

## Next immediate prompt: Phase 2.5

```text
/goal
You are Hermes, acting as a senior Python/Home Assistant Wyoming Protocol engineer.

Project: /workspace/wyoming-s2cpp-tts

Quota protection: Keep this run small. Do not build s2.cpp, do not download models, do not run Docker builds, and do not implement final streaming/cancellation yet.

Goal: Implement Phase 2.5 only: wire the tested s2.cpp HTTP client into the Wyoming TTS path as an opt-in non-streaming backend mode, while keeping the Phase 1 fake PCM mode as the default fallback/test mode.

Requirements:
- Inspect the existing Phase 1 Wyoming server and Phase 2 s2_client implementation first.
- Keep Home Assistant/Wyoming protocol behavior in app/wyoming_server.py.
- Keep backend HTTP details in app/s2_client.py.
- Add a small config switch such as TTS_BACKEND=fake|s2cpp, defaulting to fake.
- When TTS_BACKEND=fake, all existing fake Wyoming tests must keep passing.
- When TTS_BACKEND=s2cpp, convert one buffered s2.cpp client result into Wyoming AudioStart/AudioChunk/AudioStop events.
- Use mocked s2.cpp responses in tests; do not require a real backend.
- Do not implement progressive streaming, cancellation, Docker packaging, CUDA builds, or model downloads.
- Document the backend mode switch and limitations clearly.
- Run the cheapest relevant tests available.
- Make one git commit with a clear message.

Final response: summarize files changed, tests run, git status, and the next recommended prompt.
```

## Phase 2 prompt

```text
/goal
Implement Phase 2 for /workspace/wyoming-s2cpp-tts: connect the Python wrapper to an already-running s2.cpp HTTP /generate endpoint. Keep it backend-client focused. Do not build s2.cpp, download models, or change Docker packaging. Add tests with mocked HTTP responses and document how to point S2_HOST/S2_PORT at an external test server. Commit the change.
```

## Phase 3 prompt

```text
/goal
Implement Phase 3 for /workspace/wyoming-s2cpp-tts: turn the scaffold Dockerfile and entrypoint into a container structure that can supervise the Python Wyoming wrapper and a future s2.cpp server process. Do not compile CUDA or download models. Add clear TODOs where the real s2.cpp binary will be copied/built later. Add a lightweight container smoke-test if possible without building GPU code. Commit the change.
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

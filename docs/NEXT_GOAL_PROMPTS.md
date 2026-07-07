# Next Hermes `/goal` prompts

Run these phases one at a time. Keep each run small. Do **not** start the CUDA/s2.cpp build until the Wyoming server works with fake/test audio, the backend client path is proven, and an external backend smoke path is documented. Phase 2.5 is complete; the next immediate goal is an optional external s2.cpp smoke-test step if an already-running backend is available.

## Next immediate prompt: Phase 2.75

```text
/goal
You are Hermes, acting as a senior Python/Home Assistant Wyoming Protocol engineer.

Project: /workspace/wyoming-s2cpp-tts

Quota protection: Keep this run small. Do not build s2.cpp, do not download models, do not run Docker builds, and do not implement final streaming/cancellation yet.

Goal: Implement Phase 2.75 only: add an optional direct smoke-test path for an already-running external s2.cpp HTTP /generate endpoint, without requiring that backend for normal tests or CI.

Requirements:
- Inspect the current Phase 2.5 implementation first.
- Keep fake backend as the default and keep all mocked tests passing.
- Do not start, build, compile, package, or supervise s2.cpp.
- Do not download GGUF models.
- Add a small script or documented command that uses Settings.from_env(), S2_HOST, S2_PORT, and TTS_BACKEND=s2cpp to send one direct /generate request when a backend is already available.
- The smoke path must be opt-in and skipped/harmless when no backend is available.
- Add tests for any new parsing/helper code using mocks only.
- Document expected inputs, expected output, and limitations.
- Run the cheapest relevant tests available.
- Make one git commit with a clear message.

Final response: summarize files changed, tests run, git status, and the next recommended prompt.
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

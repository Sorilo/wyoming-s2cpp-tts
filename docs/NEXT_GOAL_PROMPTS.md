# Next Hermes `/goal` prompts

Run these phases one at a time. Keep each run small. Phase 4 is complete as a documentation/static-validation phase; the next immediate goal is Phase 5 streaming plumbing with mocks unless a real backend is already available.

## Next immediate prompt: Phase 5

```text
/goal
You are Hermes, acting as a senior Python/Home Assistant Wyoming Protocol engineer.

Project: /workspace/wyoming-s2cpp-tts

Quota protection: Keep this run small. Do not download GGUF models. Do not perform CUDA builds. Do not run Docker builds unless explicitly cheap and safe. Use mocked backend responses if no real s2.cpp server is available.

Goal: Implement Phase 5 only: add streaming TTS plumbing from s2.cpp HTTP output to Wyoming audio chunks, focusing on low time-to-first-audio and measurement hooks.

Requirements:
- Inspect the current Phase 4 docs, s2_client, and wyoming_server implementation first.
- Keep `TTS_BACKEND=fake` as default and keep all tests passing.
- Do not build/vendor s2.cpp or download models.
- Do not claim real streaming backend success unless actually tested against a running backend.
- Add/adjust client interfaces so mocked chunked backend audio can be converted progressively into Wyoming `AudioStart`/`AudioChunk`/`AudioStop`.
- Include WAV-header handling if the mocked backend uses WAV; otherwise clearly document raw PCM assumptions.
- Add lightweight timing/measurement hooks for time-to-first-audio and bytes/chunks emitted.
- Preserve existing buffered `s2cpp` mode or migrate it safely with tests.
- Use tests with mocked streaming responses; do not require GPU/model infrastructure.
- Run the cheapest relevant tests/static checks available.
- Make one git commit with a clear message.

Final response: summarize files changed, tests run, git status, and the next recommended prompt.
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

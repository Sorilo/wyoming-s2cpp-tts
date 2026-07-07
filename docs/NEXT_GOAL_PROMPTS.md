# Next Hermes `/goal` prompts

Run these phases one at a time. Keep each run small. Phase 3 is complete; the next immediate goal is Phase 4: document and, only if clearly safe, lightly validate the CUDA/s2.cpp build and Unraid GPU runtime path. Do not download large models.

## Next immediate prompt: Phase 4

```text
/goal
You are Hermes, acting as a senior local-AI/Unraid GPU container engineer.

Project: /workspace/wyoming-s2cpp-tts

Quota protection: Keep this run small. Do not download GGUF models. Do not perform long CUDA builds. Do not run Docker builds unless they are explicitly cheap and safe. Prefer documentation, scripts, and static checks over heavy build work.

Goal: Implement Phase 4 only: research and add a precise CUDA/s2.cpp build and Unraid NVIDIA runtime plan for this repo, with lightweight validation hooks where possible.

Requirements:
- Inspect the current Phase 3 Dockerfile/entrypoint and docs first.
- Keep fake backend as default and keep all tests passing.
- Do not vendor s2.cpp or download models.
- Do not claim CUDA/s2.cpp build success unless actually tested.
- Add docs for the intended future CUDA base image/build path and Unraid NVIDIA runtime settings.
- Add a lightweight script or checklist for verifying GPU visibility (`nvidia-smi`) inside a future container.
- Add placeholders/TODOs in Dockerfile or docs for where the s2.cpp binary/build stage will go.
- Keep `/models`, `/voices`, `/config`, `10200`, `8088`, and internal `3030` conventions intact.
- Run the cheapest relevant tests/static checks available.
- Make one git commit with a clear message.

Final response: summarize files changed, tests run, git status, and the next recommended prompt.
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

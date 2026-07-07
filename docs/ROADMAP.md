# Roadmap

## Phase 0: scaffold and docs

Create this repository scaffold, architecture docs, roadmap, Docker placeholders, Python package skeleton, minimal tests, and a first git commit.

## Phase 1: minimal Wyoming server with fake PCM/test audio

Implemented. The service can run a minimal Wyoming TCP fake TTS server that handles `Describe` and `Synthesize`, returning deterministic local PCM test-tone audio without touching s2.cpp or model inference.

## Phase 2: connect wrapper to existing s2.cpp HTTP `/generate`

Implemented at the backend-client level. `app/s2_client.py` can POST JSON to an already-running external `/generate` endpoint and return raw audio bytes. The Wyoming server still uses fake PCM; routing backend audio through Wyoming is deferred to Phase 2.5.

## Phase 2.5: opt-in non-streaming s2.cpp backend mode

Wire the tested s2.cpp client into the Wyoming `Synthesize` path behind a config switch, while keeping fake PCM as the default fallback/test mode. Convert one buffered backend response into Wyoming audio events. Do not implement final progressive streaming or cancellation yet.

## Phase 3: Docker container with s2.cpp supervised process

Package the Python wrapper and s2.cpp server startup into one container process model. Add process supervision and clean shutdown behavior.

## Phase 4: CUDA/s2.cpp build and Unraid GPU support

Add the CUDA-capable s2.cpp build path, NVIDIA runtime notes, and Unraid GPU setup validation. Verify with `nvidia-smi` and a tiny synthesis test.

## Phase 5: streaming TTS and low time-to-first-audio

Implement progressive audio streaming from s2.cpp to Wyoming. Measure time-to-first-audio, realtime factor, and chunk behavior.

## Phase 6: cancellation/barge-in-friendly behavior

Handle client disconnects, synthesis cancellation, timeouts, queue cancellation policy, and cleanup of partial responses.

## Phase 7: tests, troubleshooting, and release tagging

Add protocol tests, queue tests, cancellation tests, integration notes, troubleshooting docs, and tag a first release.

## Phase 8: future hardware upgrades and multi-worker/multi-model support

Explore higher-quality quantizations, multiple model profiles, additional GPUs, multi-worker scheduling, and routing policies after the single-GPU baseline is stable.

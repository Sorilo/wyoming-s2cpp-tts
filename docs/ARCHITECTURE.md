# Architecture

## Goal

Build a Home Assistant-compatible Wyoming Protocol TTS service that uses Fish Speech S2 Pro through `s2.cpp` GGUF models, eventually packaged as an Unraid WebUI-friendly Docker container.

## Final service flow

```text
Home Assistant Assist pipeline
  -> Wyoming Protocol TCP connection on 10200
  -> Python Wyoming adapter
  -> local s2.cpp HTTP server on 3030
  -> Fish Speech S2 Pro GGUF model under /models
  -> RTX 3080 GPU
  -> streamed PCM audio back to Home Assistant
```

## Home Assistant / Wyoming role

Home Assistant should connect to this service using the Wyoming Protocol integration. Home Assistant should not need to know about Fish Speech, GGUF files, CUDA, or the internal s2.cpp HTTP API.

The planned public service endpoint is `tcp://0.0.0.0:10200`.

## Python wrapper role

The Python wrapper will eventually:

- Listen for Wyoming TTS requests.
- Validate and normalize requested text/voice options.
- Enqueue synthesis work.
- Forward requests to the local s2.cpp HTTP server.
- Convert returned audio into Wyoming-compatible audio events.
- Stream raw PCM chunks as soon as practical.
- Cancel backend work when the Wyoming client disconnects, where the backend supports cancellation.
- Expose a small health/debug HTTP endpoint on port `8088`.

## s2.cpp role

`s2.cpp` is planned as the model runtime. It should run in HTTP server mode inside the same container or supervised process group, listening internally on `127.0.0.1:3030`.

Port `3030` should not be exposed by default in Unraid unless debug mode is intentionally enabled.

## Model and voice directories

Planned container paths:

- `/models` for GGUF model files, starting with `s2-pro-q6_k.gguf`.
- `/voices` for reference voices or voice metadata.
- `/config` for service configuration and future profiles.

Planned Unraid host mappings:

- `/mnt/user/appdata/wyoming-s2cpp-tts/models -> /models`
- `/mnt/user/appdata/wyoming-s2cpp-tts/voices -> /voices`
- `/mnt/user/appdata/wyoming-s2cpp-tts/config -> /config`

## Queue and worker model

The first real implementation should treat `s2.cpp` as **single-active-synthesis**:

- One active synthesis worker.
- Bounded queue, default max size `3`.
- Clear busy/timeout behavior.
- Avoid concurrent GPU-heavy requests until benchmarking proves safe.

This keeps RTX 3080 VRAM usage predictable and avoids duplicated model loads.

## Low-latency streaming design

The final design should optimize for low time-to-first-audio:

- Segment sentences where possible.
- Request streaming/chunked output from s2.cpp when available.
- Strip or transform WAV headers if the backend streams WAV, because Wyoming audio chunks should be raw PCM once audio has started.
- Emit `AudioStart`, repeated audio chunks, and `AudioStop` rather than waiting for a full file.
- Keep a small configurable startup buffer for stability.

Initial placeholder buffer knobs currently exist in configuration:

- `S2_STREAM_START_BUFFER_MS=1000`
- `S2_STREAM_START_BUFFER_MS_STABLE=4000`

These values are configurable placeholders, not benchmarked production defaults. They must remain benchmark-driven and should not be treated as validated until real streaming and latency measurements exist.

## Latency measurement ownership

The aspirational end-to-end target is under 2 seconds from detected end-of-speech through first audible playback for short, warm-path requests, including VAD endpointing.

This repository can directly measure:

- TTS request receipt / `tts_request_start_at`
- backend first byte / `tts_first_backend_byte_at`
- first Wyoming audio chunk / `wyoming_first_audio_chunk_at`
- emitted bytes and chunk count
- cancellation and cleanup timing
- request or stream duration

The following require external instrumentation or a correlated end-to-end harness:

- `stt_done_at`
- `llm_request_start_at`
- `llm_first_token_at`
- `llm_first_sentence_at`
- `ha_first_playback_at`

`ha_first_playback_at` may require satellite, player, Home Assistant, or client-side instrumentation and may not always be precisely measurable.

## Cancellation and barge-in-friendly behavior

The service should be barge-in friendly, but TTS alone cannot guarantee true barge-in. The final stack also depends on Home Assistant Assist, wake word, VAD, satellite behavior, and playback device interruption support.

Planned TTS-side behavior:

- Stream chunks as soon as possible.
- Detect Wyoming client disconnects.
- Cancel synthesis on disconnect when possible.
- Apply synthesis timeouts.
- Avoid full-audio buffering when streaming is available.
- Optionally decide later whether a new request should cancel the previous request.

Planned default:

```text
BARGE_IN_FRIENDLY=true
CANCEL_ON_CLIENT_DISCONNECT=true
CANCEL_ON_NEW_REQUEST=false
```

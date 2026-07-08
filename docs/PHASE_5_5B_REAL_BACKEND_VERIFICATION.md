# Phase 5.5B Real Backend Verification

Phase 5.5B was verified against an already-running real `rodrigomatta/s2.cpp` HTTP backend. The repository did not start, rebuild, or modify the backend container during this verification.

## Backend under test

| Field | Value |
|---|---|
| Container | `s2cpp-backend` |
| Docker network | `sorilonet` |
| Internal endpoint | `s2cpp-backend:3030` |
| Host debug endpoint | `192.168.1.45:3032` |
| Image | `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b` |
| GPU | RTX 3080 |
| Model | `/models/s2-pro-q6_k.gguf` |
| Tokenizer | `/models/tokenizer.json` |

Backend logs confirmed CUDA device detection, all 36 model layers loaded on CUDA, CUDA codec selection, and HTTP server listening on `0.0.0.0:3030`.

## Pre-checks

- DNS: `s2cpp-backend` resolved successfully.
- TCP: `s2cpp-backend:3030` accepted connections.
- HTTP: `GET /` returned `HTTP/1.1 404 Not Found`, confirming the listener was reachable.

## Verification command

```bash
.venv/bin/python scripts/smoke_s2cpp_generate.py \
  --run-real \
  --require-backend \
  --endpoint s2cpp-backend:3030 \
  --json
```

## Verified result

The smoke harness returned:

```text
phase_5_5b_status = real_backend_verified
warnings = []
```

### Buffered multipart contract

The real backend returned validated raw PCM for the repository's default buffered request shape:

| Field | Value |
|---|---|
| HTTP status | `200` |
| Content-Type | `audio/L16; rate=44100; channels=1` |
| Buffered audio format | `pcm_s16le` |
| WAV header valid | `false` |
| Buffered audio valid | `true` |
| Buffered PCM frame-aligned | `true` |
| Sample rate | `44100` |
| Channels | `1` |

This is accepted only because the response is explicitly declared PCM, sample-rate/channel metadata is present and non-contradictory, and the byte length is frame-aligned. Declared `audio/wav` responses still require a valid RIFF/WAVE header.

Observed run values:

| Field | Value |
|---|---|
| Response bytes | `376832` |
| Audio duration | `4.272471655328798` seconds |
| Request duration | `4345.311162000144` ms |

### Streaming multipart contract

| Field | Value |
|---|---|
| HTTP status | `200` |
| Content-Type | `audio/L16; rate=44100; channels=1` |
| Encoding | `pcm_s16le` |
| Sample rate | `44100` |
| Channels | `1` |
| Missing audio headers | `[]` |
| Total PCM bytes | `330586` |
| PCM frame-aligned | `true` |
| Non-empty transport reads | `81` |
| Progressive classification | `verified_progressive` |
| Time to first audio | `3850.1823269998567` ms |
| Total generation time | `3850.891481999497` ms |

## Limitations

- Direct backend-client smoke verification only; not Home Assistant/Wyoming end-to-end playback.
- Single-run timings are diagnostic only and are not benchmark claims.
- Audio quality, voice cloning quality, VRAM headroom, cancellation, and barge-in behavior remain future-phase work.
- Generated audio artifacts are not committed.

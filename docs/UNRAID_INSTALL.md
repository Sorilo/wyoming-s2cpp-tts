# Unraid install notes

The verified deployment uses two Docker containers on the Unraid `sorilonet` network:

- `s2cpp-backend` — CUDA s2.cpp HTTP backend, image `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b`
- `wyoming-s2cpp-tts` — CPU-only Wyoming wrapper, image `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-89ed2dc`

Home Assistant connects to the wrapper at `192.168.1.45:10200`. The wrapper reaches the backend at `http://s2cpp-backend:3030/generate` over `sorilonet`.

## Backend template

Use `unraid/my-s2cpp-backend.xml` for the CUDA backend.

Important settings:

| Setting | Verified value |
| --- | --- |
| Repository | `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b` |
| Container name | `s2cpp-backend` |
| Network | custom Docker network (`sorilonet`) |
| Internal HTTP port | `3030` |
| Host debug port | `3031` by default, because host `3030` is already occupied on this server |
| Model mount | `/mnt/user/appdata/s2cpp/models` → `/models` read-only |
| Voice mount | `/mnt/user/appdata/s2cpp/voices` → `/voices` read-write |
| Model path | `/models/s2-pro-q6_k.gguf` |
| Voice dir | `/voices` |
| GPU runtime | NVIDIA runtime / `--runtime=nvidia` |

The backend `POST /generate` endpoint expects `multipart/form-data`; do not configure the wrapper to send JSON to the deployed backend.

## Wrapper template

Use `unraid/my-wyoming-wrapper.xml` for the CPU-only Wyoming wrapper.

Important settings:

| Setting | Verified value |
| --- | --- |
| Repository | `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-89ed2dc` |
| Container name | `wyoming-s2cpp-tts` |
| Network | same custom Docker network as backend (`sorilonet`) |
| Host Wyoming port | `10200` |
| `TTS_BACKEND` | `s2cpp` |
| `S2_HOST` | `s2cpp-backend` |
| `S2_PORT` | `3030` |
| `S2_STREAM` | parsed/configured; see streaming caveat below |

The wrapper does not need NVIDIA runtime, CUDA, GGUF model files, or GPU access.

## Streaming caveat

Wyoming protocol streaming is implemented and verified: the wrapper handles `synthesize-start`, `synthesize-chunk`, and `synthesize-stop`, then emits `AudioStart`, `AudioChunk`, `AudioStop`, and `synthesize-stopped` for Home Assistant.

Progressive backend-audio streaming is not currently used by the production handler: although `S2_STREAM` is parsed and `synthesize_s2cpp_streaming_tts_events()` / `generate_stream()` exist, the live handler still calls buffered `synthesize_s2cpp_tts_events()` via `generate_multipart()`, then sends Wyoming audio events.

## Home Assistant setup

1. In Home Assistant, add the **Wyoming Protocol** integration.
2. Host: `192.168.1.45`
3. Port: `10200`
4. Select `wyoming-s2cpp-tts` / `s2-pro` as the TTS engine.
5. Test with "Try text-to-speech".

Verified behavior as of 2026-07-08: Home Assistant discovers the service, `s2-pro` is visible, and preview TTS audibly plays real speech.

## Voice profiles

Saved `.s2voice` files belong under `/mnt/user/appdata/s2cpp/voices` on the host and `/voices` inside the backend. Phase 7A creates and directly verifies a profile. Phase 7B adds wrapper read-only voice discovery and Home Assistant selectable voices.

Do not assume a backend HTTP voice-management endpoint. Plan against CLI profile creation/listing and `/generate` voice selection unless source inspection proves otherwise.

## Finalization status

Final restart, update, persistence, backup, and rollback validation for the Unraid templates is planned for Phase 14. Until then, prefer immutable `sha-*` image tags for every verified deployment.

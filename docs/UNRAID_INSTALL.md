# Unraid install notes — v0.1.0

The v0.1.0 deployment uses two Docker containers linked through a shared Docker bridge whose backend port is not host-published.

For the generic Docker Compose setup (recommended for v0.1.0), see `compose.yaml` and `docs/INSTALL.md`.
These Unraid notes complement the Compose approach for users who prefer Unraid'''s Docker UI templates.

> **Note**: The `unraid/*.xml` templates in this repository are **historical reference only**. The authoritative v0.1.0 deployment method is `docker compose` with `compose.yaml` and `.env.example`.

## Architecture

- **Wrapper container** (`wyoming-s2cpp-tts`): CPU-only Wyoming Protocol TCP server.
- **Backend container** (`s2cpp-backend`): CUDA s2.cpp HTTP inference server.
- **Network**: shared Docker bridge (`s2cpp-net` or your custom `NETWORK_NAME`). The backend HTTP port (3030) is **not published to the host** — only the wrapper Wyoming port (10200) is exposed.
- Home Assistant connects to the wrapper at `<your-docker-host>:10200`.

## Compose-based setup (recommended)

```bash
# 1. Copy and edit environment
cp .env.example .env
# Edit .env to set your host paths, network name, and ports

# 2. Start both containers
docker compose up -d

# 3. Verify
docker compose ps
docker compose logs -f
```

The `compose.yaml` configures:

| Component | Image | Port | Host exposure |
|-----------|-------|------|---------------|
| s2cpp-backend | `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:0.1.0` | 3030 | **None** (private network only) |
| wyoming-s2cpp-tts | `ghcr.io/sorilo/wyoming-s2cpp-tts:0.1.0` | 10200 | `<host>:10200` |

## Important settings

### Backend

| Setting | Default / suggested value |
|---------|--------------------------|
| Image | `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:0.1.0` (pin to `sha-*` for production) |
| Container name | `s2cpp-backend` |
| Network | `s2cpp-net` (shared bridge; backend port unpublished) |
| Internal HTTP port | `3030` (not published) |
| Model mount | `<your-models-dir>` → `/models` (read-only) |
| Voice mount | `<your-voices-dir>` → `/voices` (read-write for backend) |
| Model path | `/models/s2-pro-q4_k_m.gguf` (or your chosen quant) |
| GPU runtime | NVIDIA runtime / `--runtime=nvidia` |
| RTX 3080 baseline | Q4_K_M, context=32, stride=32, threads=8 |

The backend `POST /generate` endpoint expects `multipart/form-data`. Do not send JSON to the deployed backend.

### Wrapper

| Setting | Default / suggested value |
|---------|--------------------------|
| Image | `ghcr.io/sorilo/wyoming-s2cpp-tts:0.1.0` (pin to `sha-*` for production) |
| Container name | `wyoming-s2cpp-tts` |
| Network | Same shared bridge as backend (`s2cpp-net`) |
| Host Wyoming port | `10200` |
| `TTS_BACKEND` | `s2cpp` |
| `S2_HOST` | `s2cpp-backend` |
| `S2_PORT` | `3030` |
| `S2_STREAM` | `true` (progressive streaming enabled) |

The wrapper does **not** need NVIDIA runtime, CUDA, GGUF model files, or GPU access.

## Streaming and cancellation status

- Wyoming protocol streaming is implemented and verified: `synthesize-start` → `synthesize-chunk` → `synthesize-stop` → `AudioStart` → `AudioChunk` → `AudioStop` → `synthesize-stopped`.
- Progressive backend-audio streaming is enabled when `S2_STREAM=true`.
- Backend cancellation (Phase 8B2) is production-promoted: client disconnects abort synthesis and release backend busy state.
- **Stock Home Assistant 2026.7.2 + Voice PE firmware 26.6.0 does NOT pass full one-wake barge-in.** Generic `media_player.media_stop` does not cancel the TTS producer in the Assist announcement pipeline. See `docs/validation/PHASE_10_CLOSURE.md`.

## Backup and rollback

Before upgrading, back up your data directories:

```bash
cp -a /mnt/user/appdata/s2cpp/voices /mnt/user/appdata/s2cpp/voices.bak
cp -a /mnt/user/appdata/s2cpp/models /mnt/user/appdata/s2cpp/models.bak
```

For full upgrade and rollback procedures, see `docs/UPGRADE_ROLLBACK.md`.

## Voice profiles

Saved `.s2voice` files belong under `<voices-dir>` on the host and `/voices` inside the backend. Phase 7A created six CMU ARCTIC profiles. Phase 7B added wrapper read-only voice discovery and Home Assistant selectable voices. Drop-in discovery: new `.s2voice` files are discoverable without container rebuild or restart.

Do not assume a backend HTTP voice-management endpoint. Plan against CLI profile creation/listing and `/generate` voice selection.

## Home Assistant setup

1. In Home Assistant, add the **Wyoming Protocol** integration.
2. Host: `<your-docker-host-ip>`
3. Port: `10200`
4. Select `wyoming-s2cpp-tts` / `s2-pro` as the TTS engine.
5. Test with "Try text-to-speech".

See `docs/HOME_ASSISTANT_SETUP.md` for detailed setup and known limitations.

## Finalization status

Final restart, update, persistence, backup, and rollback validation for Unraid templates is planned for Phase 14. Until then, prefer immutable `sha-*` image tags for every verified deployment and use the `compose.yaml` workflow.

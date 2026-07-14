# s2cpp Backend — Docker Image & Unraid Deployment

Phase 5.5B0: minimal reproducible CUDA `rodrigomatta/s2.cpp` HTTP backend image for early testing. Phase 5.5B real backend verification has since passed against the deployed `s2cpp-backend` container.

## What this is

A standalone Docker image that builds and runs the `rodrigomatta/s2.cpp` inference engine with NVIDIA CUDA GPU acceleration. It exposes the `POST /generate` HTTP endpoint but does **not** include the Python Wyoming wrapper, GGUF models, tokenizer assets, reference audio, voices, secrets, or generated audio.

This backend image has been deployed and verified for the current Home Assistant TTS baseline when pinned to `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd`. The Phase 8B2 production image digest is `sha256:c29e41e59b470d58bf4b88c11c9ec753e00fa74a3bffbb003bc257fb9c6e46d9` and rollback remains `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b`. It is still pre-v0.1 and not fully release-hardened; see `../../docs/PHASE_5_5B_REAL_BACKEND_VERIFICATION.md` and `../../docs/ARCHITECTURE.md`.

## Image details

| Detail | Value |
|---|---|
| Upstream repo | `rodrigomatta/s2.cpp` |
| Pinned revision | `2c33261938da1a41d713768b1b391b4d368d7d2c` |
| CUDA base | `nvidia/cuda:12.4.1-runtime-ubuntu22.04` |
| Build base | `nvidia/cuda:12.4.1-devel-ubuntu22.04` |
| Internal server port | `3030` |
| Build platform | `linux/amd64` |
| GitHub Container Registry | `ghcr.io/<owner>/wyoming-s2cpp-tts-backend` |

## Published tags

| Tag | Description |
|---|---|
| `edge` | Latest push to main / manual dispatch (rolling) |
| `sha-<short>` | Immutable per-commit image (pin for rollback) |
| `v*` | Optional release tag (future) |

## Required runtime assets (mounted from host)

```
/models/
  s2-pro-q6_k.gguf      # GGUF model (q6_k recommended for RTX 3080 10 GB)
  tokenizer.json         # Qwen3 BPE tokenizer

/voices/                 # Saved .s2voice profiles (optional)
/config/                 # Reserved for future config (optional)
```

GGUF model files are available at: https://huggingface.co/rodrigomt/s2-pro-gguf

## Offline voice import

This image also contains `/usr/local/bin/import-s2voice`, FFmpeg, Python, and
the narrow parser/schema modules required to create a profile from authorized
local audio. It is an operator CLI, not a network endpoint, and does not alter
the normal `/entrypoint.sh` server startup. Real import refuses while an
`s2 --server` process is active and never stops or restarts that process.

Use a separate one-shot container with `--entrypoint
/usr/local/bin/import-s2voice`, preferably `--network none`, the same model and
read-write voices mounts, and an explicit read-only import-input mount. Start
with `--dry-run`; add GPU access only for the real import. See
[`../../docs/VOICE_PROFILES.md`](../../docs/VOICE_PROFILES.md) for the complete
rights, privacy, command, backup, and rollback procedure.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `S2_MODEL` | `/models/s2-pro-q6_k.gguf` | Path to GGUF model file (required) |
| `S2_TOKENIZER` | *auto-detect* | Path to tokenizer.json (auto-detected next to model) |
| `S2_HOST` | `0.0.0.0` | Server bind address |
| `S2_PORT` | `3030` | Internal HTTP server port |
| `S2_GPU_LAYERS` | `-1` | GPU layer offload (-1 = all 36, 0 = CPU only) |
| `S2_CUDA_DEVICE` | `0` | CUDA device index |
| `S2_THREADS` | `0` | CPU threads (0 = auto) |
| `S2_CODEC_CPU` | `false` | Keep audio codec on CPU |
| `S2_VOICE_DIR` | `/voices` | Saved voice profiles directory |
| `S2_LOG_LEVEL` | `info` | Runtime verbosity: error, warn, info, debug |
| `S2_EXTRA_ARGS` | *empty* | Additional CLI flags passed to s2 binary |

## GitHub Actions: Build & Publish

### Triggering a build

1. Push the repository to GitHub.
2. Go to **Actions** → **Publish s2cpp Backend** → **Run workflow**.
3. Optionally enter a release tag (e.g., `v0.1.0-alpha`). Leave blank for edge + SHA tags only.

The workflow:
- Does **not** require a GPU on the build runner.
- Does **not** run real synthesis smoke tests.
- Uses `GITHUB_TOKEN` (no committed secrets).
- Publishes `edge`, `sha-<short>`, and optionally a release tag.
- Uses BuildKit with GitHub Actions layer caching.
- Publishes to `ghcr.io/<owner>/wyoming-s2cpp-tts-backend`.

### Pushing a release tag

```bash
git tag v0.1.0-alpha
git push origin v0.1.0-alpha
```

Pushing a tag matching `v*` also triggers the workflow. The tag image is published alongside `edge` and `sha-<short>`.

## Unraid Deployment

### Prerequisites

1. Unraid NVIDIA plugin installed and working.
2. GPU visible to the host (`nvidia-smi`).
3. GGUF model and tokenizer files downloaded to a host directory (e.g., `/mnt/user/appdata/s2cpp/models/`).

### Install the template

```bash
cp unraid/my-s2cpp-backend.xml /boot/config/plugins/dockerMan/templates-user/
```

Then in the Unraid WebUI: **Docker** → **Add Container** → Select **s2cpp-backend** from the dropdown.

### Public vs private GHCR pulling

- **Public repository**: The image is pullable without authentication.
- **Private repository**: You must create a GitHub personal access token with `read:packages` scope, then log in on Unraid:
  ```bash
  docker login ghcr.io -u YOUR_GITHUB_USER
  ```

### Configure the container

Edit the template fields after adding:

1. **Network**: Choose the verified custom Docker network (`sorilonet`) so containers can reach each other by name.
2. **GGUF Model Directory**: Host path containing the `.gguf` file and `tokenizer.json` (read-only mount recommended).
3. **Voice Profiles Directory**: Host path for `.s2voice` files (read-write so voices can be saved).
4. **GPU**: Set `NVIDIA_VISIBLE_DEVICES` to a GPU UUID or leave empty for all GPUs.
5. **Host Port**: Default `3031` (host port 3030 is already occupied on this server). Same-network clients use the **internal** port `3030` via container name.

### Verify the backend is running

```bash
# Check logs
docker logs s2cpp-backend

# Expected startup output:
# ========================================
#  s2.cpp backend starting
# ========================================
#  model          = /models/s2-pro-q6_k.gguf
#  tokenizer      = /models/tokenizer.json
#  listen         = 0.0.0.0:3030
#  ...

# Test the /generate endpoint from the Unraid host
curl -X POST http://localhost:3031/generate   --form "text=Hello world"   --form 'params={"max_new_tokens":64}'   -o test.wav

# Or from another container on the same network:
curl -X POST http://s2cpp-backend:3030/generate   --form "text=Hello world"   --form 'params={"max_new_tokens":64}'   -o test.wav
```

### Hermes Suite / Wyoming wrapper configuration

When the Wyoming wrapper container is on the same Docker network, configure it to reach the backend by container name:

```
S2_HOST=s2cpp-backend
S2_PORT=3030
```

**Do not** use the container IP (it changes on restart). Use the container name.

### Rolling back

Use an immutable SHA tag to pin to a specific build:

1. Find the SHA tag in the GitHub Container Registry.
2. Change the `Repository` field in the Unraid template to:
   ```
   ghcr.io/YOUR_GITHUB_USER/wyoming-s2cpp-tts-backend:sha-abc1234
   ```
3. Re-pull and restart the container.

## What remains unverified

Phase 5.5B real backend smoke verification passed on Unraid with CUDA using
`ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b`, RTX 3080,
`/models/s2-pro-q6_k.gguf`, and `/models/tokenizer.json`. Later phases verified
Home Assistant discovery, audible real-speech playback, voice selection,
progressive backend streaming, and Phase 8B2 backend cancellation. The current
production backend pin is `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd`.

Still unverified:

- Subjective synthesis quality, broader VRAM headroom/realtime-factor envelopes,
  queue timeout/busy policy, and end-to-end barge-in behavior.

## Files in this phase

```
docker/s2cpp/
  Dockerfile.cuda      # Multi-stage CUDA build
  entrypoint.sh        # Startup validation + server launch
  README.md            # This documentation

.github/workflows/
  publish-s2cpp-backend.yml  # GHCR build/publish workflow

unraid/
  my-s2cpp-backend.xml       # Unraid Add Container template
```

## Phase 5.5B verification

Phase 5.5B has passed against the deployed `s2cpp-backend` container on the
`sorilonet` Docker network. The exact command was:

```bash
.venv/bin/python scripts/smoke_s2cpp_generate.py \
  --run-real \
  --require-backend \
  --endpoint s2cpp-backend:3030 \
  --json
```

Result: `phase_5_5b_status = real_backend_verified`, no warnings.

---

*Phase 5.5B0 packaged the backend image; Phase 5.5B verified real backend HTTP compatibility.*

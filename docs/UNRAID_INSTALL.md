# Unraid install draft

This is a draft for a future published Docker image. As of Phase 3, the container structure can run the Python Wyoming wrapper with the default fake backend, but it still does **not** build/start s2.cpp, download models, or provide final GPU TTS.

## Planned Unraid WebUI Add Container settings

Use the Unraid WebUI **Add Container** flow rather than Docker Compose.

### Repository/image

Future value, after an image exists:

```text
ghcr.io/<owner>/wyoming-s2cpp-tts:<tag>
```

For local development before publishing, build/tag instructions should be added only after the Docker build path is explicitly tested.

### Network

Bridge mode should be sufficient for the Wyoming TCP port unless your Home Assistant setup requires custom networking.

### Path mappings

| Host path | Container path | Purpose |
| --- | --- | --- |
| `/mnt/user/appdata/wyoming-s2cpp-tts/models` | `/models` | GGUF model files, future s2.cpp backend |
| `/mnt/user/appdata/wyoming-s2cpp-tts/voices` | `/voices` | Reference voices / voice metadata |
| `/mnt/user/appdata/wyoming-s2cpp-tts/config` | `/config` | Service config and profiles |

### Ports

| Host port | Container port | Purpose |
| --- | --- | --- |
| `10200` | `10200/tcp` | Wyoming Protocol TTS |
| `8088` | `8088/tcp` | Future health/debug HTTP |
| not exposed by default | `3030/tcp` | Internal s2.cpp HTTP |

Expose `3030` only when intentionally debugging a future internal s2.cpp HTTP server. In current Phase 3, internal s2.cpp startup is not implemented.

### Environment variables

Current useful variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `WYOMING_URI` | `tcp://0.0.0.0:10200` | Wyoming listen URI |
| `TTS_BACKEND` | `fake` | `fake` for deterministic test tone; `s2cpp` for external/buffered backend path |
| `S2_HOST` | `127.0.0.1` | s2.cpp HTTP host when using `TTS_BACKEND=s2cpp` |
| `S2_PORT` | `3030` | s2.cpp HTTP port when using `TTS_BACKEND=s2cpp` |
| `S2_MODEL` | `/models/s2-pro-q6_k.gguf` | Planned model path sent in `/generate` payload |
| `S2_VOICE_DIR` | `/voices` | Planned voice/reference directory |
| `S2CPP_ENABLE_INTERNAL_SERVER` | `false` | Future hook only; currently prints TODO messages and does not start s2.cpp |

Recommended first Unraid test mode remains:

```text
TTS_BACKEND=fake
```

That mode validates the Wyoming container path with deterministic fake audio before any GPU/model work.

## NVIDIA GPU notes

This service is intended to use one NVIDIA RTX 3080 10 GB for the first real version. Do not assume an exact Unraid NVIDIA setup until verified. See [`CUDA_S2CPP_PLAN.md`](CUDA_S2CPP_PLAN.md) for the Phase 4 CUDA/s2.cpp plan and the current untested assumptions.

General checks for a future implementation:

1. Install/configure the Unraid NVIDIA plugin or equivalent supported NVIDIA runtime path.
2. Confirm the GPU is visible on the host with `nvidia-smi`.
3. Configure the container runtime/extra parameters according to the verified Unraid NVIDIA setup.
4. Run `scripts/check_gpu_visibility.sh` inside the future GPU-enabled container.
5. Confirm the script reports `nvidia-smi` success and shows the expected device.
6. Confirm `nvidia-smi` works inside the container before debugging TTS.

Typical future environment variables may include:

```text
NVIDIA_VISIBLE_DEVICES=<gpu-id-or-all>
NVIDIA_DRIVER_CAPABILITIES=compute,utility
S2_GPU_INDEX=0
S2_GPU_LAYERS=36
```

Exact GPU runtime settings should be documented after they are tested on the target Unraid server.

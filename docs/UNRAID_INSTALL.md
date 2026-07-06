# Unraid install draft

This is a draft for a future Docker image. The current repository is scaffold-only and should not be expected to run a working TTS service yet.

## Planned Unraid WebUI Add Container settings

Use the Unraid WebUI **Add Container** flow rather than Docker Compose.

### Repository/image

Future value, after an image exists:

```text
ghcr.io/<owner>/wyoming-s2cpp-tts:<tag>
```

### Network

Bridge mode should be sufficient for the Wyoming TCP port unless your Home Assistant setup requires custom networking.

### Planned path mappings

| Host path | Container path | Purpose |
| --- | --- | --- |
| `/mnt/user/appdata/wyoming-s2cpp-tts/models` | `/models` | GGUF model files |
| `/mnt/user/appdata/wyoming-s2cpp-tts/voices` | `/voices` | Reference voices / voice metadata |
| `/mnt/user/appdata/wyoming-s2cpp-tts/config` | `/config` | Service config and profiles |

### Planned ports

| Host port | Container port | Purpose |
| --- | --- | --- |
| `10200` | `10200/tcp` | Wyoming Protocol TTS |
| `8088` | `8088/tcp` | Health/debug HTTP |
| not exposed by default | `3030/tcp` | Internal s2.cpp HTTP |

Expose `3030` only when intentionally debugging the s2.cpp HTTP server.

## NVIDIA GPU notes

This service is intended to use one NVIDIA RTX 3080 10 GB for the first real version. Do not assume an exact Unraid NVIDIA setup until verified.

General checks for a future implementation:

1. Install/configure the Unraid NVIDIA plugin or equivalent supported NVIDIA runtime path.
2. Confirm the GPU is visible on the host with `nvidia-smi`.
3. Configure the container runtime/extra parameters according to the verified Unraid NVIDIA setup.
4. Pass only the intended GPU to the container when possible.
5. Confirm `nvidia-smi` works inside the container before debugging TTS.

Typical environment variables may eventually include:

```text
NVIDIA_VISIBLE_DEVICES=<gpu-id-or-all>
NVIDIA_DRIVER_CAPABILITIES=compute,utility
S2_GPU_INDEX=0
S2_GPU_LAYERS=36
```

Exact GPU runtime settings should be documented after they are tested on the target Unraid server.

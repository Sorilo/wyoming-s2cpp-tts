# Phase 4 CUDA/s2.cpp and Unraid GPU runtime plan

This is the Phase 4 planning document for future CUDA/s2.cpp support. The build described here is **not yet tested** in this repository, and this phase intentionally does not download GGUF models, vendor s2.cpp, run a Docker build, or compile CUDA code.

## Current verified repo state

- The Python Wyoming wrapper runs with `TTS_BACKEND=fake` by default.
- `TTS_BACKEND=s2cpp` can call an already-running external `/generate` endpoint and convert one buffered PCM response into Wyoming audio events.
- `scripts/smoke_s2cpp_generate.py` can optionally test a direct external `/generate` request.
- The Phase 3 Dockerfile runs the Python wrapper and creates `/models`, `/voices`, and `/config`.

## External reference checked during Phase 4

A lightweight GitHub API/raw README lookup found `sinfisum/s2pro-gguf`, described as a Fish Audio S2 Pro GGUF / s2.cpp project. Its README documents a Windows-tested CUDA server command resembling:

```powershell
.\s2.exe --model models/s2-pro-q8_0.gguf --tokenizer models/tokenizer.json --ngl 36 --cuda 0 --server -H 127.0.0.1 -P 5000
```

Important details from that reference:

- `--server` enables the HTTP server.
- `--model` points at a GGUF model file.
- `--tokenizer` points at `tokenizer.json`.
- `--ngl 36` offloads 36 layers to GPU.
- `--cuda 0` selects GPU index 0.
- `-H` and `-P` configure host and port.
- The reference says it was created/tested on Windows with an NVIDIA RTX 3060 12GB, so Linux/Unraid behavior is **not yet verified** here.

This project should adapt those flags cautiously for Linux only after the actual upstream build process is verified.

## Intended future Linux container shape

The current Dockerfile should remain Python-wrapper-only until Phase 8A proves the s2.cpp Linux/CUDA build. The likely future structure is a multi-stage Dockerfile:

```Dockerfile
# PHASE 4 TODO, not enabled yet:
# FROM nvidia/cuda:<cuda-devel-tag> AS s2cpp-builder
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     build-essential cmake git ca-certificates
# RUN git clone --depth=1 <verified-s2cpp-repo-url> /src/s2.cpp
# WORKDIR /src/s2.cpp
# RUN <verified CUDA build command>
#
# FROM nvidia/cuda:<cuda-runtime-tag> AS runtime
# COPY --from=s2cpp-builder /src/s2.cpp/<verified-binary> /usr/local/bin/s2cpp-server
```

Do not fill in the repository URL, CUDA tag, build command, or binary path until they are actually tested.

## Planned internal runtime command

Once a Linux s2.cpp binary exists inside the image, the entrypoint should supervise it before the Python wrapper when `S2CPP_ENABLE_INTERNAL_SERVER=true`.

Planned command shape, subject to verification:

```bash
s2cpp-server \
  --model "${S2_MODEL:-/models/s2-pro-q6_k.gguf}" \
  --tokenizer "${S2_TOKENIZER:-/models/tokenizer.json}" \
  --ngl "${S2_GPU_LAYERS:-36}" \
  --cuda "${S2_GPU_INDEX:-0}" \
  --server \
  -H "${S2_INTERNAL_HOST:-127.0.0.1}" \
  -P "${S2_PORT:-3030}"
```

Container conventions to preserve:

- `/models` for GGUF model and tokenizer files.
- `/voices` for reference voices.
- `/config` for service profiles/config.
- Wyoming TCP port `10200` exposed to Home Assistant.
- Health/debug HTTP port `8088` reserved.
- s2.cpp HTTP port `3030` internal by default.

## Unraid NVIDIA runtime plan

Official NVIDIA Container Toolkit docs describe `NVIDIA_VISIBLE_DEVICES` as controlling which GPUs are visible inside the container, and `NVIDIA_DRIVER_CAPABILITIES` as controlling which driver libraries/binaries are mounted. For this service, the future Unraid template should expose:

```text
NVIDIA_VISIBLE_DEVICES=<GPU UUID or index for the RTX 3080>
NVIDIA_DRIVER_CAPABILITIES=compute,utility
```

Unraid-specific runtime setup must be verified on the target server. The expected checklist is:

1. Install/configure the Unraid NVIDIA plugin or supported NVIDIA container runtime path.
2. Confirm the host sees the target GPU:

   ```bash
   nvidia-smi
   ```

3. Configure the container to pass through only the intended GPU if possible.
4. Run `scripts/check_gpu_visibility.sh` inside the future GPU-enabled container.
5. Confirm the script reports `nvidia-smi` success and shows the expected device.
6. Only then test internal s2.cpp startup.

## Lightweight validation hook

Use:

```bash
scripts/check_gpu_visibility.sh
```

This script is safe on non-GPU systems. It reports `status=unavailable` and exits `0` when `nvidia-smi` is missing so normal CI/local tests do not require GPU infrastructure.

## Hardware expansion note

The initial baseline remains one NVIDIA RTX 3080 10 GB and one active s2.cpp synthesis. A possible later RTX 5080 16 GB upgrade, multi-worker scheduling, multi-model routing, or multi-GPU routing is post-v0.1 work and should not be introduced before the single-worker v0.1 baseline is stable.

## What Phase 4 did not verify

- No CUDA image was built.
- No s2.cpp source was cloned or vendored.
- No Linux s2.cpp binary was compiled.
- No model/tokenizer files were downloaded.
- No container GPU passthrough was tested.
- No realtime factor, VRAM usage, or audio quality was measured.

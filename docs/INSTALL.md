# Install — wyoming-s2cpp-tts v0.1.0

## Prerequisites

- Unraid (or any Docker host) with NVIDIA GPU (RTX 3080 recommended).
- NVIDIA Container Toolkit installed.
- Docker Compose v2 or later.
- GGUF model file(s) and tokenizer.json from the
  [sinfisum/s2pro-gguf](https://huggingface.co/sinfisum/s2pro-gguf) collection.
- Optional: custom `.s2voice` profiles (see Voice Profiles below).

## Quick install

```
# Clone the repository
git clone https://github.com/sorilo/wyoming-s2cpp-tts.git
cd wyoming-s2cpp-tts

# Copy and edit the environment file
cp .env.example .env
# Edit .env to set your host paths

# Start both containers
docker compose up -d

# Verify health
docker compose ps
docker compose logs -f
```

## Home Assistant setup

1. Go to Settings > Devices & services > Add Integration.
2. Select Wyoming Protocol.
3. Enter host: <your-docker-host-ip>.
4. Enter port: 10200.
5. The service auto-discovers as wyoming-s2cpp-tts with voice s2-pro.

See HOME_ASSISTANT_SETUP.md for detailed setup.

## Model preparation

Place your GGUF model and tokenizer in the host models directory:

```
/mnt/user/appdata/s2cpp/models/
├── s2-pro-q4_k_m.gguf
└── tokenizer.json
```

The backend container mounts this directory read-only at /models.

## Voice profiles (optional)

Create .s2voice profiles using the s2 CLI on the backend:

```
docker exec -it s2cpp-backend s2   --model /models/s2-pro-q4_k_m.gguf   --prompt-audio /path/to/reference.wav   --prompt-text "Reference transcript text."   --voice my_profile   --save-voice --voice-dir /voices
```

Profiles placed in the host voices directory are discoverable by the wrapper
without container rebuild or restart.

## Multi-GPU or different CUDA architectures

Set CUDA_ARCHITECTURES in your build args if building backend images:

| GPU | CUDA Arch |
|-----|-----------|
| RTX 3000 series | 86 |
| RTX 4000 series | 89 |
| RTX 5000 series | 120 |

## Troubleshooting

- Backend fails health check: Verify model file exists and MODELS_DIR is correctly mounted.
- Wrapper cannot reach backend: Both containers must be on the same Docker network.
- HA preview hangs: Ensure wrapper image includes Wyoming streaming fix (Phase 6C+).
- No audio / connection errors: Check docker compose logs wyoming-s2cpp-tts for S2ClientError.

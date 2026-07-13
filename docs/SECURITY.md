# Security — wyoming-s2cpp-tts v0.1.0

## Design principles

- **No secrets in images**: environment variables configure the service at runtime. No API keys, tokens, credentials, or secrets are baked into container images.
- **Private backend network**: the CUDA s2.cpp backend communicates only on the internal `s2cpp-net` bridge network. Its HTTP port (3030) is **never** published to the host — only the wrapper Wyoming port (10200) is exposed.
- **Read-only endpoints**: the optional admin HTTP server (`ADMIN_HTTP_ENABLED=false` by default) is loopback-bound (`127.0.0.1:10201`) and serves read-only status/metrics with no mutating endpoints.
- **No plaintext in logs**: synthesis text is never logged in full. Structured log events use SHA-256 fingerprints and omit plaintext, user identifiers, or PII. Admin HTTP `/status` and `/metrics` responses are sanitized — no plaintext, audio, or secrets are exposed.
- **Immutable image tags**: production deployments should pin to `sha-*` image tags for deterministic provenance. Floating tags (`latest`, `edge`, `0.1.0`) are acceptable for development but not for production.

## Network model

```
Host LAN
  └─ :10200 (Wyoming TCP, exposed)
       └─ wyoming-s2cpp-tts wrapper
            └─ s2cpp-net (private bridge)
                 └─ s2cpp-backend:3030 (HTTP, NOT exposed to host)
```

The `s2cpp-net` Docker network is a private bridge. Set `NETWORK_NAME` in `.env` to customize; the network should remain internal — do not publish the backend port.

## Admin HTTP safety

- **Disabled by default** (`ADMIN_HTTP_ENABLED=false`).
- When enabled, binds to `127.0.0.1:10201` (loopback only).
- Read-only endpoints: `GET /livez`, `GET /readyz`, `GET /status`, `GET /metrics`.
- No mutating endpoints (all other methods return 405).
- No plaintext, audio, secrets, tokens, or user IDs in responses.
- Bounded time/size HTTP parsing prevents resource exhaustion.
- Bind failure is non-fatal — the Wyoming service continues to run.

## Image supply chain

- Images are built from this repository and published to `ghcr.io/sorilo/wyoming-s2cpp-tts*`.
- Pinned `sha-*` tags identify exact build artifacts.
- Rollback images are documented in `docs/UPGRADE_ROLLBACK.md`.
- Users are responsible for verifying image provenance before deployment.

## Vulnerability reporting

This is a personal project. Report concerns through the repository issue tracker. Do not expect a formal security disclosure process or guaranteed response timeline.

## Operational hardening

- `.env` and `.env.*` are gitignored (except `.env.example`). Never commit real environment values.
- Model files (`.gguf`, `.pt`, `.safetensors`) and generated audio (`.wav`, `.pcm`) are gitignored.
- The wrapper container does not require NVIDIA runtime, CUDA, or GPU access.
- Backend volumes are mounted read-only where possible (`/models:ro`).

# Security — wyoming-s2cpp-tts v0.1.0

## Design principles

- **No secrets in images**: environment variables configure the service at runtime. No API keys, tokens, credentials, or secrets are baked into container images.
- **Host-unpublished backend port**: the CUDA s2.cpp backend communicates with the wrapper on the shared `s2cpp-net` bridge network. Its HTTP port (3030) is **never** published to the host — only the wrapper Wyoming port (10200) is exposed.
- **Read-only endpoints**: the optional admin HTTP server (`ADMIN_HTTP_ENABLED=false` by default) is loopback-bound (`127.0.0.1:10201`) and serves read-only status/metrics with no mutating endpoints.
- **No plaintext in logs**: synthesis text is never logged in full. Structured log events use SHA-256 fingerprints and omit plaintext, user identifiers, or PII. Admin HTTP `/status` and `/metrics` responses are sanitized — no plaintext, audio, or secrets are exposed.
- **Immutable image tags**: production deployments should pin to `sha-*` image tags for deterministic provenance. Floating tags (`latest`, `edge`, `0.1.0`) are acceptable for development but not for production.

## Network model

```
Host LAN
  └─ :10200 (Wyoming TCP, exposed)
       └─ wyoming-s2cpp-tts wrapper
            └─ s2cpp-net (shared bridge; outbound allowed)
                 └─ s2cpp-backend:3030 (HTTP, NOT exposed to host)
```

The `s2cpp-net` network is a project-scoped Docker bridge, not a Docker `internal: true` network; containers retain outbound access. Set `NETWORK_NAME` in `.env` to customize. Isolation from the host/LAN comes from never publishing backend port 3030.

## Admin HTTP safety

- **Disabled by default** (`ADMIN_HTTP_ENABLED=false`).
- When enabled, binds to `127.0.0.1:10201` (loopback only).
- Read-only endpoints: `GET /livez`, `GET /readyz`, `GET /status`, `GET /metrics`.
- No mutating endpoints (all other methods return 405).
- No plaintext, audio, secrets, tokens, or user IDs in responses.
- Bounded time/size HTTP parsing prevents resource exhaustion.
- Bind failure is non-fatal — the Wyoming service continues to run.

## Offline voice-import safety

- Voice creation is an explicit operator-side, local-filesystem workflow. The
  importer has no URL/download mode and does not expose a network service.
- Reference audio, transcripts, normalized audio, generated validation WAVs,
  models, and generated profiles are never baked into the image.
- Real import refuses while an `s2 --server` process is active, preventing an
  automatic second model-bearing process from competing for VRAM. The tool
  never stops or restarts the backend; dry-run remains available.
- Voice IDs are restricted to a bounded filename-safe grammar. Source, model,
  tokenizer, destination, transcript-file, and optional validation-WAV paths
  reject unsafe file types or symlink traversal as applicable.
- Subprocesses use argument arrays with `shell=False`. Commands have bounded
  timeouts, and errors do not echo captured subprocess output or transcripts.
- Profile, canonical sidecar, and optional validation audio are validated and
  placed from same-filesystem staging. No-overwrite is race-safe; `--force`
  requires explicit operator intent and rollback preserves an earlier pair if
  final publication fails.
- The exact transcript is required by pinned s2.cpp, appears in the child
  process arguments while it runs, and is embedded in `.s2voice`. Prefer
  `--transcript-file`, restrict local process access, and treat the resulting
  profile as sensitive when the transcript or voice is sensitive.
- License, attribution, and provenance are mandatory metadata, but operators
  remain responsible for consent and usage rights. See `VOICE_PROFILES.md`.

## Supply-chain gates

- PR CI scans full Git history with Gitleaks and scans dependencies, Dockerfiles, and source configuration with Trivy. Both gates fail closed.
- The paired-release workflow repeats the source scans, builds the wrapper/backend candidates without publishing, smoke-tests both exact local image references, and blocks publication until Trivy reports no fixed HIGH/CRITICAL image vulnerabilities.
- All third-party workflow actions are pinned to verified 40-character commits. Trivy itself is pinned to `v0.72.0` in workflow inputs.
- `.gitleaksignore` contains only eight exact fingerprints for a public Python base-image `GPG_KEY` captured in historical verification artifacts; wildcard or rule-wide suppression is prohibited.
- Images are published to `ghcr.io/sorilo/wyoming-s2cpp-tts*` only after the manual release gate.
- Pinned seven-character `sha-*` tags identify exact build artifacts; semantic image tags omit the Git tag's leading `v`.
- Release artifacts include paired digests, SBOMs, and build-provenance attestations. Rollback is documented in `docs/UPGRADE_ROLLBACK.md`.

## Vulnerability reporting

This is a personal project. Report concerns through the repository issue tracker. Do not expect a formal security disclosure process or guaranteed response timeline.

## Operational hardening

- `.env` and `.env.*` are gitignored (except `.env.example`). Never commit real environment values.
- Model files (`.gguf`, `.pt`, `.safetensors`) and generated audio (`.wav`, `.pcm`) are gitignored.
- The wrapper container does not require NVIDIA runtime, CUDA, or GPU access.
- Backend volumes are mounted read-only where possible (`/models:ro`).

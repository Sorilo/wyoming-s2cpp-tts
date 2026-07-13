# Upgrade & Rollback — wyoming-s2cpp-tts v0.1.0

## Before you start

1. **Backup your data directories**:
   ```bash
   # Back up voices and models (adjust paths as needed)
   cp -a /mnt/user/appdata/s2cpp/voices /mnt/user/appdata/s2cpp/voices.bak.$(date +%Y%m%d)
   cp -a /mnt/user/appdata/s2cpp/models /mnt/user/appdata/s2cpp/models.bak.$(date +%Y%m%d)
   ```

2. **Record current image tags**:
   ```bash
   docker compose ps
   docker images | grep wyoming-s2cpp-tts
   ```

3. **Pull new images** (if using tagged releases):
   ```bash
   docker compose pull
   ```

## Upgrading

### Docker Compose (recommended)

```bash
# 1. Pull the updated compose.yaml, .env.example, and .dockerignore
git pull origin phase/phase-11-operations

# 2. Review .env.example for new/changed variables and update .env
diff .env.example .env

# 3. Recreate containers with updated images
docker compose up -d

# 4. Verify health
docker compose ps
docker compose logs -f
```

### Immutable sha-* pins

For production, pin to specific `sha-*` image tags in your `.env`:

```ini
BACKEND_IMAGE=ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-YOURPIN
WRAPPER_IMAGE=ghcr.io/sorilo/wyoming-s2cpp-tts:sha-YOURPIN
```

This guarantees deterministic behavior. Upgrade by changing to a newer verified pin and running `docker compose up -d`.

## Rollback

### Rollback to a previous image pin

```bash
# 1. Edit .env to restore the previous image tags
#    e.g., BACKEND_IMAGE=ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-OLD
#          WRAPPER_IMAGE=ghcr.io/sorilo/wyoming-s2cpp-tts:sha-OLD

# 2. Recreate containers
docker compose down
docker compose up -d

# 3. Verify
docker compose logs -f
```

### Rollback data directories

If you backed up before upgrading:

```bash
# Stop containers
docker compose down

# Restore data
rm -rf /mnt/user/appdata/s2cpp/voices
rm -rf /mnt/user/appdata/s2cpp/models
cp -a /mnt/user/appdata/s2cpp/voices.bak.YYYYMMDD /mnt/user/appdata/s2cpp/voices
cp -a /mnt/user/appdata/s2cpp/models.bak.YYYYMMDD /mnt/user/appdata/s2cpp/models

# Start containers
docker compose up -d
```

## Downgrade caveats

- **Backend version compatibility**: newer `.s2voice` profiles may not be readable by older backend images. Keep profile backups.
- **Model compatibility**: GGUF format is backward-compatible within the same architecture. Changing quantization levels (Q4 → Q6) requires matching the model file to the backend image.
- **Config drift**: downgrading an image but keeping newer env vars may cause unrecognized-variable warnings or startup failures. Match `.env` to the target image version.

## Supported upgrade paths

| From | To | Supported | Notes |
|------|----|-----------|-------|
| v0.1.0 (any sha-*) | v0.1.0 (any sha-*) | Yes | Same Compose schema; voice/model compatibility required |
| Pre-v0.1.0 (Phase 9+) | v0.1.0 | Manual | Compose schema changed; see `docs/INSTALL.md` |
| Pre-v0.1.0 (< Phase 9) | v0.1.0 | Not supported | Expect breaking changes |

## Related documents

- `docs/SECURITY.md` — security posture and network model
- `docs/RELEASE.md` — release checklist and tagging
- `docs/INSTALL.md` — fresh install instructions
- `docs/UNRAID_INSTALL.md` — Unraid-specific notes

# Release Process — wyoming-s2cpp-tts v0.1.0

## Versioning

This project uses **v0.1.0** as the initial release baseline. Version tags follow
semantic versioning at the v0.x level:

- **Major**: 0 (pre-1.0)
- **Minor**: increments for feature releases (0.1, 0.2, ...)
- **Patch**: increments for hotfixes on a released minor (0.1.1, 0.1.2, ...)

## Release checklist

Before tagging a release:

- [ ] Full test suite passes (excluding environment-specific tests):
  ```bash
  python -m pytest tests/ --ignore=tests/test_realtime_tuning_unraid.py -q
  ```

- [ ] `.env.example` is up to date with all configurable variables.

- [ ] `README.md` reflects current deployment reality.

- [ ] `CHANGELOG.md` entry exists for the release under `## Unreleased` and
  will be moved to a versioned heading at release time.

- [ ] `docs/SECURITY.md` is reviewed for accuracy.

- [ ] `docs/UPGRADE_ROLLBACK.md` is reviewed and upgrade path is documented.

- [ ] `docs/INSTALL.md` is tested on a clean clone.

- [ ] No real server IPs, tokens, or credentials in any tracked file.

- [ ] Container images are built and published to `ghcr.io/sorilo/`:
  - Wrapper: `ghcr.io/sorilo/wyoming-s2cpp-tts:0.1.0` +
    `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-XXXXXXXX`
  - Backend: `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:0.1.0` +
    `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-XXXXXXXX`

- [ ] Image SHA digests are recorded in release notes.

## Tagging

```bash
git tag -a v0.1.0 -m "v0.1.0: initial Wyoming s2.cpp TTS release"
git push origin v0.1.0
```

Tags are lightweight annotated tags on the release branch.

## Image publication

Images are built via GitHub Actions workflows and published to
`ghcr.io/sorilo/`:

| Image | Description |
|-------|-------------|
| `wyoming-s2cpp-tts:0.1.0` | CPU-only Wyoming Protocol wrapper |
| `wyoming-s2cpp-tts-backend:0.1.0` | CUDA s2.cpp inference backend |

Each push produces an immutable `sha-<short-commit>` tag for production pinning.

## Rollback criteria

Roll back a release if any of the following occur in production:

- Container fails to start or health check fails consistently.
- Wyoming protocol regression: HA cannot discover or use the service.
- Audio quality regression: garbled, truncated, or silent output.
- Resource regression: VRAM exhaustion, CPU saturation, or OOM.
- Regression in barge-in, cancellation, or scheduler behavior.

See `docs/UPGRADE_ROLLBACK.md` for the rollback procedure.

## Known limitations at v0.1.0

- Stock Home Assistant 2026.7.2 + Voice PE 26.6.0 does **not** pass full
  one-wake barge-in (announcement pipeline limitation). This is an external
  platform constraint, not a repository defect. See
  `docs/validation/PHASE_10_CLOSURE.md`.
- Full Unraid template persistence, restart, update, and backup validation is
  deferred to Phase 14.
- Hardware-upgrade benchmarking is post-v0.1 work.

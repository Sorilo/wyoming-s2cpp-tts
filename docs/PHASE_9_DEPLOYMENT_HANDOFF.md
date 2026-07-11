# Phase 9 Production Deployment Handoff

## Status

Phase 9 implementation, merge, automated testing, isolated Unraid validation, candidate verification, and per-container production deployment verification are complete. PR #2 merged to `main` as `1a0b93f818f61cf560f7921a54cf984b86066798`.

Both validated images are now running in production. The final non-destructive production smoke remains: one short direct Wyoming request, one long direct Wyoming request, one Home Assistant VM TTS request, then a log and restart-count check. Do not repeat the 876-test suite or isolated stress tests unless deployment reveals an unexplained problem.

Canonical evidence:

- Isolated validation: `verification_artifacts/phase_9_live_smoke/20260711_050514/`
- Production discovery/deployment: `verification_artifacts/phase_9_production/20260711_072721/`

Evidence archives remain local and ignored by Git; do not commit raw archives.

## Production images and provenance

| Component | Source revision | Production image | Canonical digest |
| --- | --- | --- | --- |
| Wrapper | `7db26b70092db973a0a5c25270cf9d544afa02cf` | `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-7db26b7` | `sha256:04619a20028cabc088a56bfa461461bf71a4c6753a77195dd78cb7e5011e8d5f` |
| Backend | `6e629d0066f40ebe36a611db6e2dd4172ddcb412` | `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-6e629d0` | `sha256:3a1d202dfe5bae8b692babc130e630d0638d6d5e3f64dc584cd6bd316a123439` |

Never use wrapper digest `sha256:d15748c59a97bbebf22d803ea09e09718abdafd61c085ad46ef1bd4aa314b787`.

Active Unraid templates:

- Backend: `/boot/config/plugins/dockerMan/templates-user/my-s2cpp-backend.xml`
- Wrapper: `/boot/config/plugins/dockerMan/templates-user/my-wyoming-s2cpp-tts.xml`

`/boot/config/plugins/dockerMan/templates-user/my-wyoming-wrapper.xml` is stale and must remain untouched. Only `s2cpp-backend` and `wyoming-s2cpp-tts` belong to this Docker deployment. Home Assistant runs as a VM, not a Docker container.

## Production settings

Wrapper settings changed for Phase 9:

```text
S2_BACKEND_BUSY_MAX_RETRIES=10
S2_BACKEND_BUSY_RETRY_DELAY_MS=500
```

Required preserved settings:

```text
S2_QUEUE_WAIT_TIMEOUT_SEC=30
S2_SYNTHESIS_TIMEOUT_SEC=120
S2_HOST=s2cpp-backend
S2_PORT=3030
WYOMING_URI=tcp://0.0.0.0:10200
```

Preserve all other backend and wrapper fields, mounts, ports, networking, GPU assignment, voice selection, and runtime tuning settings.

## Completed verification

- Repository suite: **876 passed, 0 failed, 0 skipped**.
- Isolated real-hardware Unraid validation: **PASS**.
- Short and long synthesis: **PASS**; long RTF approximately `0.961`.
- FIFO behavior and final queue depth: **PASS**.
- Queue-full rejection and recovery: **PASS**.
- Three disconnect/recovery cycles: **PASS**.
- Persistent HTTP 503/busy latch: absent.
- `Task exception was never retrieved`: absent.
- `disconnect_cleanup_error`: absent.
- Backend deployment verification: immutable identity, GPU use, port `3032`, zero restarts, valid buffered/progressive audio: **PASS**.
- Wrapper deployment verification: immutable identity, healthy, port `10200`, backend reachability, settings `10`/`500`, zero restarts: **PASS**.

## Compact final smoke

Run one short and one long sequential Wyoming request to `127.0.0.1:10200`. Both must produce non-empty, frame-aligned 44.1 kHz mono s16le PCM with one `AudioStart` and one `AudioStop`, and both containers must remain running with restart count zero.

Then issue one ordinary TTS request from the Home Assistant VM and confirm that speech is audible. Do not change Home Assistant VM configuration.

After the three requests, capture:

```bash
docker inspect wyoming-s2cpp-tts --format 'wrapper image={{.Config.Image}} running={{.State.Running}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restarts={{.RestartCount}}'
docker inspect s2cpp-backend --format 'backend image={{.Config.Image}} running={{.State.Running}} restarts={{.RestartCount}}'
docker logs --since 15m wyoming-s2cpp-tts 2>&1 | tail -n 200
docker logs --since 15m s2cpp-backend 2>&1 | tail -n 200
nvidia-smi --query-compute-apps=gpu_uuid,pid,process_name,used_memory --format=csv,noheader
```

Acceptance requires audible HA speech, both direct requests valid, wrapper healthy, both restart counts zero, backend GPU use present, and no unexplained traceback, persistent HTTP 503/busy exhaustion, CUDA/OOM failure, unobserved task exception, or disconnect-cleanup error.

## Rollback

Rollback is triggered by a blocking startup failure, invalid/empty audio, wrapper health failure, loss of backend GPU operation, restart loop, persistent HTTP 503/busy exhaustion, or unexplained runtime exception.

Restore only these values in the active templates and preserve everything else:

```text
Backend Repository:
ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd

Wrapper Repository:
ghcr.io/sorilo/wyoming-s2cpp-tts:sha-12f3bf8

S2_BACKEND_BUSY_MAX_RETRIES=3
S2_BACKEND_BUSY_RETRY_DELAY_MS=200
```

Both rollback images were verified local before deployment. After rollback, apply both templates and repeat only the compact service checks and one ordinary TTS request.

## Closeout boundary

Phase 9 repository implementation is closed. After the final smoke passes, record production smoke as complete without reopening runtime implementation. Phase 9B is planning-only until its separate planning PR is reviewed; no Phase 9B runtime code is part of this closeout.

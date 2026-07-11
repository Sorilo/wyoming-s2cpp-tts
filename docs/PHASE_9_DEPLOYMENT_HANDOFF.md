# Phase 9 Production Deployment Handoff

## Status

Phase 9 implementation, merge, automated testing, isolated Unraid validation, candidate verification, and per-container production deployment verification are complete. PR #2 merged to `main` as `1a0b93f818f61cf560f7921a54cf984b86066798`.

Both validated images are running in production, and the final non-destructive production smoke passed. Phase 9 is deployed and closed. Do not repeat the 876-test suite or isolated stress tests unless a future deployment reveals an unexplained problem.

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

## Final production smoke — PASS

Final production verification completed successfully:

- Backend `sha-6e629d0` and wrapper `sha-7db26b7` were running.
- Wrapper health was `healthy`; both restart counts remained zero.
- Backend host port `3032` and wrapper host port `10200` were reachable.
- Wrapper-to-backend connectivity succeeded.
- Backend inference remained on GPU `GPU-65b9a886-d157-27fa-09d1-8894bc5cc135` using approximately `3372 MiB`.
- Effective Phase 9 settings were retries `10`, retry delay `500 ms`, queue wait timeout `30 s`, and synthesis timeout `120 s`.

Direct Wyoming results:

| Request | Result | Audio | Chunks | First audio | RTF |
| --- | --- | --- | ---: | ---: | ---: |
| Short | PASS | 44.1 kHz, mono, 16-bit PCM; one `AudioStart` and one `AudioStop` | 24 | `1.445 s` | `1.002` |
| Long | PASS | 44.1 kHz, mono, 16-bit PCM; one `AudioStart` and one `AudioStop` | 191 | `1.358 s` | `0.974` |

Home Assistant VM smoke: **PASS**. Voice `cmu_rms_male_us` produced audible and intelligible speech, the request completed normally, backend stream status was `ok`, queue depth returned to zero, first backend audio arrived at `1398 ms`, wrapper forwarding overhead was `1 ms`, and both `AudioStart` and `AudioStop` were emitted.

Final log scan: **PASS**. No blocking pattern, persistent busy/503 state, traceback, CUDA failure, OOM, unobserved task exception, or `disconnect_cleanup_error` was present.

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

Phase 9 implementation, deployment, direct production smoke, Home Assistant production smoke, and log verification are complete. Rollback remains prepared, and Phase 9 is closed. Phase 9B was subsequently implemented as a source-only refactor; no Phase 9B image was published or deployed, so production remains on the verified Phase 9 images.

# Phase 9 Deployment Handoff

## Closeout status

Phase 9 validation is complete with classification **PASS**. This handoff is the canonical closeout record. Validation did **not** deploy either candidate image; the production wrapper and backend remained running and unchanged. Temporary validation containers were removed.

Canonical evidence: `verification_artifacts/phase_9_live_smoke/20260711_050514/`

## Final validated candidates

| Component | Source revision | Image | Canonical digest |
| --- | --- | --- | --- |
| Wrapper | `7db26b70092db973a0a5c25270cf9d544afa02cf` | `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-7db26b7` | `sha256:04619a20028cabc088a56bfa461461bf71a4c6753a77195dd78cb7e5011e8d5f` |
| Backend | `6e629d0066f40ebe36a611db6e2dd4172ddcb412` | `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-6e629d0` | `sha256:3a1d202dfe5bae8b692babc130e630d0638d6d5e3f64dc584cd6bd316a123439` |

These are validated deployment candidates, not a statement of current production deployment.

## Production observed during validation

Production was unchanged throughout the isolated run:

- Wrapper: `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-12f3bf8`
- Backend: `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd`
- Model/runtime baseline: Q4_K_M, context 32, decode stride 32, 8 threads

The live Phase 9 candidate configuration was:

| Variable | Validated value | Meaning |
| --- | ---: | --- |
| `S2_BACKEND_BUSY_MAX_RETRIES` | `10` | 10 additional retries; 11 total attempts including the initial request |
| `S2_BACKEND_BUSY_RETRY_DELAY_MS` | `500` | Delay between busy retries |
| `S2_QUEUE_WAIT_TIMEOUT_SEC` | `30` | Maximum queue wait |
| `S2_SYNTHESIS_TIMEOUT_SEC` | `120` | Maximum backend synthesis time |

## Acceptance evidence

- Harness task exited `0`; overall classification and short synthesis: **PASS**.
- Long synthesis: **PASS**, 119 chunks, RTF approximately `0.961`.
- FIFO admission, execution order, valid PCM, and final queue depth zero: **PASS**.
- Queue-full rejection emitted no PCM; queued work completed and a subsequent recovery synthesis returned valid PCM: **PASS**.
- Three deliberate disconnect/recovery cycles produced valid recovery audio, released queue/backend busy state, and left no persistent HTTP 503 latch: **PASS**.
- Logs contained neither `Task exception was never retrieved` nor `disconnect_cleanup_error`.
- Production wrapper/backend identity remained unchanged; temporary validation containers were removed.
- Repository test baseline: **876 passed, 0 failed, 0 skipped**.

See `summary.md`, `results.json`, `console.log`, the preserved wrapper/backend logs, and production before/after comparison files in the canonical evidence directory.

## Deployment and rollback

Deployment was not performed as part of validation or this documentation closeout. A later authorized deployment must pin both candidate digests above, preserve the validated settings, verify production identity after replacement, and repeat a non-destructive short synthesis/recovery check.

Wrapper rollback remains `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-22db725`.

Earlier wrapper candidates `sha-bbb72e8` and `sha-78ee86b` are superseded. `sha-12f3bf8` is historical/obsolete and must not be selected for a new Phase 9 deployment.

## Historical validation note

Earlier candidate runs exposed wrapper busy-retry/disconnect handling defects and a backend persistent-busy deadlock. Those candidates failed or were superseded. The final wrapper initializes retry state and treats expected operational disconnects without unobserved task exceptions; the final backend avoids recursive cancellation locking and releases busy state on all paths. Only the final candidate pair and canonical PASS evidence above govern Phase 9 acceptance.

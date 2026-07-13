# Phase 10 Closure — End-to-End Barge-In

**Validation date (UTC):** 2026-07-13
**Branch:** `phase/phase-10-end-to-end-barge-in`
**Validation harness revision:** `47940bba5e054676301f07c1f0ab28279eafa391`
**Deployed revision:** `75936bc3607ce6fc38730ac2232397127f9e3c23`
**Status:** **Phase 10 implementation validation complete with documented external stock-platform limitation.**

## Decision

Phase 10 is implementation-complete for behavior owned by this repository. The wrapper correctly cancels synthesis after a Wyoming client disconnect, propagates cancellation to the native backend, releases scheduler state, and accepts a follow-up request. Overlap recovery also completes without a persistent queue or busy latch.

This closure does **not** classify stock Home Assistant Voice Preview Edition single-wake barge-in as passing. Generic `media_player.media_stop` on the validated stock stack neither stops the active Assist announcement nor cancels the Home Assistant TTS producer, so no Wyoming disconnect or cancellation reaches this service.

## Validated stack

| Component | Version or immutable identity |
|---|---|
| Home Assistant | `2026.7.2` |
| Voice PE firmware | `26.6.0` |
| ESPHome | `2026.6.0` |
| Wrapper image | `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-75936bc` |
| Backend image | `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-75936bc` |

`47940bb` identifies the final harness source; it was not deployed. No rebuild, redeployment, firmware change, HA configuration change, or container-template change occurred.

## Scenario matrix

| Scenario | Result | Assertions | Authoritative artifact |
|---|---:|---:|---|
| Health | PASS | 6/6 | `artifacts/phase10/closure-75936bc/20260713T024425Z/` |
| Normal synthesis | PASS | 7/7 | `artifacts/phase10/closure-75936bc/20260713T024448Z/` |
| Direct disconnect | PASS | 25/25 | `artifacts/phase10/20260713T040525Z/` |
| Overlap recovery | PASS | 8/8 | `artifacts/phase10/20260713T040932Z/` |
| Generic HA media stop | EXTERNAL LIMITATION | 7/9 | `artifacts/phase10/media-stop-rerun-3-harness-47940bb/20260713T053252Z/` |
| Stock Voice PE one-wake barge-in | **NOT PASS** | deferred | Upstream lifecycle fix or Cortex-Satellite required |

The failed media-stop assertions are `ha_media_playback_stopped` and `server_cancellation_observed`. They describe one external lifecycle gap: the stock player did not terminate the announcement, so the producer connection remained live and emitted no cancellation to this repository.

## Repository-owned results

### Direct disconnect — PASS

The final correlated run passed all 25 assertions. Audio began before disconnect; wrapper cancellation and backend native abort were correlated; final decode was skipped; cleanup and busy-guard release completed; scheduler depth, pending, waiting, and active synthesis returned to zero; and the correlated follow-up produced valid PCM. This proves the repository contract: when the Wyoming transport disconnects, connection-owned work is cancelled and the service recovers.

### Overlap recovery — PASS

The overlap run passed all eight assertions. Both requests produced protocol-valid audio and the final scheduler state was quiescent, with no pending request, waiter, active synthesis, or persistent busy condition.

## Stock media-stop root cause

Assist TTS on Voice PE uses `ANNOUNCEMENT_PIPELINE`; ordinary media uses `MEDIA_PIPELINE`. HA 2026.7.2's generic ESPHome player stop sends `MEDIA_PLAYER_COMMAND_STOP` without an announcement selector. ESPHome 2026.6.0 selects `MEDIA_PIPELINE` when that selector is absent or false, and selects `ANNOUNCEMENT_PIPELINE` only for `announcement=true`. The unqualified stop therefore targets normal media, not the active Assist announcement. ESPHome's Voice Assistant-owned stop path explicitly sends STOP with `announcement=true`.

Pinned source evidence:

- [HA 2026.7.2 ESPHome media-player stop](https://github.com/home-assistant/core/blob/f9122fb28dd30d3833b3b313924befbc82157f97/homeassistant/components/esphome/media_player.py)
- [Voice PE 26.6.0 dual pipelines](https://github.com/esphome/home-assistant-voice-pe/blob/772f2b9c8a881899a6f7b44d997aa6093c7e8aa7/home-assistant-voice.yaml)
- [ESPHome 2026.6.0 pipeline selection](https://github.com/esphome/esphome/blob/e2157a3d26a8959c7c7ff212ab40afdd7b9f9d13/esphome/components/speaker_source/speaker_source_media_player.cpp)
- [ESPHome announcement-aware Voice Assistant stop](https://github.com/esphome/esphome/blob/e2157a3d26a8959c7c7ff212ab40afdd7b9f9d13/esphome/components/voice_assistant/voice_assistant.cpp)

HA consumes Wyoming TTS in a background cache-loading task. Generic player stop does not cancel that producer. Stock Wyoming has no independent asynchronous "cancel active synthesis" event; this service learns cancellation through transport closure, write failure, connection-scoped replacement, or internal cancellation. Thus player stop does not imply HA producer cancellation, Wyoming closure, wrapper cancellation, or backend abort.

The final run matched this mechanism: HA accepted the service call; media remained `playing`; the operator observed a brief pause followed by resumed original speech; the connection stayed active; and backend generation continued normally.

## Ownership boundary

This repository owns Wyoming connection lifecycle, connection-scoped scheduler cancellation, wrapper-to-backend propagation, native cancellation/cleanup instrumentation, queue/busy recovery, and status/metrics evidence.

It does not own HA's media-player semantics or TTS producer lifetime, Voice PE pipeline routing, ESPHome firmware, or stock wake policy. A repository cancel-all endpoint would be unsafe without authenticated exact-session correlation and is not part of this closure.

## Final runtime health

A fresh read-only check reported `RUNNING`, ready, zero active connections, depth/pending/waiting `0/0/0`, no active synthesis, admitted/completed `72/72`, no failures/timeouts/rejections, and zero backend busy retries. Both images remain `sha-75936bc`.

## Verification

| Gate | Result |
|---|---:|
| Focused Phase 10 plus patch tests | 264 passed |
| Patch application/observability subset | 6 passed |
| Full suite | 1,512 passed |
| Established exclusion | only `tests/test_realtime_tuning_unraid.py` |
| Python compilation | PASS |
| `git diff --check` | PASS |
| Patch dry-run and clean application | PASS |
| Hygiene and staged secret scan | PASS before commit |

## Known limitations

- Stock Voice PE one-wake barge-in is **not passed**.
- Generic HA media stop is not announcement-aware on this stack.
- HA's producer can continue after player stop; the server cannot infer interruption.
- The final media-stop artifact's empty wrapper log prevents proof of an exact eventual socket-disconnect timestamp; it proves the connection/synthesis remained active at the action boundary and no cancellation/abort was observed.
- Full one-wake behavior depends on an upstream fix or a client that owns playback and transport cancellation together.

## Rollback

No rollback is required; closure changes documentation only. To restore the validated deployment independently, pin the existing Unraid templates to:

```text
ghcr.io/sorilo/wyoming-s2cpp-tts:sha-75936bc
ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-75936bc
```

Preserve environment, volumes, ports, and networking; update/restart only `wyoming-s2cpp-tts` and `s2cpp-backend`; verify exact images, readiness, quiescent scheduler state, and follow-up synthesis. HA is outside this repository's Docker scope and must not be modified for rollback.

## Cortex-Satellite acceptance criteria

A single interruption action must prove:

1. replacement wake during speech with no second wake;
2. announcement stop and bounded local-buffer flush;
3. exact TTS producer/session cancellation;
4. exact Wyoming closure or peer-supported correlated cancel;
5. wrapper cancellation and native backend abort;
6. stale decode/audio suppression and pending-phrase cleanup;
7. scheduler release and replacement completion;
8. zero connections/depth/pending/waiting/active after recovery;
9. repeatability without queue, task, socket, or busy leaks;
10. correlation across wake, playback, Wyoming, wrapper, and backend evidence.

## Artifact handling

The full report and evidence remain untracked under `artifacts/phase10/closure-final/`. Raw artifacts must not be committed. The report hash is stored in a sibling `.sha256` sidecar and recorded in the PR summary.

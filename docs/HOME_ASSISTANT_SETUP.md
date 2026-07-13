# Home Assistant setup — v0.1.0

## Deployment overview

The Wyoming TTS service is deployed as two Docker containers on a private Docker bridge network:

- **Wrapper:** `wyoming-s2cpp-tts` (CPU-only), Wyoming port 10200
- **Backend:** `s2cpp-backend` (CUDA), HTTP port 3030 (private network only; not published to host)

The wrapper is exposed to your LAN at `<your-docker-host>:10200`. Home Assistant connects to this host:port.

See `compose.yaml` for the full service definition and `docs/INSTALL.md` for installation instructions.

## Add the Wyoming integration

1. In Home Assistant, go to **Settings → Devices & services**
2. Select **Add Integration**
3. Search for **Wyoming Protocol**
4. Enter host: `<your-docker-host-ip>`
5. Enter port: `10200`

The service auto-discovers as `wyoming-s2cpp-tts` with voice `s2-pro` (en, zh), 44100 Hz, streaming=true.

## Assist pipeline

1. Go to Assist pipeline settings
2. Select `wyoming-s2cpp-tts` as the TTS engine
3. Save the pipeline
4. Test with "Try text-to-speech" in the integration settings

Expected behavior: Home Assistant sends a Wyoming streaming request (`synthesize-start` → `synthesize-chunk` x N → `synthesize-stop`), the wrapper synthesizes via the s2.cpp backend, emits `AudioStart` / `AudioChunk` / `AudioStop` / `synthesize-stopped`, and real speech plays through the selected media player.

## Known limitation: stock HA one-wake barge-in

**Stock Home Assistant 2026.7.2 with Voice PE firmware 26.6.0 and ESPHome 2026.6.0 does NOT pass full one-wake barge-in.** This is an external platform limitation, not a repository defect:

- Generic `media_player.media_stop` targets the normal media pipeline while Assist uses the announcement pipeline.
- HA keeps the TTS producer alive — no Wyoming disconnect or cancellation reaches this service.
- Full physical interruption plus producer cancellation is deferred to an announcement-aware upstream lifecycle or Cortex-Satellite.

The repository-owned disconnect cancellation, backend abort, scheduler cleanup, and recovery contracts **all pass** — the gap is in how stock HA signals interruption to the TTS service. See `docs/validation/PHASE_10_CLOSURE.md` for the full evidence.

## Available voice profiles

Six custom `.s2voice` profiles were created from CMU ARCTIC reference recordings
and verified via direct backend synthesis (Phase 7A). They are stored on the
Unraid host at `<your-voices-dir>`:

| Profile ID | Gender | Accent |
|-----------|--------|--------|
| `cmu_bdl_male_us` | male | US English |
| `cmu_rms_male_us` | male | US English |
| `cmu_jmk_male_canadian` | male | Canadian English |
| `cmu_slt_female_us` | female | US English |
| `cmu_clb_female_us` | female | US English |
| `cmu_eey_female_us` | female | US English |

**Voice selection in Home Assistant is wired (Phase 7B).** The wrapper
discovers `.s2voice` profiles from `/voices` and advertises them through
Wyoming Describe. Select a voice in Home Assistant TTS settings — the selected
voice is forwarded to the backend as `voice` and `voice_dir` multipart fields.

**Drop-in discovery:** New `.s2voice` files placed in your voices directory
are discoverable without rebuilding or restarting the wrapper container.
However, Home Assistant may cache Wyoming Describe results. To see a newly
dropped-in voice in the HA UI:

1. Go to **Settings → Devices & services → Wyoming Protocol**.
2. Select the `wyoming-s2cpp-tts` integration.
3. Choose **Reload** from the three-dot menu.

This reloads the cached voice list — the wrapper itself does not need a restart.

Human listening assessment: all six voices are acceptable as temporary assistant
voices but sound somewhat robotic. This is not a confirmed downstream defect;
perceived quality may be influenced by the older CMU ARCTIC recordings, the
short reference clip, and the model quantization. A personal clean recording is
planned for later as a better quality test.

## Streaming status

Wyoming protocol streaming is implemented and verified: the wrapper handles `synthesize-start`, `synthesize-chunk`, and `synthesize-stop`, then emits `AudioStart`, `AudioChunk`, `AudioStop`, and `synthesize-stopped` for Home Assistant.

Progressive backend-audio streaming is wired (Phase 7.5A). When `S2_STREAM=true` (configured in `compose.yaml` or Unraid), the handler yields Wyoming audio events progressively as backend transport chunks arrive instead of buffering the complete response. When `S2_STREAM=false`, the buffered path is preserved.

Backend cancellation (Phase 8B2) is production-promoted: deliberate disconnects stop abandoned synthesis promptly and release backend busy state.

## Backup and rollback

Before upgrading or making configuration changes:

```bash
# Back up Home Assistant (via HA UI: Settings → System → Backups)
# Back up TTS data directories
cp -a /mnt/user/appdata/s2cpp/voices /mnt/user/appdata/s2cpp/voices.bak
cp -a /mnt/user/appdata/s2cpp/models /mnt/user/appdata/s2cpp/models.bak
```

For full upgrade and rollback procedures, see `docs/UPGRADE_ROLLBACK.md`.

## Troubleshooting

### Spinner hangs on preview

Fixed as of Phase 6C. The handler supports the full Wyoming streaming TTS lifecycle. Ensure you are running a wrapper image from v0.1.0 or later.

### No audio / connection errors

- Verify both containers are running on the private Docker bridge network (`s2cpp-net`).
- Verify the wrapper image is pinned to a v0.1.0 `sha-*` tag.
- Verify the backend image is pinned to a v0.1.0 `sha-*` tag.
- Check that the backend is reachable from the wrapper at `http://s2cpp-backend:3030/generate`.
- Check wrapper logs for `S2ClientError`; JSON 400 errors indicate an old wrapper path that is not using multipart/form-data.

### Voice not found

Custom voice selection is implemented as of Phase 7B. If a voice does not appear
in Home Assistant:

- Verify the `.s2voice` file exists on the host in your voices directory.
- Reload the Wyoming Protocol integration in Home Assistant
  (Settings → Devices & services → Wyoming Protocol → three-dot menu → Reload).
- Check wrapper logs for startup voice discovery messages.
- If `S2_DEFAULT_VOICE` is configured, verify it matches a discovered profile ID.

## Related documents

- `docs/INSTALL.md` — full installation instructions
- `docs/UNRAID_INSTALL.md` — Unraid-specific deployment notes
- `docs/UPGRADE_ROLLBACK.md` — upgrade and rollback procedures
- `docs/SECURITY.md` — security posture and network model
- `docs/validation/PHASE_10_CLOSURE.md` — barge-in validation evidence

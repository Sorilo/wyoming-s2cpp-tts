# Home Assistant setup

## Verified deployment

The Wyoming TTS service is deployed as two Docker containers on the Unraid `sorilonet` network:

- **Wrapper:** `wyoming-s2cpp-tts` (CPU-only), image `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-89ed2dc`, Wyoming port 10200
- **Backend:** `s2cpp-backend` (CUDA), image `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b`, HTTP port 3030

The wrapper is exposed to LAN at `192.168.1.45:10200`. Home Assistant runs at `192.168.1.233`.

## Add the Wyoming integration

1. In Home Assistant, go to **Settings \u2192 Devices & services**
2. Select **Add Integration**
3. Search for **Wyoming Protocol**
4. Enter host: `192.168.1.45`
5. Enter port: `10200`

The service auto-discovers as `wyoming-s2cpp-tts` with voice `s2-pro` (en, zh), 44100 Hz, streaming=true.

## Assist pipeline

1. Go to Assist pipeline settings
2. Select `wyoming-s2cpp-tts` as the TTS engine
3. Save the pipeline
4. Test with "Try text-to-speech" in the integration settings

Expected behavior: Home Assistant sends a Wyoming streaming request (`synthesize-start` \u2192 `synthesize-chunk` x N \u2192 `synthesize-stop`), the wrapper synthesizes via the s2.cpp backend, emits `AudioStart` / `AudioChunk` / `AudioStop` / `synthesize-stopped`, and real speech plays through the selected media player.

## Available voice profiles

Six custom `.s2voice` profiles were created from CMU ARCTIC reference recordings
and verified via direct backend synthesis (Phase 7A). They are stored on the
Unraid host at `/mnt/user/appdata/s2cpp/voices`:

| Profile ID | Gender | Accent |
|-----------|--------|--------|
| `cmu_bdl_male_us` | male | US English |
| `cmu_rms_male_us` | male | US English |
| `cmu_jmk_male_canadian` | male | Canadian English |
| `cmu_slt_female_us` | female | US English |
| `cmu_clb_female_us` | female | US English |
| `cmu_eey_female_us` | female | US English |

**Voice selection in Home Assistant is now wired (Phase 7B).**  The wrapper
discovers ``.s2voice`` profiles from ``/voices`` and advertises them through
Wyoming Describe.  Select a voice in Home Assistant TTS settings — the selected
voice is forwarded to the backend as ``voice`` and ``voice_dir`` multipart fields.

**Drop-in discovery:** New ``.s2voice`` files placed in
``/mnt/user/appdata/s2cpp/voices`` are discoverable without rebuilding or
restarting the wrapper container.  However, Home Assistant may cache Wyoming
Describe results.  To see a newly dropped-in voice in the HA UI:

1. Go to **Settings → Devices & services → Wyoming Protocol**.
2. Select the ``wyoming-s2cpp-tts`` integration.
3. Choose **Reload** from the three-dot menu.

This reloads the cached voice list — the wrapper itself does not need a restart.

Human listening assessment: all six voices are acceptable as temporary assistant
voices but sound somewhat robotic. This is not a confirmed downstream defect;
perceived quality may be influenced by the older CMU ARCTIC recordings, the
short reference clip, and the model quantization. A personal clean recording is
planned for later as a better quality test.

## Verified behavior (2026-07-08)

- HA discovers the Wyoming service at `192.168.1.45:10200`
- `s2-pro` voice appears in TTS settings
- "Try text-to-speech" generates and audibly plays real speech
- Wyoming streaming TTS lifecycle completes (`synthesize-stopped` emitted)
- Full STT \u2192 conversation \u2192 TTS satellite workflow not yet verified
- Cancellation and barge-in not yet tested

## Streaming caveat

Wyoming protocol streaming is implemented and verified (see above).

Progressive backend-audio streaming is now wired (Phase 7.5A). When `S2_STREAM=true` (already configured in Unraid), the handler yields Wyoming audio events progressively as backend transport chunks arrive instead of buffering the complete response. When `S2_STREAM=false`, the buffered path is preserved.

Note: previous real-backend measurements showed time-to-first-audio at ~3.8 s. Phase 7.5A does not guarantee a major latency reduction; measure live latency after deploying the new wrapper image (Phase 7.5B).

## Troubleshooting

### Spinner hangs on preview

Fixed in wrapper image `sha-89ed2dc`. The handler supports the full Wyoming streaming TTS lifecycle. Older images only handled the legacy `synthesize` event and ignored streaming events.

### No audio / connection errors

- Verify both containers are running on the `sorilonet` network.
- Verify the wrapper image is pinned to `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-89ed2dc` or a newer intentionally tested immutable tag.
- Verify the backend image is pinned to `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b` or a newer intentionally tested immutable tag.
- Check that the backend is reachable from the wrapper at `http://s2cpp-backend:3030/generate`.
- Check wrapper logs for `S2ClientError`; JSON 400 errors indicate an old wrapper path that is not using multipart/form-data.

### Voice not found

Custom voice selection is implemented as of Phase 7B. If a voice does not appear
in Home Assistant:

- Verify the ``.s2voice`` file exists on the host at
  ``/mnt/user/appdata/s2cpp/voices/<profile_id>.s2voice``.
- Reload the Wyoming Protocol integration in Home Assistant
  (Settings → Devices & services → Wyoming Protocol → three-dot menu → Reload).
- Check wrapper logs for startup voice discovery messages.
- If ``S2_DEFAULT_VOICE`` is configured, verify it matches a discovered profile ID.

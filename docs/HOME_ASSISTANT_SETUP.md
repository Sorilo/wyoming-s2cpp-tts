# Home Assistant setup

## Verified deployment

The Wyoming TTS service is deployed as two Docker containers on the Unraid `sorilonet` network:

- **Wrapper:** `wyoming-s2cpp-tts` (CPU-only), image `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-89ed2dc`, Wyoming port 10200
- **Backend:** `s2cpp-backend` (CUDA), image `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b`, HTTP port 3030

The wrapper is exposed to LAN at `192.168.1.45:10200`. Home Assistant runs at `192.168.1.233`.

## Add the Wyoming integration

1. In Home Assistant, go to **Settings → Devices & services**
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

Expected behavior: Home Assistant sends a Wyoming streaming request (`synthesize-start` → `synthesize-chunk` x N → `synthesize-stop`), the wrapper synthesizes via the s2.cpp backend, emits `AudioStart` / `AudioChunk` / `AudioStop` / `synthesize-stopped`, and real speech plays through the selected media player.

## Verified behavior (2026-07-08)

- HA discovers the Wyoming service at `192.168.1.45:10200`
- `s2-pro` voice appears in TTS settings
- "Try text-to-speech" generates and audibly plays real speech
- Wyoming streaming TTS lifecycle completes (`synthesize-stopped` emitted)
- Full STT → conversation → TTS satellite workflow not yet verified
- Cancellation and barge-in not yet tested

## Streaming caveat

Wyoming protocol streaming is implemented and verified: the wrapper handles `synthesize-start`, `synthesize-chunk`, and `synthesize-stop`, then emits `AudioStart`, `AudioChunk`, `AudioStop`, and `synthesize-stopped` for Home Assistant.

Progressive backend-audio streaming is not currently used by the production handler: although `S2_STREAM` is parsed and `synthesize_s2cpp_streaming_tts_events()` / `generate_stream()` exist, the live handler still calls buffered `synthesize_s2cpp_tts_events()` via `generate_multipart()`, then sends Wyoming audio events.

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

Custom voice selection is not implemented in the wrapper yet. Saved `.s2voice` profile creation and direct backend verification are Phase 7A; wrapper discovery and Home Assistant selectable voices are Phase 7B. Until then, expect only the generic `s2-pro` voice in Home Assistant.

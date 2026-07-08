# Home Assistant setup

## Verified deployment

The Wyoming TTS service is deployed as two Docker containers on the Unraid
`sorilonet` network:

- **Wrapper:** `wyoming-s2cpp-tts` (CPU-only) on port 10200
- **Backend:** `s2cpp-backend` (CUDA) on port 3030

The wrapper is exposed to LAN at `192.168.1.45:10200`.

## Add the Wyoming integration

1. In Home Assistant, go to **Settings → Devices & services**
2. Select **Add Integration**
3. Search for **Wyoming Protocol**
4. Enter host: `192.168.1.45`
5. Enter port: `10200`

The service auto-discovers as `wyoming-s2cpp-tts` with voice `s2-pro`
(en, zh), 44100 Hz, streaming=true.

## Assist pipeline

1. Go to Assist pipeline settings
2. Select `wyoming-s2cpp-tts` as the TTS engine
3. Save the pipeline
4. Test with "Try text-to-speech" in the integration settings

Expected behavior: Home Assistant sends a streaming request
(synthesize-start -> synthesize-chunk x N -> synthesize-stop), the
wrapper synthesizes via the s2.cpp backend, and real speech plays
through the selected media player.

## Verified behavior (2026-07-08)

- HA discovers the Wyoming service at 192.168.1.45:10200
- s2-pro voice appears in TTS settings
- "Try text-to-speech" generates and audibly plays real speech
- Streaming TTS lifecycle complete (synthesize-stopped emitted)
- Full STT -> conversation -> TTS satellite workflow not yet verified
- Cancellation and barge-in not yet tested

## Troubleshooting

### Spinner hangs on preview
Fixed in wrapper image `sha-89ed2dc`. The handler now supports the full
streaming TTS lifecycle. Older images only handled the legacy `synthesize`
event and ignored streaming events.

### No audio / connection errors
- Verify both containers are running on the `sorilonet` network
- Check that the backend is reachable from the wrapper:
  `docker exec wyoming-s2cpp-tts curl -s http://s2cpp-backend:3030/generate`
- Check wrapper logs for S2ClientError (JSON 400 = multipart not enabled,
  update to sha-15884c0 or later)

### Voice not found
Voice profiles are stored under `/voices` on the backend container.
Ensure the host directory is mounted and populated.

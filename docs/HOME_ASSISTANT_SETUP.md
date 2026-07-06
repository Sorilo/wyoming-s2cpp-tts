# Home Assistant setup draft

This guide is for the future working service. The current repository scaffold does not yet run a real Wyoming TTS server.

## Add the Wyoming integration

1. In Home Assistant, go to **Settings -> Devices & services**.
2. Select **Add Integration**.
3. Search for **Wyoming Protocol**.
4. Enter the host/IP of the Unraid server running this container.
5. Enter port `10200`.

## Assist pipeline

After Home Assistant discovers or connects to the Wyoming service:

1. Go to the Assist pipeline settings.
2. Select this Wyoming service as the TTS engine.
3. Save the pipeline.
4. Test with a short phrase, for example:

```text
Hello from the local Fish Speech S2 Pro Wyoming TTS service.
```

## Barge-in note

This TTS wrapper should eventually be barge-in friendly by streaming chunks early and cancelling synthesis on disconnect. However, true barge-in also depends on Home Assistant, wake word handling, VAD, microphone capture, satellite behavior, and whether the playback device can be interrupted.

If playback cannot be interrupted by the satellite or media device, changing only this TTS service will not provide full barge-in.

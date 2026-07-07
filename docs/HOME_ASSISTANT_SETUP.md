# Home Assistant setup draft

This guide is for validating the current Wyoming boundary and, later, the real Fish Speech S2 Pro service. The current repository can run a Wyoming TTS server with the safe default `TTS_BACKEND=fake`; that returns deterministic test-tone audio, not real speech. Real s2.cpp inference, progressive streaming, cancellation/barge-in validation, and final Home Assistant end-to-end latency measurement are not implemented yet.

## Add the Wyoming integration

1. Start the service in fake mode:

   ```bash
   python -m app.main
   ```

2. In Home Assistant, go to **Settings -> Devices & services**.
3. Select **Add Integration**.
4. Search for **Wyoming Protocol**.
5. Enter the host/IP of the machine running this service.
6. Enter port `10200`.

## Assist pipeline

After Home Assistant discovers or connects to the Wyoming service:

1. Go to the Assist pipeline settings.
2. Select this Wyoming service as the TTS engine.
3. Save the pipeline.
4. Test with a short phrase, for example:

```text
Hello from the local Wyoming test TTS service.
```

Expected current behavior in fake mode: Home Assistant receives deterministic test-tone audio. Do not treat this as real Fish Speech, s2.cpp, streaming, audio-quality, cancellation, or latency validation.

## Future real-backend validation

Real Home Assistant validation belongs after the approved streaming/cancellation/backend phases. The final end-to-end test should document, where measurable:

- Assist pipeline connection.
- STT-to-conversation-to-TTS operation.
- streamed playback.
- audio correctness.
- cancellation/barge-in behavior where supported.
- TTS-side and end-to-end latency timestamps.

## Barge-in note

This TTS wrapper should eventually be barge-in friendly by streaming chunks early and cancelling synthesis on disconnect. However, true barge-in also depends on Home Assistant, wake word handling, VAD, microphone capture, satellite behavior, and whether the playback device can be interrupted.

If playback cannot be interrupted by the satellite or media device, changing only this TTS service will not provide full barge-in.

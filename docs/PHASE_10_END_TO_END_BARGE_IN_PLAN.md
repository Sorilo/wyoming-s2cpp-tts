# Phase 10: End-to-End Barge-In — Planning Document

**Status**: Planning-only. No code or production changes.
**Branch**: `planning/phase-10-end-to-end-barge-in`
**Base**: `origin/main` (`ec633bdd2596cd6fa8865b62c34720163fc185b9`)
**Production**: Wrapper `sha-7db26b7`, Backend `sha-6e629d0` (Phase 9C and 9.5 not deployed)

---

## 1. Layer Separation

Barge-in requires four distinct, independently verifiable operations. Each layer has
a different owner, different observable signals, and different failure modes.

| # | Operation | Owner | Observable | Cancellable by wrapper? |
|---|-----------|-------|------------|------------------------|
| 1 | HA cancels / supersedes current Assist pipeline | Home Assistant AssistPipeline / AssistSatelliteEntity | Pipeline task is cancelled; new pipeline starts | No — passive observer only |
| 2 | Wyoming client connection or synthesis request is cancelled | Wyoming protocol / HA Wyoming integration | `AsyncTcpClient` disconnect or close; `SynthesizeStopped` ordering | Yes — detects disconnect, write failure, or explicit cancellation |
| 3 | Wrapper cancels queued/active backend work and releases scheduler | `SpeechScheduler` / `StreamingCoordinator` | Scheduler depth/pending/active return to zero; backend stream closes | Yes — owns cancellation primitives |
| 4 | Satellite/media-player stops buffered physical playback | Satellite firmware (ESPHome) / HA media_player | Speaker buffer drained or stopped; microphone re-enabled | No — cannot observe or control directly |

**Critical insight**: Operations 3 and 4 are separate. Service-side synthesis cancellation
(operation 3) **does not prove** audible playback stopped (operation 4). The wrapper can
only observe operation 4 indirectly through HA entity state changes, timing gaps, or
operator confirmation.

---

## 2. Exact Known Home Assistant Assist Pipeline Sequence

Based on source-code inspection of `AssistSatelliteEntity` (Home Assistant core,
`homeassistant/components/assist_satellite/entity.py`):

### 2.1 Pipeline Lifecycle

```
User speaks wake word
  ↓
[Satellite detects wake word locally]
  ↓
Satellite calls async_accept_pipeline_from_satellite(audio_stream, start_stage=STT, ...)
  ↓
async_accept_pipeline_from_satellite:
  1. await self._cancel_running_pipeline()       ← CANCELS any prior pipeline task
  2. Creates chat session
  3. Creates _pipeline_task = async_pipeline_from_audio_stream(...)
  4. Awaits _pipeline_task
  5. Finally: _pipeline_task = None
  ↓
async_pipeline_from_audio_stream:
  - STT stage: audio stream → speech-to-text
  - INTENT stage: text → conversation agent → response text
  - TTS stage: response text → TTS audio (Wyoming provider)
  ↓
PipelineEvent callbacks → _internal_on_pipeline_event:
  - WAKE_WORD_START → IDLE (unless RESPONDING)
  - STT_START → LISTENING
  - INTENT_START → PROCESSING
  - TTS_START → RESPONDING
  - RUN_END → IDLE (if no TTS occurred)
  ↓
TTS audio is streamed to satellite/media_player
  ↓
Satellite calls tts_response_finished() → IDLE
```

### 2.2 Cancellation Mechanism (Home Assistant Side)

**`_cancel_running_pipeline()`** (line 547–554 of entity.py):
```python
async def _cancel_running_pipeline(self) -> None:
    if self._pipeline_task is not None:
        self._pipeline_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._pipeline_task
        self._pipeline_task = None
```

This is called **before every new pipeline** (`async_accept_pipeline_from_satellite`,
`async_internal_announce`, `async_internal_start_conversation`,
`async_internal_ask_question`).

**Implication**: When a user speaks a new wake word during TTS playback, HA cancels
the running pipeline task. The TTS Wyoming connection will receive a disconnect
(or the pipeline's TTS stage will be cancelled), which propagates to the wrapper
as a client disconnect or write failure.

### 2.3 Wyoming Integration TTS Behaviour

Based on `WyomingTtsProvider` (`homeassistant/components/wyoming/tts.py`):

- **Buffered path** (`async_get_tts_audio`): Opens `AsyncTcpClient`, writes
  `Synthesize` event, reads audio events until `AudioStop`, returns WAV data.
  No explicit cancellation; relies on HA pipeline task cancellation to close
  the connection.

- **Streaming path** (`async_stream_tts_audio`): Connects, creates background
  task for writing text chunks, yields audio bytes via `data_gen()`.
  `data_gen()` disconnects on finally. Pipeline cancellation should trigger
  `data_gen()` cleanup via generator closure.

**Wyoming TTS protocol events** (from `wyoming.tts`):
- `Synthesize`, `SynthesizeStart`, `SynthesizeChunk`, `SynthesizeStop` (client→server)
- `AudioStart`, `AudioChunk`, `AudioStop`, `SynthesizeStopped` (server→client)

**There is no explicit Wyoming TTS cancel event.** Cancellation happens at the
transport layer (TCP disconnect) or via HA asyncio task cancellation which
triggers `AsyncTcpClient` cleanup.

### 2.4 ESPHome Voice Assistant Behaviour

Based on `voice_assistant.cpp` (ESPHome source):

**States**: IDLE → START_MICROPHONE → STARTING_MICROPHONE → START_PIPELINE →
STARTING_PIPELINE → STREAMING_MICROPHONE → STOP_MICROPHONE → STOPPING_MICROPHONE →
AWAITING_RESPONSE → STREAMING_RESPONSE → RESPONSE_FINISHED

**Playback paths**:
1. **USE_SPEAKER (direct I2S speaker)**: Audio arrives via `on_audio()` callback,
   buffered in `speaker_buffer_`, written to hardware via `speaker_->play()`.
   Stream ends via TTS_STREAM_END event → `stream_ended_=true` → drains buffer →
   `speaker_->stop()`. **Stop mechanism**: `speaker_->stop()` is called after
   buffer drain in RESPONSE_FINISHED state.

2. **USE_MEDIA_PLAYER (URL-based)**: TTS response URL received in TTS_END or
   INTENT_PROGRESS events. `media_player_->make_call().set_media_url(url)
   .set_announcement(true).perform()`. **Stop mechanism**:
   `media_player_->make_call().set_command(MEDIA_PLAYER_COMMAND_STOP)
   .set_announcement(true).perform()` — called in `request_stop()` when
   state is STREAMING_RESPONSE.

**Key observations for barge-in**:
- ESPHome `request_stop()` (line 690–733) handles STREAMING_RESPONSE state:
  stops media player if USE_MEDIA_PLAYER, and sends `signal_stop_()` (a
  `VoiceAssistantRequest{start=false}` message) if TTS streaming hasn't reached
  TTS_END yet.
- For speaker path (USE_SPEAKER), request_stop() in STREAMING_RESPONSE state
  appears to do **nothing** — the stream continues until TTS_STREAM_END.
  **UNKNOWN**: Whether the satellite can actually stop the speaker mid-stream
  before TTS_STREAM_END arrives.
- The satellite can start a new pipeline while still in STREAMING_RESPONSE
  via `request_start()` (continuous mode) or a new wake word.

### 2.5 Unknowns Requiring Target Discovery

| # | Unknown | How to Resolve |
|---|---------|----------------|
| U1 | **Satellite hardware type**: ESPHome device model? ESP32-S3-BOX-3? M5Stack? Custom? | Check HA Devices page; query entity registry |
| U2 | **Player interface**: USE_SPEAKER or USE_MEDIA_PLAYER or both? | Check ESPHome YAML config; observe entity state during playback |
| U3 | **Wake word provider**: microWakeWord on-device? openWakeWord HA-side? Custom? | Check HA Assist pipeline configuration |
| U4 | **STT provider**: Faster-Whisper? Whisper? Wyoming? Cloud? | Check HA Assist pipeline configuration |
| U5 | **Conversation agent**: HA built-in? OpenAI? Custom? | Check HA Assist pipeline configuration |
| U6 | **TTS provider**: wyoming-s2cpp-tts (this wrapper)? Something else? | Check HA Assist pipeline configuration |
| U7 | **VAD location**: Satellite on-device? HA server-side? Both? | Check pipeline config; ESPHome YAML `silence_detection` flag |
| U8 | **AEC ownership**: Does the satellite have hardware AEC? Software AEC? None? | Check ESPHome YAML; check if mic is muted during playback |
| U9 | **Noise suppression**: Satellite-side? HA-side? Both? | Check ESPHome YAML `noise_suppression_level` |
| U10 | **Full duplex capability**: Can the satellite listen while playing? | Test: trigger wake word during TTS playback; observe if mic is live |
| U11 | **Audio buffering layers**: How many buffers between wrapper PCM output and speaker cone? | Trace: wrapper → Wyoming TCP → HA → satellite API → speaker buffer → I2S DAC |
| U12 | **Playback stop latency**: If we cancel synthesis, how long until speaker stops? | Measure audibly; correlate with logs |
| U13 | **New TTS stream replaces current?**: Does a new TTS stream automatically stop prior playback? | Test: send new TTS while playing; observe |
| U14 | **Wake word during playback**: Does the satellite detect a wake word while TTS is playing? | Test: speak wake word during playback; observe satellite state |
| U15 | **HA pipeline debug traces**: Are pipeline traces enabled? What do they show during barge-in? | Enable `debug` logging for `assist_pipeline`; capture trace |

---

## 3. Validation State Model

This model defines **observable** lifecycle states for one logical voice response.
It is a validation tool — NOT a production state machine to add to the codebase
unless implementation evidence proves it is needed.

### 3.1 States

| State | Layer | Definition |
|-------|-------|------------|
| `REQUESTED` | HA pipeline | User spoke wake word; pipeline task created |
| `QUEUED` | Wrapper scheduler | Synthesis request admitted to FIFO queue |
| `SYNTHESIZING` | Wrapper/Backend | Backend actively generating audio |
| `STREAMING` | Wrapper→HA | AudioStart emitted; PCM chunks flowing |
| `PLAYING` | Satellite | Speaker/media_player actively producing sound |
| `INTERRUPT_REQUESTED` | HA pipeline | New wake word detected; `_cancel_running_pipeline()` called |
| `SYNTHESIS_CANCELLED` | Wrapper | Scheduler cancelled; backend stream closed |
| `PLAYBACK_STOP_REQUESTED` | Satellite | Stop command sent to speaker/media_player |
| `PLAYBACK_STOPPED` | Satellite | Speaker silent; buffer drained |
| `REPLACED` | HA pipeline | Replacement pipeline running; new TTS flowing |
| `COMPLETED` | Any | Normal end-to-end completion |
| `FAILED` | Any | Terminal error (timeout, backend error, etc.) |

### 3.2 State Transitions

```
REQUESTED ──────────────────────────────────────────────────────────────┐
  │                                                                      │
  ▼                                                                      │
QUEUED ──► SYNTHESIZING ──► STREAMING ──► PLAYING ──► COMPLETED        │
  │            │                │             │            ▲             │
  │            │                │             │            │             │
  ▼            ▼                ▼             ▼            │             │
  ├── INTERRUPT_REQUESTED ◄─────┴─────────────┘            │             │
  │            │                                            │             │
  │            ▼                                            │             │
  │     SYNTHESIS_CANCELLED ───────────────► REPLACED        │             │
  │            │       (if no audio reached player)          │             │
  │            ▼                                            │             │
  │     PLAYBACK_STOP_REQUESTED                             │             │
  │            │                                            │             │
  │            ▼                                            │             │
  │     PLAYBACK_STOPPED ───────► REPLACED ─────────────────┘             │
  │                                                                       │
  └──► FAILED (any state can transition to FAILED on error/timeout)       │
```

### 3.3 Valid Transitions

- **REQUESTED → QUEUED**: Scheduler admits the synthesis request
- **QUEUED → SYNTHESIZING**: Scheduler activates; backend request starts
- **SYNTHESIZING → STREAMING**: First AudioStart/AudioChunk emitted
- **STREAMING → PLAYING**: Satellite begins audible output
- **PLAYING → COMPLETED**: Normal end of TTS response; pipeline ends
- **Any non-terminal → INTERRUPT_REQUESTED**: New wake word; pipeline cancelled
- **INTERRUPT_REQUESTED → SYNTHESIS_CANCELLED**: Wrapper observes disconnect/cancellation
- **SYNTHESIS_CANCELLED → REPLACED**: Original request was cancelled before any audio reached the player; no playback stop exists to request
- **SYNTHESIS_CANCELLED → PLAYBACK_STOP_REQUESTED**: Audio may have reached the player, so the proven satellite/player stop mechanism is invoked
- **PLAYBACK_STOP_REQUESTED → PLAYBACK_STOPPED**: Speaker silent
- **PLAYBACK_STOPPED → REPLACED**: New response begins
- **REPLACED → COMPLETED**: Replacement response finishes normally
- **Any → FAILED**: Timeout, backend error, network failure

### 3.4 Critical Distinctions

The model separates:
- **Synthesis state** (QUEUED, SYNTHESIZING, SYNTHESIS_CANCELLED) — owned by scheduler
- **Transport state** (STREAMING) — owned by Wyoming handler
- **Player state** (PLAYING, PLAYBACK_STOP_REQUESTED, PLAYBACK_STOPPED) — owned by satellite
- **Replacement-request state** (INTERRUPT_REQUESTED, REPLACED) — owned by HA pipeline

**The wrapper can observe synthesis and transport states directly. Player state
requires indirect observation** (HA entity state changes, timing, operator confirmation).

---

## 4. Correlation and Monotonic Timestamps

### 4.1 Correlation Scheme

Design a bounded correlation scheme tracing one response through the full path.
Use existing sanitized IDs where possible. Do NOT expose: synthesis plaintext,
raw audio, secrets, tokens, authorization headers, unbounded IDs as metric
labels, or task/future representations.

| Layer | Identifier | Source | Sanitization |
|-------|-----------|--------|--------------|
| HA pipeline | `pipeline_run_id` | HA `pipeline_run` event data | Already a UUID — safe |
| HA conversation | `conversation_id` | HA `chat_session` | Already a ULID — safe |
| Wyoming connection | `conn_id` | Wrapper `WyomingTcpServer` | Auto-generated short ID |
| Wrapper synthesis | `synthesis_id` | `SpeechRequest.id` | Auto-generated short ID |
| Backend request | `backend_request_id` | Wrapper generates UUID | Safe UUID |
| Satellite | `entity_id` | HA entity registry (e.g. `assist_satellite.esp32_s3_box_3`) | Already HA entity ID |

**Correlation chain**: `pipeline_run_id` → `conn_id` → `synthesis_id` → `backend_request_id`

The wrapper can only observe `conn_id` → `synthesis_id` → `backend_request_id`.
HA-side correlation requires post-hoc log matching via timestamps.

### 4.2 Monotonic Timestamps

Record monotonic (`time.monotonic()`) timestamps for at minimum:

| # | Event | Source | Observable by wrapper? |
|---|-------|--------|------------------------|
| T1 | Wake word detected | Satellite log / HA event | No (indirectly via new connection) |
| T2 | VAD speech start | Satellite / HA event | No |
| T3 | Assist pipeline start | HA `pipeline_run` event | No (unless correlated) |
| T4 | TTS request accepted | Wrapper `conn_created` / `synthesize_received` | **Yes** |
| T5 | Scheduler admission | Wrapper `scheduler_admitted` | **Yes** |
| T6 | Backend synthesis start | Wrapper `backend_request_start` | **Yes** |
| T7 | First PCM produced | Wrapper `backend_stream_first_audio` | **Yes** |
| T8 | First audio sent | Wrapper `first_wyoming_audio` | **Yes** |
| T9 | Physical playback start | Satellite / HA media_player state | No* |
| T10 | User interruption speech start | Satellite VAD / wake word event | No* |
| T11 | Cancellation received by wrapper | Wrapper `conn_closed` / task cancelled | **Yes** |
| T12 | Active synthesis cancellation | Wrapper `synthesis_cancelled` | **Yes** |
| T13 | Backend stream closure | Wrapper `backend_stream_closed` | **Yes** |
| T14 | Scheduler release | Wrapper `scheduler_released` | **Yes** |
| T15 | Playback-stop command | Satellite / HA service call | No* |
| T16 | Physical playback stop | Satellite speaker state / audible silence | No* |
| T17 | Replacement request admission | Wrapper `scheduler_admitted` (new synthesis_id) | **Yes** |
| T18 | Replacement first audio | Wrapper `first_wyoming_audio` (new synthesis_id) | **Yes** |

\* Observable indirectly via HA state changes, operator confirmation, or
audible measurement with a stopwatch.

### 4.3 Latency Measurements

| Measurement | Formula | Requires |
|-------------|---------|----------|
| Wrapper cancellation latency | T12 − T11 | Wrapper logs only |
| Synthesis-to-silence gap | T14 − T11 | Wrapper logs only |
| End-to-end barge-in latency | T17 − T10 | HA logs + operator timing |
| Playback stop latency | T16 − T11 (or T15) | Satellite logs + operator timing |
| Replacement response latency | T18 − T17 | Wrapper logs only |

---

## 5. Wrapper-Side Contract

### 5.1 What the Wrapper MUST Preserve

All Phase 9.5 behaviour unless a concrete Phase 10 defect requires change.

### 5.2 What the Wrapper MUST Do (Barge-In Requirements)

- Reject no valid replacement request due to stale ownership
- Cancel queued work exactly once
- Cancel active synthesis exactly once
- Close backend streams exactly once
- Release scheduler ownership exactly once
- Unblock any coordinator consumer
- Avoid pending tasks and unobserved exceptions
- Avoid persistent backend busy state
- Prevent the next phrase from starting after interruption
- Avoid stale audio chunks after cancellation
- Avoid false `AudioStop`
- Avoid false `SynthesizeStopped`
- Avoid duplicate controlled errors
- Allow the replacement request to enter FIFO normally
- Preserve Phase 9.5 phrase fairness and one-active-backend-operation invariant

### 5.3 Cancellation States to Verify

Verify cancellation in ALL of these states:

| # | State | Test Method |
|---|-------|-------------|
| 1 | Before scheduler admission | Submit request, cancel before scheduler processes it |
| 2 | While queued | Submit 3 requests, cancel the middle one |
| 3 | During backend request setup | Cancel during HTTP connection/request phase |
| 4 | Before first PCM | Cancel after backend request sent, before first audio |
| 5 | After AudioStart but before first AudioChunk | Cancel in the narrow window |
| 6 | During progressive PCM | Cancel mid-stream |
| 7 | Between phrases | Cancel between two progressive phrases |
| 8 | After final audio chunk but before terminal events | Cancel during drain |
| 9 | During graceful drain | Cancel while coordinator is draining |
| 10 | During client disconnect | Simulate TCP disconnect |

### 5.4 Wrapper Cancellation Primitives (Already Exist)

Based on source-code inspection:

| Primitive | Location | Function |
|-----------|----------|----------|
| `S2StreamResult.cancel()` | `app/s2_client.py:342` | Unblocks blocked `read()` workers |
| `SpeechScheduler.cancel_connection()` | `app/speech/scheduler.py:377` | Cancels all queued work for a connection |
| `SpeechScheduler.cancel_active_for_connection()` | `app/speech/scheduler.py:402` | Cancels active work for a connection |
| `SpeechScheduler.cancel_synthesis()` | `app/speech/scheduler.py:391` | Cancels by synthesis_id |
| `StreamingCoordinator.cancel()` | `app/speech/stream_coordinator.py:257` | Cancels pending, active, and consumer task |
| Wyoming handler `connection_closed()` | `app/wyoming_server.py` | Disconnect-triggered cleanup |
| Wyoming handler `_handle_task_exception()` | `app/wyoming_server.py` | Cancels stream on task exception |

**Configuration flags (from `app/config.py`)**:
- `CANCEL_ON_CLIENT_DISCONNECT = True` (default) — cancel queued/active on disconnect
- `CANCEL_ON_NEW_REQUEST = False` (default) — do NOT auto-cancel on new request
- `BARGE_IN_FRIENDLY = True` (default) — parsed but **no behavioral references found**

### 5.5 Automated Wrapper Tests (Planned)

Add deterministic tests BEFORE implementation changes. Use:
- Fake backend streams (already exist)
- `asyncio.Event` for synchronization
- Bounded waits (no arbitrary sleeps)
- Fake player/cancellation adapters (new, if needed)
- Direct coordinator calls
- Simulated disconnects
- Simulated replacement requests

Minimum test coverage:

| # | Test | Coverage |
|---|------|----------|
| T1 | Active progressive phrase cancellation | Cancel during active synthesis |
| T2 | Queued phrase cancellation | Cancel while queued, verify removal |
| T3 | Cancellation between phrases | Cancel between progressive phrases |
| T4 | Replacement request after cancellation | Verify new request admitted after cancel |
| T5 | Disconnect plus replacement race | Race disconnect with new request |
| T6 | Cancellation while draining | Cancel during coordinator drain |
| T7 | Cancellation before AudioStart | Cancel before first audio event |
| T8 | Cancellation after AudioStart | Cancel after AudioStart, before chunk |
| T9 | Generator closure | Verify `async_generator_aclose` called |
| T10 | Scheduler depth returns to zero | Verify queue drained after cancel |
| T11 | Pending count returns to zero | Verify pending=0 after cancel |
| T12 | Active count returns to zero | Verify active=0 after cancel |
| T13 | No stale events after cancellation | Verify no AudioChunk after cancel |
| T14 | No success terminals after interruption | Verify no AudioStop/SynthesizeStopped after cancel |
| T15 | No double release | Verify scheduler release exactly once |
| T16 | No duplicate error | Verify error events not duplicated |
| T17 | No unobserved task exception | Verify clean task cleanup |
| T18 | Backend-busy recovery after interruption | Verify 503 recovery after cancel |

**Test suite requirement**: Run authoritative suite with zero failures:
```bash
.venv/bin/python -m pytest tests/ \
  --ignore=tests/test_realtime_tuning_unraid.py \
  -q -o addopts=
```
Unraid-specific suite reported separately.

---

## 6. Physical Playback Stop Contract

### 6.1 What We Know

Based on ESPHome `voice_assistant.cpp` source:

**USE_MEDIA_PLAYER path** (URL-based playback):
- **Stop command exists**: `media_player_->make_call().set_command(MEDIA_PLAYER_COMMAND_STOP).set_announcement(true).perform()`
- **When called**: In `request_stop()` when state is `STREAMING_RESPONSE`
- **How triggered**: New wake word → `request_start()` → ... → eventually `request_stop()` on old pipeline? **UNKNOWN**
- **ESPHome stop via HA service**: `media_player.media_stop` service on the media_player entity should work. **To verify**.

**USE_SPEAKER path** (direct I2S):
- **Stop mechanism**: `speaker_->stop()` called in RESPONSE_FINISHED state, but only AFTER buffer is drained
- **Mid-stream stop**: `request_stop()` in STREAMING_RESPONSE state does NOT call `speaker_->stop()` when USE_SPEAKER
- **Implication**: If the target uses USE_SPEAKER, the wrapper cannot stop physical playback — only the stream ending can
- **Workaround**: May need HA automation to call `esphome.voice_assistant_stop` or similar service

### 6.2 Unknowns to Resolve (During Implementation)

| # | Question | Investigation Method |
|---|----------|---------------------|
| P1 | Which playback path does the target use? (USE_SPEAKER, USE_MEDIA_PLAYER, or both?) | Check ESPHome YAML; observe entity types in HA |
| P2 | Can ESPHome `voice_assistant_stop` service stop playback mid-stream? | Test via HA Developer Tools → Services |
| P3 | Does a new TTS URL automatically stop prior playback on the media_player? | Test: send new TTS while playing; observe |
| P4 | How much audio is buffered in the ESPHome voice-assistant speaker buffer (`SPEAKER_BUFFER_SIZE = 16 KiB`)? | For the source snapshot's 16 kHz mono s16le path: 16,384 / (16,000 × 2) ≈ 512 ms maximum buffer duration; verify the target build and measure actual drain |
| P5 | Is there buffering in HA's TTS pipeline, Wyoming client, or ALSA/PulseAudio? | Check HA TTS provider code; check satellite OS |
| P6 | Can the microphone hear the speaker (AEC quality)? | Test: play TTS, speak wake word, see if detected |
| P7 | Is the microphone muted during playback? | Test: observe mic state during TTS playback |

### 6.3 Stop Contract for Target Discovery

During implementation, execute this procedure to determine the physical stop
mechanism:

1. **Identify the player entity**:
   - Check HA Devices page for ESPHome voice assistant
   - Note: entity type (media_player, speaker), entity_id, supported features

2. **Test manual stop**:
   - Play a long TTS response
   - Call `media_player.media_stop` on the entity via Developer Tools
   - Observe: does audio stop immediately, after buffer drain, or not at all?

3. **Test new-stream replacement**:
   - Play a long TTS response
   - Immediately send another TTS request
   - Observe: does first audio stop? Is there overlap?

4. **Test wake-word during playback**:
   - Play a long TTS response
   - Speak wake word clearly near the device
   - Observe: does the satellite detect it? Does HA cancel the pipeline?

5. **Measure stop latency**:
   - Use a stopwatch: start at wake word utterance, stop at audible silence
   - Record 5 trials, compute average and range
   - Correlate with HA/wrapper logs

### 6.4 Evidence-Based Threshold-Setting Procedure

**Do NOT invent numbers.** Thresholds must be based on actual measurements.

Procedure:
1. Execute the 16-case live matrix (Section 7) with the target hardware
2. For each case, measure:
   - `t_cancel_wrapper`: Time from HA pipeline cancel to wrapper synthesis cancel (from logs)
   - `t_stop_audible`: Time from HA pipeline cancel to audible silence (stopwatch)
   - `t_replacement_first_audio`: Time from replacement request to first audio (from logs)
3. Calculate for each metric: mean, median, p95, min, max across all cases
4. Set acceptance thresholds at p95 + 20% margin (or 2× mean for stop_audible)
5. Document thresholds with supporting evidence in the implementation PR

**Provisional targets** (to be replaced by measured values):
- Wrapper cancellation observed within: **TBD ms** (from HA pipeline cancel)
- Physical playback stops within: **TBD ms** (from HA pipeline cancel)
- Replacement request begins within: **TBD ms** (from cancellation completion)

---

## 7. Live Validation Matrix (16 Cases)

### 7.1 Test Environment Setup

**Required before testing**:
1. Enable debug logging for `assist_pipeline`, `wyoming`, `assist_satellite` in HA
2. Enable `DEBUG` level in wrapper via `LOG_LEVEL=debug`
3. Capture wrapper logs: `docker logs -f wyoming-s2cpp-tts 2>&1 | tee p10_wrapper.log`
4. Capture HA logs: HA Supervisor → System → Logs → download
5. Have a stopwatch / second device for audible measurements

### 7.2 Test Matrix

For each case, record: exact user action, expected result, HA trace, wrapper
logs, backend logs, player state, audible observation, timestamps, pass/fail,
cleanup state, next-request recovery.

| # | Case | Procedure | Expected Result |
|---|------|-----------|-----------------|
| 1 | **Interrupt before first audio** | Send long TTS request; speak wake word immediately after (before audio starts) | Pipeline cancelled; no audio played; new pipeline processes wake word |
| 2 | **Interrupt immediately after first audio** | Send TTS request; speak wake word as soon as audio begins | Audio stops quickly; new pipeline starts; no stale audio |
| 3 | **Interrupt in the middle of a long phrase** | Send long TTS request; speak wake word mid-playback | Audio stops; pipeline cancelled; new response begins |
| 4 | **Interrupt between two progressive phrases** | Send long text (multi-sentence); speak wake word between sentences | Current phrase stops; remaining phrases cancelled; new response |
| 5 | **Interrupt near the end of playback** | Send TTS request; speak wake word just before response ends | Pipeline cancelled; minimal stale audio; new response |
| 6 | **New wake word while response is queued** | Send 3 TTS requests; speak wake word while first is still queued | All queued requests cancelled; new pipeline starts |
| 7 | **New wake word while backend is synthesizing** | Send TTS request; speak wake word during backend generation (before audio) | Synthesis cancelled; backend stream closed; new pipeline |
| 8 | **New wake word while audio is streaming** | Send TTS request; speak wake word during active audio streaming | Stream cancelled; new pipeline; no stale chunks |
| 9 | **Wake word while physical player drains buffered audio** | Send TTS; speak wake word after wrapper has stopped sending but speaker still has buffer | New pipeline starts; remaining buffer may still play (document behaviour) |
| 10 | **Repeated interruptions (3 cycles)** | Interrupt → new response → interrupt → new response → interrupt → new response | All 3 cycles recover cleanly; no resource leaks |
| 11 | **Two users speaking close together** | User A speaks, gets response; User B speaks wake word during response; then User A speaks again | Both interruptions handled; final response correct |
| 12 | **Wake word caused by device's own speaker audio** | Play TTS response that contains the wake word phrase | May or may not trigger false wake word (document behaviour; AEC test) |
| 13 | **Network disconnect during interruption** | Start TTS; disconnect satellite network during playback; reconnect; speak wake word | System recovers after reconnect; no persistent busy state |
| 14 | **Backend busy response during replacement** | Force backend to respond 503 during replacement request | Retry mechanism works; eventual success or controlled failure |
| 15 | **HA restart or pipeline failure during playback** | Restart HA core during TTS playback; verify recovery | System recovers after HA restart; no stale state |
| 16 | **Satellite reconnect after interruption** | Power-cycle satellite during TTS playback; reconnect; speak wake word | System recovers; new pipeline works normally |

### 7.3 Evidence Schema

For each test case, record the following in a structured evidence document
(`docs/PHASE_10_LIVE_VALIDATION_EVIDENCE.md`):

```markdown
### Case N: [Case Name]
- **Date/Time**: [ISO timestamp]
- **Operator**: [name]
- **User action**: [exact description of what was done]
- **Expected result**: [from matrix above]
- **Actual result**: PASS / FAIL / PARTIAL
- **HA pipeline trace**:
  - pipeline_run_id: [UUID]
  - pipeline events: [list]
  - cancellation observed: YES/NO/@time
- **Wrapper logs**:
  - conn_id: [ID]
  - synthesis_id: [ID]
  - cancellation received: YES/NO/@monotonic_time
  - synthesis cancelled: YES/NO/@monotonic_time
  - scheduler released: YES/NO/@monotonic_time
  - backend stream closed: YES/NO/@monotonic_time
  - replacement admitted: YES/NO/@monotonic_time
- **Backend logs**: [relevant lines]
- **Player state**: [media_player/speaker entity state before, during, after]
- **Audible observation**: [subjective description; did audio stop? how quickly?]
- **Timestamps**:
  - t_wake_word_utterance: [stopwatch or system time]
  - t_wrapper_cancel: [monotonic from logs]
  - t_audible_silence: [stopwatch]
  - t_replacement_start: [monotonic from logs]
  - t_replacement_first_audio: [monotonic from logs]
- **Cleanup state**: [queue depth, pending count, active count after test]
- **Next-request recovery**: [did next request work without manual reset?]
- **Notes**: [any observations, anomalies, limitations]
```

---

## 8. Safety and Operator Boundaries

### 8.1 What the Wrapper MUST NOT Do

- Do NOT attempt to control the satellite hardware directly
- Do NOT send Wyoming events that the protocol does not define
- Do NOT expose synthesis text, audio, credentials, or tokens in logs
- Do NOT mutate Home Assistant configuration
- Do NOT mutate production Docker containers or images
- Do NOT mutate Unraid templates

### 8.2 Home Assistant Configuration Changes (If Needed)

Any HA configuration changes discovered during target discovery MUST be:
- **Minimal**: Only what is proven necessary
- **Documented**: Exact change, reason, and reversibility
- **Reversible**: Provide exact rollback procedure
- **Separately approved**: Document in implementation PR before application

**No HA changes are permitted during the planning phase.**

### 8.3 Rollback

**Wrapper rollback**: Revert to production images (wrapper `sha-7db26b7`, backend `sha-6e629d0`) if:
- Any barge-in change causes regression in non-barge-in synthesis
- Any resource leak is introduced (tasks, streams, connections)
- Any test failure is introduced in the authoritative suite
- Live validation shows unacceptable behaviour

**Rollback procedure**:
1. Stop wrapper container
2. Update image tag to `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-7db26b7`
3. Start wrapper container
4. Verify with `/healthz` or direct TTS request
5. Restore any HA configuration changes

### 8.4 Stop Conditions

**Stop Phase 10 implementation immediately if**:
- Physical playback cannot be stopped by any available mechanism
- The satellite cannot detect wake words during TTS playback
- AEC is insufficient and the device hears itself, causing cascading barge-in loops
- Cancellation causes persistent backend 503/busy latch
- Cancellation introduces resource leaks that accumulate over cycles
- Phase 9.5 progressive synthesis behaviour regresses
- Any test failure is introduced

---

## 9. Acceptance Criteria

Phase 10 is complete only when ALL of the following are proven:

1. ✅ A user interruption can supersede an active response
2. ✅ Active wrapper synthesis is cancelled or allowed to terminate according to contract
3. ✅ Queued and pending phrases are removed
4. ✅ Backend stream closes
5. ✅ Scheduler ownership releases
6. ✅ No stale audio continues to be sent by the wrapper
7. ✅ Physical playback stops through a proven player mechanism
8. ✅ The replacement request begins without manual reset
9. ✅ No persistent 503/busy latch occurs
10. ✅ No duplicate audio or terminal events occur
11. ✅ No leaked tasks, streams, or connections remain
12. ✅ Repeated barge-in cycles recover cleanly
13. ✅ Phase 9.5 progressive synthesis remains intact when no interruption occurs
14. ✅ Rollback is documented and tested or dry-run verified
15. ✅ All automated wrapper tests pass (zero failures)
16. ✅ Live test matrix executed with recorded evidence
17. ✅ Acceptance thresholds set based on measured evidence (not invented)

---

## 10. Implementation Decision Gates

| Gate | When | Decision |
|------|------|----------|
| G1 | After planning doc review | Approve plan; merge planning PR |
| G2 | After target discovery | Determine: USE_SPEAKER vs USE_MEDIA_PLAYER; confirm stop mechanism |
| G3 | After wrapper test implementation | All 18+ cancellation tests pass; zero regressions |
| G4 | After live matrix execution | All 16 cases pass or have documented limitations |
| G5 | After evidence review | Thresholds established from measurements |
| G6 | Before merge | All acceptance criteria met; rollback verified |
| G7 | Separate deployment gate | (Not in Phase 10 scope) Image publication and deployment |

### Gate G2 Decision Tree

```
Target Discovery Complete
  │
  ├─ USE_MEDIA_PLAYER path confirmed
  │    └─ media_player.media_stop works → PROCEED with media_player approach
  │
  ├─ USE_SPEAKER path confirmed, speaker_->stop() accessible
  │    └─ Can stop mid-stream → PROCEED with speaker approach
  │
  ├─ USE_SPEAKER path confirmed, speaker_->stop() NOT accessible mid-stream
  │    └─ Workaround found (HA automation, ESPHome service) → PROCEED with workaround
  │
  └─ No physical stop mechanism found
       └─ STOP. Document limitation. Phase 10 blocked until satellite supports
          mid-stream speaker stop or target hardware is changed.
```

---

## 11. Implementation Boundaries

### 11.1 Possible Implementation Areas

Implement only changes required by observed defects. Possible areas include:

- Clearer cancellation propagation (verify existing, add if missing)
- Explicit cancellation hook for the active logical streaming session
- Player-stop integration boundary (documented, configurable)
- Additional sanitized correlation logging
- Small HA automation/blueprint for playback stop (if needed)
- Satellite-side playback-stop hook (if configurable)
- Tests and validation tooling

### 11.2 Non-Goals (Strict)

Do NOT implement:
- Phase 11 STT/LLM orchestration
- General voice assistant framework
- New conversation router
- New memory system
- Wake-word model training
- New VAD model
- Model switching
- Multi-GPU scheduling
- Multi-worker TTS
- Semantic request priority
- Response replacement outside barge-in needs
- Arbitrary media playback control
- Whole-home audio
- Mobile app support
- Cloud deployment
- New TTS voices
- Quantization changes
- Backend model changes

---

## 12. Target Discovery Questionnaire

During implementation (Phase 10 implementation branch), execute this
questionnaire against the real HA environment. Record results in the
implementation PR.

### 12.1 Home Assistant Configuration

| # | Question | Answer |
|---|----------|--------|
| Q1 | Assist pipeline name and ID? | |
| Q2 | STT provider (entity, type)? | |
| Q3 | TTS provider (entity, type)? | |
| Q4 | Conversation agent (entity, type)? | |
| Q5 | Wake word provider? | |
| Q6 | Is pipeline debug logging enabled? | |
| Q7 | Wyoming integration host/port for TTS? | |
| Q8 | Does Assist expose cancellation on new wake word? | |

### 12.2 Satellite Configuration

| # | Question | Answer |
|---|----------|--------|
| Q9 | Satellite entity type and ID? | |
| Q10 | Satellite hardware model? | |
| Q11 | ESPHome YAML: `speaker` configured? (USE_SPEAKER) | |
| Q12 | ESPHome YAML: `media_player` configured? (USE_MEDIA_PLAYER) | |
| Q13 | ESPHome YAML: `microphone` configured? Which I2S? | |
| Q14 | ESPHome YAML: `noise_suppression_level`? | |
| Q15 | ESPHome YAML: `auto_gain`? | |
| Q16 | ESPHome YAML: `volume_multiplier`? | |
| Q17 | ESPHome YAML: `silence_detection` enabled? | |
| Q18 | ESPHome YAML: `use_wake_word` enabled? | |
| Q19 | ESPHome YAML: `wake_word` model? | |
| Q20 | ESPHome YAML: `continuous` mode? | |

### 12.3 Playback Architecture

| # | Question | Answer |
|---|----------|--------|
| Q21 | Audio path: wrapper → HA → satellite? Any intermediate buffers? | |
| Q22 | Target speaker buffer size and actual drain time? (The source snapshot uses 16 KiB at 16 kHz mono s16le, a maximum ≈512 ms buffer duration; verify the target build.) | |
| Q23 | Can satellite listen while playing? (Full duplex?) | |
| Q24 | Is mic muted during playback? | |
| Q25 | Does AEC exist? Hardware or software? | |
| Q26 | Does wake word work during TTS playback? | |
| Q27 | Does `media_player.media_stop` stop playback? | |
| Q28 | Does new TTS URL auto-replace current? | |
| Q29 | Does `esphome.voice_assistant_stop` stop playback? | |

---

## 13. Document Reconciliations

This section records minimal documentation updates performed as part of this
planning phase to align current-status sections with the actual repository state.

### 13.1 `docs/NEXT_GOAL_PROMPTS.md` — Phase 9C Section

The "Current state after Phase 9C" section (lines 6–20) is stale. It describes
the state after Phase 9 but uses "Phase 9C" terminology. This section has been
updated (see commit) to:
- Use correct phase naming (Phase 9 → Phase 9C)
- Add explicit mention that Phase 9B (domain refactor) and Phase 9.5
  (progressive synthesis) are also complete but not deployed
- Reference the new Phase 10 planning document
- Keep all historical prompt sections unchanged

### 13.2 Other Docs

No other docs require reconciliation for this planning phase. The following
were checked and found accurate as-is:
- `docs/ROADMAP.md`: Already lists Phase 10 correctly
- `TODO.md`: Already lists Phase 10 correctly
- `CHANGELOG.md`: Already documents Phase 9.5 as Unreleased
- `docs/ARCHITECTURE.md`: Reconciled stale Phase 8 cancellation-cleanup wording; Phase 10 remains unverified end-to-end work
- `README.md`: No stale sections requiring update

---

## 14. Git Workflow

### Planning Branch (This Branch)

- **Branch**: `planning/phase-10-end-to-end-barge-in`
- **Type**: Documentation-only
- **Files**: `docs/PHASE_10_END_TO_END_BARGE_IN_PLAN.md`, `docs/NEXT_GOAL_PROMPTS.md`
- **No code, test, config, or template changes**

### Future Implementation Branch (Not Part of This Plan)

- **Branch**: `phase/phase-10-end-to-end-barge-in` (or similar)
- **Type**: Implementation + validation
- **Includes**: Code changes, tests, live evidence

---

## 15. Unresolved Operator-Provided Inputs

The following inputs are required from the operator during the implementation
phase. They cannot be resolved through repository inspection alone.

| # | Input Needed | Impact |
|---|-------------|--------|
| O1 | Target satellite hardware model and ESPHome YAML configuration | Determines playback stop mechanism |
| O2 | Actual HA Assist pipeline configuration (STT, TTS, conversation agent, wake word) | Confirms pipeline structure |
| O3 | Whether the satellite can detect wake words during TTS playback | Determines if barge-in is even possible without hardware changes |
| O4 | Whether `media_player.media_stop` stops playback on the target entity | Determines the physical stop approach |
| O5 | Whether HA pipeline debug traces can be enabled for testing | Required for correlation evidence |
| O6 | Acceptance thresholds for cancellation/playback/replacement latency | Must be based on measured evidence from target hardware |
| O7 | Whether `esphome.voice_assistant_stop` service is available and effective | Alternative physical stop mechanism |
| O8 | Confirmation of production image pins (no deployment without separate gate) | Safety boundary |

---

*Planning document version 1.0. Created 2026-07-12. Review and approve before
proceeding to implementation.*

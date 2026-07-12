# Phase 10 -- Voice Preview Edition Operator Discovery Checklist

**Purpose:** Guide an operator through 15 discovery items from the Home
Assistant (HA) UI for Voice Preview Edition (VPE), producing an evidence
bundle for Phase 10 end-to-end barge-in verification.

**Status:** Evidence template -- fill fields marked `[FILL]`, do not modify
structure.

**Gate classification definitions (do NOT classify yet -- classify AFTER
collecting all evidence):**

| Gate | Name | Meaning |
|------|------|---------|
| A | Existing behavior sufficient | Current cancel/stop already works acceptably; no Phase 10 changes needed |
| B | HA stops pipeline; playback continues | HA supersedes the pipeline and wrapper work releases correctly, but VPE keeps playing already-buffered audio |
| C | Wrapper cancellation fails | HA supersedes the pipeline, but queued or active wrapper/backend work remains instead of releasing correctly |
| D | Wake word unavailable | Wake word cannot be activated during or shortly after playback (barge-in impossible) |

---

## Important Pre-Execution Notes

1. **UI-first.** Every item is collected from the HA browser UI. No SSH
   required. No YAML editing required. No command-line tools needed.
2. **Record exact labels.** UI menus, labels, and navigation vary across HA
   versions. Always record the **exact label string** you clicked, not the
   suggested path in this document.
3. **Redact all secrets.** Replace API tokens, long-lived access tokens,
   MQTT passwords, device tracker data, and private IPs with `[REDACTED]`.
4. **Preferred evidence format:** Screenshot (PNG) with visible URL bar.
   For text-only output, paste into a fenced code block.
5. **Unraid / container note.** The wyoming-s2cpp-tts wrapper and backend
   run as Docker containers on Unraid. They are **NOT** HA add-ons. The HA
   "Add-ons" UI does not show them. Capture their status from the Unraid
   Docker tab or another read-only Unraid container view only if later requested; none of the 15 HA/VPE discovery items requires container mutation.
6. **Version-specific menus.** If a documented path does not exist in your
   HA version, record the actual navigation path you used and note the
   attempt.

---

## Evidence Template (per-item)

For each item capture:
- **Timestamp** (ISO 8601)
- **Exact UI path navigated** (copy labels verbatim)
- **Screenshot** (PNG preferred) or **raw text** (code block)
- **Field values copied verbatim** (do not paraphrase)
- **Any warnings, errors, or unexpected UI states observed**

---

## Discovery Items

### Item 1: Device Name / Hardware
**What:** The VPE device entity name, model, and hardware details.

**UI path (version-dependent; record actual):**
Settings > Devices & services > Devices > [find "Voice Preview Edition"] >
Device info

**Fields to capture:**
- Device name: `[FILL]`
- Manufacturer: `[FILL]`
- Model: `[FILL]`
- Hardware revision: `[FILL]`
- Firmware version: `[FILL]`
- Connected via: `[FILL: ESPHome / Wyoming / other]`

**Evidence:**
```
[ATTACH screenshot or paste text]
```

---

### Item 2: Satellite Entity ID
**What:** The VPE satellite/assist satellite entity ID.

**UI path (version-dependent; record actual):**
Settings > Devices & services > Entities > search "satellite" > click the
VPE satellite entity

**Fields to capture:**
- Entity ID: `[FILL]`
- State: `[FILL]`
- Pipeline assigned: `[FILL]`

**Evidence:**
```
[ATTACH screenshot or paste text]
```

---

### Item 3: Media-Player / Speaker Entity ID
**What:** The VPE speaker/media-player entity for audio output. Note
whether the speaker is a separate entity from the satellite.

**UI path (version-dependent; record actual):**
Settings > Devices & services > Entities > search "media_player" or
"speaker" > click the VPE media player entity

**Fields to capture:**
- Entity ID: `[FILL]`
- State: `[FILL]`
- Volume: `[FILL]`
- Supported features: `[FILL]`
- Is this entity separate from the satellite? `[FILL: yes / no -- same entity]`

**Evidence:**
```
[ATTACH screenshot or paste text]
```

---

### Item 4: Pipeline Name
**What:** The name of the active voice pipeline assigned to the VPE.

**UI path (version-dependent; record actual):**
Settings > Voice assistants > click on the pipeline name

**Fields to capture:**
- Pipeline name: `[FILL]`
- Language: `[FILL]`

**Evidence:**
```
[ATTACH screenshot or paste text]
```

---

### Item 5: Wake Word Provider
**What:** Which wake word engine is configured for the pipeline.

**UI path (version-dependent; record actual):**
Settings > Voice assistants > [pipeline name] > Wake word section

**Fields to capture:**
- Wake word provider: `[FILL: e.g. openWakeWord, microWakeWord, Wyoming, none]`
- Wake word(s) configured: `[FILL]`
- Sensitivity / threshold: `[FILL]`

**Evidence:**
```
[ATTACH screenshot or paste text]
```

---

### Item 6: Speech-to-Text (STT) Provider
**What:** Which STT engine is configured for the pipeline.

**UI path (version-dependent; record actual):**
Settings > Voice assistants > [pipeline name] > Speech-to-text section

**Fields to capture:**
- STT provider: `[FILL: e.g. Whisper, faster-whisper, Wyoming, cloud]`
- Language: `[FILL]`
- Model (if shown): `[FILL]`

**Evidence:**
```
[ATTACH screenshot or paste text]
```

---

### Item 7: Conversation Agent
**What:** Which conversation agent / intent processor is configured.

**UI path (version-dependent; record actual):**
Settings > Voice assistants > [pipeline name] > Conversation agent section

**Fields to capture:**
- Conversation agent: `[FILL: e.g. Home Assistant, OpenAI, custom]`
- Model / provider details: `[FILL]`

**Evidence:**
```
[ATTACH screenshot or paste text]
```

---

### Item 8: Text-to-Speech (TTS) Provider
**What:** Which TTS engine is configured for the pipeline. For the Unraid
setup this is typically a Wyoming protocol TTS service.

**UI path (version-dependent; record actual):**
Settings > Voice assistants > [pipeline name] > Text-to-speech section

**Fields to capture:**
- TTS provider: `[FILL: e.g. Wyoming, Piper, cloud TTS]`
- Wyoming TTS service name (if applicable): `[FILL]`
- Voice / language: `[FILL]`

**Evidence:**
```
[ATTACH screenshot or paste text]
```

---

### Item 9: Wake Word During Playback (Observational Test)
**What:** Can the wake word be detected while TTS audio is playing through
the VPE speaker? This is an observational test -- no tools needed.

**Procedure:**
1. Trigger any TTS response (e.g. ask "What time is it?" or use a manual
   TTS action).
2. While the response audio is playing, speak the wake word clearly.
3. Observe whether the VPE LED ring changes behavior (indicates wake word
   detected) and whether the pipeline restarts.

**Fields to capture:**
- TTS audio playing at time of test? `[FILL: yes / no]`
- Wake word detected during playback? `[FILL: yes / no]`
- VPE LED behavior observed: `[FILL: describe what the LED ring did]`
- Pipeline activity observed: `[FILL: new pipeline run started? / nothing?]`

**Evidence:**
```
[ATTACH screenshot of Assist debug timeline if pipeline restarted, or describe observation]
```

---

### Item 10: Microphone Muted / Suspended During Playback (Observational)
**What:** Does the VPE microphone mute or suspend itself while TTS audio is
playing? This determines whether barge-in is architecturally possible.

**Procedure:**
1. Check the microphone entity state BEFORE triggering TTS.
2. Trigger a TTS response.
3. While audio plays, immediately check the microphone entity state again.
4. Also check: does the VPE respond to the wake word immediately after
   playback ends? (Cold-start latency test.)

**Fields to capture:**
- Microphone state before TTS: `[FILL: on / off / idle / listening]`
- Microphone state during TTS playback: `[FILL: on / off / idle / muted]`
- Microphone state immediately after TTS ends: `[FILL]`
- Wake word works immediately after playback? `[FILL: yes / no / delay ~N seconds]`

**Evidence:**
```
[ATTACH screenshot(s) of microphone entity before/during/after]
```

---

### Item 11: Assist Pipeline Debug Trace
**What:** A full pipeline debug trace showing STT > Intent > TTS stages
with timing.

**UI path (version-dependent; record actual):**
Settings > Voice assistants > [pipeline name] > three-dot menu > Debug

**Procedure:**
1. Open the Assist debug view.
2. Trigger a test utterance (e.g. "What time is it?").
3. Capture the full trace with stage timings.

**Fields to capture:**
- Utterance text: `[FILL]`
- STT stage: `[FILL: provider, time taken, result text]`
- Intent stage: `[FILL: intent matched, time taken]`
- TTS stage: `[FILL: provider, time taken, audio generated]`
- Total pipeline time: `[FILL]`
- Any errors or timeouts: `[FILL]`

**Evidence:**
```
[ATTACH screenshot of debug trace]
```

---

### Item 12: ESPHome Logs / Diagnostics
**What:** ESPHome device logs and diagnostics for the VPE.

**UI path (version-dependent; record actual):**
Settings > Devices & services > Devices > Voice Preview Edition > ...
(look for ESPHome diagnostics, logs, or "Visit" link to ESPHome dashboard)

**Fields to capture:**
- ESPHome version (if shown): `[FILL]`
- VPE firmware version (from ESPHome): `[FILL]`
- Recent log entries (last ~20 lines, redact IPs): `[FILL]`
- Any errors or warnings in logs: `[FILL]`

**Evidence:**
```
[ATTACH screenshot of ESPHome device page or paste log text]
```

---

### Item 13: Entity Attributes for Playback / Stop
**What:** Entity attributes and services available for controlling playback
and stopping TTS output.

**UI path (version-dependent; record actual):**
Developer tools > States > filter for VPE media_player entity

**Fields to capture:**
- Entity ID: `[FILL]`
- All attributes (copy verbatim): `[FILL -- redact IPs/secrets]`
- Available services for this entity: `[FILL]`

Also check:
Developer tools > Actions (called "Services" in older HA releases) > search for
"stop" / "pause" / "cancel" and record only actions the UI actually exposes
for the VPE entity. Do not assume a dedicated TTS cancel action exists.

**Evidence:**
```
[ATTACH screenshot(s) of entity attributes and available services]
```

---

### Item 14: Exact HA and VPE Firmware Versions
**What:** Precise version strings for Home Assistant and VPE firmware.

**UI path (version-dependent; record actual):**
- HA version: Settings > About > copy version string
- VPE firmware: Settings > Devices & services > Devices > Voice Preview
  Edition > firmware version field

**Fields to capture:**
- HA version (exact): `[FILL]`
- HA installation type: `[FILL: e.g. Container, HA OS, Supervised, Core]`
- VPE firmware version (exact): `[FILL]`
- ESPHome version (if separate): `[FILL]`
- Wyoming protocol version (if shown in integration): `[FILL]`

**Evidence:**
```
[ATTACH screenshot(s)]
```

---

### Item 15: Native Stop / Pause / Cancel Service
**What:** Identify the exact service calls available to stop or cancel TTS
audio playback on the VPE.

**UI path (version-dependent; record actual):**
Developer tools > Actions (called "Services" in older HA releases) > search for
"media" / "tts" / "stop" / "pause" / "cancel"

**Procedure:**
1. List ALL services that can target the VPE media_player entity.
2. For each action actually shown (for example `media_player.media_stop` or
   `media_player.media_pause` when present), record its exact domain/action name
   and accepted parameters. Do not infer or invent a TTS cancel action.
3. If possible, test a manual service call while TTS is playing and
   observe whether the audio actually stops.

**Fields to capture:**
- Service(s) found: `[FILL: list of domain.service names]`
- VPE entity accepted as target? `[FILL: yes / no per service]`
- Manual stop test result: `[FILL: audio stopped? yes / no / could not test]`

**Evidence:**
```
[ATTACH screenshot(s) of service list and/or test result]
```

---

## Copy/Paste Response Template

After completing all 15 items, copy the filled form below and post it as
the discovery evidence bundle:

```
=== PHASE 10 VPE DISCOVERY EVIDENCE BUNDLE ===
Date: [FILL]
Operator: [FILL]

Item  1 (device name/hardware):     [FILL brief summary; see screenshots]
Item  2 (satellite entity ID):      [FILL]
Item  3 (media-player/speaker):     [FILL]
Item  4 (pipeline name):            [FILL]
Item  5 (wake-word provider):       [FILL]
Item  6 (STT):                      [FILL]
Item  7 (conversation agent):       [FILL]
Item  8 (TTS):                      [FILL]
Item  9 (wake word during playback):[FILL]
Item 10 (mic muted during playback):[FILL]
Item 11 (Assist pipeline debug):   [FILL]
Item 12 (ESPHome logs/diag):       [FILL]
Item 13 (playback/stop attributes):[FILL]
Item 14 (exact HA + VPE versions): [FILL]
Item 15 (native stop/cancel svc):  [FILL]

Screenshots: [LIST filenames or links]
==========================================
```

---

## Gate Classification (fill AFTER all 15 items)

| Gate | Name | Classification | Evidence Basis |
|------|------|---------------|----------------|
| A | Existing HA/VPE behavior sufficient | `[FILL: APPLIES / DOES NOT APPLY / UNKNOWN]` | Assist trace + Items 9, 10, 13, 15 + correlated wrapper/backend logs |
| B | HA stops pipeline; VPE playback continues | `[FILL: APPLIES / DOES NOT APPLY / UNKNOWN]` | Assist trace + Items 9, 10, 13, 15 |
| C | Wrapper cancellation fails | `[FILL: APPLIES / DOES NOT APPLY / UNKNOWN]` | Correlated wrapper/backend logs + Assist trace |
| D | Wake word unavailable during playback | `[FILL: APPLIES / DOES NOT APPLY / UNKNOWN]` | Items 5, 9, 10 |

**Notes / anomalies observed:** `[FILL]`

---

## Operator Sign-off

- **Operator name/ID:** `[FILL]`
- **Date completed:** `[FILL]`
- **Notes / anomalies observed:** `[FILL]`

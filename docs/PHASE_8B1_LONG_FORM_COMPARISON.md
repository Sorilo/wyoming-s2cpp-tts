# Phase 8B1 Long-Form Audio-Quality Comparison

This is an investigation/runbook for the newly noticed long-form beeping or
stuttering report. It is **not** a production-default change and does not begin
Phase 9.

## Scope

Compare the same long text and same voice across these safe context settings:

- `S2_CODEC_CONTEXT_FRAMES=4`
- `S2_CODEC_CONTEXT_FRAMES=64`
- `S2_CODEC_CONTEXT_FRAMES=auto` (or blank/`-1`), which lets the backend use its
  default/160-frame context

Keep:

- `S2_STREAM=true`
- `S2_SEGMENT_SENTENCES=false`
- same backend image: `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd`
- same wrapper image: `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-9c134cc`
- same voice for every run

Do **not** test known-crashing context values. Phase 7.5D1 found that values
`1`, `8`, `16`, `32`, `48`, `96`, and `128` caused backend crashes. Known working
comparison values are `4`, `64`, and backend default/`160`.

## Evidence Before This Comparison

Phase 7.5D1 showed:

- `segment_sentences=false` is the key progressive path.
- First backend PCM was about 150 ms for context/default configurations.
- Context `4` was roughly 4.8–5.8x faster total synthesis than the conservative
  default/160 in the tested long/medium cases.
- Context `64` was a slower but potentially more conservative compromise than
  context `4` in the medium test.

Phase 7.5D3 live verification with context `4` showed:

- first backend PCM: 242 ms
- first Wyoming audio: 529 ms
- total synthesis: about 3890 ms
- exact PCM accounting
- no duplicate playback
- short/normal output sounded good

The reported long-form beeping/stuttering was noticed later and is not yet
reproduced by objective artifacts.

## Client Probe

Run this probe once per active wrapper context. The script does not modify live
containers; it labels and measures whatever context the wrapper is already using.

```bash
cd /workspace/wyoming-s2cpp-tts
PYTHONPATH=. .venv/bin/python scripts/live_compare_long_form_contexts.py \
  --host 192.168.1.45 --port 10200 \
  --context-label 4 \
  --voice cmu_jmk_male_canadian \
  --timeout 120
```

Repeat with `--context-label 64` and `--context-label auto` after changing the
wrapper setting externally (for example through the Unraid template/UI and a
controlled wrapper restart). Hermes must not modify the live containers directly
for this goal.

The script saves per-context JSON and WAV files under:

```text
verification_artifacts/phase_8b1/long_form/context_<label>/
```

Collected client-side fields include:

- first Wyoming `AudioChunk` time
- total synthesis time
- audio duration
- real-time factor (`wall_clock / audio_duration`)
- audio seconds produced per wall second
- chunk count
- inter-chunk arrival gaps
- longest inter-chunk gap
- approximate stream buffer trend and underrun likelihood
- PCM bytes and WAV output

`first_backend_audio_ms` is not directly visible to a Wyoming client. Correlate
it from wrapper logs (`backend_stream_first_audio`) captured with
`capture_phase_8b1_logs.sh`.

## Listening Checklist

For each context WAV, record whether you hear:

- beeping or tonal artifacts
- stuttering
- repeated syllables
- missing syllables
- boundary clicks
- robotic voice identity changes
- prosody changes
- clipped sentence endings
- unnatural pauses
- playback underruns

Numerical analysis can find timing/chunking defects, silence gaps, clipping, or
large discontinuities, but it does **not** prove perceptual quality. Human
listening remains required.

## Interpretation

A. Context `4` has long-form artifacts but `64` is clean:
recommend context `64` as the production compromise; do not redesign the
architecture.

B. Context `4` and `64` are clean but buffer metrics show underrun risk:
investigate buffering/chunk cadence before blaming codec quality.

C. All contexts exhibit the issue:
isolate backend output versus Wyoming/Home Assistant playback before changing
wrapper code.

D. Only Home Assistant playback exhibits the issue while saved WAVs are clean:
preserve backend/wrapper output and investigate playback path later.

E. A real wrapper PCM/rechunking defect is proven:
fix narrowly with tests.

Do not set `S2_SEGMENT_SENTENCES=true` as the first response; that would discard
the major latency improvement and return to sentence-level buffering.

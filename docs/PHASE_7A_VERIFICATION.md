# Phase 7A Verification — CMU ARCTIC Voice Profiles

## Summary

Six one-time `.s2voice` voice profiles were created from CMU ARCTIC reference
recordings and verified via direct backend multipart synthesis. All six produced
valid RIFF/WAVE audio output. Wrapper behavior, Docker images, model files, and
Home Assistant settings were not changed during Phase 7A.

## Voice profiles created

| Code | Profile ID | Gender | Accent | Size |
|------|-----------|--------|--------|------|
| bdl | `cmu_bdl_male_us` | male | US English | ~5.0 KB |
| rms | `cmu_rms_male_us` | male | US English | ~5.9 KB |
| jmk | `cmu_jmk_male_canadian` | male | Canadian English | ~5.1 KB |
| slt | `cmu_slt_female_us` | female | US English | ~4.7 KB |
| clb | `cmu_clb_female_us` | female | US English | ~5.5 KB |
| eey | `cmu_eey_female_us` | female | US English | ~5.2 KB |

- **Persistent directory:** `/mnt/user/appdata/s2cpp/voices`
- **Reference utterance:** `arctic_a0407` from CMU ARCTIC
- **Reference text (from archive transcript):** "Mercedes screamed, cried, laughed, and manifested the chaotic abandonment of hysteria."
- **License:** CMU ARCTIC uses a free-software license permitting commercial exploitation. Attribution and included license/readme files must be preserved.

## Verification results

### Voice listing

All six profiles are visible via `s2 --list-voices --voice-dir /voices`. The
initial lightweight attempt without GPU runtime failed because the s2 executable
links `libcuda.so.1` even for listing; the GPU-backed fallback with
`--runtime=nvidia` and the assigned GPU UUID succeeded.

### Direct backend synthesis

Multipart `POST /generate` requests were issued against
`http://127.0.0.1:3032/generate` from the Unraid host for each profile. Results:

| Profile ID | HTTP | Content-Type | RIFF/WAVE | Result |
|-----------|------|-------------|-----------|--------|
| `cmu_bdl_male_us` | 200 | audio/wav | valid | PASS |
| `cmu_rms_male_us` | 200 | audio/wav | valid | PASS |
| `cmu_jmk_male_canadian` | 200 | audio/wav | valid | PASS |
| `cmu_slt_female_us` | 200 | audio/wav | valid | PASS |
| `cmu_clb_female_us` | 200 | audio/wav | valid | PASS |
| `cmu_eey_female_us` | 200 | audio/wav | valid | PASS |

**Synthesis: 6/6 passed, 0 failed.**

Comparison WAV files saved under
`/mnt/user/appdata/s2cpp/verification_artifacts/phase_7a/`.

## Human listening assessment

All six voices are usable and acceptable as temporary assistant voices. They
sound **somewhat more robotic than desired**. This is **not** a confirmed
downstream defect. The perceived quality may be influenced by:

- The older CMU ARCTIC reference recordings (studio quality but dated)
- The short reference clip (~5–7 seconds)
- The model and quantization (`s2-pro-q6_k.gguf`)
- The synthesis parameters used

A personal clean recording will be created later and is expected to be a better
long-term voice-quality test.

## Operational caveats

1. **FestVox HTTPS unreachable:** The `https://festvox.org/` endpoint was
   unreachable from the Unraid host. The official FestVox HTTP archive endpoint
   (`http://festvox.org/cmu_arctic/packed/`) was used successfully instead.

2. **CUDA-linked --list-voices:** The s2 executable links `libcuda.so.1` even
   for `--list-voices`. Voice listing requires the NVIDIA runtime and the
   assigned GPU UUID. A lightweight listing attempt without GPU failed.

## Backend state

- `s2cpp-backend` was stopped during voice profile creation to release GPU VRAM
  and restarted before synthesis verification.
- Backend was confirmed running at script completion.

## What was not changed

- Wrapper behavior, image, and container unchanged
- Backend image and container unchanged
- Model files unchanged
- Home Assistant settings unchanged
- No application runtime code, Dockerfiles, or Home Assistant settings modified
- No voice profiles, source audio, full dataset transcripts, or generated WAV
  files committed to the repository

## Next phase

Phase 7B: wrapper voice discovery, voice selection, default voice
configuration, Wyoming Describe exposure, Home Assistant selection, and drop-in
discovery for later personal voice profiles.

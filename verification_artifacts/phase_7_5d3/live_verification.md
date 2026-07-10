# Phase 7.5D3: Live Progressive Streaming Verification

**Date:** 2026-07-09
**Wrapper:** `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-4c23aa8`
**Backend:** `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b`

## Request Settings

- `S2_STREAM=true`, `segment_sentences=false`, `codec_decode_context_frames=4`
- Voice: `cmu_jmk_male_canadian`
- One Home Assistant "Try Voice" request

## Live Measurements

| Metric | Value |
|---|---|
| `backend_stream_headers elapsed_ms` | 20 ms |
| `backend_stream_first_audio elapsed_ms` | 242 ms |
| `first_wyoming_audio elapsed_ms` | 529 ms |
| `backend_stream_done total_backend_stream_ms` | 3889 ms |
| `syn_stopped total_synthesis_ms` | 3890 ms |
| Backend PCM bytes | 229,376 |
| Wyoming PCM bytes | 229,376 |
| Byte accounting | **Exact match** |
| Duplicate synthesis | **None** |

## Comparison with segment_sentences=true

| Metric | Before (7.5B) | After (7.5D3) |
|---|---|---|
| First backend PCM | 2932 ms | **242 ms** |
| Progressive window | ~5 ms | **3647 ms** |
| Total stream | 2937 ms | 3889 ms |

## Human Listening

- Voice identity: good
- No robotic artifacts, boundary clicks, or truncation
- User assessment: "sounded great"

## Conclusion

Progressive streaming is confirmed working in production. First audio improved
from ~2932ms (sentence-buffered) to ~242ms (frame-level progressive). PCM byte
accounting is exact. No regressions.

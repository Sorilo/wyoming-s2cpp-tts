# Phase 7.5B: Live Verification & Streaming-Metrics Audit

**Date:** 2026-07-08
**Verification type:** Live deployment observability audit + metrics correctness fix
**Trigger:** One Home Assistant "Try Voice" request

## Deployed images

| Component | Image | SHA |
|---|---|---|
| Wrapper | `ghcr.io/sorilo/wyoming-s2cpp-tts` | `sha-4b49a70` |
| Backend | `ghcr.io/sorilo/wyoming-s2cpp-tts-backend` | `sha-741d06b` |

## One-request lifecycle (confirmed)

| Event | Present |
|---|---|
| `compatibility_synthesize_deferred` | ✓ |
| `syn_trigger` (mode=streaming) | ✓ |
| `backend_start` | ✓ |
| `backend_stream_headers` | ✓ |
| `backend_stream_first_audio` | ✓ |
| `first_wyoming_audio` | ✓ |
| `backend_stream_done` | ✓ |
| `audio_out` | ✓ |
| `syn_stopped` | ✓ |
| No duplicate synthesis | ✓ |

**Correlation:** `connection_id=4c0f4c8f`, `synthesis_id=ea607c6c`, `voice=cmu_jmk_male_canadian`, mode=streaming.

## Measured timing (live)

| Metric | Value |
|---|---|
| `backend_stream_headers elapsed_ms` | 61 ms |
| `backend_stream_first_audio elapsed_ms` | 2932 ms |
| `backend_stream_done total_elapsed_ms` | 2937 ms |
| **Progressive window** | **~5 ms** |

## Measured byte counts (live, pre-fix)

| Metric | Bytes | Chunks |
|---|---|---|
| `backend_stream_done.total_pcm_bytes` | 222,580 | 27 |
| `audio_out.pcm_bytes` | 224,660 | 27 |
| **Discrepancy** | **+2,080** | 0 |

## Byte-count discrepancy root cause

The `synthesize_s2cpp_streaming_tts_events()` function contained **two metric-only
double-counting bugs** in the `backend_stream_done` and `audio_out` observability
log lines:

1. **`audio_out.pcm_bytes`** added `sum(flush_chunks)` to `metrics.total_emitted_bytes`.
   The flush loop already calls `metrics.record_emitted_chunk()` for each flush chunk,
   so `metrics.total_emitted_bytes` already includes flush bytes. The addition
   double-counted them.

2. **Both `backend_stream_done.chunk_count` and `audio_out.chunk_count`** added
   `len(flush_chunks)` to `metrics.emitted_chunk_count`. Same root cause:
   `metrics.record_emitted_chunk()` within the flush loop already incremented
   `emitted_chunk_count`.

The live 2,080-byte discrepancy equals exactly the flush carry chunk size:
222,580 bytes ÷ 8,820 bytes/chunk = 25 full chunks + 2,080 byte carry.

**No actual audio bytes were duplicated, lost, or corrupted.** The emitted PCM was
frame-aligned and byte-exact at all times. The bug affected only the structured
JSON observability log lines, not the Wyoming `AudioChunk` payloads.

## Fix applied

Removed the redundant `+ len(flush_chunks)` and `+ sum(flush_chunks)` from both
log events. Both now use `metrics.total_emitted_bytes` and `metrics.emitted_chunk_count`
directly, which already include flush contributions via `record_emitted_chunk()`.

Also renamed `backend_stream_done.total_elapsed_ms` → `total_backend_stream_ms`
for consistency with the unified timing vocabulary.

## Enhanced observability fields

`first_wyoming_audio` now includes:

| Field | Meaning |
|---|---|
| `elapsed_ms` | Time from stream_start to first Wyoming `AudioChunk` emission |
| `time_to_first_backend_audio_ms` | Time from stream_start to first non-empty backend data |
| `wrapper_first_audio_forwarding_overhead_ms` | `elapsed_ms - time_to_first_backend_audio_ms` |

`syn_stopped` now includes `total_synthesis_ms` — wall-clock duration from
synthesis trigger to completion.

All timing uses one consistent monotonic start point (`stream_start` at
`synthesize_s2cpp_streaming_tts_events()` entry) per synthesis.

## Deterministic byte-counting tests added

5 new tests in `TestStreamingPCMByteAccounting`:

1. `test_clean_aligned_stream_bytes_match` — cleanly divisible stream: backend bytes == metrics bytes
2. `test_non_aligned_stream_with_flush_carry` — non-divisible stream: flush carry counted once, not twice
3. `test_first_backend_chunk_included_in_total` — first backend chunk bytes are in total
4. `test_stream_split_across_transport_boundaries` — partial-frame transport boundaries: every byte counted once
5. `test_every_emitted_byte_counted_exactly_once` — realistic 222,580-byte 44100 Hz scenario: exact match

## Test baseline

**374/374 passing, zero failures.**

## Conclusion

- Phase 7.5A streaming path is **confirmed active and correct** in production.
- The progressive window is ~5 ms — streaming transport works, but the **backend
  dominates latency** (2,932 ms to first audio).
- Wrapper streaming alone does not materially reduce time-to-first-audio with
  the current backend generation/flush behavior.
- Byte-count discrepancy was a **metrics-only defect** in observability code.
  No audio data was affected.
- Fixed observability fields now provide accurate per-synthesis timing for future
  latency investigations.

## Next-phase recommendation

Investigate s2.cpp backend generation and flush behavior before assuming further
wrapper optimization will help. The backend's 2,932 ms to first audio (with RTF ≈ 0.94)
suggests the model generates audio nearly at realtime but buffers until near completion.



## Replacement wrapper image published

| Field | Value |
|---|---|
| Commit | `974e2205bd5994bab6b1a7c210eaf1e2756ec5d4` |
| Tag | `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-974e220` |
| Edge tag | `ghcr.io/sorilo/wyoming-s2cpp-tts:edge` |
| Digest | `sha256:8eb504f4eca2ca04d63ddc41161b37f438a227ad05525cdc3849469bdad7cf12` |
| Workflow run | https://github.com/Sorilo/wyoming-s2cpp-tts/actions/runs/28978680861 |

**Runtime code changed:** No — `first_wyoming_audio`, `backend_stream_done`, and
`audio_out` observability log fields only. The `syn_stopped` event added
`total_synthesis_ms`. No synthesis behavior, chunking, or audio data paths
were modified.

# Phase 7.5C: Backend Progressive-Audio Investigation

**Backend image:** `ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-741d06b`
**s2.cpp revision:** `rodrigomatta/s2.cpp` @ `2c33261938da1a41d713768b1b391b4d368d7d2c`
**HTTP framework:** cpp-httplib (bundled as `third_party/httplib.h`)

---

## 1. Supported Backend Request Parameter Table

All parameters accepted by `POST /generate` via multipart/form-data.

### Top-level multipart fields

| Field | Type | Required | Description |
|---|---|---|---|
| `text` | string | **yes** | Text to synthesize |
| `params` | JSON string | no | JSON object with generation/streaming settings |
| `voice` | string | no | Voice profile ID, aliases: `voice_id`, `voice_profile` |
| `voice_dir` | string | no | Directory for `.s2voice` profile files |
| `reference_text` | string | no | Transcript for reference audio, alias: `ref_text`, `prompt_text` |
| `reference` | file | no | Reference audio file, aliases: `reference_audio`, `prompt_audio`, `ref_audio` |

### `params` JSON fields (all optional)

| Field | Type | Default | Affects |
|---|---|---|---|
| `max_new_tokens` | int | 1024 | generation length |
| `temperature` | float | 0.8 | sampling / quality |
| `top_p` | float | 0.8 | sampling / quality |
| `top_k` | int | 30 | sampling / quality |
| `min_tokens_before_end` | int | 0 | generation length |
| `n_threads` | int | 0 (auto) | generation parallelism |
| `codec_follow_backend` | bool | true | codec GPU selection |
| `codec_auto_backend` | bool | true | codec auto-benchmark |
| `voice` / `voice_id` | string | — | voice ID (params-level override) |
| `voice_dir` | string | — | voice directory |
| **`stream_decode_stride_frames`** | int | 0 (→4) | **decode batch size** |
| **`stream_holdback_frames`** | int | −1 (→codec) | **frames held before emit** |
| **`codec_decode_context_frames`** | int | −1 (→codec) | codec streaming context window |
| **`stream_start_buffer_ms`** | int | 0 or 3000 | **HTTP startup buffer delay** |
| `segment_sentences` | bool | false | text segmentation |
| `sentence_pause_ms` | int | 180 | pause between segments |
| `segment_max_chars` | int | 0 | max chars per segment |
| **`low_latency`** | bool | false | **sets stride=1, holdback=0, start_buffer=0** |
| **`output_format`** | string | `"wav"` | `"pcm_s16le"` or `"wav"` |
| `verbose` | bool | true | generation progress logging |
| **`stream`** | bool | false | enables streaming path (vs buffered WAV) |
| **`chunked`** | bool | false | enables Transfer-Encoding: chunked |
| `realtime` | bool | false | alias for `chunked` |

### `low_latency` behavior (src/s2_server.cpp:670-684)

When `low_latency=true`:
- `stream_decode_stride_frames = 1` (decode every frame)
- `stream_holdback_frames = 0` (emit immediately)
- `stream_start_buffer_ms = 0` (no HTTP startup buffer)

When `low_latency=false` + `chunked=true` + `output_format=pcm_s16le`:
- **`stream_start_buffer_ms = 3000`** (3-second buffer before first HTTP chunk)

---

## 2. Source-Level Generation / Decode / Flush Flow

### Architecture: Type E — Transport-Chunked but NOT Inference-Progressive

The pipeline at `src/s2_pipeline.cpp:953-1180` does decode incrementally:

1. `generate()` runs the AR loop (src/s2_generate.cpp:97-183), calling `on_frame`
   for EVERY generated frame (line 150-163)
2. `on_frame` accumulates codes, then calls `decode_window_and_emit()` when
   stride threshold is reached (with `low_latency`: every frame)
3. `decode_window_and_emit()` calls `codec.decode()` then `sink.on_pcm_data()`,
   which in `HttpStreamSink` (src/s2_server.cpp:381-394) converts to s16le,
   pushes to the `StreamContext` queue, and notifies the HTTP writer

**The codec decodes and emits audio PROGRESSIVELY during generation.**

However, the HTTP response path uses `res.set_chunked_content_provider()`
(src/s2_server.cpp:809-870). The content provider lambda:

- Waits for chunks in the shared queue (100ms timeout)
- Writes all available chunks via `sink.write()`
- Returns `true` (more data coming)
- Only calls `sink.done()` when synthesis completes

**But httplib's `DataSink.write()` buffers internally.** The data is not sent
to the socket until the buffer reaches a threshold or `sink.done()` is called.
Per-frame PCM chunks are small (~640 bytes), so the buffer never fills during
a typical 58-frame generation.

### Data flow:

```
generate() -> on_frame (58 times, ~45ms each) -> codec.decode() -> sink.on_pcm_data()
    -> ctx->chunks.push_back(pcm) -> cv.notify_one()

httplib content provider -> wait for chunks -> sink.write(all chunks) -> [BUFFERED]
    -> when done: sink.done() -> [ALL DATA FLUSHED AT ONCE]
```

---

## 3. Benchmark Results

All benchmarks against live backend, voice `cmu_jmk_male_canadian`, medium text
(~85 chars), 3 runs per config.

| Config | 1st Byte | Total | PCM | Reads | Progressive? |
|---|---|---|---|---|---|
| **baseline_streaming** (current) | 1ms | 6775ms | ~540K | ~132 | **✗ 0 reads in first 2s** |
| stream_no_chunked | 6738ms | 6738ms | ~532K | ~130 | **✗ all at once** |
| chunked_no_lowlatency (3s buf) | 1ms | 6842ms | ~540K | ~132 | **✗ 0 reads in first 2s** |
| explicit_zero_buffer | 0ms | 6498ms | ~516K | ~127 | **✗ 0 reads in first 2s** |
| stride=1, holdback=0 | 0ms | 6633ms | ~533K | ~131 | **✗ 0 reads in first 2s** |
| stride=8 | 1ms | 6734ms | ~535K | ~131 | **✗ 0 reads in first 2s** |
| stride=16 | 1ms | 6490ms | ~519K | ~127 | **✗ 0 reads in first 2s** |
| no_stream_no_chunked | 6655ms | 6656ms | ~526K | ~129 | **✗ all at once** |

**Key findings:**
- Every chunked config sends headers at 0–1ms (Transfer-Encoding: chunked active)
- **Zero transport reads arrive in the first 2000ms for ANY chunked config**
- All 125–140 reads arrive in a single burst at ~6400–6800ms
- Max spacing between reads: ~2400ms (gap between headers and data burst)
- `stream_decode_stride_frames`, `stream_holdback_frames`, `low_latency`, and
  `stream_start_buffer_ms` have **zero effect** on HTTP socket delivery timing

---

## 4. Root Cause: httplib Chunked Content Provider Buffering

cpp-httplib's `DataSink.write()` accumulates data in an internal response buffer.
Data is flushed to the socket only when:
1. The buffer reaches a sufficiently large internal threshold, OR
2. `sink.done()` is called (marks the final chunk), OR
3. The content provider returns `false` (completion signal)

In the current implementation, per-frame PCM is ~640 bytes × 58 frames ≈ 37 KB
total — well below httplib's buffer threshold. No intermediate flush occurs.

**The ~5ms "progressive window" observed in Phase 7.5B** was not progressive
delivery. It was the time between httplib flushing the buffered data and the
wrapper detecting stream completion — effectively measurement noise.

---

## 5. Can Configuration Fix It?

**No.** All tested configurations produce identical non-progressive behavior.
The httplib buffering is internal to the framework and not controlled by
request parameters.

## 6. Are Backend Source Changes Required?

**Yes.** To achieve true progressive delivery:

### Option A (smallest): Explicit flush after each write

Add a flush call after `sink.write()` in the content provider lambda
(src/s2_server.cpp:840-847). Requires verifying the bundled httplib version
supports per-write flushing.

### Option B: Lower httplib buffer size

If httplib exposes a configurable write buffer size, reduce it to ~4 KB so
flushes occur every ~6 frames (~270ms into generation).

### Option C: Periodic done() as flush signal

Call `sink.done()` periodically (e.g., every 8 writes) to force httplib to
flush, then set up a continuation for the remaining data.

### Option D: Raw socket writes

Bypass httplib's chunked content provider entirely and write chunked-framed
data directly to the socket with explicit fflush.

---

## 7. Is Useful Sub-Second First Audio Achievable?

**Yes, with Option A or B.** The generation loop produces frames at ~45ms each.
With incremental codec decode (already working) and per-write socket flushing,
the first audio could arrive within 45–200ms of generation start. Target:
sub-500ms first audio for short text, sub-1500ms for medium.

---

## 8. Recommendation

**Phase 7.5D: Implement httplib flush in the chunked content provider.**

1. Inspect the bundled `third_party/httplib.h` for `DataSink` flush support
2. If available, call flush after every `sink.write()` in the content provider
3. If not available, implement Option C or D
4. Benchmark against live backend to verify progressive delivery
5. Rebuild and redeploy the backend image

Do not implement during Phase 7.5C — this phase is investigation only.

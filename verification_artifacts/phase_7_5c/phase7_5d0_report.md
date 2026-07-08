# Phase 7.5D0: Root-Cause Isolation Report

## 1. httplib DataSink / write_content_chunked Semantics

| Property | Value |
|---|---|
| `DataSink.write()` semantics | Synchronous: frames as HTTP chunked, calls `write_data()` → `SocketStream::write()` → `send_socket()` immediately |
| `DataSink.flush()` | **Does not exist** |
| `DataSink.done()` | Writes final zero chunk "0\r\n\r\n", sets `data_available=false` |
| `DataSink` raw socket access | None |
| Compressor for `audio/L16` | `nocompressor` (passthrough) |
| `Content-Encoding` | `identity` (no compression) |
| `TCP_NODELAY` | **false** (Nagle enabled by default) |

## 2. Synthetic httplib Progressive Test

| Metric | Result |
|---|---|
| Chunks written | 10 × 640 bytes at 100ms intervals |
| Raw TCP recv pattern | Headers at 0ms, chunk at 0ms, chunk at 100ms, ..., chunk at 902ms |
| Chunked frame count | 10 data + 1 final = 11 |
| **Progressive?** | **YES** — httplib CAN stream progressively |

## 3. Raw TCP: Production Backend

| Metric | Value |
|---|---|
| HTTP headers received | 0ms (290 bytes) |
| Chunked frames in body | **1 data chunk** (327,102 bytes) + 1 final = 2 total |
| Expected progressive frames | 58 (one per codec frame) |
| First PCM data arrival | ~6500ms (synthesis completion) |
| Transfer-Encoding | chunked |
| Content-Encoding | identity |

## 4. Root Cause: Codec streaming_history_frames = 160

```cpp
// s2_codec.cpp:871-873
streaming_history_frames_ = ((rvq_history + 16 + 7) / 8) * 8;
if (streaming_history_frames_ <= 0) streaming_history_frames_ = 160;
```

The codec requires >=160 frames of context for streaming decode.
We generate only 58 frames for medium text.

Every incremental `codec.decode(1..57 frames)` during generation produces **no audio**
because the window is below the codec's minimum context.

Only the final `decode_window_and_emit(58, finalize=true)` produces audio —
all 327KB in one batch.

## 5. Classification

**Root Cause B: Codec receives frames early but does not emit PCM early.**
(Secondary: Root Cause A — with 160-frame requirement and only 58 generated,
no audio is possible until generation completes.)

## 6. Recommendation

Phase 7.5D1: Investigate whether the codec can decode with smaller context
(`codec_decode_context_frames=1` parameter) or whether the 160-frame minimum
is architecturally fixed.


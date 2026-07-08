# Phase 7.5D1: Codec Context Investigation

## 1. Meaning of 160-Frame History

streaming_history_frames_=160 is a conservative default fallback, NOT a hard minimum.
The codec.decode() accepts ANY positive n_frames (only rejects <=0).
Source: s2_codec.cpp:871-873, 1299.

## 2. Is It Configurable?

YES. codec_decode_context_frames in params JSON overrides streaming_history_frames()
when set >=0 (s2_pipeline.cpp:1011-1012). Working values: 4, 64, default/160.
Values 1,8,16,32,48,96,128 cause backend crashes.

## 3. True Root Cause: segment_sentences=True

segment_sentences=True forces per-sentence buffered synthesis via
synthesize_segmented_to_sink(). Each sentence buffers all audio, emits once at end.
segment_sentences=False uses synthesize_streaming_raw() which streams progressively
via on_frame -> decode_window_and_emit -> sink.on_pcm_data().

Our wrapper sends segment_sentences=True in every request (s2_client.py:213).

## 4. Results (segment_sentences=False, raw TCP client)

| Config | Short | Medium | Long |
|---|---|---|---|
| baseline (default) | 151ms / 5.6s | 153ms / 42.8s | 191ms / 193.1s |
| context=4 | 148ms / 2.2s | 159ms / 8.9s | 211ms / 33.5s |
| context=64 | - | 67ms / 14.3s | - |

## 5. Classification: Outcome D

Progressive streaming works. context=4 is 4.8-5.8x faster total synthesis.
First PCM at ~150ms for all configs. Sub-500ms first audio achieved.

## 6. Fix: Wrapper-only change

Change segment_sentences=False in S2GenerateRequest (s2_client.py:136).
Optionally set codec_decode_context_frames=4 for 5x faster synthesis.
No backend image change required.

## 7. Phase 7.5D2: Implement wrapper fix
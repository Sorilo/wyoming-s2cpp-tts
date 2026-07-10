# S2 Pro Quantization Benchmark Results

- **Endpoint**: `127.0.0.1:3034`
- **Text length**: 361 chars
- **Stride**: 4 (fixed)
- **Codec context**: 24
- **Holdback**: 0
- **Start buffer**: 0 ms
- **Low latency**: True
- **Sample rate**: 44100 Hz (mono s16le)

## Candidate Models

| Quant | Filename | SHA-256 | Size (GB) | Exists |
|-------|----------|---------|-----------|--------|
| Q4_K_M | s2-pro-q4_k_m.gguf | 83963e1b7cec980b... | 3.57 | YES |

## Results by Quantization

| Quant | Avg RTF | Min RTF | Max RTF | Avg First PCM (ms) | Avg Total (ms) | Success |
|-------|---------|---------|---------|---------------------|----------------|---------|
| Q4_K_M | 1.266 | 1.266 | 1.266 | 253 | 27685 | 1/1 |

## Recommendation

**⚠️ Quality unverified**: Based on RTF and latency only.
**Audio quality has not been assessed**. Listen to PCM files before selecting.

- **Recommended quant**: Q4_K_M (RTF=1.266)
- **Status**: Slower than real time ❌ — consider Phase 8E

### Suggested backend environment
```bash
# Model: s2-pro-q4_k_m.gguf
S2_STREAM_DECODE_STRIDE_FRAMES=4
```

## Listening Checklist

- [ ] Clicks / pops
- [ ] Missing or repeated syllables
- [ ] Word stretching or unnatural pacing
- [ ] Robotic or metallic artifacts
- [ ] Voice consistency across runs
- [ ] Natural prosody and intonation
- [ ] Appropriate pauses
- [ ] Clipped word endings
- [ ] Overall preference ranking

## PCM Artifacts

Convert PCM to WAV for listening:
```bash
ffmpeg -f s16le -ar 44100 -ac 1 -i <file>.pcm <file>.wav
# ffmpeg available at /usr/bin/ffmpeg on Hermes Suite
```
# Q4_K_M Codec Context Screening

**Goal**: find the smallest codec context without audible tapping/blipping.
Fixed: Q4_K_M, threads=8, stride=4, holdback=0, low_latency=true.

## Results

| Context | RTF Mean | RTF Med | RTF Range | 1st PCM (ms) | Total (ms) |
|---------|----------|---------|-----------|--------------|------------|
| 4 | 1.008 | 1.006 | 1.004–1.013 | 247 | 22436 |
| 8 | 1.062 | 1.062 | 1.062–1.062 | 239 | 25262 |
| 12 | 1.108 | 1.108 | 1.108–1.108 | 246 | 24538 |
| 16 | 1.166 | 1.166 | 1.166–1.166 | 252 | 26046 |
| 24 | 1.266 | 1.266 | 1.266–1.266 | 253 | 27685 |
| 32 | 1.374 | 1.374 | 1.374–1.374 | 257 | 32680 |
| 48 | 1.573 | 1.573 | 1.573–1.573 | 239 | 35641 |
| 64 | 1.760 | 1.761 | 1.757–1.761 | 245 | 38222 |

## Listening Guidance

For each context, listen to the WAV file and assess:
- Tapping/blipping at word boundaries
- Overall voice quality
- Artifacts not present at context 4

**Select the smallest context that eliminates audible tapping.**
Context 64 is for reference — do NOT automatically select it.

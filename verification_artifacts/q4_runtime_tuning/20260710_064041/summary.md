# Q4 Runtime Tuning Results

**Q4_K_M, stride 4 fixed**

## Thread Sweep

| Config | Success | RTF Mean | RTF Med | RTF Min | RTF Max | 1st PCM (ms) |
|--------|---------|----------|---------|---------|---------|--------------|
| threads_0 | 3/3 | 1.004 | 1.000 | 0.997 | 1.013 | 226 |
| threads_16 | 3/3 | 0.968 | 0.971 | 0.960 | 0.974 | 216 |
| threads_24 | 3/3 | 0.988 | 0.991 | 0.982 | 0.991 | 219 |
| threads_32 | 3/3 | 1.006 | 1.008 | 1.001 | 1.010 | 226 |
| threads_8 | 3/3 | 0.954 | 0.952 | 0.949 | 0.962 | 208 |

## Recommendation

⚠️ **PROVISIONAL** — human listening of blipping WAVs required.

- Speed winner: lowest RTF configuration from thread/affinity sweeps
- Quality: evaluate blipping diagnostic WAVs before final selection
- Do NOT promote automatically

# Q4 Runtime Tuning Results

**Q4_K_M, stride 4 fixed**

## Blipping Diagnostic

| Config | Success | RTF Mean | RTF Med | RTF Min | RTF Max | 1st PCM (ms) |
|--------|---------|----------|---------|---------|---------|--------------|
| blip_ctx4_hb0 | 3/3 | 0.954 | 0.954 | 0.953 | 0.954 | 211 |
| blip_ctx64_hb0 | 3/3 | 1.711 | 1.713 | 1.704 | 1.715 | 211 |
| blip_ctx64_hb1 | 3/3 | 1.728 | 1.727 | 1.724 | 1.733 | 210 |

## Recommendation

⚠️ **PROVISIONAL** — human listening of blipping WAVs required.

- Speed winner: lowest RTF configuration from thread/affinity sweeps
- Quality: evaluate blipping diagnostic WAVs before final selection
- Do NOT promote automatically

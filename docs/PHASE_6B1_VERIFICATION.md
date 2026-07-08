# Phase 6B1: Live Deployment Verification

**Date:** 2026-07-08
**Wrapper image:** `ghcr.io/sorilo/wyoming-s2cpp-tts:sha-bbf8dfe`

## Objective

Verify the deployed Wyoming TTS wrapper through the actual Wyoming protocol before
Home Assistant registration.

## Deployment Under Test

| Component | Host/IP | Port |
|-----------|---------|------|
| Wyoming wrapper | wyoming-s2cpp-tts (172.20.0.53) | 10200 |
| s2.cpp backend | s2cpp-backend (172.20.0.52) | 3030 |
| Docker network | sorilonet | — |

## Verification Results

### 1. DNS & TCP Connectivity PASS
Both containers reachable: DNS resolves, TCP connects.

### 2. Backend Direct Validation PASS
- Multipart/form-data: 44100 Hz, mono, s16le, ~80-100KB per utterance, ~1.2s response
- JSON POST: 400 (expected — backend is multipart-only)

### 3. Wyoming Describe PASS
Returns 1 TTS program, 1 voice. (Hardcoded fake metadata; updated in fix.)

### 4. Wyoming Synthesize FAIL (FIXED)
**Symptom:** Connection closed immediately after Synthesize — zero response bytes.

**Root Cause:** synthesize_s2cpp_tts_events() called client.generate() (JSON POST).
The real s2.cpp backend only accepts multipart/form-data.
JSON -> HTTP 400 -> S2ClientError -> handler crash -> connection close.

**Fix:** Changed S2GenerateClient protocol from generate() to generate_multipart(),
updated synthesize_s2cpp_tts_events(), build_info_event(), and all test mocks.
277/277 tests pass.

### 5. Local Reference Flow PASS
AudioStart -> 6x AudioChunk -> AudioStop, 4ms total, progressive, frame-aligned.
WAV saved: verification_artifacts/phase6b1_local_fake_backend_(reference).wav

### 6. Updated Describe (post-fix)
Program: wyoming-s2cpp-tts, Voice: s2-pro [en, zh], 44100Hz mono s16le, streaming=True

## Home Assistant Registration

After rebuilding wrapper image:
1. Settings -> Devices & Services -> Add Integration -> Wyoming Protocol
2. Host: 192.168.1.45, Port: 10200
3. Service auto-discovered as wyoming-s2cpp-tts with s2-pro voice

## Remediation

Rebuild wrapper image: docker build -f docker/wrapper/Dockerfile -t ghcr.io/sorilo/wyoming-s2cpp-tts:fix-multipart .

## Files Changed
- app/wyoming_server.py — generate() -> generate_multipart(), dynamic Describe
- tests/test_wyoming_s2cpp_backend.py — mock update
- tests/test_metrics.py — mock updates
- verification_artifacts/phase6b1_report.json
- verification_artifacts/phase6b1_local_fake_backend_(reference).wav
- docs/PHASE_6B1_VERIFICATION.md (this file)

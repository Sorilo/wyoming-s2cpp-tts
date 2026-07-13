# Voice Profiles — Phase 11

## Overview

The voice-profile workstream adds structured management for ``.s2voice``
binary voice profiles used by the s2.cpp TTS backend.  Each profile can
optionally carry a JSON sidecar (``<id>.s2voice.json``) with license,
attribution, and provenance metadata.

## Binary Format (``.s2voice``)

Matches the upstream ``s2_voice.cpp`` format exactly:

| Offset | Type    | Size | Field            |
|--------|---------|------|------------------|
| 0      | char[8] | 8    | magic ``S2VOICE\0`` |
| 8      | uint32  | 4    | version (1)      |
| 12     | int32   | 4    | num_codebooks    |
| 16     | int32   | 4    | T_prompt         |
| 20     | int32   | 4    | sample_rate      |
| 24     | int32   | 4    | codebook_size    |
| 28     | uint64  | 8    | transcript_len   |
| 36     | uint64  | 8    | codes_size       |
| 44     | bytes   | var  | transcript (null-terminated UTF-8) |
| 44+N   | bytes   | var  | codes (int32_t array) |

Header is 44 bytes.  All integers are native little-endian.

### Compatibility Contract

Only ``num_codebooks``, ``codebook_size``, and ``sample_rate`` are
checked for compatibility.  ``T_prompt`` is explicitly excluded per
the upstream specification.

## Modules

### ``app.voice_schema.py``

- ``VOICE_SIDECAR_SCHEMA`` — JSON Schema (draft 2020-12) for sidecar files.
  Requires ``id``, ``license``, ``attribution``.  Optional: ``provenance``,
  ``description``, ``language``, ``gender``, ``tags``, ``notes``.
- ``VOICE_SIDECAR_EXAMPLE`` — A sanitised, invented example sidecar
  (contains NO real voices, audio, or transcripts).

### ``app.voice_profile.py``

- ``parse_s2voice(data)`` — Bounded parser with safety limits:
  - Max transcript length: 1 MB
  - Max codes size: 100 MB
  - Rejects: wrong magic/version, truncation, zero-length transcript,
    non-null-terminated transcript, trailing data, oversized lengths
- ``S2VoiceProfile`` dataclass with ``is_compatible()`` method
- ``compute_voice_hash(data)`` — SHA-256 hex digest
- ``verify_voice_hash(data, expected)`` — hash verification with error on mismatch
- ``generate_manifest(data, profile, voice_id, sidecar)`` — full manifest dict

### ``app.voice_cli.py``

Local-only CLI functions (no network, no URL downloader):

- ``cmd_validate(path)`` — Validate a .s2voice file (and optional sidecar)
- ``cmd_import(source, dest_dir, voice_id, force=False)`` —
  Atomic import using same-filesystem temp file + rename.
  Pre-validates source, rejects collisions by default,
  rejects unsafe IDs, rejects symlink destinations.
- ``cmd_audit(voice_dir)`` — Audit all profiles: validity, license,
  attribution, managed vs unmanaged status, hash, issues
- ``cmd_licenses(voice_dir)`` — License summary across all profiles

## Backward Compatibility

The existing ``app.voice_discovery.discover_voices()`` function remains
unchanged.  Unmanaged profiles (without sidecars) continue to be
discovered and served as they were before.  Sidecar files are never
read by the discovery path.

## Safety

- No runtime strict enforcement — CLI tools are advisory
- No production hooks — tools are operator-side only
- No URL downloader — all operations are local filesystem only
- No real voices/audio/transcripts committed — only synthetic test fixtures
- Temp files are cleaned up on import failure
- Symlinks are rejected at import destination (traversal prevention)

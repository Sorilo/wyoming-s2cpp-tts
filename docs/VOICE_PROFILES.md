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
  Requires ``id``, ``license``, ``attribution``. Optional fields include
  ``provenance``, ``hash_sha256``, ``description``, ``language``, ``gender``,
  ``tags``, and ``notes``.
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

## Offline import from local audio (Phase 11.1)

The backend image includes `/usr/local/bin/import-s2voice`. It converts a local
WAV, FLAC, MP3, M4A, OGG, Opus, WebM, or AAC reference into a managed
`<id>.s2voice` plus canonical `<id>.s2voice.json` sidecar. The importer has no
URL or download mode and makes no network requests. Use `--network none` for a
one-shot container as an additional boundary.

### Rights and privacy prerequisites

Only import audio you are authorized to use. Every import requires a license,
attribution, and provenance source. Prefer `--transcript-file` over
`--transcript` so the words are not stored in shell history. The exact
transcript is necessarily passed to the pinned local `s2` process and is
embedded in the generated binary, but it is redacted from importer JSON output
and errors.

Keep recordings and transcript files outside the repository, or under the
ignored `voice-import-inputs/` directory. Generated profiles, sidecars, common
reference-audio formats, `*.transcript.txt`, and validation WAVs are gitignored.
Review the sidecar before distributing a profile; metadata does not itself
create usage rights.

### Guarded dry-run

A dry-run validates all local paths, metadata, collision policy, and prints the
redacted FFmpeg/s2.cpp argv without starting either program or writing files:

Both dry-run and real import require `S2CPP_REVISION` from the packaged backend
image. It must be exactly 40 lowercase hexadecimal characters and identifies
the actual s2.cpp revision included in that image. Missing or malformed
metadata fails closed; abbreviated, uppercase, non-hexadecimal, and all other
invalid values are rejected. These failures are bounded, sanitized JSON and do
not expose the transcript. Successful dry-run JSON reports the validated
revision, and a real import records it in the generated `.s2voice.json`
sidecar's provenance.

```bash
docker run --rm --network none \
  --entrypoint /usr/local/bin/import-s2voice \
  -v "${MODEL_DIR}:/models:ro" \
  -v "${VOICE_DIR}:/voices:rw" \
  -v "${IMPORT_DIR}:/import:ro" \
  "${BACKEND_IMAGE}" \
  /import/reference.flac \
  --transcript-file /import/reference.transcript.txt \
  --id example-speaker \
  --license CC-BY-4.0 \
  --attribution "Example Speaker" \
  --provenance-source "authorized local recording" \
  --model /models/s2-pro-q4_k_m.gguf \
  --tokenizer /models/tokenizer.json \
  --voice-dir /voices \
  --dry-run
```

Use an immutable `sha-*` value or image digest for `BACKEND_IMAGE`, and select
only an image that passed exact-image CI. That CI verifies both the runtime
`S2CPP_REVISION` environment value and the
`wyoming-s2cpp-tts.s2cpp-revision` OCI label against the pinned revision.
Substitute only real local paths and rights metadata; do not copy the invented
values above blindly.

### Real import and active-server guard

Profile creation loads the GGUF model and performs one validation synthesis;
the pinned upstream tool has no encode-only mode. A second model-bearing process
can exhaust VRAM. Therefore a real import refuses while any exact
`s2 ... --server` process is visible. Dry-run remains available. The importer
never stops or restarts the backend automatically.

For a real import, the operator must deliberately stop the backend container,
run the one-shot command above with GPU access and without `--dry-run`, and then
restart the backend manually after checking the JSON result. Add the runtime's
normal GPU option (for example `--gpus all`) to `docker run`. Production restart
and Home Assistant validation are separate operator-controlled steps, not part
of the import command.

By default the validation synthesis WAV, normalized audio, and temporary files
are deleted. To retain the generated validation WAV, first create a directory
beneath the mounted voice directory and add, for example:

```text
--validation-wav /voices/validation/example-speaker.wav
```

The requested WAV must remain beneath `/voices`, may not traverse symlinks, and
must use the same filesystem. Existing profiles, sidecars, and validation WAVs
are never replaced unless `--force` is explicit. Placement uses same-filesystem
staging, no-overwrite publication, SHA-256 sidecar metadata, parser validation,
and rollback of earlier placements if the final profile commit fails.

After import, run the existing local audit commands and back up both
`<id>.s2voice` and `<id>.s2voice.json`. Restoring or rolling back requires the
matching pair; do not mix a profile with a sidecar carrying a different hash.

## Backward Compatibility

The existing ``app.voice_discovery.discover_voices()`` function remains
unchanged.  Unmanaged profiles (without sidecars) continue to be
discovered and served as they were before.  Sidecar files are never
read by the discovery path.

## Safety

- Local importer/management CLIs validate strictly; runtime voice discovery remains
  backward-compatible and does not enforce sidecars
- No production hooks — tools are operator-side only
- No URL downloader — all operations are local filesystem only
- No real voices/audio/transcripts committed — only synthetic test fixtures
- Temporary normalized/validation audio is cleaned unless retention is explicit
- Source and destination symlinks are rejected where they could cross trust boundaries
- Real generation refuses while `s2 --server` is active and never stops it

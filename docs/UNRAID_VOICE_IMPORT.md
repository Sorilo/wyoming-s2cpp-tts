# Unraid voice-import operator

## Purpose and execution boundary

The Unraid voice-import operator creates a managed `.s2voice` profile from an
authorized local recording and UTF-8 transcript. It is a daemon-free host-side
workflow intended to run manually through the Unraid **User Scripts** plugin or
an Unraid terminal.

Hermes does not need Docker-daemon access. Hermes may prepare inputs and review
the resulting sanitized report, but the trusted execution boundary is the
operator script that an authorized administrator runs on the Unraid host. The
operator uses the local Docker CLI only for one exact configured backend
container and one exact immutable backend image.

Development and automated tests use injected fakes. **No production Docker,
GPU import, Unraid installation, backend restart, Home Assistant change, or real
voice creation was performed while developing this operator.**

## Architecture and lifecycle

For a real import, the operator:

1. validates the recording, transcript file, configuration, model, tokenizer,
   destination paths, and immutable image identity;
2. inspects the exact configured backend container and records its initial
   state;
3. stops only that container when it was initially running;
4. runs `/usr/local/bin/import-s2voice` in a one-shot container using the exact
   validated backend image;
5. validates the generated profile, sidecar, optional requested validation WAV,
   ownership, modes, hashes, IDs, revisions, and staging cleanup;
6. restarts and health-checks the backend if and only if it was initially
   running, unless `--restart-backend` was explicitly requested; and
7. emits a bounded, sanitized JSON report.

An initially stopped backend remains stopped by default. After the operator has
stopped an initially running backend, importer failure, timeout, output
validation failure, `SIGINT`, and `SIGTERM` still enter bounded recovery. Import
and recovery failures are reported separately so one does not hide the other.

A nonblocking file lock rejects concurrent imports. The lock is released on all
normal and handled failure exits.

## Recommended persistent paths

The shipped example uses:

```text
/mnt/user/appdata/s2cpp/operator/             operator and config
/mnt/user/appdata/s2cpp/models/               GGUF model and tokenizer
/mnt/user/appdata/s2cpp/voices/               profiles and sidecars
/mnt/user/appdata/s2cpp/voice-import-inputs/  authorized audio/transcripts
/mnt/user/appdata/s2cpp/operator/reports/     sanitized reports
```

Keep recordings and transcripts outside Git. Back up the voices directory
before the first real import and before replacing an existing profile. A usable
backup contains the matching `.s2voice` and `.s2voice.json` pair.

## Install or export the operator

From a reviewed checkout of this repository on the Unraid host:

```bash
python3 scripts/install_unraid_voice_operator.py \
  /mnt/user/appdata/s2cpp/operator

cp /mnt/user/appdata/s2cpp/operator/config.env.example \
  /mnt/user/appdata/s2cpp/operator/config.env

mkdir -p /mnt/user/appdata/s2cpp/operator/reports
chmod 700 /mnt/user/appdata/s2cpp/operator
chmod 600 /mnt/user/appdata/s2cpp/operator/config.env
```

The installer exports only:

- `unraid_add_voice.py` (`0755`);
- `add-s2voice` (`0755`); and
- `config.env.example` (`0644`).

It rejects symlink targets and regular or dangling destination symlinks before
copying. It does not install a daemon or modify an existing Docker container.
Review the exported files and example configuration before creating
`config.env`.

## Configuration

Use `scripts/unraid_add_voice_config.env.example` as the key allowlist. Unknown
or duplicate keys fail closed. A representative deployment configuration is:

```dotenv
BACKEND_CONTAINER=s2cpp-backend
BACKEND_IMAGE=ghcr.io/OWNER/IMAGE:sha-0123456789abcdef0123456789abcdef01234567
MODELS_DIR=/mnt/user/appdata/s2cpp/models
VOICES_DIR=/mnt/user/appdata/s2cpp/voices
IMPORT_INPUTS_DIR=/mnt/user/appdata/s2cpp/voice-import-inputs
MODEL_CONTAINER_PATH=/models/s2-pro-q6_k.gguf
TOKENIZER_CONTAINER_PATH=/models/tokenizer.json
CUDA_DEVICE=0
GPU_LAYERS=99
STOP_TIMEOUT_SEC=30
IMPORT_TIMEOUT_SEC=600
RESTART_TIMEOUT_SEC=120
HEALTH_POLL_INTERVAL_SEC=2
HEALTH_POLL_TIMEOUT_SEC=120
EXPECTED_SOURCE_REVISION=0123456789abcdef0123456789abcdef01234567
EXPECTED_S2CPP_REVISION=0123456789abcdef0123456789abcdef01234567
LOCK_FILE=/mnt/user/appdata/s2cpp/operator/import.lock
```

Replace every invented value with the exact deployed value. Do not place
transcript text, credentials, Docker authentication, or other secrets in this
file.

### Immutable image and revision requirements

`BACKEND_IMAGE` must be immutable: either `sha256:` plus 64 lowercase
hexadecimal characters, or a tag ending in `:sha-` plus a full 40-character
lowercase Git revision. Mutable tags such as `latest` are rejected.

Before any stop or import, the operator verifies agreement among:

- the configured image reference;
- the image's resolved ID or digest;
- the production container's configured image and image ID;
- the required source-revision OCI label;
- the required s2.cpp-revision OCI label;
- the runtime revision environment value; and
- the configured full 40-character lowercase revisions.

Post-import validation separately requires
`provenance.s2cpp_revision` in the generated sidecar. `provenance` must be an
object, and the revision must be exactly 40 lowercase hexadecimal characters
and match `EXPECTED_S2CPP_REVISION`. Missing, uppercase, shortened, malformed,
or mismatched values fail closed; image metadata is not a substitute.

## Prepare inputs

Place an authorized reference recording and its transcript beneath
`IMPORT_INPUTS_DIR`. Both must be regular files, not symlinks. Model and
tokenizer paths must remain beneath `MODELS_DIR` and may not traverse symlinks.
Use only recordings and voices you have permission to process.

The transcript is mandatory and must be supplied using `--transcript-file`.
Never place transcript text directly in a command line. The planned command and
reports redact the transcript path where appropriate and never include its
contents.

## Dry-run

Dry-run validates inputs, exact container/image identity, revisions, and the
planned one-shot argv. It may inspect Docker state, but it does not acquire the
import lock, stop/start a container, run the importer, or write voice artifacts.

```bash
/mnt/user/appdata/s2cpp/operator/add-s2voice \
  --config /mnt/user/appdata/s2cpp/operator/config.env \
  --audio /mnt/user/appdata/s2cpp/voice-import-inputs/example.wav \
  --transcript-file /mnt/user/appdata/s2cpp/voice-import-inputs/example.transcript.txt \
  --voice-id example-voice \
  --license permission-granted \
  --attribution "Authorized speaker" \
  --provenance-source "Authorized local recording" \
  --validation-wav-relative validation/example-voice.wav \
  --report-file /mnt/user/appdata/s2cpp/operator/reports/example-voice-dry-run.json \
  --dry-run
```

Review the JSON report and planned command before real execution. Ensure the
configured container is the intended backend and the resolved image/revisions
match the production deployment.

## Real import

Run the same reviewed command without `--dry-run` and use a different report
filename:

```bash
/mnt/user/appdata/s2cpp/operator/add-s2voice \
  --config /mnt/user/appdata/s2cpp/operator/config.env \
  --audio /mnt/user/appdata/s2cpp/voice-import-inputs/example.wav \
  --transcript-file /mnt/user/appdata/s2cpp/voice-import-inputs/example.transcript.txt \
  --voice-id example-voice \
  --license permission-granted \
  --attribution "Authorized speaker" \
  --provenance-source "Authorized local recording" \
  --validation-wav-relative validation/example-voice.wav \
  --report-file /mnt/user/appdata/s2cpp/operator/reports/example-voice-import.json
```

Use `--force` only after backing up and reviewing the existing matching profile
and sidecar. Use `--restart-backend` only when you intentionally want an
initially stopped backend started after the operation.

### Validation WAV

`--validation-wav-relative` is optional. When omitted, no retained validation
WAV is required. Once requested, the exact relative output is mandatory: it
must exist, be a nonempty regular file, not be a symlink, and resolve beneath
the configured voices directory. A successful importer exit with a missing or
invalid requested WAV is an artifact-validation failure, not overall success;
normal backend recovery still runs.

## Docker permissions and prohibitions

The operator permits only these bounded Docker operations:

- `docker inspect <exact-container>`;
- `docker image inspect <exact-immutable-image>`;
- `docker stop --time <bounded-seconds> <exact-container>`;
- `docker run` for the validated one-shot importer; and
- `docker start <exact-container>` when lifecycle recovery requires it.

The importer uses an argv array, an explicit entrypoint, `--rm`,
`--network none`, GPU device selection, and exactly these data mounts:

- models: `/models:ro`;
- import inputs: `/inputs:ro`; and
- voices: `/voices:rw`.

The operator does **not** use `shell=True`, `os.system`, `eval`, or `exec`; mount
the Docker socket; use privileged mode; remove containers, images, volumes, or
networks; build or pull images; publish ports; use wildcard container
operations; or alter the wrapper container. Never broaden the host account's
Docker permissions beyond what the approved User Script needs.

## Reports and troubleshooting

Reports are written to stdout and, when requested, atomically to an existing
report parent directory. They include schema version, timestamps, overall
status, lifecycle history, voice/file names, backend initial/final states,
restart and recovery evidence, configured/resolved image identity,
runtime/OCI/s2.cpp revisions, importer and post-import outcomes, timings,
profile/sidecar paths and SHA-256, ownership/modes, validation-WAV evidence,
flags, and bounded warnings/errors.

Reports exclude transcript contents, broad environment dumps, Docker
authentication, unbounded child output, and raw commands containing private
transcript text.

Common fail-closed results:

- **Identity or revision failure:** compare `config.env` with `docker inspect`
  and `docker image inspect`; do not bypass or shorten a revision.
- **Lock busy:** wait for the active import to finish. Confirm no operator is
  running before considering stale-lock maintenance.
- **Input/path rejection:** remove traversal or symlinks and keep every input
  beneath its configured root.
- **Importer timeout/failure:** preserve the sanitized report and confirm the
  backend recovery result before retrying.
- **Artifact-validation failure:** inspect the profile, sidecar provenance/hash,
  requested validation WAV, ownership/modes, and staging residue.
- **Recovery failure:** restore the exact backend container manually and verify
  health before any new import.

Do not delete the source recording, transcript, existing profile, or report
until backup and validation are complete.

## Manual Wyoming and Home Assistant validation

A successful operator report proves only the host import and bounded backend
recovery. It does not prove Wyoming discovery, synthesis, Home Assistant use, or
subjective voice quality.

After a successful real import, an authorized operator must still:

1. confirm the backend is in its intended final state and healthy;
2. verify the new voice is discovered through the Wyoming wrapper;
3. synthesize a short test through the real Wyoming path;
4. select and synthesize the voice through Home Assistant; and
5. listen for intelligibility, identity, artifacts, and acceptable quality.

Wyoming Whisper auto-transcription is deferred to a later phase. The Phase 11.2
operator requires a prepared transcript file and does not add transcription.

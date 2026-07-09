#!/bin/bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/capture_phase_8b1_logs.sh [--duration SECONDS] [--outdir DIR] [WRAPPER] [BACKEND]

Captures Phase 8B1 wrapper/backend logs, GPU samples, timestamps, image IDs,
and container health/status. With --duration it exits unattended after the
specified number of seconds. Without --duration it prompts only when stdin is a
TTY; in non-interactive/background use it runs until SIGINT/SIGTERM.
USAGE
}

DURATION=""
OUTDIR="verification_artifacts/phase_8b1"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --duration)
      DURATION="${2:?--duration requires seconds}"
      shift 2
      ;;
    --outdir)
      OUTDIR="${2:?--outdir requires a path}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -* )
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      break
      ;;
  esac
done

WRAPPER="${1:-wyoming-s2cpp-tts}"
BACKEND="${2:-s2cpp-backend-diag}"
mkdir -p "$OUTDIR"

START_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
STOP_REASON="signal"
WPID=""
BPID=""
GPID=""

capture_identity() {
  local container="$1"
  local prefix="$2"
  local image status health
  image=$(docker inspect "$container" --format '{{.Config.Image}}' 2>&1 || true)
  status=$(docker inspect "$container" --format '{{.State.Status}}' 2>&1 || true)
  health=$(docker inspect "$container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>&1 || true)
  printf '%s_image=%s\n%s_status=%s\n%s_health=%s\n' "$prefix" "$image" "$prefix" "$status" "$prefix" "$health"
}

write_metadata() {
  local end_ts="$1"
  local metadata="$OUTDIR/capture-metadata.json"
  local wrapper_image backend_image wrapper_status backend_status wrapper_health backend_health
  wrapper_image=$(docker inspect "$WRAPPER" --format '{{.Config.Image}}' 2>&1 || true)
  backend_image=$(docker inspect "$BACKEND" --format '{{.Config.Image}}' 2>&1 || true)
  wrapper_status=$(docker inspect "$WRAPPER" --format '{{.State.Status}}' 2>&1 || true)
  backend_status=$(docker inspect "$BACKEND" --format '{{.State.Status}}' 2>&1 || true)
  wrapper_health=$(docker inspect "$WRAPPER" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>&1 || true)
  backend_health=$(docker inspect "$BACKEND" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>&1 || true)
  METADATA_PATH="$metadata" \
  START_TS_JSON="$START_TS" \
  END_TS_JSON="$end_ts" \
  STOP_REASON_JSON="$STOP_REASON" \
  WRAPPER_JSON="$WRAPPER" \
  BACKEND_JSON="$BACKEND" \
  WRAPPER_IMAGE_JSON="$wrapper_image" \
  BACKEND_IMAGE_JSON="$backend_image" \
  WRAPPER_STATUS_JSON="$wrapper_status" \
  BACKEND_STATUS_JSON="$backend_status" \
  WRAPPER_HEALTH_JSON="$wrapper_health" \
  BACKEND_HEALTH_JSON="$backend_health" \
  python3 - <<'METADATA_PY'
import json
import os
from pathlib import Path
metadata = {
    "start_utc": os.environ["START_TS_JSON"],
    "end_utc": os.environ["END_TS_JSON"],
    "stop_reason": os.environ["STOP_REASON_JSON"],
    "wrapper": os.environ["WRAPPER_JSON"],
    "backend": os.environ["BACKEND_JSON"],
    "wrapper_image": os.environ["WRAPPER_IMAGE_JSON"],
    "backend_image": os.environ["BACKEND_IMAGE_JSON"],
    "wrapper_status": os.environ["WRAPPER_STATUS_JSON"],
    "backend_status": os.environ["BACKEND_STATUS_JSON"],
    "wrapper_health": os.environ["WRAPPER_HEALTH_JSON"],
    "backend_health": os.environ["BACKEND_HEALTH_JSON"],
}
Path(os.environ["METADATA_PATH"]).write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
METADATA_PY
}

cleanup() {
  local end_ts
  end_ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  for pid in ${WPID:-} ${BPID:-} ${GPID:-}; do
    if [[ -n "$pid" ]]; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  wait ${WPID:-} ${BPID:-} ${GPID:-} 2>/dev/null || true
  write_metadata "$end_ts"
  echo "Capture: $START_TS -> $end_ts"
  echo "Metadata: $OUTDIR/capture-metadata.json"
  echo "Events found:"
  grep -c "cancel" "$OUTDIR/backend-live.log" 2>/dev/null || echo "0 backend cancel events"
  grep -c "disconnect" "$OUTDIR/wrapper-live.log" 2>/dev/null || echo "0 wrapper disconnect events"
}
trap cleanup EXIT
trap 'STOP_REASON=signal; exit 0' INT TERM

echo "=== Phase 8B1 Log Capture ==="
echo "Start: $START_TS"
echo "Wrapper: $WRAPPER  Backend: $BACKEND"
echo "Output: $OUTDIR"
echo "Duration: ${DURATION:-until signal/manual stop}"
echo

capture_identity "$WRAPPER" wrapper > "$OUTDIR/container-identities.txt" || true
capture_identity "$BACKEND" backend >> "$OUTDIR/container-identities.txt" || true

# Capture existing logs and then live logs.
docker logs "$WRAPPER" --tail 500 2>&1 > "$OUTDIR/wrapper-pre.log" || true
docker logs "$BACKEND" --tail 500 2>&1 > "$OUTDIR/backend-pre.log" || true

docker logs -f "$WRAPPER" 2>&1 > "$OUTDIR/wrapper-live.log" &
WPID=$!
docker logs -f "$BACKEND" 2>&1 > "$OUTDIR/backend-live.log" &
BPID=$!

(
  while true; do
    echo "=== $(date -u +%H:%M:%S) ===" >> "$OUTDIR/nvidia-smi.log"
    if command -v nvidia-smi >/dev/null 2>&1; then
      nvidia-smi --query-gpu=utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu --format=csv,noheader 2>&1 >> "$OUTDIR/nvidia-smi.log" || true
    else
      echo "nvidia-smi not available" >> "$OUTDIR/nvidia-smi.log"
    fi
    sleep 2
  done
) &
GPID=$!

echo "Loggers running (PIDs: w=$WPID b=$BPID g=$GPID)"
echo "Run: PYTHONPATH=. .venv/bin/python scripts/live_verify_phase_8b1.py --host <ip> --port 10200"

if [[ -n "$DURATION" ]]; then
  STOP_REASON="duration"
  sleep "$DURATION"
elif [[ -t 0 ]]; then
  echo "Press Enter when done to stop..."
  read -r
  STOP_REASON="manual"
else
  echo "No TTY detected; running until SIGINT/SIGTERM. Use --duration for unattended runs."
  while true; do sleep 3600; done
fi

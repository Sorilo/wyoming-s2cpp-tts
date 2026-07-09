#!/bin/bash
set -euo pipefail
WRAPPER="${1:-wyoming-s2cpp-tts}"
BACKEND="${2:-s2cpp-backend-diag}"
OUTDIR="verification_artifacts/phase_8b1"
mkdir -p "$OUTDIR"
START_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "=== Phase 8B1 Log Capture ==="
echo "Start: $START_TS"
echo "Wrapper: $WRAPPER  Backend: $BACKEND"
echo "Output: $OUTDIR"
echo

# Capture existing logs
docker logs "$WRAPPER" --tail 500 2>&1 > "$OUTDIR/wrapper-pre.log" || true
docker logs "$BACKEND" --tail 500 2>&1 > "$OUTDIR/backend-pre.log" || true

# Start captures
docker logs -f "$WRAPPER" 2>&1 > "$OUTDIR/wrapper-live.log" &
WPID=$!
docker logs -f "$BACKEND" 2>&1 > "$OUTDIR/backend-live.log" &
BPID=$!
while true; do
  echo "=== $(date -u +%H:%M:%S) ===" >> "$OUTDIR/nvidia-smi.log"
  nvidia-smi --query-gpu=utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu --format=csv,noheader 2>&1 >> "$OUTDIR/nvidia-smi.log"
  sleep 2
done &
GPID=$!

echo "Loggers running (PIDs: w=$WPID b=$BPID g=$GPID)"
echo "Run: python3 scripts/live_verify_phase_8b1.py --host <ip> --port 10200"
echo "Press Enter when done to stop..."
read -r

kill $WPID $BPID $GPID 2>/dev/null || true
wait $WPID $BPID $GPID 2>/dev/null || true
END_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "Capture: $START_TS -> $END_TS"
echo "Events found:"
grep -c "cancel" "$OUTDIR/backend-live.log" 2>/dev/null || echo "0 backend cancel events"
grep -c "disconnect" "$OUTDIR/wrapper-live.log" 2>/dev/null || echo "0 wrapper disconnect events"

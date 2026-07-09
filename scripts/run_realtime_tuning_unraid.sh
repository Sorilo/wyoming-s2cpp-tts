#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# Unraid host-side real-time stride tuning orchestration script
#
# This is the ONLY script you need to run on your Unraid host after pulling
# the commit.  Default behavior is benchmark-only and safe — no containers
# are modified unless you explicitly pass --apply with --yes.
#
# Usage:
#   # Safe benchmark (recommended first run)
#   bash scripts/run_realtime_tuning_unraid.sh --benchmark
#
#   # Capture system state without benchmarking
#   bash scripts/run_realtime_tuning_unraid.sh --capture-only
#
#   # Apply a winning stride to the wrapper container
#   bash scripts/run_realtime_tuning_unraid.sh --apply 4 --yes
#
#   # Restore previous wrapper environment
#   bash scripts/run_realtime_tuning_unraid.sh --restore
# ────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Colour helpers ─────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'
CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }

# ── Configuration ──────────────────────────────────────────────────────────
WRAPPER_CONTAINER="wyoming-s2cpp-tts"
BACKEND_CONTAINER="s2cpp-backend"
BACKEND_ENDPOINT="${BACKEND_ENDPOINT:-s2cpp-backend:3030}"
BENCHMARK_TEXT="${BENCHMARK_TEXT:-The quick brown fox jumps over the lazy dog. This is a benchmark test for real time speech synthesis performance.}"
STRIDES="${STRIDES:-1,2,4,8}"
CODEC_CONTEXT="${CODEC_CONTEXT:-4}"
WARMUP_RUNS="${WARMUP_RUNS:-1}"
MEASURED_RUNS="${MEASURED_RUNS:-3}"

# ── Resolve repository root ────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ARTIFACT_BASE="$REPO_ROOT/verification_artifacts/realtime_tuning"

# ── Parse arguments ────────────────────────────────────────────────────────
MODE=""
APPLY_STRIDE=""
APPLY_YES=false

if [[ $# -eq 0 ]]; then
    info "No mode specified — defaulting to --benchmark"
    MODE="benchmark"
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --benchmark)     MODE="benchmark"; shift ;;
        --capture-only)  MODE="capture"; shift ;;
        --apply)         MODE="apply"; APPLY_STRIDE="$2"; shift 2 ;;
        --restore)       MODE="restore"; shift ;;
        --yes)           APPLY_YES=true; shift ;;
        *)               err "Unknown argument: $1"; exit 1 ;;
    esac
done

# ── Mode: apply safety gate ────────────────────────────────────────────────
if [[ "$MODE" == "apply" ]] && [[ "$APPLY_YES" != true ]]; then
    err "--apply requires --yes for confirmation"
    err "Usage: bash $0 --apply STRIDE --yes"
    exit 1
fi

# ── Verify required commands ───────────────────────────────────────────────
info "Verifying required commands..."
for cmd in docker curl python3 nvidia-smi git; do
    if ! command -v "$cmd" &>/dev/null; then
        err "$cmd not found — is it installed?"
        exit 1
    fi
done
ok "All required commands found"

# ── Git identity ───────────────────────────────────────────────────────────
COMMIT_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo 'unknown')"
COMMIT_SHORT="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown')"
info "Repository: $REPO_ROOT"
info "Git commit: $COMMIT_SHORT ($BRANCH)"

# ── Timestamped artifact directory ─────────────────────────────────────────
TIMESTAMP="$(date -u +%Y%m%d_%H%M%S)"
ARTIFACT_DIR="$ARTIFACT_BASE/$TIMESTAMP"
mkdir -p "$ARTIFACT_DIR"
info "Artifact directory: $ARTIFACT_DIR"

# ── Capture system state ───────────────────────────────────────────────────
capture_system_state() {
    local dir="$1"
    info "Capturing system state..."

    # Container info
    {
        echo "=== Container Status ==="
        docker inspect "$WRAPPER_CONTAINER" 2>/dev/null || echo "(wrapper not found)"
        echo ""
        echo "=== Backend Status ==="
        docker inspect "$BACKEND_CONTAINER" 2>/dev/null || echo "(backend not found)"
        echo ""
        echo "=== Container Logs (last 50 lines) ==="
        echo "--- Wrapper ---"
        docker logs --tail 50 "$WRAPPER_CONTAINER" 2>/dev/null || echo "(no wrapper logs)"
        echo "--- Backend ---"
        docker logs --tail 50 "$BACKEND_CONTAINER" 2>/dev/null || echo "(no backend logs)"
        echo ""
        echo "=== Git ==="
        echo "commit=$COMMIT_SHA"
        echo "branch=$BRANCH"
        echo ""
        echo "=== Timestamp ==="
        date -u +"%Y-%m-%dT%H:%M:%SZ"
    } > "$dir/system_state.txt"

    # Wrapper environment
    docker inspect "$WRAPPER_CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' \
        2>/dev/null > "$dir/wrapper_env.txt" || echo "(not found)" > "$dir/wrapper_env.txt"

    # GPU info
    {
        echo "=== nvidia-smi ==="
        nvidia-smi --query-gpu=name,uuid,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw,clocks.sm,clocks.mem,pstate \
            --format=csv,noheader 2>/dev/null || echo "(nvidia-smi not available)"
    } > "$dir/gpu_info.txt"

    ok "System state captured"
}

capture_system_state "$ARTIFACT_DIR"

# ── Mode: capture-only ─────────────────────────────────────────────────────
if [[ "$MODE" == "capture" ]]; then
    info "Capture-only mode — no benchmark or deployment changes made."
    info "Artifacts: $ARTIFACT_DIR"
    exit 0
fi

# ── Mode: restore ──────────────────────────────────────────────────────────
if [[ "$MODE" == "restore" ]]; then
    ROLLBACK_FILE="$REPO_ROOT/verification_artifacts/realtime_tuning/rollback.env"
    if [[ ! -f "$ROLLBACK_FILE" ]]; then
        err "No rollback file found at $ROLLBACK_FILE"
        err "Cannot restore — no previous apply was recorded."
        exit 1
    fi
    warn "This will restore the wrapper environment from a previous apply."
    warn "Rollback file: $ROLLBACK_FILE"
    warn ""
    warn "⚠️  Manual container update may be required."
    warn "    Unraid WebUI → Docker → wyoming-s2cpp-tts → Edit →"
    warn "    Update environment variables, then Apply."
    echo ""
    cat "$ROLLBACK_FILE"
    exit 0
fi

# ── Mode: apply ────────────────────────────────────────────────────────────
if [[ "$MODE" == "apply" ]]; then
    warn ""
    warn "╔══════════════════════════════════════════════════════════════╗"
    warn "║  APPLY MODE — this STRIDE will be applied to the wrapper    ║"
    warn "╚══════════════════════════════════════════════════════════════╝"
    warn ""
    warn "Stride to apply: $APPLY_STRIDE"
    warn ""
    warn "⚠️  This script does NOT automatically recreate your container."
    warn "    Update these values in Unraid WebUI:"
    warn "      Docker → wyoming-s2cpp-tts → Edit"
    warn ""

    # Save rollback file
    ROLLBACK_FILE="$ARTIFACT_DIR/rollback.env"
    docker inspect "$WRAPPER_CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' \
        2>/dev/null > "$ROLLBACK_FILE" || true
    echo "# Rollback saved at $(date -u)" >> "$ROLLBACK_FILE"

    cat <<APPLYMSG

Suggested Unraid environment values:

  S2_STREAM_DECODE_STRIDE_FRAMES=$APPLY_STRIDE
  S2_STREAM_HOLDBACK_FRAMES=0
  S2_STREAM_START_BUFFER_MS=0
  S2_LOW_LATENCY=true
  S2_CODEC_CONTEXT_FRAMES=$CODEC_CONTEXT
  S2_SEGMENT_SENTENCES=false

Rollback file: $ROLLBACK_FILE
To restore previous settings:
  bash $0 --restore

⚠️  IMPORTANT: Listen to candidate audio BEFORE applying.
   RTF alone does not guarantee audio quality.
APPLYMSG
    exit 0
fi

# ── Mode: benchmark ────────────────────────────────────────────────────────
if [[ "$MODE" != "benchmark" ]]; then
    err "Unknown mode: $MODE"
    exit 1
fi

info "=== Starting Real-Time Stride Tuning Benchmark ==="
info "Backend endpoint: $BACKEND_ENDPOINT"
info "Strides: $STRIDES"
info "Codec context: $CODEC_CONTEXT"

# Run the Python benchmark harness
cd "$REPO_ROOT"

if [[ ! -f scripts/benchmark_realtime_tuning.py ]]; then
    err "benchmark_realtime_tuning.py not found in scripts/"
    exit 1
fi

python3 scripts/benchmark_realtime_tuning.py \
    --run-real \
    --endpoint "$BACKEND_ENDPOINT" \
    --text "$BENCHMARK_TEXT" \
    --strides "$STRIDES" \
    --codec-context "$CODEC_CONTEXT" \
    --warmup-runs "$WARMUP_RUNS" \
    --measured-runs "$MEASURED_RUNS" \
    --output-dir "$ARTIFACT_DIR"

info ""
info "╔══════════════════════════════════════════════════════════════╗"
info "║  BENCHMARK COMPLETE                                         ║"
info "╚══════════════════════════════════════════════════════════════╝"
info ""
info "Artifacts: $ARTIFACT_DIR"
info ""
info "Next steps:"
info "  1. Listen to generated PCM files:"
info "     ffmpeg -f s16le -ar 44100 -ac 1 -i stride4_run1.pcm stride4_run1.wav"
info ""
info "  2. Review the summary:"
info "     cat $ARTIFACT_DIR/summary.md"
info ""
info "  3. If satisfied with a candidate stride, apply it:"
info "     bash $0 --apply <STRIDE> --yes"
info ""
info "  ⚠️  RTF alone does not guarantee audio quality."
info "      You MUST listen to the candidate outputs before applying."
info ""

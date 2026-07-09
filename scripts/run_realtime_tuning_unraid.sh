#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# Unraid host-side real-time stride tuning orchestration script
#
# This is the ONLY script you need to run on your Unraid host after pulling
# the commit.  Default behavior is benchmark-only and safe — no containers
# are modified unless you explicitly pass --apply with --yes.
#
# IMPORTANT DISTINCTION:
#   - Direct-backend benchmark (--benchmark): works NOW against the running
#     s2cpp-backend container. No wrapper rebuild required.
#   - Home Assistant / Wyoming deployment: the NEW env vars
#     (S2_STREAM_DECODE_STRIDE_FRAMES, etc.) require a WRAPPER REBUILD with
#     these code changes. The current production wrapper
#     (ghcr.io/sorilo/wyoming-s2cpp-tts:sha-9c134cc) does NOT support them.
#
# Usage:
#   # Safe benchmark (recommended first run — works immediately)
#   bash scripts/run_realtime_tuning_unraid.sh --benchmark
#
#   # Capture system state without benchmarking
#   bash scripts/run_realtime_tuning_unraid.sh --capture-only
#
#   # See what settings would be applied (does NOT modify anything)
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
WRAPPER_CONTAINER="${WRAPPER_CONTAINER:-wyoming-s2cpp-tts}"
BACKEND_CONTAINER="${BACKEND_CONTAINER:-s2cpp-backend}"
BACKEND_ENDPOINT="${BACKEND_ENDPOINT:-s2cpp-backend:3030}"
BENCHMARK_TEXT="${BENCHMARK_TEXT:-The quick brown fox jumps over the lazy dog. This is a benchmark test for real time speech synthesis performance.}"
STRIDES="${STRIDES:-1,2,4,8}"
CODEC_CONTEXT="${CODEC_CONTEXT:-4}"
WARMUP_RUNS="${WARMUP_RUNS:-1}"
MEASURED_RUNS="${MEASURED_RUNS:-3}"

# Known wrapper images that support the Phase 8C stride tuning env vars.
# These are populated when a new wrapper image is published.
KNOWN_SUPPORTED_WRAPPER_IMAGES=(
    # "ghcr.io/sorilo/wyoming-s2cpp-tts:sha-NEWCOMMIT"  # TODO: add after publish
)

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
MISSING=""
for cmd in docker curl python3 nvidia-smi git; do
    if ! command -v "$cmd" &>/dev/null; then
        MISSING="$MISSING $cmd"
    fi
done
if [[ -n "$MISSING" ]]; then
    err "Missing required commands:$MISSING"
    exit 1
fi
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

    # Wrapper environment and image
    docker inspect "$WRAPPER_CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' \
        2>/dev/null > "$dir/wrapper_env.txt" || echo "(not found)" > "$dir/wrapper_env.txt"

    docker inspect "$WRAPPER_CONTAINER" --format '{{.Config.Image}}' \
        2>/dev/null > "$dir/wrapper_image.txt" || echo "(not found)" > "$dir/wrapper_image.txt"

    # GPU info
    {
        echo "=== nvidia-smi ==="
        nvidia-smi --query-gpu=name,uuid,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw,clocks.sm,clocks.mem,pstate \
            --format=csv,noheader 2>/dev/null || echo "(nvidia-smi not available)"
    } > "$dir/gpu_info.txt"

    ok "System state captured"
}

capture_system_state "$ARTIFACT_DIR"

# ── Helper: check if running wrapper supports Phase 8C env vars ────────────
wrapper_supports_stride_tuning() {
    # Get running wrapper image
    local wrapper_image
    wrapper_image="$(docker inspect "$WRAPPER_CONTAINER" --format '{{.Config.Image}}' 2>/dev/null || echo '')"

    if [[ -z "$wrapper_image" ]]; then
        return 1
    fi

    # Check against known supported images
    for supported in "${KNOWN_SUPPORTED_WRAPPER_IMAGES[@]}"; do
        if [[ "$wrapper_image" == "$supported" ]]; then
            return 0
        fi
    done

    return 1
}

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
    warn "⚠️  Manual container update required:"
    warn "    Unraid WebUI → Docker → $WRAPPER_CONTAINER → Edit →"
    warn "    Restore environment variables, then Apply."
    echo ""
    cat "$ROLLBACK_FILE"
    exit 0
fi

# ── Mode: apply ────────────────────────────────────────────────────────────
if [[ "$MODE" == "apply" ]]; then
    warn ""
    warn "╔══════════════════════════════════════════════════════════════╗"
    warn "║  APPLY MODE — stride tuning settings for the wrapper       ║"
    warn "╚══════════════════════════════════════════════════════════════╝"
    warn ""
    warn "Stride to apply: $APPLY_STRIDE"
    warn ""

    # Check wrapper support
    WRAPPER_IMAGE="$(docker inspect "$WRAPPER_CONTAINER" --format '{{.Config.Image}}' 2>/dev/null || echo 'unknown')"

    if wrapper_supports_stride_tuning; then
        ok "Running wrapper ($WRAPPER_IMAGE) supports Phase 8C stride tuning env vars."
        ok "You can set the environment variables below and restart the container."
    else
        warn "╔══════════════════════════════════════════════════════════════╗"
        warn "║  WRAPPER REBUILD REQUIRED                                   ║"
        warn "╚══════════════════════════════════════════════════════════════╝"
        warn ""
        warn "Your running wrapper image: $WRAPPER_IMAGE"
        warn "This image does NOT support the new stride tuning env vars."
        warn ""
        warn "The direct-backend benchmark (--benchmark) works NOW."
        warn "For Home Assistant / Wyoming to use these settings, you MUST:"
        warn ""
        warn "  1. Wait for a new wrapper image to be published from this branch."
        warn "  2. Pull the new image (e.g. ghcr.io/sorilo/wyoming-s2cpp-tts:sha-XXXXXXX)."
        warn "  3. Update the container to use the new image."
        warn "  4. THEN set the environment variables below."
        warn ""
        warn "Current production wrapper: ghcr.io/sorilo/wyoming-s2cpp-tts:sha-9c134cc"
        warn "This wrapper image does NOT support S2_STREAM_DECODE_STRIDE_FRAMES etc."
        warn ""
    fi

    # Always print the suggested settings
    echo ""
    info "Suggested Unraid environment values:"
    echo ""
    echo "  S2_STREAM_DECODE_STRIDE_FRAMES=$APPLY_STRIDE"
    echo "  S2_STREAM_HOLDBACK_FRAMES=0"
    echo "  S2_STREAM_START_BUFFER_MS=0"
    echo "  S2_LOW_LATENCY=true"
    echo "  S2_CODEC_CONTEXT_FRAMES=$CODEC_CONTEXT"
    echo "  S2_SEGMENT_SENTENCES=false"
    echo ""

    # Save rollback file
    ROLLBACK_FILE="$ARTIFACT_DIR/rollback.env"
    docker inspect "$WRAPPER_CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' \
        2>/dev/null > "$ROLLBACK_FILE" || true
    echo "# Rollback saved at $(date -u)" >> "$ROLLBACK_FILE"
    echo "# Wrapper image: $WRAPPER_IMAGE" >> "$ROLLBACK_FILE"

    info "Rollback file: $ROLLBACK_FILE"
    echo "To restore previous settings:"
    echo "  bash $0 --restore"
    echo ""
    warn "⚠️  IMPORTANT: Listen to candidate audio BEFORE applying."
    warn "   RTF alone does not guarantee audio quality."
    echo ""
    info "This script has NOT modified any containers, images, or running services."
    exit 0
fi

# ── Mode: benchmark ────────────────────────────────────────────────────────
if [[ "$MODE" != "benchmark" ]]; then
    err "Unknown mode: $MODE"
    exit 1
fi

info ""
info "╔══════════════════════════════════════════════════════════════╗"
info "║  DIRECT BACKEND BENCHMARK                                   ║"
info "║  Contacts s2cpp-backend directly — no wrapper involved.     ║"
info "║  Works immediately against the running backend container.   ║"
info "╚══════════════════════════════════════════════════════════════╝"
info ""

info "=== Starting Real-Time Stride Tuning Benchmark ==="
info "Backend endpoint: $BACKEND_ENDPOINT"
info "Strides: $STRIDES"
info "Codec context: $CODEC_CONTEXT"
info ""

# Verify Python and the benchmark harness
cd "$REPO_ROOT"

if [[ ! -f scripts/benchmark_realtime_tuning.py ]]; then
    err "benchmark_realtime_tuning.py not found in scripts/"
    exit 1
fi

# Verify the backend is reachable
info "Checking backend connectivity..."
if ! curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "http://$BACKEND_ENDPOINT/" &>/dev/null; then
    warn "Could not reach backend at $BACKEND_ENDPOINT"
    warn "The benchmark will likely fail. Check:"
    warn "  - Is the s2cpp-backend container running?"
    warn "  - Is the endpoint correct?"
    warn "  - Are you on the sorilonet Docker network?"
    warn ""
    warn "Continuing anyway..."
fi

# Run the Python benchmark harness with PYTHONPATH set to repo root
info "Running benchmark harness..."
PYTHONPATH="$REPO_ROOT" python3 scripts/benchmark_realtime_tuning.py \
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
info "     ffmpeg -f s16le -ar 44100 -ac 1 -i $ARTIFACT_DIR/stride4_run1.pcm stride4_run1.wav"
info ""
info "  2. Review the summary:"
info "     cat $ARTIFACT_DIR/summary.md"
info ""
info "  3. IMPORTANT DISTINCTION:"
info "     - These benchmark results are from DIRECT BACKEND calls."
info "     - They measure what the backend can achieve with different strides."
info "     - For Home Assistant / Wyoming to use these settings, you MUST"
info "       rebuild the wrapper with a new image that includes this code."
info "     - The current production wrapper (sha-9c134cc) does NOT support"
info "       S2_STREAM_DECODE_STRIDE_FRAMES or the other new env vars."
info ""
info "  4. To see what settings to apply (informational only):"
info "     bash $0 --apply <STRIDE> --yes"
info ""
info "  ⚠️  RTF alone does not guarantee audio quality."
info "      You MUST listen to the candidate outputs before applying."
info "      No wrapper rebuild has occurred — this was a direct benchmark."
info ""

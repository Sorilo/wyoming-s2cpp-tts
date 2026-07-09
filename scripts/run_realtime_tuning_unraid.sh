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
# --- Endpoint discovery ---
discover_endpoint() {
    if [[ -n "${USER_ENDPOINT:-}" ]]; then
        echo "$USER_ENDPOINT"
        info "Using user-supplied endpoint: $USER_ENDPOINT"
        return
    fi
    local host_port
    host_port=$(docker inspect "$BACKEND_CONTAINER" --format '{{range $p,$c := .NetworkSettings.Ports}}{{if eq $p "3030/tcp"}}{{range $c}}{{.HostPort}}{{end}}{{end}}{{end}}' 2>/dev/null || echo '')
    if [[ -n "$host_port" ]]; then
        echo "127.0.0.1:$host_port"
        info "Discovered backend at 127.0.0.1:$host_port (Docker published port)"
        return
    fi
    if curl -s --connect-timeout 2 "http://127.0.0.1:3031/" &>/dev/null; then
        echo "127.0.0.1:3031"
        info "Using fallback debug port: 127.0.0.1:3031"
        return
    fi
    if [[ -f /.dockerenv ]] || grep -q docker /proc/1/cgroup 2>/dev/null; then
        echo "s2cpp-backend:3030"
        info "Using container DNS: s2cpp-backend:3030 (detected container runtime)"
        return
    fi
    warn "Could not auto-discover backend endpoint."
    warn "Defaulting to s2cpp-backend:3030 (may not resolve from Unraid host)."
    echo "s2cpp-backend:3030"
}
BACKEND_ENDPOINT=$(discover_endpoint)
export BACKEND_ENDPOINT
DEFAULT_BENCHMARK_TEXT="The morning sun cast long shadows across the quiet neighborhood as residents began their daily routines. A gentle breeze carried the scent of fresh coffee from the corner cafe, where early customers sat reading newspapers and checking their phones. Children hurried past with backpacks slung over their shoulders, their laughter echoing off the brick buildings."
BENCHMARK_TEXT="${BENCHMARK_TEXT:-$DEFAULT_BENCHMARK_TEXT}"
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
LATEST_ROLLBACK="$ARTIFACT_BASE/latest_rollback.env"

# ── Parse arguments ────────────────────────────────────────────────────────
MODE=""
APPLY_STRIDE=""
APPLY_YES=false
RESTORE_FILE=""
USER_ENDPOINT=""

if [[ $# -eq 0 ]]; then
    info "No mode specified — defaulting to --benchmark"
    MODE="benchmark"
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --benchmark)     MODE="benchmark"; shift ;;
        --capture-only)  MODE="capture"; shift ;;
        --apply)
            MODE="apply"
            if [[ -z "${2:-}" || "${2:-}" == -* ]]; then
                err "--apply requires a stride value (positive integer 1-64)"
                err "Usage: bash $0 --apply STRIDE --yes"
                exit 1
            fi
            APPLY_STRIDE="$2"
            if ! [[ "$APPLY_STRIDE" =~ ^[0-9]+$ ]] || [ "$APPLY_STRIDE" -lt 1 ] || [ "$APPLY_STRIDE" -gt 64 ]; then
                err "Invalid stride: $APPLY_STRIDE (must be 1-64)"
                exit 1
            fi
            shift 2
            ;;
        --restore)
            MODE="restore"
            if [[ -n "${2:-}" && "${2:-}" != -* ]]; then
                RESTORE_FILE="$2"; shift 2
            else
                shift
            fi
            ;;
        --endpoint)
            if [[ -z "${2:-}" || "${2:-}" == -* ]]; then
                err "--endpoint requires a host:port value"
                exit 1
            fi
            USER_ENDPOINT="$2"; shift 2
            ;;
        --yes)           APPLY_YES=true; shift ;;
        *)               err "Unknown argument: $1"; exit 1 ;;
    esac
done

# ── Validate numeric config values ─────────────────────────────────────────
VALIDATE_ERRORS=""
for var_name in CODEC_CONTEXT WARMUP_RUNS MEASURED_RUNS; do
    val="${!var_name}"
    if ! [[ "$val" =~ ^[0-9]+$ ]] || [ "$val" -le 0 ]; then
        VALIDATE_ERRORS="$VALIDATE_ERRORS  $var_name=$val (must be positive integer)\n"
    fi
done
if ! [[ "$STRIDES" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
    VALIDATE_ERRORS="$VALIDATE_ERRORS  STRIDES=$STRIDES (must be comma-separated integers like 1,2,4,8)\n"
fi
case "$CODEC_CONTEXT" in
    4|64|160) ;;
    *) VALIDATE_ERRORS="$VALIDATE_ERRORS  CODEC_CONTEXT=$CODEC_CONTEXT (accepted: 4, 64, 160)\n" ;;
esac
if [[ -n "$VALIDATE_ERRORS" ]]; then
    err "Invalid configuration:"
    echo -e "$VALIDATE_ERRORS"
    exit 1
fi

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
    if [[ -n "$RESTORE_FILE" ]]; then
        ROLLBACK_FILE="$RESTORE_FILE"
    elif [[ -f "$LATEST_ROLLBACK" ]]; then
        ROLLBACK_FILE="$LATEST_ROLLBACK"
    else
        err "No rollback file specified and no latest_rollback.env found."
        err "Specify: bash $0 --restore <path/to/rollback.env>"
        err "Or run --apply first to create a rollback file."
        exit 1
    fi
    if [[ ! -f "$ROLLBACK_FILE" ]]; then
        err "Rollback file not found: $ROLLBACK_FILE"
        exit 1
    fi
    if ! grep -q "S2_" "$ROLLBACK_FILE" 2>/dev/null && ! grep -q "TTS_BACKEND" "$ROLLBACK_FILE" 2>/dev/null; then
        err "Rollback file does not appear to contain valid environment variables: $ROLLBACK_FILE"
        err "File contents (first 5 lines):"
        head -5 "$ROLLBACK_FILE"
        exit 1
    fi
    info "RESTORE - previous wrapper environment values"
    info "Rollback file: $ROLLBACK_FILE"
    echo ""
    cat "$ROLLBACK_FILE"
    echo ""
    warn "This script prints the previous values but does NOT modify containers."
    warn "To restore, manually update in Unraid WebUI:"
    warn "  Docker -> $WRAPPER_CONTAINER -> Edit -> restore env vars -> Apply"
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

    cp "$ROLLBACK_FILE" "$LATEST_ROLLBACK.tmp" && mv "$LATEST_ROLLBACK.tmp" "$LATEST_ROLLBACK"
    info "Rollback saved:"
    info "  $ROLLBACK_FILE"
    info "  $LATEST_ROLLBACK (latest)"
    echo ""
    echo "To restore previous settings (prints env values, does NOT modify containers):"
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

# Connectivity check (fail-stop)
info "Checking backend connectivity at $BACKEND_ENDPOINT ..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "http://$BACKEND_ENDPOINT/" 2>/dev/null || echo "000")
if [[ "$HTTP_CODE" == "000" ]]; then
    err "Cannot reach backend at http://$BACKEND_ENDPOINT/"
    err "TCP connection failed - the backend may not be running."
    err ""
    err "Troubleshooting:"
    err "  1. docker ps | grep $BACKEND_CONTAINER"
    err "  2. docker port $BACKEND_CONTAINER 3030"
    err "  3. Try: bash $0 --benchmark --endpoint 127.0.0.1:PORT"
    exit 1
elif [[ "$HTTP_CODE" == "404" ]]; then
    ok "Backend reachable (HTTP $HTTP_CODE) - /generate endpoint will be used."
    warn "Note: HTTP 404 on / is normal; the backend listens on /generate."
else
    ok "Backend reachable (HTTP $HTTP_CODE)"
fi

# GPU telemetry
GPU_TELEM_FILE="$ARTIFACT_DIR/gpu_telemetry.csv"
echo "timestamp,utilization_gpu,memory_used_mib,memory_total_mib,temperature_gpu,power_draw_w,clock_sm_mhz,clock_mem_mhz,pstate" > "$GPU_TELEM_FILE"
(while true; do ts="$(date -u +%Y-%m-%dT%H:%M:%S)"; gpu_data=$(nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,clocks.sm,clocks.mem,pstate --format=csv,noheader,nounits 2>/dev/null | head -1); [[ -n "$gpu_data" ]] && echo "$ts,$gpu_data" >> "$GPU_TELEM_FILE"; sleep 1; done) &
GPU_TELEM_PID=$!
info "GPU telemetry started (PID $GPU_TELEM_PID)"

# Backend log boundary
LOG_BOUNDARY=$(docker logs "$BACKEND_CONTAINER" --tail 1 2>/dev/null | head -1 || echo "")
echo "Log boundary: ${LOG_BOUNDARY:0:80}..." > "$ARTIFACT_DIR/log_boundary.txt"

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

# Capture backend metrics
info "Capturing backend metrics..."
BACKEND_METRICS_FILE="$ARTIFACT_DIR/backend_metrics.log"
if [[ -z "$LOG_BOUNDARY" ]]; then
    docker logs "$BACKEND_CONTAINER" --tail 100 2>/dev/null > "$BACKEND_METRICS_FILE" || true
else
    docker logs "$BACKEND_CONTAINER" 2>/dev/null | awk -v b="$LOG_BOUNDARY" 'found {print} $0 == b {found=1}' > "$BACKEND_METRICS_FILE" 2>/dev/null || true
fi
if [[ -f "$BACKEND_METRICS_FILE" ]]; then
    grep '\[Metrics\]' "$BACKEND_METRICS_FILE" > "$ARTIFACT_DIR/backend_metrics_parsed.txt" 2>/dev/null || true
    METRIC_COUNT=$(wc -l < "$ARTIFACT_DIR/backend_metrics_parsed.txt" 2>/dev/null || echo 0)
    ok "Backend metrics captured: $METRIC_COUNT [Metrics] lines"
fi

# Stop GPU telemetry
if [[ -n "${GPU_TELEM_PID:-}" ]] && kill -0 "$GPU_TELEM_PID" 2>/dev/null; then
    kill "$GPU_TELEM_PID" 2>/dev/null || true
    wait "$GPU_TELEM_PID" 2>/dev/null || true
    ok "GPU telemetry stopped"
fi
TELEM_LINES=$(wc -l < "$GPU_TELEM_FILE" 2>/dev/null || echo 0)
ok "GPU telemetry: $TELEM_LINES samples"

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

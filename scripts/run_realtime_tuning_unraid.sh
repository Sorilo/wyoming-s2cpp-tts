#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# Phase 8C: Unraid host-side real-time stride tuning orchestration script
#
# Default: --benchmark (safe, no container changes).  Requires --apply --yes
# to print deployment settings (never modifies containers automatically).
#
# Endpoint precedence: --endpoint > BACKEND_ENDPOINT env > Docker port >
# 127.0.0.1:3031 > container DNS (only inside container).
# ────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Colour helpers ─────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'
CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*" >&2; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*" >&2; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*" >&2; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ── Default configuration ──────────────────────────────────────────────────
WRAPPER_CONTAINER="${WRAPPER_CONTAINER:-wyoming-s2cpp-tts}"
BACKEND_CONTAINER="${BACKEND_CONTAINER:-s2cpp-backend}"
CODEC_CONTEXT="${CODEC_CONTEXT:-4}"
WARMUP_RUNS="${WARMUP_RUNS:-1}"
MEASURED_RUNS="${MEASURED_RUNS:-3}"

DEFAULT_BENCHMARK_TEXT="The morning sun cast long shadows across the quiet neighborhood as residents began their daily routines. A gentle breeze carried the scent of fresh coffee from the corner cafe, where early customers sat reading newspapers and checking their phones. Children hurried past with backpacks slung over their shoulders, their laughter echoing off the brick buildings."
BENCHMARK_TEXT="${BENCHMARK_TEXT:-$DEFAULT_BENCHMARK_TEXT}"

KNOWN_SUPPORTED_WRAPPER_IMAGES=()

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
STRIDES=""

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
        --strides)
            if [[ -z "${2:-}" || "${2:-}" == -* ]]; then
                err "--strides requires a comma-separated list"
                exit 1
            fi
            STRIDES="$2"; shift 2
            ;;
        --yes)           APPLY_YES=true; shift ;;
        *)               err "Unknown argument: $1"; exit 1 ;;
    esac
done

# Default strides after parsing
STRIDES="${STRIDES:-1,2,4,8}"

# ── Validate numeric config values ─────────────────────────────────────────
validate_positive_int() { [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -gt 0 ]; }
validate_non_negative_int() { [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -ge 0 ]; }
validate_port() { [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -ge 1 ] && [ "$1" -le 65535 ]; }

VALIDATE_ERRORS=""

# Codec context
case "$CODEC_CONTEXT" in
    4|64|160) ;;
    *) VALIDATE_ERRORS="${VALIDATE_ERRORS}  CODEC_CONTEXT=$CODEC_CONTEXT (accepted: 4, 64, 160)\n" ;;
esac

# Warmup runs (non-negative, 0 allowed)
if ! validate_non_negative_int "$WARMUP_RUNS"; then
    VALIDATE_ERRORS="${VALIDATE_ERRORS}  WARMUP_RUNS=$WARMUP_RUNS (must be non-negative integer)\n"
fi

# Measured runs (must be positive)
if ! validate_positive_int "$MEASURED_RUNS"; then
    VALIDATE_ERRORS="${VALIDATE_ERRORS}  MEASURED_RUNS=$MEASURED_RUNS (must be positive integer)\n"
fi

# STRIDES: validate each value
if ! [[ "$STRIDES" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
    VALIDATE_ERRORS="${VALIDATE_ERRORS}  STRIDES=$STRIDES (must be comma-separated integers like 1,2,4,8)\n"
else
    IFS=',' read -ra STRIDE_ARR <<< "$STRIDES"
    for s in "${STRIDE_ARR[@]}"; do
        if ! [[ "$s" =~ ^[0-9]+$ ]] || [ "$s" -lt 1 ] || [ "$s" -gt 64 ]; then
            VALIDATE_ERRORS="${VALIDATE_ERRORS}  STRIDES contains invalid value: $s (each must be 1-64)\n"
            break
        fi
    done
fi

if [[ -n "$VALIDATE_ERRORS" ]]; then
    err "Invalid configuration:"
    echo -e "$VALIDATE_ERRORS" >&2
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

# ── Endpoint discovery (AFTER argument parsing) ────────────────────────────
BACKEND_ENDPOINT_OVERRIDE="${BACKEND_ENDPOINT:-}"
BACKEND_ENDPOINT=""
ENDPOINT_DISCOVERY_METHOD=""

discover_endpoint() {
    # 1. User-supplied --endpoint
    if [[ -n "${USER_ENDPOINT:-}" ]]; then
        BACKEND_ENDPOINT="$USER_ENDPOINT"
        ENDPOINT_DISCOVERY_METHOD="user supplied (--endpoint)"
        return 0
    fi

    # 2. BACKEND_ENDPOINT environment variable
    if [[ -n "${BACKEND_ENDPOINT_OVERRIDE:-}" ]]; then
        BACKEND_ENDPOINT="$BACKEND_ENDPOINT_OVERRIDE"
        ENDPOINT_DISCOVERY_METHOD="environment variable"
        return 0
    fi

    # 3. Docker host-published port mapped to backend container 3030
    local host_port
    host_port=$(docker inspect "$BACKEND_CONTAINER" \
        --format '{{range $p,$c := .NetworkSettings.Ports}}{{if eq $p "3030/tcp"}}{{range $c}}{{.HostPort}}{{end}}{{end}}{{end}}' \
        2>/dev/null || echo '')
    if [[ -n "$host_port" ]] && [[ "$host_port" =~ ^[0-9]+$ ]] && [ "$host_port" -ge 1 ] && [ "$host_port" -le 65535 ]; then
        BACKEND_ENDPOINT="127.0.0.1:$host_port"
        ENDPOINT_DISCOVERY_METHOD="Docker published port ($host_port)"
        return 0
    fi

    # 4. Fallback: documented debug port
    if curl -s --connect-timeout 2 "http://127.0.0.1:3031/" &>/dev/null; then
        BACKEND_ENDPOINT="127.0.0.1:3031"
        ENDPOINT_DISCOVERY_METHOD="fallback debug port (3031)"
        return 0
    fi

    # 5. Container DNS (only if running inside a container)
    if [[ -f /.dockerenv ]] || grep -q docker /proc/1/cgroup 2>/dev/null; then
        BACKEND_ENDPOINT="s2cpp-backend:3030"
        ENDPOINT_DISCOVERY_METHOD="container DNS (s2cpp-backend:3030)"
        return 0
    fi

    # 6. Last resort
    BACKEND_ENDPOINT="s2cpp-backend:3030"
    ENDPOINT_DISCOVERY_METHOD="fallback (s2cpp-backend:3030, may not resolve)"
    warn "Could not auto-discover backend endpoint. Defaulting to s2cpp-backend:3030."
    return 0
}

discover_endpoint
info "Backend endpoint: $BACKEND_ENDPOINT"
info "Discovery method: $ENDPOINT_DISCOVERY_METHOD"

# ── Validate endpoint format ───────────────────────────────────────────────
validate_endpoint() {
    local ep="$1"
    if [[ -z "$ep" ]]; then
        return 1
    fi
    # Support host:port and IPv4:port
    if [[ "$ep" =~ ^[a-zA-Z0-9._-]+:[0-9]+$ ]] || [[ "$ep" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+$ ]]; then
        local port="${ep##*:}"
        validate_port "$port"
        return $?
    fi
    # Bracketed IPv6
    if [[ "$ep" =~ ^\[[0-9a-fA-F:]+\]:[0-9]+$ ]]; then
        local port="${ep##*]:}"
        validate_port "$port"
        return $?
    fi
    return 1
}

if ! validate_endpoint "$BACKEND_ENDPOINT"; then
    err "Invalid endpoint format: $BACKEND_ENDPOINT"
    err "Expected: host:port (e.g. 127.0.0.1:3031 or s2cpp-backend:3030)"
    exit 1
fi

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
        echo "=== Git ==="
        echo "commit=$COMMIT_SHA"
        echo "branch=$BRANCH"
        echo ""
        echo "=== Endpoint ==="
        echo "endpoint=$BACKEND_ENDPOINT"
        echo "discovery_method=$ENDPOINT_DISCOVERY_METHOD"
        echo ""
        echo "=== Timestamp ==="
        date -u +"%Y-%m-%dT%H:%M:%SZ"
    } > "$dir/system_state.txt"

    docker inspect "$WRAPPER_CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' \
        2>/dev/null > "$dir/wrapper_env.txt" || echo "(not found)" > "$dir/wrapper_env.txt"
    docker inspect "$WRAPPER_CONTAINER" --format '{{.Config.Image}}' \
        2>/dev/null > "$dir/wrapper_image.txt" || echo "(not found)" > "$dir/wrapper_image.txt"

    {
        echo "=== nvidia-smi ==="
        nvidia-smi --query-gpu=index,name,uuid,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw,clocks.sm,clocks.mem,pstate \
            --format=csv,noheader 2>/dev/null || echo "(nvidia-smi not available)"
    } > "$dir/gpu_snapshot.txt"
    ok "System state captured"
}

capture_system_state "$ARTIFACT_DIR"

# ── Helper: check if running wrapper supports Phase 8C env vars ────────────
wrapper_supports_stride_tuning() {
    local wrapper_image
    wrapper_image="$(docker inspect "$WRAPPER_CONTAINER" --format '{{.Config.Image}}' 2>/dev/null || echo '')"
    [[ -z "$wrapper_image" ]] && return 1
    for supported in "${KNOWN_SUPPORTED_WRAPPER_IMAGES[@]}"; do
        [[ "$wrapper_image" == "$supported" ]] && return 0
    done
    return 1
}

# ── GPU telemetry (all GPUs) ───────────────────────────────────────────────
GPU_TELEM_PID=""
GPU_TELEM_FILE=""

start_gpu_telemetry() {
    GPU_TELEM_FILE="$ARTIFACT_DIR/gpu_telemetry.csv"
    echo "timestamp,gpu_index,gpu_uuid,utilization_pct,memory_used_mib,memory_total_mib,temperature_c,power_w,sm_clock_mhz,mem_clock_mhz,pstate" > "$GPU_TELEM_FILE"
    (
        while true; do
            local ts
            ts="$(date -u +%Y-%m-%dT%H:%M:%S)"
            nvidia-smi --query-gpu=index,uuid,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,clocks.sm,clocks.mem,pstate \
                --format=csv,noheader,nounits 2>/dev/null | while IFS=, read -r idx uuid util mem_used mem_total temp power sm memclock pstate; do
                echo "$ts,$idx,$uuid,$util,$mem_used,$mem_total,$temp,$power,$sm,$memclock,$pstate"
            done >> "$GPU_TELEM_FILE"
            sleep 1
        done
    ) &
    GPU_TELEM_PID=$!
    info "GPU telemetry started (PID $GPU_TELEM_PID) -> $GPU_TELEM_FILE"
}

cleanup_telemetry() {
    if [[ -n "${GPU_TELEM_PID:-}" ]] && kill -0 "$GPU_TELEM_PID" 2>/dev/null; then
        kill -TERM -$GPU_TELEM_PID 2>/dev/null || kill "$GPU_TELEM_PID" 2>/dev/null || true
        ok "GPU telemetry stopped"
    fi
    GPU_TELEM_PID=""
}

# ── Backend metric capture per-run ─────────────────────────────────────────
capture_backend_metrics() {
    local label="$1" output="$2" since_ts="${3:-}"
    if [[ -n "$since_ts" ]]; then
        docker logs "$BACKEND_CONTAINER" --since "$since_ts" 2>/dev/null > "$output" || true
    else
        docker logs "$BACKEND_CONTAINER" --tail 50 2>/dev/null > "$output" || true
    fi
    # Extract [Metrics] lines for parsing
    grep '\[Metrics\]' "$output" 2>/dev/null > "${output}.metrics" || true
}

parse_metric_field() {
    local line="$1" field="$2"
    echo "$line" | grep -oP "(?<=\b${field}=)[0-9.]+" | head -1 || echo "null"
}

# ── Connectivity check ─────────────────────────────────────────────────────
check_connectivity() {
    local http_code
    info "Checking backend connectivity at $BACKEND_ENDPOINT ..."
    if ! http_code="$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 5 "http://$BACKEND_ENDPOINT/" 2>/dev/null)"; then
        http_code="000"
    fi

    if [[ "$http_code" == "000" ]]; then
        err "Cannot reach backend at http://$BACKEND_ENDPOINT/"
        err "TCP connection failed - the backend may not be running."
        err ""
        err "Troubleshooting:"
        err "  1. docker ps | grep $BACKEND_CONTAINER"
        err "  2. docker port $BACKEND_CONTAINER 3030"
        err "  3. Try: bash $0 --benchmark --endpoint 127.0.0.1:PORT"
        return 1
    elif [[ "$http_code" == "404" ]]; then
        ok "Backend reachable (HTTP $http_code) - /generate endpoint will be used."
        warn "Note: HTTP 404 on / is normal; the backend listens on /generate."
    else
        ok "Backend reachable (HTTP $http_code)"
    fi
    return 0
}

# ═══════════════════════════════════════════════════════════════════════════
# MODE DISPATCH
# ═══════════════════════════════════════════════════════════════════════════

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
    if ! grep -q "S2_\|TTS_BACKEND" "$ROLLBACK_FILE" 2>/dev/null; then
        err "Rollback file does not appear to contain valid environment variables: $ROLLBACK_FILE"
        head -5 "$ROLLBACK_FILE" >&2
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
    warn "APPLY MODE - stride tuning settings for the wrapper"
    warn ""
    warn "Stride to apply: $APPLY_STRIDE"
    warn ""

    WRAPPER_IMAGE="$(docker inspect "$WRAPPER_CONTAINER" --format '{{.Config.Image}}' 2>/dev/null || echo 'unknown')"

    if wrapper_supports_stride_tuning; then
        ok "Running wrapper ($WRAPPER_IMAGE) supports Phase 8C stride tuning env vars."
        ok "You can set the environment variables below and restart the container."
    else
        warn "WRAPPER REBUILD REQUIRED"
        warn "Your running wrapper image: $WRAPPER_IMAGE"
        warn "This image does NOT support the new stride tuning env vars."
        warn ""
        warn "The direct-backend benchmark (--benchmark) works NOW."
        warn "For Home Assistant / Wyoming to use these settings, you MUST:"
        warn "  1. Wait for a new wrapper image to be published from this branch."
        warn "  2. Pull the new image."
        warn "  3. Update the container to use the new image."
        warn "  4. THEN set the environment variables below."
        warn ""
        warn "Current production wrapper: ghcr.io/sorilo/wyoming-s2cpp-tts:sha-9c134cc"
    fi

    echo ""
    info "Suggested Unraid environment values:"
    echo "  S2_STREAM_DECODE_STRIDE_FRAMES=$APPLY_STRIDE"
    echo "  S2_STREAM_HOLDBACK_FRAMES=0"
    echo "  S2_STREAM_START_BUFFER_MS=0"
    echo "  S2_LOW_LATENCY=true"
    echo "  S2_CODEC_CONTEXT_FRAMES=$CODEC_CONTEXT"
    echo "  S2_SEGMENT_SENTENCES=false"
    echo ""

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
    warn "IMPORTANT: Listen to candidate audio BEFORE applying."
    warn "  RTF alone does not guarantee audio quality."
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
info "DIRECT BACKEND BENCHMARK"
info "Contacts s2cpp-backend directly - no wrapper involved."
info "Works immediately against the running backend container."
info ""

info "=== Starting Real-Time Stride Tuning Benchmark ==="
info "Backend endpoint: $BACKEND_ENDPOINT ($ENDPOINT_DISCOVERY_METHOD)"
info "Strides: $STRIDES"
info "Codec context: $CODEC_CONTEXT"
info "Text length: ${#BENCHMARK_TEXT} chars"
info ""

cd "$REPO_ROOT"

if [[ ! -f scripts/benchmark_realtime_tuning.py ]]; then
    err "benchmark_realtime_tuning.py not found in scripts/"
    exit 1
fi

# ── Connectivity check (fail-stop) ─────────────────────────────────────────
if ! check_connectivity; then
    exit 1
fi

# ── Install cleanup trap BEFORE starting telemetry ─────────────────────────
cleanup() {
    local exit_code=$?
    if [[ -n "${GPU_TELEM_PID:-}" ]] && kill -0 "$GPU_TELEM_PID" 2>/dev/null; then
        kill -TERM -$GPU_TELEM_PID 2>/dev/null || kill "$GPU_TELEM_PID" 2>/dev/null || true
        ok "GPU telemetry stopped (cleanup)"
    fi
    GPU_TELEM_PID=""
    exit $exit_code
}
trap cleanup EXIT INT TERM

# ── GPU telemetry ──────────────────────────────────────────────────────────
start_gpu_telemetry

# ── Per-stride, per-run benchmark with metric correlation ───────────────────
IFS=',' read -ra STRIDE_ARR <<< "$STRIDES"
RUN_INDEX=0
METRICS_JSON_FILE="$ARTIFACT_DIR/per_run_metrics.json"
echo "[" > "$METRICS_JSON_FILE"
FIRST_ENTRY=true

# Build base args shared across all runs
BASE_ARGS=(
    --run-real
    --endpoint "$BACKEND_ENDPOINT"
    --text "$BENCHMARK_TEXT"
    --codec-context "$CODEC_CONTEXT"
)

for stride in "${STRIDE_ARR[@]}"; do
    info "Stride $stride: warmup=$WARMUP_RUNS, measured=$MEASURED_RUNS"

    # Warm-up runs (use special --warmup flag with --single-run mode)
    for ((w=1; w<=WARMUP_RUNS; w++)); do
        info "  Warm-up ${w}/${WARMUP_RUNS}..."
        RUN_INDEX=$((RUN_INDEX + 1))
        RUN_LABEL="stride${stride}_warmup${w}"

        # Record timestamp for docker logs --since
        RUN_TS=$(date -u +%Y-%m-%dT%H:%M:%S)

        PYTHONPATH="$REPO_ROOT" python3 scripts/benchmark_realtime_tuning.py \
            "${BASE_ARGS[@]}" \
            --strides "$stride" \
            --warmup-runs 0 \
            --measured-runs 1 \
            --output-dir "$ARTIFACT_DIR" \
            --run-label "$RUN_LABEL" || warn "Warm-up ${w}/${WARMUP_RUNS} failed (stride $stride)"

        # Capture backend metrics for this run
        METRICS_FILE="$ARTIFACT_DIR/${RUN_LABEL}_metrics.log"
        capture_backend_metrics "$RUN_LABEL" "$METRICS_FILE" "$RUN_TS"
    done

    # Measured runs
    for ((m=1; m<=MEASURED_RUNS; m++)); do
        info "  Run ${m}/${MEASURED_RUNS}..."
        RUN_INDEX=$((RUN_INDEX + 1))
        RUN_LABEL="stride${stride}_run${m}"

        RUN_TS=$(date -u +%Y-%m-%dT%H:%M:%S)

        PYTHONPATH="$REPO_ROOT" python3 scripts/benchmark_realtime_tuning.py \
            "${BASE_ARGS[@]}" \
            --strides "$stride" \
            --warmup-runs 0 \
            --measured-runs 1 \
            --output-dir "$ARTIFACT_DIR" \
            --run-label "$RUN_LABEL"

        # Capture backend metrics for this run
        METRICS_FILE="$ARTIFACT_DIR/${RUN_LABEL}_metrics.log"
        capture_backend_metrics "$RUN_LABEL" "$METRICS_FILE" "$RUN_TS"

        # Parse metrics into JSON
        METRIC_LINES=$(grep '\[Metrics\]' "$METRICS_FILE" 2>/dev/null || true)
        if [[ -n "$METRIC_LINES" ]]; then
            while IFS= read -r mline; do
                if [[ "$FIRST_ENTRY" == true ]]; then FIRST_ENTRY=false; else echo "," >> "$METRICS_JSON_FILE"; fi
                cat >> "$METRICS_JSON_FILE" <<EOJ
  {
    "stride": $stride,
    "run": $m,
    "run_type": "measured",
    "run_label": "$RUN_LABEL",
    "raw": $(echo "$mline" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read().strip()))"),
    "generate_ms": $(parse_metric_field "$mline" "generate" || echo null),
    "stream_decode_ms": $(parse_metric_field "$mline" "stream_decode" || echo null),
    "stream_batches": $(parse_metric_field "$mline" "stream_batches" || echo null),
    "ar_only_ms": $(parse_metric_field "$mline" "ar_only" || echo null),
    "total_ms": $(parse_metric_field "$mline" "total" || echo null),
    "total_rtf": $(parse_metric_field "$mline" "total_rtf" || echo null)
  }
EOJ
            done <<< "$METRIC_LINES"
        fi
    done
done

echo "]" >> "$METRICS_JSON_FILE"

# Aggregate per-run results into canonical files
info "Aggregating per-run results..."
python3 scripts/aggregate_results.py "$ARTIFACT_DIR"

# ── Produce summary ────────────────────────────────────────────────────────
info ""
info "BENCHMARK COMPLETE"
info ""
info "Artifacts: $ARTIFACT_DIR"
info ""
info "Files:"
info "  summary.md                  - Markdown summary"
info "  results.json                - Full JSON results"
info "  gpu_telemetry.csv           - GPU samples (~1 Hz, all GPUs)"
info "  per_run_metrics.json        - Per-run backend metric correlation"
info "  stride*_run*_metrics.log    - Raw backend logs per run"
info "  stride*_run*.pcm            - Raw PCM audio artifacts"
info "  system_state.txt            - System/container/git state"
info ""
info "Next steps:"
info "  1. Listen to generated PCM files:"
info "     ffmpeg -f s16le -ar 44100 -ac 1 -i $ARTIFACT_DIR/stride4_run1.pcm stride4_run1.wav"
info "  2. Review per-run metrics:"
info "     cat $ARTIFACT_DIR/per_run_metrics.json | python3 -m json.tool"
info ""
info "  IMPORTANT: No wrapper rebuild has occurred."
info "  For HA/Wyoming deployment, a new wrapper image is required."
info "  RTF alone does not guarantee audio quality."
info "  No live RTX 3080 benchmark was performed."
info ""

exit 0

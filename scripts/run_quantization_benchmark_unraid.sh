#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# Phase 8D.2: Unraid host-side controlled quantization benchmark orchestrator.
#
# One candidate per backend process — the s2.cpp server loads ONE GGUF at
# startup via the S2_MODEL environment variable.  HTTP requests cannot switch
# models mid-process.
#
# Default: --dry-run (safe — no containers created or touched).
# Requires --run-real for live execution.
# ────────────────────────────────────────────────────────────────────────────

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'
CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*" >&2; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*" >&2; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*" >&2; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ── Configuration defaults ──────────────────────────────────────────────────
BENCH_CONTAINER="s2cpp-backend-bench"
BENCH_PORT="${BENCH_PORT:-3033}"
BACKEND_IMAGE="ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd"
GPU_UUID=""              # Set after discovery
STRIDE=4
CODEC_CONTEXT=4
WARMUP_RUNS=1
MEASURED_RUNS=3
TIMEOUT=120

# Known S2 Pro GGUF quants and approximate sizes
declare -A MODEL_SIZES=(
  ["s2-pro-q6_k.gguf"]="4.5"
  ["s2-pro-q5_k_m.gguf"]="4.0"
  ["s2-pro-q4_k_m.gguf"]="3.6"
)

# ── Resolve repository root ────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ARTIFACT_BASE="$REPO_ROOT/verification_artifacts/quant_benchmark"

# ── Parse arguments ────────────────────────────────────────────────────────
RUN_REAL=false
USER_CANDIDATES=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-real)  RUN_REAL=true; shift ;;
    --models)    USER_CANDIDATES="$2"; shift 2 ;;
    --port)      BENCH_PORT="$2"; shift 2 ;;
    --stride)    STRIDE="$2"; shift 2 ;;
    --gpu)       GPU_UUID="$2"; shift 2 ;;
    *)           err "Unknown argument: $1"; exit 1 ;;
  esac
done

# Candidates: Q6 (baseline), Q5, Q4
CANDIDATE_FILES=("s2-pro-q6_k.gguf" "s2-pro-q5_k_m.gguf" "s2-pro-q4_k_m.gguf")
CANDIDATE_LABELS=("q6_k" "q5_k_m" "q4_k_m")
BENCHMARK_TEXT="The morning sun cast long shadows across the quiet neighborhood as residents began their daily routines. A gentle breeze carried the scent of fresh coffee from the corner cafe, where early customers sat reading newspapers and checking their phones. Children hurried past with backpacks slung over their shoulders, their laughter echoing off the brick buildings."

# ── Pre-flight checks ──────────────────────────────────────────────────────
check_prereqs() {
  local missing=""
  for cmd in docker curl python3 nvidia-smi git; do
    command -v "$cmd" &>/dev/null || missing="$missing $cmd"
  done
  if [[ -n "$missing" ]]; then
    err "Missing required commands:$missing"
    exit 1
  fi
  ok "All required commands found"
}

# ── Discover host model mount ──────────────────────────────────────────────
discover_model_mount() {
  HOST_MODELS=$(docker inspect s2cpp-backend \
    --format '{{range .Mounts}}{{if eq .Destination "/models"}}{{.Source}}{{end}}{{end}}' \
    2>/dev/null || echo '')
  if [[ -z "$HOST_MODELS" ]]; then
    err "Cannot discover /models mount from production backend container."
    err "Is the s2cpp-backend container running?"
    exit 1
  fi
  info "Host model directory: $HOST_MODELS"
}

# ── Discover idle GPU ──────────────────────────────────────────────────────
discover_idle_gpu() {
  if [[ -n "$GPU_UUID" ]]; then
    info "Using user-supplied GPU: $GPU_UUID"
    return
  fi

  # Find GPUs and check utilization
  local production_gpu="GPU-65b9a886-d157-27fa-09d1-8894bc5cc135"
  local all_gpus
  all_gpus=$(nvidia-smi --query-gpu=uuid,utilization.gpu --format=csv,noheader,nounits 2>/dev/null)

  if [[ -z "$all_gpus" ]]; then
    err "nvidia-smi returned no GPUs"
    exit 1
  fi

  while IFS=, read -r uuid util; do
    uuid=$(echo "$uuid" | xargs)
    util=$(echo "$util" | xargs)
    if [[ "$uuid" == "$production_gpu" ]] && [[ "$util" -gt 10 ]]; then
      warn "Production GPU $uuid is active ($util% util) — cannot use for benchmark"
    elif [[ "$util" -lt 10 ]]; then
      GPU_UUID="$uuid"
      info "Selected idle GPU: $uuid (util=$util%)"
      break
    fi
  done <<< "$all_gpus"

  if [[ -z "$GPU_UUID" ]]; then
    err "No suitably idle GPU found. GPUs:"
    echo "$all_gpus" >&2
    err "Override with --gpu UUID to force a specific GPU."
    exit 1
  fi
}

# ── Calculate required storage ─────────────────────────────────────────────
check_storage() {
  local needed_gb=0
  for f in "${CANDIDATE_FILES[@]}"; do
    if [[ ! -f "$HOST_MODELS/$f" ]]; then
      needed_gb=$(echo "$needed_gb + ${MODEL_SIZES[$f]:-4.0}" | bc)
    fi
  done
  # Add 2 GB headroom
  needed_gb=$(echo "$needed_gb + 2" | bc)
  local available_gb
  available_gb=$(df -BG "$HOST_MODELS" | awk 'NR==2 {print $4}' | sed 's/G//')
  info "Storage needed for missing models + headroom: ~${needed_gb} GB"
  info "Storage available on $HOST_MODELS: ${available_gb} GB"
  if [[ $(echo "$available_gb < $needed_gb" | bc) -eq 1 ]]; then
    err "Insufficient storage ($available_gb GB avail < $needed_gb GB needed)"
    exit 1
  fi
  ok "Storage sufficient"
}

# ── Start temporary backend container ──────────────────────────────────────
start_backend() {
  local model_file="$1" label="$2"

  info "Starting temporary backend for $label ($model_file)..."

  docker rm -f "$BENCH_CONTAINER" 2>/dev/null || true

  docker run -d --name "$BENCH_CONTAINER" \
    --gpus "\"device=$GPU_UUID\"" \
    --network sorilonet \
    -p "$BENCH_PORT:3030" \
    -v "$HOST_MODELS:/models:ro" \
    -e "S2_MODEL=/models/$model_file" \
    -e "S2_GPU_LAYERS=-1" \
    -e "S2_CODEC_CPU=false" \
    -e "S2_THREADS=0" \
    "$BACKEND_IMAGE" \
    > /dev/null

  info "Waiting for backend readiness (bounded ${TIMEOUT}s)..."
  local elapsed=0
  while [[ $elapsed -lt $TIMEOUT ]]; do
    # Check startup log for expected model
    local startup_log
    startup_log=$(docker logs "$BENCH_CONTAINER" 2>/dev/null || true)
    if echo "$startup_log" | grep -q "Launching: s2 --model /models/$model_file"; then
      ok "Model confirmed in startup log: /models/$model_file"
      break
    fi
    # Also check for errors
    if echo "$startup_log" | grep -qi "ERROR"; then
      err "Backend startup error detected:"
      echo "$startup_log" | grep -i "ERROR" >&2
      docker rm -f "$BENCH_CONTAINER" 2>/dev/null || true
      return 1
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done

  if [[ $elapsed -ge $TIMEOUT ]]; then
    err "Backend readiness timeout after ${TIMEOUT}s"
    docker logs "$BENCH_CONTAINER" 2>/dev/null | tail -20 >&2
    docker rm -f "$BENCH_CONTAINER" 2>/dev/null || true
    return 1
  fi

  # Verify HTTP endpoint
  local http_code
  http_code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 \
    "http://127.0.0.1:$BENCH_PORT/" 2>/dev/null || echo "000")
  if [[ "$http_code" == "000" ]]; then
    err "Backend HTTP endpoint not reachable"
    docker rm -f "$BENCH_CONTAINER" 2>/dev/null || true
    return 1
  fi
  ok "Backend HTTP reachable (HTTP $http_code)"
  return 0
}

# ── Stop and remove temporary container ────────────────────────────────────
stop_backend() {
  docker rm -f "$BENCH_CONTAINER" 2>/dev/null || true
  ok "Temporary backend removed"
}

# ── Capture backend metrics from container logs ────────────────────────────
capture_backend_metrics() {
  local label="$1" output_file="$2"
  docker logs "$BENCH_CONTAINER" 2>/dev/null > "$output_file" || true
  grep '\[Metrics\] Streaming' "$output_file" 2>/dev/null > "${output_file}.metrics" || true
  if [[ -s "${output_file}.metrics" ]]; then
    ok "Backend metrics captured for $label"
  else
    warn "No [Metrics] Streaming line found for $label"
  fi
}

# ── Capture container state ────────────────────────────────────────────────
capture_container_state() {
  local dir="$1" label="$2" model_file="$3"
  docker inspect "$BENCH_CONTAINER" > "$dir/container_inspect.json" 2>/dev/null || true
  docker logs "$BENCH_CONTAINER" > "$dir/startup.log" 2>/dev/null || true
  docker inspect "$BENCH_CONTAINER" --format '{{.Config.Image}}' > "$dir/backend_image.txt" 2>/dev/null || true
  docker inspect "$BENCH_CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' > "$dir/backend_env.txt" 2>/dev/null || true
  echo "$model_file" > "$dir/model_filename.txt"
  # Record SHA-256 and size from host
  if [[ -f "$HOST_MODELS/$model_file" ]]; then
    sha256sum "$HOST_MODELS/$model_file" | awk '{print $1}' > "$dir/model_sha256.txt"
    stat -c%s "$HOST_MODELS/$model_file" > "$dir/model_size.txt"
  fi
}

# ── GPU telemetry sampler (background) ─────────────────────────────────────
GPU_TELEM_PID=""
start_gpu_telemetry() {
  local file="$1"
  echo "timestamp,gpu_uuid,util_pct,mem_used_mib,mem_total_mib,temp_c,power_w,sm_mhz,mem_mhz" > "$file"
  (
    while kill -0 $$ 2>/dev/null; do
      nvidia-smi --query-gpu=uuid,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,clocks.sm,clocks.mem \
        --format=csv,noheader,nounits 2>/dev/null | while IFS=, read -r uuid util mem_used mem_total temp power sm mem; do
        echo "$(date -u +%Y-%m-%dT%H:%M:%S),$uuid,$util,$mem_used,$mem_total,$temp,$power,$sm,$mem" >> "$file"
      done
      sleep 1
    done
  ) &
  GPU_TELEM_PID=$!
}

stop_gpu_telemetry() {
  if [[ -n "${GPU_TELEM_PID:-}" ]] && kill -0 "$GPU_TELEM_PID" 2>/dev/null; then
    kill "$GPU_TELEM_PID" 2>/dev/null || true
    wait "$GPU_TELEM_PID" 2>/dev/null || true
  fi
}

# ── Cleanup trap ───────────────────────────────────────────────────────────
cleanup() {
  stop_gpu_telemetry
  docker rm -f "$BENCH_CONTAINER" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

check_prereqs
discover_model_mount

# Dry-run mode
if [[ "$RUN_REAL" != true ]]; then
  echo "========================================"
  echo " DRY RUN — no containers will be created"
  echo "========================================"
  echo ""
  echo "Would benchmark on GPU: (auto-detect idle)"
  echo "Backend image: $BACKEND_IMAGE"
  echo "Host models dir: $HOST_MODELS"
  echo "Stride: $STRIDE"
  echo ""
  echo "Candidate models:"
  for i in "${!CANDIDATE_FILES[@]}"; do
    f="${CANDIDATE_FILES[$i]}"
    label="${CANDIDATE_LABELS[$i]}"
    if [[ -f "$HOST_MODELS/$f" ]]; then
      echo "  ✓ $label ($f) — EXISTS"
    else
      echo "  ✗ $label ($f) — MISSING (~${MODEL_SIZES[$f]:-?} GB needed)"
    fi
  done
  echo ""
  echo "Per-candidate sequence:"
  echo "  1. docker run ... --gpus device=IDLE_UUID -e S2_MODEL=/models/<file>"
  echo "  2. Wait for 'Launching: s2 --model /models/<file>' in logs"
  echo "  3. Verify HTTP endpoint reachable"
  echo "  4. Run benchmark_quantization.py --run-real --models <host_path>"
  echo "  5. Capture container inspect, startup logs, backend metrics"
  echo "  6. docker rm -f s2cpp-backend-bench"
  echo ""
  echo "Add --run-real to execute."
  exit 0
fi

# ── Live execution ─────────────────────────────────────────────────────────
discover_idle_gpu
check_storage

TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)
ARTIFACT_DIR="$ARTIFACT_BASE/$TIMESTAMP"
mkdir -p "$ARTIFACT_DIR"
info "Artifact directory: $ARTIFACT_DIR"

# System state
{
  echo "timestamp=$TIMESTAMP"
  echo "bench_container=$BENCH_CONTAINER"
  echo "bench_port=$BENCH_PORT"
  echo "backend_image=$BACKEND_IMAGE"
  echo "gpu_uuid=$GPU_UUID"
  echo "stride=$STRIDE"
  echo "codec_context=$CODEC_CONTEXT"
  echo "host_models=$HOST_MODELS"
  echo "git_commit=$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "git_branch=$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  docker inspect s2cpp-backend --format '{{.Config.Image}}' 2>/dev/null || echo "unknown"
} > "$ARTIFACT_DIR/system_state.txt"

# Copy listening checklist from docs if available, else create
if [[ -f "$REPO_ROOT/docs/STREAMING_STRIDE_AND_QUANT_BENCHMARKS.md" ]]; then
  grep -A20 'Listening Checklist' "$REPO_ROOT/docs/STREAMING_STRIDE_AND_QUANT_BENCHMARKS.md" \
    > "$ARTIFACT_DIR/listening_checklist.md" 2>/dev/null || true
fi

# Combined results accumulator
ALL_RESULTS="[]"

for i in "${!CANDIDATE_FILES[@]}"; do
  MODEL_FILE="${CANDIDATE_FILES[$i]}"
  LABEL="${CANDIDATE_LABELS[$i]}"

  echo ""
  echo "========================================"
  echo " Candidate: $LABEL ($MODEL_FILE)"
  echo "========================================"

  if [[ ! -f "$HOST_MODELS/$MODEL_FILE" ]]; then
    warn "Model file missing: $HOST_MODELS/$MODEL_FILE — skipping"
    continue
  fi

  CANDIDATE_DIR="$ARTIFACT_DIR/$LABEL"
  mkdir -p "$CANDIDATE_DIR"

  # Start GPU telemetry for this candidate
  start_gpu_telemetry "$CANDIDATE_DIR/gpu_telemetry.csv"

  # Start backend with this specific model
  if ! start_backend "$MODEL_FILE" "$LABEL"; then
    err "Failed to start backend for $LABEL"
    stop_gpu_telemetry
    continue
  fi

  # Capture container state
  capture_container_state "$CANDIDATE_DIR" "$LABEL" "$MODEL_FILE"

  # Run benchmark (single model, live mode)
  info "Running benchmark for $LABEL..."
  PYTHONPATH="$REPO_ROOT" python3 "$REPO_ROOT/scripts/benchmark_quantization.py" \
    --run-real \
    --endpoint "127.0.0.1:$BENCH_PORT" \
    --models "$HOST_MODELS/$MODEL_FILE" \
    --stride "$STRIDE" \
    --codec-context "$CODEC_CONTEXT" \
    --warmup-runs "$WARMUP_RUNS" \
    --measured-runs "$MEASURED_RUNS" \
    --output-dir "$ARTIFACT_DIR" \
    --candidate-dir "$LABEL" \
    --text "$BENCHMARK_TEXT" \
    --timeout "$TIMEOUT" \
    || warn "Benchmark for $LABEL exited non-zero"

  # Capture backend metrics
  capture_backend_metrics "$LABEL" "$CANDIDATE_DIR/backend_metrics.log"

  # Stop telemetry
  stop_gpu_telemetry

  # Stop backend
  stop_backend

  # Collect per-candidate result
  if [[ -f "$CANDIDATE_DIR/results.json" ]]; then
    CANDIDATE_JSON=$(python3 -c "
import json
with open('$CANDIDATE_DIR/results.json') as f:
    data = json.load(f)
# Wrap single-model result
data['candidate_label'] = '$LABEL'
data['model_filename'] = '$MODEL_FILE'
print(json.dumps(data))
" 2>/dev/null || echo '{}')
    ALL_RESULTS=$(python3 -c "
import json
arr = json.loads('$ALL_RESULTS')
arr.append(json.loads('''$CANDIDATE_JSON'''))
print(json.dumps(arr, indent=2))
" 2>/dev/null || echo "$ALL_RESULTS")
  fi

  info "Candidate $LABEL complete"
done

# ── Write combined results ─────────────────────────────────────────────────
echo "$ALL_RESULTS" | python3 -m json.tool > "$ARTIFACT_DIR/combined_results.json" 2>/dev/null || true

# Combined summary
cat > "$ARTIFACT_DIR/summary.md" << 'SUMMARYEOF'
# Quantization Benchmark Results

**Status: Provisional — requires human listening before selection.**

## Test Configuration
SUMMARYEOF

cat "$ARTIFACT_DIR/system_state.txt" >> "$ARTIFACT_DIR/summary.md"

cat >> "$ARTIFACT_DIR/summary.md" << 'SUMMARYEOF'

## Per-Candidate Artifacts
SUMMARYEOF

for label in "${CANDIDATE_LABELS[@]}"; do
  if [[ -d "$ARTIFACT_DIR/$label" ]]; then
    echo "- **$label**: \`$ARTIFACT_DIR/$label/\`" >> "$ARTIFACT_DIR/summary.md"
    if [[ -f "$ARTIFACT_DIR/$label/model_sha256.txt" ]]; then
      echo "  - SHA-256: \`$(cat "$ARTIFACT_DIR/$label/model_sha256.txt")\`" >> "$ARTIFACT_DIR/summary.md"
    fi
  fi
done

cat >> "$ARTIFACT_DIR/summary.md" << 'SUMMARYEOF'

## Next Steps

1. Convert PCM to WAV for listening (use ffmpeg in Hermes-Suite):
   ```
   docker exec Hermes-Suite ffmpeg -f s16le -ar 44100 -ac 1 \
     -i /workspace/wyoming-s2cpp-tts/<artifact_dir>/q6_k/quant_q6_k_run1.pcm \
     /workspace/wyoming-s2cpp-tts/<artifact_dir>/q6_k/quant_q6_k_run1.wav
   ```
2. Evaluate audio quality using the listening checklist.
3. DO NOT promote a model without human listening.
SUMMARYEOF

# Model SHA-256 manifest
for f in "${CANDIDATE_FILES[@]}"; do
  if [[ -f "$HOST_MODELS/$f" ]]; then
    sha256sum "$HOST_MODELS/$f" >> "$ARTIFACT_DIR/model_sha256.txt"
  fi
done

# Cleanup status
echo "cleanup_status=ok" > "$ARTIFACT_DIR/cleanup_status.txt"
echo "containers_removed=true" >> "$ARTIFACT_DIR/cleanup_status.txt"

echo ""
info "========================================"
info " BENCHMARK COMPLETE"
info "========================================"
info "Artifact directory: $ARTIFACT_DIR"
info ""
info "Contents:"
ls -la "$ARTIFACT_DIR/"
for label in "${CANDIDATE_LABELS[@]}"; do
  if [[ -d "$ARTIFACT_DIR/$label" ]]; then
    info "  $label/: $(ls "$ARTIFACT_DIR/$label/" 2>/dev/null | wc -l) files"
  fi
done
info ""
info "⚠️  IMPORTANT: Human listening of WAV files is REQUIRED before model selection."
info "   Convert PCM → WAV using Hermes-Suite ffmpeg (see summary.md)."
info "   DO NOT change the production model without listening."
info ""

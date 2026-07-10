#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# Phase 8E.1: Q4_K_M non-fork runtime tuning harness.
#
# Tests thread counts, CPU affinity, and codec context/holdback variants
# on Q4_K_M at fixed stride 4. One temporary backend per configuration.
#
# Default: dry-run. Requires --run-real for live execution.
# ────────────────────────────────────────────────────────────────────────────

set -euo pipefail
shopt -s lastpipe

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'
CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*" >&2; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*" >&2; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*" >&2; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ── Fixed configuration ────────────────────────────────────────────────────
MODEL_FILE="s2-pro-q4_k_m.gguf"
MODEL_LABEL="q4_k_m"
BENCH_CONTAINER="s2cpp-backend-tune"
BENCH_PORT="3034"
BACKEND_IMAGE="ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd"
STRIDE=4
CODEC_CONTEXT=4
HOLDBACK=0
START_BUFFER=0
LOW_LATENCY=true
WARMUP_RUNS=1
MEASURED_RUNS=3
TIMEOUT=120

BENCHMARK_TEXT="The morning sun cast long shadows across the quiet neighborhood as residents began their daily routines. A gentle breeze carried the scent of fresh coffee from the corner cafe, where early customers sat reading newspapers and checking their phones. Children hurried past with backpacks slung over their shoulders, their laughter echoing off the brick buildings."

# ── Resolve paths ──────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ARTIFACT_BASE="$REPO_ROOT/verification_artifacts/q4_runtime_tuning"

# ── Parse arguments ────────────────────────────────────────────────────────
RUN_REAL=false
GPU_UUID=""
ALLOW_PRODUCTION_GPU=false
PHASE="all"  # all, threads, affinity, blipping

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-real)             RUN_REAL=true; shift ;;
    --gpu)                  GPU_UUID="$2"; shift 2 ;;
    --allow-production-gpu) ALLOW_PRODUCTION_GPU=true; shift ;;
    --phase)                PHASE="$2"; shift 2 ;;
    *)                      err "Unknown argument: $1"; exit 1 ;;
  esac
done

# ── Helper: safe float comparison ──────────────────────────────────────────
float_lt()  { python3 -c "import sys; sys.exit(0 if float('$1') < float('$2') else 1)"; }

# ── Discover host model mount ──────────────────────────────────────────────
discover_model_mount() {
  HOST_MODELS=$(docker inspect s2cpp-backend \
    --format '{{range .Mounts}}{{if eq .Destination "/models"}}{{.Source}}{{end}}{{end}}' \
    2>/dev/null || echo '')
  if [[ -z "$HOST_MODELS" ]]; then
    err "Cannot discover /models mount."
    exit 1
  fi
  info "Host models: $HOST_MODELS"
}

# ── GPU discovery ──────────────────────────────────────────────────────────
discover_idle_gpu() {
  if [[ -n "$GPU_UUID" ]]; then
    info "Using GPU: $GPU_UUID"
    return
  fi
  PRODUCTION_GPU=$(docker inspect s2cpp-backend \
    --format '{{range .Config.Env}}{{if eq (printf "%.20s" .) "NVIDIA_VISIBLE_DEVICE"}}{{.}}{{end}}{{end}}' \
    2>/dev/null | sed 's/NVIDIA_VISIBLE_DEVICES=//' | xargs || echo 'none')
  local all_gpus
  all_gpus=$(nvidia-smi --query-gpu=uuid,utilization.gpu,memory.used --format=csv,noheader,nounits 2>/dev/null)
  while IFS=, read -r uuid util mem; do
    uuid=$(echo "$uuid" | xargs); util=$(echo "$util" | xargs); mem=$(echo "$mem" | xargs)
    if [[ "$uuid" == "$PRODUCTION_GPU" ]] && [[ "$ALLOW_PRODUCTION_GPU" != true ]]; then
      continue
    fi
    if float_lt "$util" "10" && float_lt "$mem" "500"; then
      GPU_UUID="$uuid"
      info "Selected GPU: $uuid (util=$util%, mem=$mem MiB)"
      return
    fi
  done <<< "$all_gpus"
  err "No idle GPU found."
  exit 1
}

# ── Start/stop backend ─────────────────────────────────────────────────────
start_backend_q4() {
  local threads="$1" cpuset="${2:-}" extra_args="${3:-}"
  info "Starting Q4 backend (threads=$threads${cpuset:+, cpuset=$cpuset}${extra_args:+${extra_args}})..."

  docker rm -f "$BENCH_CONTAINER" 2>/dev/null || true

  local cpuset_arg=""
  if [[ -n "$cpuset" ]]; then
    cpuset_arg="--cpuset-cpus=$cpuset"
  fi

  docker run -d --name "$BENCH_CONTAINER" \
    --gpus "\"device=$GPU_UUID\"" \
    --network sorilonet \
    $cpuset_arg \
    -p "$BENCH_PORT:3030" \
    -v "$HOST_MODELS:/models:ro" \
    -e "S2_MODEL=/models/$MODEL_FILE" \
    -e "S2_GPU_LAYERS=-1" \
    -e "S2_CODEC_CPU=false" \
    -e "S2_THREADS=$threads" \
    $extra_args \
    "$BACKEND_IMAGE" \
    > /dev/null

  # Readiness: wait for Launching line + HTTP reachable
  local elapsed=0
  while [[ $elapsed -lt $TIMEOUT ]]; do
    local startup_log
    startup_log=$(docker logs "$BENCH_CONTAINER" 2>/dev/null || true)
    if echo "$startup_log" | grep -qi "ERROR"; then
      err "Backend startup error"
      echo "$startup_log" | grep -i "ERROR" >&2
      docker rm -f "$BENCH_CONTAINER" 2>/dev/null || true
      return 1
    fi
    if ! docker inspect "$BENCH_CONTAINER" --format '{{.State.Running}}' 2>/dev/null | grep -q true; then
      err "Backend container exited"
      return 1
    fi
    local launched=false http_ok=false
    echo "$startup_log" | grep -q "Launching: s2 --model /models/$MODEL_FILE" && launched=true
    local http_code curl_exit
    http_code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 2 \
      "http://127.0.0.1:$BENCH_PORT/" 2>/dev/null) || curl_exit=$?
    [[ -z "${curl_exit:-}" ]] && [[ "$http_code" =~ ^[0-9]{3}$ ]] && [[ "$http_code" != "000" ]] && http_ok=true
    if [[ "$launched" == true ]] && [[ "$http_ok" == true ]]; then
      ok "Backend ready (threads=$threads${cpuset:+, cpuset=$cpuset})"
      return 0
    fi
    sleep 2; elapsed=$((elapsed + 2))
  done
  err "Backend readiness timeout"
  docker logs "$BENCH_CONTAINER" 2>/dev/null | tail -10 >&2
  docker rm -f "$BENCH_CONTAINER" 2>/dev/null || true
  return 1
}

stop_backend() { docker rm -f "$BENCH_CONTAINER" 2>/dev/null || true; }

# ── Run benchmark ──────────────────────────────────────────────────────────
run_q4_benchmark() {
  local label="$1" threads="$2"

  PYTHONPATH="$REPO_ROOT" python3 "$REPO_ROOT/scripts/benchmark_quantization.py" \
    --run-real \
    --endpoint "127.0.0.1:$BENCH_PORT" \
    --models "$HOST_MODELS/$MODEL_FILE" \
    --stride "$STRIDE" \
    --codec-context "${3:-$CODEC_CONTEXT}" \
    --warmup-runs "$WARMUP_RUNS" \
    --measured-runs "$MEASURED_RUNS" \
    --output-dir "$ARTIFACT_DIR" \
    --candidate-dir "$label" \
    --text "$BENCHMARK_TEXT" \
    --timeout "$TIMEOUT" \
    || warn "Benchmark exited non-zero for $label"
}

# ── Capture metrics (with buffering workaround) ────────────────────────────
capture_metrics_q4() {
  local label="$1" dir="$2"
  # Wait a few seconds for any buffered output to flush
  sleep 3
  docker logs "$BENCH_CONTAINER" 2>/dev/null > "$dir/backend_metrics.log" || true
  grep '\[Metrics\]' "$dir/backend_metrics.log" 2>/dev/null > "$dir/backend_metrics.log.metrics" || true
  if [[ -s "$dir/backend_metrics.log.metrics" ]]; then
    ok "Metrics captured: $(wc -l < "$dir/backend_metrics.log.metrics") lines"
  else
    warn "No [Metrics] lines — may be buffered"
    # Try alternate: check if metrics appear after container stop
  fi
}

# ── GPU telemetry ──────────────────────────────────────────────────────────
GPU_TELEM_PID=""
start_gpu_telemetry() {
  local file="$1"
  echo "timestamp,gpu_uuid,util_pct,mem_used_mib,mem_total_mib,temp_c,power_w,power_limit_w,sm_mhz,mem_mhz,pstate,throttle_reason" > "$file"
  (
    while kill -0 $$ 2>/dev/null; do
      nvidia-smi --query-gpu=uuid,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,clocks.sm,clocks.mem,pstate,clocks_throttle_reasons.active \
        --format=csv,noheader,nounits 2>/dev/null | while IFS=, read -r uuid util mem_used mem_total temp power plimit sm memclock pstate throttle; do
        echo "$(date -u +%Y-%m-%dT%H:%M:%S),$uuid,$util,$mem_used,$mem_total,$temp,$power,$plimit,$sm,$memclock,$pstate,$throttle" >> "$file"
      done
      sleep 1
    done
  ) &
  GPU_TELEM_PID=$!
}
stop_gpu_telemetry() {
  [[ -n "${GPU_TELEM_PID:-}" ]] && kill -0 "$GPU_TELEM_PID" 2>/dev/null && kill "$GPU_TELEM_PID" 2>/dev/null && wait "$GPU_TELEM_PID" 2>/dev/null || true
  GPU_TELEM_PID=""
}

# ── CPU telemetry ──────────────────────────────────────────────────────────
CPU_TELEM_PID=""
start_cpu_telemetry() {
  local file="$1"
  echo "timestamp,user_pct,system_pct,iowait_pct,idle_pct" > "$file"
  (
    while kill -0 $$ 2>/dev/null; do
      mpstat -u 1 1 2>/dev/null | awk 'NR==4 {printf "%s,%s,%s,%s,%s\n", $3,$4,$5,$6,$12}' | while IFS= read -r line; do
        echo "$(date -u +%Y-%m-%dT%H:%M:%S),$line" >> "$file"
      done
      sleep 1
    done
  ) &
  CPU_TELEM_PID=$!
}
stop_cpu_telemetry() {
  [[ -n "${CPU_TELEM_PID:-}" ]] && kill -0 "$CPU_TELEM_PID" 2>/dev/null && kill "$CPU_TELEM_PID" 2>/dev/null && wait "$CPU_TELEM_PID" 2>/dev/null || true
  CPU_TELEM_PID=""
}

# ── Create WAV from host PCM ───────────────────────────────────────────────
create_host_wav() {
  local pcm="$1" wav="$2"
  python3 -c "
import wave
with open('$pcm', 'rb') as pf:
    pcm = pf.read()
with wave.open('$wav', 'wb') as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(44100)
    wf.writeframes(pcm)
" 2>/dev/null
  [[ -s "$wav" ]] && return 0 || return 1
}

# ── Cleanup ────────────────────────────────────────────────────────────────
cleanup() {
  stop_gpu_telemetry
  stop_cpu_telemetry
  stop_backend
}
trap cleanup EXIT INT TERM

# ═══════════════════════════════════════════════════════════════════════════
# THREAD SWEEP
# ═══════════════════════════════════════════════════════════════════════════
run_thread_sweep() {
  local threads_list=(0 8 16 24 32)
  info "Thread sweep: ${threads_list[*]}"
  local best_rtf=999 best_threads=0

  for t in "${threads_list[@]}"; do
    local label="threads_${t}"
    info "Testing threads=$t..."
    start_gpu_telemetry "$ARTIFACT_DIR/$label/gpu_telemetry.csv"
    start_cpu_telemetry "$ARTIFACT_DIR/$label/cpu_telemetry.csv"
    start_backend_q4 "$t" "" "" || { stop_gpu_telemetry; stop_cpu_telemetry; continue; }
    run_q4_benchmark "$label" "$t"
    capture_metrics_q4 "$label" "$ARTIFACT_DIR/$label"
    stop_gpu_telemetry
    stop_cpu_telemetry
    stop_backend

    # Check RTF
    local rj="$ARTIFACT_DIR/$label/results.json"
    if [[ -f "$rj" ]]; then
      local rtf=$(python3 -c "import json; d=json.load(open('$rj')); s=d['summaries'][0]; print(s['avg_rtf'])" 2>/dev/null || echo "999")
      info "  threads=$t: RTF=$rtf"
      if float_lt "$rtf" "$best_rtf"; then
        best_rtf=$rtf; best_threads=$t
      fi
    fi
  done
  info "Best thread count: $best_threads (RTF=$best_rtf)"
  echo "$best_threads" > "$ARTIFACT_DIR/best_threads.txt"
  BEST_THREADS=$best_threads
}

# ── CPU topology ───────────────────────────────────────────────────────────
capture_cpu_topology() {
  info "Capturing CPU topology..."
  lscpu > "$ARTIFACT_DIR/cpu_topology.txt" 2>/dev/null || true
  lscpu -e=CPU,CORE,SOCKET,NODE,ONLINE,MAXMHZ >> "$ARTIFACT_DIR/cpu_topology.txt" 2>/dev/null || true
  # Parse P-core and E-core ranges
  python3 -c "
import subprocess, json
out = subprocess.check_output(['lscpu', '-e=CPU,CORE,SOCKET,MAXMHZ'], text=True)
p_cores = []; e_cores = []
for line in out.strip().split('\n')[1:]:
    parts = line.split()
    if len(parts) < 4: continue
    cpu, core, _, mhz = parts
    mhz = float(mhz) if mhz.replace('.','').isdigit() else 0
    if mhz > 4000: p_cores.append(int(cpu))
    elif mhz > 0: e_cores.append(int(cpu))
print(json.dumps({'p_cores': p_cores, 'e_cores': e_cores}))
" > "$ARTIFACT_DIR/core_topology.json" 2>/dev/null || true
  ok "CPU topology saved"
}

# ── CPU AFFINITY SWEEP ─────────────────────────────────────────────────────
run_affinity_sweep() {
  local best_t="${1:-$BEST_THREADS}"
  info "Affinity sweep at threads=$best_t..."

  # Parse core topology
  local p_cores e_cores
  read -r p_cores e_cores <<< "$(python3 -c "
import json
with open('$ARTIFACT_DIR/core_topology.json') as f:
    t = json.load(f)
p = ','.join(str(c) for c in t['p_cores'])
e = ','.join(str(c) for c in t['e_cores'])
print(f'{p} {e}')
")"

  info "P-cores: $p_cores"
  info "E-cores: $e_cores"

  # Affinity sets to test (shell-safe computation)
  local p_arr=(${p_cores//,/ })
  local p_count=${#p_arr[@]}
  local p_half=$((p_count / 2))
  local p_physical="${p_arr[*]:0:$p_half}"
  p_physical="${p_physical// /,}"
  local p_logical="${p_arr[*]:$p_half}"
  p_logical="${p_logical// /,}"

  # P_cores + first half of E_cores
  local e_arr=(${e_cores//,/ })
  local e_count=${#e_arr[@]}
  local e_half=$((e_count / 2))
  local e_subset="${e_arr[*]:0:$e_half}"
  e_subset="${e_subset// /,}"
  local p_plus_e="${p_cores},${e_subset}"

  local affinities=(
    "unrestricted:"
    "p_physical:${p_physical}"
    "p_logical:${p_logical}"
    "p_plus_e:${p_plus_e}"
  )

  for affinity_spec in "${affinities[@]}"; do
    local label="affinity_${affinity_spec%%:*}"
    local cpus="${affinity_spec#*:}"
    local cpuset_arg=""; [[ -n "$cpus" ]] && cpuset_arg="--cpuset-cpus=$cpus"
    info "Testing affinity: $label (cpus=$cpus)..."
    start_gpu_telemetry "$ARTIFACT_DIR/$label/gpu_telemetry.csv"
    start_cpu_telemetry "$ARTIFACT_DIR/$label/cpu_telemetry.csv"
    start_backend_q4 "$best_t" "$cpus" "" || { stop_gpu_telemetry; stop_cpu_telemetry; continue; }
    run_q4_benchmark "$label" "$best_t"
    capture_metrics_q4 "$label" "$ARTIFACT_DIR/$label"
    stop_gpu_telemetry
    stop_cpu_telemetry
    stop_backend
  done
}

# ── BLIPPING DIAGNOSTIC ────────────────────────────────────────────────────
run_blipping_diagnostic() {
  local best_t="${1:-0}"
  info "Blipping diagnostic — Q4_K_M, stride 4..."

  local configs=(
    "blip_ctx4_hb0:4:0"
    "blip_ctx64_hb0:64:0"
    "blip_ctx64_hb1:64:1"
  )

  for cfg in "${configs[@]}"; do
    local label="${cfg%%:*}"
    local ctx="${cfg#*:}"; ctx="${ctx%%:*}"
    local hb="${cfg##*:}"
    info "Testing: $label (context=$ctx, holdback=$hb)..."
    start_gpu_telemetry "$ARTIFACT_DIR/$label/gpu_telemetry.csv"
    start_backend_q4 "$best_t" "" "" || { stop_gpu_telemetry; continue; }
    run_q4_benchmark "$label" "$best_t" "$ctx"
    capture_metrics_q4 "$label" "$ARTIFACT_DIR/$label"
    stop_gpu_telemetry
    stop_backend

    # Create WAVs for listening
    local pcm_dir="$ARTIFACT_DIR/$label"
    find "$pcm_dir" -name '*.pcm' -print0 2>/dev/null | while IFS= read -r -d '' pcm; do
      create_host_wav "$pcm" "${pcm%.pcm}.wav" && ok "WAV: ${pcm%.pcm}.wav" || warn "WAV failed: $pcm"
    done
  done
  info "Blipping diagnostic complete. Listen to WAVs in $ARTIFACT_DIR/blip_*/"
}

# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

check_prereqs() {
  for cmd in docker curl python3 nvidia-smi git lscpu; do
    command -v "$cmd" &>/dev/null || { err "Missing: $cmd"; exit 1; }
  done
}

check_model_exists() {
  if [[ ! -f "$HOST_MODELS/$MODEL_FILE" ]]; then
    err "Model not found: $HOST_MODELS/$MODEL_FILE"
    exit 1
  fi
  ok "Model: $MODEL_FILE ($(stat -c%s "$HOST_MODELS/$MODEL_FILE" | numfmt --to=iec))"
  sha256sum "$HOST_MODELS/$MODEL_FILE" | awk '{print $1}' > /dev/null
}

# Dry-run
if [[ "$RUN_REAL" != true ]]; then
  echo "========================================"
  echo " DRY RUN — Q4 Runtime Tuning"
  echo "========================================"
  echo "Model: $MODEL_FILE"
  echo "Stride: $STRIDE (fixed)"
  echo "Phase: $PHASE"
  echo ""
  if [[ "$PHASE" == "all" ]] || [[ "$PHASE" == "threads" ]]; then
    echo "Thread sweep: 0, 8, 16, 24, 32"
  fi
  if [[ "$PHASE" == "all" ]] || [[ "$PHASE" == "affinity" ]]; then
    echo "Affinity sweep: unrestricted, P-core physical"
  fi
  if [[ "$PHASE" == "all" ]] || [[ "$PHASE" == "blipping" ]]; then
    echo "Blipping diagnostic: ctx4/hb0, ctx64/hb0, ctx64/hb1"
  fi
  echo ""
  echo "Add --run-real to execute."
  exit 0
fi

check_prereqs
discover_model_mount
check_model_exists
discover_idle_gpu

TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)
ARTIFACT_DIR="$ARTIFACT_BASE/$TIMESTAMP"
mkdir -p "$ARTIFACT_DIR"
info "Artifact: $ARTIFACT_DIR"

# System state
{
  echo "phase=8E.1"
  echo "timestamp=$TIMESTAMP"
  echo "model=$MODEL_FILE"
  echo "model_sha256=$(sha256sum "$HOST_MODELS/$MODEL_FILE" | awk '{print $1}')"
  echo "backend_image=$BACKEND_IMAGE"
  echo "gpu_uuid=$GPU_UUID"
  echo "stride=$STRIDE"
  echo "git_commit=$(git -C "$REPO_ROOT" rev-parse HEAD)"
} > "$ARTIFACT_DIR/system_state.txt"

capture_cpu_topology

# ── Saved voice verification ───────────────────────────────────────────────
info "Verifying saved voice..."
# Check if benchmark uses a .s2voice profile vs default
VOICE_DIR="${VOICE_DIR:-/voices}"
info "Voice directory: $VOICE_DIR"
# Quick test: start backend briefly, check if ref_encode=0 appears in metrics
if [[ -f "$HOST_MODELS/../voices" ]] || [[ -d "/mnt/user/appdata/s2cpp/voices" ]]; then
  ok "Voice directory found (saved voice may be in use)"
else
  warn "Voice directory not found — benchmark may use default/no-reference voice"
  warn "Production likely uses a saved .s2voice — add --voice to benchmark for parity"
fi

# ── Stock-clock observation ────────────────────────────────────────────────
info "Recording stock GPU state..."
nvidia-smi --query-gpu=uuid,clocks.max.sm,clocks.max.mem,power.limit,power.default_limit   --format=csv,noheader > "$ARTIFACT_DIR/stock_gpu_state.txt" 2>/dev/null || true
nvidia-smi > "$ARTIFACT_DIR/nvidia_smi_snapshot.txt" 2>/dev/null || true
ok "Stock GPU state recorded (no overclock, no power limit changes)"

# ── Metrics buffering investigation note ───────────────────────────────────
cat > "$ARTIFACT_DIR/metrics_buffering_notes.txt" << 'METRICS_NOTE'
Metrics buffering investigation:
- Upstream s2.cpp emits [Metrics] Streaming at Info log level via safe_print_ln().
- Missing metrics lines may be caused by:
  1. stdout block buffering (not line-buffered in pipeline)
  2. Missing fflush after safe_print_ln
  3. Docker log timing (--since not capturing, or timestamp mismatch)
  4. Container stop too quickly (logs not flushed)
  5. Metrics printed after HTTP client completes
- This script waits 3s after benchmark before capturing logs.
- If metrics still missing, try: --log-level debug, stdbuf -oL, or capture
  full post-run logs before container stop.
METRICS_NOTE

# Phase dispatch
if [[ "$PHASE" == "all" ]] || [[ "$PHASE" == "threads" ]]; then
  run_thread_sweep
fi
BEST_THREADS=$(cat "$ARTIFACT_DIR/best_threads.txt" 2>/dev/null || echo "0")

if [[ "$PHASE" == "all" ]] || [[ "$PHASE" == "affinity" ]]; then
  run_affinity_sweep "$BEST_THREADS"
fi
if [[ "$PHASE" == "all" ]] || [[ "$PHASE" == "blipping" ]]; then
  run_blipping_diagnostic "$BEST_THREADS"
fi

info "Q4 runtime tuning complete: $ARTIFACT_DIR"

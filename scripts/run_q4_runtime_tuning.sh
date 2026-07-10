#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# Phase 8E.1a: Q4_K_M non-fork runtime tuning harness (corrected).
#
# Thread-count sweep, CPU-affinity sweep, blipping diagnostic.
# Q4-only. Stride 4 fixed. One temporary backend per configuration.
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

# ── State tracking ─────────────────────────────────────────────────────────
ATTEMPTED=0; SUCCESSFUL=0; FAILED=0; MISSING_RESULTS=0; MISSING_TELEM=0; WAV_FAILURES=0

# ── Parse arguments ────────────────────────────────────────────────────────
RUN_REAL=false; GPU_UUID=""; ALLOW_PRODUCTION_GPU=false
PHASE="all"; USER_THREADS=""; RESUME_ARTIFACT=""; USER_VOICE=""; USER_VOICE_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-real)             RUN_REAL=true; shift ;;
    --gpu)                  GPU_UUID="$2"; shift 2 ;;
    --allow-production-gpu) ALLOW_PRODUCTION_GPU=true; shift ;;
    --phase)                PHASE="$2"; shift 2 ;;
    --threads)              USER_THREADS="$2"; shift 2 ;;
    --resume-artifact)      RESUME_ARTIFACT="$2"; shift 2 ;;
    --voice)                USER_VOICE="$2"; shift 2 ;;
    --voice-dir)            USER_VOICE_DIR="$2"; shift 2 ;;
    *)                      err "Unknown argument: $1"; exit 1 ;;
  esac
done

# Validate phase
case "$PHASE" in
  all|threads|affinity|blipping) ;;
  *) err "Invalid phase: $PHASE (valid: all, threads, affinity, blipping)"; exit 1 ;;
esac

# ── Helpers ────────────────────────────────────────────────────────────────
float_lt() { python3 -c "import sys; sys.exit(0 if float('$1') < float('$2') else 1)"; }
ensure_dir() { mkdir -p "$1"; }

# ── Discover host mounts ───────────────────────────────────────────────────
discover_mounts() {
  HOST_MODELS=$(docker inspect s2cpp-backend \
    --format '{{range .Mounts}}{{if eq .Destination "/models"}}{{.Source}}{{end}}{{end}}' 2>/dev/null || echo '')
  HOST_VOICES=$(docker inspect s2cpp-backend \
    --format '{{range .Mounts}}{{if eq .Destination "/voices"}}{{.Source}}{{end}}{{end}}' 2>/dev/null || echo '')
  [[ -z "$HOST_MODELS" ]] && { err "Cannot discover /models mount"; exit 1; }
  info "Host models: $HOST_MODELS"
  [[ -n "$HOST_VOICES" ]] && info "Host voices: $HOST_VOICES" || warn "No /voices mount found on production backend"
}

# ── Discover production voice ──────────────────────────────────────────────
discover_production_voice() {
  if [[ -n "$USER_VOICE" ]]; then
    info "Using user-supplied voice: $USER_VOICE"
    PRODUCTION_VOICE="$USER_VOICE"
    PRODUCTION_VOICE_DIR="${USER_VOICE_DIR:-/voices}"
    return
  fi
  # Check production wrapper env
  PRODUCTION_VOICE=$(docker inspect wyoming-s2cpp-tts \
    --format '{{range .Config.Env}}{{if eq (printf "%.16s" .) "S2_DEFAULT_VOICE="}}{{.}}{{end}}{{end}}' \
    2>/dev/null | sed 's/S2_DEFAULT_VOICE=//' | xargs || echo '')
  PRODUCTION_VOICE_DIR=$(docker inspect wyoming-s2cpp-tts \
    --format '{{range .Config.Env}}{{if eq (printf "%.13s" .) "S2_VOICE_DIR="}}{{.}}{{end}}{{end}}' \
    2>/dev/null | sed 's/S2_VOICE_DIR=//' | xargs || echo '/voices')
  if [[ -n "$PRODUCTION_VOICE" ]]; then
    info "Production voice: $PRODUCTION_VOICE (dir=$PRODUCTION_VOICE_DIR)"
    # Verify the .s2voice file exists on host
    local voice_file="$HOST_VOICES/${PRODUCTION_VOICE}.s2voice"
    if [[ -f "$voice_file" ]]; then
      ok "Voice profile exists: $voice_file"
    else
      warn "Voice profile not found on host: $voice_file"
      warn "Add --voice <voice_id> --voice-dir <host_dir> to specify manually"
    fi
  else
    warn "Could not discover production voice from wrapper env"
    warn "Benchmark will use default/no-reference voice — not representative"
    warn "Add --voice <voice_id> --voice-dir <host_dir> for production parity"
    PRODUCTION_VOICE=""
    PRODUCTION_VOICE_DIR="/voices"
  fi
}

# ── CPU topology from /sys/devices/system/cpu ──────────────────────────────
build_cpu_topology() {
  local out="$1"
  python3 -c "
import json, os, glob

cores = {}
for cpu_dir in sorted(glob.glob('/sys/devices/system/cpu/cpu[0-9]*')):
    cpu_id = int(os.path.basename(cpu_dir).replace('cpu', ''))
    # Read core_type (preferred, Linux 6.0+)
    ct_file = os.path.join(cpu_dir, 'topology/core_type')
    core_type = open(ct_file).read().strip() if os.path.exists(ct_file) else None
    # Read thread_siblings_list
    ts_file = os.path.join(cpu_dir, 'topology/thread_siblings_list')
    siblings = open(ts_file).read().strip() if os.path.exists(ts_file) else str(cpu_id)
    # Read core_id
    cid_file = os.path.join(cpu_dir, 'topology/core_id')
    core_id = int(open(cid_file).read().strip()) if os.path.exists(cid_file) else cpu_id
    # Read cpu_capacity (fallback for P/E classification)
    cap_file = os.path.join(cpu_dir, 'cpu_capacity')
    capacity = int(open(cap_file).read().strip()) if os.path.exists(cap_file) else 1024

    cores[str(cpu_id)] = {
        'cpu_id': cpu_id,
        'core_id': core_id,
        'core_type': core_type or ('P-core' if capacity >= 950 else 'E-core'),
        'siblings': siblings,
        'capacity': capacity,
    }

# Classify into sets
p_cores = [str(c['cpu_id']) for c in cores.values() if c['core_type'] == 'P-core']
e_cores = [str(c['cpu_id']) for c in cores.values() if c['core_type'] == 'E-core']

# Build affinity sets
seen_p_cores = set()
p_physical = []
p_all_threads = []
for cid in p_cores:
    c = cores[cid]
    if c['core_id'] not in seen_p_cores:
        seen_p_cores.add(c['core_id'])
        p_physical.append(cid)
    p_all_threads.append(cid)

# P + E subset: all P-core threads + half of distinct E-cores
e_distinct = []
seen_e = set()
for cid in e_cores:
    c = cores[cid]
    if c['core_id'] not in seen_e:
        seen_e.add(c['core_id'])
        e_distinct.append(cid)
e_half = e_distinct[:max(1, len(e_distinct)//2)]
p_plus_e = p_all_threads + e_half

result = {
    'core_topology': cores,
    'p_cores': p_cores,
    'e_cores': e_cores,
    'affinity_sets': {
        'unrestricted': ','.join(sorted(cores.keys(), key=int)),
        'p_physical': ','.join(sorted(p_physical, key=int)),
        'p_all_threads': ','.join(sorted(p_all_threads, key=int)),
        'p_plus_e': ','.join(sorted(p_plus_e, key=int)),
    }
}
with open('$out', 'w') as f:
    json.dump(result, f, indent=2)

# Validate sets are non-empty and subset of online CPUs
online = {str(c['cpu_id']) for c in cores.values()}
for name, cpuset in result['affinity_sets'].items():
    cpus = set(cpuset.split(',')) if cpuset else set()
    if not cpus:
        print(f'WARN: empty affinity set: {name}', file=__import__('sys').stderr)
    elif not cpus.issubset(online):
        print(f'ERROR: affinity set {name} contains offline CPUs', file=__import__('sys').stderr)
        __import__('sys').exit(1)

print(json.dumps({'status': 'ok', 'affinity_count': len(result['affinity_sets'])}))
" 2>/dev/null
}

# ── GPU discovery ──────────────────────────────────────────────────────────
discover_idle_gpu() {
  if [[ -n "$GPU_UUID" ]]; then
    # Validate user-supplied GPU
    if ! nvidia-smi --query-gpu=uuid --format=csv,noheader 2>/dev/null | grep -qF "$GPU_UUID"; then
      err "GPU UUID not found: $GPU_UUID"
      nvidia-smi --query-gpu=uuid,name --format=csv,noheader >&2
      exit 1
    fi
    local util mem
    read -r util mem <<< "$(nvidia-smi --query-gpu=uuid,utilization.gpu,memory.used \
      --format=csv,noheader,nounits 2>/dev/null | grep "^$GPU_UUID" | cut -d, -f2- | xargs)"
    PRODUCTION_GPU=$(docker inspect s2cpp-backend \
      --format '{{range .Config.Env}}{{if eq (printf "%.20s" .) "NVIDIA_VISIBLE_DEVICE"}}{{.}}{{end}}{{end}}' \
      2>/dev/null | sed 's/NVIDIA_VISIBLE_DEVICES=//' | xargs || echo 'none')
    if [[ "$GPU_UUID" == "$PRODUCTION_GPU" ]] && [[ "$ALLOW_PRODUCTION_GPU" != true ]]; then
      err "Supplied GPU is production GPU. Add --allow-production-gpu."
      exit 1
    fi
    if float_lt "$util" "80" || [[ "$ALLOW_PRODUCTION_GPU" == true ]]; then
      info "GPU: $GPU_UUID (util=$util%, mem=$mem MiB)"
      return
    fi
    warn "GPU $GPU_UUID appears busy (util=$util%). Continuing anyway (user-specified)."
    return
  fi
  # Auto-detect idle non-production GPU
  PRODUCTION_GPU="${PRODUCTION_GPU:-none}"
  local all_gpus
  all_gpus=$(nvidia-smi --query-gpu=uuid,utilization.gpu,memory.used --format=csv,noheader,nounits 2>/dev/null)
  while IFS=, read -r uuid util mem; do
    uuid=$(echo "$uuid" | xargs); util=$(echo "$util" | xargs); mem=$(echo "$mem" | xargs)
    [[ "$uuid" == "$PRODUCTION_GPU" ]] && continue
    if float_lt "$util" "10" && float_lt "$mem" "500"; then
      GPU_UUID="$uuid"
      info "Selected GPU: $uuid (util=$util%, mem=$mem MiB)"
      return
    fi
  done <<< "$all_gpus"
  err "No idle GPU found. Use --gpu UUID or --allow-production-gpu."
  exit 1
}

# ── Start/stop backend ─────────────────────────────────────────────────────
start_backend_q4() {
  local threads="$1" cpuset="${2:-}" label="$3"
  info "Starting Q4 backend: $label (threads=$threads${cpuset:+, cpuset=$cpuset})..."

  docker rm -f "$BENCH_CONTAINER" 2>/dev/null || true

  local cpuset_arg=""; [[ -n "$cpuset" ]] && cpuset_arg="--cpuset-cpus=$cpuset"
  local voice_mount=""; [[ -n "$HOST_VOICES" ]] && voice_mount="-v $HOST_VOICES:/voices:ro"

  docker run -d --name "$BENCH_CONTAINER" \
    --gpus "\"device=$GPU_UUID\"" \
    --network sorilonet \
    $cpuset_arg \
    -p "$BENCH_PORT:3030" \
    -v "$HOST_MODELS:/models:ro" \
    $voice_mount \
    -e "S2_MODEL=/models/$MODEL_FILE" \
    -e "S2_GPU_LAYERS=-1" \
    -e "S2_CODEC_CPU=false" \
    -e "S2_THREADS=$threads" \
    "$BACKEND_IMAGE" \
    > /dev/null

  # Readiness: poll until Launching line + HTTP reachable, resetting each iteration
  local elapsed=0
  while [[ $elapsed -lt $TIMEOUT ]]; do
    local curl_exit="" http_code="" launched="" http_ok=""
    local startup_log
    startup_log=$(docker logs "$BENCH_CONTAINER" 2>/dev/null || true)

    # Check for fatal errors
    if echo "$startup_log" | grep -qi "ERROR"; then
      err "Backend startup error"; echo "$startup_log" | grep -i "ERROR" >&2
      docker rm -f "$BENCH_CONTAINER" 2>/dev/null || true; return 1
    fi
    # Container alive
    if ! docker inspect "$BENCH_CONTAINER" --format '{{.State.Running}}' 2>/dev/null | grep -q true; then
      err "Backend container exited"; docker logs "$BENCH_CONTAINER" 2>/dev/null | tail -10 >&2; return 1
    fi

    echo "$startup_log" | grep -q "Launching: s2 --model /models/$MODEL_FILE" && launched=true
    http_code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 2 \
      "http://127.0.0.1:$BENCH_PORT/" 2>/dev/null); curl_exit=$?
    [[ -z "${curl_exit:-}" ]] && [[ "$http_code" =~ ^[0-9]{3}$ ]] && [[ "$http_code" != "000" ]] && http_ok=true

    if [[ "$launched" == true ]] && [[ "$http_ok" == true ]]; then
      ok "Backend ready: $label"
      return 0
    fi
    sleep 2; elapsed=$((elapsed + 2))
  done
  err "Backend readiness timeout"; docker rm -f "$BENCH_CONTAINER" 2>/dev/null || true; return 1
}

stop_backend() { docker rm -f "$BENCH_CONTAINER" 2>/dev/null || true; }

# ── Run benchmark ──────────────────────────────────────────────────────────
run_q4_benchmark() {
  local label="$1" threads="$2" ctx="${3:-4}" hb="${4:-0}" sb="${5:-0}" ll="${6:-true}"
  local extra_args="--codec-context $ctx"
  # Pass low_latency explicitly (default is true, --no-low-latency disables)
  [[ "$ll" != "true" ]] && extra_args="$extra_args --no-low-latency"

  PYTHONPATH="$REPO_ROOT" python3 "$REPO_ROOT/scripts/benchmark_quantization.py" \
    --run-real \
    --endpoint "127.0.0.1:$BENCH_PORT" \
    --models "$HOST_MODELS/$MODEL_FILE" \
    --stride "$STRIDE" \
    --codec-context "$ctx" \
    --warmup-runs "$WARMUP_RUNS" \
    --measured-runs "$MEASURED_RUNS" \
    --output-dir "$ARTIFACT_DIR" \
    --candidate-dir "$label" \
    --text "$BENCHMARK_TEXT" \
    --timeout "$TIMEOUT" \
    ${USER_VOICE:+--voice "$USER_VOICE"} \
    ${USER_VOICE_DIR:+--voice-dir "$USER_VOICE_DIR"} \
    || warn "Benchmark exited non-zero for $label"
}

# ── Capture metrics ────────────────────────────────────────────────────────
capture_metrics_q4() {
  local label="$1" dir="$2"
  sleep 3  # allow buffered output to flush
  docker logs "$BENCH_CONTAINER" 2>/dev/null > "$dir/backend_metrics.log" || true
  grep '\[Metrics\]' "$dir/backend_metrics.log" 2>/dev/null > "$dir/backend_metrics.log.metrics" || true
}

# ── GPU telemetry ──────────────────────────────────────────────────────────
GPU_TELEM_PID=""
start_gpu_telemetry() {
  local file="$1"
  ensure_dir "$(dirname "$file")"
  echo "timestamp,gpu_uuid,util_pct,mem_used_mib,mem_total_mib,temp_c,power_w,power_limit_w,sm_mhz,mem_mhz,pstate,throttle_reason" > "$file"
  (
    while kill -0 $$ 2>/dev/null; do
      nvidia-smi --query-gpu=uuid,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,clocks.sm,clocks.mem,pstate,clocks_throttle_reasons.active \
        --format=csv,noheader,nounits 2>/dev/null | while IFS=, read -r uuid util mu mt temp pw pl sm mc ps thr; do
        echo "$(date -u +%Y-%m-%dT%H:%M:%S),$uuid,$util,$mu,$mt,$temp,$pw,$pl,$sm,$mc,$ps,$thr" >> "$file"
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

# ── CPU telemetry (Python /proc/stat fallback) ─────────────────────────────
CPU_TELEM_PID=""
start_cpu_telemetry() {
  local file="$1"
  ensure_dir "$(dirname "$file")"
  echo "timestamp,cpu_id,user,system,iowait,idle" > "$file"
  python3 -c "
import time, os
prev = {}
while True:
    try:
        with open('/proc/stat') as f:
            for line in f:
                if not line.startswith('cpu'): break
                parts = line.split()
                cpu = parts[0]
                vals = [int(x) for x in parts[1:8]]
                total = sum(vals)
                idle = vals[3]
                if cpu in prev:
                    prev_total, prev_idle = prev[cpu]
                    d_total = total - prev_total
                    d_idle = idle - prev_idle
                    util = 100.0 * (d_total - d_idle) / d_total if d_total > 0 else 0.0
                    ts = __import__('datetime').datetime.utcnow().isoformat() + 'Z'
                    with open('$file', 'a') as out:
                        out.write(f'{ts},{cpu},{util:.1f},0.0,0.0,{100.0-util:.1f}\\n')
                prev[cpu] = (total, idle)
    except: pass
    time.sleep(1)
" &
  CPU_TELEM_PID=$!
}
stop_cpu_telemetry() {
  [[ -n "${CPU_TELEM_PID:-}" ]] && kill -0 "$CPU_TELEM_PID" 2>/dev/null && kill "$CPU_TELEM_PID" 2>/dev/null && wait "$CPU_TELEM_PID" 2>/dev/null || true
  CPU_TELEM_PID=""
}

# ── Create WAV ─────────────────────────────────────────────────────────────
create_host_wav() {
  local pcm="$1" wav="$2"
  python3 -c "
import wave
with open('$pcm', 'rb') as pf: pcm = pf.read()
with wave.open('$wav', 'wb') as wf:
    wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(44100); wf.writeframes(pcm)
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
  local best_rtf=999 best_threads=0

  for t in "${threads_list[@]}"; do
    local label="threads_${t}"; ensure_dir "$ARTIFACT_DIR/$label"
    ATTEMPTED=$((ATTEMPTED + 1))
    info "Threads=$t..."

    start_gpu_telemetry "$ARTIFACT_DIR/$label/gpu_telemetry.csv"
    start_cpu_telemetry "$ARTIFACT_DIR/$label/cpu_telemetry.csv"
    if ! start_backend_q4 "$t" "" "$label"; then
      stop_gpu_telemetry; stop_cpu_telemetry; FAILED=$((FAILED + 1)); continue
    fi
    run_q4_benchmark "$label" "$t" "$CODEC_CONTEXT" "$HOLDBACK" "$START_BUFFER" "$LOW_LATENCY"
    capture_metrics_q4 "$label" "$ARTIFACT_DIR/$label"
    docker logs "$BENCH_CONTAINER" 2>/dev/null > "$ARTIFACT_DIR/$label/startup.log" || true
    stop_gpu_telemetry; stop_cpu_telemetry; stop_backend

    local rj="$ARTIFACT_DIR/$label/results.json"
    if [[ -f "$rj" ]]; then
      local rtf=$(python3 -c "import json; d=json.load(open('$rj')); s=d['summaries'][0]; print(s['avg_rtf'])" 2>/dev/null || echo "999")
      info "  RTF=$rtf"
      if float_lt "$rtf" "$best_rtf"; then best_rtf=$rtf; best_threads=$t; fi
      SUCCESSFUL=$((SUCCESSFUL + 1))
    else
      MISSING_RESULTS=$((MISSING_RESULTS + 1)); FAILED=$((FAILED + 1))
    fi
  done
  echo "$best_threads" > "$ARTIFACT_DIR/best_threads.txt"
  info "Best threads: $best_threads (RTF=$best_rtf)"
  BEST_THREADS=$best_threads
}

# ── AFFINITY SWEEP ─────────────────────────────────────────────────────────
run_affinity_sweep() {
  local best_t="${1:-0}"
  local topo_file="$ARTIFACT_DIR/core_topology.json"
  if [[ ! -f "$topo_file" ]]; then
    err "Topology file not found. Run --phase threads first or provide core_topology.json."
    return 1
  fi
  local sets
  sets=$(python3 -c "import json; t=json.load(open('$topo_file')); print(json.dumps(t['affinity_sets']))" 2>/dev/null || echo '{}')

  for name in unrestricted p_physical p_all_threads p_plus_e; do
    local cpus=$(python3 -c "import json; print(json.loads('''$sets''').get('$name',''))" 2>/dev/null)
    [[ -z "$cpus" ]] && { warn "Empty cpuset for $name, skipping"; continue; }

    local label="affinity_${name}"; ensure_dir "$ARTIFACT_DIR/$label"
    ATTEMPTED=$((ATTEMPTED + 1))
    info "Affinity: $name (cpus=$cpus, threads=$best_t)..."

    start_gpu_telemetry "$ARTIFACT_DIR/$label/gpu_telemetry.csv"
    start_cpu_telemetry "$ARTIFACT_DIR/$label/cpu_telemetry.csv"
    if ! start_backend_q4 "$best_t" "$cpus" "$label"; then
      stop_gpu_telemetry; stop_cpu_telemetry; FAILED=$((FAILED + 1)); continue
    fi
    run_q4_benchmark "$label" "$best_t" "$CODEC_CONTEXT" "$HOLDBACK" "$START_BUFFER" "$LOW_LATENCY"
    capture_metrics_q4 "$label" "$ARTIFACT_DIR/$label"
    docker logs "$BENCH_CONTAINER" 2>/dev/null > "$ARTIFACT_DIR/$label/startup.log" || true
    stop_gpu_telemetry; stop_cpu_telemetry; stop_backend

    [[ -f "$ARTIFACT_DIR/$label/results.json" ]] && SUCCESSFUL=$((SUCCESSFUL + 1)) || { MISSING_RESULTS=$((MISSING_RESULTS + 1)); FAILED=$((FAILED + 1)); }
  done
}

# ── BLIPPING DIAGNOSTIC ────────────────────────────────────────────────────
run_blipping_diagnostic() {
  local best_t="${1:-0}"
  local configs=(
    "blip_ctx4_hb0:4:0"
    "blip_ctx64_hb0:64:0"
    "blip_ctx64_hb1:64:1"
  )
  for cfg in "${configs[@]}"; do
    local label="${cfg%%:*}"; local rest="${cfg#*:}"; local ctx="${rest%%:*}"; local hb="${rest##*:}"
    ensure_dir "$ARTIFACT_DIR/$label"
    ATTEMPTED=$((ATTEMPTED + 1))
    info "Blipping: $label (context=$ctx, holdback=$hb)..."

    start_gpu_telemetry "$ARTIFACT_DIR/$label/gpu_telemetry.csv"
    if ! start_backend_q4 "$best_t" "" "$label"; then
      stop_gpu_telemetry; FAILED=$((FAILED + 1)); continue
    fi
    run_q4_benchmark "$label" "$best_t" "$ctx" "$hb" "$START_BUFFER" "$LOW_LATENCY"
    capture_metrics_q4 "$label" "$ARTIFACT_DIR/$label"
    docker logs "$BENCH_CONTAINER" 2>/dev/null > "$ARTIFACT_DIR/$label/startup.log" || true
    stop_gpu_telemetry; stop_backend

    if [[ -f "$ARTIFACT_DIR/$label/results.json" ]]; then
      SUCCESSFUL=$((SUCCESSFUL + 1))
      # Create WAVs
      local wav_ok=true
      while IFS= read -r -d '' pcm; do
        create_host_wav "$pcm" "${pcm%.pcm}.wav" || wav_ok=false
      done < <(find "$ARTIFACT_DIR/$label" -name '*.pcm' -print0 2>/dev/null || true)
      [[ "$wav_ok" != true ]] && WAV_FAILURES=$((WAV_FAILURES + 1))
    else
      MISSING_RESULTS=$((MISSING_RESULTS + 1)); FAILED=$((FAILED + 1))
    fi
  done
}

# ── Combined aggregation ───────────────────────────────────────────────────
generate_combined_report() {
  python3 "$REPO_ROOT/scripts/_generate_q4_combined_report.py" "$ARTIFACT_DIR" 2>/dev/null || warn "Combined report generation failed"
}

# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

# Dry-run
if [[ "$RUN_REAL" != true ]]; then
  echo "========================================"
  echo " DRY RUN — Q4 Runtime Tuning"
  echo "========================================"
  echo "Model: $MODEL_FILE | Stride: $STRIDE (fixed) | Phase: $PHASE"
  [[ -n "$USER_THREADS" ]] && echo "Threads: $USER_THREADS"
  echo ""
  [[ "$PHASE" == "all" || "$PHASE" == "threads" ]] && echo "Thread sweep: 0, 8, 16, 24, 32"
  [[ "$PHASE" == "all" || "$PHASE" == "affinity" ]] && echo "Affinity: unrestricted, p_physical, p_all_threads, p_plus_e"
  [[ "$PHASE" == "all" || "$PHASE" == "blipping" ]] && echo "Blipping: ctx4/hb0, ctx64/hb0, ctx64/hb1"
  echo ""
  echo "Phased execution:"
  echo "  --phase threads"
  echo "  --phase affinity --threads <best>"
  echo "  --phase blipping --threads <best>"
  echo ""
  echo "Add --run-real to execute."
  exit 0
fi

# Check prerequisites
for cmd in docker curl python3 nvidia-smi git; do
  command -v "$cmd" &>/dev/null || { err "Missing: $cmd"; exit 1; }
done

discover_mounts
[[ ! -f "$HOST_MODELS/$MODEL_FILE" ]] && { err "Model not found: $HOST_MODELS/$MODEL_FILE"; exit 1; }
ok "Model: $MODEL_FILE ($(stat -c%s "$HOST_MODELS/$MODEL_FILE" | numfmt --to=iec))"
discover_production_voice
discover_idle_gpu

TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)
if [[ -n "$RESUME_ARTIFACT" ]]; then
  ARTIFACT_DIR="$RESUME_ARTIFACT"
  info "Resuming artifact: $ARTIFACT_DIR"
else
  ARTIFACT_DIR="$ARTIFACT_BASE/$TIMESTAMP"
fi
ensure_dir "$ARTIFACT_DIR"

# System state
{
  echo "phase=8E.1a"; echo "timestamp=$TIMESTAMP"
  echo "model=$MODEL_FILE"; echo "stride=$STRIDE"
  echo "production_voice=$PRODUCTION_VOICE"; echo "production_voice_dir=$PRODUCTION_VOICE_DIR"
  echo "gpu_uuid=$GPU_UUID"; echo "git_commit=$(git -C "$REPO_ROOT" rev-parse HEAD)"
} > "$ARTIFACT_DIR/system_state.txt"
nvidia-smi > "$ARTIFACT_DIR/nvidia_smi_snapshot.txt" 2>/dev/null || true
nvidia-smi --query-gpu=uuid,clocks.max.sm,clocks.max.mem,power.limit,power.default_limit \
  --format=csv,noheader > "$ARTIFACT_DIR/stock_gpu_state.txt" 2>/dev/null || true

# CPU topology
build_cpu_topology "$ARTIFACT_DIR/core_topology.json" || warn "CPU topology build had issues"

# Best threads (from user, resume, or sweep)
BEST_THREADS="${USER_THREADS:-0}"
[[ "$PHASE" == "all" || "$PHASE" == "threads" ]] && run_thread_sweep
[[ -f "$ARTIFACT_DIR/best_threads.txt" ]] && BEST_THREADS=$(cat "$ARTIFACT_DIR/best_threads.txt")

[[ "$PHASE" == "all" || "$PHASE" == "affinity" ]] && run_affinity_sweep "$BEST_THREADS"
[[ "$PHASE" == "all" || "$PHASE" == "blipping" ]] && run_blipping_diagnostic "$BEST_THREADS"

generate_combined_report

# ── Final accounting ───────────────────────────────────────────────────────
echo ""
info "=== ACCOUNTING ==="
info "Attempted: $ATTEMPTED | Successful: $SUCCESSFUL | Failed: $FAILED"
info "Missing results: $MISSING_RESULTS | WAV failures: $WAV_FAILURES"
info "Artifact: $ARTIFACT_DIR"

FINAL_EXIT=0
if [[ $FAILED -gt 0 ]]; then
  err "Incomplete: $FAILED/$ATTEMPTED configurations failed"
  FINAL_EXIT=1
fi
if [[ $ATTEMPTED -gt 0 ]] && [[ $SUCCESSFUL -eq 0 ]]; then
  err "All configurations failed"
  FINAL_EXIT=1
fi
[[ $FINAL_EXIT -eq 0 ]] && info "Q4 runtime tuning complete" || warn "Q4 runtime tuning had failures"
exit $FINAL_EXIT

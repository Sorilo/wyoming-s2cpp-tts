#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# Phase 8E.1b: Q4_K_M runtime tuning harness (validated for live execution).
# ────────────────────────────────────────────────────────────────────────────
set -euo pipefail; shopt -s lastpipe

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info() { echo -e "${CYAN}[INFO]${NC}  $*" >&2; }
ok()   { echo -e "${GREEN}[OK]${NC}    $*" >&2; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*" >&2; }
err()  { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ── Config ─────────────────────────────────────────────────────────────────
MODEL_FILE="s2-pro-q4_k_m.gguf"; MODEL_LABEL="q4_k_m"
BENCH_CONTAINER="s2cpp-backend-tune"; BENCH_PORT="3034"
BACKEND_IMAGE="ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd"
STRIDE=4; CODEC_CONTEXT=4; HOLDBACK=0; START_BUFFER=0; LOW_LATENCY=true
WARMUP_RUNS=1; MEASURED_RUNS=3; TIMEOUT=120
BENCHMARK_TEXT="The morning sun cast long shadows across the quiet neighborhood as residents began their daily routines. A gentle breeze carried the scent of fresh coffee from the corner cafe, where early customers sat reading newspapers and checking their phones. Children hurried past with backpacks slung over their shoulders, their laughter echoing off the brick buildings."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ARTIFACT_BASE="$REPO_ROOT/verification_artifacts/q4_runtime_tuning"

ATTEMPTED=0; SUCCESSFUL=0; FAILED=0; MISSING_RESULTS=0; WAV_FAILURES=0

# ── Args ───────────────────────────────────────────────────────────────────
RUN_REAL=false; GPU_UUID=""; ALLOW_PRODUCTION_GPU=false; FORCE_BUSY_GPU=false
PHASE="all"; USER_THREADS=""; RESUME_ARTIFACT=""; USER_VOICE=""; USER_VOICE_DIR=""
VALIDATE_ONLY=false; SMOKE_TEST=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-real)             RUN_REAL=true; shift ;;
    --gpu)                  GPU_UUID="$2"; shift 2 ;;
    --allow-production-gpu) ALLOW_PRODUCTION_GPU=true; shift ;;
    --force-busy-gpu)       FORCE_BUSY_GPU=true; shift ;;
    --phase)                PHASE="$2"; shift 2 ;;
    --threads)              USER_THREADS="$2"; shift 2 ;;
    --resume-artifact)      RESUME_ARTIFACT="$2"; shift 2 ;;
    --voice)                USER_VOICE="$2"; shift 2 ;;
    --voice-dir)            USER_VOICE_DIR="$2"; shift 2 ;;
    --validate-only)        VALIDATE_ONLY=true; shift ;;
    --smoke-test)           SMOKE_TEST=true; shift ;;
    *) err "Unknown: $1"; exit 1 ;;
  esac
done

case "$PHASE" in all|threads|affinity|blipping|context-screen) ;; *) err "Invalid phase: $PHASE"; exit 1 ;; esac

float_lt() { python3 -c "import sys; sys.exit(0 if float('$1') < float('$2') else 1)"; }
ensure_dir() { mkdir -p "$1"; }

# ── Discover mounts ────────────────────────────────────────────────────────
discover_mounts() {
  HOST_MODELS=$(docker inspect s2cpp-backend --format '{{range .Mounts}}{{if eq .Destination "/models"}}{{.Source}}{{end}}{{end}}' 2>/dev/null || echo '')
  HOST_VOICES=$(docker inspect s2cpp-backend --format '{{range .Mounts}}{{if eq .Destination "/voices"}}{{.Source}}{{end}}{{end}}' 2>/dev/null || echo '')
  [[ -z "$HOST_MODELS" ]] && { err "Cannot discover /models mount"; exit 1; }
  info "Host models: $HOST_MODELS"
  [[ -n "$HOST_VOICES" ]] && info "Host voices: $HOST_VOICES" || warn "No /voices mount on production backend"
}

# ── Discover production voice (full env parsing) ───────────────────────────
discover_production_voice() {
  EFFECTIVE_VOICE="${USER_VOICE:-}"
  CONTAINER_VOICE_DIR="/voices"

  if [[ -n "$EFFECTIVE_VOICE" ]]; then
    info "Using user-supplied voice: $EFFECTIVE_VOICE"
    # Verify the .s2voice file exists
    local voice_host="${USER_VOICE_DIR:-$HOST_VOICES}"
    local voice_file="$voice_host/${EFFECTIVE_VOICE}.s2voice"
    if [[ -n "$voice_host" ]] && [[ ! -f "$voice_file" ]]; then
      err "Voice profile not found: $voice_file"
      err "Supply --voice-dir with the correct host source directory."
      exit 1
    fi
    if [[ -f "$voice_file" ]]; then
      ok "Voice profile verified: $voice_file"
    fi
    return
  fi

  # Parse full wrapper environment, match exact keys
  local wrapper_env
  wrapper_env=$(docker inspect wyoming-s2cpp-tts --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null || echo '')
  EFFECTIVE_VOICE=$(echo "$wrapper_env" | grep '^S2_DEFAULT_VOICE=' | head -1 | cut -d= -f2- | xargs || echo '')
  local wrapper_voice_dir
  wrapper_voice_dir=$(echo "$wrapper_env" | grep '^S2_VOICE_DIR=' | head -1 | cut -d= -f2- | xargs || echo '/voices')
  CONTAINER_VOICE_DIR="$wrapper_voice_dir"

  if [[ -n "$EFFECTIVE_VOICE" ]] && [[ -n "$EFFECTIVE_VOICE" ]]; then
    info "Production voice: $EFFECTIVE_VOICE (dir=$CONTAINER_VOICE_DIR)"
    # Verify .s2voice file exists on host
    local voice_host="${USER_VOICE_DIR:-$HOST_VOICES}"
    local voice_file="$voice_host/${EFFECTIVE_VOICE}.s2voice"
    if [[ -n "$voice_host" ]] && [[ -f "$voice_file" ]]; then
      ok "Voice profile exists: $voice_file"
    elif [[ -n "$HOST_VOICES" ]]; then
      err "Production voice '$EFFECTIVE_VOICE' not found at: $voice_file"
      err "Supply --voice <id> --voice-dir <host_source_dir> or create the profile."
      exit 1
    else
      warn "No /voices host mount — cannot verify voice file"
    fi
  else
    warn "No production voice found — benchmark uses default (not representative)"
    warn "Add --voice <voice_id> --voice-dir <host_dir> for production parity"
    EFFECTIVE_VOICE=""
  fi
}

# ── Discover production GPU (full env parsing) ─────────────────────────────
discover_production_gpu() {
  local backend_env
  backend_env=$(docker inspect s2cpp-backend --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null || echo '')
  PRODUCTION_GPU=$(echo "$backend_env" | grep '^NVIDIA_VISIBLE_DEVICES=' | head -1 | cut -d= -f2- | xargs || echo '')
  if [[ -z "$PRODUCTION_GPU" ]]; then
    PRODUCTION_GPU=$(nvidia-smi --query-compute-apps=gpu_uuid --format=csv,noheader 2>/dev/null | head -1 | xargs || echo 'none')
  fi
  info "Production GPU: $PRODUCTION_GPU"
}

# ── GPU selection ──────────────────────────────────────────────────────────
select_gpu() {
  if [[ -n "$GPU_UUID" ]]; then
    if ! nvidia-smi --query-gpu=uuid --format=csv,noheader 2>/dev/null | grep -qF "$GPU_UUID"; then
      err "GPU UUID not found: $GPU_UUID"; nvidia-smi --query-gpu=uuid,name --format=csv,noheader >&2; exit 1
    fi
    if [[ "$GPU_UUID" == "$PRODUCTION_GPU" ]] && [[ "$ALLOW_PRODUCTION_GPU" != true ]]; then
      err "Supplied GPU is production GPU. Add --allow-production-gpu."; exit 1
    fi
    if [[ "$FORCE_BUSY_GPU" != true ]]; then
      local util mem
      read -r util mem <<< "$(nvidia-smi --query-gpu=uuid,utilization.gpu,memory.used --format=csv,noheader,nounits 2>/dev/null | grep "^$GPU_UUID" | cut -d, -f2- | xargs)"
      if float_lt "$util" "80"; then
        info "GPU: $GPU_UUID (util=$util%, mem=$mem MiB)"
        return
      fi
      err "GPU $GPU_UUID is busy (util=$util%). Add --force-busy-gpu to override."
      exit 1
    fi
    info "GPU: $GPU_UUID (forced)"
    return
  fi
  # Auto: exclude production, prefer idle
  local all_gpus
  all_gpus=$(nvidia-smi --query-gpu=uuid,utilization.gpu,memory.used --format=csv,noheader,nounits 2>/dev/null)
  while IFS=, read -r uuid util mem; do
    uuid=$(echo "$uuid" | xargs); util=$(echo "$util" | xargs); mem=$(echo "$mem" | xargs)
    [[ "$uuid" == "$PRODUCTION_GPU" ]] && continue
    if float_lt "$util" "10" && float_lt "$mem" "500"; then
      GPU_UUID="$uuid"; info "Selected GPU: $uuid (util=$util%, mem=$mem MiB)"; return
    fi
  done <<< "$all_gpus"
  err "No idle GPU. Use --gpu UUID or --allow-production-gpu."; exit 1
}

# ── CPU topology from /sys ─────────────────────────────────────────────────
build_cpu_topology() {
  local out="$1"
  python3 -c "
import json, os, glob, sys, subprocess

cores = {}
classification_method = 'unknown'

for cpu_dir in sorted(glob.glob('/sys/devices/system/cpu/cpu[0-9]*')):
    cpu_id = int(os.path.basename(cpu_dir).replace('cpu', ''))
    ct_file = os.path.join(cpu_dir, 'topology/core_type')
    raw_type = open(ct_file).read().strip() if os.path.exists(ct_file) else None
    norm_type = 'unknown'

    # 1. core_type (preferred)
    if raw_type:
        t = raw_type.strip()
        if t.isdigit():
            norm_type = 'P-core' if int(t) == 0 else 'E-core'
        else:
            t = t.lower()
            if 'core' in t and 'atom' not in t: norm_type = 'P-core'
            elif 'atom' in t: norm_type = 'E-core'
        if norm_type != 'unknown':
            classification_method = 'core_type'

    # 2. lscpu CORETYPE
    if norm_type == 'unknown':
        try:
            lscpu_out = subprocess.check_output(['lscpu', '-e=CPU,CORETYPE'], text=True, timeout=5)
            for line in lscpu_out.strip().split(chr(10)):
                parts = line.split()
                if len(parts) >= 2 and parts[0] == str(cpu_id):
                    ct = parts[1].lower()
                    if 'p' in ct or ('core' in ct and 'atom' not in ct): norm_type = 'P-core'
                    elif 'e' in ct or 'atom' in ct: norm_type = 'E-core'
                    break
            if norm_type != 'unknown':
                classification_method = 'lscpu_coretype'
        except: pass

    # 3. cpu_capacity
    if norm_type == 'unknown':
        cap_file = os.path.join(cpu_dir, 'cpu_capacity')
        if os.path.exists(cap_file):
            cap = int(open(cap_file).read().strip())
            norm_type = 'P-core' if cap > 900 else 'E-core' if cap > 100 else 'unknown'
            if norm_type != 'unknown':
                classification_method = 'cpu_capacity'

    ts_file = os.path.join(cpu_dir, 'topology/thread_siblings_list')
    siblings = open(ts_file).read().strip() if os.path.exists(ts_file) else str(cpu_id)
    cid_file = os.path.join(cpu_dir, 'topology/core_id')
    core_id = int(open(cid_file).read().strip()) if os.path.exists(cid_file) else cpu_id

    cores[str(cpu_id)] = {'cpu_id': cpu_id, 'core_id': core_id, 'raw_type': raw_type,
                           'normalized_type': norm_type, 'siblings': siblings,
                           'classification_method': ''}

# 4. Hybrid Intel fallback: thread_siblings_list — P-cores have 2 siblings (HT), E-cores have 1
if all(c['normalized_type'] == 'unknown' for c in cores.values()):
    # Group by physical core: core_id determines which CPUs share a physical core
    core_groups = {}
    for cid, c in cores.items():
        key = c['core_id']
        core_groups.setdefault(key, []).append(cid)
    for cid, c in cores.items():
        group = core_groups[c['core_id']]
        if len(group) == 2:
            c['normalized_type'] = 'P-core'
        elif len(group) == 1:
            c['normalized_type'] = 'E-core'
    classification_method = 'thread_siblings'

# Record method
for c in cores.values():
    c['classification_method'] = classification_method

p_cores = sorted([c['cpu_id'] for c in cores.values() if c['normalized_type'] == 'P-core'])
e_cores = sorted([c['cpu_id'] for c in cores.values() if c['normalized_type'] == 'E-core'])
unknown = [c['cpu_id'] for c in cores.values() if c['normalized_type'] == 'unknown']

print(f'Classification method: {classification_method}', file=sys.stderr)
print(f'P-cores: {p_cores}', file=sys.stderr)
print(f'E-cores: {e_cores}', file=sys.stderr)

if unknown:
    print(f'WARN: {len(unknown)} CPUs unclassified: {unknown}', file=sys.stderr)
if not p_cores:
    print('ERROR: No P-cores identified', file=sys.stderr); sys.exit(1)
if not e_cores:
    print('WARN: No E-cores identified (expected on i9-13900K)', file=sys.stderr)

# Build affinity sets
seen_p = set(); p_physical = []; p_all = []
for cid in map(str, sorted(p_cores)):
    c = cores[cid]
    if c['core_id'] not in seen_p: seen_p.add(c['core_id']); p_physical.append(cid)
    p_all.append(cid)
seen_e = set(); e_distinct = []
for cid in map(str, sorted(e_cores)):
    c = cores[cid]
    if c['core_id'] not in seen_e: seen_e.add(c['core_id']); e_distinct.append(cid)
e_half = e_distinct[:max(1, len(e_distinct)//2)]
p_plus_e = p_all + e_half

affinity = {
    'unrestricted': ','.join(map(str, sorted(cores.keys()))),
    'p_physical': ','.join(p_physical),
    'p_all_threads': ','.join(p_all),
    'p_plus_e': ','.join(p_plus_e),
}
for name, cpuset in affinity.items():
    if not cpuset:
        print(f'ERROR: empty affinity set: {name}', file=sys.stderr); sys.exit(1)

with open('$out', 'w') as f:
    json.dump({
        'classification_method': classification_method,
        'core_topology': {str(k): v for k, v in cores.items()},
        'p_cores': [str(c) for c in p_cores],
        'e_cores': [str(c) for c in e_cores],
        'affinity_sets': affinity,
    }, f, indent=2)

# Validate expected topology for i9-13900K
if len(p_cores) == 16 and len(p_physical) == 8 and len(e_cores) == 16:
    print(f'MATCH: i9-13900K topology confirmed (8P+16E, 32 logical)', file=sys.stderr)
elif len(p_all) == 16 and len(e_cores) >= 8:
    print(f'WARN: P/E ratio unusual but usable: {len(p_cores)} P logical, {len(e_cores)} E logical', file=sys.stderr)

print(json.dumps({'status': 'ok', 'method': classification_method,
                  'p_logical': len(p_cores), 'e_logical': len(e_cores),
                  'p_physical': len(p_physical), 'affinity_sets': list(affinity.keys())}))
" 2>/dev/null
}

# ── Readiness (set -e safe, resets each iteration) ─────────────────────────
wait_backend_ready() {
  local label="$1"; local elapsed=0
  while [[ $elapsed -lt $TIMEOUT ]]; do
    local startup_log launched=false http_ok=false
    startup_log=$(docker logs "$BENCH_CONTAINER" 2>/dev/null || true)

    if echo "$startup_log" | grep -qi "ERROR"; then
      err "Backend startup error"; echo "$startup_log" | grep -i "ERROR" >&2; return 1
    fi
    if ! docker inspect "$BENCH_CONTAINER" --format '{{.State.Running}}' 2>/dev/null | grep -q true; then
      err "Backend exited"; docker logs "$BENCH_CONTAINER" 2>/dev/null | tail -10 >&2; return 1
    fi

    echo "$startup_log" | grep -q "Launching: s2 --model /models/$MODEL_FILE" && launched=true

    # Safe curl: capture status, exit code separately
    local http_code=""; local curl_exit=0
    if http_code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 2 "http://127.0.0.1:$BENCH_PORT/" 2>/dev/null); then
      curl_exit=0
    else
      curl_exit=$?; http_code="000"
    fi
    if [[ $curl_exit -eq 0 ]] && [[ "$http_code" =~ ^[0-9]{3}$ ]] && [[ "$http_code" != "000" ]]; then
      http_ok=true
    fi

    if [[ "$launched" == true ]] && [[ "$http_ok" == true ]]; then
      ok "Backend ready: $label"; return 0
    fi
    sleep 2; elapsed=$((elapsed + 2))
  done
  err "Readiness timeout"; return 1
}

# ── Start/stop backend ─────────────────────────────────────────────────────
start_backend_q4() {
  local threads="$1" cpuset="${2:-}" label="$3"
  info "Starting: $label (threads=$threads${cpuset:+, cpuset=$cpuset})..."
  docker rm -f "$BENCH_CONTAINER" 2>/dev/null || true
  local cpuset_arg=""; [[ -n "$cpuset" ]] && cpuset_arg="--cpuset-cpus=$cpuset"
  local voice_host="${USER_VOICE_DIR:-$HOST_VOICES}"
  local voice_mount=""; [[ -n "$voice_host" ]] && voice_mount="-v $voice_host:/voices:ro"
  docker run -d --name "$BENCH_CONTAINER" \
    --gpus "\"device=$GPU_UUID\"" --network sorilonet $cpuset_arg \
    -p "$BENCH_PORT:3030" -v "$HOST_MODELS:/models:ro" $voice_mount \
    -e "S2_MODEL=/models/$MODEL_FILE" -e "S2_GPU_LAYERS=-1" -e "S2_CODEC_CPU=false" \
    -e "S2_THREADS=$threads" "$BACKEND_IMAGE" > /dev/null
  wait_backend_ready "$label"
}
stop_backend() { docker rm -f "$BENCH_CONTAINER" 2>/dev/null || true; }

# ── Run benchmark (bash array, all params) ─────────────────────────────────
run_q4_benchmark() {
  local label="$1" threads="$2" cpuset="$3" ctx="$4" hb="$5" sb="$6" ll="$7"
  local args=(
    --run-real --endpoint "127.0.0.1:$BENCH_PORT" --models "$HOST_MODELS/$MODEL_FILE"
    --stride "$STRIDE" --codec-context "$ctx" --holdback "$hb" --start-buffer-ms "$sb"
    --warmup-runs "$WARMUP_RUNS" --measured-runs "$MEASURED_RUNS"
    --output-dir "$ARTIFACT_DIR" --candidate-dir "$label" --text "$BENCHMARK_TEXT" --timeout "$TIMEOUT"
  )
  [[ "$ll" != "true" ]] && args+=(--no-low-latency)
  [[ -n "${EFFECTIVE_VOICE:-}" ]] && args+=(--voice "$EFFECTIVE_VOICE")
  [[ -n "${CONTAINER_VOICE_DIR:-}" ]] && args+=(--voice-dir "$CONTAINER_VOICE_DIR")

  # Save effective config
  python3 -c "import json; json.dump({
    'threads': $threads, 'cpuset': '${cpuset}', 'stride': $STRIDE,
    'codec_context': $ctx, 'holdback': $hb, 'start_buffer_ms': $sb,
    'low_latency': '$ll' == 'true', 'voice': '${EFFECTIVE_VOICE:-}',
    'voice_dir': '${CONTAINER_VOICE_DIR:-/voices}', 'model': '$MODEL_FILE'
  }, open('$ARTIFACT_DIR/$label/effective_config.json', 'w'), indent=2)" 2>/dev/null

  PYTHONPATH="$REPO_ROOT" python3 "$REPO_ROOT/scripts/benchmark_quantization.py" "${args[@]}" || warn "Benchmark exited non-zero for $label"

  # Verify settings match between effective_config.json and results.json
  local rj="$ARTIFACT_DIR/$label/results.json"
  if [[ -f "$rj" ]]; then
    python3 << VERIFYEOF
import json, sys
with open('$rj') as f:
    data = json.load(f)
eff = json.load(open('$ARTIFACT_DIR/$label/effective_config.json'))
errors = []

# Check measured runs
measured = [r for s in data['summaries'] for r in s['runs'] if r.get('run_type')=='measured' and r.get('status')=='success']
if len(measured) < 3:
    errors.append(f'Only {len(measured)}/3 measured runs succeeded')

# Verify effective settings match requested (from results.json metadata)
meta_stride = data.get('stride')
meta_ctx = data.get('codec_context')
if meta_stride is not None and meta_stride != eff['stride']:
    errors.append(f'stride mismatch: requested={eff["stride"]}, effective={meta_stride}')
if meta_ctx is not None and meta_ctx != eff['codec_context']:
    errors.append(f'codec_context mismatch: requested={eff["codec_context"]}, effective={meta_ctx}')

if errors:
    print(f'VALIDATE_FAIL: $label: ' + '; '.join(errors), file=sys.stderr)
    sys.exit(1)
else:
    print(f'VALIDATE_OK: $label')
VERIFYEOF
    if [[ $? -ne 0 ]]; then
      warn "Settings validation failed for $label"
      return 1
    fi
  fi
}

# ── Metrics ────────────────────────────────────────────────────────────────
capture_metrics_q4() { local l="$1" d="$2"; sleep 3; docker logs "$BENCH_CONTAINER" 2>/dev/null > "$d/backend_metrics.log" || true; grep '\[Metrics\]' "$d/backend_metrics.log" 2>/dev/null > "$d/backend_metrics.log.metrics" || true; }

# ── Telemetry ──────────────────────────────────────────────────────────────
GPU_TELEM_PID=""
start_gpu_telemetry() { local f="$1"; ensure_dir "$(dirname "$f")"
  echo "timestamp,gpu_uuid,util_pct,mem_used_mib,mem_total_mib,temp_c,power_w,power_limit_w,sm_mhz,mem_mhz,pstate,throttle_reason" > "$f"
  ( while kill -0 $$ 2>/dev/null; do nvidia-smi --query-gpu=uuid,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,clocks.sm,clocks.mem,pstate,clocks_throttle_reasons.active --format=csv,noheader,nounits 2>/dev/null | while IFS=, read -r u ut mu mt t pw pl sm mc ps thr; do echo "$(date -u +%Y-%m-%dT%H:%M:%S),$u,$ut,$mu,$mt,$t,$pw,$pl,$sm,$mc,$ps,$thr" >> "$f"; done; sleep 1; done ) & GPU_TELEM_PID=$!; }
stop_gpu_telemetry() { [[ -n "${GPU_TELEM_PID:-}" ]] && kill -0 "$GPU_TELEM_PID" 2>/dev/null && kill "$GPU_TELEM_PID" 2>/dev/null && wait "$GPU_TELEM_PID" 2>/dev/null || true; GPU_TELEM_PID=""; }

CPU_TELEM_PID=""
start_cpu_telemetry() { local f="$1"; ensure_dir "$(dirname "$f")"
  echo "timestamp,cpu_id,user_pct,system_pct,iowait_pct,idle_pct" > "$f"
  python3 -c "import time,os; prev={}
while True:
  try:
    with open('/proc/stat') as fh:
      for line in fh:
        if not line.startswith('cpu'): break
        p=line.split(); cpu=p[0]; v=[int(x) for x in p[1:8]]; t=sum(v); idle=v[3]
        if cpu in prev:
          pt,pi=prev[cpu]; dt=t-pt; di=idle-pi; u=100.0*(dt-di)/dt if dt>0 else 0
          ts=__import__('datetime').datetime.utcnow().isoformat()+'Z'
          with open('$f','a') as o: o.write(f'{ts},{cpu},{u:.1f},0.0,0.0,{100.0-u:.1f}\\n')
        prev[cpu]=(t,idle)
  except: pass
  time.sleep(1)" & CPU_TELEM_PID=$!; }
stop_cpu_telemetry() { [[ -n "${CPU_TELEM_PID:-}" ]] && kill -0 "$CPU_TELEM_PID" 2>/dev/null && kill "$CPU_TELEM_PID" 2>/dev/null && wait "$CPU_TELEM_PID" 2>/dev/null || true; CPU_TELEM_PID=""; }

create_host_wav() { local p="$1" w="$2"
  python3 -c "import wave; pcm=open('$p','rb').read(); wf=wave.open('$w','wb'); wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(44100); wf.writeframes(pcm)" 2>/dev/null; [[ -s "$w" ]]; }

cleanup() { stop_gpu_telemetry; stop_cpu_telemetry; stop_backend; }
trap cleanup EXIT INT TERM

# ── Run sweeps ─────────────────────────────────────────────────────────────
run_thread_sweep() {
  local best_rtf=999 best_t=0
  for t in 0 8 16 24 32; do
    local label="threads_${t}"; ensure_dir "$ARTIFACT_DIR/$label"; ATTEMPTED=$((ATTEMPTED+1))
    start_gpu_telemetry "$ARTIFACT_DIR/$label/gpu_telemetry.csv"
    start_cpu_telemetry "$ARTIFACT_DIR/$label/cpu_telemetry.csv"
    start_backend_q4 "$t" "" "$label" || { stop_gpu_telemetry; stop_cpu_telemetry; FAILED=$((FAILED+1)); continue; }
    if ! run_q4_benchmark "$label" "$t" "" "$CODEC_CONTEXT" "$HOLDBACK" "$START_BUFFER" "$LOW_LATENCY"; then
      warn "Benchmark/validation failed for $label"
    fi
    capture_metrics_q4 "$label" "$ARTIFACT_DIR/$label"
    docker logs "$BENCH_CONTAINER" 2>/dev/null > "$ARTIFACT_DIR/$label/startup.log" || true
    stop_gpu_telemetry; stop_cpu_telemetry; stop_backend
    local rj="$ARTIFACT_DIR/$label/results.json"
    if [[ -f "$rj" ]] && python3 -c "import json; d=json.load(open('$rj')); measured=[r for s in d['summaries'] for r in s['runs'] if r.get('run_type')=='measured' and r.get('status')=='success']; exit(0 if len(measured)==3 else 1)" 2>/dev/null; then
      local rtf=$(python3 -c "import json; d=json.load(open('$rj')); print(d['summaries'][0]['avg_rtf'])" 2>/dev/null || echo "999")
      info "  RTF=$rtf"; float_lt "$rtf" "$best_rtf" && { best_rtf=$rtf; best_t=$t; }; SUCCESSFUL=$((SUCCESSFUL+1))
    else
      MISSING_RESULTS=$((MISSING_RESULTS+1)); FAILED=$((FAILED+1))
    fi
  done
  echo "$best_t" > "$ARTIFACT_DIR/best_threads.txt"; BEST_THREADS=$best_t
}

run_affinity_sweep() {
  local best_t="${1:-0}"; local topo="$ARTIFACT_DIR/core_topology.json"
  [[ ! -f "$topo" ]] && { err "No topology. Run --phase threads first."; return 1; }
  local sets=$(python3 -c "import json; print(json.dumps(json.load(open('$topo'))['affinity_sets']))" 2>/dev/null || echo '{}')
  for name in unrestricted p_physical p_all_threads p_plus_e; do
    local cpus=$(python3 -c "import json; print(json.loads('''$sets''').get('$name',''))" 2>/dev/null)
    [[ -z "$cpus" ]] && { warn "Empty cpuset: $name"; continue; }
    local label="affinity_${name}"; ensure_dir "$ARTIFACT_DIR/$label"; ATTEMPTED=$((ATTEMPTED+1))
    start_gpu_telemetry "$ARTIFACT_DIR/$label/gpu_telemetry.csv"
    start_cpu_telemetry "$ARTIFACT_DIR/$label/cpu_telemetry.csv"
    start_backend_q4 "$best_t" "$cpus" "$label" || { stop_gpu_telemetry; stop_cpu_telemetry; FAILED=$((FAILED+1)); continue; }
    if ! run_q4_benchmark "$label" "$best_t" "$cpus" "$CODEC_CONTEXT" "$HOLDBACK" "$START_BUFFER" "$LOW_LATENCY"; then
      warn "Benchmark/validation failed for $label"
    fi
    capture_metrics_q4 "$label" "$ARTIFACT_DIR/$label"
    docker logs "$BENCH_CONTAINER" 2>/dev/null > "$ARTIFACT_DIR/$label/startup.log" || true
    stop_gpu_telemetry; stop_cpu_telemetry; stop_backend
    [[ -f "$ARTIFACT_DIR/$label/results.json" ]] && SUCCESSFUL=$((SUCCESSFUL+1)) || { MISSING_RESULTS=$((MISSING_RESULTS+1)); FAILED=$((FAILED+1)); }
  done
}

run_blipping_diagnostic() {
  local best_t="${1:-0}"
  for cfg in "blip_ctx4_hb0:4:0" "blip_ctx64_hb0:64:0" "blip_ctx64_hb1:64:1"; do
    local label="${cfg%%:*}"; local r="${cfg#*:}"; local ctx="${r%%:*}"; local hb="${r##*:}"
    ensure_dir "$ARTIFACT_DIR/$label"; ATTEMPTED=$((ATTEMPTED+1))
    start_gpu_telemetry "$ARTIFACT_DIR/$label/gpu_telemetry.csv"
    start_backend_q4 "$best_t" "" "$label" || { stop_gpu_telemetry; FAILED=$((FAILED+1)); continue; }
    if ! run_q4_benchmark "$label" "$best_t" "" "$ctx" "$hb" "$START_BUFFER" "$LOW_LATENCY"; then
      warn "Benchmark/validation failed for $label"
    fi
    capture_metrics_q4 "$label" "$ARTIFACT_DIR/$label"
    docker logs "$BENCH_CONTAINER" 2>/dev/null > "$ARTIFACT_DIR/$label/startup.log" || true
    stop_gpu_telemetry; stop_backend
    if [[ -f "$ARTIFACT_DIR/$label/results.json" ]]; then
      SUCCESSFUL=$((SUCCESSFUL+1))
      local wok=true
      while IFS= read -r -d '' pcm; do create_host_wav "$pcm" "${pcm%.pcm}.wav" || wok=false; done < <(find "$ARTIFACT_DIR/$label" -name '*.pcm' -print0 2>/dev/null || true)
      [[ "$wok" != true ]] && WAV_FAILURES=$((WAV_FAILURES+1))
    else
      MISSING_RESULTS=$((MISSING_RESULTS+1)); FAILED=$((FAILED+1))
    fi
  done
}

# ── Context screening ──────────────────────────────────────────────────────
run_context_screen() {
  local contexts=(4 8 12 16 24 32 48 64)
  info "Context screen: ${contexts[*]} (threads=8, stride=4, hb=0, ll=true)"

  for ctx in "${contexts[@]}"; do
    local label="context_${ctx}"; ensure_dir "$ARTIFACT_DIR/$label"
    ATTEMPTED=$((ATTEMPTED + 1))
    info "Context=$ctx..."

    start_gpu_telemetry "$ARTIFACT_DIR/$label/gpu_telemetry.csv"
    start_cpu_telemetry "$ARTIFACT_DIR/$label/cpu_telemetry.csv"
    if ! start_backend_q4 "8" "" "$label"; then
      stop_gpu_telemetry; stop_cpu_telemetry; FAILED=$((FAILED+1)); continue
    fi
    if ! run_q4_benchmark "$label" "8" "" "$ctx" "0" "0" "true"; then
      warn "Benchmark/validation failed for $label"
    fi
    capture_metrics_q4 "$label" "$ARTIFACT_DIR/$label"
    docker logs "$BENCH_CONTAINER" 2>/dev/null > "$ARTIFACT_DIR/$label/startup.log" || true
    stop_gpu_telemetry; stop_cpu_telemetry; stop_backend

    # Create WAV for listening
    local wok=true
    while IFS= read -r -d '' pcm; do create_host_wav "$pcm" "${pcm%.pcm}.wav" || wok=false; done < <(find "$ARTIFACT_DIR/$label" -name '*.pcm' -print0 2>/dev/null || true)
    [[ "$wok" != true ]] && WAV_FAILURES=$((WAV_FAILURES+1))

    local rj="$ARTIFACT_DIR/$label/results.json"
    if [[ -f "$rj" ]]; then
      local rtf=$(python3 -c "import json; d=json.load(open('$rj')); s=d['summaries'][0]; print(s['avg_rtf'])" 2>/dev/null || echo "999")
      info "  ctx=$ctx: RTF=$rtf"
      SUCCESSFUL=$((SUCCESSFUL+1))
    else
      MISSING_RESULTS=$((MISSING_RESULTS+1)); FAILED=$((FAILED+1))
    fi
  done

  # Generate context comparison
  python3 "$REPO_ROOT/scripts/_generate_context_comparison.py" "$ARTIFACT_DIR" 2>/dev/null || warn "Context report failed"
}

generate_combined_report() { python3 "$REPO_ROOT/scripts/_generate_q4_combined_report.py" "$ARTIFACT_DIR" 2>/dev/null || warn "Report failed"; }

# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

# Validate-only mode (no Docker starts, no synthesis)
if [[ "$VALIDATE_ONLY" == true ]]; then
  echo "=== Validate-Only ==="
  echo "Branch: $(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD)"
  echo "Commit: $(git -C "$REPO_ROOT" rev-parse HEAD)"
  echo "Model: $MODEL_FILE"
  discover_mounts
  [[ -f "$HOST_MODELS/$MODEL_FILE" ]] && echo "Model exists: $(stat -c%s "$HOST_MODELS/$MODEL_FILE" | numfmt --to=iec)" && sha256sum "$HOST_MODELS/$MODEL_FILE" || echo "MISSING"
  discover_production_gpu; echo "Production GPU: $PRODUCTION_GPU"
  discover_production_voice; echo "Voice: ${EFFECTIVE_VOICE:-none} (container_dir=${CONTAINER_VOICE_DIR:-/voices}, host=${USER_VOICE_DIR:-${HOST_VOICES:-none}})"
  echo "Host voices: ${HOST_VOICES:-none}"
  build_cpu_topology /tmp/q4_topo_validate.json && python3 -c "import json; t=json.load(open('/tmp/q4_topo_validate.json')); print(f'P-cores: {len(t[\"p_cores\"])}, E-cores: {len(t[\"e_cores\"])}'); [print(f'  {k}: {v}') for k,v in t['affinity_sets'].items()]" || echo "Topology failed"
  echo ""
  echo "Blipping benchmark args:"; for cfg in "blip_ctx4_hb0:4:0" "blip_ctx64_hb0:64:0" "blip_ctx64_hb1:64:1"; do echo "  $cfg"; done
  exit 0
fi

# Dry-run
if [[ "$RUN_REAL" != true ]]; then
  echo "========================================"; echo " DRY RUN — Q4 Runtime Tuning"; echo "========================================"
  echo "Model: $MODEL_FILE | Stride: $STRIDE | Phase: $PHASE"; [[ -n "$USER_THREADS" ]] && echo "Threads: $USER_THREADS"
  echo ""; [[ "$PHASE" == "all" || "$PHASE" == "threads" ]] && echo "Thread sweep: 0,8,16,24,32"
  [[ "$PHASE" == "all" || "$PHASE" == "affinity" ]] && echo "Affinity: unrestricted, p_physical, p_all_threads, p_plus_e"
  [[ "$PHASE" == "all" || "$PHASE" == "context-screen" ]] && echo "Context screen: 4,8,12,16,24,32,48,64 (threads=8, stride=4, hb=0)"
  [[ "$PHASE" == "all" || "$PHASE" == "blipping" ]] && echo "Blipping: ctx4/hb0, ctx64/hb0, ctx64/hb1"
  echo ""; echo "--validate-only  : inspect env, no Docker"; echo "--smoke-test     : one short synthesis"; echo "--run-real       : full sweep"
  exit 0
fi

# Live execution
for cmd in docker curl python3 nvidia-smi git; do command -v "$cmd" &>/dev/null || { err "Missing: $cmd"; exit 1; }; done
discover_mounts
[[ ! -f "$HOST_MODELS/$MODEL_FILE" ]] && { err "Model not found: $HOST_MODELS/$MODEL_FILE"; exit 1; }
ok "Model: $MODEL_FILE ($(stat -c%s "$HOST_MODELS/$MODEL_FILE" | numfmt --to=iec))"
discover_production_gpu; select_gpu; discover_production_voice

TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)
ARTIFACT_DIR="${RESUME_ARTIFACT:-$ARTIFACT_BASE/$TIMESTAMP}"; ensure_dir "$ARTIFACT_DIR"
{ echo "phase=8E.1b"; echo "timestamp=$TIMESTAMP"; echo "model=$MODEL_FILE"; echo "stride=$STRIDE"
  echo "voice=${EFFECTIVE_VOICE:-none}"; echo "voice_dir=${CONTAINER_VOICE_DIR:-/voices}"
  echo "gpu_uuid=$GPU_UUID"; echo "production_gpu=$PRODUCTION_GPU"
  echo "git_commit=$(git -C "$REPO_ROOT" rev-parse HEAD)"; } > "$ARTIFACT_DIR/system_state.txt"
nvidia-smi > "$ARTIFACT_DIR/nvidia_smi_snapshot.txt" 2>/dev/null || true
nvidia-smi --query-gpu=uuid,clocks.max.sm,clocks.max.mem,power.limit --format=csv,noheader > "$ARTIFACT_DIR/stock_gpu_state.txt" 2>/dev/null || true
build_cpu_topology "$ARTIFACT_DIR/core_topology.json" || warn "Topology build had issues"

# Smoke test mode (fail non-zero on any error)
if [[ "$SMOKE_TEST" == true ]]; then
  SMOKE_FAILED=false
  info "Smoke test — one short synthesis..."
  ensure_dir "$ARTIFACT_DIR/smoke_test"
  start_backend_q4 "0" "" "smoke" || { err "Smoke backend failed"; exit 1; }
  if PYTHONPATH="$REPO_ROOT" python3 "$REPO_ROOT/scripts/benchmark_quantization.py" \
    --run-real --endpoint "127.0.0.1:$BENCH_PORT" --models "$HOST_MODELS/$MODEL_FILE" \
    --stride "$STRIDE" --codec-context 4 --warmup-runs 0 --measured-runs 1 \
    --output-dir "$ARTIFACT_DIR" --candidate-dir "smoke_test" \
    --text "Hello, this is a smoke test." --timeout 60 \
    ${EFFECTIVE_VOICE:+--voice "$EFFECTIVE_VOICE"} \
    ${CONTAINER_VOICE_DIR:+--voice-dir "$CONTAINER_VOICE_DIR"}; then
    ok "Smoke synthesis succeeded"
  else
    err "Smoke synthesis failed"; SMOKE_FAILED=true
  fi
  # Verify results
  srj="$ARTIFACT_DIR/smoke_test/results.json"
  if [[ -f "$srj" ]]; then
    measured=$(python3 -c "import json; d=json.load(open('$srj')); measured=[r for s in d['summaries'] for r in s['runs'] if r.get('run_type')=='measured' and r.get('status')=='success']; print(len(measured))" 2>/dev/null || echo 0)
    if [[ "$measured" -ge 1 ]]; then ok "$measured measured run(s) succeeded"; else err "No measured runs succeeded"; SMOKE_FAILED=true; fi
  else
    err "results.json missing"; SMOKE_FAILED=true
  fi
  # Verify PCM
  pcm_count=$(find "$ARTIFACT_DIR/smoke_test" -name '*.pcm' -size +0c 2>/dev/null | wc -l)
  if [[ "$pcm_count" -ge 1 ]]; then ok "$pcm_count non-empty PCM file(s)"; else err "No non-empty PCM files"; SMOKE_FAILED=true; fi
  # Create and verify WAV
  wav_ok=false
  find "$ARTIFACT_DIR/smoke_test" -name '*.pcm' -print0 2>/dev/null | while IFS= read -r -d '' pcm; do
    if create_host_wav "$pcm" "${pcm%.pcm}.wav"; then ok "Smoke WAV: ${pcm%.pcm}.wav"; wav_ok=true; fi
  done
  # Re-check outside subshell
  wav_count=$(find "$ARTIFACT_DIR/smoke_test" -name '*.wav' -size +0c 2>/dev/null | wc -l)
  if [[ "$wav_count" -ge 1 ]]; then ok "$wav_count valid WAV file(s)"; else err "No valid WAV files"; SMOKE_FAILED=true; fi
  stop_backend
  if [[ "$SMOKE_FAILED" == true ]]; then
    err "Smoke test FAILED"; exit 1
  fi
  ok "Smoke test passed"; exit 0
fi

# Full sweep
BEST_THREADS="${USER_THREADS:-8}"
[[ "$PHASE" == "all" || "$PHASE" == "threads" ]] && run_thread_sweep
[[ -f "$ARTIFACT_DIR/best_threads.txt" ]] && BEST_THREADS=$(cat "$ARTIFACT_DIR/best_threads.txt")
[[ "$PHASE" == "all" || "$PHASE" == "affinity" ]] && run_affinity_sweep "$BEST_THREADS"
[[ "$PHASE" == "all" || "$PHASE" == "context-screen" ]] && run_context_screen
[[ "$PHASE" == "all" || "$PHASE" == "blipping" ]] && run_blipping_diagnostic "$BEST_THREADS"
generate_combined_report

echo ""; info "=== ACCOUNTING ==="; info "Attempted: $ATTEMPTED | Success: $SUCCESSFUL | Failed: $FAILED"
info "Missing: $MISSING_RESULTS | WAV failures: $WAV_FAILURES"; info "Artifact: $ARTIFACT_DIR"
FINAL_EXIT=0; [[ $FAILED -gt 0 ]] && { err "Incomplete: $FAILED/$ATTEMPTED failed"; FINAL_EXIT=1; }
[[ $FINAL_EXIT -eq 0 ]] && info "Q4 tuning complete" || warn "Q4 tuning had failures"
exit $FINAL_EXIT

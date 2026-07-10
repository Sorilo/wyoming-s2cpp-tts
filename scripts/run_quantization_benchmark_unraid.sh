#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# Phase 8D.3: Unraid host-side controlled quantization benchmark orchestrator.
#
# One candidate per backend process — the s2.cpp server loads ONE GGUF at
# startup via the S2_MODEL environment variable.  HTTP requests cannot switch
# models mid-process.
#
# Default: dry-run (safe — no containers created or touched).
# Requires --run-real for live execution.
# ────────────────────────────────────────────────────────────────────────────

set -euo pipefail
shopt -s lastpipe

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
ALLOW_PRODUCTION_GPU=false
STRIDE=4
CODEC_CONTEXT=4
WARMUP_RUNS=1
MEASURED_RUNS=3
TIMEOUT=120

# Exact upstream download URLs
Q5_URL="https://huggingface.co/rodrigomt/s2-pro-gguf/resolve/main/s2-pro-q5_k_m.gguf?download=true"
Q4_URL="https://huggingface.co/rodrigomt/s2-pro-gguf/resolve/main/s2-pro-q4_k_m.gguf?download=true"

# Verified approximate sizes (GB)
declare -A MODEL_SIZES=(
  ["s2-pro-q6_k.gguf"]="4.53"
  ["s2-pro-q5_k_m.gguf"]="4.03"
  ["s2-pro-q4_k_m.gguf"]="3.57"
)

# ── Resolve repository root ────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ARTIFACT_BASE="$REPO_ROOT/verification_artifacts/quant_benchmark"

# ── Parse arguments ────────────────────────────────────────────────────────
RUN_REAL=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-real)             RUN_REAL=true; shift ;;
    --port)                 BENCH_PORT="$2"; shift 2 ;;
    --stride)               STRIDE="$2"; shift 2 ;;
    --gpu)                  GPU_UUID="$2"; shift 2 ;;
    --allow-production-gpu) ALLOW_PRODUCTION_GPU=true; shift ;;
    *)                      err "Unknown argument: $1"; exit 1 ;;
  esac
done

# Candidates: Q6 (baseline), Q5, Q4 — exact filenames and labels
CANDIDATE_FILES=("s2-pro-q6_k.gguf" "s2-pro-q5_k_m.gguf" "s2-pro-q4_k_m.gguf")
CANDIDATE_LABELS=("q6_k" "q5_k_m" "q4_k_m")
declare -A CANDIDATE_URLS=(
  ["s2-pro-q5_k_m.gguf"]="$Q5_URL"
  ["s2-pro-q4_k_m.gguf"]="$Q4_URL"
)
BENCHMARK_TEXT="The morning sun cast long shadows across the quiet neighborhood as residents began their daily routines. A gentle breeze carried the scent of fresh coffee from the corner cafe, where early customers sat reading newspapers and checking their phones. Children hurried past with backpacks slung over their shoulders, their laughter echoing off the brick buildings."

# ── Safe arithmetic (avoids bc dependency) ──────────────────────────────────
float_lt()  { python3 -c "import sys; sys.exit(0 if float('$1') < float('$2') else 1)"; }
float_add() { python3 -c "print(float('$1') + float('$2'))"; }

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
    exit 1
  fi
  info "Host model directory: $HOST_MODELS"
}

# ── Discover production GPU UUID from actual container ─────────────────────
discover_production_gpu() {
  PRODUCTION_GPU=$(docker inspect s2cpp-backend \
    --format '{{range .Config.Env}}{{if eq (printf "%.20s" .) "NVIDIA_VISIBLE_DEVICE"}}{{.}}{{end}}{{end}}' \
    2>/dev/null | sed 's/NVIDIA_VISIBLE_DEVICES=//' | xargs || echo '')
  if [[ -z "$PRODUCTION_GPU" ]]; then
    # Fallback: query nvidia-smi for GPU with active compute processes
    PRODUCTION_GPU=$(nvidia-smi --query-compute-apps=gpu_uuid --format=csv,noheader 2>/dev/null | head -1 | xargs || echo '')
  fi
  if [[ -z "$PRODUCTION_GPU" ]]; then
    warn "Could not discover production GPU; default exclusion disabled"
    PRODUCTION_GPU="none"
  fi
  info "Production GPU: $PRODUCTION_GPU"
}

# ── Discover idle GPU ──────────────────────────────────────────────────────
discover_idle_gpu() {
  # Validate user-supplied GPU UUID
  if [[ -n "$GPU_UUID" ]]; then
    if nvidia-smi --query-gpu=uuid --format=csv,noheader 2>/dev/null | grep -qF "$GPU_UUID"; then
      local util mem
      read -r util mem <<< "$(nvidia-smi --query-gpu=uuid,utilization.gpu,memory.used \
        --format=csv,noheader,nounits 2>/dev/null | grep "^$GPU_UUID" | cut -d, -f2- | xargs)"
      info "User-supplied GPU: $GPU_UUID (util=$util%, mem_used=$mem MiB)"
      if [[ "$GPU_UUID" == "$PRODUCTION_GPU" ]] && [[ "$ALLOW_PRODUCTION_GPU" != true ]]; then
        err "User-supplied GPU is the production GPU. Add --allow-production-gpu to override."
        exit 1
      fi
      return
    else
      err "User-supplied GPU UUID not found: $GPU_UUID"
      nvidia-smi --query-gpu=uuid,name --format=csv,noheader >&2
      exit 1
    fi
  fi

  # Auto-select: exclude production GPU, prefer idle
  local all_gpus
  all_gpus=$(nvidia-smi --query-gpu=uuid,utilization.gpu,memory.used --format=csv,noheader,nounits 2>/dev/null)
  if [[ -z "$all_gpus" ]]; then
    err "nvidia-smi returned no GPUs"
    exit 1
  fi

  while IFS=, read -r uuid util mem; do
    uuid=$(echo "$uuid" | xargs); util=$(echo "$util" | xargs); mem=$(echo "$mem" | xargs)
    # Skip production GPU (always, unless overridden)
    if [[ "$uuid" == "$PRODUCTION_GPU" ]]; then
      if [[ "$ALLOW_PRODUCTION_GPU" == true ]]; then
        GPU_UUID="$uuid"
        info "Using production GPU $uuid (allowed via --allow-production-gpu): util=$util% mem=$mem MiB"
        return
      fi
      info "Skipping production GPU: $uuid (util=$util%)"
      continue
    fi
    # Idle check: utilization < 10% and memory < 500 MiB
    if float_lt "$util" "10" && float_lt "$mem" "500"; then
      GPU_UUID="$uuid"
      info "Selected idle GPU: $uuid (util=$util%, mem_used=$mem MiB)"
      return
    else
      info "GPU $uuid not idle enough: util=$util% mem=$mem MiB"
    fi
  done <<< "$all_gpus"

  err "No suitably idle GPU found."
  echo "$all_gpus" >&2
  err "Use --gpu UUID or --allow-production-gpu."
  exit 1
}

# ── Calculate required storage (no bc) ─────────────────────────────────────
check_storage() {
  local needed=0
  for f in "${CANDIDATE_FILES[@]}"; do
    if [[ ! -f "$HOST_MODELS/$f" ]]; then
      needed=$(float_add "$needed" "${MODEL_SIZES[$f]:-4.0}")
    fi
  done
  # Add 2 GB headroom
  needed=$(float_add "$needed" "2")
  local available
  available=$(df -BG "$HOST_MODELS" | awk 'NR==2 {print $4}' | sed 's/G//')
  info "Storage needed (missing models + headroom): ~${needed} GB"
  info "Storage available: ${available} GB"
  if float_lt "$available" "$needed"; then
    err "Insufficient storage ($available GB < $needed GB)"
    exit 1
  fi
  ok "Storage sufficient"
}

# ── Download missing models ────────────────────────────────────────────────
download_if_missing() {
  local file="$1" url="$2"
  if [[ -f "$HOST_MODELS/$file" ]]; then
    ok "$file already present"
    return 0
  fi
  if [[ -z "$url" ]]; then
    warn "No download URL for $file — must be obtained manually"
    return 1
  fi
  info "Downloading $file (~${MODEL_SIZES[$file]:-?} GB)..."
  curl --continue-at - --fail --location --retry 3 \
    -o "$HOST_MODELS/${file}.part" "$url" || {
    err "Download failed for $file"
    rm -f "$HOST_MODELS/${file}.part"
    return 1
  }
  mv "$HOST_MODELS/${file}.part" "$HOST_MODELS/$file"
  ok "Downloaded: $file ($(stat -c%s "$HOST_MODELS/$file" | numfmt --to=iec) bytes)"
  sha256sum "$HOST_MODELS/$file"
  return 0
}

# ── Wait for full backend readiness ────────────────────────────────────────
wait_backend_ready() {
  local model_file="$1"
  local elapsed=0 interval=2
  local launched=false gpu_layers_ok=false codec_ok=false http_ok=false

  info "Waiting for backend readiness (bounded ${TIMEOUT}s)..."

  while [[ $elapsed -lt $TIMEOUT ]]; do
    local startup_log
    startup_log=$(docker logs "$BENCH_CONTAINER" 2>/dev/null || true)

    # 1. Check for fatal errors
    if echo "$startup_log" | grep -qi "ERROR"; then
      err "Backend startup error detected:"
      echo "$startup_log" | grep -i "ERROR" >&2
      return 1
    fi
    # Check container still alive
    if ! docker inspect "$BENCH_CONTAINER" --format '{{.State.Running}}' 2>/dev/null | grep -q true; then
      err "Backend container exited during startup"
      docker logs "$BENCH_CONTAINER" 2>/dev/null | tail -30 >&2
      return 1
    fi

    # 2. Confirm expected Launching line
    if [[ "$launched" != true ]]; then
      if echo "$startup_log" | grep -q "Launching: s2 --model /models/$model_file"; then
        launched=true
        ok "Model confirmed: /models/$model_file"
      fi
    fi

    # 3. Check GPU layers loaded
    if [[ "$launched" == true ]] && [[ "$gpu_layers_ok" != true ]]; then
      if echo "$startup_log" | grep -qE 'gpu_layers|all.*layers.*loaded|offload.*36/36|layers.*offload'; then
        gpu_layers_ok=true
        ok "GPU layers loaded"
      fi
    fi

    # 4. Check codec backend
    if [[ "$launched" == true ]] && [[ "$codec_ok" != true ]]; then
      if echo "$startup_log" | grep -qiE 'codec.*backend|codec.*init|codec.*load|codec.*ready|codec.*cuda'; then
        codec_ok=true
        ok "Codec backend ready"
      fi
    fi

    # 5. Poll HTTP endpoint
    local http_code
    http_code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 2 \
      "http://127.0.0.1:$BENCH_PORT/" 2>/dev/null || echo "000")
    if [[ "$http_code" != "000" ]]; then
      http_ok=true
    fi

    # All conditions met
    if [[ "$launched" == true ]] && [[ "$http_ok" == true ]]; then
      ok "Backend ready: model=$model_file, http_code=$http_code"
      return 0
    fi

    sleep "$interval"
    elapsed=$((elapsed + interval))
  done

  err "Backend readiness timeout after ${TIMEOUT}s"
  docker logs "$BENCH_CONTAINER" 2>/dev/null | tail -20 >&2
  return 1
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

  wait_backend_ready "$model_file"
}

# ── Stop and remove temporary container ────────────────────────────────────
stop_backend() {
  docker rm -f "$BENCH_CONTAINER" 2>/dev/null || true
}

# ── Per-run metric capture with timestamp correlation ──────────────────────
capture_run_metrics() {
  local label="$1" since_ts="$2" output_file="$3"
  # Poll with bounded timeout for completed [Metrics] Streaming line
  local max_wait=20 elapsed=0 interval=1
  while [[ $elapsed -lt $max_wait ]]; do
    docker logs "$BENCH_CONTAINER" --since "$since_ts" 2>/dev/null > "$output_file" || true
    if grep -q '\[Metrics\] Streaming' "$output_file" 2>/dev/null; then
      break
    fi
    sleep "$interval"
    elapsed=$((elapsed + interval))
  done
  grep '\[Metrics\] Streaming' "$output_file" 2>/dev/null > "${output_file}.metrics" || true
  if [[ ! -s "${output_file}.metrics" ]]; then
    warn "No [Metrics] Streaming line found for $label after ${elapsed}s"
    return 1
  fi
  return 0
}

# ── Parse metrics line into JSON object ────────────────────────────────────
parse_metrics_json() {
  local line="$1"
  python3 -c "
import re, json
line = '''$line'''
fields = ['frames','audio_s','ref_encode','kv_init','stride','holdback',
          'decode_context','generate','stream_decode','stream_batches',
          'ar_only','total','total_rtf','max_rss']
result = {}
for f in fields:
    m = re.search(rf'\b{f}=([0-9.]+)', line)
    if m:
        try:
            result[f] = float(m.group(1))
        except ValueError:
            result[f] = m.group(1)
print(json.dumps(result))
" 2>/dev/null || echo "null"
}

# ── Capture container state ────────────────────────────────────────────────
capture_container_state() {
  local dir="$1" label="$2" model_file="$3"
  docker inspect "$BENCH_CONTAINER" > "$dir/container_inspect.json" 2>/dev/null || true
  docker logs "$BENCH_CONTAINER" > "$dir/startup.log" 2>/dev/null || true
  docker inspect "$BENCH_CONTAINER" --format '{{.Config.Image}}' > "$dir/backend_image.txt" 2>/dev/null || true
  docker inspect "$BENCH_CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' > "$dir/backend_env.txt" 2>/dev/null || true
  echo "$model_file" > "$dir/model_filename.txt"
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
  GPU_TELEM_PID=""
}

# ── Auto-create WAV from PCM ───────────────────────────────────────────────
create_wav() {
  local pcm_path="$1" wav_path="$2"
  # Primary: Hermes-Suite ffmpeg via docker exec
  if docker exec Hermes-Suite /usr/bin/ffmpeg -y -f s16le -ar 44100 -ac 1 \
    -i "$pcm_path" "$wav_path" 2>/dev/null; then
    if [[ -s "$wav_path" ]]; then
      ok "WAV created (ffmpeg): $wav_path ($(stat -c%s "$wav_path") bytes)"
      return 0
    fi
  fi
  # Fallback: Python wave module
  warn "ffmpeg failed, trying Python wave fallback..."
  python3 -c "
import wave
pcm_path = '$pcm_path'
wav_path = '$wav_path'
with open(pcm_path, 'rb') as pf:
    pcm = pf.read()
with wave.open(wav_path, 'wb') as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(44100)
    wf.writeframes(pcm)
" 2>/dev/null
  if [[ -s "$wav_path" ]]; then
    ok "WAV created (wave fallback): $wav_path ($(stat -c%s "$wav_path") bytes)"
    return 0
  fi
  err "WAV creation failed for $pcm_path"
  return 1
}

# ── Discover PCM files from results.json ───────────────────────────────────
discover_pcm_paths() {
  local results_json="$1"
  python3 -c "
import json, sys
with open('$results_json') as f:
    data = json.load(f)
for s in data.get('summaries', []):
    for run in s.get('runs', []):
        p = run.get('pcm_path', '')
        if p and run.get('status') == 'success':
            print(p)
" 2>/dev/null
}

# ── Generate combined comparison table ─────────────────────────────────────
generate_combined_summary() {
  local artifact_dir="$1" summary_md="$2"
  python3 -c "
import json, sys, os
from pathlib import Path

artifacts = Path('$artifact_dir')
results = {}

# Collect per-candidate results
for label in ['q6_k', 'q5_k_m', 'q4_k_m']:
    rj = artifacts / label / 'results.json'
    if not rj.exists():
        continue
    with open(rj) as f:
        data = json.load(f)
    runs = []
    for s in data.get('summaries', []):
        for run in s.get('runs', []):
            if run.get('status') == 'success' and run.get('run_type') == 'measured':
                runs.append(run)
    if not runs:
        continue

    rtfs = [r['rtf'] for r in runs if r.get('rtf') is not None]
    firsts = [r['time_to_first_pcm_ms'] for r in runs]
    totals = [r['total_wall_ms'] for r in runs]
    rtfs.sort()
    firsts.sort()
    totals.sort()

    def median(lst):
        n = len(lst)
        if n == 0: return None
        m = n // 2
        return (lst[m] + lst[~m]) / 2 if n % 2 == 0 else lst[m]

    # Backend metrics
    bm = runs[0].get('backend_metrics', {}) if runs else {}
    results[label] = {
        'success': len(runs),
        'rtf_mean': sum(rtfs)/len(rtfs) if rtfs else None,
        'rtf_median': median(rtfs),
        'rtf_min': rtfs[0] if rtfs else None,
        'rtf_max': rtfs[-1] if rtfs else None,
        'first_pcm_mean': sum(firsts)/len(firsts) if firsts else None,
        'first_pcm_median': median(firsts),
        'total_wall_mean': sum(totals)/len(totals) if totals else None,
        'generate_mean': bm.get('generate'),
        'stream_decode_mean': bm.get('stream_decode'),
        'ar_only_mean': bm.get('ar_only'),
        'kv_init': bm.get('kv_init'),
        'max_rss': bm.get('max_rss'),
        'model_sha': (artifacts / label / 'model_sha256.txt').read_text().strip() if (artifacts / label / 'model_sha256.txt').exists() else '',
        'model_size': (artifacts / label / 'model_size.txt').read_text().strip() if (artifacts / label / 'model_size.txt').exists() else '',
    }

# Write combined JSON
with open(artifacts / 'combined_results.json', 'w') as f:
    json.dump(results, f, indent=2)

# Write Markdown summary
lines = [
    '# Quantization Benchmark Results',
    '',
    '**Status: Provisional — human listening required before model selection.**',
    '',
    '## Comparison Table',
    '',
    '| Quant | Success | RTF Mean | RTF Med | RTF Min | RTF Max | 1st PCM Mean (ms) | Total Mean (ms) | Gen (s) | SD (s) | AR (s) | KV (s) | VRAM (MiB) |',
    '|-------|---------|----------|---------|---------|---------|--------------------|-----------------|---------|--------|--------|--------|-------------|',
]

for label in ['q6_k', 'q5_k_m', 'q4_k_m']:
    r = results.get(label)
    if not r:
        lines.append(f'| {label} | — | — | — | — | — | — | — | — | — | — | — | — |')
        continue
    def f(v, fmt='.2f'):
        return f'{v:{fmt}}' if v is not None else '—'
    lines.append(
        f'| {label} | {r[\"success\"]}/3 | {f(r[\"rtf_mean\"], \".3f\")} | {f(r[\"rtf_median\"], \".3f\")} | '
        f'{f(r[\"rtf_min\"], \".3f\")} | {f(r[\"rtf_max\"], \".3f\")} | {f(r[\"first_pcm_mean\"], \".0f\")} | '
        f'{f(r[\"total_wall_mean\"], \".0f\")} | {f(r[\"generate_mean\"])} | {f(r[\"stream_decode_mean\"])} | '
        f'{f(r[\"ar_only_mean\"])} | {f(r[\"kv_init\"])} | {f(r[\"max_rss\"], \".0f\")} |'
    )

lines += ['', '## Recommendation', '',
          '⚠️ **PROVISIONAL**: Based on RTF and latency metrics only.',
          '**Human listening is REQUIRED before model selection.**', '',
          '### Decision Rule', '',
          '- RTF ≤ 0.95: safe real-time with margin ✅',
          '- 0.95 < RTF < 1.0: real-time achievable, tight margin ⚠️',
          '- RTF ≥ 1.0: slower than real-time ❌',
          '',
          '### Model SHA-256', '']

for label in ['q6_k', 'q5_k_m', 'q4_k_m']:
    r = results.get(label)
    if r and r['model_sha']:
        lines.append(f'- **{label}**: `{r[\"model_sha\"]}`')

with open('$summary_md', 'w') as f:
    f.write('\\n'.join(lines))
print('combined summary written')
"
}

# ── Cleanup trap ───────────────────────────────────────────────────────────
FINAL_EXIT_CODE=0
cleanup() {
  stop_gpu_telemetry
  docker rm -f "$BENCH_CONTAINER" 2>/dev/null || true
  if [[ -n "${ARTIFACT_DIR:-}" ]]; then
    echo "cleanup_status=ok" > "$ARTIFACT_DIR/cleanup_status.txt" 2>/dev/null || true
    echo "containers_removed=true" >> "$ARTIFACT_DIR/cleanup_status.txt" 2>/dev/null || true
    echo "exit_code=$FINAL_EXIT_CODE" >> "$ARTIFACT_DIR/cleanup_status.txt" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

check_prereqs
discover_model_mount

# ── Dry-run mode ───────────────────────────────────────────────────────────
if [[ "$RUN_REAL" != true ]]; then
  echo "========================================"
  echo " DRY RUN — no containers will be created"
  echo "========================================"
  echo ""
  echo "Backend image: $BACKEND_IMAGE"
  echo "Host models dir: $HOST_MODELS"
  echo "Stride: $STRIDE"
  echo ""
  echo "Candidate models:"
  for i in "${!CANDIDATE_FILES[@]}"; do
    f="${CANDIDATE_FILES[$i]}"
    label="${CANDIDATE_LABELS[$i]}"
    if [[ -f "$HOST_MODELS/$f" ]]; then
      sha=$(sha256sum "$HOST_MODELS/$f" 2>/dev/null | awk '{print $1}' || echo "unknown")
      echo "  ✓ $label ($f) — EXISTS ($(stat -c%s "$HOST_MODELS/$f" | numfmt --to=iec))"
      echo "    SHA-256: $sha"
    else
      url="${CANDIDATE_URLS[$f]:-<manual download>}"
      echo "  ✗ $label ($f) — MISSING (~${MODEL_SIZES[$f]:-?} GB)"
      echo "    Download: curl --continue-at - --fail --location --retry 3 -o \"$HOST_MODELS/${f}.part\" \"$url\""
    fi
  done
  echo ""
  echo "Per-candidate sequence (one fresh backend container per candidate):"
  echo "  1. docker run ... S2_MODEL=/models/<file>"
  echo "  2. Wait for Launching + GPU layers + codec + HTTP ready"
  echo "  3. benchmark_quantization.py --run-real --models <host_path>"
  echo "  4. Capture per-run metrics, container state, GPU telemetry"
  echo "  5. Auto-create WAV via Hermes-Suite ffmpeg"
  echo "  6. docker rm -f $BENCH_CONTAINER"
  echo ""
  echo "Add --run-real to execute."
  exit 0
fi

# ── Live execution ─────────────────────────────────────────────────────────
discover_production_gpu
discover_idle_gpu
check_storage

# Download missing models
info "Checking model availability..."
DOWNLOAD_FAILED=false
for f in "${CANDIDATE_FILES[@]}"; do
  if [[ ! -f "$HOST_MODELS/$f" ]]; then
    url="${CANDIDATE_URLS[$f]:-}"
    if ! download_if_missing "$f" "$url"; then
      DOWNLOAD_FAILED=true
    fi
  fi
done
if [[ "$DOWNLOAD_FAILED" == true ]]; then
  err "One or more models could not be downloaded. Cannot proceed."
  FINAL_EXIT_CODE=1
  exit 1
fi

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

# Model SHA-256 manifest
for f in "${CANDIDATE_FILES[@]}"; do
  if [[ -f "$HOST_MODELS/$f" ]]; then
    sha256sum "$HOST_MODELS/$f" >> "$ARTIFACT_DIR/model_sha256.txt"
  fi
done

# Copy listening checklist from benchmark doc
if grep -q 'Listening Checklist' "$REPO_ROOT/docs/STREAMING_STRIDE_AND_QUANT_BENCHMARKS.md" 2>/dev/null; then
  sed -n '/^## .*Listening/,/^## /p' "$REPO_ROOT/docs/STREAMING_STRIDE_AND_QUANT_BENCHMARKS.md" \
    > "$ARTIFACT_DIR/listening_checklist.md" 2>/dev/null || true
else
  echo "# Listening Checklist" > "$ARTIFACT_DIR/listening_checklist.md"
  echo "- Clicks/pops, missing syllables, word stretching, robotic artifacts, voice consistency, prosody, pauses, clipped endings, overall preference" >> "$ARTIFACT_DIR/listening_checklist.md"
fi

FAILED_CANDIDATES=0
COMPLETED_CANDIDATES=0

for i in "${!CANDIDATE_FILES[@]}"; do
  MODEL_FILE="${CANDIDATE_FILES[$i]}"
  LABEL="${CANDIDATE_LABELS[$i]}"

  echo ""
  echo "========================================"
  echo " Candidate $((i+1))/3: $LABEL ($MODEL_FILE)"
  echo "========================================"

  CANDIDATE_DIR="$ARTIFACT_DIR/$LABEL"
  mkdir -p "$CANDIDATE_DIR"

  # GPU telemetry for this candidate
  start_gpu_telemetry "$CANDIDATE_DIR/gpu_telemetry.csv"

  # Start backend
  if ! start_backend "$MODEL_FILE" "$LABEL"; then
    err "Failed to start backend for $LABEL"
    stop_gpu_telemetry
    FAILED_CANDIDATES=$((FAILED_CANDIDATES + 1))
    continue
  fi

  # Capture container state
  capture_container_state "$CANDIDATE_DIR" "$LABEL" "$MODEL_FILE"

  # Run benchmark with per-run metric capture
  info "Running benchmark for $LABEL..."

  # Record start timestamp for log correlation
  BENCH_START_TS=$(date -u +%Y-%m-%dT%H:%M:%S)

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
    || {
      warn "Benchmark for $LABEL exited non-zero"
      FAILED_CANDIDATES=$((FAILED_CANDIDATES + 1))
    }

  # Capture backend metrics (correlated via --since timestamp)
  capture_run_metrics "$LABEL" "$BENCH_START_TS" "$CANDIDATE_DIR/backend_metrics.log" || {
    warn "Missing metrics for $LABEL"
  }

  # Inject per-run metrics into results.json
  if [[ -f "$CANDIDATE_DIR/results.json" ]] && [[ -s "$CANDIDATE_DIR/backend_metrics.log.metrics" ]]; then
    python3 -c "
import json
with open('$CANDIDATE_DIR/results.json') as f:
    data = json.load(f)
lines = open('$CANDIDATE_DIR/backend_metrics.log.metrics').read().strip().split('\n')
# Correlate: last N completed [Metrics] Streaming lines (one per run)
metrics_lines = [l for l in lines if 'Streaming' in l]
run_idx = 0
for s in data.get('summaries', []):
    for run in s.get('runs', []):
        if run.get('status') == 'success' and run_idx < len(metrics_lines):
            import re
            mline = metrics_lines[run_idx]
            fields = ['frames','audio_s','ref_encode','kv_init','stride','holdback',
                      'decode_context','generate','stream_decode','stream_batches',
                      'ar_only','total','total_rtf','max_rss']
            result = {}
            for fld in fields:
                m = re.search(r'\b' + fld + r'=([0-9.]+)', mline)
                if m:
                    try: result[fld] = float(m.group(1))
                    except: result[fld] = m.group(1)
            run['backend_metrics'] = result
            run_idx += 1
with open('$CANDIDATE_DIR/results.json', 'w') as f:
    json.dump(data, f, indent=2)
print(f'  Injected {run_idx} per-run metrics into results.json')
" 2>/dev/null || warn "Metric injection failed for $LABEL"
  fi

  # Stop telemetry
  stop_gpu_telemetry

  # Auto-create WAV from first measured run PCM
  PCM_FILES=$(discover_pcm_paths "$CANDIDATE_DIR/results.json" 2>/dev/null || true)
  WAV_OK=true
  if [[ -n "$PCM_FILES" ]]; then
    # Convert first PCM file
    FIRST_PCM=$(echo "$PCM_FILES" | head -1)
    # Map host path to Hermes-Suite container path
    if [[ "$FIRST_PCM" == /mnt/user/* ]]; then
      HERMES_PCM="${FIRST_PCM/\/mnt\/user\/appdata\/hermes-agent\/webui-workspace\//\/workspace\/}"
    else
      HERMES_PCM="$FIRST_PCM"
    fi
    WAV_PATH="${FIRST_PCM%.pcm}.wav"
    HERMES_WAV="${WAV_PATH/\/mnt\/user\/appdata\/hermes-agent\/webui-workspace\//\/workspace\/}"
    if ! create_wav "$HERMES_PCM" "$HERMES_WAV"; then
      WAV_OK=false
    fi
  else
    warn "No PCM files found for $LABEL — cannot create WAV"
    WAV_OK=false
  fi

  if [[ "$WAV_OK" != true ]]; then
    FAILED_CANDIDATES=$((FAILED_CANDIDATES + 1))
  fi

  # Stop backend
  stop_backend

  COMPLETED_CANDIDATES=$((COMPLETED_CANDIDATES + 1))
  info "Candidate $LABEL complete ($COMPLETED_CANDIDATES/3)"
done

# ── Validation: all three candidates required ──────────────────────────────
if [[ $COMPLETED_CANDIDATES -lt 3 ]]; then
  err "Only $COMPLETED_CANDIDATES/3 candidates completed"
  FINAL_EXIT_CODE=1
fi
if [[ $FAILED_CANDIDATES -gt 0 ]]; then
  err "$FAILED_CANDIDATES candidate(s) had failures — comparison incomplete"
  FINAL_EXIT_CODE=1
fi

# ── Combined summary ────────────────────────────────────────────────────────
generate_combined_summary "$ARTIFACT_DIR" "$ARTIFACT_DIR/summary.md"

echo ""
info "========================================"
info " BENCHMARK COMPLETE"
info "========================================"
info "Artifact directory: $ARTIFACT_DIR"
info "Candidates completed: $COMPLETED_CANDIDATES/3"
info "Candidates failed: $FAILED_CANDIDATES"
info ""
info "Artifact structure:"
ls -la "$ARTIFACT_DIR/" 2>/dev/null
for label in "${CANDIDATE_LABELS[@]}"; do
  if [[ -d "$ARTIFACT_DIR/$label" ]]; then
    pcm_count=$(find "$ARTIFACT_DIR/$label" -name '*.pcm' 2>/dev/null | wc -l)
    wav_count=$(find "$ARTIFACT_DIR/$label" -name '*.wav' 2>/dev/null | wc -l)
    info "  $label/: $pcm_count PCM, $wav_count WAV, $(ls "$ARTIFACT_DIR/$label/" 2>/dev/null | wc -l) total files"
  fi
done
info ""
if [[ $FINAL_EXIT_CODE -ne 0 ]]; then
  err "Benchmark completed with errors. Review logs above."
else
  info "⚠️  Human listening of WAV files is REQUIRED before model selection."
  info "   DO NOT change the production model without listening."
fi
info ""

exit $FINAL_EXIT_CODE

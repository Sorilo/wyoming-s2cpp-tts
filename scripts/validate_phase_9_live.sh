#!/bin/bash
# Phase 9 headless live validation — run from Unraid terminal
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
pass() { echo -e "${GREEN}PASS${NC} $*"; }
fail() { echo -e "${RED}FAIL${NC} $*"; }
info() { echo -e "${YELLOW}INFO${NC} $*"; }

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TIMESTAMP="$(date -u +%Y%m%d_%H%M%S)"
ARTIFACT_DIR="$REPO_DIR/verification_artifacts/phase_9_live_smoke/$TIMESTAMP"
mkdir -p "$ARTIFACT_DIR"

TEST_IMAGE="${PHASE9_TEST_IMAGE:-ghcr.io/sorilo/wyoming-s2cpp-tts:sha-5355048}"
EXPECTED_DIGEST="${PHASE9_EXPECTED_DIGEST:-}"
SHADOW_NAME="wyoming-s2cpp-tts-phase9-smoke-${TIMESTAMP}"
CLIENT_NAME="wyoming-s2cpp-tts-phase9-client-${TIMESTAMP}"
SHADOW_PORT="10201"
PROD_NAME="wyoming-s2cpp-tts"
BACKEND_NAME="s2cpp-backend"
TEST_BACKEND_NAME="s2cpp-backend-phase9-smoke-${TIMESTAMP}"
PHASE9_TEST_GPU_UUID="${PHASE9_TEST_GPU_UUID:-}"
BACKEND_IMAGE="${PHASE9_BACKEND_IMAGE:-}"
BACKEND_DIGEST="${PHASE9_BACKEND_DIGEST:-}"
CREATED_CONTAINERS=""
USE_HELPER_CONTAINER="false"
export PHASE9_FAILURE_TYPE="infrastructure"
export PHASE9_FAILURE_REASON="harness prerequisite failed; see console output"

cleanup() {
    local exit_code=$?
    trap - EXIT INT TERM
    [ -n "${LOG_FOLLOWER_PID:-}" ] && kill "$LOG_FOLLOWER_PID" 2>/dev/null || true
    [ -n "${LOG_FOLLOWER_PID:-}" ] && wait "$LOG_FOLLOWER_PID" 2>/dev/null || true
    # Preserve evidence before exact removal, even on setup/client failure.
    for pair in "$PROD_NAME production-after-wrapper.json" "$BACKEND_NAME production-after-backend.json"; do
        set -- $pair; snapshot "$1" "$ARTIFACT_DIR/$2" 2>/dev/null || printf '{"error":"inspect unavailable"}\n' > "$ARTIFACT_DIR/$2"
    done
    if docker inspect "$TEST_BACKEND_NAME" >/dev/null 2>&1; then
        docker inspect "$TEST_BACKEND_NAME" > "$ARTIFACT_DIR/test-backend-inspect-final.json" 2>&1 || true
        docker logs "$TEST_BACKEND_NAME" > "$ARTIFACT_DIR/test-backend-logs.txt" 2>&1 || true
    else
        printf '{"state":"not_created"}\n' > "$ARTIFACT_DIR/test-backend-inspect-final.json"
        printf 'test backend was not created\n' > "$ARTIFACT_DIR/test-backend-logs.txt"
    fi
    python3 - "$ARTIFACT_DIR" <<'PY_FINAL'
import json, os, sys
p=sys.argv[1]
def load(name):
 try: return json.load(open(os.path.join(p,name)))
 except Exception as e: return {"error":f"{type(e).__name__}: {e}"}
for name in ("wrapper","backend"):
 b,a=load(f"production-before-{name}.json"),load(f"production-after-{name}.json")
 out={"container":name,"unchanged":bool(b.get("id") and b.get("id")==a.get("id") and a.get("running")),"before":b,"after":a}
 json.dump(out,open(os.path.join(p,f"production-comparison-{name}.json"),"w"),indent=2)
prod=load("production-before-backend.json"); test=load("test-backend-inspect-final.json")
test_id=test[0].get("Id") if isinstance(test,list) and test else None
json.dump({"production_id":prod.get("id"),"test_backend_id":test_id,"distinct":bool(test_id and test_id != prod.get("id"))},open(os.path.join(p,"production-comparison-test-backend.json"),"w"),indent=2)
rp=os.path.join(p,"results.json")
if not os.path.exists(rp):
 json.dump({"classification":"FAIL","failure_type":os.environ.get("PHASE9_FAILURE_TYPE","runtime"),"reason":os.environ.get("PHASE9_FAILURE_REASON","harness exited before client results; see console.log"),"tests":{}},open(rp,"w"),indent=2)
r=load("results.json")
with open(os.path.join(p,"summary.md"),"w") as f:
 f.write("# Phase 9 Live Validation Report\n")
 f.write(f"- **Classification:** {r.get('classification','FAIL')}\n- **Failure type:** {r.get('failure_type','none')}\n- **Reason:** {r.get('reason','none')}\n")
PY_FINAL
    local status="ok" c
    # Reverse creation order; remove only exact names recorded by this run.
    for c in $CLIENT_NAME $SHADOW_NAME $TEST_BACKEND_NAME; do
        case " $CREATED_CONTAINERS " in *" $c "*) docker rm -f "$c" >/dev/null 2>&1 || status="cleanup_failure";; esac
    done
    for c in $CREATED_CONTAINERS; do docker inspect "$c" >/dev/null 2>&1 && status="cleanup_failure" || true; done
    echo "cleanup_status: $status" >> "$ARTIFACT_DIR/console.log" 2>/dev/null || true
    [ "$status" = ok ] || { export PHASE9_FAILURE_TYPE="cleanup"; exit_code=1; }
    exit "$exit_code"
}
trap cleanup EXIT INT TERM

if ! [[ "$EXPECTED_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    fail "PHASE9_EXPECTED_DIGEST must be sha256 followed by 64 lowercase hex characters"
    exit 1
fi
[ -n "$PHASE9_TEST_GPU_UUID" ] || { fail "PHASE9_TEST_GPU_UUID is required"; exit 1; }
[ -n "$BACKEND_IMAGE" ] || { fail "PHASE9_BACKEND_IMAGE is required"; exit 1; }
[[ "$BACKEND_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { fail "PHASE9_BACKEND_DIGEST must be an explicit sha256 digest"; exit 1; }

echo "=== Phase 9 Live Validation ==="
echo "Artifacts: $ARTIFACT_DIR"
echo "Started: $(date -u -Iseconds)"

# ── 1. Docker access ────────────────────────────────────────────
info "Step 1: Docker access"
docker version > /dev/null 2>&1 || { fail "Docker daemon unreachable"; exit 1; }
pass "Docker accessible"

# ── 2. Production identity ──────────────────────────────────────
info "Step 2: Production identity"
snapshot() {
    local c="$1" out="$2"
    python3 -c "
import json, subprocess
data = json.loads(subprocess.check_output(['docker','inspect', '$c']))[0]
networks = list((data.get('NetworkSettings',{}).get('Networks',{}) or {}).keys())
out = {
    'id': data['Id'], 'image_ref': data['Config']['Image'],
    'image_id': data['Image'], 'created': data['Created'],
    'started': data['State']['StartedAt'],
    'restart_count': data['RestartCount'],
    'running': data['State']['Running'],
    'networks': networks,
}
with open('$out','w') as f: json.dump(out, f, indent=2)
" 2>/dev/null
}
for c in "$PROD_NAME" "$BACKEND_NAME"; do
    docker inspect "$c" > /dev/null 2>&1 || { fail "Cannot inspect $c"; exit 1; }
done
snapshot "$PROD_NAME" "$ARTIFACT_DIR/production-before-wrapper.json"
snapshot "$BACKEND_NAME" "$ARTIFACT_DIR/production-before-backend.json"
pass "Production identity recorded"

# ── 3. Shared network ───────────────────────────────────────────
info "Step 3: Finding shared network"
WRAPPER_NETS=$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$PROD_NAME")
BACKEND_NETS=$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$BACKEND_NAME")
INTERSECTION=""
for net in $WRAPPER_NETS; do
    for bnet in $BACKEND_NETS; do
        [ "$net" = "$bnet" ] && INTERSECTION="$INTERSECTION $net"
    done
done
INTERSECTION="${INTERSECTION# }"
[ -z "$INTERSECTION" ] && { fail "No shared network"; exit 1; }
SHARED_NET=""
NET_COUNT=$(echo "$INTERSECTION" | wc -w)
if [ "$NET_COUNT" -eq 1 ]; then SHARED_NET="$INTERSECTION"
elif [ -n "${PHASE9_SMOKE_NETWORK:-}" ]; then
    for net in $INTERSECTION; do
        [ "$net" = "$PHASE9_SMOKE_NETWORK" ] && SHARED_NET="$net" && break
    done
    [ -z "$SHARED_NET" ] && { fail "PHASE9_SMOKE_NETWORK=$PHASE9_SMOKE_NETWORK not in $INTERSECTION"; exit 1; }
else fail "Multiple networks: $INTERSECTION. Set PHASE9_SMOKE_NETWORK."; exit 1; fi
pass "Network: $SHARED_NET"

# ── 4. Isolated backend config/start ────────────────────────────
info "Step 4: Starting isolated backend on benchmark GPU"
export PHASE9_FAILURE_TYPE="backend"
export PHASE9_FAILURE_REASON="isolated backend setup or readiness failed"
PROD_GPU_IDS=$(python3 - "$BACKEND_NAME" <<'PY_GPU'
import json, subprocess, sys
x=json.loads(subprocess.check_output(["docker","inspect",sys.argv[1]]))[0]
ids=[]
for e in x.get("Config",{}).get("Env",[]) or []:
 if e.startswith(("NVIDIA_VISIBLE_DEVICES=","CUDA_VISIBLE_DEVICES=")): ids += e.partition("=")[2].split(",")
for req in x.get("HostConfig",{}).get("DeviceRequests",[]) or []: ids += req.get("DeviceIDs") or []
print("\n".join(i.strip() for i in ids if i.strip() and i.strip() not in {"all","void","none"}))
PY_GPU
)
if printf '%s\n' "$PROD_GPU_IDS" | grep -Fxq "$PHASE9_TEST_GPU_UUID"; then
    fail "PHASE9_TEST_GPU_UUID is assigned to the production backend"; exit 1
fi
BACKEND_PORT=$(docker inspect -f '{{range .Config.Env}}{{if eq (index (split . "=") 0) "S2_PORT"}}{{index (split . "=") 1}}{{end}}{{end}}' "$BACKEND_NAME")
BACKEND_PORT="${BACKEND_PORT:-3030}"
docker pull "$BACKEND_IMAGE" >/dev/null
BACKEND_REPODIGESTS=$(docker image inspect "$BACKEND_IMAGE" --format '{{json .RepoDigests}}')
echo "$BACKEND_REPODIGESTS" | grep -Fq "@$BACKEND_DIGEST" || { fail "Backend image digest mismatch"; exit 1; }
# Clone only ordinary environment and filesystem mounts; never clone GPU/device,
# network identity, ports, restart policy, privileges, or daemon control sockets.
python3 - "$BACKEND_NAME" "$ARTIFACT_DIR/backend-clone.args" <<'PY_ARGS'
import json, subprocess, sys
name, output = sys.argv[1:]
d = json.loads(subprocess.check_output(["docker", "inspect", name]))[0]
args=[]
for item in d.get("Config",{}).get("Env",[]) or []:
    key=item.partition("=")[0]
    if key not in {"NVIDIA_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES"}:
        args += ["-e", item]
for m in d.get("Mounts",[]) or []:
    src, dst = m.get("Source"), m.get("Destination")
    if src and dst and ("docker" + ".sock") not in src and ("docker" + ".sock") not in dst:
        args += ["-v", f"{src}:{dst}:ro"]
with open(output,"w") as f:
    for arg in args: f.write(arg.replace("\\","\\\\").replace("\n","\\n")+"\n")
PY_ARGS
mapfile -t BACKEND_CLONE_ARGS < "$ARTIFACT_DIR/backend-clone.args"
docker run -d --name "$TEST_BACKEND_NAME" \
    --label com.sorilo.phase9-live-smoke=true --network "$SHARED_NET" \
    --gpus "device=$PHASE9_TEST_GPU_UUID" \
    "${BACKEND_CLONE_ARGS[@]}" "$BACKEND_IMAGE" >/dev/null
CREATED_CONTAINERS="$TEST_BACKEND_NAME"
docker inspect "$TEST_BACKEND_NAME" > "$ARTIFACT_DIR/test-backend-inspect-start.json"
READY=0
for _ in $(seq 1 60); do
    docker inspect -f '{{.State.Running}}' "$TEST_BACKEND_NAME" 2>/dev/null | grep -qx true || break
    docker exec "$TEST_BACKEND_NAME" python3 -c "import socket;s=socket.socket();s.settimeout(1);s.connect(('127.0.0.1',$BACKEND_PORT))" >/dev/null 2>&1 && READY=1 && break
    sleep 2
done
[ "$READY" = 1 ] || { fail "isolated backend readiness failed"; exit 1; }
BACKEND_HOST="$TEST_BACKEND_NAME"
pass "Isolated backend ready: $TEST_BACKEND_NAME on GPU $PHASE9_TEST_GPU_UUID"

# ── 5. Voice mount ──────────────────────────────────────────────
info "Step 5: Voice mount"
VOICE_DIR=$(docker inspect -f '{{range .Config.Env}}{{if eq (index (split . "=") 0) "S2_VOICE_DIR"}}{{index (split . "=") 1}}{{end}}{{end}}' "$PROD_NAME")
VOICE_DIR="${VOICE_DIR:-/voices}"
VOICE_SRC=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "'$VOICE_DIR'"}}{{.Source}}{{end}}{{end}}' "$PROD_NAME")
VOICE_MOUNT_ARGS=()
[ -n "$VOICE_SRC" ] && VOICE_MOUNT_ARGS=(-v "$VOICE_SRC:$VOICE_DIR:ro")
[ -z "$VOICE_SRC" ] && { fail "No mount for $VOICE_DIR"; exit 1; }
info "Voice: $VOICE_SRC -> $VOICE_DIR"

# ── 6. Port check ───────────────────────────────────────────────
ss -tlnp 2>/dev/null | grep -q ":${SHADOW_PORT} " && { fail "Port $SHADOW_PORT in use"; exit 1; }
pass "Port $SHADOW_PORT free"

# ── 7. Pull and verify image ────────────────────────────────────
info "Step 7: Pulling image"
docker pull "$TEST_IMAGE" 2>&1 | tail -1
DIGESTS=$(docker image inspect "$TEST_IMAGE" --format '{{json .RepoDigests}}' 2>/dev/null)
IMAGE_ID=$(docker image inspect "$TEST_IMAGE" --format '{{.Id}}' 2>/dev/null)
echo "$DIGESTS" | grep -Fq "@$EXPECTED_DIGEST" && pass "Digest verified" || { fail "Digest mismatch"; exit 1; }

# ── 8. Backend idle check (fixed integer bug) ───────────────────
info "Step 8: Idle check"
ACTIVE=$(docker logs "$PROD_NAME" --since 15s 2>&1 | grep -c "queue_started" || true)
ACTIVE="${ACTIVE:-0}"
if ! [[ "$ACTIVE" =~ ^[0-9]+$ ]]; then ACTIVE=0; fi
[ "$ACTIVE" -gt 0 ] && { info "Active synthesis — waiting 10s..."; sleep 10; }
pass "Backend idle check: $ACTIVE recent synthesis"

# ── 9. Start shadow wrapper ─────────────────────────────────────
info "Step 9: Starting shadow wrapper"
docker run -d --name "$SHADOW_NAME" \
    --label com.sorilo.phase9-live-smoke=true \
    --network "$SHARED_NET" \
    -p "127.0.0.1:$SHADOW_PORT:10200" \
    -e TTS_BACKEND=s2cpp \
    -e "S2_HOST=$TEST_BACKEND_NAME" -e "S2_PORT=$BACKEND_PORT" \
    -e S2_MODEL=/models/s2-pro-q4_k_m.gguf \
    -e S2_GPU_LAYERS=-1 -e S2_CODEC_CPU=false \
    -e S2_STREAM=true -e S2_SEGMENT_SENTENCES=false \
    -e S2_CODEC_CONTEXT_FRAMES=32 -e S2_STREAM_DECODE_STRIDE_FRAMES=32 \
    -e S2_STREAM_HOLDBACK_FRAMES=0 -e S2_STREAM_START_BUFFER_MS=0 \
    -e S2_INITIAL_BUFFER_MS=0 -e S2_LONG_FORM_BUFFER_MS=0 \
    -e S2_MAX_INITIAL_BUFFER_MS=0 -e S2_LOW_LATENCY=true \
    -e S2_DEFAULT_VOICE=cmu_bdl_male_us -e "S2_VOICE_DIR=$VOICE_DIR" \
    -e MAX_QUEUE_SIZE=3 -e CANCEL_ON_NEW_REQUEST=false \
    -e CANCEL_ON_CLIENT_DISCONNECT=true \
    -e S2_BACKEND_BUSY_MAX_RETRIES=10 -e S2_BACKEND_BUSY_RETRY_DELAY_MS=500 \
    -e S2_QUEUE_WAIT_TIMEOUT_SEC=30 -e S2_SYNTHESIS_TIMEOUT_SEC=120 \
    "${VOICE_MOUNT_ARGS[@]}" "$TEST_IMAGE" 2>&1
CREATED_CONTAINERS="$CREATED_CONTAINERS $SHADOW_NAME"
sleep 3
docker ps --filter "name=$SHADOW_NAME" --format '{{.Status}}' | grep -q "Up" \
    || { fail "Shadow wrapper not running"; docker logs "$SHADOW_NAME" 2>&1 | tail -20; exit 1; }
pass "Shadow wrapper running"

# Start background log follower (for helper-mode log access)
LOG_FOLLOWER_PID=""
docker logs --follow --since 0s "$SHADOW_NAME" > "$ARTIFACT_DIR/shadow-live.log" 2>&1 &
LOG_FOLLOWER_PID=$!
info "Log follower PID: $LOG_FOLLOWER_PID"

# ── 10. Wait for backend ────────────────────────────────────────
info "Step 10: Backend reachable"
READY=0
for i in $(seq 1 20); do
    if docker exec "$SHADOW_NAME" python3 -c "
import socket; s=socket.socket(); s.settimeout(2)
try: s.connect(('$BACKEND_HOST',$BACKEND_PORT)); print('ok'); s.close()
except: pass" 2>/dev/null | grep -q ok; then READY=1; break; fi
    sleep 1.5
done
[ "$READY" = "1" ] && pass "Backend reachable" || { fail "Backend unreachable"; exit 1; }
sleep 2

# ── 11. Select Python runtime ───────────────────────────────────
info "Step 11: Python runtime"
export PHASE9_FAILURE_TYPE="import"
export PHASE9_FAILURE_REASON="no usable Python runtime with wyoming import"
CLIENT_HOST="127.0.0.1"
CLIENT_PORT="$SHADOW_PORT"
PYTHON=""
RUNNER="host"

try_python() {
    local py="$1"
    if "$py" -c "import wyoming" 2>/dev/null; then
        PYTHON="$py"; return 0
    fi
    return 1
}

if [ -f "$REPO_DIR/.venv/bin/python" ] && try_python "$REPO_DIR/.venv/bin/python"; then
    info "Using $PYTHON"
elif try_python "$(which python3 2>/dev/null || echo python3)"; then
    info "Using $PYTHON"
else
    info "No host Python with wyoming — using helper container"
    export PHASE9_FAILURE_TYPE="helper"
    export PHASE9_FAILURE_REASON="helper container startup or import failed"
    RUNNER="helper"
    USE_HELPER_CONTAINER="true"
    CLIENT_HOST="$SHADOW_NAME"
    CLIENT_PORT="10200"

    docker run -d --name "$CLIENT_NAME" \
        --label com.sorilo.phase9-live-smoke=true \
        --network "$SHARED_NET" \
        --user 0:0 \
        --entrypoint sleep \
        -v "$REPO_DIR:/workspace:ro" \
        -v "$ARTIFACT_DIR:/artifacts" \
        -e "SHADOW_CONTAINER=$SHADOW_NAME" \
        -e SHADOW_LOG_PATH=/artifacts/shadow-live.log \
        -e "BACKEND_CONTAINER=$TEST_BACKEND_NAME" \
        "$TEST_IMAGE" infinity 2>&1

    CREATED_CONTAINERS="$CREATED_CONTAINERS $CLIENT_NAME"
    sleep 2
    docker ps --filter "name=$CLIENT_NAME" --format '{{.Status}}' | grep -q "Up" \
        || { fail "Helper container not running"; exit 1; }

    if docker exec "$CLIENT_NAME" python3 -c "import wyoming; print('wyoming OK')" 2>/dev/null | grep -q OK; then
        pass "Helper container: wyoming available"
    else
        fail "Helper container: wyoming import failed"; exit 1
    fi
fi

# ── 12. Run validation client ───────────────────────────────────
info "Step 12: Running client ($RUNNER mode)"
export PHASE9_FAILURE_TYPE="runtime"
export PHASE9_FAILURE_REASON="validation client failed before writing results"
[ -f "$ARTIFACT_DIR/shadow-live.log" ] && [ -r "$ARTIFACT_DIR/shadow-live.log" ] \
    || { fail "Shadow live log missing or unreadable"; exit 1; }

run_client() {
    if [ "$RUNNER" = "helper" ]; then
        docker exec -e SHADOW_LOG_PATH=/artifacts/shadow-live.log "$CLIENT_NAME" python3 \
            /workspace/scripts/phase_9_live_client.py \
            "$CLIENT_HOST" "$CLIENT_PORT" /artifacts 2>&1
    else
        SHADOW_CONTAINER="$SHADOW_NAME" BACKEND_CONTAINER="$TEST_BACKEND_NAME" \
            SHADOW_LOG_PATH="$ARTIFACT_DIR/shadow-live.log" "$PYTHON" "$REPO_DIR/scripts/phase_9_live_client.py" \
            "$CLIENT_HOST" "$CLIENT_PORT" "$ARTIFACT_DIR" 2>&1
    fi
}

set +e
run_client | tee "$ARTIFACT_DIR/console.log"
CLIENT_EXIT=${PIPESTATUS[0]}
set -e
info "Client exit: $CLIENT_EXIT"

# ── 13. Stop log follower ──────────────────────────────────────
if [ -n "$LOG_FOLLOWER_PID" ]; then
    kill "$LOG_FOLLOWER_PID" 2>/dev/null || true
    wait "$LOG_FOLLOWER_PID" 2>/dev/null || true
    LOG_FOLLOWER_PID=""
fi

# ── 14. Collect logs ────────────────────────────────────────────
docker logs "$SHADOW_NAME" > "$ARTIFACT_DIR/shadow-wrapper-logs.txt" 2>&1
docker logs "$TEST_BACKEND_NAME" > "$ARTIFACT_DIR/test-backend-logs.txt" 2>&1
docker inspect "$TEST_BACKEND_NAME" > "$ARTIFACT_DIR/test-backend-inspect-final.json" 2>&1
[ "$RUNNER" = "helper" ] && docker logs "$CLIENT_NAME" > "$ARTIFACT_DIR/helper-logs.txt" 2>&1

# Test-backend comparison proves it is distinct from production.
python3 - "$ARTIFACT_DIR" <<'PY_COMPARE'
import json, os, sys
p=sys.argv[1]
try:
 prod=json.load(open(os.path.join(p,"production-before-backend.json")))
 test=json.load(open(os.path.join(p,"test-backend-inspect-final.json")))[0]
 out={"production_id":prod.get("id"),"test_backend_id":test.get("Id"),"distinct":prod.get("id") != test.get("Id")}
except Exception as e: out={"distinct":False,"error":f"{type(e).__name__}: {e}"}
json.dump(out,open(os.path.join(p,"production-comparison-test-backend.json"),"w"),indent=2)
PY_COMPARE

# ── 15. Production comparison ───────────────────────────────────
snapshot "$PROD_NAME" "$ARTIFACT_DIR/production-after-wrapper.json" 2>/dev/null || true
snapshot "$BACKEND_NAME" "$ARTIFACT_DIR/production-after-backend.json" 2>/dev/null || true

python3 -c "
import json, os
artifact = '$ARTIFACT_DIR'

# Check if client produced results.json
client_results_path = os.path.join(artifact, 'results.json')
if not os.path.exists(client_results_path):
    # Infrastructure failure — client didn't produce output
    prod_ok = True
    try:
        for c in ['wrapper','backend']:
            before = json.load(open(f'{artifact}/production-before-{c}.json'))
            after = json.load(open(f'{artifact}/production-after-{c}.json'))
            if before.get('id') != after.get('id'): prod_ok = False
            if not after.get('running'): prod_ok = False
    except: prod_ok = 'unknown'
    results = {
        'classification': 'FAIL',
        'failure_type': 'infrastructure',
        'reason': 'validation client exited without results.json; inspect console.log for exact stderr',
        'production_unchanged': prod_ok,
        'tests': {}
    }
    with open(f'{artifact}/results.json','w') as f: json.dump(results, f, indent=2)
    with open(f'{artifact}/summary.md','w') as f:
        f.write(f'# Phase 9 Live Validation — INFRASTRUCTURE FAILURE\n')
        f.write(f'**Reason:** validation client exited without results.json; see console.log\n')
        f.write(f'**Production unchanged:** {prod_ok}\n')
    print(f'Infrastructure failure — shell fallback results written')
    print(f'Classification: FAIL (infrastructure)')
    exit(0)

with open(client_results_path) as f: results = json.load(f)

# Merge with production check
PROD_OK = True
for c in ['wrapper','backend']:
    try:
        before = json.load(open(f'{artifact}/production-before-{c}.json'))
        after = json.load(open(f'{artifact}/production-after-{c}.json'))
        if before.get('id') != after.get('id'): PROD_OK = False
        if not after.get('running'): PROD_OK = False
        unchanged = (before.get('id') == after.get('id'))
        with open(f'{artifact}/production-comparison-{c}.json','w') as f:
            json.dump({'container': c, 'unchanged': unchanged, 'before_id': before.get('id'), 'after_id': after.get('id')}, f, indent=2)
    except: PROD_OK = False

results['production_unchanged'] = PROD_OK
client_class = results.get('classification','FAIL')
final_class = 'FAIL' if (client_class == 'FAIL' or not PROD_OK) else client_class
results['classification'] = final_class
with open(client_results_path,'w') as f: json.dump(results, f, indent=2)

# Summary
with open(f'{artifact}/summary.md','w') as f:
    f.write(f'# Phase 9 Live Validation Report\n')
    f.write(f'- **Classification:** {final_class}\n')
    f.write(f'- **Production unchanged:** {PROD_OK}\n')
    f.write(f'- **Artifacts:** {artifact}\n')
    tests = results.get('tests', {})
    for k,v in tests.items():
        if isinstance(v, dict) and 'status' in v:
            f.write(f'- {k}: {v[\"status\"]}\n')
print(f'Classification: {final_class}')
" 2>&1 | tee -a "$ARTIFACT_DIR/console.log"

echo "========================================"
echo "Phase 9 Live Validation Complete"
FINAL_CLASS=$(python3 -c "import json; print(json.load(open('$ARTIFACT_DIR/results.json'))['classification'])")
echo "Classification: $FINAL_CLASS"
echo "Artifacts: $ARTIFACT_DIR"
echo "========================================"

case "$FINAL_CLASS" in
    PASS) exit 0 ;; PARTIAL) exit 2 ;; *) exit 1 ;;
esac

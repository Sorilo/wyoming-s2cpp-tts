#!/bin/bash
# Phase 9 headless live validation — run from Unraid terminal
# Usage: bash scripts/validate_phase_9_live.sh
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
pass() { echo -e "${GREEN}PASS${NC} $*"; }
fail() { echo -e "${RED}FAIL${NC} $*"; }
info() { echo -e "${YELLOW}INFO${NC} $*"; }

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TIMESTAMP="$(date -u +%Y%m%d_%H%M%S)"
ARTIFACT_DIR="$REPO_DIR/verification_artifacts/phase_9_live_smoke/$TIMESTAMP"
mkdir -p "$ARTIFACT_DIR"

TEST_IMAGE="ghcr.io/sorilo/wyoming-s2cpp-tts:sha-12f3bf8"
EXPECTED_DIGEST="sha256:1954a448a52cf6ebbbd4c09c231fb416b045d8d421d25b1c3e11acf82be28d9b"
SHADOW_NAME="wyoming-s2cpp-tts-phase9-smoke-${TIMESTAMP}"
SHADOW_PORT="10201"
PROD_NAME="wyoming-s2cpp-tts"
BACKEND_NAME="s2cpp-backend"
CREATED_CONTAINERS=""
PYTHON=""

# ── Cleanup trap ────────────────────────────────────────────────
cleanup() {
    local exit_code=$?
    info "Cleaning up temporary containers..."
    for c in $CREATED_CONTAINERS; do
        docker stop "$c" 2>/dev/null || true
        docker rm "$c" 2>/dev/null || true
        info "Removed: $c"
    done
    exit $exit_code
}
trap cleanup EXIT INT TERM

echo "=== Phase 9 Live Validation ==="
echo "Artifacts: $ARTIFACT_DIR"
echo "Started: $(date -u -Iseconds)"
echo ""

# ── 1. Docker access ────────────────────────────────────────────
info "Step 1: Docker access"
docker version > /dev/null 2>&1 || { fail "Docker daemon unreachable"; exit 1; }
pass "Docker accessible"

# ── 2. Find Python ──────────────────────────────────────────────
[ -f "$REPO_DIR/.venv/bin/python" ] && PYTHON="$REPO_DIR/.venv/bin/python"
[ -z "$PYTHON" ] && PYTHON="$(which python3 2>/dev/null || echo python3)"

# ── 3. Production identity ──────────────────────────────────────
info "Step 3: Production identity"
snapshot() {
    local c="$1" out="$2"
    "$PYTHON" -c "
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

# ── 4. Shared network ───────────────────────────────────────────
info "Step 4: Finding shared network"
WRAPPER_NETS=$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$PROD_NAME")
BACKEND_NETS=$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$BACKEND_NAME")
INTERSECTION=""
for net in $WRAPPER_NETS; do
    for bnet in $BACKEND_NETS; do
        [ "$net" = "$bnet" ] && INTERSECTION="$INTERSECTION $net"
    done
done
INTERSECTION="${INTERSECTION# }"
if [ -z "$INTERSECTION" ]; then
    fail "No shared network between wrapper and backend"
    exit 1
fi
SHARED_NET=""
NET_COUNT=$(echo "$INTERSECTION" | wc -w)
if [ "$NET_COUNT" -eq 1 ]; then
    SHARED_NET="$INTERSECTION"
elif [ -n "${PHASE9_SMOKE_NETWORK:-}" ]; then
    for net in $INTERSECTION; do
        [ "$net" = "$PHASE9_SMOKE_NETWORK" ] && SHARED_NET="$net" && break
    done
    [ -z "$SHARED_NET" ] && { fail "PHASE9_SMOKE_NETWORK=$PHASE9_SMOKE_NETWORK not in intersection: $INTERSECTION"; exit 1; }
else
    fail "Multiple shared networks: $INTERSECTION. Set PHASE9_SMOKE_NETWORK to select one."
    exit 1
fi
pass "Network: $SHARED_NET"

# ── 5. Backend config ───────────────────────────────────────────
info "Step 5: Backend configuration"
BACKEND_HOST=$(docker inspect -f '{{range .Config.Env}}{{if eq (index (split . "=") 0) "S2_HOST"}}{{index (split . "=") 1}}{{end}}{{end}}' "$PROD_NAME")
BACKEND_PORT=$(docker inspect -f '{{range .Config.Env}}{{if eq (index (split . "=") 0) "S2_PORT"}}{{index (split . "=") 1}}{{end}}{{end}}' "$PROD_NAME")
BACKEND_PORT="${BACKEND_PORT:-3030}"
[ -z "$BACKEND_HOST" ] && { fail "S2_HOST not set in production wrapper"; exit 1; }
info "Backend: $BACKEND_HOST:$BACKEND_PORT"

# ── 6. Voice mount ──────────────────────────────────────────────
info "Step 6: Voice mount"
VOICE_DIR=$(docker inspect -f '{{range .Config.Env}}{{if eq (index (split . "=") 0) "S2_VOICE_DIR"}}{{index (split . "=") 1}}{{end}}{{end}}' "$PROD_NAME")
VOICE_DIR="${VOICE_DIR:-/voices}"
VOICE_SRC=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "'$VOICE_DIR'"}}{{.Source}}{{end}}{{end}}' "$PROD_NAME")
VOICE_MOUNT_ARGS=()
if [ -n "$VOICE_SRC" ]; then
    VOICE_MOUNT_ARGS=(-v "$VOICE_SRC:$VOICE_DIR:ro")
    info "Voice mount: $VOICE_SRC -> $VOICE_DIR"
else
    fail "No Docker mount found for S2_VOICE_DIR=$VOICE_DIR"
    exit 1
fi

# ── 7. Port check ───────────────────────────────────────────────
ss -tlnp 2>/dev/null | grep -q ":${SHADOW_PORT} " && { fail "Port $SHADOW_PORT in use"; exit 1; }
pass "Port $SHADOW_PORT free"

# ── 8. Pull and verify image ────────────────────────────────────
info "Step 8: Pulling image"
docker pull "$TEST_IMAGE" 2>&1 | tail -1
DIGESTS=$(docker image inspect "$TEST_IMAGE" --format '{{json .RepoDigests}}' 2>/dev/null)
IMAGE_ID=$(docker image inspect "$TEST_IMAGE" --format '{{.Id}}' 2>/dev/null)
echo "ImageID: $IMAGE_ID"
echo "RepoDigests: $DIGESTS"
echo "$DIGESTS" | grep -q "$EXPECTED_DIGEST" && pass "Digest verified" || { fail "Digest mismatch: expected $EXPECTED_DIGEST"; exit 1; }

# ── 9. Wait for idle backend ────────────────────────────────────
info "Step 9: Checking backend idle"
ACTIVE=$(docker logs "$PROD_NAME" --tail 5 2>&1 | grep -c "queue_started" || echo 0)
[ "$ACTIVE" -gt 0 ] && info "Active synthesis detected — waiting 10s..." && sleep 10

# ── 10. Start shadow wrapper ────────────────────────────────────
info "Step 10: Starting shadow wrapper"
docker run -d --name "$SHADOW_NAME" \
    --label com.sorilo.phase9-live-smoke=true \
    --network "$SHARED_NET" \
    -p "127.0.0.1:$SHADOW_PORT:10200" \
    -e TTS_BACKEND=s2cpp \
    -e "S2_HOST=$BACKEND_HOST" \
    -e "S2_PORT=$BACKEND_PORT" \
    -e S2_MODEL=/models/s2-pro-q4_k_m.gguf \
    -e S2_GPU_LAYERS=-1 \
    -e S2_CODEC_CPU=false \
    -e S2_STREAM=true \
    -e S2_SEGMENT_SENTENCES=false \
    -e S2_CODEC_CONTEXT_FRAMES=32 \
    -e S2_STREAM_DECODE_STRIDE_FRAMES=32 \
    -e S2_STREAM_HOLDBACK_FRAMES=0 \
    -e S2_STREAM_START_BUFFER_MS=0 \
    -e S2_INITIAL_BUFFER_MS=0 \
    -e S2_LONG_FORM_BUFFER_MS=0 \
    -e S2_MAX_INITIAL_BUFFER_MS=0 \
    -e S2_LOW_LATENCY=true \
    -e S2_DEFAULT_VOICE=cmu_bdl_male_us \
    -e "S2_VOICE_DIR=$VOICE_DIR" \
    -e MAX_QUEUE_SIZE=3 \
    -e CANCEL_ON_NEW_REQUEST=false \
    -e CANCEL_ON_CLIENT_DISCONNECT=true \
    -e S2_BACKEND_BUSY_MAX_RETRIES=3 \
    -e S2_BACKEND_BUSY_RETRY_DELAY_MS=200 \
    -e S2_QUEUE_WAIT_TIMEOUT_SEC=30 \
    -e S2_SYNTHESIS_TIMEOUT_SEC=120 \
    "${VOICE_MOUNT_ARGS[@]}" \
    "$TEST_IMAGE" 2>&1

CREATED_CONTAINERS="$SHADOW_NAME"
sleep 3
docker ps --filter "name=$SHADOW_NAME" --format '{{.Status}}' | grep -q "Up" \
    || { fail "Shadow wrapper not running"; docker logs "$SHADOW_NAME" 2>&1 | tail -20; exit 1; }
pass "Shadow wrapper running"

# ── 11. Wait for backend ────────────────────────────────────────
info "Step 11: Waiting for backend..."
READY=0
for i in $(seq 1 20); do
    if docker exec "$SHADOW_NAME" python3 -c "
import socket; s=socket.socket(); s.settimeout(2)
try: s.connect(('$BACKEND_HOST',$BACKEND_PORT)); print('ok'); s.close()
except: pass" 2>/dev/null | grep -q ok; then
        READY=1; break
    fi
    sleep 1.5
done
[ "$READY" = "1" ] && pass "Backend reachable" || { fail "Backend unreachable"; exit 1; }
sleep 2

# ── 12. Run validation client (PIPESTATUS-safe) ─────────────────
info "Step 12: Running Phase 9 validation client"
export SHADOW_CONTAINER="$SHADOW_NAME"
export BACKEND_CONTAINER="$BACKEND_NAME"

set +e
"$PYTHON" "$REPO_DIR/scripts/phase_9_live_client.py" "127.0.0.1" "$SHADOW_PORT" "$ARTIFACT_DIR" 2>&1 | tee "$ARTIFACT_DIR/console.log"
CLIENT_EXIT=${PIPESTATUS[0]}
set -e
info "Client exit code: $CLIENT_EXIT"

# ── 13. Collect logs ────────────────────────────────────────────
docker logs "$SHADOW_NAME" > "$ARTIFACT_DIR/shadow-wrapper-logs.txt" 2>&1
docker logs "$BACKEND_NAME" --tail 100 > "$ARTIFACT_DIR/backend-log-excerpt.txt" 2>&1

# ── 14. Parse queue events ──────────────────────────────────────
"$PYTHON" -c "
import json
events = []
with open('$ARTIFACT_DIR/shadow-wrapper-logs.txt') as f:
    for line in f:
        try: events.append(json.loads(line))
        except: pass
queue_events = [e for e in events if e.get('event','').startswith('queue_')]
with open('$ARTIFACT_DIR/parsed-queue-events.json','w') as f:
    json.dump(queue_events, f, indent=2)
depths = [e.get('queue_depth',-1) for e in queue_events if 'queue_depth' in e]
final = depths[-1] if depths else -1
print(f'Queue events: {len(queue_events)}, final depth: {final}')
" 2>&1 | tee -a "$ARTIFACT_DIR/console.log"

# ── 15. Production comparison ───────────────────────────────────
snapshot "$PROD_NAME" "$ARTIFACT_DIR/production-after-wrapper.json" 2>/dev/null
snapshot "$BACKEND_NAME" "$ARTIFACT_DIR/production-after-backend.json" 2>/dev/null

"$PYTHON" -c "
import json
CLASSIFICATION = 'PASS'
for c in ['wrapper','backend']:
    before = json.load(open('$ARTIFACT_DIR/production-before-'+c+'.json'))
    after = json.load(open('$ARTIFACT_DIR/production-after-'+c+'.json'))
    if set(before.keys()) != set(after.keys()):
        CLASSIFICATION = 'FAIL'
    changed = {}
    for k in before:
        if before[k] != after.get(k):
            changed[k] = {'before': before[k], 'after': after.get(k)}
    result = {'container': c, 'unchanged': len(changed)==0, 'changes': changed}
    with open('$ARTIFACT_DIR/production-comparison-'+c+'.json','w') as f:
        json.dump(result, f, indent=2)
    if not after.get('running'):
        CLASSIFICATION = 'FAIL'
        print(f'FAIL: {c} not running')
    elif changed:
        CLASSIFICATION = 'FAIL'
        print(f'FAIL: {c} changed: {list(changed.keys())}')
    else:
        print(f'PASS: {c} unchanged')
# Read client classification and merge with production result
try:
    with open('$ARTIFACT_DIR/results.json') as f:
        client_results = json.load(f)
    client_class = client_results.get('classification','FAIL')
    if client_class == 'FAIL' or CLASSIFICATION == 'FAIL':
        final_class = 'FAIL'
    elif client_class == 'PARTIAL':
        final_class = 'PARTIAL'
    else:
        final_class = CLASSIFICATION
except:
    final_class = 'FAIL'
with open('$ARTIFACT_DIR/results.json') as f: results = json.load(f)
results['classification'] = final_class
results['production_unchanged'] = (CLASSIFICATION == 'PASS')
with open('$ARTIFACT_DIR/results.json','w') as f: json.dump(results, f, indent=2)
print(f'Final classification: {final_class}')
" 2>&1 | tee -a "$ARTIFACT_DIR/console.log"

# ── 16. Generate summary ────────────────────────────────────────
"$PYTHON" -c "
import json
with open('$ARTIFACT_DIR/results.json') as f: r = json.load(f)
with open('$ARTIFACT_DIR/summary.md','w') as f:
    f.write(f'''# Phase 9 Live Validation Report
- **Date:** $(date -u -Iseconds)
- **Image:** $TEST_IMAGE
- **Digest:** $EXPECTED_DIGEST
- **Shadow:** $SHADOW_NAME
- **Network:** $SHARED_NET

## Classification: {r.get('classification','UNKNOWN')}

## Test Results
| Test | Status |
|------|--------|
''')
    for k,v in r.items():
        if k not in ('classification','short','long','fifo_1','fifo_2','fifo_3','queue_full','disconnect','recovery_cycles','production_unchanged'):
            if isinstance(v, dict) and 'status' in v:
                f.write(f'| {k} | {v[\"status\"]} |\n')
    f.write('''
## Artifacts
| File | Description |
|------|-------------|
| results.json | Final authoritative results |
| short.wav | Short synthesis output |
| long.wav | Long synthesis output |
| recovery.wav | Disconnect recovery output |
| parsed-queue-events.json | Queue lifecycle events |
| production-comparison-*.json | Before/after diff |
''')
" 2>/dev/null

echo ""
echo "========================================"
echo "Phase 9 Live Validation Complete"
FINAL_CLASS=$("$PYTHON" -c "import json; print(json.load(open('$ARTIFACT_DIR/results.json'))['classification'])")
echo "Classification: $FINAL_CLASS"
echo "Artifacts: $ARTIFACT_DIR"
echo "========================================"

case "$FINAL_CLASS" in
    PASS) exit 0 ;;
    PARTIAL) exit 2 ;;
    *) exit 1 ;;
esac

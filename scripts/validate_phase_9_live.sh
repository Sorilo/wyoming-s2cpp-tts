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
TEST_DIGEST="ghcr.io/sorilo/wyoming-s2cpp-tts@sha256:1954a448a52cf6ebbbd4c09c231fb416b045d8d421d25b1c3e11acf82be28d9b"
SHADOW_NAME="wyoming-s2cpp-tts-phase9-smoke-${TIMESTAMP}"
SHADOW_PORT="10201"
PROD_NAME="wyoming-s2cpp-tts"
BACKEND_NAME="s2cpp-backend"
CREATED_CONTAINERS=""

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

# ── 2. Production identity ──────────────────────────────────────
info "Step 2: Production identity"
for c in "$PROD_NAME" "$BACKEND_NAME"; do
    docker inspect "$c" > "$ARTIFACT_DIR/${c}-before.json" 2>/dev/null || {
        fail "Cannot inspect $c"; exit 1; }
done
python3 -c "
import json, subprocess
for c in ['$PROD_NAME', '$BACKEND_NAME']:
    data = json.loads(subprocess.check_output(['docker','inspect',c]))[0]
    out = {
        'id': data['Id'], 'image_ref': data['Config']['Image'],
        'image_id': data['Image'], 'created': data['Created'],
        'started': data['State']['StartedAt'],
        'restart_count': data['RestartCount'],
        'running': data['State']['Running'],
        'networks': list((data.get('NetworkSettings',{}).get('Networks',{}) or {}).keys()),
    }
    with open('$ARTIFACT_DIR/production-before-'+c+'.json','w') as f: json.dump(out,f,indent=2)
" 2>/dev/null
pass "Production identity recorded"

# ── 3. Shared network ───────────────────────────────────────────
info "Step 3: Finding shared network"
WRAPPER_NETS=$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$PROD_NAME" 2>/dev/null)
BACKEND_NETS=$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$BACKEND_NAME" 2>/dev/null)
info "Wrapper nets: $WRAPPER_NETS"
info "Backend nets: $BACKEND_NETS"

SHARED_NET=""
for net in $WRAPPER_NETS; do
    for bnet in $BACKEND_NETS; do
        [ "$net" = "$bnet" ] && SHARED_NET="$net" && break
    done
    [ -n "$SHARED_NET" ] && break
done
[ -z "$SHARED_NET" ] && { fail "No shared Docker network found"; exit 1; }
info "Selected network: $SHARED_NET"
echo "{\"network\":\"$SHARED_NET\"}" > "$ARTIFACT_DIR/network.json"

# ── 4. Backend config ───────────────────────────────────────────
info "Step 4: Backend configuration"
# Extract backend endpoint from production wrapper
BACKEND_HOST=""
BACKEND_PORT="3030"
# Try S2_HOST first
BH=$(docker inspect -f '{{range .Config.Env}}{{if eq (index (split . "=") 0) "S2_HOST"}}{{index (split . "=") 1}}{{end}}{{end}}' "$PROD_NAME" 2>/dev/null)
[ -n "$BH" ] && BACKEND_HOST="$BH"
BP=$(docker inspect -f '{{range .Config.Env}}{{if eq (index (split . "=") 0) "S2_PORT"}}{{index (split . "=") 1}}{{end}}{{end}}' "$PROD_NAME" 2>/dev/null)
[ -n "$BP" ] && BACKEND_PORT="$BP"
info "Backend: $BACKEND_HOST:$BACKEND_PORT"

# ── 5. Voice mount ──────────────────────────────────────────────
info "Step 5: Voice mount"
VOICE_DIR=$(docker inspect -f '{{range .Config.Env}}{{if eq (index (split . "=") 0) "S2_VOICE_DIR"}}{{index (split . "=") 1}}{{end}}{{end}}' "$PROD_NAME" 2>/dev/null)
VOICE_DIR="${VOICE_DIR:-/voices}"
VOICE_SRC=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "'$VOICE_DIR'"}}{{.Source}}{{end}}{{end}}' "$PROD_NAME" 2>/dev/null)

VOICE_MOUNT=""
if [ -n "$VOICE_SRC" ]; then
    VOICE_MOUNT="-v $VOICE_SRC:$VOICE_DIR:ro"
    info "Voice mount: $VOICE_SRC -> $VOICE_DIR"
else
    info "No voice mount found (may use built-in voices)"
fi

# ── 6. Build shadow env ─────────────────────────────────────────
info "Step 6: Building shadow environment"
# Collect production env and override with Phase 9 values
PROD_ENV=$(docker inspect -f '{{range .Config.Env}}--env {{.}} {{end}}' "$PROD_NAME" 2>/dev/null || echo "")

# ── 7. Port check ───────────────────────────────────────────────
ss -tlnp 2>/dev/null | grep -q ":${SHADOW_PORT} " && { fail "Port $SHADOW_PORT in use"; exit 1; }
pass "Port $SHADOW_PORT free"

# ── 8. Pull and verify image ────────────────────────────────────
info "Step 8: Pulling image"
docker pull "$TEST_DIGEST" 2>&1 | tail -1
DIGEST=$(docker image inspect "$TEST_IMAGE" --format '{{index .RepoDigests 0}}' 2>/dev/null)
info "Image digest: $DIGEST"
echo "$DIGEST" | grep -q "sha256:1954a448a52cf6ebbbd4c09c231fb416b045d8d421d25b1c3e11acf82be28d9b" \
    && pass "Digest matches" || { fail "Digest mismatch: $DIGEST"; exit 1; }

# ── 9. Wait for idle backend ─────────────────────────────────────
info "Step 9: Checking backend idle state"
ACTIVE=$(docker logs "$PROD_NAME" --tail 5 2>&1 | grep -c "queue_started\|syn_trigger" || echo 0)
if [ "$ACTIVE" -gt 0 ]; then
    info "Production wrapper has active synthesis — waiting 10s..."
    sleep 10
fi

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
    $VOICE_MOUNT \
    "$TEST_IMAGE" 2>&1

CREATED_CONTAINERS="$SHADOW_NAME"
sleep 3

docker ps --filter "name=$SHADOW_NAME" --format '{{.Status}}' | grep -q "Up" \
    || { fail "Shadow wrapper not running"; docker logs "$SHADOW_NAME" 2>&1 | tail -20; exit 1; }
pass "Shadow wrapper running"
docker inspect "$SHADOW_NAME" > "$ARTIFACT_DIR/shadow-wrapper-inspect.json" 2>/dev/null

# ── 11. Wait for backend reachable ──────────────────────────────
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

# Wait for Wyoming describe to be available
sleep 2

# ── 12. Run Python validation client ────────────────────────────
info "Step 12: Running Phase 9 validation client"
PYTHON=""
[ -f "$REPO_DIR/.venv/bin/python" ] && PYTHON="$REPO_DIR/.venv/bin/python"
[ -z "$PYTHON" ] && PYTHON="$(which python3 2>/dev/null || echo python3)"

export SHADOW_CONTAINER="$SHADOW_NAME"
export BACKEND_CONTAINER="$BACKEND_NAME"

"$PYTHON" "$REPO_DIR/scripts/phase_9_live_client.py" "127.0.0.1" "$SHADOW_PORT" "$ARTIFACT_DIR" 2>&1 | tee "$ARTIFACT_DIR/console.log"
CLIENT_EXIT=$?

# ── 13. Collect logs ────────────────────────────────────────────
docker logs "$SHADOW_NAME" > "$ARTIFACT_DIR/shadow-wrapper-logs.txt" 2>&1
docker logs "$BACKEND_NAME" --tail 100 > "$ARTIFACT_DIR/backend-log-excerpt.txt" 2>&1

# ── 14. Parse structured queue events ───────────────────────────
info "Step 14: Parsing queue events"
python3 -c "
import json, sys
events = []
with open('$ARTIFACT_DIR/shadow-wrapper-logs.txt','r') as f:
    for line in f:
        try: events.append(json.loads(line))
        except: pass
queue_events = [e for e in events if e.get('event','').startswith('queue_')]
with open('$ARTIFACT_DIR/parsed-queue-events.json','w') as f:
    json.dump(queue_events, f, indent=2)
depths = [e.get('queue_depth',-1) for e in queue_events if 'queue_depth' in e]
final = depths[-1] if depths else -1
print(f'Queue events: {len(queue_events)}, final depth: {final}')
errors = sum(1 for e in events if e.get('event','') in ('backend_stream_done','backend_busy_exhausted','synthesis_timeout'))
warnings = sum(1 for line in open('$ARTIFACT_DIR/shadow-wrapper-logs.txt') if 'warning' in line.lower())
print(f'Errors: {errors}, Warnings: {warnings}')
" 2>&1 | tee -a "$ARTIFACT_DIR/console.log"

# ── 15. Production verification ─────────────────────────────────
info "Step 15: Production verification"
for c in "$PROD_NAME" "$BACKEND_NAME"; do
    docker inspect "$c" > "$ARTIFACT_DIR/${c}-after.json" 2>/dev/null || true
done
python3 -c "
import json
for c in ['$PROD_NAME', '$BACKEND_NAME']:
    before = json.load(open('$ARTIFACT_DIR/production-before-'+c+'.json'))
    after_data = json.loads(__import__('subprocess').check_output(['docker','inspect',c]))[0]
    after = {'id': after_data['Id'], 'image_ref': after_data['Config']['Image'],
             'image_id': after_data['Image'], 'created': after_data['Created'],
             'started': after_data['State']['StartedAt'],
             'restart_count': after_data['RestartCount'],
             'running': after_data['State']['Running']}
    changed = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    result = {'container': c, 'unchanged': len(changed)==0, 'changes': changed}
    with open('$ARTIFACT_DIR/production-comparison-'+c+'.json','w') as f: json.dump(result,f,indent=2)
    print(f'{c}: unchanged={len(changed)==0}' + (f' changes={changed}' if changed else ''))
    if not after['running']:
        print(f'  WARNING: {c} not running!')
" 2>&1 | tee -a "$ARTIFACT_DIR/console.log"

# ── 16. Generate summary ────────────────────────────────────────
info "Step 16: Generating summary"
python3 -c "
import json
with open('$ARTIFACT_DIR/results.json') as f: r = json.load(f)
with open('$ARTIFACT_DIR/summary.md','w') as f:
    f.write(f'''# Phase 9 Live Validation Report
- **Date:** $(date -u -Iseconds)
- **Image:** $TEST_IMAGE
- **Digest:** $TEST_DIGEST
- **Shadow:** $SHADOW_NAME
- **Network:** $SHARED_NET
- **Port:** $SHADOW_PORT

## Overall Classification: {r.get('classification','UNKNOWN')}

## Test Results
| Test | Status |
|------|--------|
''')
    for k,v in r.items():
        if k not in ('classification','short','long','fifo_1','fifo_2','fifo_3','queue_full','disconnect','recovery_cycles'):
            if isinstance(v, dict) and 'status' in v:
                f.write(f'| {k} | {v[\"status\"]} |\n')
    f.write('''
## Artifacts
| File | Description |
|------|-------------|
| results.json | Machine-readable results |
| short.wav | Short synthesis output |
| long.wav | Long synthesis output |
| fifo-request-*.wav | FIFO test outputs |
| recovery.wav | Disconnect recovery output |
| post-queue-full-recovery.wav | Queue-full recovery output |
| shadow-wrapper-logs.txt | Full shadow container logs |
| parsed-queue-events.json | Extracted queue lifecycle events |
| production-before-*.json | Production identity before test |
| production-comparison-*.json | Production before/after diff |
''')
print('Summary written')
" 2>&1

echo ""
echo "========================================"
echo "Phase 9 Live Validation Complete"
echo "Classification: $(python3 -c "import json; print(json.load(open('$ARTIFACT_DIR/results.json'))['classification'])")"
echo "Exit code: $CLIENT_EXIT"
echo "Artifacts: $ARTIFACT_DIR"
echo "========================================"

exit $CLIENT_EXIT

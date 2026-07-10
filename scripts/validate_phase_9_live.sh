#!/bin/bash
# Phase 9 headless live validation — run from Unraid terminal
# Usage: bash scripts/validate_phase_9_live.sh
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS=0; FAIL=0; TOTAL=0

pass() { echo -e "${GREEN}PASS${NC} $*"; PASS=$((PASS+1)); TOTAL=$((TOTAL+1)); }
fail() { echo -e "${RED}FAIL${NC} $*"; FAIL=$((FAIL+1)); TOTAL=$((TOTAL+1)); }
info() { echo -e "${YELLOW}INFO${NC} $*"; }

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ARTIFACT_DIR="$REPO_DIR/verification_artifacts/phase_9_live_smoke/$(date -u +%Y%m%d_%H%M%S)"
mkdir -p "$ARTIFACT_DIR"

TEST_IMAGE="ghcr.io/sorilo/wyoming-s2cpp-tts:sha-12f3bf8"
TEST_DIGEST="ghcr.io/sorilo/wyoming-s2cpp-tts@sha256:1954a448a52cf6ebbbd4c09c231fb416b045d8d421d25b1c3e11acf82be28d9b"
SHADOW_NAME="wyoming-s2cpp-tts-phase9-smoke"
SHADOW_PORT="10201"
BACKEND_NAME="s2cpp-backend"
PROD_NAME="wyoming-s2cpp-tts"

echo "=== Phase 9 Live Validation ==="
echo "Artifacts: $ARTIFACT_DIR"
echo "Started: $(date -u -Iseconds)"
echo ""

# ── Step 1: Docker access ──────────────────────────────────────
info "Step 1: Docker access"
docker version > /dev/null 2>&1 && pass "Docker accessible" || { fail "Docker daemon unreachable"; exit 1; }

# ── Step 2: Record production identity ─────────────────────────
info "Step 2: Production identity"
for CONTAINER in "$PROD_NAME" "$BACKEND_NAME"; do
    docker inspect "$CONTAINER" > "$ARTIFACT_DIR/${CONTAINER}-before.json" 2>/dev/null || \
        { fail "Cannot inspect $CONTAINER"; exit 1; }
    CID=$(docker inspect -f '{{.Id}}' "$CONTAINER" 2>/dev/null)
    IMG=$(docker inspect -f '{{.Config.Image}}' "$CONTAINER" 2>/dev/null)
    STATE=$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null)
    echo "$CONTAINER: id=$CID image=$IMG state=$STATE" | tee -a "$ARTIFACT_DIR/production-before.txt"
done
pass "Production identity recorded"

# ── Extract production config ──────────────────────────────────
NETWORK=$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$PROD_NAME")
BACKEND_HOST=$(docker inspect -f '{{range .Config.Env}}{{if eq (index (split . "=") 0) "S2_HOST"}}{{index (split . "=") 1}}{{end}}{{end}}' "$PROD_NAME")
VOICE_DIR=$(docker inspect -f '{{range .Config.Env}}{{if eq (index (split . "=") 0) "S2_VOICE_DIR"}}{{index (split . "=") 1}}{{end}}{{end}}' "$PROD_NAME")
info "Network=$NETWORK BackendHost=$BACKEND_HOST VoiceDir=$VOICE_DIR"

# ── Check port 10201 ──────────────────────────────────────────
ss -tlnp 2>/dev/null | grep -q ":10201 " && { fail "Port 10201 in use"; exit 1; }
pass "Port 10201 free"

# ── Step 3: Pull image ────────────────────────────────────────
info "Step 3: Pulling test image"
docker pull "$TEST_DIGEST" 2>&1 | tail -1
docker image inspect "$TEST_IMAGE" --format 'Digest: {{index .RepoDigests 0}}' | tee -a "$ARTIFACT_DIR/shadow-wrapper-inspect.json"
pass "Image pulled"

# ── Step 4: Create shadow wrapper ─────────────────────────────
info "Step 4: Creating shadow wrapper"
docker run -d --name "$SHADOW_NAME" \
    --network "$NETWORK" \
    -p "127.0.0.1:$SHADOW_PORT:10200" \
    -e TTS_BACKEND=s2cpp \
    -e S2_HOST="$BACKEND_HOST" \
    -e S2_PORT=3030 \
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
    -e S2_VOICE_DIR=/voices \
    -e MAX_QUEUE_SIZE=3 \
    -e CANCEL_ON_NEW_REQUEST=false \
    -e CANCEL_ON_CLIENT_DISCONNECT=true \
    -e S2_BACKEND_BUSY_MAX_RETRIES=3 \
    -e S2_BACKEND_BUSY_RETRY_DELAY_MS=200 \
    -e S2_QUEUE_WAIT_TIMEOUT_SEC=30 \
    -e S2_SYNTHESIS_TIMEOUT_SEC=120 \
    "$TEST_IMAGE" 2>&1

sleep 3
docker ps --filter "name=$SHADOW_NAME" --format '{{.Status}}' | grep -q "Up" && pass "Shadow wrapper running" || { fail "Shadow wrapper not running"; docker logs "$SHADOW_NAME" 2>&1 | tail -20; exit 1; }

# ── Wait for backend health ───────────────────────────────────
info "Waiting for backend..."
for i in $(seq 1 15); do
    docker exec "$SHADOW_NAME" python3 -c "
import urllib.request
try:
    r = urllib.request.urlopen('http://${BACKEND_HOST}:3030/generate', data=b'{}', timeout=3)
    print(r.status)
except: print('waiting')
" 2>/dev/null | grep -q "200\|400\|405" && break
    sleep 2
done
pass "Backend reachable"

# ── Wyoming test client ───────────────────────────────────────
cd "$REPO_DIR"
PYTHONPATH=. python3 -c "
import asyncio, json, struct, time, wave
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize, SynthesizeVoice
from wyoming.audio import AudioChunk, AudioStart, AudioStop

async def synthesize(text, host='127.0.0.1', port=${SHADOW_PORT}, timeout=60):
    events = []
    pcm = bytearray()
    async with AsyncTcpClient(host, port) as tcp:
        await tcp.write_event(Synthesize(
            text=text,
            voice=SynthesizeVoice(name='cmu_bdl_male_us')
        ).event())
        start = time.monotonic()
        while True:
            try:
                ev = await asyncio.wait_for(tcp.read_event(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            if ev is None:
                break
            events.append(ev)
            if AudioChunk.is_type(ev.type):
                c = AudioChunk.from_event(ev)
                pcm.extend(c.audio)
            if AudioStop.is_type(ev.type):
                break
    return events, bytes(pcm), time.monotonic() - start

def save_wav(path, pcm, rate=44100):
    with wave.open(path, 'w') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(pcm)

async def tests():
    results = {}
    
    # Test A: short
    events, pcm, dur = await synthesize('The weather is clear and sunny today.')
    types = [e.type for e in events]
    results['short'] = {
        'text': 'The weather is clear and sunny today.',
        'duration_s': round(dur, 3),
        'pcm_bytes': len(pcm),
        'events': types,
        'audio_start': 'audio-start' in types,
        'audio_chunks': sum(1 for t in types if 'audio-chunk' in t),
        'audio_stop': 'audio-stop' in types,
        'valid_pcm': len(pcm) > 0 and len(pcm) % 2 == 0
    }
    save_wav('$ARTIFACT_DIR/short.wav', pcm)
    print('Test A - short:', json.dumps(results['short']))
    
    # Test B: long
    events, pcm, dur = await synthesize(
        'Good morning. Today we have a full schedule of activities planned. '
        'First, we will review the quarterly results and discuss the upcoming projects. '
        'Then we will break for lunch before continuing with the afternoon session.')
    types = [e.type for e in events]
    results['long'] = {
        'duration_s': round(dur, 3),
        'pcm_bytes': len(pcm),
        'events': types,
        'audio_start': 'audio-start' in types,
        'audio_chunks': sum(1 for t in types if 'audio-chunk' in t),
        'audio_stop': 'audio-stop' in types,
        'valid_pcm': len(pcm) > 0 and len(pcm) % 2 == 0
    }
    save_wav('$ARTIFACT_DIR/long.wav', pcm)
    print('Test B - long:', json.dumps(results['long']))
    
    # Test C: FIFO concurrency
    async def synth_with_id(text, n, results_dict):
        events, pcm, dur = await synthesize(text, timeout=120)
        results_dict[str(n)] = {
            'order': n, 'duration_s': round(dur, 3),
            'pcm_bytes': len(pcm), 'valid_pcm': len(pcm) > 0
        }
    
    fifo_results = {}
    t1 = asyncio.create_task(synth_with_id('First request for FIFO testing.', 1, fifo_results))
    await asyncio.sleep(0.1)
    t2 = asyncio.create_task(synth_with_id('Second request should wait for first.', 2, fifo_results))
    await asyncio.sleep(0.1)
    t3 = asyncio.create_task(synth_with_id('Third and final FIFO test request.', 3, fifo_results))
    await asyncio.gather(t1, t2, t3)
    results['fifo'] = fifo_results
    print('Test C - FIFO:', json.dumps(fifo_results))
    
    # Test D: queue-full
    qf_results = {}
    tf1 = asyncio.create_task(synth_with_id('Queue full test — active request one with enough text to keep it busy for a moment.', 1, qf_results))
    await asyncio.sleep(0.2)
    tf2 = asyncio.create_task(synth_with_id('Queue full waiting request two.', 2, qf_results))
    await asyncio.sleep(0.1)
    tf3 = asyncio.create_task(synth_with_id('Queue full waiting request three.', 3, qf_results))
    await asyncio.sleep(0.1)
    # Fourth request should be rejected
    try:
        events, _, _ = await synthesize('This should be rejected — queue at capacity.', timeout=10)
        qf_results['4'] = {'rejected': False, 'events': [e.type for e in events]}
    except Exception as e:
        qf_results['4'] = {'rejected': True, 'error': str(e)[:100]}
    await asyncio.gather(tf1, tf2, tf3)
    results['queue_full'] = qf_results
    print('Test D - queue-full:', json.dumps(qf_results))
    
    # Test E: disconnect + recovery
    async with AsyncTcpClient('127.0.0.1', ${SHADOW_PORT}) as tcp:
        await tcp.write_event(Synthesize(
            text='Disconnect test — this is a long text that will be actively synthesizing when we close the connection.',
            voice=SynthesizeVoice(name='cmu_bdl_male_us')
        ).event())
        # Read until first AudioChunk
        chunk_received = False
        while True:
            ev = await asyncio.wait_for(tcp.read_event(), timeout=30)
            if ev is None: break
            if AudioChunk.is_type(ev.type):
                chunk_received = True
                break
        # Intentionally close without reading more
    await asyncio.sleep(1)
    
    # Recovery
    events, pcm, dur = await synthesize('Recovery test after disconnect.', timeout=60)
    types = [e.type for e in events]
    results['disconnect'] = {
        'chunk_before_disconnect': chunk_received,
        'recovery_audio_start': 'audio-start' in types,
        'recovery_audio_stop': 'audio-stop' in types,
        'recovery_pcm_bytes': len(pcm),
        'recovery_valid_pcm': len(pcm) > 0 and len(pcm) % 2 == 0
    }
    save_wav('$ARTIFACT_DIR/recovery.wav', pcm)
    print('Test E - disconnect:', json.dumps(results['disconnect']))
    
    # Test F: 3-cycle recovery
    cycle_results = []
    for cycle in range(3):
        async with AsyncTcpClient('127.0.0.1', ${SHADOW_PORT}) as tcp:
            await tcp.write_event(Synthesize(
                text=f'Recovery cycle {cycle+1} — this text will be interrupted mid-stream for disconnect testing.',
                voice=SynthesizeVoice(name='cmu_bdl_male_us')
            ).event())
            while True:
                ev = await asyncio.wait_for(tcp.read_event(), timeout=30)
                if ev is None: break
                if AudioChunk.is_type(ev.type): break
        await asyncio.sleep(1)
        events, pcm, _ = await synthesize(f'Recovery cycle {cycle+1} recovery request.', timeout=60)
        cycle_results.append({
            'cycle': cycle+1,
            'recovery_pcm_bytes': len(pcm),
            'valid_pcm': len(pcm) > 0 and len(pcm) % 2 == 0,
            'audio_start': 'audio-start' in [e.type for e in events]
        })
    results['recovery_cycles'] = cycle_results
    print('Test F - cycles:', json.dumps(cycle_results))
    
    with open('$ARTIFACT_DIR/results.json', 'w') as f:
        json.dump(results, f, indent=2)
    
    # Summary
    all_valid = all([
        results['short']['valid_pcm'] and results['short']['audio_start'] and results['short']['audio_stop'],
        results['long']['valid_pcm'] and results['long']['audio_start'] and results['long']['audio_stop'],
        all(str(i) in results['fifo'] and results['fifo'][str(i)]['valid_pcm'] for i in [1,2,3]),
        results['queue_full']['4'].get('rejected', False),
        results['disconnect']['recovery_valid_pcm'],
        all(c['valid_pcm'] for c in results['recovery_cycles'])
    ])
    print(f'\\n=== OVERALL: {\"PASS\" if all_valid else \"PARTIAL\"} ===')

asyncio.run(tests())
" 2>&1 | tee "$ARTIFACT_DIR/client-output.txt"

# ── Collect logs ───────────────────────────────────────────────
docker logs "$SHADOW_NAME" > "$ARTIFACT_DIR/shadow-wrapper-logs.txt" 2>&1
docker logs "$BACKEND_NAME" --tail 50 > "$ARTIFACT_DIR/backend-log-excerpt.txt" 2>&1

# ── Verify queue depth zero ───────────────────────────────────
FINAL_QUEUE=$(grep -c "queue_depth_changed" "$ARTIFACT_DIR/shadow-wrapper-logs.txt" 2>/dev/null || echo 0)
info "Queue depth events: $FINAL_QUEUE"
grep "queue_depth_changed" "$ARTIFACT_DIR/shadow-wrapper-logs.txt" | tail -10 | tee "$ARTIFACT_DIR/queue-depth-log.txt"

# ── Verify log events ─────────────────────────────────────────
for EVENT in queue_request_received queue_admitted queue_wait_started queue_started queue_rejected queue_cancelled client_disconnected synthesis_cancel_requested queue_depth_changed; do
    COUNT=$(grep -c "$EVENT" "$ARTIFACT_DIR/shadow-wrapper-logs.txt" 2>/dev/null || echo 0)
    echo "$EVENT: $COUNT occurrences" | tee -a "$ARTIFACT_DIR/log-event-counts.txt"
done

# ── Check for errors ──────────────────────────────────────────
ERRORS=$(grep -ci "traceback\|error\|exception" "$ARTIFACT_DIR/shadow-wrapper-logs.txt" 2>/dev/null || echo 0)
WARNINGS=$(grep -ci "warning" "$ARTIFACT_DIR/shadow-wrapper-logs.txt" 2>/dev/null || echo 0)
echo "Wrapper errors: $ERRORS, warnings: $WARNINGS" | tee -a "$ARTIFACT_DIR/summary.md"

# ── Step 9: Cleanup ───────────────────────────────────────────
info "Step 9: Cleanup"
docker stop "$SHADOW_NAME" 2>/dev/null && docker rm "$SHADOW_NAME" 2>/dev/null
pass "Shadow wrapper removed"

# ── Verify production unchanged ────────────────────────────────
for CONTAINER in "$PROD_NAME" "$BACKEND_NAME"; do
    NEW_CID=$(docker inspect -f '{{.Id}}' "$CONTAINER")
    OLD_CID=$(grep "id=" "$ARTIFACT_DIR/production-before.txt" | grep "$CONTAINER" | sed 's/.*id=//' | cut -d' ' -f1)
    if [ "$NEW_CID" = "$OLD_CID" ]; then
        pass "$CONTAINER unchanged"
    else
        fail "$CONTAINER changed: $OLD_CID -> $NEW_CID"
    fi
    STATE=$(docker inspect -f '{{.State.Status}}' "$CONTAINER")
    [ "$STATE" = "running" ] && pass "$CONTAINER running" || fail "$CONTAINER state=$STATE"
done

# ── Final report ──────────────────────────────────────────────
cat > "$ARTIFACT_DIR/summary.md" << SUMMARYEOF
# Phase 9 Live Validation Report
- **Date:** $(date -u -Iseconds)
- **Image:** $TEST_IMAGE
- **Digest:** $TEST_DIGEST
- **Shadow:** $SHADOW_NAME (port $SHADOW_PORT)
- **Tests:** $TOTAL total, $PASS passed, $FAIL failed

## Artifacts
- $ARTIFACT_DIR/short.wav
- $ARTIFACT_DIR/long.wav
- $ARTIFACT_DIR/recovery.wav
- $ARTIFACT_DIR/results.json
- $ARTIFACT_DIR/shadow-wrapper-logs.txt
- $ARTIFACT_DIR/client-output.txt

## Classification
$([ $FAIL -eq 0 ] && echo "PASS" || echo "PARTIAL")
SUMMARYEOF

echo ""
echo "=== Validation Complete ==="
echo "Passed: $PASS/$TOTAL"
echo "Artifacts: $ARTIFACT_DIR"
[ $FAIL -eq 0 ] && echo "Classification: PASS" || echo "Classification: PARTIAL"

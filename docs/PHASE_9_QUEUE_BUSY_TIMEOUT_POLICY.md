# Phase 9: Deterministic Queue, Busy/Timeout Policy

## Queue Semantics

`max_size` **includes the active synthesis**:

| max_size | Active | Waiting | Behavior |
|----------|--------|---------|----------|
| 1 | 1 | 0 | Reject when busy |
| 3 | 1 | ≤ 2 | Up to 2 wait |
| N | 1 | ≤ N-1 | Up to N-1 wait |

### Properties
- One active backend synthesis guaranteed
- Waiting queue is FIFO (preserved by `asyncio.Semaphore`)
- `QueueFullError` raised when queue at capacity
- `QueueTimeoutError( asyncio.TimeoutError)` raised on queue wait timeout
- Queue counters return to zero after all completions

## Configuration

| Env Var | Default | Range | Description |
|---------|---------|-------|-------------|
| `MAX_QUEUE_SIZE` | 3 | ≥ 1 | Queue capacity (includes active) |
| `S2_QUEUE_WAIT_TIMEOUT_SEC` | 30 | ≥ 0, ≤ 300 | Max seconds waiting in queue |
| `S2_SYNTHESIS_TIMEOUT_SEC` | 120 | ≥ 0.1, ≤ 600 | Max seconds for backend synthesis |
| `S2_BACKEND_BUSY_MAX_RETRIES` | 3 | ≥ 1, ≤ 10 | Max 503 retries before giving up |
| `S2_BACKEND_BUSY_RETRY_DELAY_MS` | 200 | ≥ 0, ≤ 10000 | Milliseconds between retries |
| `CANCEL_ON_NEW_REQUEST` | false | bool | Cancel active + queued on new request |
| `CANCEL_ON_CLIENT_DISCONNECT` | true | bool | (Planned — future phase) |
| `BARGE_IN_FRIENDLY` | true | bool | (Existing — unchanged) |

## Timeout Policy

### Queue Wait Timeout
- Applies before request becomes the active synthesis
- Raises `QueueTimeoutError` (subclass of `asyncio.TimeoutError`)
- Does not call backend, does not consume worker
- Request removed cleanly from queue
- Logs exact wait duration and queue depth

### Synthesis Timeout
- Begins when active backend synthesis starts
- Deadline checked before each chunk read in progressive streaming loop
- Raises `asyncio.TimeoutError` with `synthesis_timeout` log event
- Does NOT emit successful AudioStop after timeout
- Backend stream cleaned up (via `with` block `__exit__`)

## HTTP 503 / Backend-Busy Policy

- `S2BackendBusyError` raised when backend returns HTTP 503
- Detected in `S2StreamResult.__enter__` (connection) and `__next__` (mid-stream)
- `S2ClientError` now carries optional `status_code` for diagnostics
- Non-503 HTTP errors raise `S2ClientError` (not retried)
- Streaming function catches `S2BackendBusyError` separately from generic errors
- Logged with `backend_busy` event including `audio_start_emitted` flag

## Terminal Behavior by Request Type

| Scenario | Emitted | Notes |
|----------|---------|-------|
| Success (legacy) | AudioStart → AudioChunk* → AudioStop | No SynthesizeStopped |
| Success (streaming) | AudioStart → AudioChunk* → AudioStop → SynthesizeStopped | |
| Queue rejection | Connection error | `QueueFullError` raised immediately |
| Queue timeout | `QueueTimeoutError` | No backend call |
| Synthesis timeout | No AudioStop | Error logged, stream cleaned up |
| Backend 503 | No AudioStop | Error logged with `audio_start_emitted` |
| Backend error | No AudioStop | Generic S2ClientError |
| Client disconnect | Incomplete | GeneratorExit cleans up stream |
| Cancel-on-new | Previous request cancelled | New request proceeds |

## State Machine

```
REQUEST → [reject if full]
        → admitted
        → waiting (FIFO semaphore)
          → [timeout if exceeds S2_QUEUE_WAIT_TIMEOUT_SEC]
          → [cancel if disconnected]
        → active (acquired semaphore)
          → [timeout if exceeds S2_SYNTHESIS_TIMEOUT_SEC]
          → [cancel if CANCEL_ON_NEW_REQUEST + new request]
        → complete / error / timeout
        → released (semaphore freed, counters decremented)
```

## Cleanup Guarantees

- Queue counters (`pending`, `depth`) always return to zero
- `asyncio.Semaphore` always released in `finally`
- `with client.generate_stream()` ensures stream `__exit__` on all paths
- `cancel_waiting()` removes all entries for a connection
- `cancel_active_if_matches()` cancels the active asyncio Task

## Structured Log Events

| Event | Fields |
|-------|--------|
| `queue_request_received` | synthesis_id, connection_id, queue_depth, max_queue_size |
| `queue_admitted` | synthesis_id, connection_id, queue_depth, max_queue_size |
| `queue_rejected` | synthesis_id, connection_id, queue_depth, reason=queue_full |
| `queue_wait_started` | synthesis_id, connection_id, queue_depth |
| `queue_wait_timeout` | synthesis_id, connection_id, queue_depth, wait_timeout_sec |
| `queue_cancelled` | synthesis_id, connection_id, reason=cancelled_while_waiting |
| `queue_started` | synthesis_id, connection_id, queue_depth |
| `queue_depth_changed` | synthesis_id, connection_id, queue_depth |
| `backend_busy` | retry_count, max_retries, audio_start_emitted |
| `synthesis_timeout` | elapsed_ms, pcm_bytes_received, chunk_count, audio_start_emitted |

## Phase 9.5 Handoff

- Progressive LLM phrase synthesis (next phase)
- Retry loop in handler for 503 (wrapping streaming calls with retry count)
- CANCEL_ON_CLIENT_DISCONNECT full wiring
- End-to-end failure/recovery verification against real Home Assistant

## Rollback

```bash
# Rollback wrapper to pre-Phase-9 image:
docker pull ghcr.io/sorilo/wyoming-s2cpp-tts:sha-22db725
# Backend unchanged: ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd
```

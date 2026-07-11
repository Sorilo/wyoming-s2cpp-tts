# Phase 9: Deterministic Queue, Busy Retries, and Timeout Policy

## Queue Algorithm

Explicit FIFO deque of `asyncio.Future` objects — no `asyncio.Semaphore`.

**How it works:**
1. Admission under lock — reject if `depth >= max_size`
2. If no active worker, become active immediately
3. Otherwise, append a future to the FIFO waiter deque
4. Active worker completes → resolve next waiter's future
5. Cancellation removes the waiter future immediately (under lock)
6. Timeout catches `asyncio.wait_for(future)` and removes from deque

**Guarantees:**
- No semaphore over-release possible (no semaphore at all)
- Cancel removes waiter immediately (future.cancel() + deque removal)
- Exactly one active operation at all times
- Counters cannot go negative
- No permit-leak paths

## Configuration

| Env Var | Default | Range | Description |
|---------|---------|-------|-------------|
| `MAX_QUEUE_SIZE` | 3 | ≥ 1 | Queue capacity (includes active) |
| `S2_QUEUE_WAIT_TIMEOUT_SEC` | 30 | ≥ 0, ≤ 300 | Max seconds waiting in queue |
| `S2_SYNTHESIS_TIMEOUT_SEC` | 120 | ≥ 0.1, ≤ 600 | Max seconds for backend synthesis |
| `S2_BACKEND_BUSY_MAX_RETRIES` | 3 | ≥ 1, ≤ 10 | Additional retries after initial attempt |
| `S2_BACKEND_BUSY_RETRY_DELAY_MS` | 200 | ≥ 0, ≤ 10000 | Milliseconds between retries |
| `CANCEL_ON_NEW_REQUEST` | false | bool | Cancel active + queued on new request |

## HTTP 503 Retry Semantics

- `S2_BACKEND_BUSY_MAX_RETRIES` = additional retries after initial try
- Default 3 → at most **4 total attempts** (1 initial + 3 retries)
- Retry only `S2BackendBusyError` (HTTP 503)
- Retry only before PCM observed and before AudioStart emitted
- Uses cancellation-aware `asyncio.sleep` for inter-attempt delay
- All attempts share the original synthesis deadline
- Each attempt owns a complete `with client.generate_stream()` lifecycle
- Exhaustion cleans up, releases worker, logs `backend_busy_exhausted`

## Synthesis Deadline

- Single monotonic deadline from `S2_SYNTHESIS_TIMEOUT_SEC`
- Covers: connection, headers, 503 retries + delays, buffering, progressive reads
- Checked before each `await asyncio.to_thread(_read_stream_chunk, stream)`
- On timeout: raises `asyncio.TimeoutError`, logged as `synthesis_timeout`
- Stream `__exit__` handles cleanup (via `with` block)
- No successful AudioStop emitted after timeout

## State Machine

```
REQUEST → [reject if depth >= max_size]
        → admitted (depth++)
        → [active if slot free] OR [appended to waiter deque]
        → waiting (await future)
          → [timeout → remove from deque, raise QueueTimeoutError]
          → [cancel → remove from deque, raise CancelledError]
        → active (future resolved)
          → [running operation]
          → complete → resolve next waiter future
        → released (depth--, pending--)
```

## Client Disconnect

- `cancel_waiting(connection_id)` — removes all waiters for connection under lock
- `cancel_active_if_matches(synthesis_id)` — cancels active asyncio Task
- Wired via `CANCEL_ON_NEW_REQUEST` handler checks
- Full `CANCEL_ON_CLIENT_DISCONNECT` wiring deferred to Phase 9.5

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
| `backend_busy` | attempt, max_attempts, pcm_observed, audio_start_emitted |
| `backend_busy_retry` | retry_count, max_total_attempts, delay_ms |
| `backend_busy_exhausted` | retry_count, max_total_attempts, pcm_observed |
| `synthesis_timeout` | elapsed_ms, pcm_bytes_received, chunk_count, audio_start_emitted |

## Phase 9.5

Progressive LLM phrase synthesis only. All Phase 9 reliability work (queue,
retries, timeouts, disconnect) is complete.

## Rollback

```bash
docker pull ghcr.io/sorilo/wyoming-s2cpp-tts:sha-22db725
# Backend unchanged: ghcr.io/sorilo/wyoming-s2cpp-tts-backend:sha-edf89bd
```

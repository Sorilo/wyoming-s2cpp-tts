# Phase 9B Speech Scheduler Domain Implementation Plan

> **For Hermes:** Implement this plan test-first in small, behavior-preserving slices. Do not combine Phase 9B with deferred scheduling features.

## Goal

Extract the queue, request identity, lifecycle ownership, and synthesis-session boundaries currently embedded in `app/wyoming_server.py` into explicit domain objects without changing externally observable behavior.

## Architecture

Add a small `app/speech/` domain package. `SpeechScheduler` becomes the sole owner of admission, FIFO activation, queue depth, active task identity, cancellation, and release. Wyoming handlers remain protocol adapters: they create a `SpeechRequest`, submit a synthesis operation, and preserve the existing event/error sequence. Backend generation and audio forwarding remain unchanged.

The migration is an internal refactor only. It must preserve one active synthesis, bounded FIFO capacity, current retry/deadline semantics, cancellation/disconnect cleanup, structured event names and fields, and Wyoming output ordering.

## Non-goals

Phase 9B does **not** implement:

- semantic priority ordering or priority queues;
- replacement or deduplication behavior;
- interrupt-policy or new-request behavior changes;
- progressive phrase queues or phrase-boundary synthesis;
- barge-in or physical playback interruption;
- admin HTTP, liveness, readiness, status, or metrics endpoints;
- Phase 9C, Phase 9.5, Phase 10, or Phase 11 work;
- backend, model, voice, container, template, or Home Assistant changes.

Reserved metadata must remain inert and must not affect FIFO order.

## Current architecture

`app/wyoming_server.py` currently combines several responsibilities:

- `SingleWorkerSynthesisQueue` (around lines 929-1084) owns admission, waiter futures, depth/pending counters, active synthesis/connection/task identity, queue timeout, cancellation, and FIFO handoff.
- `FakeTtsEventHandler` creates synthesis IDs and nested operation closures for legacy and streaming requests.
- `_run_operational()` maps queue/backend/deadline failures to Wyoming `Error` events.
- `disconnect()` cancels waiting and active work by connection.
- backend-busy retry and the single monotonic synthesis deadline live in the existing synthesis path and must not move or change during the initial scheduler extraction.
- tests in `tests/test_phase_9_queue_busy_timeouts.py`, `tests/test_queue_behavior.py`, streaming/compatibility tests, and disconnect tests lock down the behavior.

The principal refactor risk is changing timing or ownership while moving code, especially around waiter cancellation, active-task assignment, terminal errors, and `AudioStart`/`AudioStop`/`synthesize-stopped` ordering.

## Proposed domain objects

### `SpeechMetadata`

An immutable dataclass carrying optional descriptive metadata only:

- `voice: str | None`
- `trigger: Literal["legacy", "streaming"]`
- `text_fingerprint: str | None`
- reserved inert fields such as `semantic_priority: int | None` and `replacement_key: str | None`

Reserved fields are stored for future compatibility but are never consulted by Phase 9B scheduling.

### `SpeechRequest`

An immutable dataclass representing one admitted unit of speech:

- `synthesis_id: str`
- `connection_id: str`
- `text: str` declared with `field(repr=False)`
- `metadata: SpeechMetadata`
- `created_monotonic: float`

It validates non-empty IDs. Synthesis requires text in memory, but plaintext text must never appear in dataclass `repr`, scheduler snapshots, structured logs, log-facing exceptions, debug output, or lifecycle observability. Safe diagnostics may contain synthesis/connection IDs, text fingerprint and length, voice, trigger, timestamps, lifecycle state, and terminal reason.

### `ScheduledSpeech`

A scheduler-owned mutable record:

- `request: SpeechRequest`
- `state: SpeechState`
- `admitted_monotonic: float`
- `started_monotonic: float | None`
- `completed_monotonic: float | None`
- `terminal_reason: str | None`
- private waiter future and active task references

No mutable scheduler internals are exposed to handlers. Public snapshots are immutable and must not expose plaintext text, request objects containing text, task/future objects, or other mutable scheduler-owned state.

### `SpeechState`

A closed internal lifecycle enum:

```text
CREATED -> REJECTED
CREATED -> WAITING
WAITING -> ACTIVE
WAITING -> CANCELLED
WAITING -> TIMED_OUT

ACTIVE -> COMPLETED
ACTIVE -> CANCELLED
ACTIVE -> TIMED_OUT
ACTIVE -> FAILED
```

Terminal states are idempotent. `FAILED` represents backend errors, backend-busy retry exhaustion, PCM validation failures, unexpected transport/backend errors, and other non-timeout active-operation failures. A separate terminal reason or error classification records useful detail without proliferating terminal enum values. State transitions occur under the scheduler lock; operation execution occurs outside it.

### `SpeechScheduler`

Owns:

- capacity and queue-wait timeout;
- one active `ScheduledSpeech`;
- a FIFO deque of waiting `ScheduledSpeech` records;
- admission/rejection;
- activation and exact FIFO handoff;
- queue wait timeout;
- cancellation by synthesis ID or connection ID;
- depth/pending snapshots;
- admission-latency observability;
- operation execution through `run(request, operation)`.

The explicit public boundary used by Wyoming handlers consists of:

- `run(request, operation)`;
- immutable `snapshot()` data;
- cancellation by synthesis ID;
- cancellation by connection ID; and
- a public operation implementing the existing `CANCEL_ON_NEW_REQUEST` compatibility behavior without handler reads of `_depth`, `_active`, `_active_task`, `_active_synthesis_id`, `_active_connection_id`, `_pending`, or `_waiters`.

Exact operation names may differ if implementation tests demonstrate a cleaner boundary, but handler code must neither read nor mutate scheduler-private fields.

It preserves `QueueFullError` and `QueueTimeoutError` initially so the protocol boundary remains unchanged. A compatibility alias for `SingleWorkerSynthesisQueue` may exist for one migration slice only and must be removed before Phase 9B acceptance unless downstream imports require a documented deprecation.

### `SynthesisSession`

A protocol-adapter-owned record for one Wyoming synthesis lifecycle:

- `request: SpeechRequest`
- resolved voice and trigger;
- whether `AudioStart` was emitted;
- whether `AudioStop` was emitted;
- whether the client is connected;
- whether streaming-only `synthesize-stopped` is eligible;
- PCM/chunk counters already maintained by existing helpers;
- optional private generator/resource closer reference;
- exactly-once cleanup/resource-closure state.

The first slice introduces the object without moving backend streaming logic. Later slices use it to remove duplicated legacy/streaming cleanup state while preserving exact wire ordering.

## Required invariants

1. **FIFO:** activation order equals successful admission order; reserved metadata cannot reorder work.
2. **Capacity:** `MAX_QUEUE_SIZE` includes the active request exactly as in Phase 9.
3. **Single worker:** no more than one operation is active.
4. **Counters:** depth and pending never become negative and return to zero after all terminal paths.
5. **Busy retries:** `S2_BACKEND_BUSY_MAX_RETRIES` remains additional retries; production `10` means 11 total attempts. Retry remains limited to pre-PCM/pre-`AudioStart` HTTP 503 responses.
6. **Deadlines:** queue wait uses `S2_QUEUE_WAIT_TIMEOUT_SEC`; all backend attempts and retry delays share the existing `S2_SYNTHESIS_TIMEOUT_SEC` monotonic deadline.
7. **Cancellation:** waiting cancellation removes the entry immediately; active cancellation targets the matching task; connection disconnect cancels that connection's waiting and active work.
8. **Recovery:** every rejection, timeout, cancellation, disconnect, or backend failure releases ownership and allows the next valid request to run.
9. **Wyoming ordering:** legacy success remains `AudioStart -> AudioChunk* -> AudioStop`; streaming success remains `AudioStart -> AudioChunk* -> AudioStop -> synthesize-stopped`.
10. **Controlled failures:** queue full, queue timeout, backend busy exhaustion, synthesis timeout, and backend errors retain current Wyoming error codes and do not emit false successful terminal events.
11. **Observability:** existing structured event names/required fields remain; any new lifecycle/admission metric is additive.
12. **Active failure lifecycle:** synthesis timeout enters `TIMED_OUT`; other active-operation failures enter `FAILED`, release ownership exactly once, and preserve their existing Wyoming error mapping.
13. **No plaintext exposure:** request text never appears in dataclass `repr`, snapshots, structured logs, log-facing exceptions, debug output, or lifecycle observability.

## Migration sequence

### Slice 1: Characterization tests and domain value objects

**Create:**

- `app/speech/__init__.py`
- `app/speech/models.py`
- `tests/test_speech_models.py`

**Modify only if needed:**

- no runtime call sites yet.

**Test first:**

- valid and invalid request IDs;
- immutable metadata/request behavior;
- default metadata is inert;
- request text is excluded from `repr`;
- lifecycle transition table rejects illegal transitions;
- terminal transitions are idempotent;
- active non-timeout failure transitions to `FAILED`;
- snapshots exclude futures/tasks, request objects, and plaintext text.

**Verification:**

```bash
python -m pytest tests/test_speech_models.py -q
```

Expected: new tests pass; no production behavior changes.

### Slice 2: Extract scheduler behind equivalent API

**Create:**

- `app/speech/scheduler.py`
- `tests/test_speech_scheduler.py`

**Modify:**

- `app/wyoming_server.py` only to import scheduler errors/types after equivalence is proven.

**Test first:** copy behavior expectations, not implementation details, from `tests/test_queue_behavior.py` and the queue sections of `tests/test_phase_9_queue_busy_timeouts.py`:

- first request starts immediately;
- admitted waiters start FIFO;
- capacity includes active work;
- full requests reject without depth changes;
- timed-out waiters are removed exactly once;
- cancelled waiters are removed exactly once;
- active completion/cancellation hands off to the next waiter;
- active failure hands off to the next waiter exactly once;
- connection cancellation affects only matching requests;
- simultaneous completion/cancellation leaves consistent counters;
- semantic metadata cannot reorder work;
- snapshots are immutable and plaintext-safe.

Implement `SpeechScheduler.run(request, operation)` with the same lock/future mechanics and exception types. Keep operation execution outside the lock.

**Verification:**

```bash
python -m pytest tests/test_speech_scheduler.py tests/test_queue_behavior.py -q
```

### Slice 3: Route Wyoming handlers through `SpeechRequest`

**Modify:**

- `app/wyoming_server.py`
- `tests/test_phase_9_queue_busy_timeouts.py`
- `tests/test_compatibility_synthesize.py`
- `tests/test_streaming_protocol.py`

Construct one `SpeechRequest` per legacy or accumulated streaming synthesis. Replace handler reads of private queue fields with explicit scheduler methods/snapshots. Keep `_run_operational()` error mapping unchanged.

**Test first:**

- legacy and streaming requests preserve IDs, connection ownership, voice, and trigger;
- `CANCEL_ON_NEW_REQUEST` uses a public scheduler cancellation method;
- handlers never read or mutate scheduler-private fields;
- disconnect cancellation remains connection-scoped;
- full/timeout errors retain exact codes;
- compatibility synthesize deferral still prevents double synthesis.

**Verification:**

```bash
python -m pytest   tests/test_phase_9_queue_busy_timeouts.py   tests/test_compatibility_synthesize.py   tests/test_streaming_protocol.py -q
```

### Slice 4: Introduce `SynthesisSession` without backend changes

**Create:**

- `app/speech/session.py`
- `tests/test_synthesis_session.py`

**Modify:**

- `app/wyoming_server.py`

Wrap existing per-request protocol state and cleanup bookkeeping in `SynthesisSession`. Do not rewrite `synthesize_s2cpp_streaming_tts_events()`, `S2Client`, multipart fields, retry loops, or backend deadlines.

**Test first:**

- exactly-once `AudioStart` and `AudioStop` state;
- streaming-only `synthesize-stopped` eligibility;
- disconnect marks the session terminal and invokes cleanup once;
- generator/resource closure runs once;
- unexpected writes still propagate;
- cancellation after partial PCM does not emit a false `AudioStop`;
- buffered and progressive paths preserve event sequences.

**Verification:**

```bash
python -m pytest   tests/test_synthesis_session.py   tests/test_wyoming_streaming.py   tests/test_phase_7_5_streaming.py   tests/test_wyoming_s2cpp_backend.py -q
```

### Slice 5: Add scheduler lifecycle observability

**Modify:**

- `app/speech/scheduler.py`
- `app/observability.py` only if a helper/schema update is required
- `tests/test_observability.py`
- `tests/test_speech_scheduler.py`

Preserve all Phase 9 queue events and fields. Add only additive fields/events for:

- lifecycle state;
- admission latency (`started_monotonic - admitted_monotonic`);
- terminal reason;
- scheduler snapshot counters.

Do not log request text or activate semantic metadata.

**Verification:**

```bash
python -m pytest tests/test_speech_scheduler.py tests/test_observability.py -q
```

### Slice 6: Remove compatibility internals and prove parity

**Modify:**

- `app/wyoming_server.py`
- `app/speech/__init__.py`
- tests that imported `SingleWorkerSynthesisQueue` directly
- `docs/ARCHITECTURE.md`
- `docs/ROADMAP.md`
- `TODO.md`
- `CHANGELOG.md`
- `docs/NEXT_GOAL_PROMPTS.md`

Remove handler access to `_depth`, `_active_synthesis_id`, `_active_task`, and waiter internals. Remove the compatibility alias if no supported caller requires it. Keep public configuration names and defaults unchanged.

**Focused verification:**

```bash
python -m pytest   tests/test_speech_models.py   tests/test_speech_scheduler.py   tests/test_synthesis_session.py   tests/test_queue_behavior.py   tests/test_phase_9_queue_busy_timeouts.py   tests/test_compatibility_synthesize.py   tests/test_streaming_protocol.py   tests/test_wyoming_streaming.py   tests/test_phase_7_5_streaming.py   tests/test_observability.py -q
```

Then run the complete repository suite once because Phase 9B changes runtime architecture:

```bash
python -m pytest -q
```

The expected count may grow beyond 876; acceptance requires zero failures and zero unexpected skips.

## Test-first strategy

For each slice:

1. Add a failing characterization/domain test.
2. Run only that test and confirm it fails for the intended reason.
3. Implement the smallest behavior-preserving change.
4. Run the focused test set.
5. Run `git diff --check`.
6. Commit the slice independently with no unrelated cleanup.

Use deterministic `asyncio.Event` coordination instead of sleeps. Use a fake monotonic clock where timing values are asserted. Never require a real backend in unit/integration tests.

## Rollback strategy

Phase 9B is a source-only refactor until separately authorized for image publication/deployment.

- Each slice is independently revertible.
- If focused parity tests regress, revert the current slice rather than adding compatibility behavior not in scope.
- If final full-suite parity fails, do not publish an image; return to the last green slice.
- Production remains on Phase 9 wrapper `sha-7db26b7` and backend `sha-6e629d0` throughout planning and implementation review.
- No database, persistent-state, template, model, voice, or Home Assistant migration is involved.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Lost waiter or double counter decrement | Transition under one lock; tests for timeout/cancel/complete races |
| FIFO drift from future metadata | Assert reserved metadata never affects deque order |
| Wrong active-task cancellation | Scheduler owns task identity; connection/synthesis-target tests |
| Changed Wyoming terminal ordering | Golden event-sequence tests for legacy, streaming, timeout, and disconnect |
| Deadline accidentally reset during extraction | Keep backend retry/deadline code unmoved; retain Phase 9 timeout tests |
| Duplicated synthesis during compatibility events | Preserve and test deferral/fallback logic |
| Resource leak on partial stream | Session cleanup is idempotent; retain disconnect/recovery tests |
| Observability breaks operational tooling | Existing names/fields are compatibility requirements; additive changes only |
| Monolithic extraction becomes too large | Six small commits with focused gates and explicit non-goals |

## Acceptance criteria

Phase 9B implementation is complete only when:

- explicit `SpeechRequest`, `SpeechMetadata`, `ScheduledSpeech`, `SpeechScheduler`, and `SynthesisSession` objects exist with documented ownership;
- `SpeechScheduler` exclusively owns queue state and handlers do not access scheduler private fields;
- the lifecycle model has deterministic, tested terminal transitions;
- active timeout and non-timeout failures are distinguished as `TIMED_OUT` and `FAILED`, with idempotent terminal handling and a separate reason where useful;
- FIFO, capacity, one-active-worker, busy retries, queue/synthesis deadlines, cancellation, disconnect recovery, and error mappings are behaviorally unchanged;
- legacy and streaming Wyoming event ordering is unchanged;
- reserved semantic metadata is demonstrably inert;
- admission latency is observable without logging plaintext;
- request plaintext is absent from reprs, snapshots, structured logs, log-facing exceptions, debug output, and lifecycle observability;
- focused tests and the final full suite pass with zero failures;
- no backend, model, image, template, voice, or Home Assistant change occurs;
- no deferred Phase 9C/9.5/10/11 behavior is implemented;
- architecture, roadmap, TODO, changelog, and next-goal documents accurately reflect the completed refactor.

## Planning decision

This document authorizes planning and review only. It does not authorize Phase 9B runtime implementation, image publication, production deployment, or any deferred feature.

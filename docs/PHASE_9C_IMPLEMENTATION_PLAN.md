# Phase 9C: Graceful Shutdown & Optional Admin Visibility — Implementation Plan

> **For Hermes:** Implement this plan test-first in small, independently revertible slices. Keep production unchanged until implementation is verified and merged.

**Goal:** Add a single service-lifecycle owner that makes shutdown safe, bounded, idempotent, and observable, and optionally expose a read-only admin HTTP listener with sanitized liveness/readiness/status/metrics endpoints.

**Architecture:** The current wrapper already has a clear separation between the Wyoming protocol adapter (`app/wyoming_server.py`), scheduler/domain state (`app/speech/`), backend client (`app/s2_client.py`), and structured observability (`app/observability.py`, `app/metrics.py`). Phase 9C should add one small lifecycle boundary that owns startup readiness, shutdown/drain sequencing, signal handling, and optional admin HTTP serving. The Wyoming server should remain the source of synthesis behavior; the new lifecycle owner should orchestrate when the Wyoming listener is accepting work, when readiness flips, and how queued/active work is drained or cancelled.

**Tech Stack:** Python `asyncio`, existing Wyoming TCP server, current `SpeechScheduler` / `SynthesisSession` domain objects, existing logging/metrics helpers, and a minimal HTTP implementation if no lightweight dependency is already present.

---

## Current state recap

From the live repository state:

- `app/main.py` simply calls `setup_logging()` and `run_server(Settings.from_env())`.
- `app/wyoming_server.py` currently starts an `AsyncTcpServer`, creates a `SpeechScheduler`, and blocks forever on `asyncio.Event().wait()`.
- Shutdown today is effectively `KeyboardInterrupt` only; there is no explicit lifecycle state machine, no readiness endpoint, and no admin HTTP listener.
- `SpeechScheduler` already owns queue admission, FIFO activation, cancellation, and release.
- `SynthesisSession` already tracks per-request cleanup, connection state, and cancellation eligibility.
- Structured observability exists, but no lifecycle snapshot / admin surface exists yet.

This means Phase 9C should mostly add orchestration around existing domain objects rather than reworking synthesis behavior.

---

## Strict non-goals

Phase 9C does **not**:

- change the backend model, quantization, or voice profiles;
- modify Home Assistant or the Wyoming protocol itself;
- publish images or change GHCR tags;
- modify Unraid templates or production containers;
- expose mutating admin endpoints;
- introduce a general-purpose web framework solely for the admin port;
- begin Phase 9.5, Phase 10, or Phase 11 work;
- weaken privacy, cardinality, queue, or shutdown invariants to make the implementation easier.

---

## Phase 9C contracts to implement

### Lifecycle model

Introduce an explicit lifecycle owner with a small closed state model, such as:

- `STARTING`
- `RUNNING`
- `DRAINING`
- `STOPPING`
- `STOPPED`
- `FAILED`

Exact names may differ, but transitions must be explicit and testable.

Required semantics:

- startup begins not-ready;
- a startup sequence may transition `STARTING -> RUNNING` only after the Wyoming listener and required internal components are initialized;
- a startup failure may transition `STARTING -> FAILED`;
- SIGTERM/SIGINT initiate shutdown exactly once;
- readiness becomes false immediately on shutdown start;
- draining may transition `RUNNING -> DRAINING`;
- draining may transition `DRAINING -> STOPPING` once queued work is cancelled and the active grace timer is running;
- shutdown completion may transition `STOPPING -> STOPPED`;
- shutdown failure may transition `RUNNING` or `DRAINING` or `STOPPING -> FAILED`;
- no new Wyoming work is admitted after shutdown begins;
- the Wyoming listener stops accepting new connections;
- in-flight requests follow one deterministic drain/cancel policy;
- shutdown has a bounded grace period;
- backend streams / generators / resource closers are not leaked;
- waiting requests are released, not orphaned;
- active work cannot block process exit indefinitely;
- repeated shutdown calls/signals are idempotent;
- shutdown failures are surfaced and produce a non-zero exit where appropriate.

### Drain policy

Use the current architecture’s safest deterministic policy unless tests show a better one:

1. Mark service draining and readiness false.
2. Stop accepting new Wyoming connections and new speech admissions.
3. Reject new requests on already-open connections with a controlled service-shutting-down response when safe.
4. Cancel queued/waiting requests and release scheduler state.
5. Let the active synthesis finish for a configurable grace period.
6. If the grace period expires, cancel the active synthesis and close its backend resources exactly once.
7. Close remaining client connections and finish process shutdown.

The implementation must define and test:

- queue-waiting request behavior;
- active request behavior before `AudioStart`;
- active request behavior after `AudioStart`;
- whether `AudioStop` / `synthesize-stopped` can be emitted during cancellation;
- client-visible Wyoming termination/error behavior;
- duplicate shutdown / duplicate disconnect cleanup prevention;
- how active ownership and scheduler depth return to zero;
- interaction with `CANCEL_ON_CLIENT_DISCONNECT`.

Important: do **not** emit a misleading successful terminal sequence for a cancelled partial stream.

### Shutdown configuration

Add a small validated config surface for shutdown grace handling. Prefer a minimal set such as:

- `SHUTDOWN_GRACE_TIMEOUT_SEC`

Optional extras are acceptable only if clearly justified.

Requirements:

- defaults must be safe for Docker/Unraid;
- shutdown must never be unbounded by default;
- do not rename existing env vars;
- validate type/range at startup.

### Optional admin HTTP interface

Implement only as a separate, optional, read-only listener.

Required defaults and safety:

- disabled by default;
- loopback-bound by default (`127.0.0.1`) unless a specific safe container-network reason is proven;
- independent of the Wyoming port;
- no plaintext synthesis, raw audio, secrets, tokens, or env dumps;
- lightweight and non-blocking;
- governed by the same lifecycle owner as the Wyoming server;
- no mutating actions.

Suggested config surface:

- `ADMIN_HTTP_ENABLED=false`
- `ADMIN_HTTP_HOST=127.0.0.1`
- `ADMIN_HTTP_PORT=<documented non-conflicting port>`

If the repo’s conventions suggest a different naming style, keep it consistent but preserve the same semantics.

### Required admin endpoints

#### `GET /livez`

Purpose: liveness.

Semantics:

- 200 while the process/event loop is alive and serving the endpoint;
- may remain 200 while draining;
- minimal response;
- must not claim dependency health it cannot prove.

#### `GET /readyz`

Purpose: traffic readiness.

Semantics:

- non-200, preferably 503, during startup, draining, stopping, stopped, or failed states;
- 200 only when the service is ready to accept new Wyoming work;
- no live TTS generation on probe;
- no expensive backend call per probe.

If backend readiness is reported, it must be bounded/cached/passive.

#### `GET /status`

Purpose: sanitized JSON operational snapshot.

Should include only non-sensitive fields such as:

- lifecycle state;
- readiness;
- uptime;
- wrapper version/revision when available;
- scheduler depth;
- scheduler pending count;
- active synthesis presence or a sanitized identifier;
- active connection count;
- configured maximum queue size;
- cumulative request/success/failure/cancellation/timeout counters if available;
- passive backend health/outcome if available;
- whether admin HTTP is enabled.

Must never include plaintext text, raw request bodies, audio, secrets, tokens, full env dumps, or mutable async objects.

#### `GET /metrics`

Pick one stable format and make it explicit:

- Prometheus exposition text if implemented correctly; or
- sanitized JSON if the repo already strongly favors JSON metrics.

Do not call JSON “Prometheus” unless it really is Prometheus exposition with a correct content type.

Metrics may include:

- process uptime;
- readiness;
- lifecycle state;
- scheduler depth and pending count;
- active synthesis gauge;
- active Wyoming connections;
- admitted / rejected / completed / cancelled / timed-out / failed totals;
- backend-busy retry totals;
- safe duration summaries only if already supported.

Avoid unbounded labels and high-cardinality dimensions.

---

## Proposed implementation shape

Add a small orchestration layer, likely one of:

- `ServiceLifecycle`
- `ShutdownCoordinator`
- `AdminHttpServer`
- an immutable operational snapshot type

Exact names may differ, but there must be one clear owner coordinating:

- signal receipt;
- lifecycle transitions;
- readiness;
- Wyoming server start/stop;
- optional admin HTTP start/stop;
- graceful drain cancellation;
- final shutdown completion.

Suggested file layout:

- `app/lifecycle.py` or similar for lifecycle state + coordinator
- `app/admin_http.py` or similar for the optional admin listener
- `app/observability.py` / `app/metrics.py` for sanitized snapshot helpers if needed
- `app/wyoming_server.py` for wiring the coordinator into server startup and shutdown
- `app/config.py` for new shutdown/admin config
- `tests/` for lifecycle, admin endpoint, and shutdown behavior coverage

Keep the admin interface and lifecycle orchestration small; do not introduce a large web framework just for four read-only endpoints.

---

## Implementation slices

### Slice 1: Characterize current startup/shutdown behavior

**Objective:** Lock down the current no-lifecycle baseline before changing behavior.

**Files:**
- `tests/test_wyoming_server.py`
- `tests/test_config.py`
- `tests/test_observability.py` if needed for snapshot expectations

**Tests to add/adjust:**
- server startup is still non-ready until initialized;
- current shutdown path is KeyboardInterrupt-based only;
- no admin endpoints exist yet;
- settings parsing rejects invalid shutdown/admin env values once added.

**Verification:**

```bash
python -m pytest tests/test_wyoming_server.py tests/test_config.py -q
```

### Slice 2: Add lifecycle state and shutdown coordinator

**Objective:** Introduce a single lifecycle owner with idempotent transitions and bounded shutdown semantics.

**Files:**
- Create: `app/lifecycle.py`
- Modify: `app/main.py`
- Modify: `app/wyoming_server.py`
- Modify: `app/config.py`
- Test: `tests/test_lifecycle.py`

**Tests first:**
- initial state is not-ready;
- readiness flips true only after initialization completes;
- shutdown transitions happen once;
- repeated shutdown calls are idempotent;
- shutdown timeout is enforced;
- drain completion and forced cancellation are both observable;
- lifecycle snapshots are sanitized.

**Implementation notes:**
- encapsulate signal handling via `asyncio` signal callbacks where supported;
- avoid stopping the event loop from an unsafe callback;
- make the coordinator awaitable so `run_server()` can block until termination;
- ensure a failure during startup or shutdown yields a non-success exit code.

**Verification:**

```bash
python -m pytest tests/test_lifecycle.py -q
```

### Slice 3: Wire lifecycle into Wyoming server startup/shutdown

**Objective:** Make the Wyoming listener and scheduler obey the new lifecycle state.

**Files:**
- `app/wyoming_server.py`
- `tests/test_wyoming_server.py`
- `tests/test_shutdown_behavior.py` (new)

**Tests first:**
- readiness is false before listener init;
- readiness is true after listener init;
- SIGTERM/SIGINT begins shutdown once;
- no new queue admissions occur after draining begins;
- queued work is cancelled and released;
- active work gets the configured grace period;
- active work is cancelled after timeout;
- active ownership and depth return to zero;
- duplicate signals do not double-cancel or double-close;
- waiting requests do not hang on shutdown.

**Implementation notes:**
- keep scheduler ownership in `SpeechScheduler`;
- use `SpeechScheduler.cancel_connection()` / `cancel_synthesis()` / active task cancellation as the mechanism rather than duplicating scheduler internals;
- avoid changing synthesis response ordering except where shutdown requires termination;
- ensure disconnect cleanup and shutdown cleanup are idempotent together.

**Verification:**

```bash
python -m pytest tests/test_wyoming_server.py tests/test_shutdown_behavior.py -q
```

### Slice 4: Define admin snapshot helpers

**Objective:** Add a sanitized operational snapshot structure without serving it yet.

**Files:**
- Create or modify: `app/metrics.py`
- Create or modify: `app/lifecycle.py`
- Modify: `app/speech/scheduler.py` if a new snapshot field is needed
- Test: `tests/test_status_snapshot.py`

**Tests first:**
- snapshot contains only safe fields;
- no plaintext text, raw audio, secrets, env dumps, or task objects;
- scheduler depth/pending and active presence are represented correctly;
- lifecycle state and readiness are included;
- snapshot is stable across repeated calls.

**Verification:**

```bash
python -m pytest tests/test_status_snapshot.py -q
```

### Slice 5: Implement optional admin HTTP server

**Objective:** Serve `/livez`, `/readyz`, `/status`, and `/metrics` on a separate optional port.

**Files:**
- Create: `app/admin_http.py`
- Modify: `app/lifecycle.py`
- Modify: `app/config.py`
- Test: `tests/test_admin_http.py`

**Tests first:**
- disabled by default;
- loopback default host;
- port is configurable and non-conflicting;
- `/livez` returns 200 while running;
- `/readyz` returns 503 during startup/draining/stopping and 200 only when ready;
- `/status` returns sanitized JSON with the expected fields;
- `/metrics` returns the chosen stable format and content type;
- unsupported methods return deterministic errors;
- malformed requests are rejected safely;
- request size / timeout limits are enforced;
- the admin server shuts down with the same lifecycle owner.

**Implementation notes:**
- prefer a compact asyncio HTTP responder if the repo has no safe lightweight HTTP dependency;
- keep parsing strict and response bodies minimal;
- do not implement any mutating admin action;
- make sure endpoint code cannot leak plaintext synthesis text or raw audio.

**Verification:**

```bash
python -m pytest tests/test_admin_http.py -q
```

### Slice 6: Update docs and operational guidance

**Objective:** Record the new shutdown/admin behavior and next-step guidance.

### Deterministic test strategy

Use the existing in-process pytest style already common in this repo: deterministic async primitives, explicit barriers/events/futures, and no arbitrary sleeps for correctness. Prefer narrow focused tests first, then one end-to-end integration test per major slice. Keep the authoritative suite command separate from Unraid-specific shell validation so results stay comparable to the Phase 9B baseline.

### Rollback strategy

If any Phase 9C slice fails review or tests, revert only that slice commit on the implementation branch before continuing. Keep the planning branch and planning PR documentation-only. If a shutdown/admin change creates a stability regression, back out the smallest committed slice that introduced the regression, restore readiness/shutdown behavior to the last known-good state, and re-run the focused slice tests plus the standard suite before proceeding.

**Files:**
- `CHANGELOG.md`
- `TODO.md`
- `docs/ROADMAP.md`
- `docs/NEXT_GOAL_PROMPTS.md`
- `docs/ARCHITECTURE.md`
- `README.md` if startup/admin usage changes

**Tests / checks:**
- update any stale assertions that refer to old startup/shutdown behavior;
- keep docs aligned with actual defaults and env names.

**Verification:**

```bash
python -m pytest -q
```

---

## Key risks and tradeoffs

- **Shutdown race with active synthesis:** The active task and scheduler owner must agree on who cancels what; otherwise double cleanup or orphaned waiters can occur.
- **Signal handling portability:** `asyncio` signal callbacks work differently across environments; keep the code path safe on Linux and degrade gracefully where necessary.
- **Admin endpoint overreach:** The read-only interface must stay tiny; do not add mutating or debugging endpoints that expose sensitive data.
- **False-success terminal events:** Cancelled partial streams must not emit a fake successful `AudioStop`/`synthesize-stopped` sequence.
- **Testing async shutdown:** Use deterministic events/futures/barriers rather than sleeps.

---

## Acceptance criteria checklist

- [ ] Service has an explicit lifecycle owner.
- [ ] Readiness is false before initialization and false again immediately on shutdown.
- [ ] SIGTERM/SIGINT trigger exactly one shutdown sequence.
- [ ] Shutdown is bounded by `SHUTDOWN_GRACE_TIMEOUT_SEC`.
- [ ] Active and queued work are released deterministically.
- [ ] Duplicate shutdown calls are idempotent.
- [ ] Optional admin HTTP is disabled by default.
- [ ] `/livez`, `/readyz`, `/status`, and `/metrics` behave as specified.
- [ ] Admin responses are sanitized and read-only.
- [ ] No production container/template changes are made in the planning phase.
- [ ] Full test suite passes after implementation.

---

## Next step after the plan is approved

Implement this in a separate branch from updated `main`, using small TDD slices and review after each slice. Keep production images unchanged until the implementation PR passes all acceptance criteria.

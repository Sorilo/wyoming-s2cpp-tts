#!/usr/bin/env python3
"""Phase 9 headless live validation client.

Usage:
    SHADOW_CONTAINER=<name> \
    python scripts/phase_9_live_client.py <host> <port> <artifact_dir>
"""
from __future__ import annotations
import asyncio, hashlib, json, os, sys, time, wave
from dataclasses import dataclass, field

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize, SynthesizeVoice


@dataclass
class RequestResult:
    text: str; host: str; port: int
    submit_time: float = 0.0
    audio_start_time: float | None = None
    first_chunk_time: float | None = None
    completion_time: float | None = None
    events: list[dict] = field(default_factory=list)
    pcm: bytearray = field(default_factory=bytearray)
    rate: int = 0; width: int = 0; channels: int = 0
    audio_start_count: int = 0; audio_stop_count: int = 0
    chunk_timestamps: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def audio_start_s(self) -> float | None:
        if self.audio_start_time is not None and self.submit_time is not None:
            return round(self.audio_start_time - self.submit_time, 4)
        return None

    @property
    def first_chunk_s(self) -> float | None:
        if self.first_chunk_time is not None and self.submit_time is not None:
            return round(self.first_chunk_time - self.submit_time, 4)
        return None

    @property
    def duration_s(self) -> float | None:
        if self.completion_time is not None and self.submit_time is not None:
            return round(self.completion_time - self.submit_time, 3)
        return None

    @property
    def pcm_bytes(self) -> int: return len(self.pcm)

    @property
    def audio_duration_s(self) -> float | None:
        if self.rate and self.width and self.channels and self.pcm is not None:
            return round(len(self.pcm) // (self.width * self.channels) / self.rate, 3)
        return None

    @property
    def rtf(self) -> float | None:
        if self.duration_s is not None and self.audio_duration_s is not None and self.audio_duration_s > 0:
            return round(self.duration_s / self.audio_duration_s, 3)
        return None

    @property
    def protocol_valid(self) -> bool:
        if self.audio_start_count != 1: return False
        if self.audio_stop_count != 1: return False
        if any("audio-chunk" in e["type"] and self.audio_start_count == 0 for e in self.events):
            return False
        return True

    @property
    def valid(self) -> bool:
        return (len(self.errors) == 0 and self.protocol_valid
                and self.rate > 0 and self.pcm_bytes > 0
                and self.pcm_bytes % (self.width * self.channels) == 0)

    def to_dict(self) -> dict:
        types = [e["type"] for e in self.events]
        return {
            "text": self.text, "submit_time": self.submit_time,
            "audio_start_s": self.audio_start_s,
            "first_chunk_s": self.first_chunk_s,
            "duration_s": self.duration_s,
            "pcm_bytes": self.pcm_bytes,
            "audio_duration_s": self.audio_duration_s,
            "rtf": self.rtf, "rate": self.rate,
            "width": self.width, "channels": self.channels,
            "audio_start_count": self.audio_start_count,
            "audio_stop_count": self.audio_stop_count,
            "chunk_count": sum(1 for t in types if "audio-chunk" in t),
            "event_count": len(self.events),
            "protocol_valid": self.protocol_valid,
            "valid": self.valid, "errors": self.errors,
        }


async def synthesize(text: str, host: str, port: int, timeout: float = 120.0,
                     voice: str = "cmu_bdl_male_us") -> RequestResult:
    r = RequestResult(text=text, host=host, port=port)
    r.submit_time = time.monotonic()
    try:
        async with AsyncTcpClient(host, port) as tcp:
            await tcp.write_event(Synthesize(
                text=text, voice=SynthesizeVoice(name=voice)).event())
            while True:
                try:
                    ev = await asyncio.wait_for(tcp.read_event(), timeout=timeout)
                except asyncio.TimeoutError:
                    r.errors.append("timeout"); break
                if ev is None: break
                now = time.monotonic()
                r.events.append({"type": ev.type, "time": round(now - r.submit_time, 4)})
                if AudioStart.is_type(ev.type):
                    s = AudioStart.from_event(ev)
                    r.rate, r.width, r.channels = s.rate, s.width, s.channels
                    r.audio_start_time = now; r.audio_start_count += 1
                elif AudioChunk.is_type(ev.type):
                    c = AudioChunk.from_event(ev)
                    if r.first_chunk_time is None: r.first_chunk_time = now
                    if c.rate != r.rate: r.errors.append("chunk rate mismatch")
                    if c.width != r.width: r.errors.append("chunk width mismatch")
                    if c.channels != r.channels: r.errors.append("chunk channels mismatch")
                    if len(c.audio) % (r.width * r.channels) != 0: r.errors.append("chunk not frame-aligned")
                    r.pcm.extend(c.audio)
                    ts = ev.data.get("timestamp", 0) if ev.data else 0
                    if r.chunk_timestamps and ts < r.chunk_timestamps[-1]:
                        r.errors.append("nonmonotonic timestamp")
                    r.chunk_timestamps.append(ts)
                elif AudioStop.is_type(ev.type):
                    r.audio_stop_count += 1; r.completion_time = now; break
    except Exception as e:
        r.errors.append(f"{type(e).__name__}: {e}")
    if r.completion_time is None: r.completion_time = time.monotonic()
    return r


def save_wav(path: str, r: RequestResult) -> None:
    if not r.pcm or not r.rate: return
    with wave.open(path, "w") as w:
        w.setnchannels(r.channels or 1); w.setsampwidth(r.width or 2)
        w.setframerate(r.rate or 44100); w.writeframes(bytes(r.pcm))




def shadow_log_path() -> str:
    """Return the required, externally-followed shadow JSONL log."""
    path = os.environ.get("SHADOW_LOG_PATH")
    if not path:
        raise RuntimeError("SHADOW_LOG_PATH is required")
    return path


def read_json_events(path) -> list[dict]:
    """Read JSON objects in order; discard only an invalid partial trailing line."""
    log_path = os.fspath(path)
    try:
        with open(log_path, "rb") as stream:
            data = stream.read()
    except OSError as exc:
        raise RuntimeError(f"shadow log is not readable: {log_path}: {exc}") from exc
    events = []
    for raw in data.splitlines():
        try:
            item = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def current_event_index(path) -> int:
    return len(read_json_events(path))


def events_since(path, baseline: int) -> list[dict]:
    return read_json_events(path)[baseline:]


def _event_name(item: dict) -> str:
    return str(item.get("event") or item.get("type") or item.get("message") or "")


async def wait_for_event(path, baseline: int, predicate, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        seen = events_since(path, baseline)
        for item in seen:
            if predicate(item):
                return item
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for event; saw {seen[-10:]!r}")
        await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))


async def wait_for_event_count(path, baseline: int, predicate, count: int,
                               timeout: float = 30.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    while True:
        found = [item for item in events_since(path, baseline) if predicate(item)]
        if len(found) >= count:
            return found[:count]
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for {count} events; found {len(found)}: {found[-10:]!r}")
        await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))


def _text_fp(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _is_event(name: str):
    return lambda item: _event_name(item) == name


async def wait_for_identity(path, baseline: int, text: str, event: str,
                            timeout: float = 30.0) -> dict:
    incoming = await wait_for_event(
        path, baseline,
        lambda item: _event_name(item) == "event_in" and item.get("text_fp") == _text_fp(text),
        timeout)
    connection_id = incoming.get("connection_id")
    return await wait_for_event(
        path, baseline,
        lambda item: _event_name(item) == event and item.get("connection_id") == connection_id,
        timeout)


def _identity_keys(events: list[dict], identity: str) -> tuple[set, set]:
    fp = _text_fp(identity)
    connections = {item.get("connection_id") for item in events
                   if item.get("text_fp") == fp or _identity_matches_raw(item, identity)}
    connections.discard(None)
    syntheses = {item.get("synthesis_id") for item in events
                 if item.get("connection_id") in connections and item.get("synthesis_id")}
    return connections, syntheses


def _identity_matches_raw(item: dict, identity: str) -> bool:
    return identity in json.dumps(item, sort_keys=True)


def _identity_matches(events: list[dict], item: dict, identity: str) -> bool:
    connections, syntheses = _identity_keys(events, identity)
    return (_identity_matches_raw(item, identity)
            or item.get("connection_id") in connections
            or item.get("synthesis_id") in syntheses)


def _terminal_depth_zero(events: list[dict]) -> bool:
    depth_events = [item for item in events if _event_name(item) in
                    ("queue_depth_changed", "request_completed", "queue_completed", "synthesis_completed")
                    and ("queue_depth" in item or "depth" in item)]
    return bool(depth_events) and (depth_events[-1].get("queue_depth", depth_events[-1].get("depth")) == 0)


def _ordered(events: list[dict], names: tuple[str, ...], identities: list[str]) -> list[int]:
    selected = [item for item in events if _event_name(item) in names]
    order = [next((i for i, identity in enumerate(identities)
                   if _identity_matches(events, item, identity)), None) for item in selected]
    return [value for value in order if value is not None]


def prove_fifo(events: list[dict], identities: list[str],
               requests: list[RequestResult]) -> tuple[bool, dict]:
    started_order = _ordered(events, ("queue_started",), identities)
    completed_order = _ordered(events,
        ("queue_depth_changed", "request_completed", "queue_completed", "synthesis_completed"), identities)
    expected = list(range(len(identities)))
    completion_sequences = [item.get("sequence") for item in events
                            if _event_name(item) in ("request_completed", "queue_completed", "synthesis_completed")
                            and item.get("sequence") is not None]
    sequence_ok = not completion_sequences or completion_sequences == list(range(1, len(identities) + 1))
    ok = (started_order == expected and completed_order == expected and sequence_ok
          and all(request.valid for request in requests) and _terminal_depth_zero(events))
    return ok, {"started_order": started_order, "completed_order": completed_order,
                "valid_pcm": [request.valid for request in requests],
                "final_depth_zero": _terminal_depth_zero(events)}


def prove_queue_full(events: list[dict], identities: list[str], rejected_identity: str,
                     requests: list[RequestResult], rejected: RequestResult) -> tuple[bool, dict]:
    rejected_events = [item for item in events if _event_name(item) == "queue_rejected"
                       and _identity_matches(events, item, rejected_identity)]
    rejected_lifecycle = [item for item in events
                          if _identity_matches(events, item, rejected_identity)]
    forbidden_events = [item for item in rejected_lifecycle
                        if _event_name(item) in ("backend_start", "audio_out", "queue_started")]
    forbidden = bool(rejected.pcm_bytes or rejected.audio_stop_count or forbidden_events)
    started_order = _ordered(events, ("queue_started",), identities)
    completed_order = _ordered(events, ("queue_depth_changed", "request_completed"), identities)
    expected = list(range(len(identities)))
    ok = (bool(rejected_events) and not forbidden and started_order == expected
          and completed_order == expected and all(request.valid for request in requests)
          and _terminal_depth_zero(events))
    return ok, {"rejection": rejected_events[0] if rejected_events else None,
                "started_order": started_order, "completed_order": completed_order,
                "rejected_pcm": rejected.pcm_bytes, "forbidden_events": forbidden_events,
                "final_depth_zero": _terminal_depth_zero(events)}


def prove_disconnect(events: list[dict], text: str, raw_text: str = "") -> tuple[bool, dict]:
    related = [item for item in events if _identity_matches(events, item, text)]
    disconnected = any(_event_name(item) == "client_disconnected" for item in related)
    cancelled = any(_event_name(item) in ("synthesis_cancelled", "synthesis_cancel_requested")
                    for item in related)
    depth_zero = _terminal_depth_zero(events)
    forbidden_strings = (
        "UnboundLocalError",
        "Task was destroyed but it is pending",
        "Task exception was never retrieved",
        "coroutine was never awaited",
    )
    rendered = raw_text + "\n" + "\n".join(json.dumps(item, sort_keys=True) for item in events)
    warnings = [warning for warning in forbidden_strings if warning in rendered]
    return disconnected and cancelled and depth_zero and not warnings, {
        "client_disconnected": disconnected, "cancelled": cancelled,
        "final_depth_zero": depth_zero, "forbidden_warnings": warnings}


async def behavioral_tests(host: str, port: int, artifact_dir: str) -> dict:
    log_path = shadow_log_path()
    if not os.path.isfile(log_path) or not os.access(log_path, os.R_OK):
        raise RuntimeError(f"SHADOW_LOG_PATH must name a readable file: {log_path}")
    results: dict = {}
    classification = "PASS"
    def record(name: str, status: str, detail=None):
        results[name] = {"status": status, "detail": detail}
        if status == "FAIL":
            nonlocal classification; classification = "FAIL"

    def partial(name: str, reason: str, detail=None):
        nonlocal classification
        if classification == "PASS": classification = "PARTIAL"
        results[name] = {"status": "PARTIAL", "reason": reason, "detail": detail}

    print("=" * 60)
    print(f"Phase 9 Live Smoke: {host}:{port}")
    print("=" * 60)

    # A: Short
    print("\n--- A: Short ---")
    r = await synthesize("The weather is clear and sunny today.", host, port, timeout=60)
    save_wav(f"{artifact_dir}/short.wav", r)
    results["short"] = r.to_dict()
    if r.valid and r.protocol_valid:
        record("short", "PASS", {"pcm_bytes": r.pcm_bytes, "duration_s": r.duration_s})
        print(f"  PASS: {r.pcm_bytes}B, {r.duration_s}s")
    else:
        record("short", "FAIL", {"errors": r.errors, "protocol_valid": r.protocol_valid})
        print(f"  FAIL: {r.errors}")

    # B: Long
    print("\n--- B: Long ---")
    r2 = await synthesize(
        "Good morning. Today we have a full schedule of activities planned. "
        "First, we will review the quarterly results and discuss the upcoming "
        "projects. Then we will break for lunch. Finally, team updates.",
        host, port, timeout=120)
    save_wav(f"{artifact_dir}/long.wav", r2)
    results["long"] = r2.to_dict()
    if r2.valid and r2.to_dict()["chunk_count"] >= 2:
        record("long", "PASS", {"chunks": r2.to_dict()["chunk_count"], "rtf": r2.rtf})
        print(f"  PASS: {r2.to_dict()['chunk_count']} chunks, RTF {r2.rtf}")
    else:
        record("long", "FAIL", {"errors": r2.errors})
        print(f"  FAIL: {r2.errors}")

    # C: FIFO -- every admission is gated by a correlated structured event.
    print("\n--- C: FIFO ---")
    fifo_texts = [f"FIFO-request-{n}-unique-fingerprint-for-tracing" for n in range(1, 4)]
    fifo_baseline = current_event_index(log_path)
    fifo_tasks = []
    fifo_tasks.append(asyncio.create_task(synthesize(fifo_texts[0], host, port, timeout=120)))
    await wait_for_identity(log_path, fifo_baseline, fifo_texts[0], "queue_started", 20)
    fifo_tasks.append(asyncio.create_task(synthesize(fifo_texts[1], host, port, timeout=120)))
    second_wait = await wait_for_identity(log_path, fifo_baseline, fifo_texts[1], "queue_wait_started", 20)
    if second_wait.get("queue_depth") != 2:
        raise AssertionError(f"FIFO request 2 did not establish depth 2: {second_wait}")
    fifo_tasks.append(asyncio.create_task(synthesize(fifo_texts[2], host, port, timeout=120)))
    third_wait = await wait_for_identity(log_path, fifo_baseline, fifo_texts[2], "queue_wait_started", 20)
    if third_wait.get("queue_depth") != 3:
        raise AssertionError(f"FIFO request 3 did not establish depth 3: {third_wait}")
    fifo_requests = await asyncio.gather(*fifo_tasks)
    await wait_for_event(log_path, fifo_baseline,
                         lambda item: _event_name(item) == "queue_depth_changed" and item.get("queue_depth") == 0, 30)
    fifo_events = events_since(log_path, fifo_baseline)
    fifo_ok, fifo_detail = prove_fifo(fifo_events, fifo_texts, fifo_requests)
    for n, request in enumerate(fifo_requests, 1):
        save_wav(f"{artifact_dir}/fifo-request-{n}.wav", request)
        results[f"fifo_{n}"] = request.to_dict()
    record("fifo", "PASS" if fifo_ok else "FAIL", fifo_detail)

    # D: Queue-full -- establish exact depths 1/2/3 before the rejected request.
    print("\n--- D: Queue-full ---")
    queue_texts = [f"Queue-full-request-{n}-deliberately-long-text-for-occupancy" for n in range(1, 4)]
    rejected_text = "Queue-full-request-4-must-be-rejected"
    queue_baseline = current_event_index(log_path)
    queue_tasks = [asyncio.create_task(synthesize(queue_texts[0], host, port, timeout=120))]
    await wait_for_identity(log_path, queue_baseline, queue_texts[0], "queue_started", 20)
    queue_tasks.append(asyncio.create_task(synthesize(queue_texts[1], host, port, timeout=120)))
    q2_wait = await wait_for_identity(log_path, queue_baseline, queue_texts[1], "queue_wait_started", 20)
    if q2_wait.get("queue_depth") != 2: raise AssertionError(f"queue depth 2 not established: {q2_wait}")
    queue_tasks.append(asyncio.create_task(synthesize(queue_texts[2], host, port, timeout=120)))
    q3_wait = await wait_for_identity(log_path, queue_baseline, queue_texts[2], "queue_wait_started", 20)
    if q3_wait.get("queue_depth") != 3: raise AssertionError(f"queue depth 3 not established: {q3_wait}")
    rejected_request = await synthesize(rejected_text, host, port, timeout=20)
    await wait_for_identity(log_path, queue_baseline, rejected_text, "queue_rejected", 20)
    queue_requests = await asyncio.gather(*queue_tasks)
    await wait_for_event(log_path, queue_baseline,
                         lambda item: _event_name(item) == "queue_depth_changed" and item.get("queue_depth") == 0, 30)
    queue_events = events_since(log_path, queue_baseline)
    queue_ok, queue_detail = prove_queue_full(
        queue_events, queue_texts, rejected_text, queue_requests, rejected_request)
    recovery5_text = "Queue-full-request-5-recovery"
    recovery5_baseline = current_event_index(log_path)
    recovery5 = await synthesize(recovery5_text, host, port, timeout=60)
    await wait_for_identity(log_path, recovery5_baseline, recovery5_text, "queue_started", 20)
    await wait_for_event(log_path, recovery5_baseline,
                         lambda item: _event_name(item) == "queue_depth_changed" and item.get("queue_depth") == 0, 20)
    recovery5_proof, recovery5_detail = prove_fifo(
        events_since(log_path, recovery5_baseline), [recovery5_text], [recovery5])
    queue_detail["recovery"] = recovery5_detail
    queue_ok = queue_ok and recovery5_proof
    save_wav(f"{artifact_dir}/post-queue-full-recovery.wav", recovery5)
    record("queue_full", "PASS" if queue_ok else "FAIL", queue_detail)

    # E/F: repeat disconnect lifecycle and recovery proof three times.
    print("\n--- E/F: Disconnect and 3-cycle recovery ---")
    cycles = []
    for cycle in range(1, 4):
        text = f"Disconnect-cycle-{cycle}-long-active-synthesis"
        baseline = current_event_index(log_path)
        raw_baseline = os.path.getsize(log_path)
        got_start = got_chunk = False
        async with AsyncTcpClient(host, port) as tcp:
            await tcp.write_event(Synthesize(text=text, voice=SynthesizeVoice(name="cmu_bdl_male_us")).event())
            while not got_chunk:
                event = await asyncio.wait_for(tcp.read_event(), timeout=30)
                if event is None: break
                if AudioStart.is_type(event.type): got_start = True
                elif AudioChunk.is_type(event.type):
                    chunk = AudioChunk.from_event(event)
                    got_chunk = got_start and bool(chunk.audio)
        await wait_for_identity(log_path, baseline, text, "client_disconnected", 30)
        await wait_for_event(log_path, baseline,
            lambda item: _event_name(item) in ("synthesis_cancelled", "synthesis_cancel_requested"), 30)
        await wait_for_event(log_path, baseline,
            lambda item: _event_name(item) == "queue_depth_changed" and item.get("queue_depth") == 0, 30)
        with open(log_path, "rb") as raw_log:
            raw_log.seek(raw_baseline)
            raw_text = raw_log.read().decode("utf-8", errors="replace")
        lifecycle_ok, lifecycle = prove_disconnect(events_since(log_path, baseline), text, raw_text)
        recovery_text = f"Recovery cycle {cycle} validation."
        recovery_baseline = current_event_index(log_path)
        recovery = await synthesize(recovery_text, host, port, timeout=60)
        await wait_for_identity(log_path, recovery_baseline, recovery_text, "queue_started", 20)
        await wait_for_event(log_path, recovery_baseline,
            lambda item: _event_name(item) == "queue_depth_changed" and item.get("queue_depth") == 0, 20)
        recovery_proof, recovery_detail = prove_fifo(
            events_since(log_path, recovery_baseline), [recovery_text], [recovery])
        cycle_ok = got_start and got_chunk and lifecycle_ok and recovery_proof
        cycles.append({"cycle": cycle, "audio_start": got_start, "nonempty_chunk": got_chunk,
                       "lifecycle": lifecycle, "recovery": recovery_detail, "recovery_valid": recovery.valid,
                       "recovery_pcm": recovery.pcm_bytes, "valid": cycle_ok})
    results["disconnect"] = cycles[0]
    record("disconnect", "PASS" if cycles[0]["valid"] else "FAIL", cycles[0])
    record("recovery_cycles", "PASS" if all(c["valid"] for c in cycles) else "FAIL", {"cycles": cycles})

    results["classification"] = classification
    with open(f"{artifact_dir}/results.json", "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nClassification: {classification}")
    return results


async def run_tests(host: str, port: int, artifact_dir: str) -> dict:
    """Run a generation preflight, then the bounded behavioral suite.

    Results are persisted even when preflight, assertions, or runtime code fail.
    A failed preflight is classified backend_unavailable and no behavioral
    request is attempted.
    """
    os.makedirs(artifact_dir, exist_ok=True)
    result_path = os.path.join(artifact_dir, "results.json")
    results: dict = {"classification": "FAIL", "tests": {}}
    try:
        try:
            backend_preflight = await asyncio.wait_for(
                synthesize("Phase nine isolated backend preflight.", host, port, timeout=45),
                timeout=50,
            )
            results["tests"]["backend_preflight"] = backend_preflight.to_dict()
            if not backend_preflight.valid:
                results.update(failure_type="backend_unavailable",
                               reason="backend preflight did not produce valid audio")
                return results
        except Exception as exc:
            results["tests"]["backend_preflight"] = {
                "status": "FAIL", "reason": f"{type(exc).__name__}: {exc}"}
            results.update(failure_type="backend_unavailable",
                           reason=f"backend preflight failed: {type(exc).__name__}: {exc}")
            return results

        try:
            behavioral = await asyncio.wait_for(
                behavioral_tests(host, port, artifact_dir), timeout=900)
            # Preserve preflight plus all behavioral results.
            preflight = results["tests"]["backend_preflight"]
            results = behavioral
            results.setdefault("tests", {})["backend_preflight"] = preflight
        except asyncio.TimeoutError:
            results.update(failure_type="runtime", section_failure="behavioral_suite",
                           reason="behavioral suite exceeded 900 seconds")
        except AssertionError as exc:
            results.update(failure_type="assertion", section_failure="behavioral_suite",
                           reason=f"AssertionError: {exc}")
        except Exception as exc:
            results.update(failure_type="runtime", section_failure="behavioral_suite",
                           reason=f"{type(exc).__name__}: {exc}")
        return results
    finally:
        # This is deliberately the sole unconditional writer; it retains all
        # completed sections and exact exception reasons.
        with open(result_path, "w") as stream:
            json.dump(results, stream, indent=2)


def main() -> None:
    if len(sys.argv) != 4:
        print("Usage: python scripts/phase_9_live_client.py <host> <port> <artifact_dir>",
              file=sys.stderr); sys.exit(1)
    host, port, artifact_dir = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    os.makedirs(artifact_dir, exist_ok=True)
    results = asyncio.run(run_tests(host, port, artifact_dir))
    c = results.get("classification", "FAIL")
    sys.exit(0 if c == "PASS" else 2 if c == "PARTIAL" else 1)


if __name__ == "__main__": main()

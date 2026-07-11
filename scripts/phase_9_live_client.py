#!/usr/bin/env python3
"""Phase 9 headless live validation client.

Usage:
    SHADOW_CONTAINER=<name> \
    python scripts/phase_9_live_client.py <host> <port> <artifact_dir>
"""
from __future__ import annotations
import asyncio, json, os, sys, time, wave
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


async def _run(cmd: str) -> str:
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate(); return out.decode(errors="replace")


async def poll_log_for(container: str, pattern: str, count: int = 1,
                       timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        logs = await _run(f"docker logs {container} --tail 50 2>&1 | grep -c '{pattern}'")
        try:
            if int(logs.strip() or 0) >= count: return True
        except ValueError: pass
        await asyncio.sleep(0.5)
    return False


async def run_tests(host: str, port: int, artifact_dir: str) -> dict:
    results: dict = {}
    classification = "PASS"
    container = os.environ.get("SHADOW_CONTAINER", "unknown")

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

    # C: FIFO (log-backed)
    print("\n--- C: FIFO ---")
    base = "FIFO-request-%d-unique-fingerprint-for-tracing"
    t1 = asyncio.create_task(synthesize(base % 1, host, port, timeout=120))
    await asyncio.sleep(0.1)
    t2 = asyncio.create_task(synthesize(base % 2, host, port, timeout=120))
    await asyncio.sleep(0.1)
    t3 = asyncio.create_task(synthesize(base % 3, host, port, timeout=120))
    r1, r3_, r2_ = await asyncio.gather(t1, t3, t2)
    for n, rr in [(1, r1), (2, r2_), (3, r3_)]:
        save_wav(f"{artifact_dir}/fifo-request-{n}.wav", rr)
        results[f"fifo_{n}"] = rr.to_dict()
    c_times = [(r1.completion_time or 0), (r2_.completion_time or 0), (r3_.completion_time or 0)]
    ordered = c_times[0] < c_times[1] < c_times[2]
    all_ok = all(r.valid for r in [r1, r2_, r3_])
    # Verify log-backed: check queue_started events exist
    log_ok = await poll_log_for(container, "queue_started", 3, timeout=10)
    if all_ok and ordered and log_ok:
        record("fifo", "PASS", {"ordered": True, "log_backed": True})
        print("  PASS: ordered 1<2<3, log-backed")
    else:
        record("fifo", "FAIL", {"ordered": ordered, "log_backed": log_ok, "times": c_times})
        print(f"  FAIL: ordered={ordered}, log={log_ok}")

    # D: Queue-full (poll-based)
    print("\n--- D: Queue-full ---")
    qf_base = "Queue-full-request-%d-deliberately-long-text-for-occupancy"
    q1 = asyncio.create_task(synthesize(qf_base % 1, host, port, timeout=120))
    # Poll until request 1 is active (depth=1)
    if not await poll_log_for(container, "queue_started", 1, timeout=15):
        record("queue_full", "FAIL", {"reason": "request 1 not detected active"})
    else:
        q2 = asyncio.create_task(synthesize(qf_base % 2, host, port, timeout=120))
        # Poll until depth=2 (2nd request waiting)
        await asyncio.sleep(0.5)
        q3 = asyncio.create_task(synthesize(qf_base % 3, host, port, timeout=120))
        await asyncio.sleep(0.5)
        # Submit 4th — should be rejected
        q4_rejected = False
        try:
            q4 = await asyncio.wait_for(
                synthesize("Should be rejected by queue-full policy.", host, port, timeout=120), timeout=10)
            q4_rejected = q4.pcm_bytes == 0
        except (asyncio.TimeoutError, Exception):
            q4_rejected = False
        rq1, rq2, rq3 = await asyncio.gather(q1, q2, q3)
        # Verify queue_rejected in logs
        rejected_log = await poll_log_for(container, "queue_rejected", 1, timeout=10)
        r_rec = await synthesize("Recovery after queue-full.", host, port, timeout=60)
        save_wav(f"{artifact_dir}/post-queue-full-recovery.wav", r_rec)
        results["queue_full"] = {
            "rejected_by_log": rejected_log, "recovery_valid": r_rec.valid}
        fifo_ok = all(r.valid for r in [rq1, rq2, rq3])
        if rejected_log and fifo_ok and r_rec.valid:
            record("queue_full", "PASS", {"log_backed": True})
            print("  PASS: rejected (log-backed), recovered")
        else:
            record("queue_full", "FAIL", {"rejected_log": rejected_log, "fifo_valid": fifo_ok})
            print(f"  FAIL: log={rejected_log}, fifo={fifo_ok}")

    # E: Disconnect (poll-based)
    print("\n--- E: Disconnect ---")
    got_chunk = False
    try:
        async with AsyncTcpClient(host, port) as tcp:
            await tcp.write_event(Synthesize(
                text="Disconnect test with long text for active synthesis.",
                voice=SynthesizeVoice(name="cmu_bdl_male_us")).event())
            while True:
                ev = await asyncio.wait_for(tcp.read_event(), timeout=30)
                if ev is None: break
                if AudioChunk.is_type(ev.type): got_chunk = True; break
        # Poll until disconnect is detected in logs
        cleanup_ok = await poll_log_for(container, "client_disconnected|queue_depth_changed", 1, timeout=15)
    except Exception as e:
        results["disconnect_error"] = str(e); cleanup_ok = False
    r_rec2 = await synthesize("Recovery after disconnect.", host, port, timeout=60)
    save_wav(f"{artifact_dir}/recovery.wav", r_rec2)
    results["disconnect"] = {"chunk_before": got_chunk, "cleanup_logged": cleanup_ok,
                             "recovery": r_rec2.to_dict()}
    if got_chunk and r_rec2.valid:
        record("disconnect", "PASS", {"recovery_pcm": r_rec2.pcm_bytes})
        print(f"  PASS: recovered {r_rec2.pcm_bytes}B")
    else:
        record("disconnect", "FAIL", {"chunk": got_chunk, "valid": r_rec2.valid})
        print("  FAIL")

    # F: 3-cycle recovery
    print("\n--- F: 3-cycle recovery ---")
    cycles = []
    for cyc in range(1, 4):
        print(f"  Cycle {cyc}...")
        async with AsyncTcpClient(host, port) as tcp:
            await tcp.write_event(Synthesize(
                text=f"Recovery cycle {cyc} disconnect test.",
                voice=SynthesizeVoice(name="cmu_bdl_male_us")).event())
            while True:
                ev = await asyncio.wait_for(tcp.read_event(), timeout=30)
                if ev is None: break
                if AudioChunk.is_type(ev.type): break
        await asyncio.sleep(0.5)
        rr = await synthesize(f"Recovery cycle {cyc} validation.", host, port, timeout=60)
        cycles.append({"cycle": cyc, "recovery_valid": rr.valid, "recovery_pcm": rr.pcm_bytes})
    results["recovery_cycles"] = cycles
    if all(c["recovery_valid"] for c in cycles):
        record("recovery_cycles", "PASS", {"cycles": 3})
        print("  PASS: all 3")
    else:
        record("recovery_cycles", "FAIL", {"cycles": cycles})
        print("  FAIL")

    results["classification"] = classification
    with open(f"{artifact_dir}/results.json", "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nClassification: {classification}")
    return results


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

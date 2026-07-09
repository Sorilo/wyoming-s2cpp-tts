#!/usr/bin/env python3
"""Phase 8B1 Live Verification Harness.

Cancels a long synthesis mid-stream, verifies immediate recovery,
and records client-side timing + audio artifacts.

Usage:
    python3 scripts/live_verify_phase_8b1.py --host 192.168.1.45 --port 10200
"""

import argparse
import asyncio
import hashlib
import json
import os
import struct
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize, SynthesizeStopped

# ── Test texts ────────────────────────────────────────────────────────────────

LONG_TEXT = (
    "Artificial intelligence has transformed the way we interact with technology "
    "in profound and unexpected ways. From voice assistants that understand natural "
    "language to recommendation systems that predict our preferences, machine "
    "learning models have become an integral part of modern life. However, building "
    "these systems requires careful consideration of data quality, model architecture, "
    "and deployment infrastructure. The balance between performance and cost remains "
    "a central challenge for engineers and researchers alike."
)

SHORT_TEXT = "Hello, this is a quick recovery test."


def text_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def pcm_to_wav(pcm: bytes, sample_rate: int, channels: int, width: int) -> bytes:
    """Wrap raw PCM s16le in a WAV container."""
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()



def classify_recovery_result(
    *,
    recovery_events: list[dict[str, Any]],
    pcm_bytes: int,
    sample_rate: int,
    channels: int,
    width: int,
    timeout: bool,
    exception: str | None,
    request_mode: str = "legacy",
) -> dict[str, Any]:
    """Classify one recovery response without weakening protocol validation.

    The Phase 8B1 harness sends a standalone legacy ``Synthesize`` event for
    recovery. For that exact request type, the correct Wyoming terminal audio
    sequence is ``AudioStart`` → one or more ``AudioChunk`` → ``AudioStop``.
    ``SynthesizeStopped`` is reserved for a streaming-text
    ``SynthesizeStart``/``SynthesizeChunk``/``SynthesizeStop`` session, so it is
    not required for a standalone legacy recovery request.
    """
    event_types = [str(e.get("type", "")) for e in recovery_events]
    audio_start_received = "audio-start" in event_types
    audio_chunk_count = event_types.count("audio-chunk")
    audio_stop_received = "audio-stop" in event_types
    synthesize_stopped_received = "synthesize-stopped" in event_types

    def _first_elapsed(*types: str) -> int | None:
        wanted = set(types)
        for event in recovery_events:
            if event.get("type") in wanted:
                value = event.get("elapsed_ms")
                return int(value) if isinstance(value, int | float) else None
        return None

    audio_start_ms = _first_elapsed("audio-start")
    first_audio_ms = _first_elapsed("audio-chunk")
    if request_mode == "streaming":
        protocol_terminal_success = synthesize_stopped_received
        completion_ms = _first_elapsed("synthesize-stopped")
        terminal_failure = "missing synthesize-stopped terminal event"
    elif request_mode == "legacy":
        protocol_terminal_success = audio_stop_received
        completion_ms = _first_elapsed("audio-stop")
        terminal_failure = "missing audio-stop terminal event"
    else:
        raise ValueError("request_mode must be 'legacy' or 'streaming'")

    frame_size = width * channels if width > 0 and channels > 0 else 0
    frame_aligned = bool(frame_size and pcm_bytes % frame_size == 0)
    pcm_valid = bool(pcm_bytes > 0 and sample_rate > 0 and frame_aligned)

    exact_failure_reason = ""
    if exception:
        exact_failure_reason = exception
    elif timeout:
        exact_failure_reason = "timeout waiting for recovery events"
    elif not audio_start_received:
        exact_failure_reason = "missing audio-start event"
    elif audio_chunk_count == 0:
        exact_failure_reason = "missing audio-chunk event"
    elif pcm_bytes <= 0:
        exact_failure_reason = "empty PCM"
    elif sample_rate <= 0 or channels <= 0 or width <= 0:
        exact_failure_reason = "invalid audio metadata"
    elif not frame_aligned:
        exact_failure_reason = "PCM not frame-aligned"
    elif not protocol_terminal_success:
        exact_failure_reason = terminal_failure

    audio_recovery_success = (
        not timeout
        and not exception
        and audio_start_received
        and audio_chunk_count > 0
        and pcm_valid
        and protocol_terminal_success
    )

    return {
        "request_mode": request_mode,
        "audio_start_received": audio_start_received,
        "audio_chunk_count": audio_chunk_count,
        "audio_stop_received": audio_stop_received,
        "synthesize_stopped_received": synthesize_stopped_received,
        "audio_start_ms": audio_start_ms,
        "non_empty_audio": pcm_bytes > 0,
        "frame_aligned": frame_aligned,
        "pcm_valid": pcm_valid,
        "timeout": timeout,
        "exception": exception,
        "server_busy_response": "server-busy" in event_types,
        "audio_recovery_success": audio_recovery_success,
        "protocol_terminal_success": protocol_terminal_success,
        "first_audio_ms": first_audio_ms,
        "completion_ms": completion_ms,
        "pcm_bytes": pcm_bytes,
        "exact_failure_reason": exact_failure_reason,
    }

async def run_cancel_cycle(
    host: str,
    port: int,
    cycle: int,
    chunks_before_disconnect: int,
    timeout: float,
    recovery_delay: float,
    artifacts_dir: Path,
) -> dict[str, Any]:
    """One cancel+recovery cycle.  Returns client-side measurements."""
    result: dict[str, Any] = {
        "cycle": cycle,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }

    # ── Phase A: Cancellation request ────────────────────────────────────
    cancel_text = LONG_TEXT
    cancel_fp = text_fingerprint(cancel_text)
    t_cycle_start = time.monotonic()

    cancel_events: list[dict[str, Any]] = []
    cancel_pcm_bytes = 0
    cancel_audiostart_time: float | None = None
    cancel_first_chunk_time: float | None = None
    cancel_disconnect_time: float | None = None

    try:
        async with AsyncTcpClient(host, port) as tcp:
            await tcp.write_event(Synthesize(text=cancel_text).event())

            chunks_received = 0
            while True:
                try:
                    ev = await asyncio.wait_for(tcp.read_event(), timeout=timeout)
                except asyncio.TimeoutError:
                    result["cancel_error"] = "timeout waiting for events"
                    break
                if ev is None:
                    break

                now_ms = int((time.monotonic() - t_cycle_start) * 1000)
                cancel_events.append({"type": ev.type, "elapsed_ms": now_ms})

                if AudioStart.is_type(ev.type):
                    cancel_audiostart_time = time.monotonic()
                elif AudioChunk.is_type(ev.type):
                    chunk = AudioChunk.from_event(ev)
                    cancel_pcm_bytes += len(chunk.audio)
                    if cancel_first_chunk_time is None:
                        cancel_first_chunk_time = time.monotonic()
                    chunks_received += 1
                elif SynthesizeStopped.is_type(ev.type):
                    # Completed before we could disconnect
                    break

                if chunks_received >= chunks_before_disconnect:
                    # Abruptly close the connection
                    cancel_disconnect_time = time.monotonic()
                    tcp._writer.close()
                    try:
                        await tcp._writer.wait_closed()
                    except Exception:
                        pass
                    break

        result["cancel_disconnect_ms"] = (
            int((cancel_disconnect_time - t_cycle_start) * 1000)
            if cancel_disconnect_time
            else None
        )
        result["cancel_audiostart_ms"] = (
            int((cancel_audiostart_time - t_cycle_start) * 1000)
            if cancel_audiostart_time
            else None
        )
        result["cancel_first_chunk_ms"] = (
            int((cancel_first_chunk_time - t_cycle_start) * 1000)
            if cancel_first_chunk_time
            else None
        )
        result["cancel_chunks_received"] = chunks_received
        result["cancel_pcm_bytes"] = cancel_pcm_bytes
        result["cancel_fingerprint"] = cancel_fp
        result["cancel_events"] = cancel_events
    except Exception as exc:
        result["cancel_error"] = f"{type(exc).__name__}: {exc}"

    # ── Recovery interval ────────────────────────────────────────────────
    await asyncio.sleep(recovery_delay)

    # ── Phase B: Recovery request ────────────────────────────────────────
    recovery_text = SHORT_TEXT
    recovery_fp = text_fingerprint(recovery_text)
    t_recovery_start = time.monotonic()

    recovery_events: list[dict[str, Any]] = []
    recovery_pcm = bytearray()
    recovery_sample_rate = 0
    recovery_channels = 1
    recovery_width = 2
    recovery_timeout = False
    recovery_exception: str | None = None
    recovery_first_audio_ms: int | None = None
    recovery_complete_ms: int | None = None
    recovery_request_mode = "legacy"

    try:
        async with AsyncTcpClient(host, port) as tcp2:
            await tcp2.write_event(Synthesize(text=recovery_text).event())

            while True:
                try:
                    ev = await asyncio.wait_for(tcp2.read_event(), timeout=timeout)
                except asyncio.TimeoutError:
                    recovery_timeout = True
                    break
                if ev is None:
                    break

                now_ms = int((time.monotonic() - t_recovery_start) * 1000)
                recovery_events.append({"type": ev.type, "elapsed_ms": now_ms})

                if AudioStart.is_type(ev.type):
                    s = AudioStart.from_event(ev)
                    recovery_sample_rate = s.rate
                    recovery_channels = s.channels
                    recovery_width = s.width
                    if recovery_first_audio_ms is None:
                        recovery_first_audio_ms = now_ms
                elif AudioChunk.is_type(ev.type):
                    c = AudioChunk.from_event(ev)
                    recovery_pcm.extend(c.audio)
                    if recovery_first_audio_ms is None:
                        recovery_first_audio_ms = now_ms
                elif AudioStop.is_type(ev.type):
                    if recovery_request_mode == "legacy":
                        recovery_complete_ms = now_ms
                        break
                elif SynthesizeStopped.is_type(ev.type):
                    if recovery_request_mode == "streaming":
                        recovery_complete_ms = now_ms
                        break

        result["recovery_first_audio_ms"] = recovery_first_audio_ms
        result["recovery_complete_ms"] = recovery_complete_ms
        result["recovery_pcm_bytes"] = len(recovery_pcm)
        result["recovery_sample_rate"] = recovery_sample_rate
        result["recovery_channels"] = recovery_channels
        result["recovery_width"] = recovery_width
        result["recovery_fingerprint"] = recovery_fp
        result["recovery_events"] = recovery_events

        # Validate recovery audio
        if recovery_pcm:
            frame_size = recovery_width * recovery_channels
            if len(recovery_pcm) % frame_size != 0:
                result["recovery_frame_aligned"] = False
            else:
                result["recovery_frame_aligned"] = True

            # Save WAV
            wav_path = artifacts_dir / f"recovery_cycle_{cycle:02d}.wav"
            wav_data = pcm_to_wav(
                bytes(recovery_pcm),
                recovery_sample_rate,
                recovery_channels,
                recovery_width,
            )
            wav_path.write_bytes(wav_data)
            result["recovery_wav_path"] = str(wav_path)
        else:
            result["recovery_frame_aligned"] = False
    except Exception as exc:
        recovery_exception = f"{type(exc).__name__}: {exc}"
        result["recovery_error"] = recovery_exception

    classification = classify_recovery_result(
        recovery_events=recovery_events,
        pcm_bytes=len(recovery_pcm),
        sample_rate=recovery_sample_rate,
        channels=recovery_channels,
        width=recovery_width,
        timeout=recovery_timeout,
        exception=recovery_exception,
        request_mode=recovery_request_mode,
    )
    result.update(classification)
    result["recovery_success"] = classification["audio_recovery_success"]
    result["recovery_error"] = classification["exact_failure_reason"]
    result["recovery_first_audio_ms"] = classification["first_audio_ms"]
    result["recovery_complete_ms"] = classification["completion_ms"]
    result["recovery_pcm_bytes"] = classification["pcm_bytes"]
    result["recovery_frame_aligned"] = classification["frame_aligned"]

    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 8B1 Live Verification")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10200)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--chunks-before-disconnect", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--recovery-delay", type=float, default=1.0)
    parser.add_argument(
        "--artifacts-dir",
        default="verification_artifacts/phase_8b1",
    )
    args = parser.parse_args()

    artifacts_dir = Path(args.artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []

    print(f"=== Phase 8B1 Live Verification ===")
    print(f"Host: {args.host}:{args.port}")
    print(f"Runs: {args.runs}")
    print(f"Chunks before disconnect: {args.chunks_before_disconnect}")
    print(f"Timeout: {args.timeout}s  Recovery delay: {args.recovery_delay}s")
    print(f"Artifacts: {artifacts_dir}")
    print()

    for cycle in range(1, args.runs + 1):
        print(f"--- Cycle {cycle}/{args.runs} ---")
        result = await run_cancel_cycle(
            host=args.host,
            port=args.port,
            cycle=cycle,
            chunks_before_disconnect=args.chunks_before_disconnect,
            timeout=args.timeout,
            recovery_delay=args.recovery_delay,
            artifacts_dir=artifacts_dir,
        )
        results.append(result)

        # Print precise summary
        dc_ms = result.get("cancel_disconnect_ms", "?")
        audio_status = "PASS" if result.get("audio_recovery_success") else "FAIL"
        protocol_status = "PASS" if result.get("protocol_terminal_success") else "FAIL"
        first_audio = result.get("first_audio_ms")
        complete = result.get("completion_ms")
        pcm = result.get("pcm_bytes", 0)
        err = result.get("exact_failure_reason") or result.get("cancel_error") or ""
        if err:
            print(
                f"  Disconnect: {dc_ms} ms | Audio: {audio_status} | "
                f"Protocol: {protocol_status} | Failure: {err} | PCM: {pcm} bytes"
            )
        else:
            print(
                f"  Disconnect: {dc_ms} ms | Audio: {audio_status} | "
                f"Protocol: {protocol_status} | First audio: {first_audio} ms | "
                f"Complete: {complete} ms | PCM: {pcm} bytes"
            )
        print()

    # Save results
    results_path = artifacts_dir / "client-results.json"
    results_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"Results saved to {results_path}")

    # Summary
    successes = sum(1 for r in results if r.get("audio_recovery_success"))
    print(f"\n=== Summary: {successes}/{len(results)} recoveries successful ===")
    if successes == len(results):
        print("All cycles passed.")
    else:
        print("SOME CYCLES FAILED — check client-results.json for details.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

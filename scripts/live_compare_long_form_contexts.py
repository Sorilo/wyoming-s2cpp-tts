#!/usr/bin/env python3
"""Long-form Wyoming TTS probe for Phase 8B1 audio-quality comparison.

This client-side harness intentionally does not modify Docker/Unraid settings.
Run it once per live wrapper context (4, 64, auto/160) after setting the wrapper
configuration externally, using the same text and voice each time.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize, SynthesizeVoice

LONG_FORM_TEXT = (
    "This is a long-form stability and audio-quality comparison for the local "
    "Wyoming text to speech service. The same passage should be used for every "
    "codec context so the results can be compared fairly. First, the assistant "
    "will speak a short introduction. Next, it will continue with several "
    "connected sentences that are long enough to expose streaming boundary "
    "behavior, possible playback underruns, repeated syllables, missing words, "
    "or tonal artifacts. The goal is not to prove perceptual quality from numbers "
    "alone. The goal is to capture timing, chunk cadence, and a matching wave file "
    "so a human can listen to context four, context sixty four, and the automatic "
    "one hundred sixty frame context under controlled conditions. If one version "
    "contains beeping, stuttering, robotic voice changes, clipped sentence endings, "
    "or unnatural pauses, mark that in the listening checklist. If all versions "
    "sound clean but the buffer model shows playback falling behind real time, the "
    "next investigation should focus on buffering and playback cadence rather than "
    "assuming a codec quality problem. Finally, this paragraph adds extra duration "
    "so the test is longer than a short recovery response and more likely to reveal "
    "long-form issues that were not audible in brief Home Assistant previews."
)


def text_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def pcm_to_wav(path: Path, pcm: bytes, sample_rate: int, channels: int, width: int) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)


def summarize_buffer(chunks: list[dict[str, Any]], sample_rate: int, channels: int, width: int) -> dict[str, Any]:
    """Estimate whether generated audio falls behind real-time playback.

    This is a client-side approximation. It treats the first AudioChunk arrival
    as playback start and compares cumulative produced audio duration to elapsed
    wall time after that point.
    """
    if not chunks or sample_rate <= 0 or channels <= 0 or width <= 0:
        return {
            "produced_audio_seconds_over_wall_clock": None,
            "buffer_seconds_min": None,
            "buffer_seconds_final": None,
            "buffer_trend": "unknown",
            "generation_fell_behind_playback": None,
            "underrun_likely": None,
        }

    frame_size = channels * width
    first_arrival = float(chunks[0]["elapsed_ms"]) / 1000.0
    cumulative_frames = 0
    buffer_levels: list[float] = []
    for chunk in chunks:
        cumulative_frames += int(chunk["bytes"]) // frame_size
        produced_seconds = cumulative_frames / sample_rate
        elapsed_after_first = max(0.0, (float(chunk["elapsed_ms"]) / 1000.0) - first_arrival)
        buffer_levels.append(produced_seconds - elapsed_after_first)

    wall_seconds = max(0.001, (float(chunks[-1]["elapsed_ms"]) / 1000.0) - first_arrival)
    audio_seconds = cumulative_frames / sample_rate
    final_buffer = buffer_levels[-1]
    min_buffer = min(buffer_levels)
    trend = "growing" if final_buffer > buffer_levels[0] else "emptying_or_flat"
    return {
        "produced_audio_seconds_over_wall_clock": round(audio_seconds / wall_seconds, 4),
        "buffer_seconds_min": round(min_buffer, 4),
        "buffer_seconds_final": round(final_buffer, 4),
        "buffer_trend": trend,
        "generation_fell_behind_playback": min_buffer < 0,
        "underrun_likely": min_buffer < -0.05,
    }


async def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    text = Path(args.text_file).read_text(encoding="utf-8") if args.text_file else LONG_FORM_TEXT
    output_dir = Path(args.output_dir) / f"context_{args.context_label}"
    output_dir.mkdir(parents=True, exist_ok=True)

    pcm = bytearray()
    sample_rate = 0
    channels = 1
    width = 2
    events: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    audio_start_ms: int | None = None
    first_chunk_ms: int | None = None
    audio_stop_ms: int | None = None
    timeout = False

    voice = SynthesizeVoice(name=args.voice) if args.voice else None
    start = time.monotonic()
    async with AsyncTcpClient(args.host, args.port) as tcp:
        await tcp.write_event(Synthesize(text=text, voice=voice).event())
        while True:
            try:
                event = await asyncio.wait_for(tcp.read_event(), timeout=args.timeout)
            except asyncio.TimeoutError:
                timeout = True
                break
            if event is None:
                break
            elapsed_ms = int((time.monotonic() - start) * 1000)
            events.append({"type": event.type, "elapsed_ms": elapsed_ms})
            if AudioStart.is_type(event.type):
                audio_start = AudioStart.from_event(event)
                sample_rate = audio_start.rate
                channels = audio_start.channels
                width = audio_start.width
                audio_start_ms = elapsed_ms
            elif AudioChunk.is_type(event.type):
                chunk = AudioChunk.from_event(event)
                pcm.extend(chunk.audio)
                if first_chunk_ms is None:
                    first_chunk_ms = elapsed_ms
                chunks.append({
                    "elapsed_ms": elapsed_ms,
                    "bytes": len(chunk.audio),
                    "timestamp_ms": chunk.timestamp,
                })
            elif AudioStop.is_type(event.type):
                audio_stop_ms = elapsed_ms
                break

    frame_size = channels * width if channels > 0 and width > 0 else 0
    frame_aligned = bool(frame_size and len(pcm) % frame_size == 0)
    audio_duration_s = (len(pcm) / frame_size / sample_rate) if frame_aligned and sample_rate else 0.0
    total_s = (audio_stop_ms or int((time.monotonic() - start) * 1000)) / 1000.0
    chunk_gaps_ms = [chunks[i]["elapsed_ms"] - chunks[i - 1]["elapsed_ms"] for i in range(1, len(chunks))]

    wav_path = output_dir / f"long_form_context_{args.context_label}.wav"
    if pcm and sample_rate and frame_aligned:
        pcm_to_wav(wav_path, bytes(pcm), sample_rate, channels, width)

    result = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "host": args.host,
        "port": args.port,
        "context_label": args.context_label,
        "voice": args.voice or "wrapper-default-or-generic",
        "text_fingerprint": text_fingerprint(text),
        "text_length": len(text),
        "audio_start_ms": audio_start_ms,
        "first_wyoming_audio_ms": first_chunk_ms,
        "first_backend_audio_ms": None,
        "first_backend_audio_note": "client-only probe; correlate wrapper backend_stream_first_audio logs from capture script",
        "total_synthesis_ms": audio_stop_ms,
        "timeout": timeout,
        "sample_rate": sample_rate,
        "channels": channels,
        "width": width,
        "pcm_bytes": len(pcm),
        "frame_aligned": frame_aligned,
        "audio_duration_seconds": round(audio_duration_s, 3),
        "real_time_factor_wall_over_audio": round(total_s / audio_duration_s, 4) if audio_duration_s else None,
        "audio_seconds_produced_per_wall_second": round(audio_duration_s / total_s, 4) if total_s else None,
        "chunk_count": len(chunks),
        "inter_chunk_gaps_ms": chunk_gaps_ms,
        "longest_inter_chunk_gap_ms": max(chunk_gaps_ms) if chunk_gaps_ms else None,
        "events": events,
        "chunks": chunks,
        "wav_path": str(wav_path) if wav_path.exists() else None,
    }
    result.update(summarize_buffer(chunks, sample_rate, channels, width))

    result_path = output_dir / f"long_form_context_{args.context_label}.json"
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in [
        "context_label", "first_wyoming_audio_ms", "total_synthesis_ms",
        "audio_duration_seconds", "real_time_factor_wall_over_audio",
        "chunk_count", "longest_inter_chunk_gap_ms", "generation_fell_behind_playback",
        "underrun_likely", "pcm_bytes", "wav_path"
    ]}, indent=2))
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 8B1 long-form context probe")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10200)
    parser.add_argument("--context-label", required=True, help="Label for the active live context, e.g. 4, 64, auto")
    parser.add_argument("--voice", default="", help="Optional Wyoming voice name; omit to use wrapper default/generic")
    parser.add_argument("--text-file", default="", help="Optional UTF-8 text file; defaults to built-in fixed long text")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--output-dir", default="verification_artifacts/phase_8b1/long_form")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run_probe(parse_args()))

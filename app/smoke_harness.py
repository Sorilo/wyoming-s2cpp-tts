"""Phase 5.5 smoke-test harness for real external s2.cpp backend validation.

This module implements the opt-in smoke-test harness that validates real
s2.cpp HTTP ``/generate`` backend compatibility without starting, building,
or downloading s2.cpp, models, or CUDA tooling.

The harness is deliberately opt-in — it never contacts a backend without
the ``--run-real`` flag or equivalent environment variable.  Without opt-in
it validates configuration and exits successfully with ``status=skipped``.

Architecture:
    - ``SmokeConfig`` — CLI/env-driven configuration
    - ``*Result`` dataclasses — structured per-check outcomes
    - ``SmokeReport`` — top-level report with machine-readable JSON
    - ``run_smoke_harness()`` — orchestrator: reachability → checks → report
    - WAV/PCM validation helpers

Phase 5.5A (harness implemented, mocked/tested) vs Phase 5.5B (real
backend verified) are tracked separately via ``SmokeReport.phase_5_5b_status``.
"""

from __future__ import annotations

import json
import socket
import struct
import sys
import time
import urllib.error
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.config import Settings
from app.s2_client import (
    S2Client,
    S2ClientError,
    S2Endpoint,
    S2GenerateRequest,
    S2GenerateResult,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class SmokeConfig:
    """Opt-in configuration for the Phase 5.5 smoke harness."""

    run_real: bool = False
    """Must be True before any real backend request is sent."""

    require_backend: bool = False
    """When True, backend unavailability returns nonzero exit status."""

    endpoint_override: str | None = None
    """Explicit ``host:port`` override for the s2.cpp backend."""

    probe_legacy_json: bool = False
    """If True, also probe the legacy JSON path (expected unsupported)."""

    output_dir: Path | None = None
    """Optional directory for diagnostic WAV/PCM output files."""

    text: str = "Hello from wyoming-s2cpp-tts smoke test."
    """Bounded test text sent to the backend."""

    timeout_seconds: float = 30.0
    """Per-request connection + read timeout."""

    def endpoint(self, settings: Settings) -> str:
        """Return the configured s2.cpp endpoint URL."""
        if self.endpoint_override is not None:
            host, _, port = self.endpoint_override.partition(":")
            ep = S2Endpoint(host=host, port=int(port or "3030"))
            return ep.generate_url
        return S2Endpoint.from_settings(settings).generate_url


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class BufferedMultipartResult:
    """Outcome of one canonical buffered multipart synthesis request."""

    status: str  # "success" | "failure" | "skipped"
    endpoint: str
    http_status: int | None = None
    content_type: str = ""
    response_byte_count: int = 0
    audio_non_empty: bool = False
    wav_header_valid: bool | None = None  # None = not checked (no audio)
    duration_ms: float = 0.0
    error_category: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StreamingMultipartResult:
    """Outcome of one canonical streaming multipart synthesis request."""

    status: str  # "success" | "failure" | "skipped"
    endpoint: str
    http_status: int | None = None
    content_type: str = ""
    audio_sample_rate: int | None = None
    audio_channels: int | None = None
    audio_encoding: str | None = None
    missing_audio_headers: list[str] = field(default_factory=list)
    read_sizes: list[int] = field(default_factory=list)
    non_empty_read_count: int = 0
    total_pcm_bytes: int = 0
    time_to_first_data_ms: float | None = None
    total_duration_ms: float = 0.0
    eof_reached: bool = False
    stream_closed_cleanly: bool = False
    progressive_classification: str = "failed"
    pcm_frame_aligned: bool | None = None
    error_category: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LegacyJsonResult:
    """Outcome of an optional legacy JSON-path compatibility probe."""

    status: str  # "unsupported" | "unexpected_success" | "skipped"
    http_status: int | None = None
    content_type: str = ""
    response_byte_count: int = 0
    error_category: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SmokeReport:
    """Top-level Phase 5.5 smoke-test report."""

    overall_status: str  # "skipped" | "unavailable" | "completed"
    phase_5_5a_status: str  # "harness_ready_backend_not_tested"
    phase_5_5b_status: str  # "pending" | "real_backend_verified" | "real_backend_failed"
    timestamp: str
    git_commit: str
    upstream_repo: str = "rodrigomatta/s2.cpp"
    upstream_revision: str = ""
    configured_endpoint: str = ""
    buffered_multipart: BufferedMultipartResult | None = None
    streaming_multipart: StreamingMultipartResult | None = None
    legacy_json: LegacyJsonResult | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.buffered_multipart is not None:
            d["buffered_multipart"] = self.buffered_multipart.to_dict()
        if self.streaming_multipart is not None:
            d["streaming_multipart"] = self.streaming_multipart.to_dict()
        if self.legacy_json is not None:
            d["legacy_json"] = self.legacy_json.to_dict()
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# ---------------------------------------------------------------------------
# WAV validation
# ---------------------------------------------------------------------------


def _validate_wav_header(audio: bytes) -> bool:
    """Return True when *audio* begins with a plausible RIFF/WAVE header.

    Checks:
      - Minimum 44 bytes (standard PCM WAV header size)
      - ChunkID == ``b'RIFF'`` at offset 0
      - Format == ``b'WAVE'`` at offset 8
    """
    if len(audio) < 44:
        return False
    return audio[:4] == b"RIFF" and audio[8:12] == b"WAVE"


def _parse_wav_params(audio: bytes) -> dict[str, int] | None:
    """Extract audio format, channels, sample rate from a WAV header.

    Returns *None* when the header is too short or invalid.
    """
    if len(audio) < 44 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        return None
    try:
        channels = struct.unpack_from("<H", audio, 22)[0]
        sample_rate = struct.unpack_from("<I", audio, 24)[0]
        bits_per_sample = struct.unpack_from("<H", audio, 34)[0]
        return {
            "channels": channels,
            "sample_rate": sample_rate,
            "bits_per_sample": bits_per_sample,
        }
    except (struct.error, IndexError):
        return None


# ---------------------------------------------------------------------------
# PCM frame-alignment validation
# ---------------------------------------------------------------------------


def _validate_pcm_frame_alignment(
    total_bytes: int,
    channels: int,
    width: int = 2,
) -> bool:
    """Return True when *total_bytes* is a multiple of ``width * channels``."""
    frame_size = width * channels
    if frame_size <= 0:
        return False
    return total_bytes % frame_size == 0


# ---------------------------------------------------------------------------
# Reachability probe
# ---------------------------------------------------------------------------


def _probe_reachable(host: str, port: int, timeout: float = 5.0) -> bool:
    """Return True when a TCP connection to *host:port* succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Canonical buffered multipart smoke check
# ---------------------------------------------------------------------------


def _run_buffered_multipart(
    client: S2Client,
    request: S2GenerateRequest,
    endpoint_url: str,
    output_dir: Path | None,
) -> BufferedMultipartResult:
    t0 = time.monotonic()
    try:
        result: S2GenerateResult = client.generate_multipart(request)
    except S2ClientError as exc:
        elapsed = (time.monotonic() - t0) * 1000
        error_cat = _categorize_error(exc)
        return BufferedMultipartResult(
            status="failure",
            endpoint=endpoint_url,
            duration_ms=elapsed,
            error_category=error_cat,
        )

    elapsed = (time.monotonic() - t0) * 1000
    audio = result.audio

    if output_dir is not None and audio:
        out_path = output_dir / "smoke_buffered_multipart.wav"
        out_path.write_bytes(audio)

    return BufferedMultipartResult(
        status="success",
        endpoint=endpoint_url,
        http_status=200,  # urllib raises on non-2xx
        content_type=result.content_type,
        response_byte_count=len(audio),
        audio_non_empty=bool(audio),
        wav_header_valid=_validate_wav_header(audio) if audio else None,
        duration_ms=elapsed,
    )


# ---------------------------------------------------------------------------
# Canonical streaming multipart smoke check
# ---------------------------------------------------------------------------


def _run_streaming_multipart(
    client: S2Client,
    request: S2GenerateRequest,
    endpoint_url: str,
    output_dir: Path | None,
) -> StreamingMultipartResult:
    t0 = time.monotonic()
    read_sizes: list[int] = []
    total_pcm = 0
    non_empty = 0
    t_first_data: float | None = None
    eof = False
    closed_clean = False
    http_status: int | None = None
    content_type = ""
    audio_sample_rate: int | None = None
    audio_channels: int | None = None
    audio_encoding: str | None = None
    missing_headers: list[str] = []
    error_cat: str | None = None

    try:
        stream = client.generate_stream(request)
    except S2ClientError as exc:
        elapsed = (time.monotonic() - t0) * 1000
        return StreamingMultipartResult(
            status="failure",
            endpoint=endpoint_url,
            total_duration_ms=elapsed,
            error_category=_categorize_error(exc),
        )

    try:
        with stream:
            http_status = stream.status_code
            content_type = stream.content_type
            raw_headers = stream.response_headers
            audio_sample_rate, audio_channels, audio_encoding, missing_headers = (
                _parse_audio_headers(raw_headers)
            )

            for chunk in stream:
                chunk_len = len(chunk)
                read_sizes.append(chunk_len)
                total_pcm += chunk_len
                if chunk_len > 0:
                    non_empty += 1
                    if t_first_data is None:
                        t_first_data = time.monotonic()
            eof = True
        closed_clean = True
    except S2ClientError as exc:
        error_cat = _categorize_error(exc)

    total_elapsed = (time.monotonic() - t0) * 1000
    t_first_ms = (t_first_data - t0) * 1000 if t_first_data is not None else None

    # Progressive classification.
    progressive = _classify_progressive(non_empty, eof)

    # PCM frame alignment.
    frame_aligned: bool | None = None
    if audio_channels is not None and total_pcm > 0:
        frame_aligned = _validate_pcm_frame_alignment(
            total_pcm, channels=audio_channels
        )

    # Determine status.
    if error_cat is not None:
        status = "failure"
    elif total_pcm > 0:
        status = "success"
    else:
        status = "failure"

    return StreamingMultipartResult(
        status=status,
        endpoint=endpoint_url,
        http_status=http_status,
        content_type=content_type,
        audio_sample_rate=audio_sample_rate,
        audio_channels=audio_channels,
        audio_encoding=audio_encoding,
        missing_audio_headers=missing_headers,
        read_sizes=read_sizes,
        non_empty_read_count=non_empty,
        total_pcm_bytes=total_pcm,
        time_to_first_data_ms=t_first_ms,
        total_duration_ms=total_elapsed,
        eof_reached=eof,
        stream_closed_cleanly=closed_clean,
        progressive_classification=progressive,
        pcm_frame_aligned=frame_aligned,
        error_category=error_cat,
    )


# ---------------------------------------------------------------------------
# Legacy JSON probe
# ---------------------------------------------------------------------------


def _run_legacy_json(
    client: S2Client,
    request: S2GenerateRequest,
    endpoint_url: str,
) -> LegacyJsonResult:
    """Probe the legacy JSON path.  Rejection is expected from the
    multipart-only rodrigomatta/s2.cpp target — do not count this as failure
    of the canonical smoke test."""
    try:
        result: S2GenerateResult = client.generate(request)
    except S2ClientError as exc:
        return LegacyJsonResult(
            status="unsupported",
            error_category=_categorize_error(exc),
        )

    return LegacyJsonResult(
        status="unexpected_success",
        http_status=200,
        content_type=result.content_type,
        response_byte_count=len(result.audio),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _categorize_error(exc: S2ClientError) -> str:
    """Return a compact error category from an S2ClientError message."""
    msg = str(exc).lower()
    if "connection refused" in msg or "errno 111" in msg:
        return "connection_refused"
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    if "name or service not known" in msg or "getaddrinfo" in msg:
        return "dns_resolution_failure"
    if "http error" in msg or "errno" in msg:
        return "http_error"
    return "unknown"


def _parse_audio_headers(
    headers: dict[str, str],
) -> tuple[int | None, int | None, str | None, list[str]]:
    """Parse X-Audio-* response headers into typed values.

    Returns ``(sample_rate, channels, encoding, missing_headers)``.
    """
    # Normalise keys to lowercase for case-insensitive lookup.
    _h = {k.lower(): v for k, v in headers.items()}
    missing: list[str] = []
    rate_str = _h.get("x-audio-sample-rate")
    ch_str = _h.get("x-audio-channels")
    enc = _h.get("x-audio-encoding")

    rate: int | None = None
    if rate_str is not None:
        try:
            rate = int(rate_str)
        except ValueError:
            missing.append("x-audio-sample-rate (unparseable)")
    else:
        missing.append("x-audio-sample-rate")

    channels: int | None = None
    if ch_str is not None:
        try:
            channels = int(ch_str)
        except ValueError:
            missing.append("x-audio-channels (unparseable)")
    else:
        missing.append("x-audio-channels")

    if enc is None:
        missing.append("x-audio-encoding")

    return rate, channels, enc, missing


def _classify_progressive(non_empty_reads: int, eof_reached: bool) -> str:
    """Classify whether streaming delivery was genuinely progressive."""
    if non_empty_reads == 0:
        return "failed"
    if non_empty_reads >= 2:
        return "verified_progressive"
    # Exactly one non-empty read.
    if eof_reached:
        return "audio_received_but_progressiveness_inconclusive"
    return "failed"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _redact_endpoint(url: str) -> str:
    """Strip credentials and query secrets from the endpoint URL for safe output."""
    if "@" in url:
        proto, rest = url.split("://", 1)
        if "@" in rest:
            rest = rest.split("@", 1)[1]
        return f"{proto}://{rest}"
    if "?" in url:
        return url.split("?")[0]
    return url


def _git_commit(repo_root: Path) -> str:
    """Return the current Git commit hash (short), or 'unknown'."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def run_smoke_harness(
    config: SmokeConfig,
    settings: Settings,
    *,
    repo_root: Path | None = None,
    now_iso: str | None = None,
    _probe_fn: Callable[[str, int, float], bool] = _probe_reachable,
) -> SmokeReport:
    """Run the Phase 5.5 smoke harness.

    This is the main entry point.  It respects all opt-in gates and
    returns a ``SmokeReport`` regardless of backend availability.
    """
    from datetime import datetime, timezone

    timestamp = now_iso or datetime.now(timezone.utc).isoformat()
    commit = _git_commit(repo_root or Path.cwd())
    endpoint_url = config.endpoint(settings)
    redacted = _redact_endpoint(endpoint_url)

    # --- Opt-in gate --------------------------------------------------------
    if not config.run_real:
        return SmokeReport(
            overall_status="skipped",
            phase_5_5a_status="harness_ready_backend_not_tested",
            phase_5_5b_status="pending",
            timestamp=timestamp,
            git_commit=commit,
            configured_endpoint=redacted,
            upstream_revision="retrieval date: see CHANGELOG",
        )

    # --- Reachability --------------------------------------------------------
    ep = S2Endpoint.from_settings(settings)
    if config.endpoint_override is not None:
        host, _, port = config.endpoint_override.partition(":")
        ep = S2Endpoint(host=host, port=int(port or "3030"))

    reachable = _probe_fn(ep.host, ep.port, timeout=5.0)
    if not reachable:
        report = SmokeReport(
            overall_status="unavailable",
            phase_5_5a_status="harness_ready_backend_not_tested",
            phase_5_5b_status="pending",
            timestamp=timestamp,
            git_commit=commit,
            configured_endpoint=redacted,
            warnings=["backend unreachable via TCP connect"],
        )
        if config.require_backend:
            report.warnings.append(
                "require_backend is set: treat this as a hard failure"
            )
        return report

    # --- Real checks run sequentially ---------------------------------------
    client = S2Client(ep, timeout_seconds=config.timeout_seconds)
    request = S2GenerateRequest.from_settings(text=config.text, settings=settings)
    output_dir = config.output_dir

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Canonical buffered multipart
    buffered = _run_buffered_multipart(client, request, endpoint_url, output_dir)

    # 2. Canonical streaming multipart
    streaming = _run_streaming_multipart(client, request, endpoint_url, output_dir)

    # 3. Optional legacy JSON probe
    legacy: LegacyJsonResult | None = None
    if config.probe_legacy_json:
        legacy = _run_legacy_json(client, request, endpoint_url)

    # --- Phase 5.5B determination -------------------------------------------
    buffered_ok = buffered.status == "success" and buffered.wav_header_valid is True
    streaming_ok = streaming.status == "success" and streaming.total_pcm_bytes > 0

    if buffered_ok and streaming_ok:
        phase_5_5b = "real_backend_verified"
    else:
        phase_5_5b = "real_backend_failed"

    warnings: list[str] = []
    if not buffered.wav_header_valid and buffered.audio_non_empty:
        warnings.append("buffered response has audio but no valid WAV header")
    if streaming.progressive_classification == "audio_received_but_progressiveness_inconclusive":
        warnings.append(
            "streaming returned audio but only one transport read — "
            "progressive delivery is inconclusive"
        )
    if streaming.missing_audio_headers:
        warnings.append(
            f"missing streaming audio headers: {', '.join(streaming.missing_audio_headers)}"
        )
    if legacy is not None and legacy.status == "unexpected_success":
        warnings.append("legacy JSON path unexpectedly succeeded against multipart-only target")

    return SmokeReport(
        overall_status="completed",
        phase_5_5a_status="harness_ready_backend_not_tested",
        phase_5_5b_status=phase_5_5b,
        timestamp=timestamp,
        git_commit=commit,
        configured_endpoint=redacted,
        buffered_multipart=buffered,
        streaming_multipart=streaming,
        legacy_json=legacy,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Human-readable summary helper
# ---------------------------------------------------------------------------


def format_summary(report: SmokeReport) -> str:
    """Return a human-readable multi-line summary of the smoke report."""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("Phase 5.5 s2.cpp Smoke Test Report")
    lines.append("=" * 60)
    lines.append(f"  Overall:           {report.overall_status}")
    lines.append(f"  Phase 5.5A:        {report.phase_5_5a_status}")
    lines.append(f"  Phase 5.5B:        {report.phase_5_5b_status}")
    lines.append(f"  Timestamp:         {report.timestamp}")
    lines.append(f"  Git commit:        {report.git_commit}")
    lines.append(f"  Endpoint:          {report.configured_endpoint}")
    lines.append(f"  Upstream:          {report.upstream_repo}")
    lines.append("")

    b = report.buffered_multipart
    if b is not None:
        lines.append("-- Buffered Multipart --")
        lines.append(f"  Status:            {b.status}")
        lines.append(f"  HTTP status:       {b.http_status}")
        lines.append(f"  Content-Type:      {b.content_type}")
        lines.append(f"  Bytes:             {b.response_byte_count}")
        lines.append(f"  Audio non-empty:   {b.audio_non_empty}")
        lines.append(f"  WAV header valid:  {b.wav_header_valid}")
        lines.append(f"  Duration (ms):     {b.duration_ms:.1f}")
        if b.error_category:
            lines.append(f"  Error:             {b.error_category}")
        lines.append("")

    s = report.streaming_multipart
    if s is not None:
        lines.append("-- Streaming Multipart --")
        lines.append(f"  Status:            {s.status}")
        lines.append(f"  HTTP status:       {s.http_status}")
        lines.append(f"  Content-Type:      {s.content_type}")
        lines.append(f"  Sample rate:       {s.audio_sample_rate}")
        lines.append(f"  Channels:          {s.audio_channels}")
        lines.append(f"  Encoding:          {s.audio_encoding}")
        lines.append(f"  Non-empty reads:   {s.non_empty_read_count}")
        lines.append(f"  Total PCM bytes:   {s.total_pcm_bytes}")
        lines.append(f"  TTFB (ms):         {s.time_to_first_data_ms}")
        lines.append(f"  Duration (ms):     {s.total_duration_ms:.1f}")
        lines.append(f"  EOF reached:       {s.eof_reached}")
        lines.append(f"  Closed cleanly:    {s.stream_closed_cleanly}")
        lines.append(f"  Progressive:       {s.progressive_classification}")
        lines.append(f"  PCM aligned:       {s.pcm_frame_aligned}")
        if s.missing_audio_headers:
            lines.append(f"  Missing headers:   {', '.join(s.missing_audio_headers)}")
        if s.error_category:
            lines.append(f"  Error:             {s.error_category}")
        lines.append("")

    lj = report.legacy_json
    if lj is not None:
        lines.append("-- Legacy JSON Probe --")
        lines.append(f"  Status:            {lj.status}")
        lines.append(f"  HTTP status:       {lj.http_status}")
        lines.append(f"  Bytes:             {lj.response_byte_count}")
        if lj.error_category:
            lines.append(f"  Error:             {lj.error_category}")
        lines.append("")

    if report.warnings:
        lines.append("-- Warnings --")
        for w in report.warnings:
            lines.append(f"  ⚠ {w}")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)

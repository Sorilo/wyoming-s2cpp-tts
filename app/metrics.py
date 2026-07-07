"""Lightweight structured TTS metrics and tracing for synthesis requests.

Phase 5D adds request-local, concurrency-safe metrics collection across the
fake, buffered s2.cpp, and streaming s2.cpp synthesis paths.  Metrics use
``time.monotonic_ns()`` by default so durations are immune to wall-clock
adjustments; tests inject deterministic fake clocks.

.. important::

    Buffered-synthesis ``first_backend_data_ns`` records the moment the
    *completed* non-empty buffered response becomes available to the synthesis
    layer — it is **not** the literal first network byte arriving at the host.
    Streaming synthesis records the first non-empty chunk yielded by
    ``S2StreamResult``, which is the first backend data observed by this
    application but still not proof of the literal first network byte.

What this repository can measure directly:

* Synthesis-function entry (request_start)
* First non-empty audio data observed from the backend / synthetic source
* First ``AudioChunk`` produced by the synthesis function
* Emitted audio bytes and ``AudioChunk`` count
* Terminal status (success / error / cancellation observed by the coroutine)
* Total request duration (monotonic clock)

What requires external instrumentation:

* VAD completion
* STT completion
* LLM first token / complete response
* Wyoming network transmission (socket send)
* Home Assistant receipt
* Satellite receipt
* Audio-device playback start
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ── public snapshot ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SynthesisMetrics:
    """Immutable snapshot of metrics collected during one synthesis request.

    All timestamps are monotonic nanoseconds from ``time.monotonic_ns()``
    (or an injected fake clock).  Timestamps that were never populated
    (e.g. no non-empty backend data observed, no AudioChunk emitted) are
    ``None``.

    ``error_type`` is the Python exception class name (e.g. ``"ValueError"``)
    when ``terminal_status == "error"``, or ``None`` otherwise.
    """

    request_id: str
    """UUID hex string generated when no caller-supplied id is provided."""

    trace_id: Optional[str]
    """Optional externally-supplied trace / correlation identifier."""

    backend_type: str
    """"fake" or "s2cpp"."""

    synthesis_mode: str
    """"fake", "buffered", or "streaming"."""

    request_start_ns: int
    """Monotonic timestamp when synthesis measurement began."""

    first_backend_data_ns: Optional[int]
    """First non-empty audio data observed from the backend / synthetic source.

    See module-level docstring for precision limitations per synthesis mode.
    """

    first_audio_chunk_ns: Optional[int]
    """Timestamp immediately before the first ``AudioChunk`` was yielded /
    returned by the synthesis function.  ``None`` when no AudioChunk was
    emitted."""

    terminal_ns: int
    """Monotonic timestamp when metrics were finalized."""

    total_emitted_bytes: int
    """Sum of ``AudioChunk.audio`` payload lengths emitted."""

    emitted_chunk_count: int
    """Number of ``AudioChunk`` events emitted."""

    terminal_status: str
    """"success", "error", or "cancelled"."""

    error_type: Optional[str] = None
    """Python exception class name when terminal_status is "error"."""

    @property
    def duration_ns(self) -> int:
        """Total measured request duration in nanoseconds."""
        return self.terminal_ns - self.request_start_ns


# ── mutable collector ────────────────────────────────────────────────────────


class MetricsCollector:
    """Mutable per-request collector — records times/bytes then freezes.

    Typical usage::

        collector = MetricsCollector(backend_type="fake", synthesis_mode="fake")
        try:
            # ... synthesis ...
            collector.record_first_backend_data()
            for chunk in chunks:
                collector.record_first_audio_chunk()  # no-op after first
                collector.record_emitted_chunk(len(chunk.audio))
            metrics = collector.finalize("success")
        except Exception:
            metrics = collector.finalize("error", type(e).__name__)
            raise

    ``finalize()`` is idempotent-*safe* (raises ``RuntimeError`` on
    double-call) so callers catch bugs early.
    """

    def __init__(
        self,
        backend_type: str,
        synthesis_mode: str,
        *,
        request_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        clock: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._backend_type = backend_type
        self._synthesis_mode = synthesis_mode
        self._request_id = request_id or uuid.uuid4().hex
        self._trace_id = trace_id
        self._clock = clock

        self._request_start_ns: int = clock()
        self._first_backend_data_ns: Optional[int] = None
        self._first_audio_chunk_ns: Optional[int] = None
        self._total_emitted_bytes: int = 0
        self._emitted_chunk_count: int = 0
        self._terminal_ns: int = 0
        self._terminal_status: str = ""
        self._error_type: Optional[str] = None
        self._finalized: bool = False

    # -- read-only helpers (tests use these before finalize) -----------------

    @property
    def request_start_ns(self) -> int:
        return self._request_start_ns

    @property
    def first_backend_data_ns(self) -> Optional[int]:
        return self._first_backend_data_ns

    @property
    def first_audio_chunk_ns(self) -> Optional[int]:
        return self._first_audio_chunk_ns

    @property
    def total_emitted_bytes(self) -> int:
        return self._total_emitted_bytes

    @property
    def emitted_chunk_count(self) -> int:
        return self._emitted_chunk_count

    # -- recording -----------------------------------------------------------

    def record_first_backend_data(self) -> None:
        """Record the first non-empty backend / synthetic audio observation.

        Caller must ensure the data is genuinely non-empty — empty payloads
        must not trigger this.
        """
        if self._first_backend_data_ns is None:
            self._first_backend_data_ns = self._clock()

    def record_first_audio_chunk(self) -> None:
        """Record immediately before the first Wyoming ``AudioChunk`` is
        yielded or returned."""
        if self._first_audio_chunk_ns is None:
            self._first_audio_chunk_ns = self._clock()

    def record_emitted_chunk(self, byte_count: int) -> None:
        """Accumulate one emitted ``AudioChunk``."""
        self._total_emitted_bytes += byte_count
        self._emitted_chunk_count += 1

    # -- finalization --------------------------------------------------------

    def finalize(
        self,
        status: str,
        error_type: Optional[str] = None,
    ) -> SynthesisMetrics:
        """Freeze the collector and return an immutable snapshot.

        *status* must be ``"success"``, ``"error"``, or ``"cancelled"``.
        *error_type* is the Python exception class name when status is
        ``"error"``.

        Raises ``RuntimeError`` if already finalized (call-once guard).
        """
        if self._finalized:
            raise RuntimeError("MetricsCollector already finalized")
        self._finalized = True
        self._terminal_ns = self._clock()
        self._terminal_status = status
        self._error_type = error_type

        metrics = SynthesisMetrics(
            request_id=self._request_id,
            trace_id=self._trace_id,
            backend_type=self._backend_type,
            synthesis_mode=self._synthesis_mode,
            request_start_ns=self._request_start_ns,
            first_backend_data_ns=self._first_backend_data_ns,
            first_audio_chunk_ns=self._first_audio_chunk_ns,
            terminal_ns=self._terminal_ns,
            total_emitted_bytes=self._total_emitted_bytes,
            emitted_chunk_count=self._emitted_chunk_count,
            terminal_status=self._terminal_status,
            error_type=self._error_type,
        )

        # Structured log — safe: contains no text, audio bytes, or secrets.
        logger.info(
            "SynthesisMetrics request_id=%s mode=%s backend=%s status=%s "
            "duration_ns=%d bytes=%d chunks=%d",
            metrics.request_id,
            metrics.synthesis_mode,
            metrics.backend_type,
            metrics.terminal_status,
            metrics.duration_ns,
            metrics.total_emitted_bytes,
            metrics.emitted_chunk_count,
        )
        return metrics

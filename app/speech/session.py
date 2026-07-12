"""Phase 9B SynthesisSession — per-synthesis protocol lifecycle wrapper.

Tracks AudioStart/AudioStop state, client connectivity, streaming
synthesize-stopped eligibility, generator lifecycle, and exactly-once
cleanup for one Wyoming synthesis operation.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from app.speech.models import SpeechRequest


class SynthesisSession:
    """Protocol-adapter-owned record for one Wyoming synthesis lifecycle.

    Owns:
    - AudioStart / AudioStop emission tracking (idempotent)
    - client connectivity flag
    - streaming-only synthesize-stopped eligibility
    - optional async generator reference and exactly-once cleanup
    - cancellation flag

    Does NOT own: backend synthesis, retry loops, multipart/streaming
    transport, or queue scheduling.
    """

    def __init__(self, request: SpeechRequest, trigger: str = "legacy") -> None:
        self.request = request
        self.trigger = trigger
        self._audio_start_emitted = False
        self._audio_stop_emitted = False
        self._client_connected = True
        self._cancelled = False
        self._failed = False
        self._cleanup: Callable[[], Awaitable[None]] | None = None
        self._cleanup_done = False
        self._generator: Any = None

    # ── Convenience accessors ───────────────────────────────────────

    @property
    def synthesis_id(self) -> str:
        return self.request.synthesis_id

    @property
    def connection_id(self) -> str:
        return self.request.connection_id

    # ── Audio event tracking ────────────────────────────────────────

    @property
    def audio_start_emitted(self) -> bool:
        return self._audio_start_emitted

    @property
    def audio_stop_emitted(self) -> bool:
        return self._audio_stop_emitted

    def mark_audio_start(self) -> None:
        self._audio_start_emitted = True

    def mark_audio_stop(self) -> None:
        self._audio_stop_emitted = True

    # ── Streaming eligibility ───────────────────────────────────────

    @property
    def eligible_for_synthesize_stopped(self) -> bool:
        return self.trigger == "streaming" and not self._failed

    def mark_failed(self) -> None:
        """Record a terminal synthesis failure for success gating."""
        self._failed = True

    # ── Client state ────────────────────────────────────────────────

    @property
    def client_connected(self) -> bool:
        return self._client_connected

    def mark_client_disconnected(self) -> None:
        self._client_connected = False

    # ── Cancellation ────────────────────────────────────────────────

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def mark_cancelled(self) -> None:
        self._cancelled = True

    # ── Generator lifecycle ─────────────────────────────────────────

    @property
    def generator(self) -> Any:
        return self._generator

    def set_generator(self, gen: Any) -> None:
        """Store an async generator reference for cleanup."""
        self._generator = gen

    # ── Cleanup ─────────────────────────────────────────────────────

    def set_cleanup(self, cleanup: Callable[[], Awaitable[None]]) -> None:
        """Register an async cleanup callable (e.g. aclose generator, close writer)."""
        self._cleanup = cleanup

    async def disconnect(self) -> None:
        """Mark terminal and run cleanup exactly once."""
        self._client_connected = False
        if not self._cleanup_done and self._cleanup is not None:
            self._cleanup_done = True
            await self._cleanup()

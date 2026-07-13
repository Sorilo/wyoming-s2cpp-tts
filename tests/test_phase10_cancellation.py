"""Phase 10 cancellation regression tests — deterministic public-path.

Zero production code changes. Tests exercise real StreamingCoordinator
public APIs with asyncio.Event barriers — no arbitrary sleeps, no swallowed
Exceptions, no orphan create_task (every task retained and awaited).

Coverage (4 tests):
  1. Cancel active before output — consumer ends, no events, scheduler hooks/idle
  2. Cancel after first phrase while second is gated — no success AudioStop leaks
  3. Cancel then fresh replacement coordinator — normal one AudioStart/AudioStop
  4. Pending phrase clearing — cancel clears multiple pending, scheduler idle

synthesize_fn returns complete phrase atomically — tests gate the
*scheduler* before/after the operation, not within PCM output.
"""

from __future__ import annotations

import asyncio

import pytest
from wyoming.audio import AudioStart, AudioStop, AudioChunk
from wyoming.event import Event


# ── Deterministic fake synthesis ────────────────────────────────────────

async def _fake_synthesize(text: str) -> list[Event]:
    rate, width, channels = 22050, 2, 1
    chunk_bytes = 882
    num_chunks = min(max(len(text) // 10, 1), 5)
    events: list[Event] = [
        AudioStart(rate=rate, width=width, channels=channels).event(),
    ]
    for i in range(num_chunks):
        events.append(AudioChunk(
            rate=rate, width=width, channels=channels,
            audio=b"\x00" * chunk_bytes, timestamp=i * 20,
        ).event())
    events.append(AudioStop(timestamp=num_chunks * 20).event())
    return events


# ── Fake schedulers with asyncio.Event barriers ─────────────────────────

class _GateScheduler:
    """Gates run() with enter/block/exit Events; tracks cancel + idle."""

    def __init__(self):
        self.submissions: list[str] = []
        self.enter_gate = asyncio.Event()
        self.block_gate = asyncio.Event()
        self.exit_gate = asyncio.Event()
        self.cancelled_connections: set[str] = set()
        self.active_cancelled: set[str] = set()
        self._active_count = 0
        self._idle_changed = asyncio.Event()
        self._idle_changed.set()

    @property
    def is_idle(self) -> bool:
        return self._active_count == 0

    async def wait_idle(self, timeout: float = 5.0) -> None:
        if self._active_count == 0:
            return
        self._idle_changed.clear()
        await asyncio.wait_for(self._idle_changed.wait(), timeout=timeout)

    async def run(self, request, operation):
        self.submissions.append(request.text)
        self._active_count += 1
        self._idle_changed.clear()
        self.enter_gate.set()
        try:
            await self.block_gate.wait()
            await operation()
        finally:
            self._active_count -= 1
            self.exit_gate.set()
            if self._active_count == 0:
                self._idle_changed.set()

    async def cancel_connection(self, connection_id: str) -> int:
        self.cancelled_connections.add(connection_id)
        return 0

    def cancel_active_for_connection(self, connection_id: str) -> bool:
        self.active_cancelled.add(connection_id)
        return True


class _MultiGateScheduler:
    """Separate (enter, block, exit) Events per run() call; passthrough after."""

    def __init__(self, gates: list[tuple[asyncio.Event, asyncio.Event, asyncio.Event]]):
        self.submissions: list[str] = []
        self.cancelled_connections: set[str] = set()
        self.active_cancelled: set[str] = set()
        self._gates = gates
        self._idx = 0
        self._active_count = 0
        self._idle_changed = asyncio.Event()
        self._idle_changed.set()

    @property
    def is_idle(self) -> bool:
        return self._active_count == 0

    async def wait_idle(self, timeout: float = 5.0) -> None:
        if self._active_count == 0:
            return
        self._idle_changed.clear()
        await asyncio.wait_for(self._idle_changed.wait(), timeout=timeout)

    async def run(self, request, operation):
        self.submissions.append(request.text)
        self._active_count += 1
        self._idle_changed.clear()
        i = self._idx
        self._idx += 1
        if i < len(self._gates):
            enter, block, exited = self._gates[i]
            enter.set()
            try:
                await block.wait()
                await operation()
            finally:
                self._active_count -= 1
                exited.set()
                if self._active_count == 0:
                    self._idle_changed.set()
        else:
            try:
                await operation()
            finally:
                self._active_count -= 1
                if self._active_count == 0:
                    self._idle_changed.set()

    async def cancel_connection(self, connection_id: str) -> int:
        self.cancelled_connections.add(connection_id)
        return 0

    def cancel_active_for_connection(self, connection_id: str) -> bool:
        self.active_cancelled.add(connection_id)
        return True


class _PassthroughScheduler:
    """Runs operations immediately — no gating."""

    def __init__(self):
        self.submissions: list[str] = []
        self.cancelled_connections: set[str] = set()
        self.active_cancelled: set[str] = set()
        self._active_count = 0
        self._idle_changed = asyncio.Event()
        self._idle_changed.set()

    @property
    def is_idle(self) -> bool:
        return self._active_count == 0

    async def wait_idle(self, timeout: float = 5.0) -> None:
        if self._active_count == 0:
            return
        self._idle_changed.clear()
        await asyncio.wait_for(self._idle_changed.wait(), timeout=timeout)

    async def run(self, request, operation):
        self._active_count += 1
        self._idle_changed.clear()
        self.submissions.append(request.text)
        try:
            await operation()
        finally:
            self._active_count -= 1
            if self._active_count == 0:
                self._idle_changed.set()

    async def cancel_connection(self, connection_id: str) -> int:
        self.cancelled_connections.add(connection_id)
        return 0

    def cancel_active_for_connection(self, connection_id: str) -> bool:
        self.active_cancelled.add(connection_id)
        return True


# ── Helpers ─────────────────────────────────────────────────────────────

def _a_stop(events: list[Event]) -> int:
    return sum(1 for e in events if AudioStop.is_type(e.type))


def _a_start(events: list[Event]) -> int:
    return sum(1 for e in events if AudioStart.is_type(e.type))


# ═══════════════════════════════════════════════════════════════════════════
# Test 1: Cancel active before output — consumer ends, no events
# ═══════════════════════════════════════════════════════════════════════════

class TestCancelBeforeOutput:

    @pytest.mark.asyncio
    async def test_cancel_before_output_no_events_scheduler_idle(self):
        from app.speech.stream_coordinator import StreamingCoordinator

        s = _GateScheduler()
        s.block_gate.clear()
        c = StreamingCoordinator(s, _fake_synthesize, "conn-before")
        await c.start()
        c.feed_text("Hello world. Some text.")
        c.feed_done()

        events: list[Event] = []
        done = asyncio.Event()

        async def _consume():
            async for e in c:
                events.append(e)
            done.set()

        task = asyncio.create_task(_consume())
        await asyncio.wait_for(s.enter_gate.wait(), timeout=5.0)
        await c.cancel()
        await asyncio.wait_for(done.wait(), timeout=5.0)
        await task

        assert _a_stop(events) == 0
        assert _a_start(events) == 0
        assert len(events) == 0
        assert "conn-before" in s.cancelled_connections
        assert "conn-before" in s.active_cancelled

        s.block_gate.set()
        await s.wait_idle(timeout=5.0)
        assert s.is_idle


# ═══════════════════════════════════════════════════════════════════════════
# Test 2: Cancel after first phrase while second is gated
# ═══════════════════════════════════════════════════════════════════════════

class TestCancelAfterFirstPhrase:

    @pytest.mark.asyncio
    async def test_cancel_after_first_phrase_no_stale_audio_stop(self):
        from app.speech.stream_coordinator import StreamingCoordinator

        e1, b1, x1 = asyncio.Event(), asyncio.Event(), asyncio.Event()
        b1.set()
        e2, b2, x2 = asyncio.Event(), asyncio.Event(), asyncio.Event()
        # b2 stays unset

        s = _MultiGateScheduler([(e1, b1, x1), (e2, b2, x2)])
        c = StreamingCoordinator(s, _fake_synthesize, "conn-after-first")
        await c.start()
        c.feed_text("First phrase. Second phrase here.")
        c.feed_done()

        events: list[Event] = []
        done = asyncio.Event()

        async def _consume():
            async for e in c:
                events.append(e)
            done.set()

        task = asyncio.create_task(_consume())
        await asyncio.wait_for(x1.wait(), timeout=5.0)
        assert _a_start(events) == 1

        await asyncio.wait_for(e2.wait(), timeout=5.0)
        stop_before = _a_stop(events)

        await c.cancel()
        await asyncio.wait_for(done.wait(), timeout=5.0)
        await task

        b2.set()
        await asyncio.wait_for(x2.wait(), timeout=5.0)

        assert _a_stop(events) == stop_before
        assert done.is_set()
        await s.wait_idle(timeout=5.0)
        assert s.is_idle


# ═══════════════════════════════════════════════════════════════════════════
# Test 3: Cancel then fresh replacement — normal one AudioStart/AudioStop
# ═══════════════════════════════════════════════════════════════════════════

class TestCancelThenReplaceCoordinator:

    @pytest.mark.asyncio
    async def test_fresh_coordinator_after_cancel_normal_synthesis(self):
        from app.speech.stream_coordinator import StreamingCoordinator

        s = _PassthroughScheduler()

        old = StreamingCoordinator(s, _fake_synthesize, "conn-replace")
        await old.start()
        old.feed_text("Old phrase that will be cancelled.")
        old.feed_done()

        old_events: list[Event] = []
        old_done = asyncio.Event()

        async def _old_consume():
            async for e in old:
                old_events.append(e)
            old_done.set()

        old_task = asyncio.create_task(_old_consume())
        await old.cancel()
        await asyncio.wait_for(old_done.wait(), timeout=5.0)
        await old_task

        fresh = StreamingCoordinator(s, _fake_synthesize, "conn-replace")
        await fresh.start()
        fresh.feed_text("Fresh new phrase.")
        fresh.feed_done()

        fresh_events: list[Event] = []
        fresh_done = asyncio.Event()

        async def _fresh_consume():
            async for e in fresh:
                fresh_events.append(e)
            fresh_done.set()

        fresh_task = asyncio.create_task(_fresh_consume())
        await asyncio.wait_for(fresh_done.wait(), timeout=5.0)
        await fresh_task

        assert _a_start(fresh_events) == 1
        assert _a_stop(fresh_events) == 1
        chunks = [e for e in fresh_events if AudioChunk.is_type(e.type)]
        assert len(chunks) >= 1
        await s.wait_idle(timeout=5.0)
        assert s.is_idle


# ═══════════════════════════════════════════════════════════════════════════
# Test 4: Pending phrase clearing — cancel clears multiple pending, idle
# ═══════════════════════════════════════════════════════════════════════════

class TestPendingPhraseClearing:

    @pytest.mark.asyncio
    async def test_cancel_clears_pending_phrases_scheduler_idle(self):
        from app.speech.stream_coordinator import StreamingCoordinator

        s = _GateScheduler()
        s.block_gate.clear()
        c = StreamingCoordinator(s, _fake_synthesize, "conn-pending")
        await c.start()
        c.feed_text("One. Two. Three. Four.")
        c.feed_done()

        done = asyncio.Event()

        async def _consume():
            async for _ in c:
                pass
            done.set()

        task = asyncio.create_task(_consume())
        await asyncio.wait_for(s.enter_gate.wait(), timeout=5.0)
        await c.cancel()
        await asyncio.wait_for(done.wait(), timeout=5.0)
        await task

        s.block_gate.set()
        await s.wait_idle(timeout=5.0)

        assert s.is_idle
        assert "conn-pending" in s.cancelled_connections
        assert "conn-pending" in s.active_cancelled
        assert done.is_set()

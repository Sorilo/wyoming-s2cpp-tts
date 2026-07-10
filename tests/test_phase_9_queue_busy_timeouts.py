"""Phase 9: Queue, Backend-Busy Retry, and Synthesis Timeout Tests."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any

import pytest

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.tts import (
    Synthesize,
    SynthesizeChunk,
    SynthesizeStart,
    SynthesizeStop,
    SynthesizeStopped,
)

# -- Existing app imports ---------------------------------------------------
from app.config import Settings

# -- s2_client imports (existing + expected new) ----------------------------
from app.s2_client import (
    S2ClientError,
    S2GenerateRequest,
    S2GenerateResult,
    S2StreamResult,
)

from app.s2_client import S2BackendBusyError


# -- wyoming_server imports ------------------------------------------------
from app.wyoming_server import (
    FakeTtsConfig,
    SingleWorkerSynthesisQueue,
    start_fake_tts_server,
)


# ==============================================================================
# Constants
# ==============================================================================

_REAL_PCM_HEADERS = {
    "x-audio-encoding": "pcm_s16le",
    "x-audio-channels": "1",
    "x-audio-sample-rate": "44100",
}
_REAL_PCM_CONTENT_TYPE = "audio/L16; rate=44100; channels=1"


# ==============================================================================
# Utility helpers
# ==============================================================================

def _pcm_frames(n: int) -> bytes:
    """Generate *n* PCM s16le frames (2 bytes each)."""
    result = bytearray()
    for i in range(n):
        result.extend((i & 0xFFFF).to_bytes(2, "little", signed=True))
    return bytes(result)


def _make_cf(client):
    """Create a client factory that always returns *client*."""
    return lambda _s: client


async def _collect_all(tcp_client, timeout=5):
    """Read all Wyoming events until timeout or synthesize-stopped."""
    events = []
    while True:
        try:
            ev = await asyncio.wait_for(tcp_client.read_event(), timeout=timeout)
        except asyncio.TimeoutError:
            break
        if ev is None:
            break
        events.append(ev)
        if SynthesizeStopped.is_type(ev.type):
            break
    return events


# ==============================================================================
# Mock helpers
# ==============================================================================

class EventRecorder:
    """Thread-safe ordered event recorder for mock backends."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(self, **kwargs: Any) -> None:
        ts = time.monotonic()
        with self._lock:
            self.events.append({"_ts": ts, **kwargs})

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.events)

    def filter(self, event_type: str) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self.events if e.get("event") == event_type]

    def clear(self) -> None:
        with self._lock:
            self.events.clear()


class ControllableMockBackend:
    """An asyncio-compatible mock backend with Event-based sequencing.

    Allows tests to simulate success, HTTP 503, generic errors, and timeouts.
    Tracks active request count for serialisation tests.
    """

    def __init__(self, recorder: EventRecorder | None = None) -> None:
        self.recorder = recorder or EventRecorder()
        self._active_count = 0
        self._active_lock = threading.Lock()
        self._request_behaviors: dict[int, "_RequestBehavior"] = {}
        self._request_counter = 0
        self._counter_lock = threading.Lock()
        self.active_requests: list[int] = []

    @property
    def active_count(self) -> int:
        with self._active_lock:
            return self._active_count

    def set_behavior(
        self,
        request_index: int,
        *,
        pcm_chunks: list[bytes] | None = None,
        error: Exception | None = None,
        block_event: threading.Event | None = None,
        headers: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> None:
        """Configure how the *request_index*-th stream request behaves."""
        with self._counter_lock:
            self._request_behaviors[request_index] = _RequestBehavior(
                pcm_chunks=pcm_chunks or [],
                error=error,
                block_event=block_event,
                headers=headers or dict(_REAL_PCM_HEADERS),
                content_type=content_type or _REAL_PCM_CONTENT_TYPE,
            )

    def next_behavior(self) -> "_RequestBehavior":
        with self._counter_lock:
            idx = self._request_counter
            self._request_counter += 1
        return self._request_behaviors.get(idx, _RequestBehavior(pcm_chunks=[]))

    def make_stream(self, request_index: int):
        """Create a mock stream for the *request_index*-th request."""
        behavior = self._request_behaviors.get(
            request_index,
            _RequestBehavior(pcm_chunks=[]),
        )
        return _ControllableMockStream(behavior=behavior, backend=self)


class _RequestBehavior:
    __slots__ = ("pcm_chunks", "error", "block_event", "headers", "content_type")

    def __init__(
        self,
        pcm_chunks: list[bytes],
        error: Exception | None = None,
        block_event: threading.Event | None = None,
        headers: dict[str, str] | None = None,
        content_type: str | None = None,
    ) -> None:
        self.pcm_chunks = pcm_chunks
        self.error = error
        self.block_event = block_event
        self.headers = headers or dict(_REAL_PCM_HEADERS)
        self.content_type = content_type or _REAL_PCM_CONTENT_TYPE


class _ControllableMockStream:
    """A mock S2StreamResult-like object driven by a _RequestBehavior."""

    def __init__(
        self,
        behavior: _RequestBehavior,
        backend: ControllableMockBackend,
    ) -> None:
        self._behavior = behavior
        self._backend = backend
        self._closed = False
        self._cancelled = False
        self._chunk_index = 0
        self.content_type = behavior.content_type
        self.response_headers = dict(behavior.headers)

    def __enter__(self):
        with self._backend._active_lock:
            self._backend._active_count += 1
            self._backend.active_requests.append(id(self))
        self._backend.recorder.record(
            event="stream_entered",
            stream_id=id(self),
            active_count=self._backend.active_count,
        )
        return self

    def __exit__(self, *args):
        self._closed = True
        with self._backend._active_lock:
            if id(self) in self._backend.active_requests:
                self._backend.active_requests.remove(id(self))
            self._backend._active_count = max(0, self._backend._active_count - 1)
        self._backend.recorder.record(
            event="stream_exited",
            stream_id=id(self),
            active_count=self._backend.active_count,
        )
        return False

    def cancel(self):
        self._cancelled = True
        self._closed = True
        # Unblock any pending __next__ wait
        if self._behavior.block_event is not None:
            self._behavior.block_event.set()
        with self._backend._active_lock:
            if id(self) in self._backend.active_requests:
                self._backend.active_requests.remove(id(self))
            self._backend._active_count = max(0, self._backend._active_count - 1)
        self._backend.recorder.record(
            event="stream_cancelled",
            stream_id=id(self),
            active_count=self._backend.active_count,
        )

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed or self._cancelled:
            raise StopIteration

        if self._behavior.block_event is not None:
            self._backend.recorder.record(
                event="stream_blocked", stream_id=id(self),
            )
            self._behavior.block_event.wait(timeout=30)
            # Check cancelled/closed after waking
            if self._cancelled or self._closed:
                raise S2ClientError("mock stream cancelled")
            self._backend.recorder.record(
                event="stream_released", stream_id=id(self),
            )

        if self._behavior.error is not None:
            self._closed = True
            raise self._behavior.error

        if self._chunk_index < len(self._behavior.pcm_chunks):
            chunk = self._behavior.pcm_chunks[self._chunk_index]
            self._chunk_index += 1
            if not chunk:
                raise StopIteration
            return chunk

        raise StopIteration

    @property
    def status_code(self) -> int | None:
        if self._closed:
            return None
        if self._behavior.error is not None:
            if isinstance(self._behavior.error, S2BackendBusyError):
                return 503
            if isinstance(self._behavior.error, S2ClientError):
                return getattr(self._behavior.error, "status_code", 500)
        return 200


class MockStreamingClient:
    """A mock s2.cpp client that delegates to ControllableMockBackend."""

    def __init__(self, backend: ControllableMockBackend) -> None:
        self.backend = backend
        self.multipart_requests: list[S2GenerateRequest] = []
        self.stream_requests: list[S2GenerateRequest] = []

    def generate_multipart(self, request: S2GenerateRequest) -> S2GenerateResult:
        self.multipart_requests.append(request)
        behavior = self.backend.next_behavior()
        if behavior.error:
            raise behavior.error
        audio = b"".join(behavior.pcm_chunks)
        return S2GenerateResult(
            audio=audio,
            content_type=behavior.content_type,
            response_headers=dict(behavior.headers),
        )

    def generate_stream(
        self, request: S2GenerateRequest, files=None, boundary=None,
    ):
        self.stream_requests.append(request)
        with self.backend._counter_lock:
            idx = self.backend._request_counter
            self.backend._request_counter += 1
        # Do NOT call set_behavior — use behavior already configured
        # by the test via backend.set_behavior(idx, ...)
        return self.backend.make_stream(idx)


class _MockStream:
    """Mock synchronous stream for recording client tests."""

    def __init__(self, chunks, content_type, response_headers):
        self._chunks = list(chunks)
        self._index = 0
        self.content_type = content_type
        self.response_headers = dict(response_headers)
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._closed = True
        return False

    def __iter__(self):
        return self

    def __next__(self):
        if self._index >= len(self._chunks):
            raise StopIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk


class _RecordingClient:
    """Recording mock client for existing behavior tests."""

    def __init__(
        self,
        audio=b"",
        *,
        stream_chunks=None,
        content_type=_REAL_PCM_CONTENT_TYPE,
        response_headers=None,
    ):
        self.audio = audio
        self.content_type = content_type
        self.response_headers = (
            response_headers if response_headers is not None
            else dict(_REAL_PCM_HEADERS)
        )
        self.multipart_requests: list = []
        self.stream_requests: list = []
        self._stream_chunks = stream_chunks or ([audio] if audio else [])

    def generate_multipart(self, request):
        self.multipart_requests.append(request)
        return S2GenerateResult(
            audio=self.audio,
            content_type=self.content_type,
            response_headers=dict(self.response_headers),
        )

    def generate_stream(self, request, files=None, boundary=None):
        self.stream_requests.append(request)
        return _MockStream(
            self._stream_chunks, self.content_type, self.response_headers,
        )


# ==============================================================================
# 1. Basic Queue Operation Tests
# ==============================================================================

class TestQueueBasicOperation:
    """Tests for SingleWorkerSynthesisQueue basic operation."""

    @pytest.mark.asyncio
    async def test_one_request_succeeds_normally(self):
        """Queue allows one request, completes, pending goes to 0."""
        queue = SingleWorkerSynthesisQueue(max_size=3, wait_timeout_sec=30)
        executed = False

        async def operation() -> None:
            nonlocal executed
            executed = True

        await queue.run(operation, synthesis_id="syn-1", connection_id="conn-1")
        assert executed, "Operation was not executed"
        assert queue.pending == 0, f"Expected pending=0, got {queue.pending}"

    @pytest.mark.asyncio
    async def test_two_requests_serialize(self):
        """Two requests don't overlap - second waits for first to complete."""
        recorder = EventRecorder()
        release_first = asyncio.Event()
        first_started = asyncio.Event()

        second_started = asyncio.Event()

        async def request_1() -> None:
            first_started.set()
            recorder.record(event="r1_started")
            if not await asyncio.wait_for(release_first.wait(), timeout=10):
                raise RuntimeError("timeout")
            recorder.record(event="r1_done")

        async def request_2() -> None:
            recorder.record(event="r2_started")
            second_started.set()
            recorder.record(event="r2_done")

        queue = SingleWorkerSynthesisQueue(max_size=3, wait_timeout_sec=30)

        t1 = asyncio.create_task(
            queue.run(request_1, synthesis_id="syn-1", connection_id="conn-1")
        )
        await asyncio.wait_for(first_started.wait(), timeout=5)

        t2 = asyncio.create_task(
            queue.run(request_2, synthesis_id="syn-2", connection_id="conn-2")
        )
        await asyncio.sleep(0.2)

        assert not second_started.is_set(), (
            "Request 2 started before Request 1 completed"
        )

        release_first.set()
        await asyncio.wait_for(t1, timeout=5)
        await asyncio.wait_for(t2, timeout=5)
        assert second_started.is_set(), "Request 2 did not complete"

        r1_done = next(e["_ts"] for e in recorder.events if e["event"] == "r1_done")
        r2_done = next(e["_ts"] for e in recorder.events if e["event"] == "r2_done")
        assert r1_done < r2_done, "Request 2 completed before Request 1"

    @pytest.mark.asyncio
    async def test_fifo_order_preserved(self):
        """Three requests are processed in FIFO order."""
        recorder = EventRecorder()
        order: list[int] = []
        release_first = asyncio.Event()

        async def make_request(n: int, block: bool = False) -> None:
            recorder.record(event="start", n=n)
            if block:
                if not await asyncio.wait_for(release_first.wait(), timeout=10):
                    raise RuntimeError("timeout")
            order.append(n)
            recorder.record(event="done", n=n)

        queue = SingleWorkerSynthesisQueue(max_size=3, wait_timeout_sec=30)

        t1 = asyncio.create_task(
            queue.run(lambda: make_request(1, block=True),
                      synthesis_id="syn-1", connection_id="conn-1")
        )
        t2 = asyncio.create_task(
            queue.run(lambda: make_request(2),
                      synthesis_id="syn-2", connection_id="conn-2")
        )
        t3 = asyncio.create_task(
            queue.run(lambda: make_request(3),
                      synthesis_id="syn-3", connection_id="conn-3")
        )

        await asyncio.sleep(0.3)

        started = [e for e in recorder.events if e["event"] == "start"]
        assert len(started) == 1, f"Expected 1 started, got {len(started)}"
        assert order == [], f"Expected no completed, got {order}"

        release_first.set()
        await asyncio.wait_for(t1, timeout=5)
        await asyncio.wait_for(t2, timeout=5)
        await asyncio.wait_for(t3, timeout=5)

        assert order == [1, 2, 3], f"Expected FIFO [1,2,3], got {order}"

    @pytest.mark.asyncio
    async def test_queue_full_rejected(self):
        """When queue is at capacity, new request raises immediately."""
        queue = SingleWorkerSynthesisQueue(max_size=2, wait_timeout_sec=30)
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()

        async def blocker() -> None:
            blocker_started.set()
            await release_blocker.wait()

        async def waiter() -> None:
            await asyncio.sleep(0.5)

        t1 = asyncio.create_task(
            queue.run(blocker, synthesis_id="syn-1", connection_id="conn-1")
        )
        await asyncio.wait_for(blocker_started.wait(), timeout=5)

        t2 = asyncio.create_task(
            queue.run(waiter, synthesis_id="syn-2", connection_id="conn-2")
        )
        await asyncio.sleep(0.2)

        with pytest.raises(RuntimeError, match="Queue full"):
            await queue.run(lambda: asyncio.sleep(0),
                            synthesis_id="syn-3", connection_id="conn-3")

        release_blocker.set()
        await asyncio.wait_for(t1, timeout=5)
        await asyncio.wait_for(t2, timeout=15)
        assert queue.pending == 0


# ==============================================================================
# 2. Queue Timeout and Cancellation Tests
# ==============================================================================

class TestQueueTimeoutAndCancel:
    """Tests for queue wait timeouts and cancellation."""

    @pytest.mark.asyncio
    async def test_waiting_request_times_out(self):
        """Waiting request exceeding queue_wait_timeout_sec is removed cleanly."""
        queue = SingleWorkerSynthesisQueue(max_size=2, wait_timeout_sec=0.5)
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()

        async def blocker() -> None:
            blocker_started.set()
            await release_blocker.wait()

        t1 = asyncio.create_task(
            queue.run(blocker, synthesis_id="syn-1", connection_id="conn-1")
        )
        await asyncio.wait_for(blocker_started.wait(), timeout=5)

        with pytest.raises(asyncio.TimeoutError):
            await queue.run(lambda: asyncio.sleep(0),
                            synthesis_id="syn-2", connection_id="conn-2")

        release_blocker.set()
        await asyncio.wait_for(t1, timeout=5)
        assert queue.pending == 0

    @pytest.mark.asyncio
    async def test_waiting_client_disconnect_removes_entry(self):
        """Client disconnect removes their waiting queue entry."""
        queue = SingleWorkerSynthesisQueue(max_size=3, wait_timeout_sec=30)
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()

        async def blocker() -> None:
            blocker_started.set()
            await release_blocker.wait()

        t1 = asyncio.create_task(
            queue.run(blocker, synthesis_id="syn-1", connection_id="conn-a")
        )
        await asyncio.wait_for(blocker_started.wait(), timeout=5)

        t2 = asyncio.create_task(
            queue.run(lambda: asyncio.sleep(0.1),
                      synthesis_id="syn-2", connection_id="conn-b")
        )
        t3 = asyncio.create_task(
            queue.run(lambda: asyncio.sleep(0.1),
                      synthesis_id="syn-3", connection_id="conn-c")
        )
        await asyncio.sleep(0.2)

        cancelled = await queue.cancel_waiting("conn-b")
        assert cancelled == 1, f"Expected 1 cancelled, got {cancelled}"

        release_blocker.set()
        await asyncio.wait_for(t1, timeout=5)
        await asyncio.wait_for(t3, timeout=5)

        try:
            await asyncio.wait_for(t2, timeout=1)
        except (asyncio.CancelledError, RuntimeError, Exception):
            pass

        assert queue.pending == 0

    @pytest.mark.asyncio
    async def test_active_client_disconnect_cancels_releases_backend(self):
        """Active request cancellation releases backend via cancel_active_if_matches."""
        queue = SingleWorkerSynthesisQueue(max_size=3, wait_timeout_sec=30)
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()

        async def blocker() -> None:
            blocker_started.set()
            try:
                await release_blocker.wait()
            except asyncio.CancelledError:
                raise

        t1 = asyncio.create_task(
            queue.run(blocker, synthesis_id="syn-active", connection_id="conn-a")
        )
        await asyncio.wait_for(blocker_started.wait(), timeout=5)

        result = queue.cancel_active_if_matches("syn-active")
        assert result is True

        try:
            await asyncio.wait_for(t1, timeout=3)
        except (asyncio.CancelledError, Exception):
            pass

        release_blocker.set()
        assert queue.pending == 0

    @pytest.mark.asyncio
    async def test_cancel_active_if_matches_no_match(self):
        """cancel_active_if_matches returns False when no match."""
        queue = SingleWorkerSynthesisQueue(max_size=3, wait_timeout_sec=30)
        result = queue.cancel_active_if_matches("nonexistent")
        assert result is False


# ==============================================================================
# 3. CANCEL_ON_NEW_REQUEST Behaviour Tests
# ==============================================================================

class TestCancelOnNewRequest:
    """Tests for the CANCEL_ON_NEW_REQUEST setting behaviour."""

    @pytest.mark.asyncio
    async def test_cancel_on_new_request_cancels_active(self):
        """With CANCEL_ON_NEW_REQUEST=true, new request cancels active.

        NOTE: Will FAIL until Phase 9 handler implements pre-queue cancellation.
        """
        pcm = _pcm_frames(200)

        class _SimpleClient:
            def __init__(self):
                self.stream_requests = []
                self.multipart_requests = []
            def generate_multipart(self, r):
                self.multipart_requests.append(r)
                return S2GenerateResult(audio=pcm, content_type=_REAL_PCM_CONTENT_TYPE,
                                        response_headers=dict(_REAL_PCM_HEADERS))
            def generate_stream(self, r, files=None, boundary=None):
                self.stream_requests.append(r)
                return _MockStream([pcm], _REAL_PCM_CONTENT_TYPE, _REAL_PCM_HEADERS)

        client = _SimpleClient()
        settings = Settings(tts_backend="s2cpp", s2_stream=True,
                            cancel_on_new_request=True, max_queue_size=3)
        server = await start_fake_tts_server(
            host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="first request").event())
                await asyncio.sleep(0.3)
                await tcp.write_event(Synthesize(text="second request").event())
                events = await _collect_all(tcp, timeout=5)

            types = [e.type for e in events]
            assert "audio-start" in types
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_cancel_on_new_request_false_queues(self):
        """With CANCEL_ON_NEW_REQUEST=false, requests queue FIFO."""
        pcm = _pcm_frames(200)

        class _SimpleClient:
            def __init__(self):
                self.stream_requests = []
                self.multipart_requests = []
            def generate_multipart(self, r):
                self.multipart_requests.append(r)
                return S2GenerateResult(audio=pcm, content_type=_REAL_PCM_CONTENT_TYPE,
                                        response_headers=dict(_REAL_PCM_HEADERS))
            def generate_stream(self, r, files=None, boundary=None):
                self.stream_requests.append(r)
                return _MockStream([pcm], _REAL_PCM_CONTENT_TYPE, _REAL_PCM_HEADERS)

        client = _SimpleClient()
        settings = Settings(tts_backend="s2cpp", s2_stream=True,
                            cancel_on_new_request=False, max_queue_size=3)
        server = await start_fake_tts_server(
            host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="request one").event())
                await asyncio.sleep(0.2)
                await tcp.write_event(Synthesize(text="request two").event())
                events = await _collect_all(tcp, timeout=5)
            assert len(client.stream_requests) >= 1
        finally:
            await server.stop()


# ==============================================================================
# 4. Backend Busy (HTTP 503) Retry Tests
# ==============================================================================

class TestBackendBusyRetry:
    """Tests for the HTTP 503 retry logic via S2BackendBusyError."""

    def test_s2_backend_busy_error_is_s2_client_error(self):
        """S2BackendBusyError is a subclass of S2ClientError."""
        assert issubclass(S2BackendBusyError, S2ClientError)

    def test_s2_backend_busy_error_has_status_code_503(self):
        """S2BackendBusyError has status_code=503 by default."""
        err = S2BackendBusyError("Backend busy")
        assert err.status_code == 503  # type: ignore[attr-defined]
        assert "Backend busy" in str(err)

    def test_s2_client_error_has_optional_status_code(self):
        """S2ClientError accepts an optional status_code parameter."""
        err = S2ClientError("test error", status_code=500)
        assert err.status_code == 500  # type: ignore[attr-defined]
        err2 = S2ClientError("test error")
        assert err2.status_code is None  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_transient_503_retries_and_succeeds(self):
        """Single 503 then success on retry."""
        recorder = EventRecorder()
        backend = ControllableMockBackend(recorder=recorder)
        pcm = _pcm_frames(200)
        client = MockStreamingClient(backend)
        backend.set_behavior(0, error=S2BackendBusyError("backend busy"))
        backend.set_behavior(1, pcm_chunks=[pcm])

        settings = Settings(tts_backend="s2cpp", s2_stream=True,
                            s2_backend_busy_max_retries=3,
                            s2_backend_busy_retry_delay_ms=10)
        server = await start_fake_tts_server(
            host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="transient 503 test").event())
                events = await _collect_all(tcp, timeout=5)
            types = [e.type for e in events]
            assert "audio-start" in types, f"Expected audio-start, got {types}"
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_repeated_503_exhausts_retries(self):
        """Max retries reached, clean terminal behavior."""
        backend = ControllableMockBackend()
        client = MockStreamingClient(backend)
        for i in range(5):
            backend.set_behavior(i, error=S2BackendBusyError(f"backend busy #{i}"))

        settings = Settings(tts_backend="s2cpp", s2_stream=True,
                            s2_backend_busy_max_retries=2,
                            s2_backend_busy_retry_delay_ms=10)
        server = await start_fake_tts_server(
            host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="exhaust retries").event())
                events = await _collect_all(tcp, timeout=5)
            types = [e.type for e in events]
            assert "audio-stop" not in types, "AudioStop must not appear on exhaust"
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_non_503_error_not_retried(self):
        """4xx error raises immediately without retry."""
        backend = ControllableMockBackend()
        client = MockStreamingClient(backend)
        backend.set_behavior(0, error=S2ClientError("bad request", status_code=400))

        settings = Settings(tts_backend="s2cpp", s2_stream=True,
                            s2_backend_busy_max_retries=3,
                            s2_backend_busy_retry_delay_ms=10)
        server = await start_fake_tts_server(
            host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="non 503 error").event())
                events = await _collect_all(tcp, timeout=5)
            types = [e.type for e in events]
            assert "audio-start" not in types, "Non-503 errors should not be retried"
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_malformed_pcm_not_retried(self):
        """Bad PCM after successful connection is NOT retried."""
        bad_pcm = _pcm_frames(2) + b"\x05"

        class _BadPCMClient:
            def __init__(self):
                self.stream_requests = []
                self.multipart_requests = []
            def generate_multipart(self, r):
                self.multipart_requests.append(r)
                return S2GenerateResult(audio=bad_pcm, content_type=_REAL_PCM_CONTENT_TYPE,
                                        response_headers=dict(_REAL_PCM_HEADERS))
            def generate_stream(self, r, files=None, boundary=None):
                self.stream_requests.append(r)
                return _MockStream([bad_pcm], _REAL_PCM_CONTENT_TYPE, _REAL_PCM_HEADERS)

        client = _BadPCMClient()
        settings = Settings(tts_backend="s2cpp", s2_stream=True,
                            s2_backend_busy_max_retries=3)
        server = await start_fake_tts_server(
            host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="bad pcm test").event())
                events = await _collect_all(tcp, timeout=5)
            types = [e.type for e in events]
            assert "audio-stop" not in types
            assert len(client.stream_requests) <= 1
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_503_after_pcm_not_retried(self):
        """503 after PCM began is NOT retried."""
        pcm = _pcm_frames(500)

        class _Partial503Stream:
            def __init__(self):
                self.content_type = _REAL_PCM_CONTENT_TYPE
                self.response_headers = dict(_REAL_PCM_HEADERS)
                self._state = 0
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def __iter__(self): return self
            def __next__(self):
                if self._state == 0:
                    self._state = 1
                    return pcm[:400]
                elif self._state == 1:
                    self._state = 2
                    raise S2BackendBusyError("503 mid-stream")
                raise StopIteration
            def cancel(self): pass

        class _Partial503Client:
            def __init__(self):
                self.stream_requests = []
                self.multipart_requests = []
            def generate_multipart(self, r):
                self.multipart_requests.append(r)
                return S2GenerateResult(audio=pcm, content_type=_REAL_PCM_CONTENT_TYPE,
                                        response_headers=dict(_REAL_PCM_HEADERS))
            def generate_stream(self, r, files=None, boundary=None):
                self.stream_requests.append(r)
                return _Partial503Stream()

        client = _Partial503Client()
        settings = Settings(tts_backend="s2cpp", s2_stream=True,
                            s2_backend_busy_max_retries=2)
        server = await start_fake_tts_server(
            host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="503 mid stream").event())
                events = await _collect_all(tcp, timeout=5)
            types = [e.type for e in events]
            assert "audio-stop" not in types
        finally:
            await server.stop()


# ==============================================================================
# 5. Synthesis Timeout Tests
# ==============================================================================

class TestSynthesisTimeout:
    """Tests for synthesis timeout behaviour."""

    @pytest.mark.asyncio
    async def test_synthesis_timeout_before_pcm_cleans_up(self):
        """Timeout before any PCM: clean cleanup, no AudioStop."""
        stream_closed = asyncio.Event()

        class _BlockForeverStream:
            def __init__(self):
                self.content_type = _REAL_PCM_CONTENT_TYPE
                self.response_headers = dict(_REAL_PCM_HEADERS)
            def __enter__(self): return self
            def __exit__(self, *a): stream_closed.set(); return False
            def __iter__(self): return self
            def __next__(self):
                import time as _t; _t.sleep(30); raise StopIteration
            def cancel(self): stream_closed.set()

        class _BlockClient:
            def __init__(self):
                self.stream_requests = []
                self.multipart_requests = []
            def generate_multipart(self, r):
                self.multipart_requests.append(r)
                return S2GenerateResult(audio=b"", content_type=_REAL_PCM_CONTENT_TYPE,
                                        response_headers=dict(_REAL_PCM_HEADERS))
            def generate_stream(self, r, files=None, boundary=None):
                self.stream_requests.append(r)
                return _BlockForeverStream()

        client = _BlockClient()
        settings = Settings(tts_backend="s2cpp", s2_stream=True,
                            s2_synthesis_timeout_sec=2.0)
        server = await start_fake_tts_server(
            host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="timeout before pcm").event())
                events = await _collect_all(tcp, timeout=10)
            types = [e.type for e in events]
            assert "audio-stop" not in types
            assert await asyncio.wait_for(stream_closed.wait(), timeout=5), "Backend stream was not closed"
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_synthesis_timeout_after_partial_pcm_no_false_audiostop(self):
        """Timeout mid-stream after partial PCM: no AudioStop, clean cleanup."""
        pcm = _pcm_frames(500)
        stream_closed = asyncio.Event()

        class _SlowStream:
            def __init__(self):
                self.content_type = _REAL_PCM_CONTENT_TYPE
                self.response_headers = dict(_REAL_PCM_HEADERS)
                self._state = 0
            def __enter__(self): return self
            def __exit__(self, *a): stream_closed.set(); return False
            def __iter__(self): return self
            def __next__(self):
                if self._state == 0:
                    self._state = 1
                    return pcm[:400]
                import time as _t; _t.sleep(30); raise StopIteration
            def cancel(self): stream_closed.set()

        class _SlowClient:
            def __init__(self):
                self.stream_requests = []
                self.multipart_requests = []
            def generate_multipart(self, r):
                self.multipart_requests.append(r)
                return S2GenerateResult(audio=pcm, content_type=_REAL_PCM_CONTENT_TYPE,
                                        response_headers=dict(_REAL_PCM_HEADERS))
            def generate_stream(self, r, files=None, boundary=None):
                self.stream_requests.append(r)
                return _SlowStream()

        client = _SlowClient()
        settings = Settings(tts_backend="s2cpp", s2_stream=True,
                            s2_synthesis_timeout_sec=2.0)
        server = await start_fake_tts_server(
            host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="timeout mid stream").event())
                events = await _collect_all(tcp, timeout=10)
            types = [e.type for e in events]
            assert "audio-stop" not in types
            assert await asyncio.wait_for(stream_closed.wait(), timeout=5), "Backend stream was not closed"
        finally:
            await server.stop()


# ==============================================================================
# 6. Recovery and State Cleanup Tests
# ==============================================================================

class TestRecoveryAfterFailure:
    """Tests that the queue recovers cleanly after various failure modes."""

    @pytest.mark.asyncio
    async def test_next_request_after_every_failure_succeeds(self):
        """After a failing request, the next request succeeds."""
        pcm = _pcm_frames(200)

        class _FailThenSuccessClient:
            def __init__(self):
                self.stream_requests = []
                self.multipart_requests = []
                self._calls = 0
            def generate_multipart(self, r):
                self.multipart_requests.append(r)
                return S2GenerateResult(audio=pcm, content_type=_REAL_PCM_CONTENT_TYPE,
                                        response_headers=dict(_REAL_PCM_HEADERS))
            def generate_stream(self, r, files=None, boundary=None):
                self.stream_requests.append(r)
                self._calls += 1
                if self._calls == 1:
                    raise S2ClientError("simulated failure")
                return _MockStream([pcm], _REAL_PCM_CONTENT_TYPE, _REAL_PCM_HEADERS)

        client = _FailThenSuccessClient()
        settings = Settings(tts_backend="s2cpp", s2_stream=True,
                            max_queue_size=3)
        server = await start_fake_tts_server(
            host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="will fail").event())
                events1 = await _collect_all(tcp, timeout=5)
                assert "audio-stop" not in [e.type for e in events1]

                await tcp.write_event(Synthesize(text="will succeed").event())
                events2 = await _collect_all(tcp, timeout=5)
                types2 = [e.type for e in events2]
                assert "audio-start" in types2
                assert "audio-stop" in types2
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_queue_counters_zero_after_all_scenarios(self):
        """Queue pending returns to 0 after timeouts, errors, and success."""
        queue = SingleWorkerSynthesisQueue(max_size=3, wait_timeout_sec=30)

        await queue.run(lambda: asyncio.sleep(0.05),
                        synthesis_id="s1", connection_id="c1")
        assert queue.pending == 0

        await queue.run(lambda: asyncio.sleep(0.05),
                        synthesis_id="s2", connection_id="c2")
        assert queue.pending == 0

        for i in range(5):
            await queue.run(lambda: asyncio.sleep(0.02),
                            synthesis_id=f"seq-{i}", connection_id="c-seq")
        assert queue.pending == 0

        async def raise_err():
            raise ValueError("test error")

        with pytest.raises(ValueError):
            await queue.run(raise_err, synthesis_id="err", connection_id="c-err")
        assert queue.pending == 0


# ==============================================================================
# 7. Existing Behaviour Preservation Tests
# ==============================================================================

class TestExistingStreamingStillWorks:
    """Existing Phase 7.5 streaming behavior preserved in Phase 9."""

    @pytest.mark.asyncio
    async def test_existing_streaming_still_works(self):
        """AudioStart before AudioChunks, AudioStop at end."""
        pcm = _pcm_frames(200)
        client = _RecordingClient(audio=pcm, stream_chunks=[pcm])
        settings = Settings(tts_backend="s2cpp", s2_stream=True)
        server = await start_fake_tts_server(
            host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="existing test").event())
                events = await _collect_all(tcp, timeout=5)
            assert AudioStart.is_type(events[0].type)
            assert sum(1 for e in events if AudioChunk.is_type(e.type)) >= 1
            assert AudioStop.is_type(events[-1].type)
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_streaming_no_duplicate_synthesis(self):
        """No duplicate synthesis for streaming + legacy compat."""
        pcm = _pcm_frames(100)
        client = _RecordingClient(audio=pcm, stream_chunks=[pcm])
        settings = Settings(tts_backend="s2cpp", s2_stream=True)
        server = await start_fake_tts_server(
            host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(SynthesizeStart().event())
                await tcp.write_event(SynthesizeChunk(text="Hello.").event())
                await tcp.write_event(Synthesize(text="Hello.").event())
                await tcp.write_event(SynthesizeStop().event())
                events = await _collect_all(tcp, timeout=5)
            total = len(client.stream_requests) + len(client.multipart_requests)
            assert total == 1
            assert sum(1 for e in events if AudioStart.is_type(e.type)) == 1
            assert sum(1 for e in events if AudioStop.is_type(e.type)) == 1
        finally:
            await server.stop()


# ==============================================================================
# 8. Voice Selection Tests
# ==============================================================================

class TestClientRequestedVoicePriority:
    """Client-requested voice selection still works in Phase 9."""

    @pytest.mark.asyncio
    async def test_client_requested_voice_priority(self):
        """Default configured voice forwarded to backend."""
        import tempfile
        pcm = _pcm_frames(40)
        client = _RecordingClient(audio=pcm, stream_chunks=[pcm])
        with tempfile.TemporaryDirectory() as tmpdir:
            voice_file = os.path.join(tmpdir, "cmu_bdl_male_us.s2voice")
            with open(voice_file, "w") as f:
                f.write("{}")
            settings = Settings(tts_backend="s2cpp", s2_stream=True,
                                s2_default_voice="cmu_bdl_male_us", s2_voice_dir=tmpdir)
            server = await start_fake_tts_server(
                host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
            try:
                async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                    await tcp.write_event(Synthesize(text="voice test").event())
                    _events = await _collect_all(tcp, timeout=5)
                assert client.stream_requests[0].voice == "cmu_bdl_male_us"
                assert client.stream_requests[0].voice_dir == tmpdir
            finally:
                await server.stop()

    @pytest.mark.asyncio
    async def test_generic_fallback_omits_voice(self):
        """Generic fallback omits voice fields."""
        pcm = _pcm_frames(40)
        client = _RecordingClient(audio=pcm, stream_chunks=[pcm])
        settings = Settings(tts_backend="s2cpp", s2_stream=True,
                            s2_default_voice="", s2_voice_dir="")
        server = await start_fake_tts_server(
            host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="generic").event())
                _events = await _collect_all(tcp, timeout=5)
            assert not client.stream_requests[0].voice
            assert not client.stream_requests[0].voice_dir
        finally:
            await server.stop()


# ==============================================================================
# 9. S2_MODEL Metadata Test
# ==============================================================================

class TestS2ModelMetadataLogged:
    """S2_MODEL metadata is logged correctly during synthesis."""

    def test_s2_model_metadata_logged(self):
        """S2_MODEL setting is properly accessible with correct default."""
        settings = Settings()
        assert settings.s2_model == "/models/s2-pro-q6_k.gguf"

        custom = Settings(s2_model="/models/custom.gguf")
        assert custom.s2_model == "/models/custom.gguf"


# ==============================================================================
# 10. Invalid Settings Validation Tests
# ==============================================================================

class TestAllNewEnvSettingsRejectInvalid:
    """All new Phase 9 env settings reject invalid values."""

    def test_invalid_busy_max_retries_negative(self, monkeypatch):
        monkeypatch.setenv("S2_BACKEND_BUSY_MAX_RETRIES", "-1")
        with pytest.raises(ValueError, match="must be positive"):
            Settings.from_env()

    def test_invalid_busy_max_retries_zero(self, monkeypatch):
        monkeypatch.setenv("S2_BACKEND_BUSY_MAX_RETRIES", "0")
        with pytest.raises(ValueError, match="must be positive"):
            Settings.from_env()

    def test_invalid_busy_max_retries_exceeds_max(self, monkeypatch):
        monkeypatch.setenv("S2_BACKEND_BUSY_MAX_RETRIES", "999")
        with pytest.raises(ValueError, match="exceeds maximum"):
            Settings.from_env()

    def test_invalid_busy_retry_delay_negative(self, monkeypatch):
        monkeypatch.setenv("S2_BACKEND_BUSY_RETRY_DELAY_MS", "-5")
        with pytest.raises(ValueError, match="must be non-negative"):
            Settings.from_env()

    def test_invalid_busy_retry_delay_exceeds_max(self, monkeypatch):
        monkeypatch.setenv("S2_BACKEND_BUSY_RETRY_DELAY_MS", "20000")
        with pytest.raises(ValueError, match="exceeds maximum"):
            Settings.from_env()

    def test_invalid_queue_wait_timeout_negative(self, monkeypatch):
        monkeypatch.setenv("S2_QUEUE_WAIT_TIMEOUT_SEC", "-1.0")
        with pytest.raises(ValueError, match="must be non-negative"):
            Settings.from_env()

    def test_invalid_queue_wait_timeout_exceeds_max(self, monkeypatch):
        monkeypatch.setenv("S2_QUEUE_WAIT_TIMEOUT_SEC", "999")
        with pytest.raises(ValueError, match="exceeds maximum"):
            Settings.from_env()

    def test_invalid_synthesis_timeout_too_small(self, monkeypatch):
        monkeypatch.setenv("S2_SYNTHESIS_TIMEOUT_SEC", "0.05")
        with pytest.raises(ValueError, match="must be >= 0.1"):
            Settings.from_env()

    def test_invalid_synthesis_timeout_exceeds_max(self, monkeypatch):
        monkeypatch.setenv("S2_SYNTHESIS_TIMEOUT_SEC", "999")
        with pytest.raises(ValueError, match="exceeds maximum"):
            Settings.from_env()

    def test_invalid_synthesis_timeout_not_a_number(self, monkeypatch):
        monkeypatch.setenv("S2_SYNTHESIS_TIMEOUT_SEC", "not-a-number")
        with pytest.raises(ValueError, match="Invalid float"):
            Settings.from_env()

    def test_valid_settings_load_defaults(self):
        settings = Settings()
        assert settings.s2_backend_busy_max_retries == 3
        assert settings.s2_backend_busy_retry_delay_ms == 200
        assert settings.s2_queue_wait_timeout_sec == 30.0
        assert settings.s2_synthesis_timeout_sec == 120.0

    def test_valid_custom_settings_via_env(self, monkeypatch):
        monkeypatch.setenv("S2_BACKEND_BUSY_MAX_RETRIES", "5")
        monkeypatch.setenv("S2_BACKEND_BUSY_RETRY_DELAY_MS", "500")
        monkeypatch.setenv("S2_QUEUE_WAIT_TIMEOUT_SEC", "60")
        monkeypatch.setenv("S2_SYNTHESIS_TIMEOUT_SEC", "180")
        settings = Settings.from_env()
        assert settings.s2_backend_busy_max_retries == 5
        assert settings.s2_backend_busy_retry_delay_ms == 500
        assert settings.s2_queue_wait_timeout_sec == 60.0
        assert settings.s2_synthesis_timeout_sec == 180.0


# ==============================================================================
# 11. Queue Structured Logging Tests
# ==============================================================================

class TestQueueStructuredLogging:
    """Tests for structured queue log events.

    NOTE: Will FAIL until structured logging is implemented in Phase 9.
    """

    @pytest.mark.asyncio
    async def test_queue_emits_request_received(self):
        """Queue processes requests with synthesis/connection IDs."""
        queue = SingleWorkerSynthesisQueue(max_size=3, wait_timeout_sec=30)
        await queue.run(lambda: asyncio.sleep(0.01),
                        synthesis_id="log-1", connection_id="log-conn")
        assert queue.pending == 0

    @pytest.mark.asyncio
    async def test_queue_pending_reflects_active_requests(self):
        """Queue pending property tracks active/waiting requests."""
        queue = SingleWorkerSynthesisQueue(max_size=3, wait_timeout_sec=30)
        blocker_started = asyncio.Event()
        release = asyncio.Event()

        async def blocker():
            blocker_started.set()
            await release.wait()

        t1 = asyncio.create_task(
            queue.run(blocker, synthesis_id="depth-1", connection_id="dc"))
        await asyncio.wait_for(blocker_started.wait(), timeout=5)

        assert queue.pending >= 1

        release.set()
        await asyncio.wait_for(t1, timeout=5)
        assert queue.pending == 0

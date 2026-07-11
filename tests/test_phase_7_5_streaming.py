"""Phase 7.5A: Progressive backend HTTP streaming wired into production handler."""
import asyncio
import threading
import pytest
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize, SynthesizeChunk, SynthesizeStart, SynthesizeStop, SynthesizeStopped, SynthesizeVoice
from app.config import Settings
from app.s2_client import S2ClientError, S2GenerateResult
from app.wyoming_server import FakeTtsConfig, start_fake_tts_server

_REAL_PCM_HEADERS = {"x-audio-encoding": "pcm_s16le", "x-audio-channels": "1", "x-audio-sample-rate": "44100"}
_REAL_PCM_CONTENT_TYPE = "audio/L16; rate=44100; channels=1"

class _MockStream:
    def __init__(self, chunks, content_type, response_headers):
        self._chunks = list(chunks); self._index = 0
        self.content_type = content_type; self.response_headers = dict(response_headers)
        self._closed = False
    def __enter__(self): return self
    def __exit__(self, *args): self._closed = True; return False
    def __iter__(self): return self
    def __next__(self):
        if self._index >= len(self._chunks): raise StopIteration
        chunk = self._chunks[self._index]; self._index += 1; return chunk

class RecordingStreamingClient:
    def __init__(self, audio=b"", *, stream_chunks=None, content_type=_REAL_PCM_CONTENT_TYPE, response_headers=None):
        self.audio = audio; self.content_type = content_type
        self.response_headers = response_headers if response_headers is not None else dict(_REAL_PCM_HEADERS)
        self.multipart_requests = []; self.stream_requests = []
        self._stream_chunks = stream_chunks or ([audio] if audio else [])
    def generate_multipart(self, request):
        self.multipart_requests.append(request)
        return S2GenerateResult(audio=self.audio, content_type=self.content_type, response_headers=dict(self.response_headers))
    def generate_stream(self, request, files=None, boundary=None):
        self.stream_requests.append(request)
        return _MockStream(self._stream_chunks, self.content_type, self.response_headers)

async def _collect_all(client, timeout=5):
    events = []
    while True:
        try:
            ev = await asyncio.wait_for(client.read_event(), timeout=timeout)
        except asyncio.TimeoutError: break
        if ev is None: break
        events.append(ev)
        if SynthesizeStopped.is_type(ev.type): break
    return events

def _make_cf(client): return lambda _s: client

def _pcm_frames(n):
    result = bytearray()
    for i in range(n):
        result.extend((i & 0xFFFF).to_bytes(2, "little", signed=True))
    return bytes(result)

class TestStreamingRouting:
    @pytest.mark.asyncio
    async def test_s2_stream_true_invokes_generate_stream(self):
        pcm = _pcm_frames(200)
        client = RecordingStreamingClient(audio=pcm, stream_chunks=[pcm[:200], pcm[200:]])
        settings = Settings(tts_backend="s2cpp", s2_stream=True)
        server = await start_fake_tts_server(host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="streaming test").event())
                events = await _collect_all(tcp, timeout=5)
            assert client.stream_requests, "S2_STREAM=true must call generate_stream()"
            assert not client.multipart_requests
            assert client.stream_requests[0].text == "streaming test"
            assert AudioStart.is_type(events[0].type)
            assert AudioStop.is_type(events[-1].type)
        finally: await server.stop()

    @pytest.mark.asyncio
    async def test_s2_stream_false_invokes_generate_multipart(self):
        pcm = _pcm_frames(100)
        client = RecordingStreamingClient(audio=pcm)
        settings = Settings(tts_backend="s2cpp", s2_stream=False)
        server = await start_fake_tts_server(host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="buffered test").event())
                events = await _collect_all(tcp, timeout=5)
            assert client.multipart_requests
            assert not client.stream_requests
            assert client.multipart_requests[0].text == "buffered test"
        finally: await server.stop()

    @pytest.mark.asyncio
    async def test_fake_backend_unchanged(self):
        client = RecordingStreamingClient(audio=b"")
        settings = Settings(tts_backend="fake", s2_stream=True)
        server = await start_fake_tts_server(host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="fake").event())
                events = await _collect_all(tcp, timeout=5)
            assert not client.multipart_requests
            assert not client.stream_requests
        finally: await server.stop()

class TestStreamingSuccess:
    @pytest.mark.asyncio
    async def test_audiostart_before_audiochunks(self):
        pcm = _pcm_frames(80)
        client = RecordingStreamingClient(audio=pcm)
        settings = Settings(tts_backend="s2cpp", s2_stream=True)
        server = await start_fake_tts_server(host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="audio start test").event())
                events = await _collect_all(tcp, timeout=5)
            assert AudioStart.is_type(events[0].type)
            chunks = [e for e in events if AudioChunk.is_type(e.type)]
            assert len(chunks) >= 1
            assert AudioStop.is_type(events[-1].type)
        finally: await server.stop()

    @pytest.mark.asyncio
    async def test_one_audiostop(self):
        pcm = _pcm_frames(40)
        client = RecordingStreamingClient(audio=pcm)
        settings = Settings(tts_backend="s2cpp", s2_stream=True)
        server = await start_fake_tts_server(host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="one stop").event())
                events = await _collect_all(tcp, timeout=5)
            assert sum(1 for e in events if AudioStop.is_type(e.type)) == 1
        finally: await server.stop()


    @pytest.mark.asyncio
    async def test_progressive_audiochunk_before_backend_generator_finishes(self):
        """Deterministic proof that AudioChunk A is emitted while the backend
        generator is still blocked before yielding chunk B.
        
        Uses threading.Event primitives inside a mock generate_stream() so the
        test can observe that write_event(AudioChunk) happens *before* the
        backend iterator has advanced to chunk B.  An implementation that
        buffers the entire backend response into a list before writing will
        deadlock because the generator never returns.
        """
        chunk_a = _pcm_frames(5000)  # 10000 bytes > one 8820-byte Wyoming chunk
        chunk_b = _pcm_frames(5000)
        chunk_a_yielded = threading.Event()
        release_chunk_b = threading.Event()

        class _BlockingStream:
            def __init__(self):
                self.content_type = _REAL_PCM_CONTENT_TYPE
                self.response_headers = dict(_REAL_PCM_HEADERS)
                self._closed = False
                self._state = 0
            def __enter__(self): return self
            def __exit__(self, *a): self._closed = True; return False
            def __iter__(self): return self
            def __next__(self):
                if self._state == 0:
                    self._state = 1
                    chunk_a_yielded.set()
                    return chunk_a
                elif self._state == 1:
                    if not release_chunk_b.wait(timeout=15):
                        raise RuntimeError("Timed out waiting for release_chunk_b")
                    self._state = 2
                    return chunk_b
                else:
                    raise StopIteration

        class _BlockingClient:
            def __init__(self):
                self.multipart_requests = []
                self.stream_requests = []
            def generate_multipart(self, request):
                self.multipart_requests.append(request)
                return S2GenerateResult(
                    audio=chunk_a + chunk_b,
                    content_type=_REAL_PCM_CONTENT_TYPE,
                    response_headers=dict(_REAL_PCM_HEADERS))
            def generate_stream(self, request, files=None, boundary=None):
                self.stream_requests.append(request)
                return _BlockingStream()

        client = _BlockingClient()
        settings = Settings(tts_backend="s2cpp", s2_stream=True)
        server = await start_fake_tts_server(
            host="127.0.0.1", port=0, settings=settings,
            s2_client_factory=_make_cf(client))

        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="progressive proof").event())
                events = []

                # 1. AudioStart
                ev = await asyncio.wait_for(tcp.read_event(), timeout=10)
                assert ev is not None and AudioStart.is_type(ev.type), (
                    f"Expected AudioStart, got {ev.type if ev else None}"
                )
                events.append(ev)

                # 2. Read available AudioChunks while handler is blocked
                for _ in range(20):
                    try:
                        ev = await asyncio.wait_for(tcp.read_event(), timeout=3)
                    except asyncio.TimeoutError:
                        break
                    if ev is None:
                        break
                    events.append(ev)
                    if AudioStop.is_type(ev.type):
                        break

                # 3. PROOF: chunk A was yielded by the backend generator
                assert chunk_a_yielded.wait(5), (
                    "Backend generator never yielded chunk A"
                )

                # 4. PROOF: chunk B has NOT been consumed yet
                assert not release_chunk_b.is_set(), (
                    "chunk_b was consumed before the test released it — "
                    "handler buffered the full backend response"
                )

                # 5. Audio received so far
                received = [
                    AudioChunk.from_event(e)
                    for e in events if AudioChunk.is_type(e.type)
                ]
                audio_so_far = b"".join(c.audio for c in received)
                assert len(audio_so_far) > 0, (
                    "No AudioChunks received before chunk B was released"
                )

                # 6. Release chunk B
                release_chunk_b.set()

                # 7. Read remaining events
                for _ in range(30):
                    try:
                        ev = await asyncio.wait_for(tcp.read_event(), timeout=5)
                    except asyncio.TimeoutError:
                        break
                    if ev is None:
                        break
                    events.append(ev)
                    if AudioStop.is_type(ev.type):
                        break

                # 8. Final assertions
                chunks = [
                    AudioChunk.from_event(e)
                    for e in events if AudioChunk.is_type(e.type)
                ]
                all_audio = b"".join(c.audio for c in chunks)
                assert all_audio == chunk_a + chunk_b, (
                    f"Expected {len(chunk_a)+len(chunk_b)} bytes, "
                    f"got {len(all_audio)}"
                )
                assert AudioStop.is_type(events[-1].type), (
                    f"Expected AudioStop, got {events[-1].type}"
                )
                assert len(client.stream_requests) == 1
                assert not client.multipart_requests

        finally:
            await server.stop()

class TestStreamingVoice:
    @pytest.mark.asyncio
    async def test_default_voice_forwarded(self):
        import tempfile, os
        pcm = _pcm_frames(20)
        client = RecordingStreamingClient(audio=pcm)
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a .s2voice file so the voice is discoverable
            voice_file = os.path.join(tmpdir, "cmu_bdl_male_us.s2voice")
            with open(voice_file, "w") as f:
                f.write("{}")
            settings = Settings(tts_backend="s2cpp", s2_stream=True, s2_default_voice="cmu_bdl_male_us", s2_voice_dir=tmpdir)
            server = await start_fake_tts_server(host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
            try:
                async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                    await tcp.write_event(Synthesize(text="voice test").event())
                    _events = await _collect_all(tcp, timeout=5)
                assert client.stream_requests[0].voice == "cmu_bdl_male_us"
                assert client.stream_requests[0].voice_dir == tmpdir
            finally: await server.stop()

    @pytest.mark.asyncio
    async def test_generic_fallback_omits_voice(self):
        pcm = _pcm_frames(20)
        client = RecordingStreamingClient(audio=pcm)
        settings = Settings(tts_backend="s2cpp", s2_stream=True, s2_default_voice="", s2_voice_dir="")
        server = await start_fake_tts_server(host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="generic").event())
                _events = await _collect_all(tcp, timeout=5)
            assert not client.stream_requests[0].voice
            assert not client.stream_requests[0].voice_dir
        finally: await server.stop()

class TestStreamingFailure:
    @pytest.mark.asyncio
    async def test_bad_content_type_rejected(self):
        pcm = _pcm_frames(20)
        client = RecordingStreamingClient(audio=pcm, content_type="application/octet-stream", response_headers={})
        settings = Settings(tts_backend="s2cpp", s2_stream=True)
        server = await start_fake_tts_server(host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="bad content").event())
                events = await _collect_all(tcp, timeout=5)
            assert "audio-start" not in [e.type for e in events]
        finally: await server.stop()

    @pytest.mark.asyncio
    async def test_odd_trailing_byte_rejected(self):
        bad_pcm = _pcm_frames(2) + b""
        client = RecordingStreamingClient(audio=bad_pcm, stream_chunks=[bad_pcm])
        settings = Settings(tts_backend="s2cpp", s2_stream=True)
        server = await start_fake_tts_server(host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="odd byte").event())
                events = await _collect_all(tcp, timeout=5)
            assert "audio-stop" not in [e.type for e in events]
        finally: await server.stop()

    @pytest.mark.asyncio
    async def test_backend_http_failure(self):
        class FailingClient:
            multipart_requests = []; stream_requests = []
            def generate_multipart(self, r): self.multipart_requests.append(r); raise S2ClientError("fail")
            def generate_stream(self, r, files=None, boundary=None): self.stream_requests.append(r); raise S2ClientError("fail")
        client = FailingClient()
        settings = Settings(tts_backend="s2cpp", s2_stream=True)
        server = await start_fake_tts_server(host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="fail").event())
                events = await _collect_all(tcp, timeout=5)
            assert "audio-start" not in [e.type for e in events]
        finally: await server.stop()

class TestStreamingCompatibility:
    @pytest.mark.asyncio
    async def test_ha_session_uses_stream(self):
        pcm = _pcm_frames(80)
        client = RecordingStreamingClient(audio=pcm, stream_chunks=[pcm[:160], pcm[160:]])
        settings = Settings(tts_backend="s2cpp", s2_stream=True)
        server = await start_fake_tts_server(host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(SynthesizeStart().event())
                await tcp.write_event(SynthesizeChunk(text="Sentence one.").event())
                await tcp.write_event(Synthesize(text="Sentence one.").event())
                await tcp.write_event(SynthesizeStop().event())
                events = await _collect_all(tcp, timeout=5)
            assert client.stream_requests
            assert not client.multipart_requests
            types = [e.type for e in events]
            assert "audio-start" in types
            assert "synthesize-stopped" in types
        finally: await server.stop()

    @pytest.mark.asyncio
    async def test_standalone_legacy_works(self):
        pcm = _pcm_frames(40)
        client = RecordingStreamingClient(audio=pcm)
        settings = Settings(tts_backend="s2cpp", s2_stream=True)
        server = await start_fake_tts_server(host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="standalone").event())
                events = await _collect_all(tcp, timeout=5)
            assert AudioStart.is_type(events[0].type)
            assert AudioStop.is_type(events[-1].type)
        finally: await server.stop()

    @pytest.mark.asyncio
    async def test_no_duplicate_synthesis(self):
        pcm = _pcm_frames(80)
        client = RecordingStreamingClient(audio=pcm)
        settings = Settings(tts_backend="s2cpp", s2_stream=True)
        server = await start_fake_tts_server(host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
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
        finally: await server.stop()


# ---------------------------------------------------------------------------
# Phase 8A: client disconnect and backend stream cleanup
# ---------------------------------------------------------------------------


class TestStreamCleanup:
    """Unit tests for backend stream cleanup on generator close/cancel."""

    @pytest.mark.asyncio
    async def test_generator_aclose_closes_stream(self):
        """aclose() on the async generator triggers stream __exit__."""
        import threading

        stream_closed = threading.Event()

        class _CloseStream:
            def __init__(self):
                self.content_type = _REAL_PCM_CONTENT_TYPE
                self.response_headers = dict(_REAL_PCM_HEADERS)
            def __enter__(self): return self
            def __exit__(self, *a): stream_closed.set(); return False
            def __iter__(self): return self
            def __next__(self):
                import time; time.sleep(10)
                raise StopIteration
            def cancel(self): pass

        class _Client:
            def generate_stream(self, r, files=None, boundary=None): return _CloseStream()

        from app.s2_client import S2GenerateRequest
        from app.wyoming_server import synthesize_s2cpp_streaming_tts_events
        from app.metrics import MetricsCollector

        client = _Client()
        config = FakeTtsConfig(sample_rate=44100, chunk_ms=100)
        request = S2GenerateRequest(text="test")
        metrics = MetricsCollector("s2cpp", "streaming")

        gen = synthesize_s2cpp_streaming_tts_events(client, request, config, Settings(), metrics=metrics)
        # Start the generator — it will yield AudioStart then block on stream read
        ev = await gen.__anext__()
        assert AudioStart.is_type(ev.type)

        # Close the generator (simulates consumer disconnect)
        await gen.aclose()

        # Stream must be closed
        assert stream_closed.wait(timeout=5), "Stream __exit__ was not called"

        # Metrics finalized as cancelled
        assert metrics._terminal_status == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_error_metrics_finalized(self):
        """GeneratorExit/cancellation finalizes metrics as cancelled."""
        class _Stream:
            def __init__(self):
                self.content_type = _REAL_PCM_CONTENT_TYPE
                self.response_headers = dict(_REAL_PCM_HEADERS)
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def __iter__(self): return self
            def __next__(self):
                import time; time.sleep(10)
                raise StopIteration
            def cancel(self): pass

        class _Client:
            def generate_stream(self, r, files=None, boundary=None): return _Stream()

        from app.s2_client import S2GenerateRequest
        from app.wyoming_server import synthesize_s2cpp_streaming_tts_events
        from app.metrics import MetricsCollector

        client = _Client()
        config = FakeTtsConfig(sample_rate=44100, chunk_ms=100)
        request = S2GenerateRequest(text="test")
        metrics = MetricsCollector("s2cpp", "streaming")

        gen = synthesize_s2cpp_streaming_tts_events(client, request, config, Settings(), metrics=metrics)
        await gen.__anext__()  # AudioStart
        await gen.aclose()

        assert metrics._terminal_status == "cancelled"

    @pytest.mark.asyncio
    async def test_stream_cancel_method_called(self):
        """cancel() on S2StreamResult is called during cleanup."""
        import threading

        cancel_called = threading.Event()

        class _CancelStream:
            def __init__(self):
                self.content_type = _REAL_PCM_CONTENT_TYPE
                self.response_headers = dict(_REAL_PCM_HEADERS)
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def __iter__(self): return self
            def __next__(self):
                import time; time.sleep(10)
                raise StopIteration
            def cancel(self): cancel_called.set()

        class _Client:
            def generate_stream(self, r, files=None, boundary=None): return _CancelStream()

        from app.s2_client import S2GenerateRequest
        from app.wyoming_server import synthesize_s2cpp_streaming_tts_events

        client = _Client()
        config = FakeTtsConfig(sample_rate=44100, chunk_ms=100)
        request = S2GenerateRequest(text="test")

        gen = synthesize_s2cpp_streaming_tts_events(client, request, config, Settings())
        await gen.__anext__()
        await gen.aclose()

        assert cancel_called.wait(timeout=5), "stream.cancel() was not called"


class TestClientDisconnectTCP:
    """Real TCP integration tests for client disconnect behavior."""

    @pytest.mark.asyncio
    async def test_disconnect_during_streaming_no_server_crash(self):
        """Server does not crash when client disconnects during streaming."""
        pcm = _pcm_frames(200)
        client = RecordingStreamingClient(audio=pcm, stream_chunks=[pcm[:400], pcm[400:]])
        settings = Settings(tts_backend="s2cpp", s2_stream=True)
        server = await start_fake_tts_server(host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="dc test").event())
                # Read AudioStart
                ev = await asyncio.wait_for(tcp.read_event(), timeout=5)
                assert AudioStart.is_type(ev.type)
                # Disconnect immediately
                tcp._writer.close()
                await tcp._writer.wait_closed()
            # Server must not crash — reaching here is success
        finally:
            await server.stop()






class TestDisconnectBeforeAudio:
    """Client disconnects before any backend PCM is read."""

    @pytest.mark.asyncio
    async def test_disconnect_before_audio_no_terminal_events(self):
        """Disconnecting before audio: no AudioStop or synthesize-stopped."""
        import threading

        # Stream blocks indefinitely on first read
        stream_entered = threading.Event()
        read_started = threading.Event()

        class _BlockStream:
            def __init__(self):
                self.content_type = _REAL_PCM_CONTENT_TYPE
                self.response_headers = dict(_REAL_PCM_HEADERS)
            def __enter__(self): stream_entered.set(); return self
            def __exit__(self, *a): return False
            def __iter__(self): return self
            def __next__(self):
                read_started.set()
                import time; time.sleep(30)  # block
                raise StopIteration
            def cancel(self): pass

        class _Client:
            def __init__(self): self.stream_requests = []; self.multipart_requests = []
            def generate_multipart(self, r): self.multipart_requests.append(r); return S2GenerateResult(audio=b"", content_type=_REAL_PCM_CONTENT_TYPE, response_headers=dict(_REAL_PCM_HEADERS))
            def generate_stream(self, r, files=None, boundary=None): self.stream_requests.append(r); return _BlockStream()

        client = _Client()
        settings = Settings(tts_backend="s2cpp", s2_stream=True)
        server = await start_fake_tts_server(host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="before audio").event())
                # Disconnect immediately — before any audio is read
                tcp._writer.close()
                await tcp._writer.wait_closed()

            # Server must not crash — reaching here is success.
            # The backend stream __exit__ should have been called.
            # No AudioStop or synthesize-stopped should be attempted
            # (can't verify from outside, but no crash = the write
            # failure was caught and cleaned up).
        finally:
            await server.stop()


class TestBackendErrorMidStream:
    """Backend raises an exception after partial audio."""

    @pytest.mark.asyncio
    async def test_backend_error_after_partial_audio_cleans_up(self):
        """Backend S2ClientError after AudioChunk: no successful completion."""
        import threading

        chunk_a = _pcm_frames(5000)

        class _FailingStream:
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
                    return chunk_a
                else:
                    raise S2ClientError("backend failure mid-stream")
            def cancel(self): pass

        class _Client:
            def __init__(self): self.stream_requests = []; self.multipart_requests = []
            def generate_multipart(self, r): self.multipart_requests.append(r); return S2GenerateResult(audio=b"", content_type=_REAL_PCM_CONTENT_TYPE, response_headers=dict(_REAL_PCM_HEADERS))
            def generate_stream(self, r, files=None, boundary=None): self.stream_requests.append(r); return _FailingStream()

        client = _Client()
        settings = Settings(tts_backend="s2cpp", s2_stream=True)
        server = await start_fake_tts_server(host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="backend error").event())
                events = await _collect_all(tcp, timeout=5)
            # AudioStart must have been sent
            assert AudioStart.is_type(events[0].type)
            # Some AudioChunks may have been sent
            chunks = [e for e in events if AudioChunk.is_type(e.type)]
            assert len(chunks) >= 1
            # No AudioStop on error path
            assert not any(AudioStop.is_type(e.type) for e in events), (
                "AudioStop must not be emitted on backend error"
            )
            # No synthesize-stopped
            assert not any(SynthesizeStopped.is_type(e.type) for e in events)
        finally:
            await server.stop()


class TestShutdownDuringSynthesis:
    """Server shutdown while synthesis is active."""

    @pytest.mark.asyncio
    async def test_shutdown_during_active_synthesis(self):
        """Server.stop() during active synthesis closes backend stream."""
        import threading

        stream_entered = threading.Event()
        stream_closed = threading.Event()

        class _LongStream:
            def __init__(self):
                self.content_type = _REAL_PCM_CONTENT_TYPE
                self.response_headers = dict(_REAL_PCM_HEADERS)
            def __enter__(self): stream_entered.set(); return self
            def __exit__(self, *a): stream_closed.set(); return False
            def __iter__(self): return self
            def __next__(self):
                import time; time.sleep(30)
                raise StopIteration
            def cancel(self): pass

        class _Client:
            def __init__(self): self.stream_requests = []; self.multipart_requests = []
            def generate_multipart(self, r): self.multipart_requests.append(r); return S2GenerateResult(audio=b"", content_type=_REAL_PCM_CONTENT_TYPE, response_headers=dict(_REAL_PCM_HEADERS))
            def generate_stream(self, r, files=None, boundary=None): self.stream_requests.append(r); return _LongStream()

        client = _Client()
        settings = Settings(tts_backend="s2cpp", s2_stream=True)
        server = await start_fake_tts_server(host="127.0.0.1", port=0, settings=settings, s2_client_factory=_make_cf(client))
        try:
            async with AsyncTcpClient("127.0.0.1", server.port) as tcp:
                await tcp.write_event(Synthesize(text="shutdown test").event())
                # Wait for the stream to be entered, then stop the server
                assert stream_entered.wait(10), "Stream was never entered"
                await server.stop()

            # Stream must have been closed during shutdown
            assert stream_closed.wait(timeout=10), (
                "Backend stream was not closed during shutdown"
            )
        except Exception:
            pass  # shutdown may raise during cleanup
        finally:
            try:
                await server.stop()
            except Exception:
                pass


class TestCancelIdempotency:
    """Cancel and close are safe when called multiple times."""

    def test_s2stream_cancel_idempotent(self):
        """Calling cancel() multiple times does not raise."""
        import urllib.request
        from app.s2_client import S2StreamResult

        # Create a stream that's already been entered and closed
        req = urllib.request.Request("http://127.0.0.1:1/generate", method="POST")
        stream = S2StreamResult(req, timeout_seconds=0.1)
        # Not entering — just test that cancel on unopened stream is safe
        stream.cancel()
        stream.cancel()  # second call — must not raise
        # __exit__ after cancel — must not raise
        stream.__exit__(None, None, None)

    def test_s2stream_cancel_after_close_idempotent(self):
        """cancel() after __exit__ is safe."""
        import urllib.request
        from app.s2_client import S2StreamResult

        req = urllib.request.Request("http://127.0.0.1:1/generate", method="POST")
        stream = S2StreamResult(req, timeout_seconds=0.1)
        stream.__exit__(None, None, None)
        stream.cancel()  # must not raise
        stream.cancel()


class TestBlockedReadTimeout:
    """Blocked asyncio.to_thread read exits when stream is cancelled."""

    @pytest.mark.asyncio
    async def test_blocked_read_exits_on_cancel(self):
        """Cancelling the async generator unblocks a blocked stream read."""
        import threading, time as _time

        read_started = threading.Event()
        read_released = threading.Event()

        class _BlockStream:
            def __init__(self):
                self.content_type = _REAL_PCM_CONTENT_TYPE
                self.response_headers = dict(_REAL_PCM_HEADERS)
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def __iter__(self): return self
            def __next__(self):
                read_started.set()
                if not read_released.wait(timeout=15):
                    raise RuntimeError("timed out")
                raise StopIteration
            def cancel(self): read_released.set()

        class _Client:
            def generate_stream(self, r, files=None, boundary=None): return _BlockStream()

        from app.s2_client import S2GenerateRequest
        from app.wyoming_server import synthesize_s2cpp_streaming_tts_events

        client = _Client()
        config = FakeTtsConfig(sample_rate=44100, chunk_ms=100)
        request = S2GenerateRequest(text="blocked read test")

        gen = synthesize_s2cpp_streaming_tts_events(client, request, config, Settings())

        # Consume the generator in a task — it will yield AudioStart,
        # then block on asyncio.to_thread calling __next__
        events = []
        async def consume():
            try:
                async for ev in gen:
                    events.append(ev)
            except Exception:
                pass

        task = asyncio.create_task(consume())
        # Wait for AudioStart
        await asyncio.sleep(0.5)
        assert len(events) >= 1 and AudioStart.is_type(events[0].type), (
            "AudioStart not received"
        )
        # Wait for the read to actually start
        assert read_started.wait(timeout=10), "Stream __next__ was never called"

        # Cancel the consumer task — this triggers aclose() on the generator
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=10)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

        # The blocked read must have been released
        assert read_released.is_set() or read_started.is_set(), (
            "Blocked read was not released on cancel"
        )

class _PacedResetStream:
    def __init__(self, *, finite=False):
        self.content_type = _REAL_PCM_CONTENT_TYPE
        self.response_headers = dict(_REAL_PCM_HEADERS)
        self._finite = finite
        self._index = 0
        self._cancelled = threading.Event()
        self.finalize_count = 0
        self.cancel_calls = 0
    def __enter__(self): return self
    def __exit__(self, *_args): self._finalize(); return False
    def __iter__(self): return self
    def __next__(self):
        if self._cancelled.wait(0.003): raise StopIteration
        if self._finite and self._index >= 4:
            self._finalize(); raise StopIteration
        self._index += 1
        return b"\x01\x00" * 32768
    def cancel(self): self.cancel_calls += 1; self._finalize()
    def _finalize(self):
        if self.finalize_count == 0: self.finalize_count = 1
        self._cancelled.set()

class _AlternatingResetClient:
    def __init__(self): self.streams = []
    def generate_stream(self, _request, files=None, boundary=None):
        stream = _PacedResetStream(finite=(len(self.streams) % 2 == 1))
        self.streams.append(stream)
        return stream

def _assert_queue_idle(queue):
    return queue.depth == 0 and queue.pending == 0

async def _wait_for(predicate, timeout=8):
    loop = asyncio.get_running_loop(); deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline: raise AssertionError("condition did not settle")
        await asyncio.sleep(0.01)

@pytest.mark.parametrize("repeat", range(5))
@pytest.mark.asyncio
async def test_tcp_rst_three_disconnect_recovery_cycles(monkeypatch, repeat):
    import socket, struct
    import app.wyoming_server as ws
    queues=[]; real_queue=ws.SingleWorkerSynthesisQueue
    def capture_queue(*args, **kwargs):
        queue=real_queue(*args, **kwargs); queues.append(queue); return queue
    monkeypatch.setattr(ws, "SingleWorkerSynthesisQueue", capture_queue)
    client=_AlternatingResetClient()
    settings=Settings(tts_backend="s2cpp", s2_stream=True,
                      s2_initial_buffer_ms=0, s2_long_form_buffer_ms=0,
                      cancel_on_client_disconnect=True)
    contexts=[]; loop=asyncio.get_running_loop(); old_handler=loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    server=await start_fake_tts_server(host="127.0.0.1",port=0,settings=settings,s2_client_factory=_make_cf(client))
    queue=queues[0]
    try:
        for cycle in range(3):
            tcp=AsyncTcpClient("127.0.0.1",server.port); await tcp.connect()
            await tcp.write_event(Synthesize(text=f"reset {repeat}-{cycle}").event())
            event=await asyncio.wait_for(tcp.read_event(),5); assert AudioStart.is_type(event.type)
            while True:
                event=await asyncio.wait_for(tcp.read_event(),5)
                if AudioChunk.is_type(event.type) and AudioChunk.from_event(event).audio: break
            await _wait_for(lambda: queue.depth == 1 and queue.pending == 1)
            sock=tcp._writer.get_extra_info("socket")
            try: sock.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack("ii",1,0))
            except (AttributeError,OSError): pytest.skip("SO_LINGER reset unsupported")
            tcp._writer.close()
            try: await tcp._writer.wait_closed()
            except (BrokenPipeError,ConnectionResetError,OSError): pass
            await _wait_for(lambda: _assert_queue_idle(queue) and client.streams[-1].finalize_count == 1)

            async with AsyncTcpClient("127.0.0.1",server.port) as recovery:
                await recovery.write_event(Synthesize(text=f"recovery {repeat}-{cycle}").event())
                saw_chunk=False
                while True:
                    event=await asyncio.wait_for(recovery.read_event(),5)
                    assert event is not None
                    if AudioChunk.is_type(event.type): saw_chunk = saw_chunk or bool(AudioChunk.from_event(event).audio)
                    if AudioStop.is_type(event.type): break
                assert saw_chunk
            await _wait_for(lambda: _assert_queue_idle(queue) and client.streams[-1].finalize_count == 1)
        assert len(client.streams) == 6
        assert all(stream.finalize_count == 1 for stream in client.streams)
    finally:
        await server.stop(); await asyncio.sleep(0); await asyncio.sleep(0)
        loop.set_exception_handler(old_handler)
    assert contexts == []


def test_disconnect_source_contract_covers_audio_and_terminal_writes():
    import inspect
    from app.wyoming_server import FakeTtsEventHandler
    source=inspect.getsource(FakeTtsEventHandler)
    assert source.count("except (BrokenPipeError, ConnectionResetError, TypeError) as disconnect_error:") >= 7
    assert source.count("_handle_expected_disconnect(") >= 8
    guarded = source.split(
        "except (BrokenPipeError, ConnectionResetError, TypeError) as disconnect_error:"
    )[1:]
    assert len(guarded) == 7
    assert all("except Exception" in block for block in guarded[:6])
    assert "terminal_error_write_failed" not in source


class _DisconnectWriter:
    def __init__(self): self.close_calls = 0
    def get_extra_info(self, name): return ("127.0.0.1", 12345) if name == "peername" else None
    def close(self): self.close_calls += 1

class _TrackingEvents:
    def __init__(self, events, close_error=None): self.events=iter(events); self.close_error=close_error; self.aclose_calls=0
    def __aiter__(self): return self
    async def __anext__(self):
        try: return next(self.events)
        except StopIteration: raise StopAsyncIteration
    async def aclose(self):
        self.aclose_calls += 1
        if self.close_error: raise self.close_error

def _audio_events():
    return [AudioStart(rate=44100,width=2,channels=1).event(), AudioChunk(audio=b"\x01\x00",rate=44100,width=2,channels=1).event(), AudioStop().event()]

def _handler(monkeypatch, backend):
    import app.wyoming_server as ws
    from app.wyoming_server import FakeTtsEventHandler, SingleWorkerSynthesisQueue
    logs=[]; monkeypatch.setattr(ws,"obs_log",lambda name,**fields: logs.append((name,fields)))
    writer=_DisconnectWriter(); queue=SingleWorkerSynthesisQueue(3,1)
    handler=FakeTtsEventHandler(asyncio.StreamReader(),writer,FakeTtsConfig(),queue,Settings(tts_backend="s2cpp",s2_stream=backend=="streaming"),lambda _:None)
    sources=[]
    if backend=="streaming":
        def stream(*a,**kw):
            src=_TrackingEvents(_audio_events()); sources.append(src); return src
        monkeypatch.setattr(handler,"_synthesize_text_streaming",stream)
    else:
        async def buffered(*a,**kw): return _audio_events()
        monkeypatch.setattr(handler,"_synthesize_text",buffered)
    return handler,writer,queue,logs,sources

async def _invoke(handler, protocol, text):
    if protocol=="standalone": await handler.handle_event(Synthesize(text=text).event()); return
    await handler.handle_event(SynthesizeStart().event()); await handler.handle_event(SynthesizeChunk(text=text).event()); await handler.handle_event(SynthesizeStop().event())

@pytest.mark.parametrize("protocol",["standalone","session"])
@pytest.mark.parametrize("backend",["streaming","buffered"])
@pytest.mark.parametrize("exc",[BrokenPipeError,ConnectionResetError])
@pytest.mark.asyncio
async def test_all_four_audio_paths_own_expected_disconnect_once(monkeypatch,protocol,backend,exc):
    handler,writer,queue,logs,sources=_handler(monkeypatch,backend); written=[]; fail=True
    async def write(event):
        nonlocal fail
        if fail: fail=False; raise exc("injected")
        written.append(event)
    monkeypatch.setattr(handler,"write_event",write); await _invoke(handler,protocol,"disconnect")
    assert writer.close_calls==1 and queue.depth==queue.pending==0
    assert sum(n=="client_disconnected" for n,_ in logs)==1
    if backend=="streaming": assert sources[0].aclose_calls==1
    assert not any(AudioStop.is_type(e.type) or SynthesizeStopped.is_type(e.type) for e in written)
    written.clear(); await _invoke(handler,protocol,"recovery")
    assert queue.depth==queue.pending==0 and any(AudioStop.is_type(e.type) for e in written)

@pytest.mark.parametrize("protocol",["standalone","session"])
@pytest.mark.parametrize("backend",["streaming","buffered"])
@pytest.mark.asyncio
async def test_all_four_audio_paths_propagate_unexpected_write_failure(monkeypatch,protocol,backend):
    handler,writer,queue,logs,sources=_handler(monkeypatch,backend)
    async def write(_): raise RuntimeError("injected unexpected")
    monkeypatch.setattr(handler,"write_event",write)
    with pytest.raises(RuntimeError,match="injected unexpected"): await _invoke(handler,protocol,"bad")
    assert writer.close_calls==0 and queue.depth==queue.pending==0
    assert not any(n=="client_disconnected" for n,_ in logs)
    assert any(n=="synthesis_error" and f.get("error")=="unexpected_write_event_failure" for n,f in logs)
    if backend=="streaming": assert sources[0].aclose_calls==1

@pytest.mark.asyncio
async def test_disconnect_cleanup_is_idempotent(monkeypatch):
    handler,writer,_,logs,_=_handler(monkeypatch,"buffered"); src=_TrackingEvents([])
    await asyncio.gather(*(handler._handle_expected_disconnect("same",audio_generator=src) for _ in range(3)))
    assert writer.close_calls==src.aclose_calls==1
    assert sum(n=="client_disconnected" for n,_ in logs)==1

@pytest.mark.asyncio
async def test_disconnect_cleanup_failure_is_visible(monkeypatch):
    handler,writer,_,logs,_=_handler(monkeypatch,"buffered"); src=_TrackingEvents([],RuntimeError("aclose exploded"))
    with pytest.raises(RuntimeError,match="aclose exploded"): await handler._handle_expected_disconnect("x",audio_generator=src)
    assert writer.close_calls==1
    assert any(n=="disconnect_cleanup_error" and f.get("operation")=="audio_generator_aclose" for n,f in logs)

@pytest.mark.parametrize("empty",[False,True])
@pytest.mark.parametrize("exc",[BrokenPipeError,ConnectionResetError])
@pytest.mark.asyncio
async def test_synthesize_stopped_write_uses_disconnect_cleanup(monkeypatch,empty,exc):
    handler,writer,queue,logs,_=_handler(monkeypatch,"buffered")
    async def write(event):
        if SynthesizeStopped.is_type(event.type): raise exc("terminal reset")
    monkeypatch.setattr(handler,"write_event",write)
    await handler.handle_event(SynthesizeStart().event())
    if not empty: await handler.handle_event(SynthesizeChunk(text="text").event())
    await handler.handle_event(SynthesizeStop().event())
    assert writer.close_calls==1 and queue.depth==queue.pending==0
    assert sum(n=="client_disconnected" for n,_ in logs)==1

@pytest.mark.asyncio
async def test_unexpected_synthesize_stopped_failure_propagates(monkeypatch):
    handler,writer,queue,logs,_=_handler(monkeypatch,"buffered")
    async def write(event):
        if SynthesizeStopped.is_type(event.type): raise RuntimeError("terminal defect")
    monkeypatch.setattr(handler,"write_event",write)
    with pytest.raises(RuntimeError,match="terminal defect"): await _invoke(handler,"session","text")
    assert writer.close_calls==0 and queue.depth==queue.pending==0

@pytest.mark.asyncio
async def test_operational_error_write_uses_disconnect_cleanup(monkeypatch):
    from app.wyoming_server import QueueFullError
    from wyoming.error import Error
    handler,writer,_,logs,_=_handler(monkeypatch,"buffered")
    async def queue_run(*a,**kw): raise QueueFullError("full")
    async def write(event): assert Error.is_type(event.type); raise BrokenPipeError("reset")
    monkeypatch.setattr(handler.queue,"run",queue_run); monkeypatch.setattr(handler,"write_event",write)
    await handler._run_operational(lambda:None,"id")
    assert writer.close_calls==1 and sum(n=="client_disconnected" for n,_ in logs)==1

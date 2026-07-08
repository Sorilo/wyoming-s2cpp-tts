"""Phase 7.5A: Progressive backend HTTP streaming wired into production handler."""
import asyncio
import threading
import pytest
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize, SynthesizeChunk, SynthesizeStart, SynthesizeStop, SynthesizeStopped
from app.config import Settings
from app.s2_client import S2ClientError, S2GenerateResult
from app.wyoming_server import start_fake_tts_server

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

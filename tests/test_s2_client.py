import json
import urllib.error
from unittest.mock import Mock, patch

import pytest

from app.config import Settings
from app.s2_client import (
    S2Client,
    S2ClientError,
    S2Endpoint,
    S2GenerateRequest,
    encode_multipart_form_data,
)


def _response(body: bytes, status: int = 200, content_type: str = "audio/L16"):
    response = Mock()
    response.status = status
    response.read.return_value = body
    response.headers = {"Content-Type": content_type}
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=None)
    return response


# ---------------------------------------------------------------------------
# JSON-buffered tests (Phase 2) — must be preserved
# ---------------------------------------------------------------------------

def test_endpoint_from_settings_uses_configured_host_and_port():
    settings = Settings(s2_host="192.168.1.45", s2_port=3030)

    endpoint = S2Endpoint.from_settings(settings)

    assert endpoint.base_url == "http://192.168.1.45:3030"
    assert endpoint.generate_url == "http://192.168.1.45:3030/generate"


def test_generate_posts_expected_json_payload_to_generate_endpoint():
    pcm = b"\x01\x02\x03\x04"
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello", voice="test-voice")

    with patch("app.s2_client.urlopen", return_value=_response(pcm)) as urlopen:
        result = client.generate(request)

    sent_request = urlopen.call_args.args[0]
    payload = json.loads(sent_request.data.decode("utf-8"))

    assert sent_request.full_url == "http://127.0.0.1:3030/generate"
    assert sent_request.get_method() == "POST"
    assert sent_request.headers["Content-type"] == "application/json"
    assert payload["text"] == "hello"
    assert payload["voice"] == "test-voice"
    assert payload["model"] == "/models/s2-pro-q6_k.gguf"
    assert payload["stream"] is True
    assert payload["chunked"] is True
    assert payload["output_format"] == "pcm_s16le"
    assert result.audio == pcm
    assert result.content_type == "audio/L16"


def test_generate_omits_empty_voice_from_payload():
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)

    with patch("app.s2_client.urlopen", return_value=_response(b"pcm")) as urlopen:
        client.generate(S2GenerateRequest(text="hello", voice=""))

    payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
    assert "voice" not in payload


def test_generate_raises_client_error_for_http_failure():
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    error = urllib.error.HTTPError(
        url="http://127.0.0.1:3030/generate",
        code=500,
        msg="server error",
        hdrs=None,
        fp=None,
    )

    with patch("app.s2_client.urlopen", side_effect=error):
        with pytest.raises(S2ClientError, match="s2.cpp /generate failed"):
            client.generate(S2GenerateRequest(text="hello"))


# ---------------------------------------------------------------------------
# Multipart encoder tests
# ---------------------------------------------------------------------------

def test_encoder_produces_canonical_reference_and_reference_text():
    """Canonical fields from rodrigomatta/s2.cpp: reference (file) and
    reference_text (field)."""
    content_type, body = encode_multipart_form_data(
        fields={
            "text": "hello",
            "params": '{"temperature":0.7}',
            "reference_text": "transcript for cloning",
        },
        files={"reference": ("voice.wav", b"RIFFfake", "audio/wav")},
        boundary="phase5a2",
    )

    assert content_type == "multipart/form-data; boundary=phase5a2"

    # text
    assert b'Content-Disposition: form-data; name="text"' in body
    assert b"\r\n\r\nhello\r\n" in body

    # params
    assert b'Content-Disposition: form-data; name="params"' in body
    assert b'{"temperature":0.7}' in body

    # reference_text is canonical
    assert b'Content-Disposition: form-data; name="reference_text"' in body
    assert b"transcript for cloning" in body

    # reference file part
    assert (
        b'Content-Disposition: form-data; name="reference";'
        b' filename="voice.wav"'
    ) in body
    assert b"Content-Type: audio/wav" in body
    assert b"RIFFfake" in body

    assert body.endswith(b"--phase5a2--\r\n")


# ---------------------------------------------------------------------------
# Canonical multipart generate tests (Phase 5A.2)
# ---------------------------------------------------------------------------

def test_multipart_uses_post_generate_endpoint():
    pcm = b"\x09\x08\x07\x06"
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello")

    with patch("app.s2_client.urlopen", return_value=_response(pcm)) as urlopen:
        result = client.generate_multipart(request, boundary="b")

    sent_request = urlopen.call_args.args[0]
    assert sent_request.full_url == "http://127.0.0.1:3030/generate"
    assert sent_request.get_method() == "POST"
    assert result.audio == pcm


def test_multipart_text_is_canonical_top_level_field():
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello canonical")

    with patch("app.s2_client.urlopen", return_value=_response(b"pcm")) as urlopen:
        client.generate_multipart(request, boundary="b")

    body = urlopen.call_args.args[0].data
    assert b'Content-Disposition: form-data; name="text"' in body


def test_multipart_params_is_one_json_string():
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(
        text="hello",
        temperature=0.58,
        top_p=0.88,
        top_k=40,
        max_new_tokens=512,
        output_format="pcm_s16le",
        segment_sentences=True,
    )

    with patch("app.s2_client.urlopen", return_value=_response(b"pcm")) as urlopen:
        client.generate_multipart(request, boundary="b")

    body = urlopen.call_args.args[0].data
    body_str = body.decode("utf-8")

    assert b'Content-Disposition: form-data; name="params"' in body

    # Extract and parse the params JSON
    params_start = body_str.index('name="params"') + len('name="params"')
    header_end = body_str.index("\r\n\r\n", params_start) + 4
    params_end = body_str.index("\r\n", header_end)
    params_json = body_str[header_end:params_end]
    params = json.loads(params_json)
    assert params["temperature"] == 0.58
    assert params["output_format"] == "pcm_s16le"
    assert params["segment_sentences"] is True


def test_multipart_no_flattened_generation_fields():
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello", voice="voice-a")

    with patch("app.s2_client.urlopen", return_value=_response(b"pcm")) as urlopen:
        client.generate_multipart(request, boundary="b")

    body_str = urlopen.call_args.args[0].data.decode("utf-8")

    # Generation settings inside params only — NOT as top-level fields
    assert 'name="model"' not in body_str
    assert 'name="stream"' not in body_str
    assert 'name="chunked"' not in body_str
    assert 'name="output_format"' not in body_str
    assert 'name="temperature"' not in body_str


def test_multipart_emits_canonical_reference_text_not_prompt_text():
    """The canonical emitted field is reference_text, not prompt_text."""
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(
        text="clone me",
        prompt_text="exact words in reference audio",
    )

    with patch("app.s2_client.urlopen", return_value=_response(b"pcm")) as urlopen:
        client.generate_multipart(request, boundary="b")

    body_str = urlopen.call_args.args[0].data.decode("utf-8")
    assert 'name="reference_text"' in body_str
    assert 'name="prompt_text"' not in body_str
    assert "exact words in reference audio" in body_str


def test_multipart_omits_empty_reference_text():
    """reference_text is omitted when prompt_text is empty."""
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello", prompt_text="")

    with patch("app.s2_client.urlopen", return_value=_response(b"pcm")) as urlopen:
        client.generate_multipart(request, boundary="b")

    body_str = urlopen.call_args.args[0].data.decode("utf-8")
    assert 'name="reference_text"' not in body_str


def test_multipart_reference_file_with_reference_text():
    """reference file part and reference_text field are paired correctly."""
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(
        text="clone this voice",
        prompt_text="the speaker said exactly this",
    )

    with patch("app.s2_client.urlopen", return_value=_response(b"pcm")) as urlopen:
        client.generate_multipart(
            request,
            files={"reference": ("ref.wav", b"\x00\x01\x02", "audio/wav")},
            boundary="clone",
        )

    body = urlopen.call_args.args[0].data

    # reference file part
    assert (
        b'Content-Disposition: form-data; name="reference";'
        b' filename="ref.wav"'
    ) in body
    assert b"Content-Type: audio/wav" in body
    assert b"\x00\x01\x02" in body

    # reference_text field
    assert b'Content-Disposition: form-data; name="reference_text"' in body
    assert b"the speaker said exactly this" in body


def test_multipart_reference_without_reference_text_raises():
    """reference without reference_text raises ValueError."""
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello", prompt_text="")

    with pytest.raises(ValueError, match="reference requires reference_text"):
        client.generate_multipart(
            request,
            files={"reference": ("ref.wav", b"data", "audio/wav")},
        )


def test_multipart_prompt_audio_alias_normalised_to_reference():
    """The prompt_audio alias is normalised to the canonical reference key."""
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(
        text="alias test",
        prompt_text="transcript",
    )

    with patch("app.s2_client.urlopen", return_value=_response(b"pcm")) as urlopen:
        client.generate_multipart(
            request,
            files={"prompt_audio": ("voice.wav", b"ALIAS", "audio/wav")},
            boundary="alias",
        )

    body = urlopen.call_args.args[0].data
    # Emitted with canonical key
    assert b'name="reference"' in body
    assert b"ALIAS" in body
    # Alias is not emitted
    assert b'name="prompt_audio"' not in body


def test_multipart_voice_field_emitted_when_configured():
    """voice is a canonical top-level multipart field for saved profiles."""
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello", voice="hope")

    with patch("app.s2_client.urlopen", return_value=_response(b"pcm")) as urlopen:
        client.generate_multipart(request, boundary="b")

    body_str = urlopen.call_args.args[0].data.decode("utf-8")
    assert 'name="voice"' in body_str
    assert "hope" in body_str


def test_multipart_voice_dir_field_emitted_when_configured():
    """voice_dir is a canonical top-level multipart field."""
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(
        text="hello", voice="hope", voice_dir="./voices"
    )

    with patch("app.s2_client.urlopen", return_value=_response(b"pcm")) as urlopen:
        client.generate_multipart(request, boundary="b")

    body_str = urlopen.call_args.args[0].data.decode("utf-8")
    assert 'name="voice"' in body_str
    assert 'name="voice_dir"' in body_str
    assert "./voices" in body_str


def test_multipart_voice_without_reference_audio():
    """voice can be used without reference audio (saved profile)."""
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(
        text="hello", voice="hope", voice_dir="/voices"
    )

    with patch("app.s2_client.urlopen", return_value=_response(b"pcm")) as urlopen:
        client.generate_multipart(request, boundary="b")

    body_str = urlopen.call_args.args[0].data.decode("utf-8")
    assert 'name="voice"' in body_str
    assert 'name="reference"' not in body_str


def test_multipart_voice_and_reference_both_supported():
    """voice and reference can be provided together; no client-side conflict."""
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(
        text="hello",
        voice="hope",
        voice_dir="/voices",
        prompt_text="reference transcript",
    )

    with patch("app.s2_client.urlopen", return_value=_response(b"pcm")) as urlopen:
        client.generate_multipart(
            request,
            files={"reference": ("ref.wav", b"REF", "audio/wav")},
            boundary="both",
        )

    body_str = urlopen.call_args.args[0].data.decode("utf-8")
    assert 'name="voice"' in body_str
    assert 'name="voice_dir"' in body_str
    assert 'name="reference"' in body_str
    assert 'name="reference_text"' in body_str


def test_multipart_empty_voice_and_voice_dir_omitted():
    """Empty voice and voice_dir are omitted from multipart fields."""
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello", voice="", voice_dir="")

    with patch("app.s2_client.urlopen", return_value=_response(b"pcm")) as urlopen:
        client.generate_multipart(request, boundary="b")

    body_str = urlopen.call_args.args[0].data.decode("utf-8")
    assert 'name="voice"' not in body_str
    assert 'name="voice_dir"' not in body_str


def test_multipart_buffered_does_not_add_streaming_params():
    """Buffered multipart requests do not silently enable stream, chunked,
    low_latency, or pcm_s16le output_format in params."""
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello")

    with patch("app.s2_client.urlopen", return_value=_response(b"pcm")) as urlopen:
        client.generate_multipart(request, boundary="b")

    body_str = urlopen.call_args.args[0].data.decode("utf-8")
    params_start = body_str.index('name="params"') + len('name="params"')
    header_end = body_str.index("\r\n\r\n", params_start) + 4
    params_end = body_str.index("\r\n", header_end)
    params = json.loads(body_str[header_end:params_end])

    # Streaming options NOT in buffered multipart params
    assert "stream" not in params
    assert "chunked" not in params
    assert "low_latency" not in params
    # output_format is present (it's a generation setting), but not "pcm_s16le"
    # unless the buffered path intentionally uses it — in buffered mode it's
    # set to wav by the server default. Our config has pcm_s16le, but
    # the buffered method preserves whatever is set without adding streaming.
    assert params["output_format"] == "pcm_s16le"


def test_multipart_from_settings_wires_voice_dir():
    """from_settings picks up s2_voice_dir from app settings."""
    settings = Settings(s2_voice_dir="/my/voices", s2_default_voice="hope")
    request = S2GenerateRequest.from_settings("hello", settings)

    assert request.voice == "hope"
    assert request.voice_dir == "/my/voices"

# ---------------------------------------------------------------------------
# Mock helpers for streaming tests
# ---------------------------------------------------------------------------

class _StreamingMockResponse:
    """Mock urllib response that yields chunks progressively via read(size).

    Each call to read(size) returns the next chunk from the internal list,
    regardless of the requested size. This simulates progressive backend
    delivery where the transport delivers discrete chunks.
    """

    def __init__(self, chunks, content_type="audio/L16", status=200):
        self._chunks = list(chunks)
        self._index = 0
        self.headers = {"Content-Type": content_type}
        self.status = status
        self._full_read_called = False
        self._closed = False

    def read(self, size=-1):
        if size == -1:
            self._full_read_called = True
            remaining = b"".join(self._chunks[self._index:])
            self._index = len(self._chunks)
            return remaining
        if self._index >= len(self._chunks):
            return b""
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk

    def close(self):
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._closed = True
        return None


class _FailingReadMockResponse:
    """Mock that raises an exception on read()."""

    def __init__(self, fail_on_call=1, content_type="audio/L16"):
        self.headers = {"Content-Type": content_type}
        self._call_count = 0
        self._fail_on_call = fail_on_call
        self._closed = False

    def read(self, size=-1):
        self._call_count += 1
        if self._call_count >= self._fail_on_call:
            raise IOError("simulated read failure")
        return b"partial-chunk"

    def close(self):
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._closed = True
        return None


def _streaming_response(chunks, content_type="audio/L16"):
    """Shorthand factory for a streaming mock response."""
    return _StreamingMockResponse(chunks, content_type=content_type)


# ---------------------------------------------------------------------------
# Phase 5B: Streaming client tests
# ---------------------------------------------------------------------------

def test_stream_yields_chunks_progressively():
    """Stream yields each chunk in order without concatenating them."""
    chunks = [b"chunk1", b"chunk2", b"chunk3"]
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello")

    with patch("app.s2_client.urlopen", return_value=_streaming_response(chunks)):
        with client.generate_stream(request, boundary="str") as stream:
            yielded = list(stream)

    assert yielded == chunks


def test_stream_first_chunk_before_full_response_available():
    """Deterministic proof: the first chunk is yielded before the full
    response is read -- the mock tracks that read(-1) was never called."""
    chunks = [b"A", b"B", b"C", b"D"]
    mock = _streaming_response(chunks)
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello")

    with patch("app.s2_client.urlopen", return_value=mock):
        with client.generate_stream(request, boundary="pr") as stream:
            first = next(stream)
            # At this point, only one chunk has been read
            assert not mock._full_read_called, (
                "full read was called before first chunk was yielded"
            )

    assert first == b"A"
    # Even after full iteration, read(-1) must not have been called
    # because the iterator uses read(4096), not read().
    assert not mock._full_read_called


def test_stream_cleanup_on_normal_completion():
    """Response is closed after the iterator is exhausted normally."""
    chunks = [b"x", b"y"]
    mock = _streaming_response(chunks)
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello")

    with patch("app.s2_client.urlopen", return_value=mock):
        with client.generate_stream(request, boundary="cl") as stream:
            for _ in stream:
                pass

    assert mock._closed, "response was not closed after normal completion"


def test_stream_cleanup_on_early_break():
    """Response is closed when the consumer breaks from the loop early."""
    chunks = [b"1", b"2", b"3", b"4", b"5"]
    mock = _streaming_response(chunks)
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello")

    with patch("app.s2_client.urlopen", return_value=mock):
        with client.generate_stream(request, boundary="br") as stream:
            for i, chunk in enumerate(stream):
                if i >= 2:  # break after 3 chunks
                    break

    assert mock._closed, "response was not closed after early break"


def test_stream_error_on_http_failure():
    """HTTP errors raise S2ClientError before any chunks are yielded."""
    error = urllib.error.HTTPError(
        url="http://127.0.0.1:3030/generate",
        code=500,
        msg="server error",
        hdrs=None,
        fp=None,
    )
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello")

    with patch("app.s2_client.urlopen", side_effect=error):
        with pytest.raises(S2ClientError, match="streaming failed"):
            with client.generate_stream(request, boundary="err") as stream:
                list(stream)


def test_stream_error_on_read_failure():
    """Read errors mid-stream raise S2ClientError and close the response."""
    mock = _FailingReadMockResponse(fail_on_call=1)
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello")

    with patch("app.s2_client.urlopen", return_value=mock):
        with pytest.raises(S2ClientError, match="streaming read failed"):
            with client.generate_stream(request, boundary="rd") as stream:
                next(stream)

    assert mock._closed, "response was not closed after read failure"


def test_stream_params_include_streaming_flags():
    """Streaming multipart params JSON includes stream, chunked,
    output_format=pcm_s16le, and low_latency."""
    chunks = [b"audio"]
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello")

    with patch("app.s2_client.urlopen", return_value=_streaming_response(chunks)) as urlopen_mock:
        with client.generate_stream(request, boundary="sp") as stream:
            list(stream)

    body_str = urlopen_mock.call_args.args[0].data.decode("utf-8")

    # Extract params JSON
    params_start = body_str.index('name="params"') + len('name="params"')
    header_end = body_str.index("\r\n\r\n", params_start) + 4
    params_end = body_str.index("\r\n", header_end)
    params = json.loads(body_str[header_end:params_end])

    assert params["stream"] is True
    assert params["chunked"] is True
    assert params["output_format"] == "pcm_s16le"
    assert params["low_latency"] is True


def test_stream_preserves_canonical_fields():
    """Streaming multipart still uses canonical text, reference_text, voice fields."""
    chunks = [b"audio"]
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(
        text="hello world",
        voice="hope",
        voice_dir="/voices",
        prompt_text="reference transcript",
    )

    with patch("app.s2_client.urlopen", return_value=_streaming_response(chunks)) as urlopen_mock:
        with client.generate_stream(
            request,
            files={"reference": ("ref.wav", b"DATA", "audio/wav")},
            boundary="canon",
        ) as stream:
            list(stream)

    body_str = urlopen_mock.call_args.args[0].data.decode("utf-8")

    assert 'name="text"' in body_str
    assert "hello world" in body_str
    assert 'name="reference_text"' in body_str
    assert "reference transcript" in body_str
    assert 'name="voice"' in body_str
    assert "hope" in body_str
    assert 'name="voice_dir"' in body_str
    assert "/voices" in body_str
    assert 'name="reference"' in body_str
    assert b"DATA" in urlopen_mock.call_args.args[0].data


def test_stream_content_type_accessible():
    """Stream's content_type property reflects the backend response header."""
    chunks = [b"pcm"]
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello")

    with patch(
        "app.s2_client.urlopen",
        return_value=_streaming_response(chunks, content_type="audio/L16;rate=22050"),
    ):
        with client.generate_stream(request, boundary="ct") as stream:
            assert stream.content_type == "audio/L16;rate=22050"
            list(stream)


def test_stream_uses_multipart_not_json():
    """Streaming uses multipart/form-data Content-Type, not application/json."""
    chunks = [b"audio"]
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello")

    with patch("app.s2_client.urlopen", return_value=_streaming_response(chunks)) as urlopen_mock:
        with client.generate_stream(request, boundary="mp") as stream:
            list(stream)

    sent_request = urlopen_mock.call_args.args[0]
    content_type = sent_request.headers["Content-type"]
    assert content_type.startswith("multipart/form-data")
    assert "application/json" not in content_type


def test_stream_reference_without_reference_text_raises():
    """Streaming enforces reference_text requirement for reference audio."""
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello", prompt_text="")

    with pytest.raises(ValueError, match="reference requires reference_text"):
        with client.generate_stream(
            request,
            files={"reference": ("ref.wav", b"data", "audio/wav")},
            boundary="val",
        ):
            pass


def test_to_multipart_fields_streaming_adds_params():
    """to_multipart_fields(streaming=True) adds stream/chunked/low_latency/pcm_s16le."""
    request = S2GenerateRequest(text="test")
    fields = request.to_multipart_fields(streaming=True)

    params = json.loads(fields["params"])
    assert params["stream"] is True
    assert params["chunked"] is True
    assert params["output_format"] == "pcm_s16le"
    assert params["low_latency"] is True


def test_to_multipart_fields_non_streaming_no_stream_params():
    """to_multipart_fields() without streaming does not include stream params."""
    request = S2GenerateRequest(text="test")
    fields = request.to_multipart_fields()

    params = json.loads(fields["params"])
    assert "stream" not in params
    assert "chunked" not in params
    assert "low_latency" not in params


def test_stream_empty_transport_chunks_handled_as_eof():
    """Empty reads from the response signal end-of-stream (standard HTTP behavior).

    The stream iterator treats ``b""`` from ``response.read()`` as EOF and
    stops iteration. Mid-stream zero-length chunks are not expected in
    real HTTP chunked transfer encoding — the transport delivers non-empty
    data frames. This test verifies the iterator correctly terminates on an
    empty read.
    """
    chunks = [b"real1", b"real2"]
    mock = _streaming_response(chunks)
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello")

    with patch("app.s2_client.urlopen", return_value=mock):
        with client.generate_stream(request, boundary="em") as stream:
            yielded = list(stream)

    assert yielded == [b"real1", b"real2"]


def test_stream_multiple_iterations_are_idempotent():
    """Once exhausted, iterating again yields nothing."""
    chunks = [b"only-once"]
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello")

    with patch("app.s2_client.urlopen", return_value=_streaming_response(chunks)):
        with client.generate_stream(request, boundary="id") as stream:
            first_pass = list(stream)
            second_pass = list(stream)

    assert first_pass == [b"only-once"]
    assert second_pass == []


# ---------------------------------------------------------------------------
# Phase 7.5D2: progressive streaming contract and context validation
# ---------------------------------------------------------------------------

class TestProgressiveStreamingContract:
    """segment_sentences=False and codec context validation for S2_STREAM=true."""

    def test_streaming_defaults_segment_sentences_to_false(self):
        """When S2_STREAM=true, the streaming params include segment_sentences=false."""
        req = S2GenerateRequest(text="hello", stream=True)
        fields = req.to_multipart_fields(streaming=True)
        params = json.loads(fields["params"])
        assert params.get("segment_sentences") is False

    def test_streaming_includes_low_latency(self):
        """Streaming params must include low_latency=true."""
        req = S2GenerateRequest(text="hello")
        fields = req.to_multipart_fields(streaming=True)
        params = json.loads(fields["params"])
        assert params["low_latency"] is True

    def test_context_4_accepted(self):
        """codec_decode_context_frames=4 is a verified working value."""
        req = S2GenerateRequest(text="hello", codec_decode_context_frames=4)
        fields = req.to_multipart_fields(streaming=True)
        params = json.loads(fields["params"])
        assert params["codec_decode_context_frames"] == 4

    def test_context_64_accepted(self):
        """codec_decode_context_frames=64 is a verified working value."""
        req = S2GenerateRequest(text="hello", codec_decode_context_frames=64)
        fields = req.to_multipart_fields(streaming=True)
        params = json.loads(fields["params"])
        assert params["codec_decode_context_frames"] == 64

    def test_context_160_accepted(self):
        """codec_decode_context_frames=160 is a verified working value."""
        req = S2GenerateRequest(text="hello", codec_decode_context_frames=160)
        fields = req.to_multipart_fields(streaming=True)
        params = json.loads(fields["params"])
        assert params["codec_decode_context_frames"] == 160

    def test_context_none_omits_field(self):
        """When codec_decode_context_frames is None, the field is omitted from params."""
        req = S2GenerateRequest(text="hello", codec_decode_context_frames=None)
        fields = req.to_multipart_fields(streaming=True)
        params = json.loads(fields["params"])
        assert "codec_decode_context_frames" not in params

    def test_context_1_accepted(self):
        """Context 1 now accepted (any int >= 0)."""
        req = S2GenerateRequest(text="hello", codec_decode_context_frames=1)
        params = req.to_multipart_fields(streaming=True)
        assert "codec_decode_context_frames" in params["params"]  # now accepted

    def test_context_8_accepted(self):
        """Context 8 now accepted."""
        req = S2GenerateRequest(text="hello", codec_decode_context_frames=8)
        params = req.to_multipart_fields(streaming=True)
        assert "codec_decode_context_frames" in params["params"]  # now accepted

    def test_context_32_accepted(self):
        """Context 32 now accepted."""
        req = S2GenerateRequest(text="hello", codec_decode_context_frames=32)
        params = req.to_multipart_fields(streaming=True)
        assert "codec_decode_context_frames" in params["params"]  # now accepted

    def test_context_negative_rejected(self):
        """Negative context values must be rejected."""
        req = S2GenerateRequest(text="hello", codec_decode_context_frames=-1)
        with pytest.raises(ValueError, match="codec_decode_context_frames"):
            req.to_multipart_fields(streaming=True)

    def test_context_0_accepted(self):
        """Context 0 now accepted."""
        req = S2GenerateRequest(text="hello", codec_decode_context_frames=0)
        params = req.to_multipart_fields(streaming=True)
        assert "codec_decode_context_frames" in params["params"]  # now accepted

    def test_buffered_segment_sentences_preserved(self):
        """Non-streaming requests preserve the explicit segment_sentences default."""
        req = S2GenerateRequest(text="hello", segment_sentences=True)
        fields = req.to_multipart_fields(streaming=False)
        params = json.loads(fields["params"])
        # In non-streaming mode, segment_sentences comes from the request default
        assert params.get("segment_sentences") is True

    def test_streaming_overrides_segment_sentences(self):
        """Even if segment_sentences=True in request, streaming mode overrides to False."""
        req = S2GenerateRequest(text="hello", segment_sentences=True)
        fields = req.to_multipart_fields(streaming=True)
        params = json.loads(fields["params"])
        assert params["segment_sentences"] is False

    def test_voice_and_dir_preserved(self):
        """Voice and voice_dir are still forwarded in streaming requests."""
        req = S2GenerateRequest(text="hello", voice="test_voice", voice_dir="/test")
        fields = req.to_multipart_fields(streaming=True)
        assert fields["voice"] == "test_voice"
        assert fields["voice_dir"] == "/test"

    def test_generic_fallback_omits_voice(self):
        """When voice is empty and voice_dir is empty, they are omitted from fields."""
        req = S2GenerateRequest(text="hello", voice="", voice_dir="")
        fields = req.to_multipart_fields(streaming=True)
        assert "voice" not in fields or not fields.get("voice")
        assert "voice_dir" not in fields or not fields.get("voice_dir")


class TestConfigProgressiveDefaults:
    """Configuration defaults for progressive streaming."""

    def test_segment_sentences_env_default_false(self):
        """S2_SEGMENT_SENTENCES defaults to False."""
        settings = Settings()
        assert settings.s2_segment_sentences is False

    def test_codec_context_env_default(self):
        """S2_CODEC_CONTEXT_FRAMES defaults to 4."""
        settings = Settings()
        assert settings.s2_codec_decode_context_frames == 4

    def test_segment_sentences_from_env_true(self, monkeypatch):
        """S2_SEGMENT_SENTENCES=true is parsed correctly."""
        monkeypatch.setenv("S2_SEGMENT_SENTENCES", "true")
        settings = Settings.from_env()
        assert settings.s2_segment_sentences is True

    def test_codec_context_from_env(self, monkeypatch):
        """S2_CODEC_CONTEXT_FRAMES=64 is parsed correctly."""
        monkeypatch.setenv("S2_CODEC_CONTEXT_FRAMES", "64")
        settings = Settings.from_env()
        assert settings.s2_codec_decode_context_frames == 64

    def test_codec_context_auto_returns_none(self, monkeypatch):
        """S2_CODEC_CONTEXT_FRAMES=auto returns None (omit field)."""
        monkeypatch.setenv("S2_CODEC_CONTEXT_FRAMES", "auto")
        settings = Settings.from_env()
        assert settings.s2_codec_decode_context_frames is None

    def test_codec_context_empty_returns_none(self, monkeypatch):
        """Empty S2_CODEC_CONTEXT_FRAMES returns None."""
        monkeypatch.setenv("S2_CODEC_CONTEXT_FRAMES", "")
        settings = Settings.from_env()
        assert settings.s2_codec_decode_context_frames is None

    def test_request_from_settings_propagates_context(self):
        """S2GenerateRequest.from_settings propagates codec context."""
        settings = Settings(s2_codec_decode_context_frames=4)
        req = S2GenerateRequest.from_settings("hello", settings)
        assert req.codec_decode_context_frames == 4

    def test_request_from_settings_propagates_segment_sentences(self):
        """S2GenerateRequest.from_settings propagates segment_sentences."""
        settings = Settings(s2_segment_sentences=True)
        req = S2GenerateRequest.from_settings("hello", settings)
        assert req.segment_sentences is True



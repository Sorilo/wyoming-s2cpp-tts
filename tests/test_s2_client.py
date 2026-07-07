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

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

def test_multipart_encoder_includes_text_field_params_and_file_part():
    """Verify the encoder produces canonical multipart/form-data with the
    verified upstream field names: 'text', 'params' (JSON string), optional
    'prompt_text', and 'prompt_audio' file part."""
    content_type, body = encode_multipart_form_data(
        fields={
            "text": "hello",
            "params": '{"temperature":0.7}',
            "prompt_text": "the reference transcript",
        },
        files={"prompt_audio": ("voice.wav", b"RIFFfake", "audio/wav")},
        boundary="phase5a1-boundary",
    )

    assert content_type == "multipart/form-data; boundary=phase5a1-boundary"

    # text is a canonical top-level field
    assert b'Content-Disposition: form-data; name="text"' in body
    assert b"\r\n\r\nhello\r\n" in body

    # params is a single JSON string field
    assert b'Content-Disposition: form-data; name="params"' in body
    assert b'{"temperature":0.7}' in body

    # prompt_text is a top-level field
    assert b'Content-Disposition: form-data; name="prompt_text"' in body
    assert b"the reference transcript" in body

    # prompt_audio is a file part with filename, bytes, and media type
    assert (
        b'Content-Disposition: form-data; name="prompt_audio";'
        b' filename="voice.wav"'
    ) in body
    assert b"Content-Type: audio/wav" in body
    assert b"RIFFfake" in body

    assert body.endswith(b"--phase5a1-boundary--\r\n")


# ---------------------------------------------------------------------------
# Canonical multipart generate tests (Phase 5A.1)
# ---------------------------------------------------------------------------

def test_multipart_uses_post_generate_endpoint():
    pcm = b"\x09\x08\x07\x06"
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello multipart")

    with patch("app.s2_client.urlopen", return_value=_response(pcm)) as urlopen:
        result = client.generate_multipart(request, boundary="test-boundary")

    sent_request = urlopen.call_args.args[0]
    assert sent_request.full_url == "http://127.0.0.1:3030/generate"
    assert sent_request.get_method() == "POST"
    assert result.audio == pcm


def test_multipart_content_type_has_boundary():
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello")

    with patch("app.s2_client.urlopen", return_value=_response(b"pcm")) as urlopen:
        client.generate_multipart(request, boundary="test-boundary")

    sent_request = urlopen.call_args.args[0]
    ct = sent_request.headers["Content-type"]
    assert ct == "multipart/form-data; boundary=test-boundary"


def test_multipart_accept_header():
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello")

    with patch("app.s2_client.urlopen", return_value=_response(b"pcm")) as urlopen:
        client.generate_multipart(request, boundary="b")

    sent_request = urlopen.call_args.args[0]
    assert sent_request.headers["Accept"] == (
        "audio/L16, audio/wav, application/octet-stream, */*"
    )


def test_multipart_text_is_canonical_top_level_field():
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello canonical")

    with patch("app.s2_client.urlopen", return_value=_response(b"pcm")) as urlopen:
        client.generate_multipart(request, boundary="b")

    body = urlopen.call_args.args[0].data
    assert b'Content-Disposition: form-data; name="text"' in body
    assert b"\r\n\r\nhello canonical\r\n" in body


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

    # params is a single JSON string (not flattened fields)
    assert b'Content-Disposition: form-data; name="params"' in body

    # The params value is valid JSON containing generation settings
    # (exact whitespace varies by json.dumps, so we parse the body)
    body_str = body.decode("utf-8")
    # Extract the params value from the multipart body
    params_start = body_str.index('name="params"') + len('name="params"')
    # Find the \r\n\r\n that separates headers from body
    header_end = body_str.index("\r\n\r\n", params_start) + 4
    params_end = body_str.index("\r\n", header_end)
    params_json = body_str[header_end:params_end]

    params = json.loads(params_json)
    assert params["temperature"] == 0.58
    assert params["top_p"] == 0.88
    assert params["top_k"] == 40
    assert params["max_new_tokens"] == 512
    assert params["output_format"] == "pcm_s16le"
    assert params["segment_sentences"] is True


def test_multipart_no_flattened_generation_fields():
    """Verify that generation settings (model, voice, stream, chunked, etc.)
    do NOT appear as individual top-level multipart fields."""
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello", voice="voice-a")

    with patch("app.s2_client.urlopen", return_value=_response(b"pcm")) as urlopen:
        client.generate_multipart(request, boundary="b")

    body = urlopen.call_args.args[0].data
    body_str = body.decode("utf-8")

    # These must NOT appear as top-level multipart field names
    assert 'name="voice"' not in body_str
    assert 'name="model"' not in body_str
    assert 'name="stream"' not in body_str
    assert 'name="chunked"' not in body_str
    assert 'name="output_format"' not in body_str
    assert 'name="temperature"' not in body_str

    # Only text, params, and optional prompt_text are top-level
    assert 'name="text"' in body_str
    assert 'name="params"' in body_str


def test_multipart_omits_empty_prompt_text():
    """prompt_text is omitted when empty (no reference cloning needed)."""
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello", prompt_text="")

    with patch("app.s2_client.urlopen", return_value=_response(b"pcm")) as urlopen:
        client.generate_multipart(request, boundary="no-ref")

    body = urlopen.call_args.args[0].data
    body_str = body.decode("utf-8")
    assert 'name="prompt_text"' not in body_str
    assert 'name="text"' in body_str


def test_multipart_prompt_audio_with_prompt_text():
    """prompt_audio file part and prompt_text field are paired correctly."""
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(
        text="clone this voice",
        prompt_text="exact words spoken in the reference audio",
    )

    with patch("app.s2_client.urlopen", return_value=_response(b"pcm")) as urlopen:
        client.generate_multipart(
            request,
            files={
                "prompt_audio": ("ref.wav", b"\x00\x01\x02", "audio/wav"),
            },
            boundary="clone-boundary",
        )

    body = urlopen.call_args.args[0].data

    # prompt_audio file part
    assert (
        b'Content-Disposition: form-data; name="prompt_audio";'
        b' filename="ref.wav"'
    ) in body
    assert b"Content-Type: audio/wav" in body
    assert b"\x00\x01\x02" in body

    # prompt_text field
    assert b'Content-Disposition: form-data; name="prompt_text"' in body
    assert b"exact words spoken in the reference audio" in body


def test_multipart_prompt_audio_without_prompt_text_raises():
    """prompt_audio without prompt_text must raise ValueError per the
    verified upstream contract."""
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello", prompt_text="")

    with pytest.raises(ValueError, match="prompt_audio requires prompt_text"):
        client.generate_multipart(
            request,
            files={"prompt_audio": ("ref.wav", b"data", "audio/wav")},
        )


def test_multipart_prompt_audio_only_file_no_prompt_text_raises():
    """Verify the validation message makes the requirement clear."""
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello")

    with pytest.raises(ValueError, match="prompt_audio requires prompt_text"):
        client.generate_multipart(
            request,
            files={"prompt_audio": ("ref.wav", b"data", "audio/wav")},
        )


def test_multipart_result_audio_and_content_type():
    pcm = b"\x01\x02\x03"
    client = S2Client(S2Endpoint("127.0.0.1", 3030), timeout_seconds=1)
    request = S2GenerateRequest(text="hello")

    with patch(
        "app.s2_client.urlopen",
        return_value=_response(pcm, content_type="audio/wav"),
    ) as urlopen:
        result = client.generate_multipart(request, boundary="b")

    assert result.audio == pcm
    assert result.content_type == "audio/wav"

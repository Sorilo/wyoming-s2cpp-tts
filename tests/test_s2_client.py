import json
import urllib.error
from unittest.mock import Mock, patch

import pytest

from app.config import Settings
from app.s2_client import S2Client, S2ClientError, S2Endpoint, S2GenerateRequest


def _response(body: bytes, status: int = 200, content_type: str = "audio/L16"):
    response = Mock()
    response.status = status
    response.read.return_value = body
    response.headers = {"Content-Type": content_type}
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=None)
    return response


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

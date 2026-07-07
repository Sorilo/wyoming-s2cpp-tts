from app.config import Settings
from app.s2_client import S2ClientError, S2GenerateResult
from app.smoke_s2cpp import SmokeResult, run_smoke


class RecordingClient:
    def __init__(self, result=None, error=None):
        self.result = result or S2GenerateResult(audio=b"pcm", content_type="audio/L16")
        self.error = error
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result


def test_smoke_skips_unless_tts_backend_is_s2cpp():
    client = RecordingClient()
    result = run_smoke(
        settings=Settings(tts_backend="fake"),
        text="hello",
        client_factory=lambda _settings: client,
    )

    assert result.status == "skipped"
    assert result.bytes_received == 0
    assert client.requests == []


def test_smoke_calls_generate_when_s2cpp_backend_enabled():
    client = RecordingClient(
        result=S2GenerateResult(audio=b"\x01\x02\x03\x04", content_type="audio/L16")
    )
    settings = Settings(tts_backend="s2cpp", s2_host="192.168.1.45", s2_port=3030)

    result = run_smoke(
        settings=settings,
        text="hello smoke",
        client_factory=lambda _settings: client,
    )

    assert result == SmokeResult(
        status="ok",
        endpoint="http://192.168.1.45:3030/generate",
        content_type="audio/L16",
        bytes_received=4,
        message="s2.cpp /generate returned 4 bytes (audio/L16)",
    )
    assert client.requests[0].text == "hello smoke"


def test_smoke_reports_unavailable_without_raising():
    client = RecordingClient(error=S2ClientError("connection refused"))

    result = run_smoke(
        settings=Settings(tts_backend="s2cpp"),
        text="hello",
        client_factory=lambda _settings: client,
    )

    assert result.status == "unavailable"
    assert result.bytes_received == 0
    assert "connection refused" in result.message

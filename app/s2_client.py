"""s2.cpp HTTP client for an already-running backend server.

Phase 2 only adds a small client for a separate s2.cpp HTTP `/generate`
endpoint. This module does not start, build, compile, package, or supervise
s2.cpp, and it does not download models.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from app.config import Settings

# Imported as a module-level name so tests can patch app.s2_client.urlopen.
urlopen = urllib.request.urlopen


class S2ClientError(RuntimeError):
    """Raised when the external s2.cpp HTTP backend cannot generate audio."""


@dataclass(frozen=True)
class S2Endpoint:
    """Connection details for an already-running s2.cpp HTTP server."""

    host: str = "127.0.0.1"
    port: int = 3030

    @classmethod
    def from_settings(cls, settings: Settings) -> "S2Endpoint":
        """Build endpoint details from app settings."""
        return cls(host=settings.s2_host, port=settings.s2_port)

    @property
    def base_url(self) -> str:
        """Return the base HTTP URL."""
        return f"http://{self.host}:{self.port}"

    @property
    def generate_url(self) -> str:
        """Return the planned s2.cpp generation endpoint URL."""
        return f"{self.base_url}/generate"


@dataclass(frozen=True)
class S2GenerateRequest:
    """Request payload for s2.cpp `/generate`.

    The exact upstream endpoint shape may evolve as s2.cpp is verified. Keep the
    payload explicit and tested so later phases can adapt it safely.
    """

    text: str
    voice: str = ""
    model: str = "/models/s2-pro-q6_k.gguf"
    stream: bool = True
    chunked: bool = True
    output_format: str = "pcm_s16le"
    segment_sentences: bool = True
    max_new_tokens: int = 512
    temperature: float = 0.58
    top_p: float = 0.88
    top_k: int = 40

    @classmethod
    def from_settings(
        cls,
        text: str,
        settings: Settings,
        voice: str | None = None,
    ) -> "S2GenerateRequest":
        """Create a generation request from app settings."""
        return cls(
            text=text,
            voice=settings.s2_default_voice if voice is None else voice,
            model=settings.s2_model,
            stream=settings.s2_stream,
            chunked=settings.s2_chunked,
            output_format=settings.s2_output_format,
            segment_sentences=settings.s2_segment_sentences,
            max_new_tokens=settings.s2_max_new_tokens,
            temperature=settings.s2_temperature,
            top_p=settings.s2_top_p,
            top_k=settings.s2_top_k,
        )

    def to_payload(self) -> dict[str, Any]:
        """Convert to a JSON-serializable s2.cpp request payload."""
        payload: dict[str, Any] = {
            "text": self.text,
            "model": self.model,
            "stream": self.stream,
            "chunked": self.chunked,
            "output_format": self.output_format,
            "segment_sentences": self.segment_sentences,
            "max_new_tokens": self.max_new_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
        }
        if self.voice:
            payload["voice"] = self.voice
        return payload


@dataclass(frozen=True)
class S2GenerateResult:
    """Raw result returned by the external s2.cpp `/generate` endpoint."""

    audio: bytes
    content_type: str


class S2Client:
    """Small synchronous client for an already-running s2.cpp HTTP server."""

    def __init__(self, endpoint: S2Endpoint, timeout_seconds: float = 60.0) -> None:
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_settings(cls, settings: Settings) -> "S2Client":
        """Create a client from app settings."""
        return cls(S2Endpoint.from_settings(settings))

    def generate(self, request: S2GenerateRequest) -> S2GenerateResult:
        """POST to `/generate` and return raw audio bytes.

        This intentionally buffers the response for Phase 2. Progressive
        streaming belongs in Phase 5.
        """
        body = json.dumps(request.to_payload()).encode("utf-8")
        http_request = urllib.request.Request(
            self.endpoint.generate_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "audio/L16, audio/wav, application/octet-stream, */*",
            },
        )

        try:
            with urlopen(http_request, timeout=self.timeout_seconds) as response:
                audio = response.read()
                content_type = response.headers.get(
                    "Content-Type",
                    "application/octet-stream",
                )
        except urllib.error.URLError as exc:
            raise S2ClientError(f"s2.cpp /generate failed: {exc}") from exc

        return S2GenerateResult(audio=audio, content_type=content_type)

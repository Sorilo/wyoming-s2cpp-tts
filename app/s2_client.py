"""s2.cpp HTTP client for an already-running backend server.

This module contains backend-client-only helpers for a separate s2.cpp HTTP
`/generate` endpoint. It supports the existing buffered JSON path and the Phase
5A buffered multipart/form-data path. It does not start, build, compile,
package, or supervise s2.cpp, and it does not download models.
"""

from __future__ import annotations

import json
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from app.config import Settings

# Imported as a module-level name so tests can patch app.s2_client.urlopen.
urlopen = urllib.request.urlopen

MultipartFiles = dict[str, tuple[str, bytes, str]]


def _stringify_multipart_value(value: Any) -> str:
    """Convert scalar payload values to form field strings.

    Booleans intentionally use lowercase strings because typical multipart form
    endpoints receive scalar values as text, not JSON tokens.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _quote_multipart_header_value(value: str) -> str:
    """Quote a multipart header parameter value for deterministic tests."""
    return value.replace("\\", "\\\\").replace('"', r'\"')


def encode_multipart_form_data(
    fields: dict[str, Any],
    files: MultipartFiles | None = None,
    boundary: str | None = None,
) -> tuple[str, bytes]:
    """Encode fields/files as multipart/form-data.

    Phase 5A uses this for mocked request-construction compatibility only. The
    exact upstream s2.cpp field names are still unverified, so callers should
    keep tests explicit when adapting this later.
    """
    active_boundary = boundary or f"wyoming-s2cpp-tts-{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.append(f"--{active_boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                "Content-Disposition: form-data; "
                f'name="{_quote_multipart_header_value(name)}"\r\n\r\n'
            ).encode("utf-8")
        )
        chunks.append(_stringify_multipart_value(value).encode("utf-8"))
        chunks.append(b"\r\n")

    for name, (filename, content, content_type) in (files or {}).items():
        chunks.append(f"--{active_boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                "Content-Disposition: form-data; "
                f'name="{_quote_multipart_header_value(name)}"; '
                f'filename="{_quote_multipart_header_value(filename)}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(content)
        chunks.append(b"\r\n")

    chunks.append(f"--{active_boundary}--\r\n".encode("utf-8"))
    return f"multipart/form-data; boundary={active_boundary}", b"".join(chunks)


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

    def to_multipart_fields(self) -> dict[str, Any]:
        """Convert to multipart scalar fields.

        This intentionally mirrors the existing JSON payload keys for Phase 5A.
        The exact upstream multipart field names are unresolved until verified
        against a real s2.cpp backend.
        """
        return self.to_payload()


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

    def _read_generate_response(self, http_request: urllib.request.Request) -> S2GenerateResult:
        """Send a prepared `/generate` request and return buffered audio."""
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

    def generate(self, request: S2GenerateRequest) -> S2GenerateResult:
        """POST JSON to `/generate` and return raw audio bytes.

        This intentionally buffers the response for Phase 2. Progressive
        streaming belongs in Phase 5B/5C.
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
        return self._read_generate_response(http_request)

    def generate_multipart(
        self,
        request: S2GenerateRequest,
        files: MultipartFiles | None = None,
        boundary: str | None = None,
    ) -> S2GenerateResult:
        """POST multipart/form-data to `/generate` and return raw audio bytes.

        Phase 5A adds request-construction compatibility only. This method still
        buffers the backend response and does not implement streaming.
        """
        content_type, body = encode_multipart_form_data(
            fields=request.to_multipart_fields(),
            files=files,
            boundary=boundary,
        )
        http_request = urllib.request.Request(
            self.endpoint.generate_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": content_type,
                "Accept": "audio/L16, audio/wav, application/octet-stream, */*",
            },
        )
        return self._read_generate_response(http_request)

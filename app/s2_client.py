"""s2.cpp HTTP client for an already-running backend server.

This module contains backend-client-only helpers for a separate s2.cpp HTTP
``/generate`` endpoint. It supports the existing buffered JSON path, the
Phase 5A.2 buffered multipart/form-data path using canonical upstream fields
verified against the official target ``rodrigomatta/s2.cpp`` OpenAPI spec, and
the Phase 5B streaming multipart path that yields audio chunks progressively.
It does not start, build, compile, package, or supervise s2.cpp, and it does not
download models.
"""

from __future__ import annotations

import json
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings

# Imported as a module-level name so tests can patch app.s2_client.urlopen.
urlopen = urllib.request.urlopen

MultipartFiles = dict[str, tuple[str, bytes, str]]

# Canonical reference-audio file-part key and its accepted aliases.
_REFERENCE_AUDIO_FIELD = "reference"
_REFERENCE_AUDIO_ALIASES = frozenset(
    {"reference_audio", "prompt_audio", "ref_audio"}
)


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

    Phase 5A.2 uses the canonical upstream s2.cpp multipart fields verified
    against the ``rodrigomatta/s2.cpp`` OpenAPI spec.
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
    """Request payload for s2.cpp ``/generate``.

    The JSON payload shape (``to_payload``) is the existing Phase 2 buffered
    format. The multipart format (``to_multipart_fields``) was verified against
    the official target ``rodrigomatta/s2.cpp`` OpenAPI spec in Phase 5A.2 and
    uses the canonical fields: ``text``, ``params`` (one JSON string),
    optional ``reference_text``, ``voice``, and ``voice_dir``.
    """

    text: str
    voice: str = ""
    model: str = "/models/s2-pro-q6_k.gguf"
    stream: bool = True
    chunked: bool = True
    output_format: str = "pcm_s16le"
    segment_sentences: bool = False
    max_new_tokens: int = 512
    temperature: float = 0.58
    top_p: float = 0.88
    top_k: int = 40
    prompt_text: str = ""
    voice_dir: str = ""
    codec_decode_context_frames: int | None = None

    @classmethod
    def from_settings(
        cls,
        text: str,
        settings: Settings,
        voice: str | None = None,
        prompt_text: str = "",
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
            prompt_text=prompt_text,
            voice_dir=settings.s2_voice_dir,
            codec_decode_context_frames=settings.s2_codec_decode_context_frames,
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

    def to_multipart_fields(self, streaming: bool = False) -> dict[str, Any]:
        """Convert to canonical s2.cpp multipart scalar fields.

        Verified against the official target ``rodrigomatta/s2.cpp``
        (``openapi/s2-openapi.yaml``).  The canonical format uses ``text``
        as a required string field and ``params`` as a single JSON-encoded
        string holding generation settings.

        When reference-audio cloning is requested the ``reference_text``
        field accompanies the ``reference`` file part.
        ``prompt_audio`` and ``prompt_text`` are accepted upstream
        aliases but are NOT the canonical emitted field names.

        ``voice`` and ``voice_dir`` are canonical top-level fields for
        saved voice profiles (``.s2voice``).

        Individual settings such as ``stream``, ``chunked``,
        ``output_format``, and ``model`` are NOT top-level multipart
        fields — they belong inside ``params``.
        """
        params: dict[str, Any] = {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "max_new_tokens": self.max_new_tokens,
            "output_format": self.output_format,
            "segment_sentences": self.segment_sentences,
        }
        if streaming:
            params["stream"] = True
            params["chunked"] = True
            params["output_format"] = "pcm_s16le"
            params["low_latency"] = True
            params["segment_sentences"] = False

        _VALID_CONTEXTS = frozenset({4, 64, 160})
        if self.codec_decode_context_frames is not None:
            if self.codec_decode_context_frames not in _VALID_CONTEXTS:
                raise ValueError(
                    f"codec_decode_context_frames must be one of "
                    f"{sorted(_VALID_CONTEXTS)} or None, got "
                    f"{self.codec_decode_context_frames}"
                )
            params["codec_decode_context_frames"] = self.codec_decode_context_frames
        fields: dict[str, Any] = {
            "text": self.text,
            "params": json.dumps(params),
        }
        if self.prompt_text:
            fields["reference_text"] = self.prompt_text
        if self.voice:
            fields["voice"] = self.voice
        if self.voice_dir:
            fields["voice_dir"] = self.voice_dir
        return fields


@dataclass(frozen=True)
class S2GenerateResult:
    """Raw result returned by the external s2.cpp ``/generate`` endpoint."""

    audio: bytes
    content_type: str
    response_headers: dict[str, str] = field(default_factory=dict)


class S2StreamResult:
    """Streaming iterator over progressive s2.cpp audio chunks.

    Phase 5B adds this resource-safe context manager / iterator that yields
    raw audio bytes from the backend response one chunk at a time *without*
    buffering the entire response first.  The HTTP connection remains open
    while chunks are consumed and is closed on normal completion, backend
    error, or early consumer exit.

    Phase 8A adds explicit ``cancel()`` support so a blocked ``read()`` in
    ``asyncio.to_thread`` can be unblocked from the event-loop thread.

    Typical usage::

        with client.generate_stream(request) as stream:
            for chunk in stream:
                process(chunk)
    """

    def __init__(self, http_request, timeout_seconds=60.0):
        self._http_request = http_request
        self._timeout_seconds = timeout_seconds
        self._response = None
        self._closed = False
        self._cancelled = False

    def __enter__(self):
        """Open the HTTP connection and return self as the iterator."""
        try:
            self._response = urlopen(
                self._http_request, timeout=self._timeout_seconds
            )
        except urllib.error.URLError as exc:
            raise S2ClientError(
                f"s2.cpp /generate streaming failed: {exc}"
            ) from exc
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Close the HTTP response on context exit.

        Resources are released after normal completion, backend error, or
        early consumer exit.  Exceptions are NOT suppressed.
        """
        self._closed = True
        self._cancelled = True
        if self._response is not None:
            try:
                self._response.close()
            except Exception:
                pass
        return False

    def cancel(self) -> None:
        """Signal cancellation and close the underlying HTTP response.

        Thread-safe — may be called from any thread.  A blocked
        ``response.read()`` in ``__next__`` will be unblocked when the
        response is closed, causing ``__next__`` to return an empty
        chunk (which becomes ``StopIteration`` via ``_read_stream_chunk``).
        """
        self._cancelled = True
        if self._response is not None:
            try:
                self._response.close()
            except Exception:
                pass

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed or self._cancelled or self._response is None:
            raise StopIteration
        try:
            chunk = self._response.read(4096)
        except Exception as exc:
            self._closed = True
            try:
                self._response.close()
            except Exception:
                pass
            self._response = None
            raise S2ClientError(
                f"s2.cpp streaming read failed: {exc}"
            ) from exc

        if not chunk:
            try:
                self._response.close()
            except Exception:
                pass
            self._response = None
            raise StopIteration
        return chunk

    @property
    def content_type(self):
        if self._response is None:
            return "application/octet-stream"
        return self._response.headers.get(
            "Content-Type", "application/octet-stream"
        )

    @property
    def status_code(self) -> int | None:
        """HTTP status code from the backend response, or *None* before connect."""
        if self._response is None:
            return None
        return self._response.status

    @property
    def response_headers(self) -> dict[str, str]:
        """Return a copy of the backend response headers (lowercase keys).

        Returns an empty dict when the connection has not been opened yet.
        """
        if self._response is None:
            return {}
        return {k.lower(): v for k, v in self._response.headers.items()}


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
        """Send a prepared ``/generate`` request and return buffered audio."""
        try:
            with urlopen(http_request, timeout=self.timeout_seconds) as response:
                audio = response.read()
                content_type = response.headers.get(
                    "Content-Type",
                    "application/octet-stream",
                )
                response_headers = {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.URLError as exc:
            raise S2ClientError(f"s2.cpp /generate failed: {exc}") from exc

        return S2GenerateResult(
            audio=audio,
            content_type=content_type,
            response_headers=response_headers,
        )

    def generate(self, request: S2GenerateRequest) -> S2GenerateResult:
        """POST JSON to ``/generate`` and return raw audio bytes.

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

    def generate_stream(self, request, files=None, boundary=None):
        """POST multipart/form-data and yield audio chunks progressively.

        Phase 5B: builds a canonical multipart request with streaming params
        (``stream=true``, ``chunked=true``, ``output_format="pcm_s16le"``,
        ``low_latency=true``) in the ``params`` JSON string. Returns a
        ``S2StreamResult`` context manager / iterator.

        The caller must consume the iterator inside a ``with`` block::

            with client.generate_stream(request) as stream:
                for chunk in stream:
                    ...

        Real backend streaming remains unverified until a real s2.cpp
        backend is tested.
        """
        # Normalise reference-audio aliases to the canonical key.
        _files = {}
        if files:
            for key, value in files.items():
                if key in _REFERENCE_AUDIO_ALIASES:
                    _files[_REFERENCE_AUDIO_FIELD] = value
                else:
                    _files[key] = value

        has_reference = _REFERENCE_AUDIO_FIELD in _files
        if has_reference and not request.prompt_text:
            raise ValueError(
                "reference requires reference_text (transcript for the"
                " reference audio); accepted aliases: prompt_text, ref_text"
            )

        content_type, body = encode_multipart_form_data(
            fields=request.to_multipart_fields(streaming=True),
            files=_files,
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
        return S2StreamResult(http_request, self.timeout_seconds)

    def generate_multipart(
        self,
        request: S2GenerateRequest,
        files: MultipartFiles | None = None,
        boundary: str | None = None,
    ) -> S2GenerateResult:
        """POST multipart/form-data to ``/generate`` and return raw audio bytes.

        Phase 5A.2 uses canonical fields from ``rodrigomatta/s2.cpp``
        (``openapi/s2-openapi.yaml``). The canonical file-part key is
        ``reference``; accepted aliases are ``reference_audio``,
        ``prompt_audio``, and ``ref_audio`` and are normalised internally.

        When reference audio is provided, ``reference_text`` (or the
        ``prompt_text`` field on the request) is required — a
        ``ValueError`` is raised otherwise.

        This method still buffers the backend response and does not
        implement streaming.
        """
        # Normalise reference-audio aliases to the canonical key.
        _files: dict[str, tuple[str, bytes, str]] = {}
        if files:
            for key, value in files.items():
                if key in _REFERENCE_AUDIO_ALIASES:
                    _files[_REFERENCE_AUDIO_FIELD] = value
                else:
                    _files[key] = value

        has_reference = _REFERENCE_AUDIO_FIELD in _files
        if has_reference and not request.prompt_text:
            raise ValueError(
                "reference requires reference_text (transcript for the"
                " reference audio); accepted aliases: prompt_text, ref_text"
            )

        content_type, body = encode_multipart_form_data(
            fields=request.to_multipart_fields(),
            files=_files,
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

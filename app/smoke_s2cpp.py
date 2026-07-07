"""Optional direct smoke test for an already-running s2.cpp `/generate` backend.

This module is deliberately opt-in and backend-client-only. It never starts,
builds, downloads, packages, or supervises s2.cpp.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from app.config import Settings
from app.s2_client import (
    S2Client,
    S2ClientError,
    S2Endpoint,
    S2GenerateRequest,
    S2GenerateResult,
)


class GenerateClient(Protocol):
    """Minimal protocol for objects that can generate one buffered response."""

    def generate(self, request: S2GenerateRequest) -> S2GenerateResult:
        """Generate one buffered response from s2.cpp."""


ClientFactory = Callable[[Settings], GenerateClient]


@dataclass(frozen=True)
class SmokeResult:
    """Outcome of an optional direct s2.cpp smoke attempt."""

    status: str
    endpoint: str
    content_type: str
    bytes_received: int
    message: str


def run_smoke(
    settings: Settings,
    text: str,
    client_factory: ClientFactory = S2Client.from_settings,
) -> SmokeResult:
    """Run one optional direct `/generate` smoke request.

    Returns `skipped` unless `settings.tts_backend == "s2cpp"`. If the backend
    is unavailable, returns `unavailable` instead of raising so normal developer
    runs and CI do not require local model infrastructure.
    """
    endpoint = S2Endpoint.from_settings(settings).generate_url

    if settings.tts_backend != "s2cpp":
        return SmokeResult(
            status="skipped",
            endpoint=endpoint,
            content_type="",
            bytes_received=0,
            message="skipped: set TTS_BACKEND=s2cpp to opt in to direct s2.cpp smoke",
        )

    request = S2GenerateRequest.from_settings(text=text, settings=settings)
    client = client_factory(settings)
    try:
        result = client.generate(request)
    except S2ClientError as exc:
        return SmokeResult(
            status="unavailable",
            endpoint=endpoint,
            content_type="",
            bytes_received=0,
            message=f"s2.cpp backend unavailable: {exc}",
        )

    bytes_received = len(result.audio)
    return SmokeResult(
        status="ok",
        endpoint=endpoint,
        content_type=result.content_type,
        bytes_received=bytes_received,
        message=f"s2.cpp /generate returned {bytes_received} bytes ({result.content_type})",
    )

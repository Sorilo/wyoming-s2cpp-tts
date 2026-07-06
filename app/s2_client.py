"""s2.cpp HTTP client scaffold.

TODO Phase 2: add a small async/sync client for an already-running s2.cpp HTTP
server. Do not build or launch s2.cpp from this module.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class S2Endpoint:
    """Connection details for the planned local s2.cpp HTTP server."""

    host: str = "127.0.0.1"
    port: int = 3030

    @property
    def base_url(self) -> str:
        """Return the base HTTP URL."""
        return f"http://{self.host}:{self.port}"

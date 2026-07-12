"""Phase 9C Slice 4: Lightweight read-only admin HTTP server.

Pure asyncio HTTP responder — no framework dependency.  Serves:

- GET /livez   — liveness (200 while process alive)
- GET /readyz  — readiness (200 only when RUNNING, 503 otherwise)
- GET /status  — sanitized JSON operational snapshot
- GET /metrics — sanitized JSON metrics snapshot (stable format, counters in Slice 5)

Safety properties:
- Disabled by default (configurable)
- Loopback-bound by default (127.0.0.1)
- Independent of the Wyoming port
- No plaintext synthesis, raw audio, secrets, tokens, or env dumps
- Lightweight and non-blocking
- Governed by the same lifecycle owner as the Wyoming server
- No mutating actions
- Robust bounded HTTP parsing with cumulative size/timeout limits
- Endpoint errors are isolated from Wyoming
- Bind failure does not prevent service startup (admin is optional)
- Privacy: never leaks raw request data in responses or logs
- Connection tracking: all active connections closed on stop
- Deterministic status codes: 413 body, 431 header, 408 timeout, 400 malformed
- 405 includes Allow: GET header; Content-Length counts UTF-8 bytes
- /status and /metrics use independent schema functions
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from app.config import Settings
from app.lifecycle import LifecycleState, ServiceLifecycle

logger = logging.getLogger(__name__)

# ── HTTP constants ──────────────────────────────────────────────────────────

CRLF = b"\r\n"
HEADER_TERMINATOR = b"\r\n\r\n"

HTTP_STATUS_TEXTS: dict[int, str] = {
    200: "200 OK",
    400: "400 Bad Request",
    404: "404 Not Found",
    405: "405 Method Not Allowed",
    408: "408 Request Timeout",
    413: "413 Payload Too Large",
    431: "431 Request Header Fields Too Large",
    500: "500 Internal Server Error",
    503: "503 Service Unavailable",
}

# Generic, privacy-safe error messages (never echo raw request data)
_PARSE_ERROR_MESSAGES: dict[int, str] = {
    400: "Bad Request",
    408: "Request Timeout",
    413: "Payload Too Large",
    431: "Request Header Fields Too Large",
}


# ── Status snapshot helpers ─────────────────────────────────────────────────


def build_status_snapshot(
    lifecycle: ServiceLifecycle,
    settings: Settings,
    scheduler_snapshot: dict[str, Any] | None = None,
    active_connection_count: int = 0,
    version: str = "",
    counters_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a sanitized, immutable operational snapshot for /status.

    Never exposes plaintext text, raw audio, secrets, tokens, environment
    dumps, mutable async objects, or per-request identifiers.
    """
    state = lifecycle.state
    snapshot: dict[str, Any] = {
        "state": state.value,
        "ready": state.is_ready(),
        "uptime_sec": round(lifecycle.uptime_sec, 3),
        "version": version or "unknown",
        "max_queue_size": settings.max_queue_size,
        "admin_http_enabled": settings.admin_http_enabled,
    }

    if scheduler_snapshot is not None:
        # Only include sanitized counters — no IDs, no tasks
        snapshot["scheduler_depth"] = scheduler_snapshot.get("depth", 0)
        snapshot["scheduler_pending"] = scheduler_snapshot.get("pending", 0)
        snapshot["scheduler_waiting"] = scheduler_snapshot.get("waiting_count", 0)
        snapshot["has_active_synthesis"] = (
            scheduler_snapshot.get("active_synthesis_id") is not None
        )

    if counters_snapshot is not None:
        snapshot["counters"] = counters_snapshot

    snapshot["active_connections"] = active_connection_count
    return snapshot


def build_metrics_snapshot(
    lifecycle: ServiceLifecycle,
    settings: Settings,
    scheduler_snapshot: dict[str, Any] | None = None,
    active_connection_count: int = 0,
    version: str = "",
    counters_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a sanitized, stable JSON metrics snapshot for /metrics.

    Independent schema from /status.  Includes cumulative counters
    from Phase 9C Slice 5.

    Never exposes IDs, tokens, text, audio, or secrets.
    """
    state = lifecycle.state
    snapshot: dict[str, Any] = {
        "schema_version": "1.0",
        "state": state.value,
        "ready": state.is_ready(),
        "uptime_sec": round(lifecycle.uptime_sec, 3),
        "version": version or "unknown",
        "active_connections": active_connection_count,
    }

    if scheduler_snapshot is not None:
        snapshot["scheduler_depth"] = scheduler_snapshot.get("depth", 0)
        snapshot["scheduler_pending"] = scheduler_snapshot.get("pending", 0)
        snapshot["scheduler_waiting"] = scheduler_snapshot.get("waiting_count", 0)
        snapshot["has_active_synthesis"] = (
            scheduler_snapshot.get("active_synthesis_id") is not None
        )

    if counters_snapshot is not None:
        snapshot["counters"] = counters_snapshot

    return snapshot


# ── Response builders ───────────────────────────────────────────────────────

# Valid HTTP methods accepted by the admin server
_ALLOWED_METHODS = {"GET", "HEAD", "OPTIONS"}


def _json_response(status_code: int, body: dict[str, Any]) -> bytes:
    """Serialize a JSON response with correct headers and UTF-8 byte counting."""
    payload = json.dumps(body, sort_keys=True)
    payload_bytes = payload.encode("utf-8")
    status_text = HTTP_STATUS_TEXTS.get(status_code, f"{status_code} Unknown")
    extra_headers = ""
    if status_code == 405:
        extra_headers = "Allow: GET\r\n"
    return (
        f"HTTP/1.1 {status_text}\r\n"
        f"Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(payload_bytes)}\r\n"
        f"Connection: close\r\n"
        f"{extra_headers}"
        f"\r\n"
    ).encode("ascii") + payload_bytes


def _text_response(status_code: int, body: str) -> bytes:
    """Serialize a plain-text response with correct headers and UTF-8 byte counting."""
    body_bytes = body.encode("utf-8")
    status_text = HTTP_STATUS_TEXTS.get(status_code, f"{status_code} Unknown")
    return (
        f"HTTP/1.1 {status_text}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("ascii") + body_bytes


# ── HTTP request parser ─────────────────────────────────────────────────────


class HttpRequest:
    """Minimal parsed HTTP request."""

    __slots__ = ("method", "path", "version", "headers", "body")

    def __init__(
        self,
        method: str = "",
        path: str = "",
        version: str = "",
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        self.method = method
        self.path = path
        self.version = version
        self.headers: dict[str, str] = headers or {}
        self.body = body


class HttpParseError(ValueError):
    """Raised when an HTTP request is malformed or exceeds limits.

    Includes a suggested HTTP status code for response mapping.
    """

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


async def _read_until(
    reader: asyncio.StreamReader, delimiter: bytes, max_bytes: int, deadline: float
) -> bytes:
    """Read until *delimiter* or *max_bytes* bytes, with a deadline.

    Raises HttpParseError if the read exceeds *max_bytes* or the deadline expires.
    """
    data = bytearray()
    remaining = deadline - time.monotonic()
    while remaining > 0:
        try:
            chunk = await asyncio.wait_for(reader.read(1), timeout=min(remaining, 0.5))
        except asyncio.TimeoutError:
            remaining = deadline - time.monotonic()
            continue
        if not chunk:
            raise HttpParseError("Connection closed before request complete", 400)
        data.extend(chunk)
        if len(data) > max_bytes:
            raise HttpParseError("Header section too large", 431)
        if data.endswith(delimiter):
            return bytes(data)
        remaining = deadline - time.monotonic()
    raise HttpParseError("Read timeout", 408)


async def parse_http_request(
    reader: asyncio.StreamReader,
    *,
    read_timeout_sec: float = 5.0,
    max_header_size: int = 8192,
    max_body_size: int = 65536,
) -> HttpRequest:
    """Parse one HTTP/1.1 request from *reader* with bounded limits.

    Returns an ``HttpRequest`` or raises ``HttpParseError`` for malformed data.

    Cumulative *max_header_size* is enforced across the request line and all
    headers combined.  Invalid Content-Length, transfer-encoding, and HTTP
    version are rejected deterministically.
    """
    deadline = time.monotonic() + read_timeout_sec
    total_header_bytes = 0

    # ── Request line ─────────────────────────────────────────────────────
    line_bytes = await _read_until(reader, CRLF, max_header_size, deadline)
    total_header_bytes += len(line_bytes)
    if total_header_bytes > max_header_size:
        raise HttpParseError("Header section too large", 431)

    line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
    parts = line.split(" ", 2)
    if len(parts) != 3:
        raise HttpParseError("Malformed request line", 400)
    method, path, version = parts

    # Validate HTTP method
    method_upper = method.upper()
    if method_upper not in ("GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"):
        raise HttpParseError("Unsupported method", 400)
    if not path.startswith("/"):
        raise HttpParseError("Invalid request path", 400)

    # Validate HTTP version
    version_upper = version.upper()
    if version_upper not in ("HTTP/1.0", "HTTP/1.1"):
        raise HttpParseError("Unsupported HTTP version", 400)

    # ── Headers ──────────────────────────────────────────────────────────
    headers: dict[str, str] = {}
    content_length_raw: str | None = None
    content_length_count = 0
    transfer_encoding_seen = False

    while True:
        remaining_budget = max_header_size - total_header_bytes
        if remaining_budget <= 0:
            raise HttpParseError("Header section too large", 431)
        header_bytes = await _read_until(reader, CRLF, remaining_budget, deadline)
        total_header_bytes += len(header_bytes)
        if total_header_bytes > max_header_size:
            raise HttpParseError("Header section too large", 431)

        header_line = header_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
        if header_line == "":
            break  # end of headers
        if ":" not in header_line:
            raise HttpParseError("Malformed header line", 400)
        key, _, value = header_line.partition(":")
        key_lower = key.strip().lower()
        val_stripped = value.strip()

        # Detect duplicate/conflicting Content-Length
        if key_lower == "content-length":
            content_length_count += 1
            if content_length_count > 1 and val_stripped != content_length_raw:
                raise HttpParseError("Multiple conflicting Content-Length headers", 400)
            content_length_raw = val_stripped

        # Reject unsupported transfer encodings
        if key_lower == "transfer-encoding":
            transfer_encoding_seen = True
            te_values = [v.strip().lower() for v in val_stripped.split(",")]
            for te in te_values:
                if te not in ("identity",):
                    raise HttpParseError("Unsupported transfer encoding", 400)

        headers[key_lower] = val_stripped

    # Validate Content-Length value
    content_length = 0
    if content_length_raw is not None:
        try:
            content_length = int(content_length_raw)
        except (TypeError, ValueError):
            raise HttpParseError("Invalid Content-Length", 400)
        if content_length < 0:
            raise HttpParseError("Negative Content-Length", 400)

    # Reject chunked transfer-encoding even without Content-Length
    te_value = headers.get("transfer-encoding", "").lower().strip()
    if te_value == "chunked":
        raise HttpParseError("Chunked transfer encoding not supported", 400)

    # ── Body ─────────────────────────────────────────────────────────────
    body = b""
    if content_length > 0:
        if content_length > max_body_size:
            raise HttpParseError("Body too large", 413)
        remaining = deadline - time.monotonic()
        while len(body) < content_length:
            if remaining <= 0:
                raise HttpParseError("Request body timeout", 408)
            to_read = min(content_length - len(body), 4096)
            try:
                chunk = await asyncio.wait_for(
                    reader.read(to_read), timeout=min(remaining, 1.0)
                )
            except asyncio.TimeoutError:
                remaining = deadline - time.monotonic()
                continue
            if not chunk:
                # Connection closed before full body — malformed
                raise HttpParseError("Incomplete request body", 400)
            body += chunk
            remaining = deadline - time.monotonic()

    return HttpRequest(
        method=method_upper,
        path=path,
        version=version_upper,
        headers=headers,
        body=body,
    )


# ── Admin HTTP server ───────────────────────────────────────────────────────


class AdminHttpServer:
    """Lightweight read-only admin HTTP server.

    Owned by ServiceCoordinator; starts as an asyncio ``TCP server`` on the
    configured host/port.  Serves only GET requests to known paths.

    Tracks active client connections and cancels them on stop for clean
    termination.
    """

    def __init__(
        self,
        settings: Settings,
        lifecycle: ServiceLifecycle,
        get_scheduler_snapshot: callable = lambda: None,
        get_active_connection_count: callable = lambda: 0,
        get_counters_snapshot: callable = lambda: None,
        version: str = "",
    ) -> None:
        self._settings = settings
        self._lifecycle = lifecycle
        self._get_scheduler_snapshot = get_scheduler_snapshot
        self._get_active_connection_count = get_active_connection_count
        self._get_counters_snapshot = get_counters_snapshot
        self._version = version
        self._server: asyncio.AbstractServer | None = None
        self._active_tasks: set[asyncio.Task[Any]] = set()

    # ── Endpoint handlers ─────────────────────────────────────────────────

    def _handle_livez(self) -> bytes:
        """GET /livez — liveness."""
        return _json_response(200, {"status": "alive"})

    def _handle_readyz(self) -> bytes:
        """GET /readyz — traffic readiness.

        200 only when RUNNING; 503 otherwise.
        """
        if self._lifecycle.state == LifecycleState.RUNNING:
            return _json_response(200, {"status": "ready"})
        return _json_response(503, {"status": "not_ready"})

    def _handle_status(self) -> bytes:
        """GET /status — sanitized JSON operational snapshot."""
        try:
            sched_snap = self._get_scheduler_snapshot()
        except Exception:
            sched_snap = None
        try:
            conn_count = self._get_active_connection_count()
        except Exception:
            conn_count = 0
        try:
            counters_snap = self._get_counters_snapshot()
        except Exception:
            counters_snap = None
        snapshot = build_status_snapshot(
            lifecycle=self._lifecycle,
            settings=self._settings,
            scheduler_snapshot=sched_snap,
            active_connection_count=conn_count,
            version=self._version,
            counters_snapshot=counters_snap,
        )
        return _json_response(200, snapshot)

    def _handle_metrics(self) -> bytes:
        """GET /metrics — sanitized JSON metrics snapshot.

        Uses independent schema from /status.  Includes cumulative
        counters from Phase 9C Slice 5.
        """
        try:
            sched_snap = self._get_scheduler_snapshot()
        except Exception:
            sched_snap = None
        try:
            conn_count = self._get_active_connection_count()
        except Exception:
            conn_count = 0
        try:
            counters_snap = self._get_counters_snapshot()
        except Exception:
            counters_snap = None
        snapshot = build_metrics_snapshot(
            lifecycle=self._lifecycle,
            settings=self._settings,
            scheduler_snapshot=sched_snap,
            active_connection_count=conn_count,
            version=self._version,
            counters_snapshot=counters_snap,
        )
        return _json_response(200, snapshot)

    # ── Request routing ───────────────────────────────────────────────────

    def _route(self, request: HttpRequest) -> bytes:
        """Route a parsed HTTP request to the correct handler."""
        path = request.path
        method = request.method

        # Only GET is supported
        if method != "GET":
            return _json_response(405, {
                "error": "Method Not Allowed",
                "allowed": sorted(_ALLOWED_METHODS),
            })

        if path == "/livez":
            return self._handle_livez()
        elif path == "/readyz":
            return self._handle_readyz()
        elif path == "/status":
            return self._handle_status()
        elif path == "/metrics":
            return self._handle_metrics()
        else:
            return _json_response(404, {"error": "Not Found"})

    # ── Connection handler ─────────────────────────────────────────────────

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle one HTTP connection (tracked for clean termination)."""
        task = asyncio.current_task()
        if task is not None:
            self._active_tasks.add(task)
        try:
            peername = writer.get_extra_info("peername", "unknown")
            while True:
                try:
                    request = await parse_http_request(
                        reader,
                        read_timeout_sec=self._settings.admin_http_read_timeout_sec,
                        max_header_size=self._settings.admin_http_max_header_size,
                        max_body_size=self._settings.admin_http_max_body_size,
                    )
                except HttpParseError as exc:
                    # Privacy: log only error type and generic status, never raw data
                    logger.debug(
                        "Admin HTTP parse error from %s: status=%d type=%s",
                        peername,
                        exc.status_code,
                        type(exc).__name__,
                    )
                    try:
                        error_msg = _PARSE_ERROR_MESSAGES.get(
                            exc.status_code, "Bad Request"
                        )
                        writer.write(_text_response(exc.status_code, error_msg))
                        await writer.drain()
                    except Exception:
                        pass
                    break

                try:
                    response = self._route(request)
                except Exception as exc:
                    # Privacy: no stack traces, no raw request data in logs
                    logger.error(
                        "Admin HTTP handler error on %s %s: %s",
                        request.method,
                        request.path,
                        type(exc).__name__,
                    )
                    try:
                        writer.write(
                            _json_response(500, {"error": "Internal Server Error"})
                        )
                        await writer.drain()
                    except Exception:
                        pass
                    break

                try:
                    writer.write(response)
                    await writer.drain()
                except Exception:
                    break
                break
        except asyncio.CancelledError:
            # Connection cancelled during termination — clean exit
            pass
        finally:
            if task is not None:
                self._active_tasks.discard(task)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> int:
        """Start the admin HTTP listener.

        Returns the bound port (may differ from config if port=0 was used).
        Raises OSError on bind failure (handled by coordinator).
        """
        host = self._settings.admin_http_host
        port = self._settings.admin_http_port
        self._server = await asyncio.start_server(
            self._handle_connection,
            host=host,
            port=port,
        )
        # Get the actual port in case port=0 was used
        sockets = self._server.sockets
        actual_port = sockets[0].getsockname()[1] if sockets else port
        logger.info(
            "Admin HTTP server listening on %s:%s (enabled=%s)",
            host,
            actual_port,
            self._settings.admin_http_enabled,
        )
        return actual_port

    async def stop(self) -> None:
        """Stop the admin HTTP listener and cancel all active connections."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        # Cancel and await all active connection tasks
        tasks = list(self._active_tasks)
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active_tasks.clear()

        logger.info("Admin HTTP server stopped")

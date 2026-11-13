"""Bounded loopback conformance for the production Official MCP Registry fetch."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mcp_platform.registry_fetch import (
    OFFICIAL_REGISTRY_URL,
    build_registry_page_fetch,
)
from plugin_platform.mcp_registry import McpRegistryError, McpRegistryRequest

_SERVERS_PATH = "/v0/servers"
_BODY = b'{"servers":[],"metadata":{"count":0}}'
_OVERSIZED_BODY = b"x" * 4096
_SLOW_SECONDS = 10.0
_READ_SECONDS = 0.5


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        path = self.path.split("?", 1)[0]
        self.server.requested.append(self.path)  # type: ignore[attr-defined]
        if path == _SERVERS_PATH:
            self._respond(200, _BODY)
        elif path == "/oversized":
            self._respond(200, _OVERSIZED_BODY)
        elif path == "/redirect":
            self._redirect("https://registry.example.test/v0/servers")
        elif path == "/same-origin-redirect":
            self._redirect(_SERVERS_PATH)
        elif path == "/slow":
            time.sleep(_SLOW_SECONDS)
            self._respond(200, _BODY)
        elif path == "/truncated":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(_BODY) + 64))
            self.end_headers()
            self.wfile.write(_BODY)
            self.close_connection = True
        else:
            self._respond(404, b"missing")

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        return


class _LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.requested: list[str] = []


@pytest.fixture
def loopback() -> Iterator[_LoopbackServer]:
    """Serve bounded registry responses on a closed loopback socket."""
    server = _LoopbackServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _url(server: _LoopbackServer, path: str) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}{path}"


def test_production_fetch_targets_the_exact_canonical_registry_url() -> None:
    """Catches the shipped default drifting off the configured canonical source."""
    assert OFFICIAL_REGISTRY_URL == "https://registry.modelcontextprotocol.io/v0/servers"


def test_bounded_fetch_returns_the_served_page_bytes(loopback: _LoopbackServer) -> None:
    """Catches a fetch that cannot retrieve a well-formed bounded page response."""
    fetch = build_registry_page_fetch(url=_url(loopback, _SERVERS_PATH))

    payload = fetch(McpRegistryRequest())

    assert payload == _BODY
    assert len(loopback.requested) == 1
    assert loopback.requested[0].split("?", 1)[0] == _SERVERS_PATH


def test_bounded_fetch_encodes_query_cursor_and_limit(loopback: _LoopbackServer) -> None:
    """Catches a fetch that drops the maintainer-requested query, cursor, or limit."""
    fetch = build_registry_page_fetch(url=_url(loopback, _SERVERS_PATH))

    fetch(McpRegistryRequest(query="weather", cursor="abc123", limit=10))

    assert len(loopback.requested) == 1
    query = loopback.requested[0].split("?", 1)[1]
    assert "search=weather" in query
    assert "cursor=abc123" in query
    assert "limit=10" in query


@pytest.mark.parametrize(
    "url",
    [
        "https://registry.example.test/v0/servers",
        "https://registry.modelcontextprotocol.io/v0/other",
        "https://registry.modelcontextprotocol.io.evil.test/v0/servers",
        "http://registry.modelcontextprotocol.io/v0/servers",
        "https://registry.modelcontextprotocol.io/v0/servers?mirror=1",
        "ftp://registry.modelcontextprotocol.io/v0/servers",
        "http://user:pass@127.0.0.1/v0/servers",
        "not a url",
    ],
)
def test_bounded_fetch_refuses_any_url_outside_the_canonical_source(url: str) -> None:
    """Catches the builder being repointed at an attacker-chosen or credentialed origin."""
    with pytest.raises(ValueError, match="plugin_registry_fetch_url_rejected"):
        build_registry_page_fetch(url=url)


def test_bounded_fetch_refuses_a_cross_origin_redirect(loopback: _LoopbackServer) -> None:
    """Catches a redirect walking the fetch off the canonical origin."""
    fetch = build_registry_page_fetch(url=_url(loopback, "/redirect"))

    with pytest.raises(McpRegistryError, match="plugin_registry_redirect_rejected"):
        fetch(McpRegistryRequest())


def test_bounded_fetch_never_follows_a_redirect_automatically(
    loopback: _LoopbackServer,
) -> None:
    """Catches automatic redirect following that hides the real response origin."""
    fetch = build_registry_page_fetch(url=_url(loopback, "/same-origin-redirect"))

    with pytest.raises(McpRegistryError, match="plugin_registry_redirect_rejected"):
        fetch(McpRegistryRequest())

    assert len(loopback.requested) == 1
    assert loopback.requested[0].split("?", 1)[0] == "/same-origin-redirect"


def test_bounded_fetch_aborts_past_the_byte_cap_while_streaming(
    loopback: _LoopbackServer,
) -> None:
    """Catches an unbounded body being buffered before the size limit applies."""
    fetch = build_registry_page_fetch(url=_url(loopback, "/oversized"), max_bytes=1024)

    with pytest.raises(McpRegistryError, match="plugin_registry_page_too_large"):
        fetch(McpRegistryRequest())


def test_bounded_fetch_reports_a_stable_timeout_cause_with_no_raw_exception_text(
    loopback: _LoopbackServer,
) -> None:
    """Catches an unbounded read hanging discovery on a stalled registry."""
    fetch = build_registry_page_fetch(url=_url(loopback, "/slow"), read_seconds=_READ_SECONDS)

    started = time.monotonic()
    with pytest.raises(McpRegistryError) as exc_info:
        fetch(McpRegistryRequest())

    assert time.monotonic() - started < _SLOW_SECONDS
    assert str(exc_info.value) == "plugin_registry_timeout"
    assert "127.0.0.1" not in str(exc_info.value)


def test_bounded_fetch_reports_a_rejected_status(loopback: _LoopbackServer) -> None:
    """Catches an error page being handed on as authentic registry bytes."""
    fetch = build_registry_page_fetch(url=_url(loopback, "/missing"))

    with pytest.raises(McpRegistryError, match="plugin_registry_rejected"):
        fetch(McpRegistryRequest())


def test_bounded_fetch_refuses_a_truncated_response_with_no_raw_exception_text(
    loopback: _LoopbackServer,
) -> None:
    """Catches a half-delivered body being handed on as a complete registry page."""
    fetch = build_registry_page_fetch(url=_url(loopback, "/truncated"))

    with pytest.raises(McpRegistryError) as exc_info:
        fetch(McpRegistryRequest())

    assert str(exc_info.value) == "plugin_registry_unreachable"
    assert "127.0.0.1" not in str(exc_info.value)


@pytest.mark.parametrize(
    "limits",
    [
        {"connect_seconds": 0.0},
        {"read_seconds": -1.0},
        {"total_seconds": 0.0},
        {"connect_seconds": 5},
        {"max_bytes": 0},
        {"max_bytes": 2 * 1024 * 1024},
    ],
)
def test_bounded_fetch_rejects_unbounded_or_mistyped_limits(limits: dict[str, object]) -> None:
    """Catches a fetch built without real connect, read, total, or size bounds."""
    with pytest.raises(ValueError, match="plugin_registry_fetch_config_invalid"):
        build_registry_page_fetch(**limits)  # type: ignore[arg-type]


def test_bounded_fetch_refuses_a_malformed_request_object(loopback: _LoopbackServer) -> None:
    """Catches a caller handing the transport something that never passed request validation."""
    fetch = build_registry_page_fetch(url=_url(loopback, _SERVERS_PATH))

    with pytest.raises(McpRegistryError, match="plugin_registry_transport_invalid"):
        fetch(object())  # type: ignore[arg-type]

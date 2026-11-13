"""Bounded loopback conformance for the production marketplace catalog fetch."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mcp_platform.catalog_fetch import (
    MARKETPLACE_CATALOG_URL,
    build_marketplace_catalog_fetch,
)
from plugin_platform.catalog_sync import CatalogFetchError, CatalogFetchResponse

_CATALOG_PATH = "/catalog/v2/catalog.json"
_BODY = b'{"schema_version":2,"catalog_id":"test.catalog"}'
_OVERSIZED_BODY = b"x" * 4096
_SLOW_SECONDS = 10.0
_READ_SECONDS = 0.5


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        self.server.requested.append(self.path)  # type: ignore[attr-defined]
        if self.path == _CATALOG_PATH:
            self._respond(200, _BODY)
        elif self.path == "/oversized":
            self._respond(200, _OVERSIZED_BODY)
        elif self.path == "/redirect":
            self._redirect("https://plugins.example.test/catalog.json")
        elif self.path == "/same-origin-redirect":
            self._redirect(_CATALOG_PATH)
        elif self.path == "/slow":
            time.sleep(_SLOW_SECONDS)
            self._respond(200, _BODY)
        elif self.path == "/truncated":
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
    """Serve bounded catalog responses on a closed loopback socket."""
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


def test_production_fetch_targets_the_exact_canonical_catalog_url() -> None:
    """Catches the shipped default drifting off the configured canonical source."""
    assert MARKETPLACE_CATALOG_URL == "https://plugins.omega2ai.com/catalog/v2/catalog.json"


def test_bounded_fetch_returns_the_served_catalog_bytes(loopback: _LoopbackServer) -> None:
    """Catches a fetch that cannot retrieve a well-formed bounded catalog response."""
    fetch = build_marketplace_catalog_fetch(url=_url(loopback, _CATALOG_PATH))

    response = fetch()

    assert type(response) is CatalogFetchResponse
    assert response.payload == _BODY
    assert loopback.requested == [_CATALOG_PATH]


@pytest.mark.parametrize(
    "url",
    [
        "https://plugins.example.test/catalog/v2/catalog.json",
        "https://plugins.omega2ai.com/catalog/v2/other.json",
        "https://plugins.omega2ai.com.evil.test/catalog/v2/catalog.json",
        "http://plugins.omega2ai.com/catalog/v2/catalog.json",
        "https://plugins.omega2ai.com/catalog/v2/catalog.json?mirror=1",
        "ftp://plugins.omega2ai.com/catalog/v2/catalog.json",
        "not a url",
    ],
)
def test_bounded_fetch_refuses_any_url_outside_the_canonical_source(url: str) -> None:
    """Catches the synchronizer being repointed at an attacker-chosen origin."""
    with pytest.raises(ValueError, match="plugin_catalog_url_rejected"):
        build_marketplace_catalog_fetch(url=url)


def test_bounded_fetch_refuses_a_cross_origin_redirect(loopback: _LoopbackServer) -> None:
    """Catches a redirect walking the fetch off the canonical origin."""
    fetch = build_marketplace_catalog_fetch(url=_url(loopback, "/redirect"))

    with pytest.raises(CatalogFetchError, match="plugin_catalog_redirect_rejected"):
        fetch()


def test_bounded_fetch_never_follows_a_redirect_automatically(
    loopback: _LoopbackServer,
) -> None:
    """Catches automatic redirect following that hides the real response origin."""
    fetch = build_marketplace_catalog_fetch(url=_url(loopback, "/same-origin-redirect"))

    with pytest.raises(CatalogFetchError, match="plugin_catalog_redirect_rejected"):
        fetch()

    assert loopback.requested == ["/same-origin-redirect"]


def test_bounded_fetch_aborts_past_the_byte_cap(loopback: _LoopbackServer) -> None:
    """Catches an unbounded body being buffered before the size limit applies."""
    fetch = build_marketplace_catalog_fetch(
        url=_url(loopback, "/oversized"),
        max_bytes=1024,
    )

    with pytest.raises(CatalogFetchError, match="plugin_catalog_too_large"):
        fetch()


def test_bounded_fetch_reports_a_stable_timeout_cause(loopback: _LoopbackServer) -> None:
    """Catches an unbounded read hanging synchronization on a stalled server."""
    fetch = build_marketplace_catalog_fetch(
        url=_url(loopback, "/slow"),
        read_seconds=_READ_SECONDS,
    )

    started = time.monotonic()
    with pytest.raises(CatalogFetchError, match="plugin_catalog_timeout"):
        fetch()

    assert time.monotonic() - started < _SLOW_SECONDS


def test_bounded_fetch_reports_a_rejected_status(loopback: _LoopbackServer) -> None:
    """Catches an error page being handed on as authentic catalog bytes."""
    fetch = build_marketplace_catalog_fetch(url=_url(loopback, "/missing"))

    with pytest.raises(CatalogFetchError, match="plugin_catalog_rejected"):
        fetch()


def test_bounded_fetch_refuses_a_truncated_response(loopback: _LoopbackServer) -> None:
    """Catches a half-delivered body being handed on as a complete catalog."""
    fetch = build_marketplace_catalog_fetch(url=_url(loopback, "/truncated"))

    with pytest.raises(CatalogFetchError, match="plugin_catalog_unreachable"):
        fetch()


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
    with pytest.raises(ValueError, match="plugin_catalog_fetch_config_invalid"):
        build_marketplace_catalog_fetch(**limits)  # type: ignore[arg-type]

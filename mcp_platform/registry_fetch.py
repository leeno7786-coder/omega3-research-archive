"""The one bounded transport that may retrieve Official MCP Registry pages.

The plugin foundation owns registry-page validation but performs no network
I/O itself (see ``plugin_platform.mcp_registry``). This module supplies the
synchronous fetch seam ``scripts.build_marketplace_catalog``'s
``fetch-registry`` command consumes in production: it speaks only to the
configured canonical registry URL, bounds connect, read, and total duration,
caps the body while streaming it, and refuses redirects outright. Every
failure is reported as a stable ``plugin_registry_*``
``plugin_platform.mcp_registry.McpRegistryError`` cause; raw transport
exception text never crosses this boundary. It follows the same shape as
``mcp_platform.catalog_fetch``, the equivalent bounded transport for the
signed marketplace catalog itself.
"""

from __future__ import annotations

import time
from urllib.parse import urlencode, urlsplit

import httpx2

from mcp_platform.transports.streamable_http import build_bounded_sync_http_client
from plugin_platform.mcp_registry import (
    MAX_PAGE_BYTES,
    McpRegistryError,
    McpRegistryRequest,
)

OFFICIAL_REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0/servers"

DEFAULT_CONNECT_SECONDS = 5.0
DEFAULT_READ_SECONDS = 15.0
DEFAULT_TOTAL_SECONDS = 30.0

_CONFIG_INVALID = "plugin_registry_fetch_config_invalid"
_URL_REJECTED = "plugin_registry_fetch_url_rejected"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_IDENTITY_ENCODINGS = frozenset({"", "identity"})


def _admitted_url(url: str) -> str:
    """Admit only the canonical registry URL, or a loopback conformance endpoint.

    Overriding the URL is confined to loopback so no configuration,
    environment, or caller can repoint registry discovery at an
    attacker-chosen origin; a caller able to reach a loopback listener
    already runs code on this machine.
    """
    if type(url) is not str:
        raise ValueError(_URL_REJECTED)
    if url == OFFICIAL_REGISTRY_URL:
        return url
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
    except ValueError:
        raise ValueError(_URL_REJECTED) from None
    if (
        parts.scheme not in {"http", "https"}
        or hostname is None
        or hostname.casefold() not in _LOOPBACK_HOSTS
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError(_URL_REJECTED)
    return url


def build_registry_page_fetch(
    *,
    url: str = OFFICIAL_REGISTRY_URL,
    connect_seconds: float = DEFAULT_CONNECT_SECONDS,
    read_seconds: float = DEFAULT_READ_SECONDS,
    total_seconds: float = DEFAULT_TOTAL_SECONDS,
    max_bytes: int = MAX_PAGE_BYTES,
):
    """Return one bounded synchronous page transport for the Official MCP Registry."""
    if (
        type(connect_seconds) is not float
        or connect_seconds <= 0
        or type(read_seconds) is not float
        or read_seconds <= 0
        or type(total_seconds) is not float
        or total_seconds <= 0
        or type(max_bytes) is not int
        or max_bytes <= 0
        or max_bytes > MAX_PAGE_BYTES
    ):
        raise ValueError(_CONFIG_INVALID)
    endpoint = _admitted_url(url)

    def fetch(request: McpRegistryRequest) -> bytes:
        if type(request) is not McpRegistryRequest:
            raise McpRegistryError("plugin_registry_transport_invalid")
        params: dict[str, str] = {"limit": str(request.limit)}
        if request.query is not None:
            params["search"] = request.query
        if request.cursor is not None:
            params["cursor"] = request.cursor
        target = f"{endpoint}?{urlencode(params)}"
        deadline = time.monotonic() + total_seconds
        client = build_bounded_sync_http_client(
            transport=None,
            connection_seconds=connect_seconds,
            operation_seconds=read_seconds,
        )
        try:
            with client.stream("GET", target) as response:
                if response.is_redirect:
                    raise McpRegistryError("plugin_registry_redirect_rejected")
                if response.status_code != 200:
                    raise McpRegistryError("plugin_registry_rejected")
                encoding = response.headers.get("Content-Encoding", "")
                if encoding.strip().casefold() not in _IDENTITY_ENCODINGS:
                    raise McpRegistryError("plugin_registry_encoding_unsupported")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    if time.monotonic() > deadline:
                        raise McpRegistryError("plugin_registry_timeout")
                    total += len(chunk)
                    if total > max_bytes:
                        raise McpRegistryError("plugin_registry_page_too_large")
                    chunks.append(bytes(chunk))
        except McpRegistryError:
            raise
        except httpx2.TimeoutException:
            raise McpRegistryError("plugin_registry_timeout") from None
        except (httpx2.HTTPError, OSError, ValueError):
            raise McpRegistryError("plugin_registry_unreachable") from None
        finally:
            client.close()
        return b"".join(chunks)

    return fetch


__all__ = ["OFFICIAL_REGISTRY_URL", "build_registry_page_fetch"]

"""The one bounded transport that may retrieve the signed marketplace catalog.

The plugin foundation owns verification and retention but performs no network
I/O. This module supplies the synchronous fetch seam that
``plugin_platform.catalog_sync.CatalogSynchronizer`` consumes: it speaks only to
the configured canonical catalog URL, bounds connect, read, and total duration,
caps the body while streaming it, and refuses redirects outright. Every failure
is reported as a stable ``plugin_catalog_*`` cause; raw transport exception text
never crosses this boundary.
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit

import httpx2

from mcp_platform.transports.streamable_http import build_bounded_sync_http_client
from plugin_platform.catalog_sync import (
    MAX_CATALOG_PAYLOAD_BYTES,
    CatalogFetch,
    CatalogFetchError,
    CatalogFetchResponse,
)

MARKETPLACE_CATALOG_URL = "https://plugins.omega2ai.com/catalog/v2/catalog.json"

DEFAULT_CONNECT_SECONDS = 5.0
DEFAULT_READ_SECONDS = 15.0
DEFAULT_TOTAL_SECONDS = 30.0

_CONFIG_INVALID = "plugin_catalog_fetch_config_invalid"
_URL_REJECTED = "plugin_catalog_url_rejected"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_IDENTITY_ENCODINGS = frozenset({"", "identity"})


def _admitted_url(url: str) -> str:
    """Admit only the canonical catalog URL, or a loopback conformance endpoint.

    Overriding the URL is confined to loopback so no configuration, environment,
    or caller can repoint the marketplace at an attacker-chosen origin; a caller
    able to reach a loopback listener already runs code on this machine.
    """
    if type(url) is not str:
        raise ValueError(_URL_REJECTED)
    if url == MARKETPLACE_CATALOG_URL:
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


def build_marketplace_catalog_fetch(
    *,
    url: str = MARKETPLACE_CATALOG_URL,
    connect_seconds: float = DEFAULT_CONNECT_SECONDS,
    read_seconds: float = DEFAULT_READ_SECONDS,
    total_seconds: float = DEFAULT_TOTAL_SECONDS,
    max_bytes: int = MAX_CATALOG_PAYLOAD_BYTES,
) -> CatalogFetch:
    """Return one bounded synchronous fetch callable for the canonical catalog."""
    if (
        type(connect_seconds) is not float
        or connect_seconds <= 0
        or type(read_seconds) is not float
        or read_seconds <= 0
        or type(total_seconds) is not float
        or total_seconds <= 0
        or type(max_bytes) is not int
        or max_bytes <= 0
        or max_bytes > MAX_CATALOG_PAYLOAD_BYTES
    ):
        raise ValueError(_CONFIG_INVALID)
    endpoint = _admitted_url(url)

    def fetch() -> CatalogFetchResponse:
        deadline = time.monotonic() + total_seconds
        client = build_bounded_sync_http_client(
            transport=None,
            connection_seconds=connect_seconds,
            operation_seconds=read_seconds,
        )
        try:
            with client.stream("GET", endpoint) as response:
                if response.is_redirect:
                    raise CatalogFetchError("plugin_catalog_redirect_rejected")
                if response.status_code != 200:
                    raise CatalogFetchError("plugin_catalog_rejected")
                encoding = response.headers.get("Content-Encoding", "")
                if encoding.strip().casefold() not in _IDENTITY_ENCODINGS:
                    raise CatalogFetchError("plugin_catalog_encoding_unsupported")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    if time.monotonic() > deadline:
                        raise CatalogFetchError("plugin_catalog_timeout")
                    total += len(chunk)
                    if total > max_bytes:
                        raise CatalogFetchError("plugin_catalog_too_large")
                    chunks.append(bytes(chunk))
        except CatalogFetchError:
            raise
        except httpx2.TimeoutException:
            raise CatalogFetchError("plugin_catalog_timeout") from None
        except (httpx2.HTTPError, OSError, ValueError):
            raise CatalogFetchError("plugin_catalog_unreachable") from None
        finally:
            client.close()
        try:
            return CatalogFetchResponse(payload=b"".join(chunks))
        except ValueError:
            raise CatalogFetchError("plugin_catalog_too_large") from None

    return fetch

"""Bounded public-SDK support for current MCP caches and extensions."""

from __future__ import annotations

import contextvars
import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from urllib.parse import urlsplit

from mcp.client.caching import CacheConfig, CacheEntry, CacheKey, InMemoryResponseCacheStore
from mcp.client.subscriptions import (
    PromptsListChanged,
    ResourceUpdated,
    ResourcesListChanged,
    ToolsListChanged,
)

from mcp_platform.canonical import canonical_bytes, sha256_hex

MCP_APPS_EXTENSION_IDENTIFIER = "io.modelcontextprotocol/apps"
MCP_APPS_SIGNED_VERSION = "1.0.0"
_REVISION = re.compile(r"[0-9a-f]{64}\Z")
_APP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+:/-]{0,255}\Z")
_MAX_EVENT_URI_CHARS = 4_096
_MAX_METADATA_DEPTH = 8
_MAX_METADATA_ITEMS = 128
_MAX_METADATA_STRING_CHARS = 4_096
_EXECUTABLE_KEYS = frozenset(
    {
        "callback",
        "code",
        "execute",
        "handler",
        "html",
        "message",
        "onmessage",
        "render",
        "script",
        "srcdoc",
    }
)
_URI_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*\Z")
_URI_HEX = frozenset("0123456789ABCDEF")
_URI_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _positive_int(value: object, *, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _revision(value: object, *, field: str) -> str:
    if type(value) is not str or _REVISION.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 revision")
    return value


class BoundedResponseCacheStore:
    """Public cache-store adapter that caps both LRU entries and server TTLs."""

    def __init__(
        self,
        *,
        max_entries: int,
        max_ttl_seconds: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._max_ttl_seconds = _positive_int(
            max_ttl_seconds,
            field="max_ttl_seconds",
        )
        self._clock = clock
        self._max_entries = _positive_int(max_entries, field="max_entries")
        self._store = InMemoryResponseCacheStore(max_entries=self._max_entries)
        self._epoch = 0
        self._key_generations: dict[CacheKey, int] = {}
        self._captures: contextvars.ContextVar[dict[CacheKey, tuple[int, int]]] = (
            contextvars.ContextVar(
                f"omega-response-cache-captures-{id(self):x}",
                default={},
            )
        )

    def _generation(self, key: CacheKey) -> tuple[int, int]:
        return self._epoch, self._key_generations.get(key, 0)

    def _capture(self, key: CacheKey) -> None:
        captures = dict(self._captures.get())
        if key not in captures and len(captures) >= self._max_entries:
            del captures[next(iter(captures))]
        captures.setdefault(key, self._generation(key))
        self._captures.set(captures)

    def _release_logical_capture(self, key: CacheKey) -> None:
        captures = {
            candidate: generation
            for candidate, generation in self._captures.get().items()
            if (candidate.method, candidate.params_key) != (key.method, key.params_key)
        }
        self._captures.set(captures)

    async def get(self, key: CacheKey) -> CacheEntry | None:
        if type(key) is not CacheKey:
            raise TypeError("cache key must use the public SDK cache type")
        self._capture(key)
        entry = await self._store.get(key)
        if entry is not None:
            self._release_logical_capture(key)
        return entry

    async def set(self, key: CacheKey, entry: CacheEntry) -> None:
        if type(key) is not CacheKey or type(entry) is not CacheEntry:
            raise TypeError("cache key and entry must use public SDK cache types")
        captured = self._captures.get().get(key)
        if captured is not None and captured != self._generation(key):
            self._release_logical_capture(key)
            return
        expires_at = entry.expires_at
        if expires_at is None:
            await self._store.delete(key)
            self._release_logical_capture(key)
            return
        now = self._clock() if self._clock is not None else time.time()
        expires_at = min(expires_at, now + self._max_ttl_seconds)
        await self._store.set(
            key,
            CacheEntry(
                value=entry.value,
                scope=entry.scope,
                expires_at=expires_at,
            ),
        )
        self._release_logical_capture(key)

    async def delete(self, key: CacheKey) -> None:
        await self._store.delete(key)

    async def invalidate(self, key: CacheKey) -> None:
        """Advance one exact key generation before removing its cached value."""

        if type(key) is not CacheKey:
            raise TypeError("cache key must use the public SDK cache type")
        if key not in self._key_generations and len(self._key_generations) >= self._max_entries:
            self._epoch += 1
            self._key_generations.clear()
        self._key_generations[key] = self._key_generations.get(key, 0) + 1
        await self._store.delete(key)

    async def clear(self) -> None:
        self._epoch += 1
        self._key_generations.clear()
        await self._store.clear()


def build_cache_config(
    *,
    app_id: str,
    connection_revision: str,
    authorization_context_revision: str,
    max_entries: int,
    max_ttl_seconds: int,
    clock: Callable[[], float] | None = None,
) -> CacheConfig:
    """Build one private, non-secret cache partition for a bound connection."""

    if type(app_id) is not str or _APP_ID.fullmatch(app_id) is None:
        raise ValueError("app_id is invalid")
    connection_revision = _revision(connection_revision, field="connection_revision")
    authorization_context_revision = _revision(
        authorization_context_revision,
        field="authorization_context_revision",
    )
    partition = sha256_hex(
        canonical_bytes(
            {
                "app_id": app_id,
                "authorization_context_revision": authorization_context_revision,
                "connection_revision": connection_revision,
            }
        )
    )
    selected_clock = time.time if clock is None else clock
    store = BoundedResponseCacheStore(
        max_entries=max_entries,
        max_ttl_seconds=max_ttl_seconds,
        clock=selected_clock,
    )
    return CacheConfig(
        store=store,
        partition=partition,
        target_id=connection_revision,
        default_ttl_ms=0,
        clock=selected_clock,
        share_public=False,
    )


def normalize_subscription_event(event: object) -> dict[str, object]:
    """Reduce public SDK subscription events to Omega's closed data vocabulary."""

    if type(event) is ToolsListChanged:
        return {"kind": "tools_list_changed"}
    if type(event) is PromptsListChanged:
        return {"kind": "prompts_list_changed"}
    if type(event) is ResourcesListChanged:
        return {"kind": "resources_list_changed"}
    if type(event) is ResourceUpdated:
        uri = event.uri
        if not _canonical_event_uri(uri):
            raise ValueError("subscription event resource URI is invalid")
        return {"kind": "resource_updated", "uri": uri}
    raise ValueError("subscription event type is invalid")


def _canonical_event_uri(value: object) -> bool:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_EVENT_URI_CHARS
        or "\\" in value
        or any(character.isspace() or ord(character) == 0x7F for character in value)
    ):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if (
        _URI_SCHEME.fullmatch(parsed.scheme) is None
        or parsed.scheme != parsed.scheme.lower()
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or parsed.geturl() != value
    ):
        return False
    remainder = value.partition(":")[2]
    if remainder.startswith("//") and not parsed.netloc:
        return False
    if parsed.netloc:
        try:
            port = parsed.port
        except ValueError:
            return False
        host = parsed.hostname
        if host is None or host != host.lower():
            return False
        canonical_host = f"[{host}]" if parsed.netloc.startswith("[") else host
        expected = canonical_host if port is None else f"{canonical_host}:{port}"
        if parsed.netloc != expected:
            return False
        if (parsed.scheme, port) in {("http", 80), ("https", 443)}:
            return False
    if any(segment in {".", ".."} for segment in parsed.path.split("/")):
        return False
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if index + 2 >= len(value) or any(
            character not in _URI_HEX for character in value[index + 1 : index + 3]
        ):
            return False
        decoded = chr(int(value[index + 1 : index + 3], 16))
        if decoded in _URI_UNRESERVED or ord(decoded) < 0x20 or ord(decoded) == 0x7F:
            return False
        index += 3
    return True


def _metadata_value(value: object, *, key: str | None, depth: int) -> object:
    if depth > _MAX_METADATA_DEPTH:
        raise ValueError("MCP Apps metadata exceeds the nesting limit")
    if isinstance(value, Mapping):
        if len(value) > _MAX_METADATA_ITEMS:
            raise ValueError("MCP Apps metadata exceeds the item limit")
        parsed: dict[str, object] = {}
        for raw_key, item in value.items():
            if type(raw_key) is not str or not raw_key or len(raw_key) > 128:
                raise ValueError("MCP Apps metadata key is invalid")
            folded = raw_key.casefold().replace("_", "").replace("-", "")
            if folded in _EXECUTABLE_KEYS or folded.startswith("on"):
                raise ValueError("MCP Apps metadata contains an executable field")
            parsed[raw_key] = _metadata_value(item, key=raw_key, depth=depth + 1)
        return parsed
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        if len(value) > _MAX_METADATA_ITEMS:
            raise ValueError("MCP Apps metadata exceeds the item limit")
        return [_metadata_value(item, key=key, depth=depth + 1) for item in value]
    if type(value) is str:
        if len(value) > _MAX_METADATA_STRING_CHARS:
            raise ValueError("MCP Apps metadata string exceeds the limit")
        if key is not None and key.casefold().replace("_", "").endswith("uri"):
            parsed_uri = urlsplit(value)
            if parsed_uri.scheme not in {"https", "ui"} or parsed_uri.username is not None:
                raise ValueError("MCP Apps metadata URI is not inert")
        return value
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError("MCP Apps metadata contains a non-JSON value")


def _preflight_metadata(value: object, *, max_bytes: int) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    total_items = 0
    while stack:
        current, depth = stack.pop()
        if depth > _MAX_METADATA_DEPTH:
            raise ValueError("MCP Apps metadata exceeds the nesting limit")
        if isinstance(current, Mapping):
            if len(current) > _MAX_METADATA_ITEMS:
                raise ValueError("MCP Apps metadata exceeds the item limit")
            total_items += len(current)
            if total_items > _MAX_METADATA_ITEMS:
                raise ValueError("MCP Apps metadata exceeds the aggregate item limit")
            for key, item in current.items():
                if type(key) is not str or not key or len(key) > 128:
                    raise ValueError("MCP Apps metadata key is invalid")
                stack.append((item, depth + 1))
        elif isinstance(current, Sequence) and not isinstance(current, str | bytes | bytearray):
            if len(current) > _MAX_METADATA_ITEMS:
                raise ValueError("MCP Apps metadata exceeds the item limit")
            total_items += len(current)
            if total_items > _MAX_METADATA_ITEMS:
                raise ValueError("MCP Apps metadata exceeds the aggregate item limit")
            stack.extend((item, depth + 1) for item in current)
        elif type(current) is str:
            if len(current) > _MAX_METADATA_STRING_CHARS:
                raise ValueError("MCP Apps metadata string exceeds the limit")
        elif current is None or type(current) in {bool, int}:
            continue
        elif type(current) is float and math.isfinite(current):
            continue
        else:
            raise ValueError("MCP Apps metadata contains a non-JSON value")
    try:
        if len(canonical_bytes(value)) > max_bytes:
            raise ValueError("MCP Apps metadata exceeds the byte limit")
    except (TypeError, UnicodeError, ValueError) as error:
        if isinstance(error, ValueError) and str(error).startswith("MCP Apps metadata"):
            raise
        raise ValueError("MCP Apps metadata contains a non-JSON value") from None


def validate_mcp_apps_metadata(
    metadata: Mapping[str, object],
    *,
    signed_metadata: Mapping[str, object],
    declared_extensions: Mapping[str, str],
    max_bytes: int,
) -> dict[str, object]:
    """Validate bounded signed MCP Apps metadata without interpreting its content."""

    max_bytes = _positive_int(max_bytes, field="max_bytes")
    if (
        not isinstance(declared_extensions, Mapping)
        or type(declared_extensions.get(MCP_APPS_EXTENSION_IDENTIFIER)) is not str
        or not declared_extensions[MCP_APPS_EXTENSION_IDENTIFIER]
    ):
        raise ValueError("MCP Apps metadata is not declared")
    if not isinstance(metadata, Mapping) or not isinstance(signed_metadata, Mapping):
        raise ValueError("MCP Apps metadata must be mappings")
    _preflight_metadata(metadata, max_bytes=max_bytes)
    _preflight_metadata(signed_metadata, max_bytes=max_bytes)
    parsed = _metadata_value(metadata, key=None, depth=0)
    signed = _metadata_value(signed_metadata, key=None, depth=0)
    parsed_bytes = canonical_bytes(parsed)
    if parsed_bytes != canonical_bytes(signed):
        raise ValueError("MCP Apps metadata does not match its signed declaration")
    return json.loads(parsed_bytes)


__all__ = [
    "MCP_APPS_EXTENSION_IDENTIFIER",
    "BoundedResponseCacheStore",
    "build_cache_config",
    "normalize_subscription_event",
    "validate_mcp_apps_metadata",
]

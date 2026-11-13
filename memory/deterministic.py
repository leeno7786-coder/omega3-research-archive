"""Deterministic helpers for memory read/write materialization."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from memory.taxonomy.normalize import canonical_json

_STABLE_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)
_STABLE_WINDOW_SECONDS = 100 * 365 * 24 * 60 * 60


def stable_hash(*parts: Any, length: int = 16) -> str:
    """Return a stable hex digest for semantically identical inputs."""
    payload = canonical_json(_json_safe(list(parts)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[: max(1, int(length))]


def stable_iso(*parts: Any) -> str:
    """Return an ISO timestamp derived from content, not wall-clock time."""
    return _stable_datetime(*parts).isoformat()


def stable_timestamp(*parts: Any) -> float:
    """Return a deterministic UNIX timestamp float for binary encodings."""
    return _stable_datetime(*parts).timestamp()


def legacy_content_time(*parts: Any) -> str:
    """Reproduce a retired content-time signature for quarantine detection only."""
    return stable_iso(*parts)


def _stable_datetime(*parts: Any) -> datetime:
    digest = stable_hash("stable-time", *parts, length=24)
    seconds = int(digest[:12], 16) % _STABLE_WINDOW_SECONDS
    micros = int(digest[12:20], 16) % 1_000_000
    return _STABLE_EPOCH + timedelta(seconds=seconds, microseconds=micros)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(value[key])
            for key in sorted(value.keys(), key=lambda item: str(item))
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        safe_items = [_json_safe(item) for item in value]
        return sorted(safe_items, key=canonical_json)
    if isinstance(value, Path):
        return value.as_posix()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)

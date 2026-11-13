"""Sortable temporal identity for canonical filesystem memories."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def temporal_epoch_us(timestamp: str) -> int:
    """Return exact UTC microseconds since epoch for an ISO-8601 timestamp."""
    text = str(timestamp or "").strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delta = parsed.astimezone(UTC) - _EPOCH
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def build_temporal_hash(timestamp: str, memory_id: str) -> str:
    """Build a stable hash whose prefix preserves chronological ordering."""
    epoch_us = temporal_epoch_us(timestamp)
    identity = f"{timestamp}|{memory_id}".encode()
    digest = hashlib.sha256(identity).hexdigest()[:16]
    return f"{epoch_us:016x}-{digest}"

"""Authority rules for timestamps stored on canonical memory records.

Legacy Omega builds derived ``provenance.ingested_at`` from record content.
Those values are deterministic identifiers, not observations of time.  This
module recognizes that exact legacy signature and prevents it from affecting
recency, while preserving the original bytes for audit and migration.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from memory.deterministic import legacy_content_time

INVALID_CONTENT_HASH = "invalid_content_hash"
OBSERVED = "observed"
PROVIDED = "provided"


def is_legacy_content_hash_timestamp(record: Mapping[str, Any]) -> bool:
    """Return whether ``ingested_at`` matches the old content-hash formula.

    Detection is deliberately exact.  A merely old or future timestamp is not
    evidence of fabrication and remains untouched.
    """
    provenance = _mapping(record.get("provenance"))
    actual = str(provenance.get("ingested_at") or "").strip()
    memory_id = str(record.get("id") or "").strip()
    author = str(provenance.get("author") or "").strip()
    text = str(record.get("text") or "")
    coord = record.get("coord")
    if not actual or not memory_id or not author or not isinstance(coord, list):
        return False
    expected = legacy_content_time(
        "memory.core.ingested_at",
        text,
        list(coord),
        author,
        memory_id,
    )
    return actual == expected


def authoritative_event_timestamp(record: Mapping[str, Any]) -> str | None:
    """Return the best provenance-bearing event timestamp, or ``None``.

    Source/session observation time is closer to the event than canonical
    intake time, so it wins when present.  A timezone is required: a naive
    calendar value cannot establish a globally ordered event.
    """
    provenance = _mapping(record.get("provenance"))
    workspace_source = _mapping(record.get("workspace_source"))
    for candidate in (
        provenance.get("source_session_timestamp"),
        workspace_source.get("observed_at"),
    ):
        parsed = _parse_timestamp(candidate)
        if parsed is not None:
            return str(candidate).strip()

    if provenance.get("ingested_at_authority") == INVALID_CONTENT_HASH:
        return None
    if is_legacy_content_hash_timestamp(record):
        return None
    ingested_at = provenance.get("ingested_at")
    if _parse_timestamp(ingested_at) is None:
        return None
    return str(ingested_at).strip()


def authoritative_timestamp_epoch(record: Mapping[str, Any]) -> float:
    """Return authoritative event time as an epoch, or the neutral value 0."""
    return timestamp_epoch_or_zero(authoritative_event_timestamp(record))


def timestamp_epoch_or_zero(value: Any) -> float:
    """Parse a timezone-aware timestamp into a finite epoch, else return 0."""
    parsed = _parse_timestamp(value)
    if parsed is None:
        return 0.0
    try:
        epoch = parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return 0.0
    return epoch if math.isfinite(epoch) else 0.0


def invalidate_legacy_content_hash_timestamp(
    record: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Copy *record* and mark an exact legacy timestamp non-authoritative.

    No replacement time is minted.  The contaminated value stays available to
    auditors, while every authority-aware consumer treats it as unknown.
    """
    migrated = copy.deepcopy(dict(record))
    provenance = migrated.get("provenance")
    if not isinstance(provenance, dict) or not is_legacy_content_hash_timestamp(migrated):
        return migrated, False
    if provenance.get("ingested_at_authority") == INVALID_CONTENT_HASH:
        return migrated, False

    legacy_value = str(provenance.get("ingested_at") or "")
    provenance["ingested_at_authority"] = INVALID_CONTENT_HASH
    audit = migrated.get("audit")
    if isinstance(audit, list):
        for entry in audit:
            if isinstance(entry, dict) and str(entry.get("when") or "") == legacy_value:
                entry["when_authority"] = INVALID_CONTENT_HASH
    return migrated, True


def _parse_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}

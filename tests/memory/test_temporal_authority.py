from __future__ import annotations

from datetime import datetime

from memory.deterministic import stable_iso
from memory.core import build_memory_dict
from memory.temporal_authority import (
    INVALID_CONTENT_HASH,
    OBSERVED,
    PROVIDED,
    authoritative_event_timestamp,
    authoritative_timestamp_epoch,
    invalidate_legacy_content_hash_timestamp,
    is_legacy_content_hash_timestamp,
)


def _record(*, ingested_at: str, source_session_timestamp: str = "") -> dict:
    provenance = {
        "author": "operator_chat",
        "ingested_at": ingested_at,
    }
    if source_session_timestamp:
        provenance["source_session_timestamp"] = source_session_timestamp
    return {
        "id": "2.01.abc12345",
        "coord": [2, 1, 0],
        "text": "Observed fact",
        "provenance": provenance,
        "audit": [
            {"when": ingested_at, "what": "ingested", "by": "operator_chat"}
        ],
    }


def _legacy_record() -> dict:
    timestamp = stable_iso(
        "memory.core.ingested_at",
        "Observed fact",
        [2, 1, 0],
        "operator_chat",
        "2.01.abc12345",
    )
    return _record(ingested_at=timestamp)


def test_source_session_time_is_preferred_when_it_is_observed() -> None:
    record = _record(
        ingested_at="2026-08-26T14:00:00+00:00",
        source_session_timestamp="2025-05-06T07:08:09+00:00",
    )

    assert authoritative_event_timestamp(record) == "2025-05-06T07:08:09+00:00"
    assert authoritative_timestamp_epoch(record) == datetime.fromisoformat(
        "2025-05-06T07:08:09+00:00"
    ).timestamp()


def test_observed_ingest_time_is_authoritative_without_source_time() -> None:
    record = _record(ingested_at="2026-08-26T14:00:00+00:00")

    assert authoritative_event_timestamp(record) == "2026-08-26T14:00:00+00:00"


def test_exact_legacy_content_hash_time_is_never_authoritative() -> None:
    record = _legacy_record()

    assert is_legacy_content_hash_timestamp(record) is True
    assert authoritative_event_timestamp(record) is None
    assert authoritative_timestamp_epoch(record) == 0.0


def test_migration_invalidates_without_replacing_the_legacy_value() -> None:
    record = _legacy_record()
    legacy_value = record["provenance"]["ingested_at"]

    migrated, changed = invalidate_legacy_content_hash_timestamp(record)

    assert changed is True
    assert migrated["provenance"]["ingested_at"] == legacy_value
    assert migrated["provenance"]["ingested_at_authority"] == INVALID_CONTENT_HASH
    assert migrated["audit"][0]["when"] == legacy_value
    assert migrated["audit"][0]["when_authority"] == INVALID_CONTENT_HASH
    assert authoritative_event_timestamp(migrated) is None
    assert record["provenance"].get("ingested_at_authority") is None


def test_invalid_or_timezone_naive_values_are_unknown() -> None:
    assert authoritative_event_timestamp(_record(ingested_at="not-a-time")) is None
    assert authoritative_event_timestamp(_record(ingested_at="2026-08-26T14:00:00")) is None


def test_new_memory_stamps_observed_ingest_authority(monkeypatch) -> None:
    monkeypatch.setattr("memory.core.now_iso", lambda: "2026-08-26T14:00:00+00:00")

    record = build_memory_dict("new fact", [2, 1, 0], "operator_chat")

    assert record["provenance"]["ingested_at_authority"] == OBSERVED


def test_explicit_ingest_time_stamps_provided_authority() -> None:
    record = build_memory_dict(
        "imported fact",
        [2, 1, 0],
        "operator_chat",
        metadata={"ingested_at": "2025-05-06T07:08:09+00:00"},
    )

    assert record["provenance"]["ingested_at_authority"] == PROVIDED

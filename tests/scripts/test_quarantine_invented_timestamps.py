from __future__ import annotations

import json

from memory.deterministic import stable_iso
from scripts.quarantine_invented_timestamps import (
    LEDGER_SCHEMA_VERSION,
    migrate_invented_timestamps,
)


def _record(*, timestamp: str, text: str = "historical fact") -> dict:
    return {
        "id": "2.01.abc12345",
        "coord": [2, 1, 0],
        "type": "concept",
        "title": "Historical fact",
        "summary": "Historical fact",
        "text": text,
        "provenance": {
            "origin_id": "operator_chat",
            "origin_type": "ingest",
            "author": "operator_chat",
            "ingested_at": timestamp,
            "confidence": 0.8,
        },
        "flags": {"canonical": True, "private": False},
        "audit": [
            {"when": timestamp, "what": "ingested", "by": "operator_chat"}
        ],
    }


def _legacy_record() -> dict:
    text = "historical fact"
    timestamp = stable_iso(
        "memory.core.ingested_at",
        text,
        [2, 1, 0],
        "operator_chat",
        "2.01.abc12345",
    )
    return _record(timestamp=timestamp, text=text)


def _write_record(root, record: dict):
    path = root / "memory" / "200_knowledge" / "history" / "record.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_dry_run_reports_exact_signature_without_writing(tmp_path) -> None:
    path = _write_record(tmp_path, _legacy_record())
    before = path.read_bytes()

    report = migrate_invented_timestamps(root=tmp_path, apply=False)

    assert report["matched_records"] == 1
    assert report["migrated_records"] == 0
    assert report["status"] == "dry_run"
    assert path.read_bytes() == before
    assert not (tmp_path / "memory" / "indexes" / "temporal_authority_migration.json").exists()

def test_apply_invalidates_rebuilds_and_writes_content_free_ledger(tmp_path) -> None:
    record = _legacy_record()
    legacy_timestamp = record["provenance"]["ingested_at"]
    path = _write_record(tmp_path, record)
    rebuilds = []

    report = migrate_invented_timestamps(
        root=tmp_path,
        apply=True,
        rebuild=lambda root: rebuilds.append(root),
        completed_at="2026-08-26T18:00:00+00:00",
    )

    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert report["status"] == "complete"
    assert report["migrated_records"] == 1
    assert rebuilds == [tmp_path]
    assert migrated["provenance"]["ingested_at"] == legacy_timestamp
    assert migrated["provenance"]["ingested_at_authority"] == "invalid_content_hash"
    assert migrated["audit"][0]["when_authority"] == "invalid_content_hash"

    ledger_path = tmp_path / "memory" / "indexes" / "temporal_authority_migration.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    encoded = json.dumps(ledger, sort_keys=True)
    assert ledger["schema_version"] == LEDGER_SCHEMA_VERSION
    assert ledger["record_count"] == 1
    assert "historical fact" not in encoded
    assert legacy_timestamp not in encoded
    assert "text" not in ledger["records"][0]


def test_apply_is_idempotent_and_preserves_legitimate_timestamp(tmp_path) -> None:
    path = _write_record(tmp_path, _record(timestamp="2026-08-26T12:00:00+00:00"))
    before = path.read_bytes()
    rebuilds = []

    first = migrate_invented_timestamps(
        root=tmp_path,
        apply=True,
        rebuild=lambda root: rebuilds.append(root),
    )
    second = migrate_invented_timestamps(
        root=tmp_path,
        apply=True,
        rebuild=lambda root: rebuilds.append(root),
    )

    assert first["matched_records"] == 0
    assert second["matched_records"] == 0
    assert path.read_bytes() == before
    assert rebuilds == []


def test_apply_rolls_back_canonical_records_when_rebuild_fails(tmp_path) -> None:
    path = _write_record(tmp_path, _legacy_record())
    before = path.read_bytes()

    def fail_rebuild(_root):
        raise RuntimeError("projection failed")

    try:
        migrate_invented_timestamps(root=tmp_path, apply=True, rebuild=fail_rebuild)
    except RuntimeError as exc:
        assert str(exc) == "projection failed"
    else:
        raise AssertionError("migration must propagate projection failure")

    assert path.read_bytes() == before
    assert not (tmp_path / "memory" / "indexes" / "temporal_authority_migration.json").exists()

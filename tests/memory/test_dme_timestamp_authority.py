from __future__ import annotations

import json
from datetime import datetime

from memory.deterministic import stable_iso
from memory.dme.adapter import (
    DME_SCHEMA_VERSION,
    dme_meta_path,
    dme_sram_path,
    load_sram,
    rebuild_sram_from_graph,
)
from memory.dme.packer import unpack_timestamp


def _write_indexed_record(tmp_path, record: dict) -> None:
    memory_root = tmp_path / "memory"
    memory_root.mkdir(exist_ok=True)
    path = memory_root / f"{record['id']}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    index_path = memory_root / "indexes" / "coord_index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(
            {
                record["id"]: {
                    "id": record["id"],
                    "coord": record["coord"],
                    "path": f"memory/{path.name}",
                }
            }
        ),
        encoding="utf-8",
    )


def _record(timestamp: str, *, source_timestamp: bool = False) -> dict:
    provenance = {"author": "operator_chat", "ingested_at": timestamp}
    if source_timestamp:
        provenance["source_session_timestamp"] = timestamp
    return {
        "id": "2.01.abc12345",
        "coord": [2, 0, 1],
        "type": "concept",
        "title": "Observed fact",
        "summary": "Observed fact",
        "text": "Observed fact",
        "status": "active",
        "provenance": provenance,
    }


def test_dme_packs_observed_source_timestamp(tmp_path) -> None:
    timestamp = "2025-05-06T07:08:09+00:00"
    _write_indexed_record(tmp_path, _record(timestamp, source_timestamp=True))

    index = rebuild_sram_from_graph(root=tmp_path, snapshot_hash="snapshot")

    assert unpack_timestamp(index.sram[0]) == datetime.fromisoformat(timestamp).timestamp()


def test_dme_packs_zero_when_time_is_unknown(tmp_path) -> None:
    record = _record("")
    _write_indexed_record(tmp_path, record)

    index = rebuild_sram_from_graph(root=tmp_path, snapshot_hash="snapshot")

    assert unpack_timestamp(index.sram[0]) == 0.0


def test_dme_rejects_exact_legacy_content_hash_timestamp(tmp_path) -> None:
    timestamp = stable_iso(
        "memory.core.ingested_at",
        "Observed fact",
        [2, 0, 1],
        "operator_chat",
        "2.01.abc12345",
    )
    _write_indexed_record(tmp_path, _record(timestamp))

    index = rebuild_sram_from_graph(root=tmp_path, snapshot_hash="snapshot")

    assert unpack_timestamp(index.sram[0]) == 0.0


def test_dme_v6_cache_is_rebuilt_for_temporal_authority_semantics(tmp_path) -> None:
    meta_path = dme_meta_path(tmp_path)
    meta_path.parent.mkdir(parents=True)
    meta_path.write_text(
        json.dumps(
            {
                "schema_version": "omega.dme_sram.v6",
                "snapshot_hash": "uncompiled",
                "seat_count": 0,
            }
        ),
        encoding="utf-8",
    )
    dme_sram_path(tmp_path).write_bytes(b"")

    index = load_sram(root=tmp_path)

    assert DME_SCHEMA_VERSION == "omega.dme_sram.v7"
    assert index.schema_version == DME_SCHEMA_VERSION
    assert json.loads(meta_path.read_text(encoding="utf-8"))["schema_version"] == DME_SCHEMA_VERSION

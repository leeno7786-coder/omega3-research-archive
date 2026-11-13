from __future__ import annotations

import json
from pathlib import Path

import pytest

from memory.source_ledger.store import record_source


def _record(tmp_path: Path) -> dict[str, object]:
    text = "The operator's full name is Noah Lee."
    artifact = record_source(
        text,
        source_type="operator_onboarding",
        origin_id="onboarding-noah",
        root=tmp_path,
    )
    return {
        "id": "1.00.operator_name",
        "coord": [1, 0, 100],
        "type": "persona",
        "title": "Operator name",
        "summary": text,
        "text": text,
        "tags": ["operator", "noah"],
        "domain": "operator",
        "subtopic": "identity",
        "status": "active",
        "permissions": {"read": ["cognition"]},
        "contradiction_state": "none",
        "flags": {"canonical": True, "private": True},
        "enrichment": {"entities": ["Noah Lee"]},
        "provenance": {
            **artifact.citation(),
            "confidence": 0.9,
            "projection_receipt_required": True,
        },
        "embeddings": {
            "engine": "local",
            "model": "harrier-1024",
            "dim": 2,
            "vector_schema_version": 1,
            "text_768": [0.25, 0.75],
        },
    }


def test_projection_receipt_binds_authority_fields_and_embedding_metadata(
    tmp_path: Path,
) -> None:
    from memory.projection_receipt import (
        verify_projection_receipt,
        write_projection_receipt,
    )

    record = _record(tmp_path)
    write_projection_receipt(record, root=tmp_path)
    assert verify_projection_receipt(record, root=tmp_path).ok

    for field, replacement in (
        ("status", "quarantined"),
        ("permissions", {"read": ["external"]}),
        ("contradiction_state", "conflict"),
        ("flags", {"canonical": False, "private": False}),
        ("enrichment", {"entities": ["Someone else"]}),
    ):
        changed = {**record, field: replacement}
        verdict = verify_projection_receipt(changed, root=tmp_path)
        assert verdict.ok is False
        assert verdict.reason == "canonical_claim_hash_mismatch"

    changed_embedding = {
        **record,
        "embeddings": {**record["embeddings"], "model": "unexpected-model"},
    }
    verdict = verify_projection_receipt(changed_embedding, root=tmp_path)
    assert verdict.ok is False
    assert verdict.reason == "embedding_projection_mismatch"


def test_projection_receipt_is_idempotent_but_claim_changes_require_supersession(
    tmp_path: Path,
) -> None:
    from memory.projection_receipt import (
        ProjectionReceiptConflictError,
        receipt_path,
        write_projection_receipt,
    )

    record = _record(tmp_path)
    first = write_projection_receipt(record, root=tmp_path)
    before = receipt_path(str(record["id"]), root=tmp_path).read_bytes()
    again = write_projection_receipt(record, root=tmp_path)
    assert again == first
    assert receipt_path(str(record["id"]), root=tmp_path).read_bytes() == before

    changed = {**record, "text": "The operator is somebody else."}
    with pytest.raises(ProjectionReceiptConflictError, match="supersession"):
        write_projection_receipt(changed, root=tmp_path)


def test_pending_embedding_receipt_can_only_finalize_once(tmp_path: Path) -> None:
    from memory.projection_receipt import (
        ProjectionReceiptConflictError,
        verify_projection_receipt,
        write_projection_receipt,
    )

    pending = {**_record(tmp_path), "embeddings": {}}
    write_projection_receipt(pending, root=tmp_path)
    final = {**pending, "embeddings": dict(_record(tmp_path)["embeddings"])}
    write_projection_receipt(final, root=tmp_path)
    assert verify_projection_receipt(final, root=tmp_path).ok

    changed = {
        **final,
        "embeddings": {**final["embeddings"], "model": "second-model"},
    }
    with pytest.raises(ProjectionReceiptConflictError, match="embedding"):
        write_projection_receipt(changed, root=tmp_path)


def test_receipt_filename_is_contained_for_hostile_memory_id(tmp_path: Path) -> None:
    from memory.projection_receipt import receipt_path, write_projection_receipt

    record = {**_record(tmp_path), "id": "../../outside"}
    write_projection_receipt(record, root=tmp_path)
    path = receipt_path(str(record["id"]), root=tmp_path).resolve()
    path.relative_to((tmp_path / "memory" / "source_ledger" / "projection_receipts").resolve())
    assert ".." not in path.name


def test_encoding_skip_embed_writes_required_projection_receipt(
    tmp_path: Path,
) -> None:
    from memory.encoding import encode_committed_memory
    from memory.engine.validate_write import atomic_write
    from memory.projection_receipt import (
        memory_root_for_canonical_path,
        verify_projection_receipt,
    )

    record = _record(tmp_path)
    for field in ("status", "permissions", "contradiction_state"):
        record.pop(field, None)
    record["coord"] = [9, 0, 1]
    record["id"] = "9.00.receipt"
    record["embeddings"] = {}
    stamp = "2026-07-28T00:00:00+00:00"
    record["provenance"].update(
        {
            "origin_id": "projection-receipt-test",
            "origin_type": "test",
            "author": "test",
            "ingested_at": stamp,
        }
    )
    record["audit"] = [{"when": stamp, "what": "ingested", "by": "test"}]
    final_path = atomic_write(record, update_index=True)
    result = encode_committed_memory(record, final_path, skip_embed=True)

    assert result.ok, result.failure_reason
    assert "projection_receipt" in result.steps_completed
    assert record["provenance"]["projection_receipt_required"] is True
    assert verify_projection_receipt(
        record,
        root=memory_root_for_canonical_path(final_path),
    ).ok


def test_integrity_audit_reports_missing_tampered_and_orphan_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memory.integrity.audit import _projection_receipt_audit
    from memory.projection_receipt import write_projection_receipt
    import memory.engine.walker as walker

    monkeypatch.setattr(walker, "MEMORY_ROOT", tmp_path / "memory")

    def persist(record: dict[str, object], name: str) -> Path:
        path = tmp_path / "memory" / "200_knowledge" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    missing = {**_record(tmp_path), "id": "mem.missing"}
    missing_path = persist(missing, "missing")

    tampered = {**_record(tmp_path), "id": "mem.tampered"}
    tampered_path = persist(tampered, "tampered")
    write_projection_receipt(tampered, root=tmp_path)
    tampered["status"] = "quarantined"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")

    orphan = {**_record(tmp_path), "id": "mem.orphan"}
    write_projection_receipt(orphan, root=tmp_path)

    result = _projection_receipt_audit(
        {
            "mem.missing": {"path": missing_path.relative_to(tmp_path).as_posix()},
            "mem.tampered": {"path": tampered_path.relative_to(tmp_path).as_posix()},
        },
        root=tmp_path,
    )

    assert result["missing"] == 1
    assert result["claim_mismatch"] == 1
    assert result["orphans"] == ["mem.orphan"]

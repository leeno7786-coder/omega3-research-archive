from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")


@pytest.fixture
def pinned_canonical_time():
    from core.symphony_time import clear_fake_time, set_fake_time

    event_time = "2026-08-03T17:23:55+00:00"
    set_fake_time(iso=event_time)
    try:
        yield event_time
    finally:
        clear_fake_time()


def test_production_time_fields_never_call_stable_iso():
    """Content hashes are identifiers, never substitutes for observed time."""
    root = Path(__file__).resolve().parents[2]
    packages = (
        "auto_loop",
        "autonomic",
        "connectors",
        "core",
        "deepsleep_loop",
        "global_loop",
        "guardian",
        "hud",
        "knowledge_extractor",
        "memory",
        "meta_loop",
        "omega_loop",
        "pressure_loop",
        "runtime",
        "scripts",
        "sensory",
    )
    offenders: list[str] = []
    for package in packages:
        for path in (root / package).rglob("*.py"):
            if path == root / "memory" / "deterministic.py":
                continue
            if "stable_iso(" in path.read_text(encoding="utf-8"):
                offenders.append(path.relative_to(root).as_posix())

    assert not offenders, f"hash-derived fictional timestamps remain in: {offenders}"


def test_build_memory_dict_is_stable_without_caller_id(
    tmp_path, monkeypatch, pinned_canonical_time
):
    import memory.engine.validate_write as validate_write
    from memory.core import build_memory_dict

    monkeypatch.setattr(validate_write, "MEMORY_ROOT", tmp_path / "memory")

    metadata = {
        "type": "concept",
        "title": "Deterministic write path",
        "summary": "Identical writes produce identical canonical records.",
        "domain": "memory",
        "confidence": 0.91,
    }

    first = build_memory_dict(
        "Identical writes produce identical canonical records.",
        [2, 0, 7],
        "gate:test",
        metadata=metadata,
    )
    second = build_memory_dict(
        "Identical writes produce identical canonical records.",
        [2, 0, 7],
        "gate:test",
        metadata=metadata,
    )

    assert first == second
    assert first["id"].startswith("2.00.")
    assert first["provenance"]["ingested_at"] == pinned_canonical_time
    assert first["provenance"]["ingested_at"] == first["audit"][0]["when"]
    assert first["enrichment"]["intake_packet"] == second["enrichment"]["intake_packet"]


def test_build_memory_dict_stamps_deterministic_intake_packet(tmp_path, monkeypatch):
    import memory.engine.validate_write as validate_write
    from memory.core import build_memory_dict

    monkeypatch.setattr(validate_write, "MEMORY_ROOT", tmp_path / "memory")

    mem = build_memory_dict(
        "Alice likes hiking and climbing.",
        [1, 1, 4],
        "persona_router",
        enrichment={
            "entities": [{"name": "Alice", "facet": "name"}],
            "facets": {"who": ["Alice"], "what": ["likes hiking"]},
        },
        metadata={
            "type": "persona_note",
            "title": "Alice preferences",
            "summary": "Alice likes hiking and climbing.",
            "domain": "alice",
            "subtopic": "persona_note",
            "tags": ["persona", "preference"],
        },
    )

    packet = mem["enrichment"]["intake_packet"]
    provenance = mem["provenance"]
    assert packet["type"] == "memory_intake_packet"
    assert packet["schema_version"] == "omega.memory_intake_packet.v1"
    assert packet["memory_id"] == mem["id"]
    assert packet["coord"] == [1, 1, 4]
    assert packet["source_id"] == provenance["source_id"]
    assert packet["span_hash"] == provenance["span_hash"]
    assert packet["source_content_hash"] == provenance["source_content_hash"]
    assert packet["primary_lobe"] == 1
    assert packet["domain"] == "alice"
    assert packet["decomposition"]["entity_count"] == 1
    assert packet["decomposition"]["facet_keys"] == ["what", "who"]
    assert "entities" in packet["decomposition"]["keys"]


def test_build_memory_dict_carries_source_session_fields(tmp_path, monkeypatch):
    import memory.engine.validate_write as validate_write
    from memory.core import build_memory_dict

    monkeypatch.setattr(validate_write, "MEMORY_ROOT", tmp_path / "memory")

    record = build_memory_dict(
        "Session S42 said the answer is lantern.",
        [2, 0, 7],
        "longmemeval",
        metadata={
            "title": "Benchmark fact",
            "summary": "Session S42 said the answer is lantern.",
            "source_session_id": "S42",
            "source_session_timestamp": "2025-01-02T03:04:05+00:00",
        },
    )

    assert record["provenance"]["source_session_id"] == "S42"
    assert record["provenance"]["source_session_timestamp"] == "2025-01-02T03:04:05+00:00"


def test_source_ledger_artifact_bytes_are_stable_across_roots(tmp_path):
    from memory.source_ledger.store import artifact_path, record_source

    event_time = "2026-08-03T17:23:55+00:00"
    metadata = {"created_at": event_time, "source_session_timestamp": event_time}
    first = record_source(
        "Canonical source text.",
        source_type="operator_chat",
        origin_id="cycle-1",
        metadata=metadata,
        root=tmp_path / "a",
    )
    second = record_source(
        "Canonical source text.",
        source_type="operator_chat",
        origin_id="cycle-1",
        metadata=metadata,
        root=tmp_path / "b",
    )

    assert first.source_id == second.source_id
    assert (
        artifact_path(first.source_id, root=tmp_path / "a").read_bytes()
        == artifact_path(
            second.source_id,
            root=tmp_path / "b",
        ).read_bytes()
    )


def test_source_ledger_citation_carries_session_metadata(tmp_path):
    from memory.source_ledger.store import record_source

    artifact = record_source(
        "Repeated answer text.",
        source_type="longmemeval.session",
        origin_id="instance-1",
        metadata={
            "source_session_id": "session-17",
            "source_session_timestamp": "2024-06-01T12:00:00+00:00",
        },
        root=tmp_path,
    )
    different_session = record_source(
        "Repeated answer text.",
        source_type="longmemeval.session",
        origin_id="instance-1",
        metadata={
            "source_session_id": "session-18",
            "source_session_timestamp": "2024-06-02T12:00:00+00:00",
        },
        root=tmp_path,
    )

    citation = artifact.citation()
    assert citation["source_session_id"] == "session-17"
    assert citation["source_session_timestamp"] == "2024-06-01T12:00:00+00:00"
    assert different_session.source_id != artifact.source_id


def test_sense_registry_rows_are_stable_across_roots(tmp_path, pinned_canonical_time):
    from memory.sense_registry import register_sense, sense_registry_path

    kwargs = {
        "term": "agent",
        "label": "Agent",
        "coord": [2, 0, 4],
        "domain": "robotics",
        "subtopic": "planning",
        "cues": ["robot", "planner", "action"],
        "memory_id": "mem.agent",
    }
    first = register_sense(**kwargs, root=tmp_path / "a")
    second = register_sense(**kwargs, root=tmp_path / "b")

    assert first == second
    assert first.created_at == pinned_canonical_time
    assert (
        sense_registry_path(root=tmp_path / "a").read_bytes()
        == sense_registry_path(
            root=tmp_path / "b",
        ).read_bytes()
    )


def test_dme_rebuild_outputs_are_byte_stable(tmp_path, pinned_canonical_time):
    from memory.dme.adapter import (
        dme_meta_path,
        dme_row_index_path,
        dme_sram_path,
        rebuild_sram_from_graph,
    )
    from memory.dme.search import search_intersection
    from memory.taxonomy.compiler import compile_memory_graph

    memory_root = tmp_path / "memory"
    memory_root.mkdir(exist_ok=True)
    record = {
        "id": "k1",
        "coord": [2, 0, 1],
        "type": "concept",
        "title": "Neuron",
        "summary": "Neurons pass signals through synapses.",
        "domain": "biology",
        "tags": ["biology"],
        "enrichment": {"canonical": True},
    }
    (memory_root / "k1.json").write_text(json.dumps(record), encoding="utf-8")
    index_path = memory_root / "indexes" / "coord_index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps({"k1": {"coord": [2, 0, 1], "path": "memory/k1.json"}}),
        encoding="utf-8",
    )

    compile_memory_graph(root=tmp_path)
    first_sram = dme_sram_path(tmp_path).read_bytes()
    first_meta = dme_meta_path(tmp_path).read_bytes()
    first_rows = dme_row_index_path(tmp_path).read_bytes()

    rebuilt = rebuild_sram_from_graph(root=tmp_path)

    assert rebuilt.built_at == pinned_canonical_time
    assert dme_sram_path(tmp_path).read_bytes() == first_sram
    assert dme_meta_path(tmp_path).read_bytes() == first_meta
    assert dme_row_index_path(tmp_path).read_bytes() == first_rows
    result = search_intersection(
        rebuilt.sram,
        rebuilt.row_index,
        [(2, 0, 1)],
        max_results=5,
    )
    assert result.query_time_ms == 0.0

    second = rebuild_sram_from_graph(root=tmp_path)
    assert np.array_equal(second.sram, rebuilt.sram)
    assert second.to_dict() == rebuilt.to_dict()


def test_create_staging_record_is_stable_for_same_envelope(tmp_path, monkeypatch):
    import memory.engine.validate_write as validate_write
    import memory.promotion as promotion

    envelope = {
        "cycle_id": "cycle-stable",
        "timestamp_created": "2026-08-03T17:23:55+00:00",
        "coord": [2, 0, 1],
        "type": "concept",
        "source": "omega:test",
        "confidence": 0.8,
        "payload": {
            "title": "Stable staging",
            "summary": "Staging derives refs from the envelope.",
            "text": "Staging derives refs from the envelope.",
        },
    }

    monkeypatch.setattr(promotion, "STAGING_DIR", tmp_path / "a" / "guardian" / "state" / "staging")
    monkeypatch.setattr(validate_write, "MEMORY_ROOT", tmp_path / "a" / "memory")
    first_ref = promotion.create_staging(dict(envelope))
    first_bytes = (promotion.STAGING_DIR / f"{first_ref}.json").read_bytes()

    monkeypatch.setattr(promotion, "STAGING_DIR", tmp_path / "b" / "guardian" / "state" / "staging")
    monkeypatch.setattr(validate_write, "MEMORY_ROOT", tmp_path / "b" / "memory")
    second_ref = promotion.create_staging(dict(envelope))
    second_bytes = (promotion.STAGING_DIR / f"{second_ref}.json").read_bytes()

    assert second_ref == first_ref
    assert second_bytes == first_bytes


def test_atomic_write_rejects_canonical_memory_without_source_contract(tmp_path, monkeypatch):
    import memory.engine.validate_write as validate_write

    monkeypatch.setattr(validate_write, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(validate_write, "TMP_DIR", tmp_path / "memory" / "tmp_writes")
    monkeypatch.setattr(
        validate_write, "INDEX_PATH", tmp_path / "memory" / "indexes" / "coord_index.json"
    )

    record = {
        "id": "2.00.manual",
        "coord": [2, 0, 1],
        "type": "concept",
        "title": "Manual bypass",
        "summary": "This record bypasses the SourceLedger contract.",
        "text": "This record bypasses the SourceLedger contract.",
        "provenance": {
            "origin_id": "manual",
            "origin_type": "test",
            "author": "test",
            "ingested_at": "2026-07-04T00:00:00+00:00",
            "confidence": 1.0,
        },
        "flags": {"canonical": True, "private": False},
        "audit": [{"when": "2026-07-04T00:00:00+00:00", "what": "ingested", "by": "test"}],
    }

    try:
        validate_write.atomic_write(record, update_index=False)
    except ValueError as exc:
        assert "SourceLedger contract" in str(exc)
    else:
        raise AssertionError("atomic_write accepted canonical memory without source_id/span_hash")


def test_build_memory_dict_atomic_write_satisfies_source_contract(tmp_path, monkeypatch):
    import memory.engine.validate_write as validate_write
    from memory.core import build_memory_dict

    monkeypatch.setattr(validate_write, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(validate_write, "TMP_DIR", tmp_path / "memory" / "tmp_writes")
    monkeypatch.setattr(
        validate_write, "INDEX_PATH", tmp_path / "memory" / "indexes" / "coord_index.json"
    )

    record = build_memory_dict(
        "Builder-backed writes carry source proof.",
        [2, 0, 1],
        "test:builder",
        metadata={
            "title": "Builder source proof",
            "summary": "Builder-backed writes carry source proof.",
        },
    )
    path = validate_write.atomic_write(record, update_index=True)

    assert path.endswith(f"{record['id']}.json")
    assert record["provenance"]["source_id"].startswith("src_")


def test_write_memory_rejects_internal_guardian_proposals(monkeypatch):
    import memory.api as api
    from memory.api import CanonicalEnvelope, write_memory

    staged = []
    monkeypatch.setattr(
        api, "create_staging", lambda envelope: staged.append(envelope) or "stage_forbidden"
    )

    receipt = write_memory(
        CanonicalEnvelope(
            cycle_id="cycle-internal-propose",
            coord=[2, 0, 1],
            type="concept",
            payload={
                "title": "Internal proposal",
                "summary": "Internal memory writes must not route through Guardian.",
                "text": "Internal memory writes must not route through Guardian.",
            },
            source="deepsleep.identity_store",
        ),
        propose=True,
    )

    assert receipt.validation_status == "rejected"
    assert receipt.staging_ref is None
    assert staged == []
    assert "user-chat ingress" in (receipt.message or "")


def test_write_memory_allows_only_user_chat_guardian_proposals(monkeypatch):
    import guardian.decision as guardian_decision
    import memory.api as api
    from memory.api import CanonicalEnvelope, write_memory

    monkeypatch.setattr(api, "create_staging", lambda envelope: "stage_user_chat")
    monkeypatch.setattr(
        guardian_decision,
        "decide_request",
        lambda staging_ref, approver_id: SimpleNamespace(
            committed=True,
            canonical_ref="memory/000_identity/chat.json",
            kind="ALLOW",
            reason="allowed",
        ),
    )
    hooks = []
    monkeypatch.setattr(
        "memory.post_commit.run_post_commit_memory_hooks",
        lambda canonical_ref, **kwargs: hooks.append((canonical_ref, kwargs)) or {},
    )

    receipt = write_memory(
        CanonicalEnvelope(
            cycle_id="cycle-user-chat-propose",
            coord=[0, 1, 1],
            type="identity",
            payload={
                "title": "User chat fact",
                "summary": "User chat ingress may be Guardian-arbitrated.",
                "text": "User chat ingress may be Guardian-arbitrated.",
            },
            source="user_chat:hud",
        ),
        propose=True,
    )

    assert receipt.validation_status == "committed"
    assert receipt.staging_ref == "stage_user_chat"
    assert hooks and hooks[0][0] == "memory/000_identity/chat.json"


def test_promote_memory_rejects_non_user_chat_staging(tmp_path, monkeypatch):
    import memory.promotion as promotion
    from memory.api import promote_memory

    staging_dir = tmp_path / "guardian" / "state" / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(promotion, "STAGING_DIR", staging_dir)
    (staging_dir / "stage_internal.json").write_text(
        json.dumps({"envelope": {"source": "deepsleep.identity_store"}}),
        encoding="utf-8",
    )

    try:
        promote_memory("stage_internal", [], "test")
    except ValueError as exc:
        assert "user-chat ingress" in str(exc)
    else:
        raise AssertionError("promote_memory accepted non-user-chat staging")

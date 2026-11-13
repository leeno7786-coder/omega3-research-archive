"""Memory Engine v2 follow-up completion tests."""

from __future__ import annotations

import json
import time
from pathlib import Path

import memory.taxonomy.graph_store as G
import pytest

from memory.api import memory_health
from memory.integrity.audit import run_integrity_audit, run_integrity_audit_if_due
from memory.integrity.repair import repair_integrity_issues
from memory.source_ledger.store import read_source
from memory.taxonomy.canonical_capture import capture_episode_to_canonical
from pressure_loop.integrity_pressure import integrity_pressure_contributor


class _Shared:
    def __init__(self):
        self._data = {}

    def write_canonical(self, key, value):
        self._data[key] = value


def test_live_integrity_audit_uses_default_taxonomy_projection(tmp_path, monkeypatch):
    """Live audit must inspect the same default graph/DME used by recall and boot."""
    import memory.integrity.audit as audit

    projection_roots = []

    class _Rows:
        def fetchall(self):
            return []

    class _Connection:
        def execute(self, *_args, **_kwargs):
            return _Rows()

        def close(self):
            return None

    monkeypatch.setattr(audit, "_audit_root", lambda _root: tmp_path)
    monkeypatch.setattr(audit, "load_coord_index", lambda: None)
    monkeypatch.setattr(audit, "coord_index_snapshot", lambda: {})
    monkeypatch.setattr(
        audit.G,
        "connect",
        lambda root=None: projection_roots.append(("graph", root)) or _Connection(),
    )
    monkeypatch.setattr(
        audit,
        "_dme_parity_audit",
        lambda *, root: projection_roots.append(("dme", root)) or ("ok", (), ()),
    )

    report = audit.run_integrity_audit()

    assert report.status == "ok"
    assert projection_roots == [("graph", None), ("dme", None)]


@pytest.fixture
def memory_roots(tmp_path, monkeypatch):
    import core.paths as paths
    import memory.engine.validate_write as validate_write
    import memory.engine.walker as walker
    import memory.recall_verifier as recall_verifier

    mem_root = tmp_path / "memory"
    monkeypatch.setattr(validate_write, "MEMORY_ROOT", mem_root)
    monkeypatch.setattr(walker, "MEMORY_ROOT", mem_root)
    monkeypatch.setattr(walker, "INDEX_PATH", mem_root / "indexes" / "coord_index.json")
    monkeypatch.setattr(paths, "MEMORY_ROOT", mem_root)
    monkeypatch.setattr(recall_verifier, "OMEGA_ROOT", tmp_path)
    with walker._COORD_INDEX_LOCK:
        walker.COORD_INDEX.clear()
    walker._coord_index_mtime = 0.0
    return tmp_path


def test_operator_chat_source_is_cited_with_the_intake_time(memory_roots, monkeypatch):
    import memory.engine.validate_write as validate_write
    import memory.source_ledger.store as source_store

    monkeypatch.setattr(validate_write, "MEMORY_ROOT", memory_roots / "memory")
    intake_time = "2026-07-30T20:15:00+00:00"
    # The ledger must never call a wall clock of its own (the memory
    # nondeterminism scanner fails the build on datetime.now in that package);
    # it records the timestamp the EVENT carries, passed in by the caller.
    # Patching source_store._now_iso passed silently while the ledger was
    # deriving a hash-based date instead, citing an utterance as 2093-04-02.
    #
    # This used to reach the ledger through the retired SharedState ingress
    # mirror (AGENTS.md §4.0). It now
    # exercises the minter the live chat path actually calls
    # (pressure_loop/operator_chat.py:441).
    citation = source_store.record_operator_chat_source(
        "Continue the memory engine work.",
        cycle_id="cycle-op-1",
        session_timestamp=intake_time,
        root=memory_roots,
    )
    assert citation.get("source_id")
    assert citation.get("span_hash")
    artifact = read_source(citation["source_id"], root=memory_roots)
    assert artifact is not None
    assert artifact.content == "Continue the memory engine work."
    assert artifact.source_type == "operator_chat"
    assert citation.get("source_session_id") == "cycle-op-1"
    assert citation.get("source_session_timestamp") == intake_time
    assert artifact.created_at == intake_time


def test_capture_episode_preserves_operator_chat_source_citation(memory_roots, monkeypatch):
    tmp_path = memory_roots
    import memory.source_ledger.store as source_store

    citation = source_store.record_operator_chat_source(
        "Preserve this operator span.",
        cycle_id="cycle-op-2",
        root=memory_roots,
    )

    captured = []

    def fake_intake(env, *, propose=True, bypass_guardian=False, skip_embed=False):
        captured.append(env.payload)

    monkeypatch.setattr("memory.api.intake_memory", fake_intake)
    monkeypatch.setattr(
        "memory.taxonomy.canonical_capture.write_episode_to_temporal",
        lambda episode: None,
    )
    episode = {
        "episode_id": "ep-op-2",
        "closed_at": "2026-07-03T00:00:00+00:00",
        "cascade_status": "completed",
        "pressure": {
            "opened_by": "operator_query",
            "contributors": [
                {
                    "id": "operator_query",
                    "weight": 1.0,
                    # The contributor detail is the QUEUED turn's metadata — the
                    # only copy now that the ingress mirror is retired (§4.0).
                    "detail": {
                        "message": "Preserve this operator span.",
                        "ingress_class": "epistemic_request",
                        "cycle_id": "cycle-op-2",
                        "source_citation": dict(citation),
                    },
                }
            ],
        },
        "global": {"intent": "RESPOND"},
        "auto": {"status": "completed", "action": "respond"},
    }

    report = capture_episode_to_canonical(episode, cycle_id="cycle-op-2")
    assert not report["errors"]
    assert captured
    assert any(item.get("source_id") == citation["source_id"] for item in captured)


def test_integrity_audit_reports_orphan_sense_rows(memory_roots):
    tmp_path = memory_roots
    sense_path = tmp_path / "memory" / "sense_registry" / "senses.jsonl"
    sense_path.parent.mkdir(parents=True, exist_ok=True)
    sense_path.write_text(
        json.dumps(
            {
                "type": "sense_record",
                "sense_id": "sense:agent:abc",
                "term": "agent",
                "label": "Agent",
                "coord": [2, 0, 1],
                "memory_id": "missing-memory",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = run_integrity_audit(root=tmp_path)
    assert report.status == "issues"
    assert "sense:agent:abc" in report.orphan_sense_rows


def test_integrity_audit_reports_missing_intake_packet(memory_roots):
    tmp_path = memory_roots
    memory_root = tmp_path / "memory"
    (memory_root / "indexes").mkdir(parents=True, exist_ok=True)
    record_path = memory_root / "manual.json"
    record_path.write_text(
        json.dumps(
            {
                "id": "manual",
                "coord": [2, 0, 1],
                "type": "concept",
                "title": "Manual bypass",
                "summary": "Manual record without intake packet.",
                "provenance": {},
            }
        ),
        encoding="utf-8",
    )
    (memory_root / "indexes" / "coord_index.json").write_text(
        json.dumps({"manual": {"coord": [2, 0, 1], "path": "memory/manual.json"}}),
        encoding="utf-8",
    )

    report = run_integrity_audit(root=tmp_path)

    assert report.status == "issues"
    assert "manual" in report.missing_intake_packets


def test_integrity_audit_accepts_builder_intake_packet(memory_roots, monkeypatch):
    tmp_path = memory_roots
    import memory.engine.validate_write as validate_write
    import memory.engine.walker as walker
    from memory.core import build_memory_dict

    memory_root = tmp_path / "memory"
    monkeypatch.setattr(validate_write, "TMP_DIR", memory_root / "tmp_writes")
    monkeypatch.setattr(validate_write, "INDEX_PATH", memory_root / "indexes" / "coord_index.json")
    monkeypatch.setattr(walker, "INDEX_PATH", memory_root / "indexes" / "coord_index.json")
    (memory_root / "indexes").mkdir(parents=True, exist_ok=True)
    (memory_root / "tmp_writes").mkdir(parents=True, exist_ok=True)
    (memory_root / "indexes" / "coord_index.json").write_text("{}", encoding="utf-8")

    record = build_memory_dict(
        "Builder record carries intake packet.",
        [2, 0, 1],
        "test:intake",
        metadata={
            "title": "Builder intake",
            "summary": "Builder record carries intake packet.",
            "domain": "memory",
            "subtopic": "intake",
        },
    )
    validate_write.atomic_write(record, update_index=True)

    report = run_integrity_audit(root=tmp_path)

    assert record["id"] not in report.missing_intake_packets


def test_integrity_audit_reports_code_knowledge_lobe_misroutes(memory_roots, monkeypatch):
    tmp_path = memory_roots
    import memory.engine.validate_write as validate_write
    import memory.engine.walker as walker
    from memory.core import build_memory_dict

    memory_root = tmp_path / "memory"
    monkeypatch.setattr(validate_write, "TMP_DIR", memory_root / "tmp_writes")
    monkeypatch.setattr(validate_write, "INDEX_PATH", memory_root / "indexes" / "coord_index.json")
    monkeypatch.setattr(walker, "INDEX_PATH", memory_root / "indexes" / "coord_index.json")
    (memory_root / "indexes").mkdir(parents=True, exist_ok=True)
    (memory_root / "tmp_writes").mkdir(parents=True, exist_ok=True)
    (memory_root / "indexes" / "coord_index.json").write_text("{}", encoding="utf-8")

    code_in_knowledge = build_memory_dict(
        "Python ExitStack composes cleanup callbacks.",
        [2, 0, 1],
        "test:route-audit",
        metadata={
            "title": "Python ExitStack",
            "summary": "Python ExitStack composes cleanup callbacks.",
            "domain": "python",
            "subtopic": "context_managers",
        },
    )
    world_in_coding = build_memory_dict(
        "Mitochondria produce ATP in cells.",
        [8, 0, 1],
        "test:route-audit",
        metadata={
            "title": "Mitochondria ATP",
            "summary": "Mitochondria produce ATP in cells.",
            "domain": "biology",
            "subtopic": "cell_energy",
        },
    )
    validate_write.atomic_write(code_in_knowledge, update_index=True)
    validate_write.atomic_write(world_in_coding, update_index=True)

    report = run_integrity_audit(root=tmp_path)

    assert code_in_knowledge["id"] in report.lobe_route_mismatches
    assert world_in_coding["id"] in report.lobe_route_mismatches


def test_integrity_audit_accepts_code_and_world_lobe_routes(memory_roots, monkeypatch):
    tmp_path = memory_roots
    import memory.engine.validate_write as validate_write
    import memory.engine.walker as walker
    from memory.core import build_memory_dict

    memory_root = tmp_path / "memory"
    monkeypatch.setattr(validate_write, "TMP_DIR", memory_root / "tmp_writes")
    monkeypatch.setattr(validate_write, "INDEX_PATH", memory_root / "indexes" / "coord_index.json")
    monkeypatch.setattr(walker, "INDEX_PATH", memory_root / "indexes" / "coord_index.json")
    (memory_root / "indexes").mkdir(parents=True, exist_ok=True)
    (memory_root / "tmp_writes").mkdir(parents=True, exist_ok=True)
    (memory_root / "indexes" / "coord_index.json").write_text("{}", encoding="utf-8")

    code_record = build_memory_dict(
        "Python ExitStack composes cleanup callbacks.",
        [8, 0, 1],
        "test:route-audit",
        metadata={
            "title": "Python ExitStack",
            "summary": "Python ExitStack composes cleanup callbacks.",
            "domain": "python",
            "subtopic": "context_managers",
        },
    )
    world_record = build_memory_dict(
        "Mitochondria produce ATP in cells.",
        [2, 0, 1],
        "test:route-audit",
        metadata={
            "title": "Mitochondria ATP",
            "summary": "Mitochondria produce ATP in cells.",
            "domain": "biology",
            "subtopic": "cell_energy",
        },
    )
    validate_write.atomic_write(code_record, update_index=True)
    validate_write.atomic_write(world_record, update_index=True)

    report = run_integrity_audit(root=tmp_path)

    assert code_record["id"] not in report.lobe_route_mismatches
    assert world_record["id"] not in report.lobe_route_mismatches


def test_integrity_repair_restores_missing_intake_packet(memory_roots, monkeypatch):
    tmp_path = memory_roots
    import memory.engine.validate_write as validate_write
    import memory.engine.walker as walker
    from memory.core import build_memory_dict
    from memory.projection_receipt import verify_projection_receipt, write_projection_receipt

    memory_root = tmp_path / "memory"
    monkeypatch.setattr(validate_write, "TMP_DIR", memory_root / "tmp_writes")
    monkeypatch.setattr(validate_write, "INDEX_PATH", memory_root / "indexes" / "coord_index.json")
    monkeypatch.setattr(walker, "INDEX_PATH", memory_root / "indexes" / "coord_index.json")
    (memory_root / "indexes").mkdir(parents=True, exist_ok=True)
    (memory_root / "tmp_writes").mkdir(parents=True, exist_ok=True)
    (memory_root / "indexes" / "coord_index.json").write_text("{}", encoding="utf-8")

    record = build_memory_dict(
        "Repair can restore an intake packet.",
        [2, 0, 1],
        "test:intake-repair",
        metadata={
            "title": "Repair intake",
            "summary": "Repair can restore an intake packet.",
            "domain": "memory",
            "subtopic": "intake",
        },
    )
    record["provenance"]["projection_receipt_required"] = True
    write_projection_receipt(record, root=tmp_path)
    record["enrichment"].pop("intake_packet")
    validate_write.atomic_write(record, update_index=True)

    before = run_integrity_audit(root=tmp_path)
    assert record["id"] in before.missing_intake_packets
    assert before.projection_claim_hash_mismatches == 1

    repair = repair_integrity_issues(root=tmp_path)
    after = run_integrity_audit(root=tmp_path)

    assert record["id"] in repair.intake_packets_repaired
    assert record["id"] not in after.missing_intake_packets
    assert after.projection_claim_hash_mismatches == 0

    walker.load_coord_index()
    repaired_path = walker.resolve_coord_index_path(
        walker.coord_index_snapshot()[record["id"]]["path"]
    )
    assert repaired_path is not None
    repaired = json.loads(repaired_path.read_text(encoding="utf-8"))
    assert verify_projection_receipt(repaired, root=tmp_path).ok


def test_integrity_repair_refreshes_source_stable_projection_claim(memory_roots, monkeypatch):
    tmp_path = memory_roots
    import memory.engine.validate_write as validate_write
    import memory.engine.walker as walker
    from memory.core import build_memory_dict
    from memory.projection_receipt import verify_projection_receipt, write_projection_receipt

    memory_root = tmp_path / "memory"
    monkeypatch.setattr(validate_write, "TMP_DIR", memory_root / "tmp_writes")
    monkeypatch.setattr(validate_write, "INDEX_PATH", memory_root / "indexes" / "coord_index.json")
    monkeypatch.setattr(walker, "INDEX_PATH", memory_root / "indexes" / "coord_index.json")
    (memory_root / "indexes").mkdir(parents=True, exist_ok=True)
    (memory_root / "tmp_writes").mkdir(parents=True, exist_ok=True)
    (memory_root / "indexes" / "coord_index.json").write_text("{}", encoding="utf-8")

    record = build_memory_dict(
        "Receipt repair can refresh deterministic canonical metadata.",
        [2, 0, 1],
        "test:receipt-repair",
        metadata={
            "title": "Repair receipt",
            "summary": "Receipt repair can refresh deterministic canonical metadata.",
            "domain": "memory",
            "subtopic": "projection_receipts",
        },
    )
    record["provenance"]["projection_receipt_required"] = True
    record["embeddings"] = {
        "engine": "local",
        "model": "test-embed",
        "dim": 2,
        "vector_schema_version": 1,
        "text_768": [0.1, 0.2],
    }
    validate_write.atomic_write(record, update_index=True)
    write_projection_receipt(record, root=tmp_path)

    changed = dict(record)
    changed["summary"] = "Receipt repair refreshed a deterministic projection claim."
    validate_write.atomic_write(changed, update_index=True)

    before = run_integrity_audit(root=tmp_path)
    assert before.projection_claim_hash_mismatches == 1

    repair = repair_integrity_issues(root=tmp_path)
    after = run_integrity_audit(root=tmp_path)

    assert changed["id"] in repair.projection_receipts_repaired
    assert repair.projection_receipts_unrepaired == ()
    assert after.projection_claim_hash_mismatches == 0

    walker.load_coord_index()
    repaired_path = walker.resolve_coord_index_path(
        walker.coord_index_snapshot()[changed["id"]]["path"]
    )
    assert repaired_path is not None
    repaired = json.loads(repaired_path.read_text(encoding="utf-8"))
    assert verify_projection_receipt(repaired, root=tmp_path).ok


def test_integrity_repair_removes_orphan_sense_rows(memory_roots):
    tmp_path = memory_roots
    sense_path = tmp_path / "memory" / "sense_registry" / "senses.jsonl"
    sense_path.parent.mkdir(parents=True, exist_ok=True)
    sense_path.write_text(
        json.dumps(
            {
                "type": "sense_record",
                "sense_id": "sense:agent:orphan",
                "term": "agent",
                "label": "Agent",
                "coord": [2, 0, 1],
                "memory_id": "missing-memory",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    repair = repair_integrity_issues(root=tmp_path)

    assert "sense:agent:orphan" in repair.orphan_sense_rows_removed
    assert sense_path.read_text(encoding="utf-8") == ""


def test_integrity_pressure_contributor_surfaces_drift(memory_roots, monkeypatch):
    tmp_path = memory_roots
    monkeypatch.setenv("OMEGA_MEMORY_INTEGRITY_PRESSURE", "1")
    monkeypatch.setenv("OMEGA_MEMORY_INTEGRITY_AUDIT_INTERVAL", "0")

    conn = G.connect(root=tmp_path)
    try:
        G.insert_node(
            conn,
            node_id="k-orphan",
            node_type="memory_record",
            label="Orphan",
            payload={
                "type": "memory_leaf_v2",
                "memory_id": "k-orphan",
                "coord": [2, 0, 9],
                "title": "Orphan",
                "summary": "No canonical file.",
                "status": "active",
            },
            source_event_id="dewey:k-orphan",
        )
        conn.commit()
    finally:
        conn.close()

    contributor = integrity_pressure_contributor(root=tmp_path)
    assert contributor is not None
    assert contributor.type == "memory_reconciliation"
    assert contributor.source == "memory_integrity"


def test_run_integrity_audit_if_due_respects_interval(memory_roots, monkeypatch):
    tmp_path = memory_roots
    monkeypatch.setenv("OMEGA_MEMORY_INTEGRITY_AUDIT_INTERVAL", "3600")

    first = run_integrity_audit_if_due(root=tmp_path, force=True)
    second = run_integrity_audit_if_due(root=tmp_path, force=False)
    assert first.status == "ok"
    assert second.status == first.status
    assert second.audited_at == first.audited_at


def test_memory_health_runs_integrity_audit(memory_roots, monkeypatch):
    tmp_path = memory_roots
    import core.paths as paths

    monkeypatch.setattr(paths, "OMEGA_ROOT", tmp_path)
    health = memory_health()
    assert health.issues is not None

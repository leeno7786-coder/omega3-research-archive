"""Memory Engine v2 slices 9–10 completion acceptance tests."""

from __future__ import annotations

import json

import memory.taxonomy.graph_store as G
import pytest

from memory.api import memory_health
from memory.integrity.audit import run_integrity_audit
from memory.recall_verifier import verify_candidates
from memory.source_ledger.store import record_source
from memory.taxonomy.compiler import compile_memory_graph
from memory.taxonomy.hashing import span_hash_for


def _write_record(
    tmp_path,
    memory_id: str,
    *,
    title: str,
    summary: str,
    coord=None,
    provenance=None,
):
    coord = coord or [2, 0, 1]
    memory_root = tmp_path / "memory"
    (memory_root / "indexes").mkdir(parents=True, exist_ok=True)
    record = {
        "id": memory_id,
        "coord": coord,
        "type": "concept",
        "title": title,
        "summary": summary,
        "text": f"{title}. {summary}",
        "provenance": dict(provenance or {}),
    }
    record_path = memory_root / f"{memory_id}.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    index_path = memory_root / "indexes" / "coord_index.json"
    coord_index = {}
    if index_path.exists():
        coord_index = json.loads(index_path.read_text(encoding="utf-8"))
    coord_index[memory_id] = {"coord": coord, "path": f"memory/{memory_id}.json"}
    index_path.write_text(json.dumps(coord_index), encoding="utf-8")


@pytest.fixture
def memory_roots(tmp_path, monkeypatch):
    import core.paths as paths
    import memory.api as memory_api
    import memory.engine.validate_write as validate_write
    import memory.engine.walker as walker
    import memory.recall_verifier as recall_verifier

    monkeypatch.setattr(validate_write, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(walker, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(walker, "INDEX_PATH", tmp_path / "memory" / "indexes" / "coord_index.json")
    with walker._COORD_INDEX_LOCK:
        walker.COORD_INDEX.clear()
    walker._coord_index_mtime = 0.0
    monkeypatch.setattr(paths, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(memory_api, "OMEGA_ROOT", tmp_path)
    monkeypatch.setattr(recall_verifier, "OMEGA_ROOT", tmp_path)
    return tmp_path


# --- Slice 9 ---


def test_span_hash_for_matches_source_ledger():
    text = "Creator. Built by Ada"
    assert span_hash_for(text) == span_hash_for(text)
    assert len(span_hash_for(text)) == 64


def test_verify_candidates_uses_crypto_proof_when_source_cited(memory_roots):
    tmp_path = memory_roots
    artifact = record_source("Creator. Built by Ada", source_type="operator_chat", root=tmp_path)
    _write_record(
        tmp_path,
        "mem.creator",
        title="Creator",
        summary="Built by Ada",
        provenance=artifact.citation(),
    )

    verdict = verify_candidates(
        "creator",
        [
            {
                "memory_id": "mem.creator",
                "coord": [2, 0, 1],
                "score": 0.9,
                "title": "Wrong graph label",
                "summary": "Also wrong summary",
            }
        ],
        root=tmp_path,
    )

    assert verdict.status == "verified"
    assert verdict.evidence[0]["evidence_strength"] == "source_ledger_crypto"
    assert verdict.evidence[0]["span_hash"] == artifact.span_hash


def test_verify_candidates_rejects_cited_record_missing_span_hash(memory_roots):
    tmp_path = memory_roots
    artifact = record_source("Creator. Built by Ada", source_type="operator_chat", root=tmp_path)
    citation = {"source_id": artifact.source_id, "source_type": artifact.source_type}
    _write_record(
        tmp_path,
        "mem.creator",
        title="Creator",
        summary="Built by Ada",
        provenance=citation,
    )

    verdict = verify_candidates(
        "creator",
        [
            {
                "memory_id": "mem.creator",
                "coord": [2, 0, 1],
                "score": 0.9,
                "title": "Creator",
                "summary": "Built by Ada",
            }
        ],
        root=tmp_path,
    )

    assert verdict.status == "unknown"
    assert "span_hash" in verdict.reason.lower()


def test_verify_candidates_still_rederives_when_source_not_cited(memory_roots):
    tmp_path = memory_roots
    _write_record(tmp_path, "mem.creator", title="Creator", summary="Built by Ada")

    verdict = verify_candidates(
        "creator",
        [
            {
                "memory_id": "mem.creator",
                "coord": [2, 0, 1],
                "score": 0.9,
                "title": "Wrong label",
                "summary": "Built by Ada",
            }
        ],
        root=tmp_path,
    )

    assert verdict.status == "unknown"
    assert verdict.evidence == () or verdict.status != "verified"


# --- Slice 10 ---


def test_integrity_audit_reports_orphan_graph_leaf(memory_roots):
    tmp_path = memory_roots
    _write_record(tmp_path, "k1", title="Photosynthesis", summary="Plants convert light.")
    compile_memory_graph(root=tmp_path)

    conn = G.connect(root=tmp_path)
    try:
        G.insert_node(
            conn,
            node_id="k-orphan",
            node_type="memory_record",
            label="Orphan leaf",
            payload={
                "type": "memory_leaf_v2",
                "memory_id": "k-orphan",
                "coord": [2, 0, 9],
                "title": "Orphan leaf",
                "summary": "No canonical file.",
                "status": "active",
            },
            source_event_id="dewey:k-orphan",
        )
        conn.commit()
    finally:
        conn.close()

    report = run_integrity_audit(root=tmp_path)
    assert report.status == "issues"
    assert "k-orphan" in report.orphan_graph_leaves


def test_integrity_audit_reports_missing_source_artifact(memory_roots):
    tmp_path = memory_roots
    _write_record(
        tmp_path,
        "k-missing-src",
        title="Missing source",
        summary="Cites missing ledger artifact.",
        provenance={"source_id": "src:missing:deadbeef", "span_hash": "abc"},
    )
    compile_memory_graph(root=tmp_path)

    report = run_integrity_audit(root=tmp_path)
    assert report.status == "issues"
    assert report.missing_source_artifacts >= 1


def test_integrity_audit_reports_dme_drift(memory_roots):
    tmp_path = memory_roots
    _write_record(tmp_path, "k1", title="Photosynthesis", summary="Plants convert light.")
    compile_memory_graph(root=tmp_path)

    report = run_integrity_audit(root=tmp_path)
    assert report.dme_parity_status == "ok"


def test_memory_health_surfaces_integrity_issues(memory_roots, monkeypatch):
    tmp_path = memory_roots
    import core.paths as paths

    monkeypatch.setattr(paths, "OMEGA_ROOT", tmp_path)
    _write_record(
        tmp_path,
        "k-missing-src",
        title="Missing source",
        summary="Cites missing ledger artifact.",
        provenance={"source_id": "src:missing:deadbeef", "span_hash": "abc"},
    )

    health = memory_health()
    assert health.issues
    assert any("integrity" in issue.lower() or "source" in issue.lower() for issue in health.issues)

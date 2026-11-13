"""Memory Engine v2 slices 6–8 completion acceptance tests."""

from __future__ import annotations

import json

import memory.taxonomy.graph_store as G
import memory.sense_registry as sense_registry
import pytest

from memory.api import QuerySpec, read_memory, recall_verified
from memory.core import build_memory_dict
from memory.dme.adapter import dme_sram_path, load_sram, parity_report, rebuild_sram_from_graph
from memory.sense_registry import register_senses_for_memory
from memory.taxonomy.compiler import compile_memory_graph, compile_source
from memory.taxonomy import recall_graph


def _write_record(
    tmp_path,
    memory_id: str,
    *,
    title: str,
    summary: str,
    coord=None,
    provenance=None,
    enrichment=None,
    cite=True,
):
    coord = coord or [2, 0, 1]
    text = f"{title}. {summary}"
    prov = dict(provenance or {})
    # Cite by default (post 2026-07-21 fail-closed): recall verifies via ledger
    # crypto; a bare file-match (rederive) is no longer accepted. cite=False
    # exercises the unprovenanced/unknown path.
    if cite and not prov.get("source_id"):
        from memory.source_ledger.store import record_source

        artifact = record_source(
            text, source_type="operator_chat", origin_id=f"cycle-{memory_id}", root=tmp_path
        )
        prov.update(artifact.citation())
    memory_root = tmp_path / "memory"
    (memory_root / "indexes").mkdir(parents=True, exist_ok=True)
    record = {
        "id": memory_id,
        "coord": coord,
        "type": "concept",
        "title": title,
        "summary": summary,
        "text": text,
        "provenance": prov,
    }
    if enrichment:
        record["enrichment"] = enrichment
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
    import memory.engine.validate_write as validate_write
    import memory.engine.walker as walker
    import memory.recall_verifier as recall_verifier

    monkeypatch.setattr(validate_write, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(walker, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(paths, "MEMORY_ROOT", tmp_path / "memory")
    monkeypatch.setattr(recall_verifier, "OMEGA_ROOT", tmp_path)
    return tmp_path


# --- Slice 6 ---


def test_compile_source_returns_ledger_citation(tmp_path):
    from memory.source_ledger.store import record_source

    artifact = record_source("Ledger span text.", source_type="operator_chat", root=tmp_path)
    citation = compile_source(artifact.source_id, root=tmp_path)

    assert citation["source_id"] == artifact.source_id
    assert citation["span_hash"] == artifact.span_hash
    assert citation["source_content_hash"] == artifact.content_hash


def test_compiler_quarantines_leaf_when_source_artifact_missing(tmp_path):
    memory_root = tmp_path / "memory"
    (memory_root / "indexes").mkdir(parents=True, exist_ok=True)
    record = {
        "id": "k-missing",
        "coord": [2, 0, 9],
        "type": "concept",
        "title": "Missing source",
        "summary": "Cites a ledger artifact that does not exist.",
        "provenance": {"source_id": "src:missing:deadbeef"},
        "enrichment": {"canonical": True},
    }
    (memory_root / "k-missing.json").write_text(json.dumps(record), encoding="utf-8")
    (memory_root / "indexes" / "coord_index.json").write_text(
        json.dumps({"k-missing": {"coord": [2, 0, 9], "path": "memory/k-missing.json"}}),
        encoding="utf-8",
    )

    compile_memory_graph(root=tmp_path)
    conn = G.connect(root=tmp_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM nodes WHERE node_id = ? AND node_type = ?",
            ("k-missing", "memory_record"),
        ).fetchone()
    finally:
        conn.close()

    payload = json.loads(row["payload_json"])
    assert payload["status"] == "quarantined"


def test_recall_graph_skips_graph_quarantined_leaf(memory_roots, monkeypatch):
    tmp_path = memory_roots
    from memory.source_ledger.store import record_source

    artifact = record_source("Bad span hash leaf.", source_type="operator_chat", root=tmp_path)
    citation = artifact.citation()
    citation["span_hash"] = "bad-span-hash"
    _write_record(
        tmp_path,
        "k-quarantine",
        title="Bad leaf",
        summary="Bad span hash leaf.",
        provenance=citation,
    )
    compile_memory_graph(root=tmp_path)

    monkeypatch.setattr(
        recall_graph,
        "coord_index_snapshot",
        lambda: json.loads((tmp_path / "memory" / "indexes" / "coord_index.json").read_text()),
    )

    candidates = recall_graph.load_canonical_candidates(root=tmp_path)
    assert [item["id"] for item in candidates] == []


# --- Slice 7 ---


def test_register_senses_does_not_scan_body_when_title_has_no_polyseme(tmp_path):
    sense_ids = register_senses_for_memory(
        memory_id="mem.photo",
        title="Photosynthesis",
        summary="Plants convert light. Graph models and agent loops are unrelated.",
        text="Plants convert light. Graph models and agent loops are unrelated.",
        coord=[2, 0, 1],
        domain="biology",
        root=tmp_path,
    )
    assert sense_ids == ()


def test_build_memory_dict_quarantines_bare_polyseme_title():
    mem = build_memory_dict(
        text="An agent plans tasks.",
        coord=[2, 0, 1],
        source_tag="gate:test",
        metadata={"title": "Agent", "summary": "An agent plans tasks."},
    )
    assert mem["enrichment"]["status"] == "quarantined"
    assert mem["flags"]["quarantined"] is True


def test_build_memory_dict_allows_polyseme_title_with_domain():
    mem = build_memory_dict(
        text="A robot agent plans and acts.",
        coord=[2, 0, 1],
        source_tag="gate:test",
        metadata={
            "title": "Agent",
            "summary": "A robot agent plans and acts.",
            "domain": "robotics",
        },
    )
    assert "sense_ids" in mem["enrichment"]
    assert mem.get("flags", {}).get("quarantined") is not True


def test_build_memory_dict_propagates_polyseme_check_failure(monkeypatch):
    def fail_check(*_args, **_kwargs):
        raise OSError("sense registry unavailable")

    monkeypatch.setattr(sense_registry, "bare_polyseme_violation", fail_check)

    with pytest.raises(OSError, match="sense registry unavailable"):
        build_memory_dict(
            text="A detail about an agent.",
            coord=[2, 0, 1],
            source_tag="gate:test",
            metadata={"title": "Agent", "domain": "robotics"},
        )


def test_build_memory_dict_propagates_sense_attachment_failure(monkeypatch):
    def fail_attach(*_args, **_kwargs):
        raise OSError("sense attachment failed")

    monkeypatch.setattr(sense_registry, "bare_polyseme_violation", lambda *_a, **_k: None)
    monkeypatch.setattr(sense_registry, "attach_senses_to_enrichment", fail_attach)

    with pytest.raises(OSError, match="sense attachment failed"):
        build_memory_dict(
            text="A robot agent plans tasks.",
            coord=[2, 0, 1],
            source_tag="gate:test",
            metadata={"title": "Agent", "domain": "robotics"},
        )


# --- Slice 8 ---


def test_load_sram_rebuilds_when_snapshot_hash_stale(memory_roots):
    tmp_path = memory_roots
    _write_record(tmp_path, "k1", title="Neuron", summary="Neurons pass signals through synapses.")
    compile_memory_graph(root=tmp_path)
    rebuild_sram_from_graph(root=tmp_path)

    sram_path = dme_sram_path(tmp_path)
    meta_path = tmp_path / "memory" / "dme" / "sram_meta.json"
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["snapshot_hash"] = "stale-hash"
    meta_path.write_text(json.dumps(payload), encoding="utf-8")
    # Touch binary SRAM so stale meta triggers rebuild
    sram_path.write_bytes(sram_path.read_bytes())

    index = load_sram(root=tmp_path)
    assert index.snapshot_hash != "stale-hash"
    assert index.seat_count == 1


def test_read_memory_merges_dme_candidates_before_verdict_gate(memory_roots, monkeypatch):
    tmp_path = memory_roots
    _write_record(
        tmp_path,
        "k-dme-only",
        title="Neuron",
        summary="Neurons pass signals through synapses.",
        coord=[2, 0, 1],
    )
    compile_memory_graph(root=tmp_path)
    rebuild_sram_from_graph(root=tmp_path)

    class EmptyPacket:
        results = []
        snapshot_hash = "test"
        resolved_entities = ()
        evidence_event_ids = []

    monkeypatch.setattr("memory.taxonomy.recall.recall", lambda **kwargs: EmptyPacket())
    monkeypatch.setattr("memory.recall_compiler.compile_recall_plan", lambda query: None)

    result = read_memory(
        QuerySpec(
            cycle_id="cycle-dme",
            text="neuron synapses",
            allowed_lobes=[2],
            denied_lobes=[],
            limit=5,
        )
    )
    verdict = (result.meta or {}).get("recall_verdict") or {}

    assert verdict.get("status") == "verified"
    assert [c.id for c in result.candidates] == ["k-dme-only"]


def test_recall_verified_uses_dme_when_taxonomy_empty(memory_roots, monkeypatch):
    tmp_path = memory_roots
    _write_record(
        tmp_path,
        "k-dme-only",
        title="Neuron",
        summary="Neurons pass signals through synapses.",
        coord=[2, 0, 1],
    )
    compile_memory_graph(root=tmp_path)
    rebuild_sram_from_graph(root=tmp_path)

    class EmptyPacket:
        results = []
        snapshot_hash = "test"
        resolved_entities = ()
        evidence_event_ids = []

    monkeypatch.setattr("memory.taxonomy.recall.recall", lambda **kwargs: EmptyPacket())
    monkeypatch.setattr("memory.recall_compiler.compile_recall_plan", lambda query: None)

    verdict = recall_verified(
        QuerySpec(
            cycle_id="cycle-dme",
            text="neuron synapses",
            allowed_lobes=[2],
            denied_lobes=[],
            limit=5,
        )
    )
    assert verdict.status == "verified"
    assert verdict.answer_payload["candidates"][0]["memory_id"] == "k-dme-only"


def test_parity_report_ok_after_compile_refresh(memory_roots):
    tmp_path = memory_roots
    _write_record(
        tmp_path, "k1", title="Photosynthesis", summary="Plants convert light into chemical energy."
    )
    compile_memory_graph(root=tmp_path)

    report = parity_report(root=tmp_path)
    assert report.status == "ok"
    assert report.graph_active_leaf_count == report.dme_seat_count == 1

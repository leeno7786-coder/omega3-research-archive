from __future__ import annotations

import json

import memory.taxonomy.graph_store as G
from memory.taxonomy.compiler import compile_memory_graph
from memory.source_ledger.store import record_source


def test_compiler_projects_memory_record_nodes_as_memory_leaf_v2(tmp_path):
    memory_root = tmp_path / "memory"
    (memory_root / "indexes").mkdir(parents=True, exist_ok=True)
    artifact = record_source(
        "Photosynthesis. Plants convert light into chemical energy.",
        source_type="operator_chat",
        origin_id="session-biology-1",
        metadata={
            "source_session_id": "session-biology-1",
            "source_session_timestamp": "2025-03-04T05:06:07+00:00",
        },
        root=tmp_path,
    )
    record = {
        "id": "k1",
        "coord": [2, 0, 1],
        "type": "concept",
        "title": "Photosynthesis",
        "summary": "Plants convert light into chemical energy.",
        "domain": "biology",
        "subtopic": "plants",
        "tags": ["biology", "energy"],
        "provenance": artifact.citation(),
        "enrichment": {"canonical": True, "sense_ids": ["sense:photosynthesis:biology"]},
    }
    (memory_root / "k1.json").write_text(json.dumps(record), encoding="utf-8")
    (memory_root / "indexes" / "coord_index.json").write_text(
        json.dumps({"k1": {"coord": [2, 0, 1], "path": "memory/k1.json"}}),
        encoding="utf-8",
    )

    compile_memory_graph(root=tmp_path)
    conn = G.connect(root=tmp_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM nodes WHERE node_id = ? AND node_type = ?",
            ("k1", "memory_record"),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    payload = json.loads(row["payload_json"])
    assert payload["type"] == "memory_leaf_v2"
    assert payload["memory_id"] == "k1"
    assert payload["coord"] == [2, 0, 1]
    assert payload["title"] == "Photosynthesis"
    assert payload["summary"] == "Plants convert light into chemical energy."
    assert payload["source_id"] == artifact.source_id
    assert payload["source_type"] == "operator_chat"
    assert payload["span_start"] == 0
    assert payload["span_end"] == len("Photosynthesis. Plants convert light into chemical energy.")
    assert payload["span_hash"] == artifact.span_hash
    assert payload["source_session_id"] == "session-biology-1"
    assert payload["source_session_timestamp"] == "2025-03-04T05:06:07+00:00"
    assert payload["sense_ids"] == ["sense:photosynthesis:biology"]
    assert payload["status"] == "active"


def test_compiler_rebuilds_leaf_span_fields_from_source_ledger(tmp_path):
    memory_root = tmp_path / "memory"
    (memory_root / "indexes").mkdir(parents=True, exist_ok=True)
    artifact = record_source(
        "Neurons pass signals through synapses.",
        source_type="operator_chat",
        origin_id="cycle-neuron",
        root=tmp_path,
    )
    record = {
        "id": "k2",
        "coord": [2, 0, 2],
        "type": "concept",
        "title": "Neuron signal",
        "summary": "Neurons pass signals through synapses.",
        "provenance": {"source_id": artifact.source_id},
        "enrichment": {"canonical": True},
    }
    (memory_root / "k2.json").write_text(json.dumps(record), encoding="utf-8")
    (memory_root / "indexes" / "coord_index.json").write_text(
        json.dumps({"k2": {"coord": [2, 0, 2], "path": "memory/k2.json"}}),
        encoding="utf-8",
    )

    compile_memory_graph(root=tmp_path)
    conn = G.connect(root=tmp_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM nodes WHERE node_id = ? AND node_type = ?",
            ("k2", "memory_record"),
        ).fetchone()
    finally:
        conn.close()

    payload = json.loads(row["payload_json"])
    assert payload["source_id"] == artifact.source_id
    assert payload["source_type"] == "operator_chat"
    assert payload["span_start"] == 0
    assert payload["span_end"] == len("Neurons pass signals through synapses.")
    assert payload["span_hash"] == artifact.span_hash
    assert payload["source_content_hash"] == artifact.content_hash
    assert payload["status"] == "active"


def test_compiler_quarantines_leaf_when_cited_span_hash_conflicts(tmp_path):
    memory_root = tmp_path / "memory"
    (memory_root / "indexes").mkdir(parents=True, exist_ok=True)
    artifact = record_source(
        "Agent means one thing in robotics and another in AI orchestration.",
        source_type="operator_chat",
        root=tmp_path,
    )
    citation = artifact.citation()
    citation["span_hash"] = "bad-span-hash"
    record = {
        "id": "k3",
        "coord": [2, 0, 3],
        "type": "concept",
        "title": "Agent",
        "summary": "Agent has multiple technical senses.",
        "provenance": citation,
        "enrichment": {"canonical": True},
    }
    (memory_root / "k3.json").write_text(json.dumps(record), encoding="utf-8")
    (memory_root / "indexes" / "coord_index.json").write_text(
        json.dumps({"k3": {"coord": [2, 0, 3], "path": "memory/k3.json"}}),
        encoding="utf-8",
    )

    compile_memory_graph(root=tmp_path)
    conn = G.connect(root=tmp_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM nodes WHERE node_id = ? AND node_type = ?",
            ("k3", "memory_record"),
        ).fetchone()
    finally:
        conn.close()

    payload = json.loads(row["payload_json"])
    assert payload["status"] == "quarantined"
    assert payload["span_hash"] == artifact.span_hash
    assert payload["source_content_hash"] == artifact.content_hash

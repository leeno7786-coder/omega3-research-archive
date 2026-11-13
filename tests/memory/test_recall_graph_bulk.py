"""Recall-time graph expansion must not decode payloads per neighbor row.

`recall.py` seeds `expand_via_graph` with the ENTIRE canonical corpus, so
each recall walked `graph_store.neighbors()` over every record node — on the
LME-V2 enterprise graph (21,346 records / 1,131,732 edges) that is ~2.3M
rows and ~4.5M payload JSON decodes per query (py-spy measured, 507s warm).
The loop consumes only (node_id, edge_type, confidence) — all plain SQL
columns.

`_bulk_edge_adjacency(conn, edge_types)` replaces the per-node calls with
one streamed id-only scan. `graph_store.neighbors()` itself is untouched.
"""

from __future__ import annotations

import json

from memory.taxonomy import graph_store as G
from memory.taxonomy import recall_graph
from memory.taxonomy.schemas import MemoryEdge


def _build_graph(root, nodes, edges):
    """nodes: [(node_id, node_type)]; edges: [(src, dst, etype, confidence)]."""
    conn = G.connect(root)
    try:
        for node_id, node_type in nodes:
            G.insert_node(
                conn, node_id=node_id, node_type=node_type, label=node_id,
                payload={"label": node_id},
            )
        for i, (src, dst, etype, conf) in enumerate(edges):
            G.insert_edge(conn, MemoryEdge(
                edge_id=f"e{i:06d}", source_id=src, target_id=dst,
                edge_type=etype, evidence_event_id=f"ev{i:06d}",
                confidence=conf, payload={"i": i},
            ))
        conn.commit()
    finally:
        conn.close()


def _fixture(root):
    nodes = [(f"m{i:02d}", "workflow_memory") for i in range(12)]
    nodes += [("ent1", "entity"), ("ent2", "entity")]
    edges = [
        # both directions around m00
        ("m00", "m01", "MENTIONS", 1.0),
        ("m02", "m00", "SHARES_TAG", 1.0),
        # a via-link edge with a non-1.0 confidence: exercises the (node_id, edge_type,
        # confidence) carry (Cut 5A: SHARES_TAG — the semantic class is retired and would
        # be filtered by the via-link whitelist anyway)
        ("m03", "m04", "SHARES_TAG", 0.734),
        # an edge type OUTSIDE the via-link set (must be filtered)
        ("m00", "m05", "REFINES", 1.0),
        # multi-edge: same pair, two admitted types
        ("m06", "m07", "MENTIONS", 1.0),
        ("m06", "m07", "SHARES_TAG", 0.5),
        # self-loop
        ("m08", "m08", "SHARES_TAG", 1.0),
        # entity in the middle (non-record nodes still appear in adjacency)
        ("m09", "ent1", "MENTIONS", 1.0),
        ("ent1", "m10", "MENTIONS", 1.0),
        # dangling: other endpoint has no node row -> invisible per JOIN
        ("m11", "ghost", "MENTIONS", 1.0),
        ("ghost2", "m11", "SHARES_TAG", 1.0),
        # entity-entity
        ("ent1", "ent2", "SAME_AS", 1.0),
    ]
    _build_graph(root, nodes, edges)
    return [n for n, _ in nodes]


def test_bulk_adjacency_matches_per_node_neighbors(tmp_path):
    node_ids = _fixture(tmp_path)
    edge_types = list(recall_graph._VIA_LINK_EDGE_TYPES)

    conn = G.connect(tmp_path)
    try:
        adjacency = recall_graph._bulk_edge_adjacency(conn, tuple(edge_types))
        for nid in node_ids + ["ghost", "ghost2", "absent"]:
            want = [
                (r["node_id"], r["edge_type"], r["confidence"])
                for r in G.neighbors(conn, nid, edge_types=edge_types)
            ]
            got = list(adjacency.get(nid, ()))
            assert got == want, f"adjacency mismatch for {nid}"
    finally:
        conn.close()


def test_bulk_adjacency_never_decodes_payloads(tmp_path, monkeypatch):
    _fixture(tmp_path)
    edge_types = tuple(recall_graph._VIA_LINK_EDGE_TYPES)

    calls = {"n": 0}
    real_loads = json.loads

    def counting_loads(*args, **kwargs):
        calls["n"] += 1
        return real_loads(*args, **kwargs)

    conn = G.connect(tmp_path)
    try:
        monkeypatch.setattr(json, "loads", counting_loads)
        adjacency = recall_graph._bulk_edge_adjacency(conn, edge_types)
    finally:
        conn.close()

    assert adjacency, "fixture graph produced no adjacency"
    assert calls["n"] == 0, (
        f"{calls['n']} json.loads calls — the id-only scan must not decode"
    )

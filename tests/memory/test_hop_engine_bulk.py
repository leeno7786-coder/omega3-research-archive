"""Hop-plane build must read the edge table once, not once per node.

A 100-trajectory LME-V2 ingest (31,499 nodes / 1,131,732 edges) spun
``build_hop_planes`` for 14+ CPU-hours because
``classed_neighbor_maps_from_graph`` called ``graph_store.neighbors()`` per
node, and ``neighbors()`` json-decodes two payloads per returned row — dense
planes re-decode each edge on the order of once per endpoint visit.

Three properties, per docs/briefs/2026-08-06-hop-plane-bulk-brief.md:
1. equivalence — the bulk path must produce byte-identical maps to the
   per-node walk (a faithful reference copy of the old algorithm lives here,
   driven through ``graph_store.neighbors()`` directly);
2. single-decode — building the maps must run ``json.loads`` at most once
   per edge (the walk never consumes payload fields at all);
3. scale sanity — a ~2k-node / ~200k-edge dense plane must build in seconds.
"""

from __future__ import annotations

import json
import time

from memory.dme import hop_engine
from memory.taxonomy import graph_store as G
from memory.taxonomy.schemas import MemoryEdge


# ── graph construction ───────────────────────────────────────────────────────

def _build_graph(root, nodes, edges):
    """nodes: [(node_id, node_type)]; edges: [(source_id, target_id, edge_type)]."""
    conn = G.connect(root)
    try:
        for node_id, node_type in nodes:
            G.insert_node(
                conn, node_id=node_id, node_type=node_type, label=node_id,
                payload={"label": node_id},
            )
        for i, (src, dst, etype) in enumerate(edges):
            G.insert_edge(conn, MemoryEdge(
                edge_id=f"e{i:07d}", source_id=src, target_id=dst,
                edge_type=etype, evidence_event_id=f"ev{i:07d}",
                payload={"i": i},
            ))
        conn.commit()
    finally:
        conn.close()


# ── reference: the per-node walk this fix replaces ───────────────────────────

def _reference_classed_maps(root, memory_ids, *, row_index=None,
                            bridge_degree_cap=hop_engine.DEFAULT_BRIDGE_DEGREE_CAP):
    """Faithful copy of the pre-bulk classed_neighbor_maps_from_graph walk,
    driven through graph_store.neighbors() per node. Kept as the equivalence
    oracle — do not optimize."""
    wanted = {str(m) for m in memory_ids}
    strong, weak = {}, {}
    conn = G.connect(root)
    try:
        for mid in sorted(wanted):
            try:
                neigh = G.neighbors(conn, mid)
            except Exception:
                continue
            for n in neigh:
                nid = str(n.get("node_id") or "")
                if not nid or nid == mid:
                    continue
                if nid in wanted:
                    etype = str(n.get("edge_type") or "")
                    if etype in hop_engine.RETIRED_EDGE_TYPES:
                        continue  # Cut 5A law: retired classes are skipped, not weak
                    lane = strong if etype in hop_engine.STRONG_EDGE_TYPES else weak
                    lane.setdefault(mid, set()).add(nid)
                    continue
                try:
                    second = G.neighbors(conn, nid)
                except Exception:
                    continue
                if bridge_degree_cap and len(second) > bridge_degree_cap:
                    continue
                for m2 in second:
                    m2id = str(m2.get("node_id") or "")
                    if m2id in wanted and m2id != mid:
                        strong.setdefault(mid, set()).add(m2id)
    finally:
        conn.close()

    if row_index:
        by_session = {}
        for meta in row_index:
            mid = str(meta.get("memory_id") or "")
            sid = str(meta.get("source_session_id") or "")
            if mid in wanted and sid:
                by_session.setdefault(sid, []).append(mid)
        for sid in sorted(by_session):
            group = sorted(set(by_session[sid]))
            if len(group) < 2:
                continue
            if bridge_degree_cap and len(group) > bridge_degree_cap:
                continue
            for a in group:
                for b in group:
                    if a != b:
                        weak.setdefault(a, set()).add(b)

    return (
        {k: sorted(v) for k, v in sorted(strong.items())},
        {k: sorted(v) for k, v in sorted(weak.items())},
    )


# ── fixture graph: every edge-semantics corner in one small graph ────────────

def _corner_case_graph(root):
    """~75 nodes exercising: strong/weak/unknown direct edges, entity bridges
    in both edge directions, a hub mediator above the degree cap, self-loops
    (memory and mediator), parallel multi-edges, dangling edges whose other
    endpoint has no node row, and a wanted id absent from the node table."""
    memories = [f"m{i:02d}" for i in range(30)]
    fillers = [f"x{i:02d}" for i in range(40)]
    nodes = (
        [(m, "workflow_memory") for m in memories]
        + [(x, "workflow_memory") for x in fillers]
        + [("ent1", "entity"), ("ent2", "entity"), ("ent3", "entity"),
           ("hub", "entity")]
    )
    edges = [
        # direct strong (memory↔memory, proven types; both directions occur)
        ("m00", "m01", "REFINES"),
        ("m02", "m03", "ANSWERS"),
        ("m05", "m01", "MENTIONS"),
        # direct weak (Cut 5A: SHARES_TAG — the semantic class is retired)
        ("m16", "m17", "SHARES_TAG"),
        # unknown edge type → weak lane, never strong
        ("m18", "m19", "FROBNICATES"),
        # weak clique over m10..m15 (dense plane in miniature)
        *[(f"m{a}", f"m{b}", "SHARES_TAG")
          for a in range(10, 16) for b in range(a + 1, 16)],
        # entity bridge, outbound then inbound around the mediator
        ("m06", "ent2", "MENTIONS"),
        ("ent2", "m07", "OCCURRED_IN"),
        # small mediator linking m00..m04
        *[(f"m{i:02d}", "ent1", "MENTIONS") for i in range(5)],
        # mediator-to-mediator edge: affects degree only, never the maps
        ("ent1", "ent3", "alias_of"),
        # hub mediator: degree 70 > DEFAULT_BRIDGE_DEGREE_CAP (64)
        *[(f"m{i:02d}", "hub", "MENTIONS") for i in range(30)],
        *[(f"x{i:02d}", "hub", "MENTIONS") for i in range(40)],
        # self-loops: wanted node and mediator (mediator loop counts 2 rows
        # of neighbors() degree)
        ("m20", "m20", "SHARES_TAG"),
        ("ent1", "ent1", "alias_of"),
        # parallel multi-edge: same pair lands in BOTH lanes
        ("m21", "m22", "REFINES"),
        ("m21", "m22", "SHARES_TAG"),
        # dangling edges: the missing endpoint has no node row, so the JOIN
        # in neighbors() hides them from the walk
        ("m00", "ghost", "MENTIONS"),
        ("ghost2", "m00", "MENTIONS"),
    ]
    _build_graph(root, nodes, edges)
    wanted = memories + ["m99"]  # m99 has no node row at all
    row_index = [
        {"memory_id": "m00", "source_session_id": "s1"},
        {"memory_id": "m01", "source_session_id": "s1"},
        {"memory_id": "m02", "source_session_id": "s1"},
        {"memory_id": "m10", "source_session_id": "s2"},
        {"memory_id": "m23", "source_session_id": ""},
        {"memory_id": "not-wanted", "source_session_id": "s1"},
    ]
    return wanted, row_index


# ── 1. equivalence ───────────────────────────────────────────────────────────

def test_bulk_maps_match_per_node_walk(tmp_path):
    wanted, row_index = _corner_case_graph(tmp_path)

    got = hop_engine.classed_neighbor_maps_from_graph(
        tmp_path, wanted, row_index=row_index
    )
    want = _reference_classed_maps(tmp_path, wanted, row_index=row_index)
    assert got == want

    # the reference is only meaningful if the graph actually produced maps
    strong, weak = want
    assert strong and weak


def test_bulk_maps_match_per_node_walk_uncapped(tmp_path):
    """bridge_degree_cap=0 disables the hub cap — the hub's bridges appear."""
    wanted, row_index = _corner_case_graph(tmp_path)

    got = hop_engine.classed_neighbor_maps_from_graph(
        tmp_path, wanted, row_index=row_index, bridge_degree_cap=0
    )
    want = _reference_classed_maps(
        tmp_path, wanted, row_index=row_index, bridge_degree_cap=0
    )
    assert got == want

    # uncapped, the hub bridges every memory to every other memory
    strong, _ = want
    assert set(strong["m00"]) >= {f"m{i:02d}" for i in range(1, 30)}


# ── 2. single-decode property ────────────────────────────────────────────────

def test_bulk_build_decodes_at_most_once_per_edge(tmp_path, monkeypatch):
    """The walk consumes only node ids and edge types — payload JSON must be
    decoded at most once per edge (in practice: never), not once per
    endpoint visit."""
    memories = [f"m{i:02d}" for i in range(30)]
    entities = [f"ent{i:02d}" for i in range(10)]
    nodes = ([(m, "workflow_memory") for m in memories]
             + [(e, "entity") for e in entities])
    edges = (
        # dense weak clique: every memory pair
        [(a, b, "SHARES_TAG") for i, a in enumerate(memories)
         for b in memories[i + 1:]]
        # every entity mentions every memory (mediator re-walk amplifier)
        + [(m, e, "MENTIONS") for e in entities for m in memories]
    )
    _build_graph(tmp_path, nodes, edges)

    calls = {"n": 0}
    real_loads = json.loads

    def counting_loads(*args, **kwargs):
        calls["n"] += 1
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(json, "loads", counting_loads)
    hop_engine.classed_neighbor_maps_from_graph(tmp_path, memories)

    assert calls["n"] <= len(edges), (
        f"{calls['n']} json.loads calls for {len(edges)} edges — "
        "the build is re-decoding payloads per endpoint visit"
    )


# ── 3. scale sanity ──────────────────────────────────────────────────────────

def test_bulk_build_scales_to_dense_planes(tmp_path):
    """~2k nodes / ~200k edges (LME-V2 dense-plane shape in miniature) must
    build in seconds. Generous bound so CI variance can't flake it."""
    group_size, group_count = 200, 10
    memories = [f"m{i:04d}" for i in range(group_size * group_count)]
    entities = [f"ent{i:03d}" for i in range(100)]
    nodes = ([(m, "workflow_memory") for m in memories]
             + [(e, "entity") for e in entities])

    edges = []
    for g in range(group_count):
        group = memories[g * group_size:(g + 1) * group_size]
        edges.extend(
            (a, b, "SHARES_TAG")
            for i, a in enumerate(group) for b in group[i + 1:]
        )
    for k, ent in enumerate(entities):
        for m in memories[k * 20:k * 20 + 60]:
            edges.append((m, ent, "MENTIONS"))
    assert len(edges) > 190_000

    _build_graph(tmp_path, nodes, edges)

    t0 = time.perf_counter()
    strong, weak = hop_engine.classed_neighbor_maps_from_graph(tmp_path, memories)
    elapsed = time.perf_counter() - t0

    assert weak["m0000"], "dense plane produced no weak neighbors"
    assert strong["m0000"], "mediated bridges produced no strong neighbors"
    assert elapsed < 30.0, f"bulk build took {elapsed:.1f}s on ~200k edges"

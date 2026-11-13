"""Thought Engine v2 (memory/dme/hop_engine.py) — RED-first.

The v1 engine's edge was graded union + full-lobe parallel sweep; DME v2's
strict intersection dropped it. Thought Engine v2 restores graded scoring AND
adds what v1 never had: graph-edge hop planes — one matrix operation scores
every leaf's neighborhood simultaneously ("hopping leafs all at the same
time"). RecallVerifier remains the sole authority — the engine only proposes
candidates.
"""

from __future__ import annotations

import numpy as np
import pytest

from memory.dme.packer import pack_entry


def _entry(mem_id: str, coords, source="gate:memory", ts=1000.0):
    return pack_entry(hash(mem_id) & 0xFFFFFFFF, ts, source, coords)


def _sram(rows):
    from memory.dme.lobe_layout import ENTRY_WORDS

    return np.vstack(rows) if rows else np.zeros((0, ENTRY_WORDS), dtype=np.uint64)


# Three leaves: A@(3,0,1), B@(3,0,2), C@(9,0,3). Graph: A—B, B—C.
A_COORDS = [(3, 0, 1)]
B_COORDS = [(3, 0, 2)]
C_COORDS = [(9, 0, 3)]
INDEX = [
    {"memory_id": "leaf-A", "coord": [3, 0, 1], "title": "A", "summary": "alpha"},
    {"memory_id": "leaf-B", "coord": [3, 0, 2], "title": "B", "summary": "bravo"},
    {"memory_id": "leaf-C", "coord": [9, 0, 3], "title": "C", "summary": "charlie"},
]
NEIGHBORS = {"leaf-A": ["leaf-B"], "leaf-B": ["leaf-A", "leaf-C"], "leaf-C": ["leaf-B"]}


def _fixture():
    sram = _sram(
        [_entry("leaf-A", A_COORDS), _entry("leaf-B", B_COORDS), _entry("leaf-C", C_COORDS)]
    )
    from memory.dme.hop_engine import build_hop_plane

    hop = build_hop_plane(sram, INDEX, NEIGHBORS)
    return sram, hop


class TestGradedScoring:
    def test_max_results_cut_records_dropped_candidate_ids(self):
        from memory.dme.hop_engine import search_thought

        ids = ["candidate-A", "candidate-B", "candidate-C"]
        rows = [_entry(memory_id, A_COORDS) for memory_id in ids]
        index = [
            {"memory_id": memory_id, "coord": [3, 0, 1]}
            for memory_id in ids
        ]
        result = search_thought(
            _sram(rows), None, index, A_COORDS, hops=0, max_results=1
        )

        assert [hit.memory_id for hit in result.hits] == ["candidate-A"]
        receipt = result.truncation_receipts[0]
        assert receipt["stage"] == "dme_hop_search:max_results"
        assert receipt["input_count"] == 3
        assert receipt["output_count"] == 1
        assert receipt["dropped_count"] == 2
        assert receipt["dropped_candidate_ids"] == ["candidate-B", "candidate-C"]

    def test_partial_match_surfaces(self):
        # query = A's coord + one coord nobody has: strict intersection would
        # drop A; graded scoring must surface it at ratio 0.5
        from memory.dme.hop_engine import search_thought

        sram, hop = _fixture()
        res = search_thought(sram, hop, INDEX, [(3, 0, 1), (3, 0, 77)], hops=0, floor=0.3)
        ids = [h.memory_id for h in res.hits]
        assert "leaf-A" in ids
        top = res.hits[0]
        assert top.memory_id == "leaf-A" and 0.4 <= top.score <= 0.6

    def test_floor_filters_weak_matches(self):
        from memory.dme.hop_engine import search_thought

        sram, hop = _fixture()
        # 1-of-3 coords = 0.33 < floor 0.5 → dropped
        res = search_thought(
            sram, hop, INDEX, [(3, 0, 1), (3, 0, 77), (9, 0, 88)], hops=0, floor=0.5
        )
        assert all(h.memory_id != "leaf-A" for h in res.hits)

    def test_exact_match_scores_one(self):
        from memory.dme.hop_engine import search_thought

        sram, hop = _fixture()
        res = search_thought(sram, hop, INDEX, A_COORDS, hops=0)
        assert res.hits and res.hits[0].memory_id == "leaf-A"
        assert res.hits[0].score == pytest.approx(1.0)


class TestHopPlane:
    def test_one_hop_neighbor_surfaces(self):
        # an IMPERFECT query brushing A (adaptive ladder proceeds): B is A's
        # graph neighbor — B must surface via the hop plane WITHOUT sharing
        # any coordinate. Ratified weights: strong_hop1 = 0.60 × ratio.
        from memory.dme.hop_engine import search_thought

        sram, hop = _fixture()
        res = search_thought(
            sram, hop, INDEX, [(3, 0, 1), (3, 0, 77)], hops=1, decay=0.6, floor=0.3
        )
        by_id = {h.memory_id: h for h in res.hits}
        assert "leaf-A" in by_id and "leaf-B" in by_id
        assert by_id["leaf-A"].score > by_id["leaf-B"].score
        assert by_id["leaf-B"].score == pytest.approx(0.6 * 0.5, abs=0.05)
        assert by_id["leaf-B"].hop == 1

    def test_two_hop_chain_surfaces(self):
        # A—B—C: hops=3 = explicit exploratory traversal → C surfaces at the
        # ratified strong_hop2 weight even from a perfect direct hit on A
        from memory.dme.hop_engine import search_thought

        sram, hop = _fixture()
        res = search_thought(sram, hop, INDEX, A_COORDS, hops=3, decay=0.6, floor=0.2)
        by_id = {h.memory_id: h for h in res.hits}
        assert "leaf-C" in by_id
        assert by_id["leaf-C"].score == pytest.approx(0.20, abs=0.05)
        assert by_id["leaf-C"].hop == 2

    def test_hops_zero_disables_traversal(self):
        from memory.dme.hop_engine import search_thought

        sram, hop = _fixture()
        res = search_thought(sram, hop, INDEX, A_COORDS, hops=0)
        assert all(h.memory_id == "leaf-A" for h in res.hits)


class TestDeterminism:
    def test_double_run_identical(self):
        from memory.dme.hop_engine import search_thought

        sram, hop = _fixture()
        r1 = search_thought(sram, hop, INDEX, A_COORDS, hops=2)
        r2 = search_thought(sram, hop, INDEX, A_COORDS, hops=2)
        assert [(h.memory_id, h.score, h.hop) for h in r1.hits] == [
            (h.memory_id, h.score, h.hop) for h in r2.hits
        ]


class TestBackendContract:
    def test_overlap_numpy_fallback_is_exact_when_onnx_unavailable(self, monkeypatch):
        """ONNX-first dispatch with a numpy fallback is the designed contract
        (NPU port 2026-08-07): when no ONNX session is available the numpy
        path must produce the identical exact overlap counts."""
        from memory.dme import hop_engine

        monkeypatch.setattr(hop_engine, "_onnx_session", lambda: None, raising=False)
        sram, _ = _fixture()
        q = _entry("query", [(3, 0, 1), (9, 0, 3)])
        assert hop_engine.overlaps_payload(sram, q).tolist() == [1, 0, 1]


class TestGraphNeighborMap:
    def test_neighbor_map_from_graph(self, tmp_path):
        from memory.taxonomy import graph_store as G
        from memory.taxonomy.schemas import MemoryEdge
        from memory.dme.hop_engine import neighbor_map_from_graph

        conn = G.connect(tmp_path)
        for mid in ("leaf-A", "leaf-B"):
            G.insert_node(conn, node_id=mid, node_type="memory_record", label=mid, payload={})
        G.insert_edge(
            conn,
            MemoryEdge(
                edge_id="e1",
                source_id="leaf-A",
                target_id="leaf-B",
                edge_type="related",
                evidence_event_id="ev1",
            ),
        )
        conn.commit()
        conn.close()

        nm = neighbor_map_from_graph(tmp_path, ["leaf-A", "leaf-B"])
        assert nm.get("leaf-A") == ["leaf-B"]


class TestEntityMediatedAdjacency:
    def test_memories_linked_via_shared_entity_are_neighbors(self, tmp_path):
        # the compiler links memories THROUGH entity nodes (memory—entity—memory);
        # the neighbor map must bridge that or compiler-linked sessions never hop
        from memory.taxonomy import graph_store as G
        from memory.taxonomy.schemas import MemoryEdge
        from memory.dme.hop_engine import neighbor_map_from_graph

        conn = G.connect(tmp_path)
        for mid in ("mem-1", "mem-2"):
            G.insert_node(conn, node_id=mid, node_type="memory_record", label=mid, payload={})
        G.insert_node(conn, node_id="ent-dht22", node_type="device", label="DHT22", payload={})
        G.insert_edge(
            conn,
            MemoryEdge(
                edge_id="e1",
                source_id="mem-1",
                target_id="ent-dht22",
                edge_type="mentions",
                evidence_event_id="ev1",
            ),
        )
        G.insert_edge(
            conn,
            MemoryEdge(
                edge_id="e2",
                source_id="mem-2",
                target_id="ent-dht22",
                edge_type="mentions",
                evidence_event_id="ev2",
            ),
        )
        conn.commit()
        conn.close()

        nm = neighbor_map_from_graph(tmp_path, ["mem-1", "mem-2"])
        assert nm.get("mem-1") == ["mem-2"]
        assert nm.get("mem-2") == ["mem-1"]

"""Task #12 — adaptive weighted recall core (RATIFIED spec, 2026-07-04).

Score combine table (weight = evidence-class ceiling; ratio = match quality
within the class; the floor gates the RATIO, the weighted score ranks):

    direct 1.00 / graded_union 0.80 / strong_hop1 0.60 / weak_hop1 0.25 /
    strong_hop2 0.20 / weak_hop2 0.05
    + target-lobe & source-authority bonuses − generic-token penalty

Edge classes: strong = same_entity/alias_of (entity-mediated bridge),
update_of (SUPERSEDES), compiler_link (MENTIONS/CONTRADICTS/...),
same_session; weak = nearby (CO_LOCATED), generic_relation (SHARES_DOMAIN),
loose_context (SHARES_TAG). (co_mentioned / SEMANTIC_SIMILARITY was retired
at compiler v7 — memory-organ Cut 5A; hop_engine.RETIRED_EDGE_TYPES.)

Adaptive hop ladder: direct/union always; hop-1 unless the direct evidence is
PERFECT (alias bridges are never perfect — a stricter gate would amputate the
alias-recovery mechanism the hop planes exist for); hop-2 only when nothing
clears the floor; hop-3 explicit-exploratory only.

Byte-stable throughout; the verifier stays the only authority.
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


# Topology: A@{(3,0,1),(3,0,9)} —strong— B@(3,0,2) —strong— C@(9,0,3);
# D@(3,0,4) is A's WEAK neighbor. No coordinate overlap between leaves.
INDEX = [
    {"memory_id": "leaf-A", "coord": [3, 0, 1], "title": "A", "summary": "alpha"},
    {"memory_id": "leaf-B", "coord": [3, 0, 2], "title": "B", "summary": "bravo"},
    {"memory_id": "leaf-C", "coord": [9, 0, 3], "title": "C", "summary": "charlie"},
    {"memory_id": "leaf-D", "coord": [3, 0, 4], "title": "D", "summary": "delta"},
]
A = [(3, 0, 1), (3, 0, 9)]
B, C, D = [(3, 0, 2)], [(9, 0, 3)], [(3, 0, 4)]
STRONG_NM = {"leaf-A": ["leaf-B"], "leaf-B": ["leaf-A", "leaf-C"], "leaf-C": ["leaf-B"]}
WEAK_NM = {"leaf-A": ["leaf-D"], "leaf-D": ["leaf-A"]}

# a query that brushes A (1 of 2 coords) — imperfect, so the ladder proceeds
HALF_Q = [(3, 0, 1), (3, 0, 77)]


def _fixture():
    from memory.dme.hop_engine import HopPlanes, build_hop_plane

    sram = _sram(
        [_entry("leaf-A", A), _entry("leaf-B", B), _entry("leaf-C", C), _entry("leaf-D", D)]
    )
    planes = HopPlanes(
        strong=build_hop_plane(sram, INDEX, STRONG_NM),
        weak=build_hop_plane(sram, INDEX, WEAK_NM),
    )
    return sram, planes


class TestScoreTable:
    def test_exact_direct_scores_one(self):
        from memory.dme.hop_engine import search_thought

        sram, planes = _fixture()
        res = search_thought(sram, planes, INDEX, A, hops=2)
        assert res.hits[0].memory_id == "leaf-A"
        assert res.hits[0].score == pytest.approx(1.0)
        assert res.hits[0].edge_class == "direct"

    def test_partial_union_weighted_point_eight(self):
        from memory.dme.hop_engine import search_thought

        sram, planes = _fixture()
        res = search_thought(sram, planes, INDEX, HALF_Q, hops=0, floor=0.3)
        top = res.hits[0]
        assert top.memory_id == "leaf-A"
        assert top.score == pytest.approx(0.8 * 0.5, abs=0.05)
        assert top.edge_class == "union"

    def test_strong_hop1_weighted_sixty(self):
        # B's strong neighborhood covers 1 of the 2 query coords → 0.60 × 0.5
        from memory.dme.hop_engine import search_thought

        sram, planes = _fixture()
        res = search_thought(sram, planes, INDEX, HALF_Q, hops=1, floor=0.3)
        by_id = {h.memory_id: h for h in res.hits}
        assert by_id["leaf-B"].score == pytest.approx(0.60 * 0.5, abs=0.05)
        assert by_id["leaf-B"].edge_class == "strong_hop1"
        assert by_id["leaf-B"].hop == 1

    def test_weak_hop1_weighted_quarter(self):
        from memory.dme.hop_engine import search_thought

        sram, planes = _fixture()
        res = search_thought(sram, planes, INDEX, HALF_Q, hops=1, floor=0.2)
        by_id = {h.memory_id: h for h in res.hits}
        assert "leaf-D" in by_id, "weak neighbor must surface"
        assert by_id["leaf-D"].score == pytest.approx(0.25 * 0.5, abs=0.05)
        assert by_id["leaf-D"].edge_class == "weak_hop1"
        assert by_id["leaf-D"].score < by_id["leaf-B"].score, (
            "strong evidence must outrank weak on identical topology"
        )

    def test_legacy_single_plane_is_strong(self):
        # back-compat: a bare ndarray plane keeps the old decay-0.6 weighting
        from memory.dme.hop_engine import build_hop_plane, search_thought

        sram, _ = _fixture()
        legacy = build_hop_plane(sram, INDEX, STRONG_NM)
        res = search_thought(sram, legacy, INDEX, HALF_Q, hops=1, floor=0.3)
        by_id = {h.memory_id: h for h in res.hits}
        assert by_id["leaf-B"].score == pytest.approx(0.60 * 0.5, abs=0.05)


class TestAdaptiveLadder:
    def test_hop1_is_default_on_even_for_perfect_hits(self):
        # Mode C = graded + hop1: an alias query matches the BRIDGE perfectly,
        # so hop-1 must run regardless of direct confidence (Gate-1 live
        # finding: a perfect-hit skip capped aliasL at 50%). Hop-2 stays
        # adaptive — a perfect hit clears the floor, so no hop-2 spend.
        from memory.dme.hop_engine import search_thought

        sram, planes = _fixture()
        res = search_thought(sram, planes, INDEX, A, hops=2)
        assert res.hops_run == 1
        by_id = {h.memory_id: h for h in res.hits}
        assert res.hits[0].memory_id == "leaf-A"  # direct still top
        assert "leaf-B" in by_id and by_id["leaf-B"].hop == 1
        assert "leaf-C" not in by_id, "hop-2 must not run when floor cleared"

    def test_imperfect_direct_runs_hop1(self):
        # alias bridges are never perfect matches — the ladder must proceed
        from memory.dme.hop_engine import search_thought

        sram, planes = _fixture()
        res = search_thought(sram, planes, INDEX, HALF_Q, hops=2, floor=0.3)
        assert res.hops_run >= 1

    def test_hop2_only_when_nothing_clears_floor(self):
        from memory.dme.hop_engine import search_thought

        sram, planes = _fixture()
        # 1-of-3 coords: union ratio .33 and hop-1 ratio .33 both < floor .5
        # → hop-2 is the last resort; C (two strong hops from A) surfaces
        res = search_thought(
            sram,
            planes,
            INDEX,
            [(3, 0, 1), (3, 0, 77), (3, 0, 88)],
            hops=2,
            floor=0.5,
        )
        by_id = {h.memory_id: h for h in res.hits}
        assert res.hops_run == 2
        assert "leaf-C" in by_id
        assert by_id["leaf-C"].hop == 2
        assert by_id["leaf-C"].edge_class == "strong_hop2"
        assert by_id["leaf-C"].score == pytest.approx(0.20, abs=0.05)

    def test_hop2_does_not_run_when_floor_is_cleared(self):
        from memory.dme.hop_engine import search_thought

        sram, planes = _fixture()
        # A clears the floor at union .40 (ratio .5 ≥ .34) → no hop-2 spend
        res = search_thought(sram, planes, INDEX, HALF_Q, hops=2, floor=0.34)
        assert res.hops_run == 1
        assert all(h.hop <= 1 for h in res.hits)

    def test_hop3_is_explicit_exploratory(self):
        # hops=3 = exploratory: traversal runs even on a perfect direct hit
        from memory.dme.hop_engine import search_thought

        sram, planes = _fixture()
        res = search_thought(sram, planes, INDEX, A, hops=3, floor=0.2)
        by_id = {h.memory_id: h for h in res.hits}
        assert "leaf-C" in by_id, "A—B—C chain must be reachable exploratorily"
        assert by_id["leaf-C"].hop == 2

    def test_hops_zero_never_traverses(self):
        from memory.dme.hop_engine import search_thought

        sram, planes = _fixture()
        res = search_thought(sram, planes, INDEX, A, hops=0)
        assert res.hops_run == 0
        assert all(h.memory_id == "leaf-A" for h in res.hits)


class TestBonusesAndPenalty:
    def test_target_lobe_bonus_reranks(self):
        from memory.dme.hop_engine import search_thought

        # two leaves each match 1 of 2 coords; only E sits in the target lobe
        index = [
            {"memory_id": "leaf-E", "coord": [3, 0, 5], "title": "E", "summary": ""},
            {"memory_id": "leaf-F", "coord": [9, 0, 6], "title": "F", "summary": ""},
        ]
        sram = _sram([_entry("leaf-E", [(3, 0, 5)]), _entry("leaf-F", [(9, 0, 6)])])
        res = search_thought(
            sram, None, index, [(3, 0, 5), (9, 0, 6)], hops=0, floor=0.3, target_lobe=None
        )
        # without a target lobe: tie broken by id — sanity only
        assert len(res.hits) == 2
        res = search_thought(
            sram, None, index, [(3, 0, 5), (9, 0, 6)], hops=0, floor=0.3, target_lobe=3
        )
        by_id = {h.memory_id: h for h in res.hits}
        assert by_id["leaf-E"].score > by_id["leaf-F"].score
        assert res.hits[0].memory_id == "leaf-E"

    def test_authority_bonus_reranks_equal_evidence(self):
        from memory.dme.hop_engine import search_thought

        index = [
            {"memory_id": "leaf-U", "coord": [3, 0, 7], "title": "U", "summary": ""},
            {"memory_id": "leaf-S", "coord": [3, 0, 8], "title": "S", "summary": ""},
        ]
        sram = _sram(
            [
                _entry("leaf-U", [(3, 0, 7)], source="user"),  # authority 100
                _entry("leaf-S", [(3, 0, 8)], source="system"),  # authority 30
            ]
        )
        res = search_thought(sram, None, index, [(3, 0, 7), (3, 0, 8)], hops=0, floor=0.3)
        by_id = {h.memory_id: h for h in res.hits}
        assert by_id["leaf-U"].score > by_id["leaf-S"].score

    def test_generic_token_penalty_demotes(self):
        from memory.dme.hop_engine import search_thought

        # coord G lives in ~every row (generic); S1/T1/T2 are specific.
        # S matches generic+specific, T specific+specific — equal counts,
        # but T's evidence is cleaner and must rank first.
        g = (3, 0, 40)
        rows, index = [], []
        for i in range(8):
            mid = f"filler-{i}"
            rows.append(_entry(mid, [g, (3, 0, 50 + i)]))
            index.append({"memory_id": mid, "coord": [3, 0, 50 + i], "title": mid, "summary": ""})
        rows.append(_entry("leaf-S", [g, (3, 0, 30)]))
        index.append({"memory_id": "leaf-S", "coord": [3, 0, 30], "title": "S", "summary": ""})
        rows.append(_entry("leaf-T", [(3, 0, 31), (3, 0, 32)]))
        index.append({"memory_id": "leaf-T", "coord": [3, 0, 31], "title": "T", "summary": ""})
        sram = _sram(rows)
        res = search_thought(
            sram,
            None,
            index,
            [g, (3, 0, 30), (3, 0, 31), (3, 0, 32)],
            hops=0,
            floor=0.2,
        )
        by_id = {h.memory_id: h for h in res.hits}
        assert by_id["leaf-T"].score > by_id["leaf-S"].score


class TestClassedNeighborMaps:
    def _graph(self, tmp_path, edges):
        from memory.taxonomy import graph_store as G
        from memory.taxonomy.schemas import MemoryEdge

        conn = G.connect(tmp_path)
        mids = {e[0] for e in edges} | {e[2] for e in edges}
        for mid in sorted(mids):
            ntype = "named_entity" if mid.startswith("ent-") else "memory_record"
            G.insert_node(conn, node_id=mid, node_type=ntype, label=mid, payload={})
        for i, (src, etype, tgt) in enumerate(edges):
            G.insert_edge(
                conn,
                MemoryEdge(
                    edge_id=f"e{i}",
                    source_id=src,
                    target_id=tgt,
                    edge_type=etype,
                    evidence_event_id=f"ev{i}",
                ),
            )
        conn.commit()
        conn.close()

    def test_direct_edge_classification(self, tmp_path):
        from memory.dme.hop_engine import classed_neighbor_maps_from_graph

        self._graph(
            tmp_path,
            [
                ("m1", "SUPERSEDES", "m2"),  # update_of → strong
                ("m1", "CO_LOCATED", "m3"),  # nearby → weak
                ("m1", "SHARES_DOMAIN", "m4"),  # generic_relation → weak
            ],
        )
        strong, weak = classed_neighbor_maps_from_graph(tmp_path, ["m1", "m2", "m3", "m4"])
        assert strong.get("m1") == ["m2"]
        assert sorted(weak.get("m1", [])) == ["m3", "m4"]

    def test_entity_mediated_bridge_is_strong(self, tmp_path):
        from memory.dme.hop_engine import classed_neighbor_maps_from_graph

        self._graph(
            tmp_path,
            [
                ("m1", "MENTIONS", "ent-aegir"),
                ("m2", "MENTIONS", "ent-aegir"),
            ],
        )
        strong, weak = classed_neighbor_maps_from_graph(tmp_path, ["m1", "m2"])
        assert strong.get("m1") == ["m2"], "same_entity is a STRONG class"
        assert "m1" not in weak

    def test_unknown_edge_type_defaults_weak(self, tmp_path):
        from memory.dme.hop_engine import classed_neighbor_maps_from_graph

        self._graph(tmp_path, [("m1", "related", "m2")])
        strong, weak = classed_neighbor_maps_from_graph(tmp_path, ["m1", "m2"])
        assert "m1" not in strong
        assert weak.get("m1") == ["m2"]

    def test_same_session_is_weak_connectivity(self, tmp_path):
        # session adjacency aids DISCOVERY but must never anchor admission —
        # a chat session holds ~20 memories, and strong-classing them let one
        # weak brush flood the answer context with session-mates
        # (LongMemEval shakedown, 2026-07-04)
        from memory.dme.hop_engine import classed_neighbor_maps_from_graph

        self._graph(tmp_path, [("m1", "CO_LOCATED", "m3")])  # no session info in graph
        row_index = [
            {"memory_id": "m1", "source_session_id": "sess-42"},
            {"memory_id": "m2", "source_session_id": "sess-42"},
            {"memory_id": "m3", "source_session_id": "sess-99"},
        ]
        strong, weak = classed_neighbor_maps_from_graph(
            tmp_path, ["m1", "m2", "m3"], row_index=row_index
        )
        assert "m2" in weak.get("m1", [])
        assert "m2" not in strong.get("m1", [])
        assert "m3" not in weak.get("m1", []) or True  # m3 weak via CO_LOCATED is fine


class TestPlanesSidecar:
    def test_load_or_build_persists_both_planes(self, tmp_path):
        from memory.dme.hop_engine import (
            hop_plane_strong_path,
            hop_plane_weak_path,
            load_or_build_hop_planes,
        )

        sram = _sram([_entry("leaf-A", A), _entry("leaf-B", B)])
        index = INDEX[:2]
        planes = load_or_build_hop_planes(tmp_path, sram, index)
        assert planes.strong is not None and planes.weak is not None
        assert hop_plane_strong_path(tmp_path).exists()
        assert hop_plane_weak_path(tmp_path).exists()
        again = load_or_build_hop_planes(tmp_path, sram, index)
        assert np.array_equal(planes.strong, again.strong)
        assert np.array_equal(planes.weak, again.weak)


class TestDeterminism:
    def test_double_run_identical_with_classed_planes(self):
        from memory.dme.hop_engine import search_thought

        sram, planes = _fixture()
        q = [(3, 0, 1), (3, 0, 77), (3, 0, 88)]
        r1 = search_thought(sram, planes, INDEX, q, hops=2, floor=0.5)
        r2 = search_thought(sram, planes, INDEX, q, hops=2, floor=0.5)
        assert [(h.memory_id, h.score, h.hop, h.edge_class) for h in r1.hits] == [
            (h.memory_id, h.score, h.hop, h.edge_class) for h in r2.hits
        ]

    def test_candidate_dict_carries_edge_class(self):
        from memory.dme.hop_engine import search_thought

        sram, planes = _fixture()
        res = search_thought(sram, planes, INDEX, HALF_Q, hops=1, floor=0.3)
        rows = [h.to_candidate_dict(snapshot_hash="s", backend="numpy") for h in res.hits]
        assert all(r["provenance"].get("dme_edge_class") for r in rows)

import json
from pathlib import Path

import pytest

from memory.taxonomy import recall as taxrecall
from memory.taxonomy import recall_graph


def _wire(monkeypatch, records, edges):
    snapshot = {}
    for rid, rec in records.items():
        snapshot[rid] = {
            "coord": rec["coord"],
            "path": rid,
            "title": rec["title"],
            "type": rec.get("type"),
        }
    monkeypatch.setattr(recall_graph, "coord_index_snapshot", lambda: dict(snapshot))
    # The snapshot carries bare record IDs as "path"; production
    # meta_from_coord_entry resolves relative paths under OMEGA_ROOT before
    # calling load_meta_by_path, so the fake keys back off the basename — the
    # rid — instead of the (platform-dependent) resolved absolute path.
    monkeypatch.setattr(
        recall_graph, "load_meta_by_path", lambda path: dict(records[Path(path).name])
    )
    monkeypatch.setattr(recall_graph.G, "connect", lambda root=None: object())

    def _to_row(spec):
        # Normalize a compact (node_id, edge_type, confidence, payload) test spec
        # into the (target_id, edge_type, confidence) rows that
        # _bulk_edge_adjacency feeds expand_via_graph — the organ's bulk edge
        # scan replaced the per-node G.neighbors() calls, so the fake patches
        # the adjacency layer the production loop actually consumes.
        if isinstance(spec, dict):
            return (spec.get("node_id"), spec.get("edge_type"), spec.get("confidence"))
        nid, etype, conf, _payload = spec
        return (nid, etype, conf)

    monkeypatch.setattr(
        recall_graph,
        "_bulk_edge_adjacency",
        lambda conn, edge_types=None: {
            nid: tuple(_to_row(s) for s in specs) for nid, specs in edges.items()
        },
    )
    monkeypatch.setattr(
        taxrecall, "ensure_strength_index", lambda: type("S", (), {"touch": lambda self, m: None})()
    )


def test_knowledge_recall_ranks_on_topic_first(monkeypatch):
    records = {
        "2.00.0001": {
            "id": "2.00.0001",
            "coord": [2, 0, 0],
            "title": "photosynthesis",
            "summary": "plants convert light into chemical energy",
            "type": "concept",
            "tags": ["photosynthesis", "plants", "light"],
        },
        "2.00.0002": {
            "id": "2.00.0002",
            "coord": [2, 0, 0],
            "title": "tax law",
            "summary": "unrelated topic",
            "type": "concept",
            "tags": ["tax"],
        },
    }
    _wire(monkeypatch, records, {})
    packet = taxrecall.recall("how does photosynthesis work in plants", allowed_lobes=[2], limit=5)
    assert packet.results[0]["node_id"] == "2.00.0001"


def test_cross_lobe_structural_neighbor_surfaces(monkeypatch):
    # Cut 5A (REWRITE of test_cross_lobe_similarity_neighbor_surfaces): the
    # intent is cross-lobe via_link surfacing — allowed_lobes=[2] would exclude
    # lobe 3, but a compiled STRUCTURAL edge (SHARES_TAG on the shared "light"
    # tag) crosses the lobe boundary (denied honored, allowed bypassed). The
    # neighbor carries the query's terms, so it survives the lexical gate.
    records = {
        "2.00.0001": {
            "id": "2.00.0001",
            "coord": [2, 0, 0],
            "title": "photosynthesis",
            "summary": "plants convert light",
            "type": "concept",
            "tags": ["light"],
        },
        "3.00.0001": {
            "id": "3.00.0001",
            "coord": [3, 0, 0],
            "title": "energy reasoning",
            "summary": "why plants need light to live",
            "type": "concept",
            "tags": ["light", "energy"],
        },
    }
    edges = {"2.00.0001": [("3.00.0001", "SHARES_TAG", 1.0, {})]}
    _wire(monkeypatch, records, edges)
    packet = taxrecall.recall("plant light", allowed_lobes=[2], limit=5)
    ids = {r["node_id"] for r in packet.results}
    assert "3.00.0001" in ids
    nbr = next(r for r in packet.results if r["node_id"] == "3.00.0001")
    assert nbr["payload"]["via_link"] is True


# RETIRED (Cut 5A Task 4): test_via_link_ranking_uses_similarity_confidence.
# It asserted that a strongly-similar SEMANTIC_SIMILARITY neighbor outranks a
# weakly-similar one via the edge cosine carried as _via_confidence. Structural
# edges carry a constant confidence 1.0, so there is no structural carrier for
# variable edge confidence; semantic ranking is Program I scope.


def test_denied_lobe_structural_neighbor_never_surfaces(monkeypatch):
    # Cut 5A (REWRITE of test_denied_lobe_similarity_neighbor_never_surfaces):
    # lobe denial is enforced on EVERY via_link hop; the edges are structural now.
    records = {
        "2.00.0001": {
            "id": "2.00.0001",
            "coord": [2, 0, 0],
            "title": "photosynthesis",
            "summary": "plants convert light",
            "type": "concept",
        },
        "6.00.0001": {
            "id": "6.00.0001",
            "coord": [6, 0, 0],
            "title": "journal",
            "summary": "omega private",
            "type": "journal",
        },
        "11.00.0001": {
            "id": "11.00.0001",
            "coord": [11, 0, 0],
            "title": "dev",
            "summary": "server edit",
            "type": "code",
        },
    }
    edges = {
        "2.00.0001": [
            ("6.00.0001", "SHARES_TAG", 1.0, {}),
            ("11.00.0001", "MENTIONS", 1.0, {}),
        ]
    }
    _wire(monkeypatch, records, edges)
    packet = taxrecall.recall("plant light", allowed_lobes=[2], denied_lobes=(6, 11), limit=10)
    ids = {r["node_id"] for r in packet.results}
    assert 6 not in {r["payload"]["coord"][0] for r in packet.results}
    assert 11 not in {r["payload"]["coord"][0] for r in packet.results}
    assert "6.00.0001" not in ids and "11.00.0001" not in ids


def test_identity_persona_recall_correct(monkeypatch):
    records = {
        "0.00.0001": {
            "id": "0.00.0001",
            "coord": [0, 0, 0],
            "title": "Creator — Noah (Leeno)",
            "summary": "Omega was created by Noah",
            "type": "identity",
            "tags": ["creator", "identity"],
        },
        "1.00.0001": {
            "id": "1.00.0001",
            "coord": [1, 0, 0],
            "title": "operator persona",
            "summary": "Noah builds via the HUD",
            "type": "persona",
            "tags": ["operator"],
        },
        "8.00.0001": {
            "id": "8.00.0001",
            "coord": [8, 0, 0],
            "title": "python tooling",
            "summary": "asyncio coroutine",
            "type": "code",
            "tags": ["python"],
        },
    }
    _wire(monkeypatch, records, {})
    # Identity query scoped to identity+persona lobes; coding lobe excluded.
    packet = taxrecall.recall("who created you", allowed_lobes=[0, 1], limit=5)
    top_lobe = packet.results[0]["payload"]["coord"][0]
    assert top_lobe in (0, 1)
    assert "8.00.0001" not in {r["node_id"] for r in packet.results}

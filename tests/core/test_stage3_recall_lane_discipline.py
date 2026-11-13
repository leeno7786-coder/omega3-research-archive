# F:/Omega3.0/tests/core/test_stage3_recall_lane_discipline.py
import json
import pytest

from memory.taxonomy import recall_graph


def _write_record(tmp_path, rec):
    """Write a canonical record JSON file and return (id, coord, path)."""
    p = tmp_path / f"{rec['id']}.json"
    p.write_text(json.dumps(rec), encoding="utf-8")
    return rec["id"], rec["coord"], str(p)


@pytest.fixture
def fake_corpus(tmp_path, monkeypatch):
    """A small canonical corpus across lobes 2 (knowledge), 6 (journal),
    11 (dev), wired into walker's coord_index_snapshot + load_meta_by_path."""
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
            "title": "private journal",
            "summary": "omega private musing",
            "type": "journal",
        },
        "11.00.0001": {
            "id": "11.00.0001",
            "coord": [11, 0, 0],
            "title": "dev artifact",
            "summary": "server.ts edit",
            "type": "code",
        },
    }
    paths = {}
    snapshot = {}
    for rid, rec in records.items():
        p = tmp_path / f"{rid}.json"
        p.write_text(json.dumps(rec), encoding="utf-8")
        paths[rid] = str(p)
        snapshot[rid] = {
            "coord": rec["coord"],
            "path": str(p),
            "title": rec["title"],
            "type": rec["type"],
        }
    monkeypatch.setattr(recall_graph, "coord_index_snapshot", lambda: dict(snapshot))
    monkeypatch.setattr(recall_graph, "load_coord_index", lambda: dict(snapshot))
    monkeypatch.setattr(
        recall_graph,
        "load_meta_by_path",
        lambda path: json.loads(open(path, encoding="utf-8").read()),
    )
    return records


def test_denied_lobes_never_loaded(fake_corpus):
    cands = recall_graph.load_canonical_candidates(
        allowed_lobes=None, denied_lobes=(6, 11), allowed_subdomains=None
    )
    lobes = {c["coord"][0] for c in cands}
    assert 6 not in lobes and 11 not in lobes
    assert 2 in lobes


def test_allowed_lobes_filter(fake_corpus):
    cands = recall_graph.load_canonical_candidates(
        allowed_lobes=[2], denied_lobes=(6, 11), allowed_subdomains=None
    )
    assert {c["coord"][0] for c in cands} == {2}


def test_expand_via_graph_drops_denied_lobe_neighbor(fake_corpus, monkeypatch):
    # Knowledge seed 2.00.0001 has a compiled structural edge to a journal
    # (lobe 6) record AND to a dev (lobe 11) record (Cut 5A: SHARES_TAG /
    # MENTIONS — the dense similarity class is retired). Cross-lobe edges
    # must NOT surface the denied-lobe neighbors on traversal. Fakes patch
    # _bulk_edge_adjacency — the one id-only scan that replaced the per-node
    # G.neighbors() calls in the organ's expand_via_graph.
    monkeypatch.setattr(recall_graph.G, "connect", lambda root=None: object())
    monkeypatch.setattr(
        recall_graph,
        "_bulk_edge_adjacency",
        lambda conn, edge_types=None: {
            "2.00.0001": (
                ("6.00.0001", "SHARES_TAG", 1.0),
                ("11.00.0001", "MENTIONS", 1.0),
            )
        },
    )

    expanded = recall_graph.expand_via_graph(["2.00.0001"], denied_lobes=(6, 11))
    assert expanded == []  # both neighbors are in denied lobes -> dropped


def test_expand_via_graph_surfaces_allowed_cross_lobe(fake_corpus, monkeypatch, tmp_path):
    # Add a lobe-3 (reasoning) record reachable by a structural edge from the
    # knowledge seed (Cut 5A: SHARES_TAG). allowed_lobes is bypassed for
    # via_link, denied honored.
    rec = {
        "id": "3.00.0001",
        "coord": [3, 0, 0],
        "title": "light reasoning",
        "summary": "why plants need light",
        "type": "concept",
    }
    p = tmp_path / "3.00.0001.json"
    p.write_text(json.dumps(rec), encoding="utf-8")
    snap = recall_graph.coord_index_snapshot()
    snap["3.00.0001"] = {
        "coord": [3, 0, 0],
        "path": str(p),
        "title": rec["title"],
        "type": rec["type"],
    }
    monkeypatch.setattr(recall_graph, "coord_index_snapshot", lambda: dict(snap))

    monkeypatch.setattr(recall_graph.G, "connect", lambda root=None: object())
    monkeypatch.setattr(
        recall_graph,
        "_bulk_edge_adjacency",
        lambda conn, edge_types=None: {
            "2.00.0001": (("3.00.0001", "SHARES_TAG", 1.0),)
        },
    )

    expanded = recall_graph.expand_via_graph(["2.00.0001"], denied_lobes=(6, 11))
    ids = {c["id"] for c in expanded}
    assert "3.00.0001" in ids
    assert all(c.get("via_link") for c in expanded)


def test_load_canonical_candidates_ensures_index_loaded(monkeypatch):
    """The FIRST recall in a process must not return empty because COORD_INDEX was
    never loaded. load_canonical_candidates reads coord_index_snapshot() (a copy of a
    module global that starts empty) — it must ensure load_coord_index() ran first, or
    the very first 'who is your creator?' after boot grounds on nothing."""
    called = {"load": 0}

    def _spy_load():
        called["load"] += 1
        return {}

    monkeypatch.setattr(recall_graph, "load_coord_index", _spy_load)
    monkeypatch.setattr(recall_graph, "coord_index_snapshot", lambda: {})
    recall_graph.load_canonical_candidates()
    assert called["load"] >= 1, (
        "load_canonical_candidates must ensure the coord index is loaded before snapshot"
    )


def test_expand_via_graph_consumes_real_neighbors_dict_contract(tmp_path, monkeypatch):
    """expand_via_graph must consume the REAL graph_store.neighbors() return
    shape (list[dict]), not 4-tuples. Regression for the unpack crash that made
    recall raise ValueError on any seed that has a compiled graph neighbor."""
    from memory.taxonomy import graph_store as G
    from memory.taxonomy.schemas import MemoryEdge

    # Build a REAL compiled graph: knowledge seed -> reasoning target by a
    # structural MENTIONS edge (Cut 5A: the similarity class is retired and the
    # via-link whitelist never walks it).
    G.reset_graph(tmp_path)
    conn = G.connect(tmp_path)
    G.insert_node(conn, node_id="2.00.0001", node_type="memory_record", label="seed", payload={})
    G.insert_node(conn, node_id="3.00.0001", node_type="memory_record", label="tgt", payload={})
    G.insert_edge(
        conn,
        MemoryEdge(
            edge_id="e1",
            source_id="2.00.0001",
            target_id="3.00.0001",
            edge_type="MENTIONS",
            evidence_event_id="ev1",
            confidence=1.0,
        ),
    )
    conn.commit()
    conn.close()

    rec = {
        "id": "3.00.0001",
        "coord": [3, 0, 0],
        "title": "light reasoning",
        "summary": "why plants need light",
        "type": "concept",
    }
    p = tmp_path / "3.00.0001.json"
    p.write_text(json.dumps(rec), encoding="utf-8")
    snapshot = {
        "2.00.0001": {"coord": [2, 0, 0], "path": "seed-not-loaded"},
        "3.00.0001": {"coord": [3, 0, 0], "path": str(p)},
    }
    monkeypatch.setattr(recall_graph, "coord_index_snapshot", lambda: dict(snapshot))
    monkeypatch.setattr(
        recall_graph,
        "load_meta_by_path",
        lambda path: json.loads(open(path, encoding="utf-8").read()),
    )

    # Uses the REAL G.neighbors (NOT monkeypatched) — pins the dict contract.
    expanded = recall_graph.expand_via_graph(["2.00.0001"], denied_lobes=(6, 11), root=tmp_path)

    ids = {c["id"] for c in expanded}
    assert "3.00.0001" in ids, "real-shaped neighbor was not surfaced (unpack crash?)"
    assert all(c.get("via_link") for c in expanded)

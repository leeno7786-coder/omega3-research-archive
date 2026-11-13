"""Phase 7H — SQLite graph projection.

The graph is a disposable, deterministic projection of the ledger. This phase only
owns the SQLite store: schema creation, indexes, idempotent upserts, deletability.
"""

from __future__ import annotations

from memory.taxonomy import graph_store as G
from memory.taxonomy import paths as P
from memory.taxonomy.schemas import MemoryClaim, MemoryEdge, MemoryEntity, MemoryEvent


def _event(eid="evt_1"):
    return MemoryEvent(
        event_id=eid,
        event_kind="qwen_skill_result",
        episode_id="ep1",
        source="auto",
        created_at="2026-06-21T12:00:00Z",
        content_hash="h",
        payload={"status": "success"},
    )


def _entity(eid="ent_1"):
    return MemoryEntity(
        entity_id=eid,
        entity_kind="skill",
        canonical_name="codex",
        source_event_id="evt_1",
        aliases=("c",),
    )


def _claim(cid="clm_1"):
    return MemoryClaim(
        claim_id=cid,
        subject="codex",
        predicate="selected_for",
        object="inspect",
        source_event_id="evt_1",
        evidence_event_ids=("evt_1",),
    )


def _edge(eid="edg_1"):
    return MemoryEdge(
        edge_id=eid,
        source_id="ep1",
        target_id="ent_1",
        edge_type="SELECTED_SKILL",
        evidence_event_id="evt_1",
    )


def test_graph_initializes_tables(tmp_path):
    conn = G.connect(root=tmp_path)
    try:
        tables = G.table_names(conn)
        assert {"events", "entities", "claims", "nodes", "edges", "recall_log"} <= tables
        assert G.graph_path(root=tmp_path).exists()
    finally:
        conn.close()


def test_indexes_exist(tmp_path):
    conn = G.connect(root=tmp_path)
    try:
        idx = G.index_names(conn)
        for name in (
            "idx_nodes_type",
            "idx_edges_source",
            "idx_edges_target",
            "idx_edges_type",
            "idx_claims_subject",
            "idx_entities_name",
            "idx_events_episode",
            "idx_events_kind",
        ):
            assert name in idx, name
    finally:
        conn.close()


def test_insert_event_is_idempotent(tmp_path):
    conn = G.connect(root=tmp_path)
    try:
        G.insert_event(conn, _event())
        G.insert_event(conn, _event())  # same primary key
        conn.commit()
        assert G.count(conn, "events") == 1
    finally:
        conn.close()


def test_insert_entity_claim_edge_idempotent(tmp_path):
    conn = G.connect(root=tmp_path)
    try:
        for _ in range(2):
            G.insert_entity(conn, _entity())
            G.insert_claim(conn, _claim())
            G.insert_edge(conn, _edge())
            G.insert_node(
                conn,
                node_id="n1",
                node_type="skill",
                label="codex",
                payload={"x": 1},
                source_event_id="evt_1",
            )
        conn.commit()
        assert G.count(conn, "entities") == 1
        assert G.count(conn, "claims") == 1
        assert G.count(conn, "edges") == 1
        assert G.count(conn, "nodes") == 1
    finally:
        conn.close()


def test_get_event_roundtrip(tmp_path):
    conn = G.connect(root=tmp_path)
    try:
        G.insert_event(conn, _event("evt_round"))
        conn.commit()
        row = G.get_event(conn, "evt_round")
        assert row["event_id"] == "evt_round"
        assert row["event_kind"] == "qwen_skill_result"
        assert '"status":"success"' in row["payload_json"]
    finally:
        conn.close()


def test_graph_deletable_and_rebuildable(tmp_path):
    conn = G.connect(root=tmp_path)
    G.insert_event(conn, _event())
    conn.commit()
    assert G.count(conn, "events") == 1
    conn.close()

    G.reset_graph(root=tmp_path)
    assert not G.graph_path(root=tmp_path).exists()

    conn2 = G.connect(root=tmp_path)
    try:
        assert G.count(conn2, "events") == 0  # rebuilt empty
        G.insert_event(conn2, _event())  # and usable again
        conn2.commit()
        assert G.count(conn2, "events") == 1
    finally:
        conn2.close()


def test_count_rejects_unknown_table(tmp_path):
    conn = G.connect(root=tmp_path)
    try:
        import pytest

        with pytest.raises(ValueError):
            G.count(conn, "events; DROP TABLE events")
    finally:
        conn.close()


def test_graph_path_under_compiled_root(tmp_path):
    assert G.graph_path(root=tmp_path) == P.compiled_root(root=tmp_path) / "graph.sqlite"

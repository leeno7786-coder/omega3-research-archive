# F:/Omega3.0/tests/memory/taxonomy/test_compiler_v5_determinism.py
import json
from pathlib import Path
from memory.taxonomy.compiler import compile_memory_graph
from memory.taxonomy.canonical_capture import capture_episode_to_canonical
import memory.taxonomy.graph_store as G


def test_compiler_v5_dewey_edges_and_no_similarity_edges(tmp_path):
    # Set up memory root
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir(exist_ok=True)

    indexes_dir = mem_dir / "indexes"
    indexes_dir.mkdir(exist_ok=True)

    # We will write records:
    # id1: lobe 4 who, tags=["python", "test"], domain="dev"
    # id2: lobe 4 what, tags=["python", "test"], domain="dev", links to id1
    # id3: lobe 9 events, embeddings.text_768 with high similarity to id1
    # id4: lobe 6 journal (isolated, must not have similarity edges)

    # 768-dim mock vectors
    vec_a = [0.1] * 256 + [0.0] * 512
    vec_b = [0.1] * 256 + [0.0] * 512  # identical to vec_a (cosine = 1.0)
    vec_c = [0.0] * 768  # orthog

    coord_index = {
        "id1": {"coord": [4, 0, 0], "path": "memory/lobe4_who.json"},
        "id2": {"coord": [4, 1, 0], "path": "memory/lobe4_what.json"},
        "id3": {"coord": [9, 1, 0], "path": "memory/lobe9_ep.json"},
        "id4": {"coord": [6, 0, 0], "path": "memory/lobe6_journal.json"},
    }
    with open(indexes_dir / "coord_index.json", "w", encoding="utf-8") as f:
        json.dump(coord_index, f)

    # Write files
    rec1 = {
        "id": "id1",
        "coord": [4, 0, 0],
        "type": "situational",
        "title": "Title A",
        "domain": "dev",
        "tags": ["python", "test"],
        "embeddings": {"text_768": vec_a},
        "enrichment": {
            "canonical": True,
            "event_kind": "operator_input",
            "closed_at": "2026-06-29T10:00:00Z",
        },
    }
    with open(mem_dir / "lobe4_who.json", "w", encoding="utf-8") as f:
        json.dump(rec1, f)

    rec2 = {
        "id": "id2",
        "coord": [4, 1, 0],
        "type": "situational",
        "title": "Title B",
        "domain": "dev",
        "tags": ["python", "test"],
        "links": ["id1"],  # explicit link
        "embeddings": {"text_768": vec_b},
        "enrichment": {
            "canonical": True,
            "event_kind": "recall_query",
            "closed_at": "2026-06-29T10:01:00Z",
        },
    }
    with open(mem_dir / "lobe4_what.json", "w", encoding="utf-8") as f:
        json.dump(rec2, f)

    rec3 = {
        "id": "id3",
        "coord": [9, 1, 0],
        "type": "episode",
        "title": "Title C",
        "text": json.dumps({"status": "completed"}),
        "embeddings": {"text_768": vec_c},
        "enrichment": {
            "canonical": True,
            "event_kind": "auto_outcome",
            "closed_at": "2026-06-29T10:02:00Z",
        },
    }
    with open(mem_dir / "lobe9_ep.json", "w", encoding="utf-8") as f:
        json.dump(rec3, f)

    rec4 = {
        "id": "id4",
        "coord": [6, 0, 0],
        "type": "journal",
        "title": "Title D",
        "embeddings": {"text_768": vec_a},  # identical embedding but isolated lobe 6
        "enrichment": {
            "canonical": True,
            "event_kind": "reflection",
            "closed_at": "2026-06-29T10:03:00Z",
        },
    }
    with open(mem_dir / "lobe6_journal.json", "w", encoding="utf-8") as f:
        json.dump(rec4, f)

    # Compile the graph
    snap1 = compile_memory_graph(root=tmp_path)

    assert snap1 is not None
    assert snap1.snapshot_hash

    # 1. Determinism: compile again, assert same hash
    snap2 = compile_memory_graph(root=tmp_path)
    assert snap2.snapshot_hash == snap1.snapshot_hash

    # 2. Rebuild-after-delete is identical
    G.reset_graph(root=tmp_path)
    snap3 = compile_memory_graph(root=tmp_path)
    assert snap3.snapshot_hash == snap1.snapshot_hash

    # 3. Check graph edges in SQLite database
    conn = G.connect(root=tmp_path)
    try:
        # Check node_ids and node_types
        nodes = conn.execute("SELECT node_id, node_type FROM nodes").fetchall()
        node_ids = {n["node_id"] for n in nodes}
        assert "id1" in node_ids
        assert "id2" in node_ids
        assert "id3" in node_ids
        assert "id4" not in node_ids  # lobe 6 is isolated!

        # Check explicit MENTIONS edge from id2 to id1
        edges = conn.execute("SELECT source_id, target_id, edge_type FROM edges").fetchall()
        edge_tuples = {(e["source_id"], e["target_id"], e["edge_type"]) for e in edges}

        # MENTIONS
        assert ("id2", "id1", "MENTIONS") in edge_tuples

        # SHARES_DOMAIN (Title A and Title B share domain "dev")
        assert ("id1", "id2", "SHARES_DOMAIN") in edge_tuples or (
            "id2",
            "id1",
            "SHARES_DOMAIN",
        ) in edge_tuples

        # SHARES_TAG (Title A and Title B share tags "python" and "test")
        assert ("id1", "id2", "SHARES_TAG") in edge_tuples or (
            "id2",
            "id1",
            "SHARES_TAG",
        ) in edge_tuples

        # Cut 5A (v7): id1 and id2 have identical normalized truncated embeddings,
        # and that must NOT produce an edge — the graph is structural-only; the
        # SEMANTIC_SIMILARITY class is retired and stored vectors are never read.
        assert ("id1", "id2", "SEMANTIC_SIMILARITY") not in edge_tuples
        assert ("id2", "id1", "SEMANTIC_SIMILARITY") not in edge_tuples
        sim_edge = [e for e in edges if e["edge_type"] == "SEMANTIC_SIMILARITY"]
        assert sim_edge == []

        # Isolated lobe 6 has no edges at all
        for e in edges:
            assert e["source_id"] != "id4"
            assert e["target_id"] != "id4"

    finally:
        conn.close()

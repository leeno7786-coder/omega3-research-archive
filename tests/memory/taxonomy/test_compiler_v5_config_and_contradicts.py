# F:/Omega3.0/tests/memory/taxonomy/test_compiler_v5_config_and_contradicts.py
"""Stage 2 compiler correctness:
- CONTRADICTS edges read canonical contradiction_state from `enrichment`
  (where the write path stores it), not the top-level field (schema-rejected).
- config_hash folds the Dewey (structural) params so a param change is version-gated
  (Cut 5A / v7: no similarity params exist any more).
"""

import json

import memory.taxonomy.compiler as C
from memory.taxonomy.compiler import compile_memory_graph
import memory.taxonomy.graph_store as G


def _corpus(tmp_path, records):
    mem = tmp_path / "memory"
    (mem / "indexes").mkdir(parents=True, exist_ok=True)
    coord_index = {}
    for rid, rec in records.items():
        fn = f"{rid}.json"
        (mem / fn).write_text(json.dumps(rec), encoding="utf-8")
        coord_index[rid] = {"coord": rec["coord"], "path": f"memory/{fn}"}
    (mem / "indexes" / "coord_index.json").write_text(json.dumps(coord_index), encoding="utf-8")


def test_contradicts_edge_from_enrichment_contradiction_state(tmp_path):
    records = {
        "k1": {
            "id": "k1",
            "coord": [2, 0, 0],
            "type": "concept",
            "title": "claim X is true",
            "enrichment": {
                "canonical": True,
                "contradiction_state": "k2",
                "closed_at": "2026-06-29T10:00:00Z",
            },
        },
        "k2": {
            "id": "k2",
            "coord": [2, 0, 1],
            "type": "concept",
            "title": "claim X is false",
            "enrichment": {"canonical": True, "closed_at": "2026-06-29T10:01:00Z"},
        },
    }
    _corpus(tmp_path, records)
    compile_memory_graph(root=tmp_path)
    conn = G.connect(root=tmp_path)
    try:
        edges = {
            (e["source_id"], e["target_id"], e["edge_type"])
            for e in conn.execute("SELECT source_id,target_id,edge_type FROM edges").fetchall()
        }
    finally:
        conn.close()
    assert ("k1", "k2", "CONTRADICTS") in edges, (
        "CONTRADICTS edge not formed from enrichment.contradiction_state"
    )


def test_config_hash_folds_structural_params(tmp_path, monkeypatch):
    """Cut 5A (v7): the config-hash property survives with STRUCTURAL params; the
    similarity params no longer exist on the compiler at all."""
    assert not hasattr(C, "SIM_THRESHOLD") and not hasattr(C, "SIM_TOPK") and not hasattr(C, "MRL_DIM")
    records = {
        "k1": {
            "id": "k1",
            "coord": [2, 0, 0],
            "type": "concept",
            "title": "A",
            "enrichment": {"canonical": True, "closed_at": "2026-06-29T10:00:00Z"},
        },
    }
    _corpus(tmp_path, records)
    snap_a = compile_memory_graph(root=tmp_path)
    # Changing a structural edge param must change config_hash (version-gating).
    monkeypatch.setattr(C, "CO_EDGE_FANOUT", 3)
    G.reset_graph(root=tmp_path)
    snap_b = compile_memory_graph(root=tmp_path)
    assert snap_b.config_hash != snap_a.config_hash, (
        "structural param change was not folded into config_hash"
    )
    assert "simthr" not in snap_b.config_hash and "mrl" not in snap_b.config_hash


def test_compile_acquires_single_writer_lock(tmp_path, monkeypatch):
    """compile_memory_graph must run the replace+finalize under compile_lock
    so two rebuilds can never interleave and split graph.sqlite vs the manifest."""
    from contextlib import contextmanager

    events = []
    real_lock = C.compile_lock

    @contextmanager
    def _spy(root=None, **kw):
        events.append("enter")
        with real_lock(root, **kw):
            yield
        events.append("exit")

    monkeypatch.setattr(C, "compile_lock", _spy)
    records = {
        "k1": {
            "id": "k1",
            "coord": [2, 0, 0],
            "type": "concept",
            "title": "A",
            "enrichment": {"canonical": True, "closed_at": "2026-06-29T10:00:00Z"},
        },
    }
    _corpus(tmp_path, records)
    compile_memory_graph(root=tmp_path)
    assert events == ["enter", "exit"], "compile did not run under compile_lock"
    # lock released after compile (no leaked lockfile)
    from memory.taxonomy.compile_lock import lock_path

    assert not lock_path(tmp_path).exists()


def test_compile_replaces_graph_without_invalidating_active_reader(tmp_path):
    """A full compile must coexist with cognition reading the previous snapshot.

    Windows refuses to unlink an open SQLite database. On platforms that permit
    the unlink, the reader remains attached to the orphaned inode. In both cases,
    replacement must happen transactionally inside the live database instead.
    """
    records = {
        "k1": {
            "id": "k1",
            "coord": [2, 0, 0],
            "type": "concept",
            "title": "A",
            "enrichment": {"canonical": True, "closed_at": "2026-06-29T10:00:00Z"},
        },
    }
    _corpus(tmp_path, records)
    compile_memory_graph(root=tmp_path)

    reader = G.connect(root=tmp_path)
    try:
        reader.execute("BEGIN")
        previous_node_count = G.count(reader, "nodes")

        records["k2"] = {
            "id": "k2",
            "coord": [2, 0, 1],
            "type": "concept",
            "title": "B",
            "enrichment": {"canonical": True, "closed_at": "2026-06-29T10:01:00Z"},
        }
        _corpus(tmp_path, records)
        compile_memory_graph(root=tmp_path)

        assert G.count(reader, "nodes") == previous_node_count
        reader.commit()
        assert G.count(reader, "nodes") > previous_node_count
    finally:
        reader.close()

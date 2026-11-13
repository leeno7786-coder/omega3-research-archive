"""Cut 5A Task 3 — the SEMANTIC_SIMILARITY class is RETIRED from every graph/hop
consumer (no dangling class), and a stale pre-5A graph artifact cannot feed hops.

Plan: docs/superpowers/plans/2026-08-16-memory-organ-cut5a-structural-only-graph.md (Task 3,
D-5A-3, D-5A-4). semantic_admission.py is deliberately untouched (A'-seed by construction).
"""
from __future__ import annotations

import json
import logging

import pytest

from tests.memory.taxonomy.fixtures.cut5a_corpus import copy_fixture_root


def _stale_v6_graph(tmp_path):
    """A graph.sqlite shaped like a pre-5A (v6) artifact: three memory leaves; a
    SEMANTIC_SIMILARITY edge (retired), a MYSTERY edge (unknown → weak law), a
    MENTIONS edge (strong)."""
    from memory.taxonomy import graph_store as G
    from memory.taxonomy.schemas import MemoryEdge

    G.reset_graph(tmp_path)
    conn = G.connect(tmp_path)
    for nid in ("2.00.a", "2.00.b", "2.00.c", "2.00.d"):
        G.insert_node(conn, node_id=nid, node_type="memory_record", label=nid, payload={})
    G.insert_edge(conn, MemoryEdge(edge_id="e-sim", source_id="2.00.a", target_id="2.00.b",
                                   edge_type="SEMANTIC_SIMILARITY", evidence_event_id="similarity:2.00.a:2.00.b", confidence=0.91))
    G.insert_edge(conn, MemoryEdge(edge_id="e-myst", source_id="2.00.a", target_id="2.00.c",
                                   edge_type="MYSTERY", evidence_event_id="ev-m", confidence=1.0))
    G.insert_edge(conn, MemoryEdge(edge_id="e-men", source_id="2.00.a", target_id="2.00.d",
                                   edge_type="MENTIONS", evidence_event_id="ev-n", confidence=1.0))
    conn.commit()
    conn.close()
    return tmp_path


def test_edge_types_and_weak_types_do_not_contain_semantic_similarity():
    from memory.dme import hop_engine as H
    from memory.taxonomy import schemas as S
    from memory.taxonomy import recall_graph as RG

    assert "SEMANTIC_SIMILARITY" not in S.EDGE_TYPES
    assert "SEMANTIC_SIMILARITY" not in H.WEAK_EDGE_TYPES
    assert H.WEAK_EDGE_TYPES == frozenset({"CO_LOCATED", "SHARES_DOMAIN", "SHARES_TAG"})
    assert H.RETIRED_EDGE_TYPES == frozenset({"SEMANTIC_SIMILARITY"})
    assert not (H.RETIRED_EDGE_TYPES & H.STRONG_EDGE_TYPES) and not (H.RETIRED_EDGE_TYPES & H.WEAK_EDGE_TYPES)
    assert "SEMANTIC_SIMILARITY" not in RG._VIA_LINK_EDGE_TYPES
    # a MemoryEdge of the retired class no longer validates under the production schema
    from memory.taxonomy.schemas import MemoryEdge

    e = MemoryEdge(edge_id="x", source_id="a", target_id="b", edge_type="SEMANTIC_SIMILARITY", evidence_event_id="ev", confidence=1.0)
    assert any("edge_type" in err for err in e.validate())


def test_hop_engine_skips_retired_edges_from_a_stale_graph(tmp_path, caplog):
    """stale v6 artifact: the retired edge is SKIPPED (not weak, not strong) with one
    WARNING naming the count; the unknown type still falls into the weak lane; strong stays strong."""
    from memory.dme.hop_engine import classed_neighbor_maps_from_graph

    root = _stale_v6_graph(tmp_path)
    with caplog.at_level(logging.WARNING, logger="omega.memory.dme.hop_engine"):
        strong, weak = classed_neighbor_maps_from_graph(root, ["2.00.a", "2.00.b", "2.00.c", "2.00.d"])
    assert "2.00.b" not in strong.get("2.00.a", []) and "2.00.b" not in weak.get("2.00.a", [])
    assert "2.00.a" not in strong.get("2.00.b", []) and "2.00.a" not in weak.get("2.00.b", [])
    assert "2.00.c" in weak.get("2.00.a", []) and "2.00.a" in weak.get("2.00.c", [])   # unknown ⇒ weak (law preserved)
    assert "2.00.d" in strong.get("2.00.a", []) and "2.00.a" in strong.get("2.00.d", [])  # MENTIONS ⇒ strong
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "retired" in r.getMessage()]
    assert len(warnings) == 1
    assert "1" in warnings[0].getMessage() and "SEMANTIC_SIMILARITY" in warnings[0].getMessage()


def test_hop_engine_is_silent_when_no_retired_edges(tmp_path, caplog):
    from memory.taxonomy import graph_store as G
    from memory.taxonomy.schemas import MemoryEdge
    from memory.dme.hop_engine import classed_neighbor_maps_from_graph

    G.reset_graph(tmp_path)
    conn = G.connect(tmp_path)
    for nid in ("2.00.a", "2.00.b"):
        G.insert_node(conn, node_id=nid, node_type="memory_record", label=nid, payload={})
    G.insert_edge(conn, MemoryEdge(edge_id="e1", source_id="2.00.a", target_id="2.00.b", edge_type="SHARES_TAG", evidence_event_id="ev", confidence=1.0))
    conn.commit(); conn.close()
    with caplog.at_level(logging.WARNING, logger="omega.memory.dme.hop_engine"):
        _s, weak = classed_neighbor_maps_from_graph(tmp_path, ["2.00.a", "2.00.b"])
    assert "2.00.b" in weak["2.00.a"]
    assert not [r for r in caplog.records if "retired" in r.getMessage()]


def test_recall_graph_does_not_walk_semantic_similarity_edges(tmp_path, monkeypatch):
    """the via-link whitelist no longer contains the class: a stale artifact's
    SEMANTIC_SIMILARITY neighbor is never surfaced; the MENTIONS neighbor is."""
    from memory.taxonomy import recall_graph

    root = _stale_v6_graph(tmp_path)
    recs = {}
    for nid, coord in (("2.00.b", [2, 0, 1]), ("2.00.c", [2, 0, 2]), ("2.00.d", [2, 0, 3])):
        p = tmp_path / f"{nid}.json"
        p.write_text(json.dumps({"id": nid, "coord": coord, "title": nid, "summary": "x", "type": "concept"}), encoding="utf-8")
        recs[nid] = {"coord": coord, "path": str(p)}
    recs["2.00.a"] = {"coord": [2, 0, 0], "path": "seed-not-loaded"}
    monkeypatch.setattr(recall_graph, "coord_index_snapshot", lambda: dict(recs))
    monkeypatch.setattr(recall_graph, "load_meta_by_path", lambda path: json.loads(open(path, encoding="utf-8").read()))
    expanded = recall_graph.expand_via_graph(["2.00.a"], denied_lobes=(6, 11), root=root)
    ids = {c["id"] for c in expanded}
    assert "2.00.b" not in ids            # retired class never walked
    assert "2.00.d" in ids                # MENTIONS still walked
    assert all(c.get("_via_edge_type") != "SEMANTIC_SIMILARITY" for c in expanded)


def test_hop_survives_has_no_semantic_exemption():
    from memory.taxonomy.recall import _hop_survives

    meta = {"id": "9.00.x", "title": "unrelated", "summary": "nothing in common", "text": "", "via_link": True,
            "_via_edge_type": "SEMANTIC_SIMILARITY", "_via_confidence": 0.99}
    assert _hop_survives(meta, {"telescope", "mirror"}) is False
    # a structural hop with lexical overlap still survives (behaviour unchanged)
    meta2 = {"id": "9.00.y", "title": "telescope mirror", "summary": "", "text": "", "via_link": True,
             "_via_edge_type": "MENTIONS", "_via_confidence": 1.0}
    assert _hop_survives(meta2, {"telescope", "mirror"}) is True


@pytest.mark.parametrize("module,forbidden", [
    ("memory.dme.hop_engine", "SEMANTIC_SIMILARITY"),
    ("memory.taxonomy.recall_graph", "SEMANTIC_SIMILARITY"),
    ("memory.taxonomy.recall", "SEMANTIC_SIMILARITY"),
    ("memory.taxonomy.schemas", "SEMANTIC_SIMILARITY"),
])
def test_no_live_reference_to_the_retired_class_except_the_retired_set(module, forbidden):
    """the only production occurrence outside semantic_admission (kept intentionally) and the
    deferred comment sites is hop_engine.RETIRED_EDGE_TYPES itself."""
    import importlib
    import inspect

    src = inspect.getsource(importlib.import_module(module))
    hits = [ln for ln in src.splitlines() if forbidden in ln]
    if module == "memory.dme.hop_engine":
        assert hits and all("RETIRED" in ln or ln.strip().startswith("#") or ln.strip().startswith('"') or "retired" in ln.lower() for ln in hits), hits
    else:
        assert all(ln.strip().startswith("#") or "retired" in ln.lower() for ln in hits), hits


# ═════════════════════════════════════════════════════════════════════════════
# Task 5 — DME/adapter untouched; rebuild keyed on the graph snapshot identity
# ═════════════════════════════════════════════════════════════════════════════

from tests.memory.taxonomy.fixtures.cut5a_corpus import FIXTURE_DIR  # noqa: E402

_BASELINE = FIXTURE_DIR.parent / "cut5a_v6_baseline.json"


def _compile_fixture_v7(tmp_path, monkeypatch):
    import memory.taxonomy.compiler as C

    root = copy_fixture_root(tmp_path)
    snap = C.compile_memory_graph(root=root)  # also seats DME for this root (rebuild_sram_from_graph inside compile)
    assert snap.compiler_version == "omega.memory_compiler.v7"
    return root, snap


def test_dme_rebuilds_when_stored_hash_is_the_actual_v6_baseline(tmp_path, monkeypatch):
    """AUTHORITY: cut5a_v6_baseline.json["snapshot_hash"] (the REAL v6 hash of this
    fixture) → seeded into the stored DME state → the v7 graph snapshot differs →
    the adapter's load path fires rebuild_sram_from_graph with the v7 hash."""
    import memory.dme.adapter as A

    v6_hash = json.loads(_BASELINE.read_text(encoding="utf-8"))["snapshot_hash"]
    assert v6_hash == "5574ead5446c71f7fb15630e3221bb27f78f5413cd48161765892288fea25d84"
    root, snap = _compile_fixture_v7(tmp_path, monkeypatch)
    assert snap.snapshot_hash != v6_hash
    assert A._read_snapshot_hash(root) == snap.snapshot_hash  # the compiled graph's current identity
    # seed the stored DME state with the ACTUAL frozen v6 hash (a seat built against the pre-cut graph)
    meta_path = A.dme_meta_path(root)
    assert meta_path.exists() and A.dme_sram_path(root).exists()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["snapshot_hash"] == snap.snapshot_hash  # compile just seated it at v7
    meta["snapshot_hash"] = v6_hash
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    calls = []
    real = A.rebuild_sram_from_graph
    monkeypatch.setattr(A, "rebuild_sram_from_graph", lambda root=None, snapshot_hash=None, **k: (calls.append(snapshot_hash), real(root=root, snapshot_hash=snapshot_hash, **k))[1])
    index = A.load_sram(root)
    assert calls == [snap.snapshot_hash], calls  # rebuild fired, keyed on the v7 graph identity
    assert index.snapshot_hash == snap.snapshot_hash
    assert json.loads(meta_path.read_text(encoding="utf-8"))["snapshot_hash"] == snap.snapshot_hash


def test_dme_does_not_rebuild_when_stored_hash_matches_current(tmp_path, monkeypatch):
    """negative control: the trigger is the snapshot MISMATCH, not an unconditional rebuild"""
    import memory.dme.adapter as A

    root, snap = _compile_fixture_v7(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(A, "rebuild_sram_from_graph", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rebuild must not fire")))
    index = A.load_sram(root)
    assert index.snapshot_hash == snap.snapshot_hash and calls == []


def test_dme_is_vector_blind():
    """production memory/dme/ never reads text_768 / embeddings as memory-vector input;
    the scoring geometry receipt says so too."""
    import re

    from memory.dme.geometry import DME_SCORING_GEOMETRY

    repo = FIXTURE_DIR.parents[4]
    offenders = []
    for p in sorted((repo / "memory" / "dme").rglob("*.py")):
        text = p.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(text.splitlines(), 1):
            if "text_768" in line or re.search(r'(\[\s*["\']embeddings["\']\s*\]|\.get\(\s*["\']embeddings["\'])', line):
                offenders.append(f"{p.relative_to(repo)}:{i}: {line.strip()}")
    assert offenders == [], offenders
    assert DME_SCORING_GEOMETRY.uses_embeddings is False


def _ast_functions(source: str) -> dict:  # Cut 13 re-scope helper
    import ast

    tree = ast.parse(source)
    return {node.name: ast.dump(node) for node in tree.body if isinstance(node, ast.FunctionDef)}


def _git_show(repo, rev: str, path: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}", "show", f"{rev}:{path}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _without_dme_truncation_boundary(function_source: str) -> str:
    """Remove the one permitted recall-boundary addition before the frozen-body pin."""
    import ast

    function = ast.parse(function_source).body[0]
    assert isinstance(function, ast.FunctionDef)
    body = list(function.body)

    scored_ids = ast.parse('scored_ids = [str(hit.memory_id or "") for hit in result.hits]').body[0]
    scored_id_assignments = [node for node in body if ast.dump(node) == ast.dump(scored_ids)]
    assert len(scored_id_assignments) == 1

    allowed_boundary = ast.parse(
        '''
from memory.recall_truncation import build_truncation_receipt
truncation_receipts = list(getattr(result, "truncation_receipts", ()) or ())
truncation_receipts.append(
    build_truncation_receipt(
        "dme_adapter:filters_and_candidate_limit",
        scored_ids,
        (str(hit.get("memory_id") or "") for hit in hits),
        details={
            "candidate_limit": max(0, int(limit or 0)),
            "lobe_and_subdomain_filters_applied": True,
        },
    )
)
return DmeRecallExecution(
    tuple(hits), result, index.snapshot_hash, index.backend, tuple(truncation_receipts)
)
'''
    ).body
    assert [ast.dump(node) for node in body[-4:]] == [
        ast.dump(node) for node in allowed_boundary
    ]

    # Restore the pre-receipt return shape, then compare the entire scoring,
    # filtering, and candidate construction body against the frozen baseline.
    baseline_return = ast.parse(
        "return DmeRecallExecution(tuple(hits), result, index.snapshot_hash, index.backend)"
    ).body[0]
    function.body = [node for node in body[:-4] if node is not scored_id_assignments[0]] + [baseline_return]
    return ast.dump(function)


def test_dme_adapter_and_scorer_are_diff_empty_since_cut5a_start():
    """Cut-5A frozen gate (re-scoped by operator ruling during Cut 13, 2026-08-17): what Cut 5A
    promised is that DME SCORING, geometry, HopPlanes, the DME schema version and the legacy
    artifact-fingerprint / legacy plane-registration behaviour stay untouched, and that no
    stored-vector similarity producer returns. Cut 13 (memory-organ plan d1673531, D-13-24/37)
    lawfully ADDS generation-mode paths, pinned+leased registration and a view-based load to
    memory/dme/adapter.py + matmul_engine.py, so the former whole-file ``git diff --stat
    468c5771`` assertion is replaced by the exact invariants it protected — pinned by AST
    against 468c5771 (byte-identical function bodies), not by prose."""
    import ast
    from pathlib import Path

    repo = FIXTURE_DIR.parents[4]
    import memory.dme.adapter as A
    import memory.dme.hop_engine as H

    # v7 = HUD branch "observed-or-unknown" seat timestamps (memory/temporal_authority);
    # the scoring core pinned below is unchanged by that bump.
    assert A.DME_SCHEMA_VERSION == "omega.dme_sram.v7"
    # matmul_engine: the scoring core remains identical to 468c5771. The
    # endpoint owner is intentionally outside this frozen surface: Codex's
    # local-services migration moved packed DME from the retired worker to
    # /dme/packed/* while preserving the numpy/NPU exactness guards.
    old_me = _ast_functions(_git_show(repo, "468c5771", "memory/dme/matmul_engine.py"))
    new_me = _ast_functions(Path(repo, "memory/dme/matmul_engine.py").read_text(encoding="utf-8"))
    for name in ("_compute_overlaps_numpy", "_entries_to_float_matrix", "_query_to_float_vector", "query_popcount",
                 "active_backend", "_http", "_stat_sig", "_npu_enabled"):
        assert old_me[name] == new_me[name], f"matmul_engine.{name} changed since 468c5771"
    changed_me = {k for k in new_me if old_me.get(k) != new_me.get(k)}
    assert changed_me <= {
        "register_plane",
        "unregister_plane",
        "registered_generation_id",
        "compute_overlaps",
        "_generation_mode_active",
        "npu_service_base_url",
            "_npu_url",
            "_npu_healthy",
            "_npu_overlaps",
            "shutdown_runtime",
        }, changed_me
    matmul_source = Path(repo, "memory/dme/matmul_engine.py").read_text(encoding="utf-8")
    assert "_NPU_URL" not in matmul_source
    assert '_npu_url("packed/health")' in matmul_source
    assert '_npu_url("packed/plane")' in matmul_source
    assert '_npu_url("packed/overlap")' in matmul_source
    # adapter: geometry validation, seat packing, rebuild seating, coordinates, tokens, DME index shape
    # and the LEGACY branches of load_sram / _artifact_fingerprint are the Cut-5A surface — pinned
    old_ad = _ast_functions(_git_show(repo, "468c5771", "memory/dme/adapter.py"))
    new_ad = _ast_functions(Path(repo, "memory/dme/adapter.py").read_text(encoding="utf-8"))
    # (the recall ENTRY POINTS recall_candidates_observed / recall_verified are lawfully wrapped in the
    # Cut-13 projection-read transaction — D-13-28 — and are therefore not part of this pin)
    # DME v7 (HUD branch, 14e79873): _seat_from_dict additionally carries the observed-or-unknown
    # `temporal_timestamp` seat field. That is the only Cut-5A-surface body allowed to move, and it
    # is pinned byte-identical to the v7 baseline rather than to 468c5771.
    v7_ad = _ast_functions(_git_show(repo, "14e79873", "memory/dme/adapter.py"))
    for name in ("validate_index_geometry", "_coordinates_for_seat", "_query_coordinates", "_tokens", "_seat_from_dict",
                 "_coord_key", "_seats_for_rebuild", "_active_graph_seats", "parity_report",
                 "_recall_execution_loaded", "recall_candidates", "merge_dme_candidate_dicts", "dme_root"):
        if name == "_seat_from_dict":
            assert v7_ad[name] == new_ad.get(name), f"adapter.{name} changed since the DME v7 baseline 14e79873"
        elif name == "_recall_execution_loaded":
            original = old_ad[name]
            current_source = Path(repo, "memory/dme/adapter.py").read_text(encoding="utf-8")
            current_node = next(
                node for node in ast.parse(current_source).body
                if isinstance(node, ast.FunctionDef) and node.name == name
            )
            assert _without_dme_truncation_boundary(ast.unparse(current_node)) == original, (
                "adapter._recall_execution_loaded changed outside the truncation-receipt boundary"
            )
        elif name in old_ad:
            assert old_ad[name] == new_ad.get(name), f"adapter.{name} changed since 468c5771"
    # HopPlanes dataclass unchanged (strong / weak, both optional)
    import dataclasses
    assert [f.name for f in dataclasses.fields(H.HopPlanes)] == ["strong", "weak"]
    # no stored-vector similarity producer anywhere in the DME scorer/adapter
    for path in ("memory/dme/adapter.py", "memory/dme/matmul_engine.py"):
        src = Path(repo, path).read_text(encoding="utf-8")
        assert "SEMANTIC_SIMILARITY" not in src and "text_768" not in src, path
    # Cut 13 recall-risk work adds truncation accounting at the adapter boundary;
    # it does not alter the pinned geometry or introduce a similarity producer.
    adapter_source = Path(repo, "memory/dme/adapter.py").read_text(encoding="utf-8")
    assert "dme_adapter:filters_and_candidate_limit" in adapter_source

"""Cut 5A — structural-only graph compiler (Gate D1).

Task 1a (this file's first tests): the dedicated fixture corpus is deterministic
(regeneration is byte-identical) and carries every case the plan requires. The
compile-level assertions arrive with Task 1b (actual v6 baseline) and Task 2.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

from tests.memory.taxonomy.fixtures.cut5a_corpus import FIXTURE_DIR, FIXTURE_ROOT, copy_fixture_root, fixture_files

BASELINE_PATH = FIXTURE_DIR.parent / "cut5a_v6_baseline.json"

REQUIRED_IDS = {
    "2.00.s1", "4.02.s2", "9.01.s3", "2.01.s6", "2.02.s4", "2.03.n1", "2.04.n2",
    "4.00.n3", "4.00.n4", "2.05.k1", "2.05.k2", "2.06.k3", "2.06.k4", "1.00.p1", "6.00.j1",
}


def _load_records() -> dict[str, dict]:
    idx = json.loads((FIXTURE_ROOT / "memory" / "indexes" / "coord_index.json").read_text(encoding="utf-8"))
    return {rid: json.loads((FIXTURE_ROOT / entry["path"]).read_text(encoding="utf-8")) for rid, entry in idx.items()}


def test_cut5a_fixture_regenerates_byte_identical(tmp_path):
    """build_fixture.py is deterministic: regenerating into a scratch dir reproduces the committed bytes."""
    scratch = tmp_path / "regen"
    shutil.copytree(FIXTURE_DIR, scratch, ignore=shutil.ignore_patterns("root", "__pycache__"))
    spec = importlib.util.spec_from_file_location("cut5a_build_fixture_regen", scratch / "build_fixture.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    mod.build()
    regenerated = {p.relative_to(scratch).as_posix(): p.read_bytes() for p in sorted((scratch / "root").rglob("*.json"))}
    assert regenerated == fixture_files()


def test_cut5a_fixture_inventory_matches_plan():
    recs = _load_records()
    assert set(recs) == REQUIRED_IDS
    idx = json.loads((FIXTURE_ROOT / "memory" / "indexes" / "coord_index.json").read_text(encoding="utf-8"))
    assert set(idx) == REQUIRED_IDS and all(idx[r]["coord"] == recs[r]["coord"] for r in REQUIRED_IDS)
    vec = lambda r: (recs[r].get("embeddings") or {}).get("text_768")  # noqa: E731

    def head(v):
        return v[:256]

    # semantic group: identical normalized first-256 heads, DIFFERENT tails, 768-dim
    s = ["2.00.s1", "4.02.s2", "9.01.s3"]
    assert all(len(vec(r)) == 768 for r in s)
    assert head(vec(s[0])) == head(vec(s[1])) == head(vec(s[2]))
    assert len({tuple(vec(r)[256:]) for r in s}) == 3
    # cross-lobe: the S records share no coord / domain / >=2 tags with each other
    assert len({tuple(recs[r]["coord"]) for r in s}) == 3
    # graded / negative / degenerate vectors
    def cos(a, b):
        num = sum(x * y for x, y in zip(a, b)); na = sum(x * x for x in a) ** 0.5; nb = sum(x * x for x in b) ** 0.5
        return num / (na * nb)
    assert abs(cos(head(vec("2.01.s6")), head(vec("2.00.s1"))) - 0.8) < 1e-9
    assert cos(head(vec("2.02.s4")), head(vec("2.00.s1"))) < 0.60 and cos(head(vec("2.02.s4")), head(vec("2.01.s6"))) < 0.60
    assert len(vec("2.03.n1")) == 128 < 256
    assert all(x == 0.0 for x in vec("2.04.n2"))
    assert "embeddings" not in recs["4.00.n3"] and "embeddings" not in recs["4.00.n4"]
    # structural wiring
    assert recs["4.00.n3"]["coord"] == recs["4.00.n4"]["coord"] == [4, 0, 0]
    assert recs["4.00.n3"]["domain"] == recs["4.00.n4"]["domain"] == "dev"
    assert len(set(recs["4.00.n3"]["tags"]) & set(recs["4.00.n4"]["tags"])) >= 2
    assert recs["4.00.n4"]["links"] == ["4.00.n3"]
    assert recs["2.05.k2"]["supersedes"] == "2.05.k1" and recs["2.05.k1"]["superseded_by"] == "2.05.k2"
    assert recs["2.06.k3"]["enrichment"]["contradiction_state"] == "2.06.k4"
    facets = {e["facet"] for e in recs["1.00.p1"]["enrichment"]["entities"]}
    assert facets == {"name", "relationship", "business", "occupation", "location"}
    # non-recallable lobe carries the S vector
    assert recs["6.00.j1"]["coord"][0] == 6 and head(vec("6.00.j1")) == head(vec("2.00.s1"))


def test_cut5a_fixture_is_never_compiled_in_place():
    """no derived state may be committed with the fixture"""
    names = {p.name for p in FIXTURE_ROOT.rglob("*")}
    assert "taxonomy_store" not in names and "dme" not in names and not any(n.endswith(".sqlite") for n in names)


# ── Task 1b: the ACTUAL v6 baseline is real (TEMPORARY — retired in Task 2 once the compiler is v7) ──


def _load_baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def test_v6_baseline_is_bound_to_the_committed_fixture():
    """the baseline records the sha256 of every fixture file it was compiled from"""
    import hashlib

    b = _load_baseline()
    assert b["compiler_version"] == "omega.memory_compiler.v6" and b["base_sha"].startswith("468c5771")
    assert b["fixture_file_sha256"] == {rel: hashlib.sha256(data).hexdigest() for rel, data in fixture_files().items()}
    assert b["snapshot_hash"] == "5574ead5446c71f7fb15630e3221bb27f78f5413cd48161765892288fea25d84"
    assert b["edge_count"] == 65 == len(b["edges_sorted_by_edge_id"])
    assert b["edge_counts_by_type"]["SEMANTIC_SIMILARITY"] == 6 == b["semantic_similarity"]["count"]
    assert b["semantic_similarity"]["confidence_histogram"] == {"0.8": 3, "1.0": 3}
    assert hashlib.sha256(json.dumps(b["edges_sorted_by_edge_id"], sort_keys=True).encode("utf-8")).hexdigest() == b["edge_tuples_sha256"]


# ═════════════════════════════════════════════════════════════════════════════
# Task 2 — Gate D1: structural-only compiler v7, proved against the ACTUAL v6 baseline
# ═════════════════════════════════════════════════════════════════════════════

import sqlite3  # noqa: E402

import pytest  # noqa: E402


def _compile_fixture(tmp_path, monkeypatch, *, mutate=None):
    """copy the fixture, optionally mutate the records' `embeddings` (mutate(rec) -> None), compile, return (snap, edges, node_count)"""
    import memory.taxonomy.compiler as C
    from memory.taxonomy.paths import compiled_root

    root = copy_fixture_root(tmp_path)
    if mutate is not None:
        idx = json.loads((root / "memory" / "indexes" / "coord_index.json").read_text(encoding="utf-8"))
        for entry in idx.values():
            fp = root / entry["path"]
            rec = json.loads(fp.read_text(encoding="utf-8"))
            mutate(rec)
            fp.write_text(json.dumps(rec, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    snap = C.compile_memory_graph(root=root)
    conn = sqlite3.connect(f"file:{(compiled_root(root) / 'graph.sqlite').as_posix()}?mode=ro", uri=True)
    try:
        edges = sorted(list(r) for r in conn.execute(
            "SELECT edge_id, source_id, target_id, edge_type, evidence_event_id, confidence FROM edges").fetchall())
        nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    finally:
        conn.close()
    return snap, edges, nodes


def _drop_vectors(rec):
    rec.pop("embeddings", None)


def _mutate_vectors(rec):
    emb = rec.get("embeddings")
    if isinstance(emb, dict) and isinstance(emb.get("text_768"), list):
        # different values, same length: any vector-derived edge would change; a structural-only graph must not
        emb["text_768"] = [((i * 7919) % 1000) / 1000.0 - 0.5 for i in range(len(emb["text_768"]))]


def test_v7_edges_equal_actual_v6_baseline_minus_semantic_similarity(tmp_path, monkeypatch):
    r"""THE Gate-D1 parity proof: E_v7 == E_v6_actual \ E_SEMANTIC_SIMILARITY, every surviving
    structural tuple byte-identical, against the baseline frozen from the REAL v6 compiler."""
    b = _load_baseline()
    e_v6 = b["edges_sorted_by_edge_id"]
    e_sem = [e for e in e_v6 if e[3] == "SEMANTIC_SIMILARITY"]
    expected = sorted(e for e in e_v6 if e[3] != "SEMANTIC_SIMILARITY")
    assert len(e_v6) == 65 and len(e_sem) == 6 == b["edge_counts_by_type"]["SEMANTIC_SIMILARITY"] and len(expected) == 59
    snap, e_v7, nodes = _compile_fixture(tmp_path, monkeypatch)
    assert e_v7 == expected  # 59 tuples, all six fields, byte-identical
    assert len(e_v6) - len(e_v7) == 6
    assert not any(e[3] == "SEMANTIC_SIMILARITY" for e in e_v7)
    assert nodes == b["node_count"]  # nodes are not vector-derived


def test_v7_snapshot_hash_differs_from_actual_v6_baseline_and_version_is_v7(tmp_path, monkeypatch):
    import memory.taxonomy.compiler as C

    b = _load_baseline()
    snap, _e, _n = _compile_fixture(tmp_path, monkeypatch)
    assert snap.snapshot_hash != b["snapshot_hash"]
    assert snap.compiler_version == "omega.memory_compiler.v7" == C.COMPILER_VERSION


def test_graph_ignores_vector_fields_present_absent_mutated(tmp_path, monkeypatch):
    """Gate D1: vectors present / removed / mutated → identical v7 graph (hash + every tuple)."""
    s_present, e_present, n_present = _compile_fixture(tmp_path / "a", monkeypatch)
    s_absent, e_absent, n_absent = _compile_fixture(tmp_path / "b", monkeypatch, mutate=_drop_vectors)
    s_mutated, e_mutated, n_mutated = _compile_fixture(tmp_path / "c", monkeypatch, mutate=_mutate_vectors)
    assert s_present.snapshot_hash == s_absent.snapshot_hash == s_mutated.snapshot_hash
    assert e_present == e_absent == e_mutated and n_present == n_absent == n_mutated


def test_compiler_never_reads_embeddings(tmp_path, monkeypatch):
    """every record's `embeddings` key is a landmine: touching it raises; the compile must succeed"""
    import memory.taxonomy.compiler as C

    class _Landmine(dict):
        def __getitem__(self, key):
            if key == "embeddings":
                raise AssertionError("compiler read `embeddings`")
            return super().__getitem__(key)

        def get(self, key, default=None):
            if key == "embeddings":
                raise AssertionError("compiler read `embeddings`")
            return super().get(key, default)

    real_load = json.load
    real_loads = json.loads

    def _armed(obj):
        return _Landmine(obj) if isinstance(obj, dict) and "id" in obj and "coord" in obj else obj

    monkeypatch.setattr(C.json, "load", lambda fp, *a, **k: _armed(real_load(fp, *a, **k)))
    monkeypatch.setattr(C.json, "loads", lambda s_, *a, **k: _armed(real_loads(s_, *a, **k)))
    root = copy_fixture_root(tmp_path)
    snap = C.compile_memory_graph(root=root)  # must not raise
    assert snap.compiler_version == "omega.memory_compiler.v7"


def test_no_semantic_similarity_edges_in_v7_graph_and_no_similarity_symbols(tmp_path, monkeypatch):
    import memory.taxonomy.compiler as C

    _snap, edges, _n = _compile_fixture(tmp_path, monkeypatch)
    assert sum(1 for e in edges if e[3] == "SEMANTIC_SIMILARITY") == 0
    for name in ("_build_semantic_similarity_edges", "_legacy_rounded_cosine", "SIM_THRESHOLD", "SIM_TOPK", "MRL_DIM",
                 "_SIMILARITY_BLOCK_ROWS", "_ROUNDING_GUARD", "np"):
        assert not hasattr(C, name), name


def test_config_hash_has_structural_params_only():
    import memory.taxonomy.compiler as C

    h = C._effective_config_hash("x")
    assert "fanout=" in h and "mintags=" in h
    assert "simthr" not in h and "simk" not in h and "mrl" not in h


def test_v7_determinism_two_compiles_same_hash(tmp_path, monkeypatch):
    s1, e1, _ = _compile_fixture(tmp_path / "a", monkeypatch)
    s2, e2, _ = _compile_fixture(tmp_path / "b", monkeypatch)
    assert s1.snapshot_hash == s2.snapshot_hash and e1 == e2


def test_v7_structural_classes_all_survive(tmp_path, monkeypatch):
    _snap, edges, _n = _compile_fixture(tmp_path, monkeypatch)
    by = {}
    for e in edges:
        by[e[3]] = by.get(e[3], 0) + 1
    b = _load_baseline()
    for cls, n in b["edge_counts_by_type"].items():
        if cls == "SEMANTIC_SIMILARITY":
            assert cls not in by
        else:
            assert by.get(cls) == n, (cls, by.get(cls), n)
    with pytest.raises(AssertionError):
        assert "SEMANTIC_SIMILARITY" in by

"""Cut 13 Task A4 — Gate D2 / D3-lite parity on the frozen Cut-5A structural corpus:
the generation compile/DME seams (source_root → output_dir) reproduce the frozen v7
graph (49 nodes / 59 edges, snapshot hash a72a6835…) from a CAPTURED input, the DME
bytes equal the deterministic reference across fresh hash seeds, and the strict
membership law lawfully refuses the fixture's synthetic (schema-invalid) records at
build_generation while the same seams the builder calls prove parity."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from core import timekeeper as TK
from memory.projection import build as B
from memory.projection import generation as G
from tests.memory.projection._fixtures import redirect_timekeeper
from tests.memory.projection.dme_reference import reference_sram_bytes
from tests.memory.taxonomy.fixtures.cut5a_corpus import copy_fixture_root

REPO = Path(__file__).resolve().parents[3]
V7_SNAPSHOT_HASH = "a72a683502df6522037abaab1d95eba4a3d0bcc90ba31b811e012925245c2a97"


@pytest.fixture(autouse=True)
def _tk(tmp_path):
    redirect_timekeeper(tmp_path)
    yield
    TK.reset_for_tests()


def _generation_compile(profile: Path, out: Path):
    """The exact seams the builder calls, over the fixture as a captured input profile."""
    from memory.dme.adapter import rebuild_sram_from_graph
    from memory.taxonomy.compiler import compile_memory_graph

    out.mkdir(parents=True, exist_ok=True)
    (profile / "memory" / "dme").mkdir(parents=True, exist_ok=True)
    reg = profile / "memory" / "dme" / "coordinate_registry.json"
    if not reg.exists():
        reg.write_bytes(b"{}")
    snap = compile_memory_graph(source_root=profile, output_dir=out)
    report = rebuild_sram_from_graph(source_root=profile, output_dir=out, registry_input=reg, snapshot_hash=snap.snapshot_hash)
    return snap, report


def test_generation_seams_reproduce_the_frozen_v7_graph_from_a_captured_input(tmp_path):
    root = copy_fixture_root(tmp_path)
    out = tmp_path / "candidate"
    snap, report = _generation_compile(root, out)
    assert snap.snapshot_hash == V7_SNAPSHOT_HASH                                   # Gate D2: the frozen Cut-5A v7 hash
    conn = sqlite3.connect(f"file:{(out / 'graph.sqlite').as_posix()}?mode=ro", uri=True)
    try:
        edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        assert conn.execute("SELECT COUNT(*) FROM edges WHERE edge_type='SEMANTIC_SIMILARITY'").fetchone()[0] == 0
    finally:
        conn.close()
    assert (nodes, edges) == (49, 59)
    inner = json.loads((out / "graph_snapshot.json").read_text())
    assert inner["snapshot_hash"] == V7_SNAPSHOT_HASH
    assert not (out / "graph.sqlite-wal").exists() and not (out / "graph.sqlite-shm").exists()
    # writes landed ONLY in the output dir: the fixture's compiled/dme trees were never created
    assert not (root / "memory" / "taxonomy_store").exists()
    assert not (root / "memory" / "dme" / "sram.bin").exists()
    # DME bytes == the deterministic reference; artifacts complete
    assert (out / "sram.bin").read_bytes() == reference_sram_bytes(out)
    for name in ("sram_meta.json", "sram_index.json", "dme_manifest.json", "coordinate_registry.json", "planes/hop_plane_strong.bin", "planes/hop_plane_weak.bin"):
        assert (out / name).is_file(), name
    assert report.unscorable_coordinate_count == 0 and report.index.seat_count > 0


def test_legacy_in_place_compile_matches_the_generation_graph_hash(tmp_path):
    from memory.taxonomy.compiler import compile_memory_graph
    root = copy_fixture_root(tmp_path)
    legacy = compile_memory_graph(root=root)                                          # today's in-place path (comparator)
    assert legacy.snapshot_hash == V7_SNAPSHOT_HASH
    root2 = copy_fixture_root(tmp_path / "again")
    snap, _ = _generation_compile(root2, tmp_path / "cand")
    assert snap.snapshot_hash == legacy.snapshot_hash


CHILD = textwrap.dedent(
    """
    import hashlib, json, sys, tempfile
    from pathlib import Path
    sys.path.insert(0, %r)
    from tests.memory.projection.dme_reference import reference_sram_bytes
    from tests.memory.taxonomy.fixtures.cut5a_corpus import copy_fixture_root
    from memory.dme.adapter import rebuild_sram_from_graph
    from memory.taxonomy.compiler import compile_memory_graph
    tmp = Path(tempfile.mkdtemp()); root = copy_fixture_root(tmp); out = tmp / "cand"; out.mkdir()
    (root / "memory" / "dme").mkdir(parents=True, exist_ok=True); reg = root / "memory" / "dme" / "coordinate_registry.json"; reg.write_bytes(b"{}")
    snap = compile_memory_graph(source_root=root, output_dir=out)
    rebuild_sram_from_graph(source_root=root, output_dir=out, registry_input=reg, snapshot_hash=snap.snapshot_hash)
    files = {p.relative_to(out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.rglob("*")) if p.is_file()}
    print(json.dumps({"snap": snap.snapshot_hash, "files": files, "ref": hashlib.sha256(reference_sram_bytes(out)).hexdigest()}))
    """
)


def test_dme_bytes_equal_the_reference_across_fresh_hash_seeds_on_the_fixture():
    outs = []
    for seed in ("1", "2", "3", "999"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        proc = subprocess.run([sys.executable, "-c", CHILD % str(REPO)], env=env, check=True, capture_output=True, text=True, timeout=240)
        outs.append(json.loads(proc.stdout.strip().splitlines()[-1]))
    assert {o["snap"] for o in outs} == {V7_SNAPSHOT_HASH}
    assert len({json.dumps(o["files"], sort_keys=True) for o in outs}) == 1          # every artifact byte-identical across seeds
    assert all(o["ref"] == o["files"]["sram.bin"] for o in outs)


def test_build_generation_refuses_the_synthetic_fixture_by_the_strict_membership_law(tmp_path):
    root = copy_fixture_root(tmp_path)
    with G.projection_publication_authority(root) as auth:
        out = B.build_generation(root, reason="parity", authority=auth)
    assert isinstance(out, B.BuildRejected) and out.reason == "canonical_membership_error" and out.detail["kind"] == "schema_invalid"

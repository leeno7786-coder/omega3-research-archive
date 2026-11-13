"""Cut 13 Task A4 — determinism (D-13-21) and hash-seed independence (D-13-27/40): the same
captured state built at different wall-clock times / backends / roots / reasons yields the
same content hashes and generation_id; separate subprocesses under PYTHONHASHSEED=1,2,3,999
on a fixture that MINTS new coordinates yield identical registry / index / graph / DME /
plane bytes and id; the builder's SRAM equals the independent deterministic reference;
the untouched legacy builder varies (timestamp word) and is compared structurally only."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from core import timekeeper as TK
from memory.projection import build as B
from memory.projection import generation as G
from tests.memory.projection._fixtures import redirect_timekeeper, seed_records_only
from tests.memory.projection.dme_reference import reference_sram_bytes

CHILD = Path(__file__).with_name("_hashseed_child.py")


@pytest.fixture(autouse=True)
def _tk(tmp_path):
    redirect_timekeeper(tmp_path)
    yield
    TK.reset_for_tests()


def _build(profile):
    with G.projection_publication_authority(profile) as auth:
        cand = B.build_generation(profile, reason="determinism", authority=auth)
    assert isinstance(cand, G.Candidate), cand
    return cand


def _hashes(d: Path) -> dict:
    return {p.relative_to(d).as_posix(): G.sha256_file(p) for p in sorted(d.rglob("*")) if p.is_file()}


def test_same_captured_state_same_id_across_time_backend_root_and_reason(tmp_path, monkeypatch):
    profile = tmp_path / "profile"; profile.mkdir(); seed_records_only(profile)
    a = _build(profile)
    ha = _hashes(a.candidate_dir)
    # different wall clock (unpinned) + different reason + NPU-vs-numpy backend availability
    from memory.dme import matmul_engine as ME
    monkeypatch.setattr(ME, "active_backend", lambda *a, **k: "npu_packed")
    import memory.dme.adapter as A
    monkeypatch.setattr(A, "active_backend", lambda *a, **k: "npu_packed")
    with G.projection_publication_authority(profile) as auth:
        b = B.build_generation(profile, reason="a completely different reason", authority=auth)
    assert b.manifest.generation_id == a.manifest.generation_id and _hashes(b.candidate_dir) == ha
    # a different temp/profile root with the same captured state
    other = tmp_path / "elsewhere" / "profile"; shutil.copytree(profile, other, ignore=shutil.ignore_patterns("projection"))
    c = _build(other)
    assert c.manifest.generation_id == a.manifest.generation_id and _hashes(c.candidate_dir) == ha
    # every content hash equal
    assert c.manifest.content == a.manifest.content


def _child(profile: Path, mode: str, seed: str, out: Path) -> dict:
    env = dict(os.environ, PYTHONHASHSEED=seed, OMEGA_MEMORY_NATIVE_CITIZENSHIP="1")
    subprocess.run([sys.executable, str(CHILD), str(profile), mode, str(out)], check=True, env=env, timeout=240,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return json.loads(out.read_text())


def test_hash_seed_independence_in_separate_subprocesses(tmp_path):
    profile = tmp_path / "profile"; profile.mkdir(); seed_records_only(profile)   # no registry ⇒ every coordinate is MINTED
    results = {seed: _child(profile, "generation", seed, tmp_path / f"gen-{seed}.json") for seed in ("1", "2", "3", "999")}
    ids = {r["generation_id"] for r in results.values()}
    assert len(ids) == 1, results
    files = {json.dumps(r["files"], sort_keys=True) for r in results.values()}
    assert len(files) == 1                                                        # registry, index, graph, sram/meta/index/manifest, planes
    assert all(r["reference_sram_sha256"] == r["files"]["sram.bin"] for r in results.values())   # builder == deterministic reference
    # the untouched legacy builder under the same inputs varies in allocation/timestamp across seeds (structural only, D-13-40)
    legacy = {seed: _child(profile, "legacy", seed, tmp_path / f"legacy-{seed}.json") for seed in ("1", "2", "3", "999", "7", "42")}
    legacy_sram = {r["sram_sha256"] for r in legacy.values()}
    assert len(legacy_sram) >= 1
    # every legacy assignment set covers the same keys as the generation registry (structure), even when slots differ
    gen_keys = set(next(iter(results.values()))["registry"]["addresses"])
    for r in legacy.values():
        assert set(r["registry"]["addresses"]) == gen_keys
    # DEMONSTRATED: legacy set-order allocation is seed-dependent (>= 2 distinct registries or sram bytes across 6 seeds)
    legacy_reg = {json.dumps(r["registry"], sort_keys=True) for r in legacy.values()}
    assert len(legacy_reg) >= 2 or len(legacy_sram) >= 2, "legacy builder unexpectedly seed-invariant on this corpus"


def test_builder_sram_equals_the_deterministic_reference_and_legacy_structurally(tmp_path):
    profile = tmp_path / "profile"; profile.mkdir(); seed_records_only(profile)
    cand = _build(profile)
    d = cand.candidate_dir
    assert (d / "sram.bin").read_bytes() == reference_sram_bytes(d)
    # legacy rebuild under the SAME finalized registry: same ids/order/fingerprint/source-authority/payload; timestamp word may differ
    from memory.dme.adapter import rebuild_sram_from_graph
    from memory.dme.lobe_layout import ENTRY_WORDS
    from memory.taxonomy.compiler import compile_memory_graph
    dme = profile / "memory" / "dme"; dme.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(d / "coordinate_registry.json", dme / "coordinate_registry.json")
    compile_memory_graph(root=profile)
    legacy = rebuild_sram_from_graph(root=profile)
    gen = np.frombuffer((d / "sram.bin").read_bytes(), dtype=np.uint64).reshape(-1, ENTRY_WORDS)
    assert legacy.sram.shape == gen.shape
    assert [s["memory_id"] for s in legacy.row_index] == [s["memory_id"] for s in json.loads((d / "sram_index.json").read_text())]
    assert np.array_equal(legacy.sram[:, 0], gen[:, 0])                            # fingerprint word
    assert np.array_equal(legacy.sram[:, 2], gen[:, 2])                            # source authority word
    assert np.array_equal(legacy.sram[:, 3:], gen[:, 3:])                          # payload bitmap (same finalized registry)
    assert (dme / "coordinate_registry.json").read_bytes() == (d / "coordinate_registry.json").read_bytes()   # no new keys minted


def test_legacy_dense_fields_do_not_change_generation_identity(tmp_path):
    profile = tmp_path / "profile"; profile.mkdir(); seed_records_only(profile)
    a = _build(profile)
    # Legacy dense fields are representation-neutral and are not read by the
    # Ettin/DME generation builder.
    for mid, vec in (("k1", [0.1] * 768), ("k2", None)):
        p = profile / "memory" / "200_knowledge" / f"{mid}.json"
        doc = json.loads(p.read_text())
        if vec is None:
            doc.pop("embeddings", None)
        else:
            doc["embeddings"] = {"dim": 768, "engine": "gemma_gguf", "text_768": vec}
        p.write_text(json.dumps(doc))
    b = _build(profile)
    assert b.manifest.generation_id == a.manifest.generation_id                     # vectors present/absent/mutated ⇒ identical id
    assert b.manifest.content == a.manifest.content

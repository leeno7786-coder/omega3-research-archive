"""Program 0 P0.3 — full-rebuild parity (Gate D3-lite / 16 gate 11; D-13-40): drain_once's generation_id ==
the clean, outbox-blind build_generation id on the same canonical state; deltas present vs absent ⇒ same id;
the A′-full corpus copy (115 records) and the frozen Cut-5A fixture (schema-invalid under strict membership ⇒
the builder refuses it through the drain exactly as it refuses the clean build; parity at the compile seam is
Part A's test_generation_parity); DME sram bytes byte-identical to the deterministic REFERENCE across fresh
hash seeds through the drain; the legacy builder compared structurally only."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from core import timekeeper as TK
from memory import projection_delta as PD
from memory.projection import build as B
from memory.projection import generation as G
from memory.projection import projector as P
from tests.memory.projection._fixtures import redirect_timekeeper, seed_records_only, write_record
from tests.memory.projection.dme_reference import reference_sram_bytes

REPO = Path(__file__).resolve().parents[3]
CHILD = Path(__file__).with_name("_hashseed_child.py")
APRIME = REPO / "benchmarks" / "memory_organ" / "aprime_full_snapshot_468c5771" / "root" / "memory"
CUT5A_FIXTURE = REPO / "tests" / "memory" / "taxonomy" / "fixtures" / "cut5a_corpus"


@pytest.fixture(autouse=True)
def _env(tmp_path):
    redirect_timekeeper(tmp_path)
    yield
    TK.reset_for_tests()


def _emit(profile, mid, **kw):
    write_record(profile, mid, **kw)
    path = profile / "memory" / "200_knowledge" / f"{mid}.json"
    return PD.emit_delta_for_canonical(json.loads(path.read_text()), path, root=profile / "memory")["delta"]


def _clean_id(profile):
    with G.projection_publication_authority(profile) as auth:
        cand = B.build_generation(profile, reason="clean", authority=auth)
    assert isinstance(cand, G.Candidate), cand
    shutil.rmtree(cand.candidate_dir, ignore_errors=True)
    return cand.manifest.generation_id


def test_drain_id_equals_clean_outbox_blind_id_deltas_present_or_absent(tmp_path):
    profile = tmp_path / "p"; profile.mkdir(); seed_records_only(profile)
    _emit(profile, "k5", title="Glia", summary="glial support cells")            # deltas PRESENT
    clean = _clean_id(profile)
    rep = P.drain_once(profile, reason="parity")
    assert rep.status == "published_new" and rep.generation_id == clean and rep.deltas_seen == 1
    # same canonical state, deltas ABSENT (fresh copy without the outbox) ⇒ same id
    other = tmp_path / "q"; shutil.copytree(profile, other, ignore=shutil.ignore_patterns("projection", "indexes"))
    (other / "memory" / "indexes").mkdir(); shutil.copy(profile / "memory" / "indexes" / "coord_index.json", other / "memory" / "indexes" / "coord_index.json")
    rep2 = P.drain_once(other, reason="parity")
    assert rep2.status == "published_new" and rep2.generation_id == clean and rep2.deltas_seen == 0 and rep2.bootstrap_state == "virgin"
    # and the drain-built SRAM equals the deterministic reference
    d = G.projection_root(profile) / f"gen-{clean}"
    assert (d / "sram.bin").read_bytes() == reference_sram_bytes(d)


def _short_tmp(tmp_path: Path, request) -> Path:
    """The A′-full snapshot's deepest canonical path is 114 chars, and the builder stages the
    profile again under ``projection/work-<id>/input_profile/``. Under pytest's tmp base on a
    Windows box without LongPathsEnabled that crosses MAX_PATH (259), so this test alone
    works from the system temp root there. Everywhere else: tmp_path, unchanged."""
    if os.name != "nt":
        return tmp_path
    short = Path(tempfile.mkdtemp(prefix="ap-"))
    request.addfinalizer(lambda: shutil.rmtree(short, ignore_errors=True))
    return short


def test_aprime_full_copy_drain_vs_clean_parity_and_structural_legacy_comparison(tmp_path, request):
    tmp_path = _short_tmp(tmp_path, request)
    profile = tmp_path / "aprime"; profile.mkdir(); shutil.copytree(APRIME, profile / "memory")
    clean = _clean_id(profile)
    # a legacy Cut-4 outbox with rows for existing records (the live seat's shape) ⇒ adopted + drained ONCE ⇒ same id as the clean build
    for rec in sorted((profile / "memory" / "200_knowledge").rglob("*.json"))[:3]:
        PD.emit_delta_for_canonical(json.loads(rec.read_text()), rec, root=profile / "memory")
    rep = P.drain_once(profile, reason="parity")
    assert rep.status == "published_new" and rep.generation_id == clean and rep.full_builds == 1 and rep.deltas_seen == 3
    d = G.projection_root(profile) / f"gen-{clean}"
    assert (d / "sram.bin").read_bytes() == reference_sram_bytes(d)
    m = G.read_active_manifest(G.projection_root(profile))
    assert m.content.graph_snapshot_hash == "831b47a3058377e96207425033fcb77db2ea666dc5b4ca7fd00419ba8ef213c5"   # the Cut-5A A′-full v7 structural hash
    # legacy builder: structural comparison only (same graph hash; SRAM bytes are seed/timestamp dependent by design)
    legacy = tmp_path / "legacy"; legacy.mkdir(); shutil.copytree(APRIME, legacy / "memory")
    from memory.taxonomy.compiler import compile_memory_graph
    snap = compile_memory_graph(root=legacy)
    assert snap.snapshot_hash == m.content.graph_snapshot_hash


def test_cut5a_fixture_is_refused_by_the_drain_exactly_as_by_the_clean_builder(tmp_path):
    profile = tmp_path / "c5a"; (profile / "memory").mkdir(parents=True)
    src = CUT5A_FIXTURE / "memory" if (CUT5A_FIXTURE / "memory").is_dir() else CUT5A_FIXTURE
    for child in src.iterdir():
        (shutil.copytree if child.is_dir() else shutil.copy)(child, profile / "memory" / child.name)
    with G.projection_publication_authority(profile) as auth:
        clean = B.build_generation(profile, reason="clean", authority=auth)
    rep = P.drain_once(profile, reason="parity")
    if isinstance(clean, B.BuildRejected):
        assert clean.reason == "canonical_membership_error" and rep.status == "failed" and rep.detail["reason"] == "canonical_membership_error"
        assert not rep.cursor_settled and not (G.projection_root(profile) / G.MANIFEST_FILE).exists()
    else:  # pragma: no cover - the fixture becomes schema-valid one day
        assert rep.generation_id == clean.manifest.generation_id


def test_drain_generation_is_hash_seed_independent_and_equals_the_reference(tmp_path):
    profile = tmp_path / "p"; profile.mkdir(); seed_records_only(profile)
    _emit(profile, "k5", title="Glia", summary="glial support cells")
    results = []
    for seed in ("1", "2", "3", "999"):
        out = tmp_path / f"out-{seed}.json"
        # Keep the Windows temp variables so the child does not fall back to the repo root.
        env = dict(os.environ, PYTHONHASHSEED=seed, OMEGA_MEMORY_NATIVE_CITIZENSHIP="1", PATH="/usr/bin:/bin")
        proc = subprocess.run([sys.executable, str(CHILD), str(profile), "drain", str(out)], capture_output=True, text=True, timeout=600, env=env)
        assert proc.returncode == 0, proc.stderr[-1500:]
        results.append(json.loads(out.read_text()))
    ids = {r["generation_id"] for r in results}
    assert len(ids) == 1 and all(r["status"] == "published_new" for r in results)
    assert len({r["files"]["sram.bin"] for r in results}) == 1 and all(r["files"]["sram.bin"] == r["reference_sram_sha256"] for r in results)
    assert len({json.dumps(r["files"], sort_keys=True) for r in results}) == 1                       # every artifact byte-identical across seeds
    assert ids == {_clean_id(profile)}
    assert hashlib.sha256(b"").hexdigest() == P.ZERO_SHA256

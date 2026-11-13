"""Cut 13 Task A6 — Gate D2 end-to-end + failed-build-leaves-G-active + crash/torn matrix on the
REAL builder + publisher (D-13-2/16/30/41/54): mixed candidates rejected before the swap; a
build failure leaves G active and cleans up; torn temp manifests are ignored; crash intervals
resume; publish-lock contention refuses; legacy fixed-name files are retained but never
authority once a manifest exists; a corrupt ACTIVE manifest fails loud (no rollback pointer)
and the next publish restores; a canonical write during capture rejects + retries."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from core import timekeeper as TK
from memory.dme import adapter as A
from memory.dme import recall_hook as RH
from memory.projection import build as B
from memory.projection import generation as G
from memory.taxonomy import graph_store as GS
from tests.memory.projection._fixtures import build_and_publish, redirect_timekeeper, seed_legacy_profile, seed_records_only, write_record


@pytest.fixture(autouse=True)
def _tk(tmp_path):
    redirect_timekeeper(tmp_path)
    RH.force_unload_all_sessions()
    yield
    RH.force_unload_all_sessions()
    G._crash_hook = None
    TK.reset_for_tests()


def _profile(tmp_path, *, legacy=False):
    profile = tmp_path / "profile"; profile.mkdir()
    (seed_legacy_profile if legacy else seed_records_only)(profile)
    return profile


def _crash_at(point):
    def hook(p):
        if p == point:
            raise G.SimulatedCrash(point)
    G._crash_hook = hook


def test_1_mixed_generation_candidate_rejected_before_the_pointer_swap(tmp_path):
    profile = _profile(tmp_path)
    c1, r1 = build_and_publish(profile)
    write_record(profile, "k5", title="Glia", summary="glial support cells")
    proj = G.projection_root(profile)
    with G.projection_publication_authority(profile) as auth:
        c2 = B.build_generation(profile, reason="mixed", authority=auth)
        # assemble a MIXED candidate: G+1's graph with G's DME meta
        shutil.copyfile(proj / f"gen-{r1.generation_id}" / "sram_meta.json", c2.candidate_dir / "sram_meta.json")
        with pytest.raises(G.GenerationIntegrityError):
            G.publish_generation(profile, c2, authority=auth)                        # sha / graph-hash mismatch ⇒ rejected
    assert G.read_active_manifest(proj).generation_id == r1.generation_id            # G active
    assert not c2.candidate_dir.exists()


def test_2_build_raising_mid_dme_leaves_active_unchanged_and_cleans_up(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    c1, r1 = build_and_publish(profile)
    proj = G.projection_root(profile)
    import memory.dme.adapter as AD
    monkeypatch.setattr(AD, "_rebuild_generation", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mid-DME failure")))
    write_record(profile, "k5", title="Glia", summary="glial support cells")
    with G.projection_publication_authority(profile) as auth:
        with pytest.raises(RuntimeError):
            B.build_generation(profile, reason="fail", authority=auth)
        rep = G.recover_generation_intents(profile, authority=auth)
    assert G.read_active_manifest(proj).generation_id == r1.generation_id
    assert not any(p.name.startswith("work-") or p.name.endswith(".candidate") for p in proj.iterdir())
    G.validate_published_generation(proj, G.read_active_manifest(proj))


def test_3_torn_temp_manifest_is_ignored(tmp_path):
    profile = _profile(tmp_path)
    c1, r1 = build_and_publish(profile)
    proj = G.projection_root(profile)
    (proj / "manifest.json.tmp").write_bytes(b'{"schema_version": "omega.projection_manifest.v1", "generation_id": "')
    (proj / ".manifest.json.abc123.tmp").write_bytes(b"{torn")
    assert G.read_active_manifest(proj).generation_id == r1.generation_id
    with G.projection_read_transaction(profile) as ctx:
        assert ctx.mode == "generation" and ctx.view.generation_id == r1.generation_id
    G.validate_published_generation(proj, G.read_active_manifest(proj))


def test_4_crash_intervals_with_and_without_live_intent(tmp_path):
    profile = _profile(tmp_path)
    c1, r1 = build_and_publish(profile)
    proj = G.projection_root(profile)
    write_record(profile, "k5", title="Glia", summary="glial support cells")
    # crash between rename and the publication row WITH a live intent ⇒ resume the original published_new
    _crash_at("after_rename")
    with G.projection_publication_authority(profile) as auth:
        c2 = B.build_generation(profile, reason="x", authority=auth)
        with pytest.raises(G.SimulatedCrash):
            G.publish_generation(profile, c2, authority=auth)
    G._crash_hook = None
    assert G.read_active_manifest(proj).generation_id == r1.generation_id
    with G.projection_publication_authority(profile) as auth:
        rep = G.recover_generation_intents(profile, authority=auth)
    assert rep.status == "completed" and G.read_active_manifest(proj).generation_id == c2.manifest.generation_id
    assert G.read_publication_lineage(proj / G.LINEAGE_FILE).head.event == "published_new"     # never reclassified
    # WITHOUT a live intent: a receipted orphan is retained for the next publish, never GC'd immediately
    write_record(profile, "k6", title="Astro", summary="astrocytes")
    with G.projection_publication_authority(profile) as auth:
        c3 = B.build_generation(profile, reason="x", authority=auth)
        receipt = G.GenerationReceipt(candidate=c3.manifest, coverage_clock=c3.coverage_clock, source_input_origins=c3.source_input_origins)
        (c3.candidate_dir / G.RECEIPT_FILE).write_bytes(G.receipt_bytes(receipt))
        orphan_dir = proj / f"gen-{c3.manifest.generation_id}"
        c3.candidate_dir.rename(orphan_dir)
        rep = G.recover_generation_intents(profile, authority=auth)
    assert rep.status == "clean" and orphan_dir.is_dir()
    assert G.read_active_manifest(proj).generation_id == c2.manifest.generation_id
    c4, r4 = build_and_publish(profile)                                                  # same id ⇒ adopted from its receipt
    assert r4.outcome == "adopted_orphan" and r4.generation_id == c3.manifest.generation_id
    # crash between the publication row and the manifest ⇒ recovery completes the swap to the head's dir
    write_record(profile, "k7", title="Micro", summary="microglia")
    _crash_at("after_row")
    with G.projection_publication_authority(profile) as auth:
        c5 = B.build_generation(profile, reason="x", authority=auth)
        with pytest.raises(G.SimulatedCrash):
            G.publish_generation(profile, c5, authority=auth)
    G._crash_hook = None
    head = G.read_publication_lineage(proj / G.LINEAGE_FILE).head
    assert head.generation_id == c5.manifest.generation_id and (proj / f"gen-{head.generation_id}").is_dir()
    with G.projection_publication_authority(profile) as auth:
        G.recover_generation_intents(profile, authority=auth)
    m = G.read_active_manifest(proj)
    assert m.generation_id == c5.manifest.generation_id and m.coverage_clock == head.coverage_clock
    G.validate_published_generation(proj, m)


def test_5_publish_lock_contention_refuses(tmp_path):
    profile = _profile(tmp_path)
    with G.projection_publication_authority(profile):
        with pytest.raises(G.PublicationAuthorityBusy):
            with G.projection_publication_authority(profile):
                pass


def test_6_legacy_fixed_name_files_retained_but_manifest_is_the_only_authority(tmp_path):
    profile = _profile(tmp_path, legacy=True)                                             # legacy fixed-name files exist
    mem = profile / "memory"
    from memory.taxonomy import paths as P
    legacy_files = [P.compiled_root(profile) / "latest_snapshot_hash.txt", mem / "dme" / "sram.bin", mem / "dme" / "coordinate_registry.json"]
    for f in legacy_files:
        assert f.exists()
    before = {f: f.read_bytes() for f in legacy_files}
    c1, r1 = build_and_publish(profile)                                                   # migration publish
    proj = G.projection_root(profile)
    for f, data in before.items():
        assert f.exists() and f.read_bytes() == data                                       # RETAINED, never GC'd (D-13-30)
    (P.compiled_root(profile) / "latest_snapshot_hash.txt").write_text("stale-legacy")
    (mem / "dme" / "sram.bin").write_bytes(b"\x00" * 64)
    with G.projection_read_transaction(profile) as ctx:                                  # readers use the manifest ONLY
        assert GS.graph_path(profile) == ctx.view.graph_sqlite
        assert A.dme_sram_path(profile) == ctx.view.sram
        assert A._read_snapshot_hash(profile) == r1.active_manifest.content.graph_snapshot_hash
        idx = A.load_sram(root=profile)
        assert idx.seat_count == 2
    build_and_publish(profile); build_and_publish(profile)                                # GC runs; legacy files still retained
    for f in legacy_files:
        assert f.exists()


def test_7_corrupt_active_manifest_fails_loud_and_the_next_publish_restores(tmp_path):
    profile = _profile(tmp_path)
    c1, r1 = build_and_publish(profile)
    proj = G.projection_root(profile)
    (proj / G.MANIFEST_FILE).write_bytes(b"{corrupt manifest bytes")
    with pytest.raises(G.GenerationIntegrityError):
        G.read_active_manifest(proj)
    with G.projection_read_transaction(profile) as ctx:
        assert ctx.mode == "unavailable"                                                    # lane unavailable, never legacy
        with pytest.raises(G.GenerationUnavailable):
            GS.graph_path(profile)
    from memory.projection.generation import projection_health
    assert projection_health(profile).semantic_dme_lane == "unavailable"
    assert not (proj / "manifest.json.prev").exists()                                       # no rollback pointer (D-13-16)
    write_record(profile, "k5", title="Glia", summary="glial support cells")
    c2, r2 = build_and_publish(profile)                                                     # next publish restores
    assert r2.outcome == "published_new"
    G.validate_published_generation(proj, G.read_active_manifest(proj))
    with G.projection_read_transaction(profile) as ctx:
        assert ctx.mode == "generation" and ctx.view.generation_id == r2.generation_id


def test_8_canonical_write_during_capture_rejects_and_retries_end_to_end(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    from memory.taxonomy import compiler as C
    real = C.compile_memory_graph
    n = {"i": 0}

    def compile_then_write(**kw):
        n["i"] += 1
        if n["i"] == 1:
            write_record(profile, "k5", title="Glia", summary="glial support cells")
        return real(**kw)

    monkeypatch.setattr(C, "compile_memory_graph", compile_then_write)
    c1, r1 = build_and_publish(profile)
    assert n["i"] == 2 and r1.outcome == "published_new"
    seats = [s["memory_id"] for s in json.loads((G.projection_root(profile) / f"gen-{r1.generation_id}" / "sram_index.json").read_text())]
    assert "k5" in seats

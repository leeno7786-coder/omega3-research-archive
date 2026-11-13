"""Cut 13 Task A3 — path resolution through views (D-13-2/19): no manifest ⇒ today's
fixed names (byte-identical strings); manifest + ambient view ⇒ the view's paths;
manifest + NO ambient view ⇒ GenerationViewRequired; NPU registration pinned + leased
(D-13-24/37) — the ONLY generation-mode behaviour of the scorer seam."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from memory.dme import adapter as A
from memory.dme import coordinate_registry as CR
from memory.dme import hop_engine as H
from memory.dme import matmul_engine as ME
from memory.projection import generation as G
from memory.taxonomy import graph_store as GS
from memory.taxonomy import paths as P
from tests.memory.projection._fixtures import clock, publish_from_legacy, seed_legacy_profile

HELPERS = [
    ("graph_path", lambda root: GS.graph_path(root), lambda root: P.compiled_root(root) / "graph.sqlite", "graph.sqlite"),
    ("dme_sram_path", lambda root: A.dme_sram_path(root), lambda root: A.dme_root(root) / "sram.bin", "sram.bin"),
    ("dme_meta_path", lambda root: A.dme_meta_path(root), lambda root: A.dme_root(root) / "sram_meta.json", "sram_meta.json"),
    ("dme_row_index_path", lambda root: A.dme_row_index_path(root), lambda root: A.dme_root(root) / "sram_index.json", "sram_index.json"),
    ("dme_manifest_path", lambda root: A.dme_manifest_path(root), lambda root: A.dme_root(root) / "manifest.json", "dme_manifest.json"),
    ("registry_path", lambda root: CR.registry_path(root), lambda root: CR.legacy_registry_path(root), "coordinate_registry.json"),
    ("hop_plane_strong_path", lambda root: H.hop_plane_strong_path(root), lambda root: A.dme_root(root) / "hop_plane_strong.bin", "planes/hop_plane_strong.bin"),
    ("hop_plane_weak_path", lambda root: H.hop_plane_weak_path(root), lambda root: A.dme_root(root) / "hop_plane_weak.bin", "planes/hop_plane_weak.bin"),
]


@pytest.fixture(autouse=True)
def _reset_scorer():
    ME.unregister_plane()
    yield
    ME.unregister_plane()


@pytest.mark.parametrize("name,helper,legacy,view_rel", HELPERS, ids=[h[0] for h in HELPERS])
def test_every_helper_resolves_legacy_view_or_refuses(tmp_path, name, helper, legacy, view_rel):
    profile = tmp_path / "profile"; profile.mkdir()
    (profile / "memory").mkdir()
    # no manifest ⇒ today's fixed name, byte-identical string
    assert str(helper(profile)) == str(legacy(profile))
    seed_legacy_profile(profile)
    r = publish_from_legacy(profile, clock(100))
    gen_dir = G.projection_root(profile) / f"gen-{r.generation_id}"
    # manifest + no ambient view ⇒ refused (no unleased Path)
    with pytest.raises(G.GenerationViewRequired):
        helper(profile)
    # manifest + ambient view ⇒ the view's path
    with G.projection_read_transaction(profile):
        assert helper(profile) == gen_dir / view_rel
    # after the transaction: refused again
    with pytest.raises(G.GenerationViewRequired):
        helper(profile)


def test_integrity_dme_audit_opens_projection_transaction(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from memory.dme import adapter as DME
    from memory.integrity.audit import _dme_parity_audit
    from memory.projection.generation import current_read_context

    read_contexts = []
    roots = []

    def _parity_report(*, root):
        roots.append(root)
        read_contexts.append(current_read_context())
        return SimpleNamespace(status="ok", missing_in_dme=(), orphan_in_dme=())

    monkeypatch.setattr(DME, "parity_report", _parity_report)

    result = _dme_parity_audit(root=tmp_path)

    assert result == ("ok", (), ())
    assert roots == [tmp_path]
    assert read_contexts[0] is not None


def test_connect_for_read_is_legacy_connect_without_manifest_and_readonly_with_view(tmp_path):
    profile = tmp_path / "profile"; profile.mkdir()
    seed_legacy_profile(profile)
    conn = GS.connect_for_read(profile)          # legacy: today's connect (WAL + schema)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    conn.close()
    r = publish_from_legacy(profile, clock(100))
    with pytest.raises(G.GenerationViewRequired):
        GS.connect_for_read(profile)
    with G.projection_read_transaction(profile) as ctx:
        conn = GS.connect_for_read(profile)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        with pytest.raises(Exception):            # read-only: INSERT raises
            conn.execute("INSERT INTO recall_log (query_hash) VALUES ('x')")
        assert conn.execute("SELECT count(*) FROM nodes").fetchone()[0] > 0
        conn.close()
        gen_dir = ctx.view.generation_dir
        assert not (gen_dir / "graph.sqlite-wal").exists() and not (gen_dir / "graph.sqlite-shm").exists()


def test_snapshot_hash_readers_follow_the_manifest(tmp_path):
    from memory.taxonomy import graph_diff, pressure_signals, recall as TR
    profile = tmp_path / "profile"; profile.mkdir()
    seed_legacy_profile(profile)
    legacy_hash = A._read_snapshot_hash(profile)
    r = publish_from_legacy(profile, clock(100))
    (P.compiled_root(profile) / "latest_snapshot_hash.txt").write_text("stale-legacy-marker")   # legacy marker goes stale
    assert A._read_snapshot_hash(profile) == legacy_hash == r.active_manifest.content.graph_snapshot_hash
    assert TR._read_snapshot_hash(profile) == legacy_hash
    assert pressure_signals._read_snapshot_hash(profile) == legacy_hash
    assert graph_diff._stored_snapshot_hash(profile) == legacy_hash


def test_register_plane_is_pinned_and_leased_under_generation_mode(tmp_path, monkeypatch):
    profile = tmp_path / "profile"; profile.mkdir()
    seed_legacy_profile(profile)
    r = publish_from_legacy(profile, clock(100))
    proj = G.projection_root(profile)
    import core.paths as core_paths
    monkeypatch.setattr(core_paths, "MEMORY_ROOT", profile / "memory")      # the live root IS this profile
    # the legacy un-pinned signature is refused under generation mode
    with pytest.raises(G.GenerationIntegrityError):
        ME.register_plane(proj / f"gen-{r.generation_id}" / "sram.bin", 2)
    with G.projection_read_transaction(profile) as ctx:
        idx = A.load_sram(root=profile)                                       # registers pinned + leased
        assert ME.registered_generation_id() == r.generation_id
        assert G.generation_is_leased(proj, r.generation_id)
        # scoring a DIFFERENT generation's view against this registration is refused
        with pytest.raises(G.GenerationIntegrityError):
            ME.compute_overlaps(idx.sram, idx.sram[0], generation_id="f" * 64)
        # scoring the registered generation is lawful (numpy fallback here)
        out = ME.compute_overlaps(idx.sram, idx.sram[0], generation_id=r.generation_id)
        assert out.shape == (idx.seat_count,)
    # the scorer's lease outlives the read transaction until unregister_plane
    assert G.generation_is_leased(proj, r.generation_id)
    assert ME.unregister_plane(r.generation_id) is True
    assert not G.generation_is_leased(proj, r.generation_id)
    assert ME.unregister_plane(r.generation_id) is False


def test_two_leased_views_scorer_never_scores_G_against_G1_registration(tmp_path, monkeypatch):
    profile = tmp_path / "profile"; profile.mkdir()
    seed_legacy_profile(profile)
    r1 = publish_from_legacy(profile, clock(100))
    import core.paths as core_paths
    monkeypatch.setattr(core_paths, "MEMORY_ROOT", profile / "memory")
    with G.projection_read_transaction(profile) as ctx1:
        idx1 = A.load_sram(root=profile)
        # publish G+1: a DIFFERENT generation (variant dme manifest bytes) while G's view is alive
        r2 = publish_from_legacy(profile, clock(101), variant="g-plus-one")
        assert r2.generation_id != r1.generation_id
        # a second, independent view on G+1 (emulating another thread/context) loads and re-registers the scorer to G+1
        token = G._ambient.set(None)
        try:
            with G.projection_read_transaction(profile) as ctx2:
                assert ctx2.view.generation_id == r2.generation_id
                idx2 = A.load_sram(root=profile)
                assert ME.registered_generation_id() == r2.generation_id
                # G's view must not be scored against G+1's registration
                with pytest.raises(G.GenerationIntegrityError):
                    ME.compute_overlaps(idx1.sram, idx1.sram[0], generation_id=r1.generation_id)
                ME.compute_overlaps(idx2.sram, idx2.sram[0], generation_id=r2.generation_id)
        finally:
            G._ambient.reset(token)
        assert G.current_view() is ctx1.view and ctx1.view.generation_id == r1.generation_id
    ME.unregister_plane()

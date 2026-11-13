"""Cut 13 Task A3 — read-side immutability (D-13-3) and the adversarial mid-read publish
(D-13-2 proof) on the REAL DME session / graph readers: under generation mode readers
never repair, rebuild or write; a corrupt/missing artifact fails LOUD (never empty
neighbors); an in-flight operation stays on G even when G+1 publishes mid-call."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memory.dme import adapter as A
from memory.dme import hop_engine as H
from memory.dme import recall_hook as RH
from memory.dme.coordinate_registry import load_registry
from memory.projection import generation as G
from memory.taxonomy import graph_store as GS
from memory.taxonomy import recall_graph as RG
from tests.memory.projection._fixtures import clock, dir_state, publish_from_legacy, seed_legacy_profile


@pytest.fixture(autouse=True)
def _sessions():
    RH.force_unload_all_sessions()
    yield
    RH.force_unload_all_sessions()
    from memory.dme import matmul_engine as ME
    ME.unregister_plane()


def _profile(tmp_path):
    profile = tmp_path / "profile"; profile.mkdir()
    seed_legacy_profile(profile)
    r = publish_from_legacy(profile, clock(100))
    return profile, r, G.projection_root(profile) / f"gen-{r.generation_id}"


def test_load_sram_from_view_never_rebuilds_and_fails_loud_on_corruption(tmp_path, monkeypatch):
    profile, r, gen_dir = _profile(tmp_path)
    called = []
    monkeypatch.setattr(A, "rebuild_sram_from_graph", lambda *a, **k: called.append(1) or (_ for _ in ()).throw(AssertionError("rebuild called")))
    with G.projection_read_transaction(profile):
        idx = A.load_sram(root=profile)
        assert idx.seat_count == 2 and idx.snapshot_hash == r.active_manifest.content.graph_snapshot_hash
        before = dir_state(gen_dir)
        (gen_dir / "sram.bin").write_bytes(b"\x00" * 8)
        with pytest.raises(G.GenerationIntegrityError):
            A.load_sram(root=profile)
        (gen_dir / "sram.bin").unlink()
        with pytest.raises(G.GenerationIntegrityError):
            A.load_sram(root=profile)
        (gen_dir / "graph_snapshot.json").write_bytes(b"{}")
        with pytest.raises(G.GenerationIntegrityError):
            G.current_view().read_bytes("graph_snapshot.json")
        assert not called
        # nothing created or modified by any reader (only our own tampering differs)
        after = dir_state(gen_dir)
        assert set(after) <= set(before) | set()                       # no new file
        assert {k for k in after if k not in ("graph_snapshot.json",)} == {k for k in before if k not in ("sram.bin", "graph_snapshot.json")}


def test_hop_planes_from_view_never_written_and_missing_plane_fails_loud(tmp_path, monkeypatch):
    profile, r, gen_dir = _profile(tmp_path)
    monkeypatch.setattr(H, "build_hop_planes", lambda *a, **k: (_ for _ in ()).throw(AssertionError("build called")))
    with G.projection_read_transaction(profile):
        idx = A.load_sram(root=profile)
        planes = H.load_or_build_hop_planes(profile, idx.sram, idx.row_index)
        assert planes.strong.shape == idx.sram.shape
        (gen_dir / "planes" / "hop_plane_weak.bin").unlink()
        with pytest.raises(G.GenerationIntegrityError):
            H.load_or_build_hop_planes(profile, idx.sram, idx.row_index)
        assert not (gen_dir / "planes" / "hop_plane_weak.bin").exists()  # never re-created


def test_dme_session_error_escapes_and_readers_write_nothing(tmp_path):
    profile, r, gen_dir = _profile(tmp_path)
    (gen_dir / "sram_meta.json").write_bytes(b"{}")
    with G.projection_read_transaction(profile):
        before = dir_state(gen_dir)
        with pytest.raises(G.GenerationIntegrityError):                # NOT DmeLoadError, NOT empty
            with RH.dme_recall_session(profile):
                pass
        assert dir_state(gen_dir) == before


def test_graph_readers_fail_loud_not_empty_and_registry_is_strict_readonly(tmp_path):
    profile, r, gen_dir = _profile(tmp_path)
    with G.projection_read_transaction(profile):
        # healthy: expand works over the published graph
        out = RG.expand_via_graph(["k1"], root=profile, depth=1)
        assert isinstance(out, list)
        (gen_dir / "graph.sqlite").write_bytes(b"not a database at all")
        with pytest.raises(G.GenerationIntegrityError):
            RG.expand_via_graph(["k1"], root=profile, depth=1)          # escapes the reader's generic except
        with pytest.raises(G.GenerationIntegrityError):
            RG._graph_blocked_memory_ids(root=profile)
        with pytest.raises(G.GenerationIntegrityError):
            H.classed_neighbor_maps_from_graph(profile, ["k1"])
        with pytest.raises(G.GenerationIntegrityError):
            H.neighbor_map_from_graph(profile, ["k1"])
        # registry: read-only + strict
        reg = load_registry(profile)
        with pytest.raises(G.GenerationIntegrityError):
            reg.register("brand-new-token", 2)
        (gen_dir / "coordinate_registry.json").write_bytes(b"{malformed")
        from memory.dme import coordinate_registry as CR
        CR._VIEW_REGISTRY_CACHE.clear()
        with pytest.raises(G.GenerationIntegrityError):                # never "empty"
            load_registry(profile)


def test_adversarial_mid_read_publish_dme_session_and_graph_stay_on_G(tmp_path, monkeypatch):
    profile, r1, gen_dir = _profile(tmp_path)
    proj = G.projection_root(profile)
    published = {}

    def publish_g_plus_one():
        if not published:
            published["r2"] = publish_from_legacy(profile, clock(101), variant="mid-read")

    # publish G+1 between the graph access and the DME/registry access of ONE operation
    real_connect = GS.connect_for_read
    opened: list[Path] = []

    def spy_connect(root=None):
        conn = real_connect(root)
        opened.append(Path(conn.execute("PRAGMA database_list").fetchone()[2]))
        publish_g_plus_one()                                            # G+1 publishes right after the graph opened
        return conn

    monkeypatch.setattr(GS, "connect_for_read", spy_connect)
    with G.projection_read_transaction(profile) as ctx:
        RG.expand_via_graph(["k1"], root=profile, depth=1)             # graph access → publish → ...
        assert published and published["r2"].generation_id != r1.generation_id
        with RH.dme_recall_session(profile) as session:                # ... DME + registry access still on G
            assert session.generation_id == r1.generation_id
            assert G.current_view() is ctx.view
            reg = load_registry(profile)
            assert reg._path == gen_dir / "coordinate_registry.json"
            idx = A.load_sram(root=profile)
            assert idx.snapshot_hash == r1.active_manifest.content.graph_snapshot_hash
        assert opened and opened[0] == gen_dir / "graph.sqlite"
    RH.force_unload_all_sessions()
    with G.projection_read_transaction(profile) as ctx2:               # a NEW operation is on G+1
        assert ctx2.view.generation_id == published["r2"].generation_id
        with RH.dme_recall_session(profile) as session:
            assert session.generation_id == published["r2"].generation_id


def test_legacy_behaviour_unchanged_without_manifest(tmp_path):
    profile = tmp_path / "profile"; profile.mkdir()
    seed_legacy_profile(profile)
    with RH.dme_recall_session(profile) as session:
        assert session.generation_id is None and session.seat_count == 2
    with G.projection_read_transaction(profile) as ctx:
        assert ctx.mode == "legacy"
        assert RG.expand_via_graph(["k1"], root=profile, depth=1) is not None

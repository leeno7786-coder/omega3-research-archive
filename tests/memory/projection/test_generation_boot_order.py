"""Boot recovers canonical writes, replays generation intent, then invokes Program 0."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import core.paths as core_paths
from core import timekeeper as TK
from core.required_substrate import RequiredSubstrateError
from memory.projection import dispatch as D
from memory.projection import generation as G
from memory.projection import projector as P
from tests.memory.projection._fixtures import redirect_timekeeper, seed_records_only


@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    from memory import retina
    from tests.memory.projection.test_generation_semantic_retina import CountingEncoder

    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(core_paths, "OMEGA_ROOT", profile)
    monkeypatch.setattr(core_paths, "MEMORY_ROOT", profile / "memory")
    # These tests prove boot ordering over real artifacts, not model loading.
    # A required encoder failure must no longer be silently ignored by builds.
    encoder = CountingEncoder()
    monkeypatch.setattr(retina, "encoder", lambda: encoder)
    retina.unload()
    redirect_timekeeper(tmp_path)
    yield profile
    D.release_lifecycle_lock()
    retina.unload()
    TK.reset_for_tests()


def _boot_module():
    import boot

    return boot


def _observe_order(monkeypatch, order: list[str]) -> None:
    import memory.encoding_journal as EJ

    monkeypatch.setattr(EJ, "recover_unclosed", lambda _rollback: order.append("canonical-recovery") or [])
    original_replay = D.recover_generation_intents
    original_drain = D.run_program0_locked

    def replay(*args, **kwargs):
        order.append("generation-replay")
        return original_replay(*args, **kwargs)

    def drain(*args, **kwargs):
        order.append("program0-drain")
        return original_drain(*args, **kwargs)

    monkeypatch.setattr(D, "recover_generation_intents", replay)
    monkeypatch.setattr(D, "run_program0_locked", drain)


def test_boot_recovery_precedes_generation_replay_and_program0_publication(profile_env, monkeypatch):
    profile = profile_env
    seed_records_only(profile)
    order: list[str] = []
    _observe_order(monkeypatch, order)

    report: dict = {}
    _boot_module()._boot_projection_and_recovery(report)

    assert order == ["canonical-recovery", "generation-replay", "program0-drain"]
    assert report["projection_owner"] == "program0"
    assert report["projection"] == "program0:published"
    assert report["graph"] == "up"
    proj = G.projection_root(profile)
    active = G.read_active_manifest(proj)
    G.validate_published_generation(proj, active)
    assert P.read_cursor(proj).generation_id == active.generation_id


def test_open_obligation_blocks_boot_publication_and_recovered_state_is_in_first_generation(
    profile_env, monkeypatch
):
    profile = profile_env
    seed_records_only(profile)
    import memory.encoding_journal as EJ

    def recover(_rollback):
        for record in (profile / "memory").rglob("k4.json"):
            record.unlink()
        return [{"id": "k4"}]

    monkeypatch.setattr(EJ, "recover_unclosed", recover)
    monkeypatch.setattr(EJ, "journal_status", lambda: {"clean": False, "unclosed_count": 1})
    report: dict = {}
    with pytest.raises(RequiredSubstrateError) as raised:
        _boot_module()._boot_projection_and_recovery(report)

    assert raised.value.component == "memory_projection"
    assert raised.value.operation == "dispatch_execution"
    assert raised.value.reason == "blocked_on_obligations"
    assert report["projection"] == "program0:failed"
    assert not (G.projection_root(profile) / G.MANIFEST_FILE).exists()

    monkeypatch.setattr(EJ, "journal_status", lambda: {"clean": True})
    report = {}
    _boot_module()._boot_projection_and_recovery(report)
    assert report["projection"] == "program0:published"
    view = G.open_generation_view(profile / "memory")
    try:
        index = json.loads(view.read_bytes("coord_index.snapshot.json").decode("utf-8"))
        assert "k4" not in index
        assert set(index) == {"k1", "k2", "k3"}
    finally:
        view.close()


def test_reindex_and_compile_is_also_owned_by_program0(profile_env):
    profile = profile_env
    seed_records_only(profile)
    from memory.engine.reindex import reindex_and_compile
    from memory.taxonomy import paths as taxonomy_paths

    snapshot = reindex_and_compile(root=profile)
    manifest = G.read_active_manifest(G.projection_root(profile))
    assert snapshot.snapshot_hash == manifest.content.graph_snapshot_hash
    assert not (taxonomy_paths.compiled_root(profile) / "graph.sqlite").exists()
    assert not (profile / "memory" / "dme" / "sram.bin").exists()

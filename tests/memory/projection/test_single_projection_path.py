"""The published generation is the one projection path and Program 0 owns writes."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from core import timekeeper as TK
from memory.projection import dispatch as D
from memory.projection import generation as G
from tests.memory.projection._fixtures import redirect_timekeeper, seed_records_only

REPO = Path(__file__).resolve().parents[3]
OWNER_FLAGS = ("OMEGA_PROJECTION_GENERATIONS", "OMEGA_PROJECTION_DRAIN")
PRODUCTION_TREES = (
    "auto_loop", "autonomic", "browser_agent", "cognition", "config", "core", "deepsleep_loop",
    "global_loop", "guardian", "hud", "knowledge_extractor", "mcp_platform", "memory",
    "meta_loop", "omega_loop", "organ", "plugin_platform", "policy", "pressure_loop",
    "runtime", "schema", "scripts", "sensory", "skills",
)


@pytest.fixture(autouse=True)
def _isolated_timekeeper(tmp_path):
    redirect_timekeeper(tmp_path)
    D.release_lifecycle_lock()
    yield
    D.release_lifecycle_lock()
    TK.reset_for_tests()


def _profile(tmp_path) -> Path:
    profile = tmp_path / "profile"
    profile.mkdir()
    seed_records_only(profile)
    return profile


def test_dispatch_always_runs_program0_and_ignores_owner_environment(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    for generations, drain in (("0", "0"), ("1", "0"), ("1", "1"), ("invalid", "1")):
        monkeypatch.setenv(OWNER_FLAGS[0], generations)
        monkeypatch.setenv(OWNER_FLAGS[1], drain)
        report = D.projection_dispatch(profile, reason="test")
        assert report.owner == "program0"
        assert report.action in ("published", "no_work")
        assert report.generation_id is not None

    view = G.open_generation_view(profile / "memory")
    try:
        assert view.manifest.generation_id == report.generation_id
        G.validate_published_generation(G.projection_root(profile), view.manifest)
    finally:
        view.close()


def test_no_shipped_source_selects_a_projection_owner_from_environment():
    offenders = []
    for tree in PRODUCTION_TREES:
        root = REPO / tree
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in (".py", ".yaml", ".yml", ".json", ".bat", ".ps1", ".sh"):
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if any(flag in source for flag in OWNER_FLAGS):
                offenders.append(str(path.relative_to(REPO)))
    source = (REPO / "boot.py").read_text(encoding="utf-8")
    if any(flag in source for flag in OWNER_FLAGS):
        offenders.append("boot.py")
    assert offenders == []


def test_dispatch_module_has_one_unconditional_generation_owner(tmp_path):
    dispatch = importlib.import_module("memory.projection.dispatch")
    assert hasattr(dispatch, "projection_dispatch")
    assert hasattr(dispatch, "run_program0_locked")
    assert not hasattr(dispatch, "run_cut13_direct_locked")
    assert not hasattr(dispatch, "validate_flags_before_mutation")
    health = G.projection_health(tmp_path / "profile")
    assert not hasattr(health, "projection_owner")
    assert not hasattr(health, "generation_mode_retreat_required")
    assert not hasattr(health, "path_conflict")


def test_fixed_name_writers_refuse_active_generation_while_dispatch_keeps_serving_it(tmp_path):
    profile = _profile(tmp_path)
    published = D.projection_dispatch(profile, reason="boot")
    assert published.action == "published"

    # The retired fixed-name APIs remain barred once the generation is authoritative.
    from memory.dme.adapter import rebuild_sram_from_graph
    from memory.projection.dispatch import ProjectionPathConflict
    from memory.taxonomy.compiler import compile_memory_graph

    with pytest.raises(ProjectionPathConflict):
        compile_memory_graph(root=profile)
    with pytest.raises(ProjectionPathConflict):
        rebuild_sram_from_graph(root=profile)

    subsequent = D.projection_dispatch(profile, reason="boot")
    assert subsequent.owner == "program0" and subsequent.action == "no_work"
    assert subsequent.generation_id == published.generation_id


def test_candidate_build_isolated_from_active_generation(tmp_path):
    profile = _profile(tmp_path)
    published = D.projection_dispatch(profile, reason="boot")
    from memory.projection import build as B

    with G.projection_publication_authority(profile) as authority:
        candidate = B.build_generation(profile, reason="candidate-isolation", authority=authority)

    assert isinstance(candidate, G.Candidate)
    assert candidate.candidate_dir != G.projection_root(profile) / f"gen-{published.generation_id}"
    assert G.read_active_manifest(G.projection_root(profile)).generation_id == published.generation_id

"""The suite never resolves the operator's live episode journal (charter: no live-memory writes).

Found at WO-08's entry gate: ``pressure_loop.policy.episode_journal_path()`` returns the
repo-relative ``pressure_loop/state/episode_journal.jsonl`` and every reader and writer resolves it
against the repository through ``pressure_loop.episode_journal._resolve_path``. Nothing in the
session conftest redirected it, so any test that archived an episode without patching the path
appended a row to the operator's LIVE journal — the ordering authority ``journal_window`` reads
for recent outcomes and causal parents. The entry gate alone added 53 rows before it was stopped.

This checks resolution only, deliberately: a test that archived an episode to prove the leak
would itself be the leak.
"""

from __future__ import annotations

from pathlib import Path


def test_an_unpatched_test_resolves_the_episode_journal_outside_the_live_tree():
    from core.paths import OMEGA_ROOT
    from pressure_loop.episode_journal import _resolve_path
    from pressure_loop.policy import episode_journal_path

    resolved = _resolve_path(episode_journal_path()).resolve()
    live_state = (Path(OMEGA_ROOT) / "pressure_loop" / "state").resolve()
    assert not resolved.is_relative_to(live_state), (
        f"a test resolves the live operator episode journal: {resolved}"
    )


def test_the_default_journal_path_itself_is_unchanged():
    """The redirect is at resolution time; the policy's own default is not rewritten."""
    from pressure_loop.policy import episode_journal_path

    assert episode_journal_path().as_posix().endswith("pressure_loop/state/episode_journal.jsonl")


def test_the_coord_index_writer_follows_the_redirect_whenever_it_was_imported(tmp_path, monkeypatch):
    """Found during WO-08's probe: ``memory.engine.reindex`` binds ``MEMORY_ROOT`` at IMPORT, and
    the shared redirect did not override it. Whichever test imported it first fixed where every
    later ``build_index(root=None)`` wrote: another test's temp dir in one run, the operator's LIVE
    coord index in another. Simulate a stale import-time binding, apply the redirect, and require
    the writer to follow it. Resolution only — nothing is built."""
    import core.paths as core_paths
    from memory.engine import reindex
    from tests.helpers.memory_isolation import redirect_memory_to_tmp

    live = Path(core_paths.OMEGA_ROOT) / "memory"
    monkeypatch.setattr(reindex, "MEMORY_ROOT", live)  # imported while the live root was active
    monkeypatch.setattr(reindex, "INDEX_PATH", live / "indexes" / "coord_index.json")
    redirect_memory_to_tmp(tmp_path, monkeypatch)

    isolated = Path(core_paths.MEMORY_ROOT)
    assert not isolated.resolve().is_relative_to(live.resolve())
    assert reindex._index_path(None) == isolated / "indexes" / "coord_index.json"
    assert reindex.INDEX_PATH == isolated / "indexes" / "coord_index.json"

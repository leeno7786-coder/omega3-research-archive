"""``memory/projection/dispatch.py`` — what is left after the owner matrix went away.

The module used to arbitrate between three projection owners selected by two unset
environment variables; AGENTS.md §2 forbids that dual path and the selection is gone
(``tests/memory/projection/test_single_projection_path.py`` pins its absence). Three things
it owned outlive it, and they are what this file covers: the lifecycle interlock boot and
day-zero contend for, the dirty-marker digest the full rebuild compare-and-clears, and the
one-path guard that refuses over a surviving published generation.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import core.paths as core_paths
from core import timekeeper as TK
from memory.projection import dispatch as D
from memory.projection import generation as G
from tests.memory.projection._fixtures import build_and_publish, redirect_timekeeper, seed_records_only


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    redirect_timekeeper(tmp_path)
    monkeypatch.setattr(core_paths, "OMEGA_ROOT", tmp_path)               # lifecycle lock under tmp
    D.release_lifecycle_lock()
    yield
    D.release_lifecycle_lock()
    TK.reset_for_tests()


def _fresh(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    seed_records_only(profile)
    return profile


def test_a_fresh_profile_passes_the_one_path_guard_untouched(tmp_path):
    profile = _fresh(tmp_path)
    proj = G.projection_root(profile)
    D.assert_single_projection_path(profile)
    assert not proj.exists() or not any(proj.iterdir())          # the guard reads; it never mutates


@pytest.mark.parametrize("authority", ["manifest", "publication_lineage", "generation_directory", "live_intent"])
def test_every_kind_of_surviving_generation_authority_refuses_the_path(tmp_path, authority):
    profile = _fresh(tmp_path)
    proj = G.projection_root(profile)
    proj.mkdir(parents=True, exist_ok=True)
    if authority == "manifest":
        (proj / G.MANIFEST_FILE).write_text("{}", encoding="utf-8")
    elif authority == "publication_lineage":
        (proj / G.LINEAGE_FILE).write_text("{}\n", encoding="utf-8")
    elif authority == "generation_directory":
        (proj / f"{G.GENERATION_DIR_PREFIX}deadbeef").mkdir()
    else:
        (proj / G.PUBLICATION_INTENT_FILE).write_text("{}", encoding="utf-8")

    with pytest.raises(D.ProjectionPathConflict) as raised:
        D.assert_single_projection_path(profile)
    assert authority in str(raised.value)                        # the cause survives to the caller


def test_a_real_publication_refuses_the_path_afterwards(tmp_path):
    """The live shape: a profile that once published a generation cannot be maintained by the
    single path, because readers resolve that generation ahead of the files this path writes."""
    profile = _fresh(tmp_path)
    build_and_publish(profile, reason="test")
    with pytest.raises(D.ProjectionPathConflict):
        D.assert_single_projection_path(profile)


def test_dirty_marker_digest_names_the_marker_the_rebuild_will_have_satisfied(tmp_path):
    from memory.projection_delta import mark_projection_dirty

    profile = _fresh(tmp_path)
    assert D._dirty_marker_sha(profile) is None
    assert mark_projection_dirty(reason="entity-link hook mutated 1 peer canonical file(s)", memory_id="1.00.x", root=profile)
    first = D._dirty_marker_sha(profile)
    assert first is not None and len(first) == 64
    assert mark_projection_dirty(reason="marked again, differently", memory_id="1.00.y", root=profile)
    assert D._dirty_marker_sha(profile) != first                 # a rewritten marker is a different marker


def test_lifecycle_interlock(tmp_path):
    assert D.hold_lifecycle_lock() is True                                  # boot: SHARED
    assert D.hold_lifecycle_lock() is True                                  # idempotent
    fd = G._open_lock_handle(D.lifecycle_lock_path())
    assert G._try_lock(fd, exclusive=True) is False                          # reset cannot claim it while Omega is up
    os.close(fd)
    D.release_lifecycle_lock()
    fd = G._open_lock_handle(D.lifecycle_lock_path())
    assert G._try_lock(fd, exclusive=True) is True                           # reset claims it EXCLUSIVE
    assert D.hold_lifecycle_lock() is False                                  # a startup racing the reset is excluded
    G._unlock(fd)
    os.close(fd)
    assert D.hold_lifecycle_lock() is True


def test_the_lifecycle_lock_lives_outside_guardian_state(tmp_path):
    """day-zero wipes guardian/state; the interlock it claims must survive that wipe."""
    path = Path(D.lifecycle_lock_path())
    assert path.parent.name == "guardian" and path.name == D.LIFECYCLE_LOCK_FILE

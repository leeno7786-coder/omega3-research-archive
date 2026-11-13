"""One publication token authorises exactly ONE profile.

AGENTS.md §2: the owner matrix that selected ``program0`` from two unset environment
variables is gone (``test_single_projection_path.py`` pins its absence), and with it every
assertion this file used to make about which owner a flag combination picked, and about the
currentness of a published generation — a state the one projection path never produces.

What survives is the authority binding, because the retained path uses it: the full rebuild
compare-and-clears the projection dirty marker under ``projection_publication_authority``
(``memory/engine/reindex.py::_clear_satisfied_dirty_marker``). A token that could reach a
second profile would let one profile's rebuild clear another's marker.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import core.paths as core_paths
from core import timekeeper as TK
from memory import projection_delta as PD
from memory.projection import dispatch as D
from memory.projection import generation as G
from memory.projection import projector as P
from tests.memory.projection._fixtures import redirect_timekeeper, seed_records_only, write_record


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    redirect_timekeeper(tmp_path)
    monkeypatch.setattr(core_paths, "OMEGA_ROOT", tmp_path)
    D.release_lifecycle_lock()
    yield
    D.release_lifecycle_lock()
    G._crash_hook = None
    TK.reset_for_tests()


def _emit(profile, mid, **kw):
    write_record(profile, mid, **kw)
    path = profile / "memory" / "200_knowledge" / f"{mid}.json"
    return PD.emit_delta_for_canonical(json.loads(path.read_text()), path, root=profile / "memory")["delta"]


def test_outbox_helpers_refuse_a_publication_token_from_another_profile(tmp_path):
    """Review a045ec4a §4 (D-13-33): one token authorises ONE profile — the three authority-aware
    outbox wrappers bind the token to the target root; B is untouched by an A token."""
    a = tmp_path / "A"; a.mkdir(); seed_records_only(a)
    b = tmp_path / "B"; b.mkdir(); seed_records_only(b)
    P.drain_once(a, reason="boot"); P.drain_once(b, reason="boot")                        # both established
    _emit(b, "k5", title="Glia", summary="glial support cells")
    PD.mark_projection_dirty(reason="B is dirty", root=b / "memory")
    b_outbox_before = PD.outbox_path(b / "memory").read_bytes()
    b_marker_before = PD.projection_dirty_path(b / "memory").read_bytes()
    b_epoch_before = P.read_epoch(G.projection_root(b)).to_dict()
    b_cursor = P.read_cursor(G.projection_root(b))
    with G.projection_publication_authority(a) as auth_a:
        with pytest.raises(G.PublicationAuthorityRequired):
            PD.clear_projection_dirty(root=b / "memory", expected_marker_sha256=hashlib.sha256(b_marker_before).hexdigest(), authority=auth_a)
        with pytest.raises(G.PublicationAuthorityRequired):
            PD.capture_outbox_snapshot(root=b / "memory", cursor=b_cursor, authority=auth_a)
        with pytest.raises(G.PublicationAuthorityRequired):
            PD.rotate_outbox_epoch(root=b / "memory", old_state={"old_epoch": 1, "old_identity": None, "reason": "cross-profile"}, authority=auth_a)
    assert PD.outbox_path(b / "memory").read_bytes() == b_outbox_before                   # B byte-for-byte unchanged
    assert PD.projection_dirty_path(b / "memory").read_bytes() == b_marker_before
    assert P.read_epoch(G.projection_root(b)).to_dict() == b_epoch_before
    assert not list(PD.outbox_path(b / "memory").parent.glob("*.quarantine-*"))
    with G.projection_publication_authority(b) as auth_b:                                  # B's own token succeeds
        snap = PD.capture_outbox_snapshot(root=b / "memory", cursor=b_cursor, authority=auth_b)
        assert snap.captured_size == len(b_outbox_before)
        assert PD.clear_projection_dirty(root=b / "memory", expected_marker_sha256=hashlib.sha256(b_marker_before).hexdigest(), authority=auth_b) is True
    assert PD.projection_dirty_state(root=b / "memory")["dirty"] is False

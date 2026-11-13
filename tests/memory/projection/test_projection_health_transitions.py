"""Projection health stays non-current across unfinished Program 0 transitions."""

from __future__ import annotations

import json

import pytest

from core import timekeeper as TK
from memory import projection_delta as PD
from memory.projection import generation as G
from memory.projection import projector as P
from tests.memory.projection._fixtures import redirect_timekeeper, seed_records_only


@pytest.fixture(autouse=True)
def _timekeeper(tmp_path):
    redirect_timekeeper(tmp_path)
    yield
    G._crash_hook = None
    TK.reset_for_tests()


def _current_profile(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    seed_records_only(profile)
    report = P.drain_once(profile, reason="boot")
    assert report.status == "published_new"
    assert G.projection_health(profile).current is True
    return profile, G.projection_root(profile), PD.outbox_path(profile / "memory")


def _append_delta(profile, memory_id: str) -> None:
    delta = PD.ProjectionDelta.build(
        operation="upsert",
        memory_id=memory_id,
        canonical_path=f"memory/200_knowledge/{memory_id}.json",
        canonical_hash="0" * 64,
        coord=(2, 0, 1),
        commit_seq=0,
        projection_class="semantic",
        sequenced=False,
    )
    assert PD.append_projection_delta(delta, root=profile / "memory")


def test_recovery_pending_keeps_health_non_current_until_rebuild_settles(tmp_path):
    profile, _proj, outbox = _current_profile(tmp_path)
    _append_delta(profile, "k1")
    assert P.drain_once(profile, reason="deepsleep").cursor_settled
    assert G.projection_health(profile).current is True

    saved_size = outbox.stat().st_size
    outbox.write_bytes(outbox.read_bytes()[:-1])  # violate the previously settled physical snapshot
    G._crash_hook = lambda point: (
        (_ for _ in ()).throw(G.SimulatedCrash(point)) if point == "p0:after_capture" else None
    )
    with pytest.raises(G.SimulatedCrash):
        P.drain_once(profile, reason="deepsleep")
    G._crash_hook = None

    health = G.projection_health(profile)
    assert saved_size > 0
    assert health.current is False
    assert health.program0["recovery_pending"] is True
    assert health.lane_reason == "projection_outbox_recovery_pending"

    report = P.drain_once(profile, reason="deepsleep")
    assert report.cursor_settled
    assert PD.pending_outbox_recovery(profile / "memory") is None
    assert G.projection_health(profile).current is True


def test_bootstrap_intent_keeps_health_non_current_until_bootstrap_resumes(tmp_path):
    profile, proj, _outbox = _current_profile(tmp_path)
    (proj / PD.OUTBOX_EPOCH_FILE).unlink()
    (proj / PD.OUTBOX_CURSOR_FILE).unlink()
    G._crash_hook = lambda point: (
        (_ for _ in ()).throw(G.SimulatedCrash(point)) if point == "p0:bootstrap_after_intent" else None
    )
    with pytest.raises(G.SimulatedCrash):
        P.drain_once(profile, reason="boot")
    G._crash_hook = None

    health = G.projection_health(profile)
    assert health.current is False
    assert health.program0["bootstrap_intent"] is True
    assert health.lane_reason == "projection_outbox_bootstrap_incomplete"

    report = P.drain_once(profile, reason="boot")
    assert report.status == "bootstrapped"
    assert P.read_bootstrap_intent(proj) is None
    assert G.projection_health(profile).current is True


def test_metadata_corruption_keeps_health_non_current_until_rotate_first_recovery(tmp_path):
    profile, proj, _outbox = _current_profile(tmp_path)
    (proj / PD.OUTBOX_EPOCH_FILE).unlink()

    health = G.projection_health(profile)
    assert health.current is False
    assert health.program0["bootstrap_state"] == "metadata_corrupt"
    assert health.lane_reason == "projection_outbox_metadata_corrupt"

    report = P.drain_once(profile, reason="deepsleep")
    assert report.status == "outbox_metadata_corrupt_recovered"
    assert report.cursor_settled
    assert G.projection_health(profile).current is True


def test_partial_outbox_tail_is_pending_until_completed_and_settled(tmp_path):
    profile, _proj, outbox = _current_profile(tmp_path)
    delta = PD.ProjectionDelta.build(
        operation="upsert",
        memory_id="k1",
        canonical_path="memory/200_knowledge/k1.json",
        canonical_hash="0" * 64,
        coord=(2, 0, 1),
        commit_seq=0,
        projection_class="semantic",
        sequenced=False,
    )
    payload = json.dumps(delta.to_dict(), separators=(",", ":")).encode("utf-8")
    split = len(payload) // 2
    with outbox.open("ab") as stream:
        stream.write(payload[:split])

    partial = G.projection_health(profile)
    assert partial.current is False
    assert partial.pending_delta_count == 0
    assert partial.pending_partial_bytes == split
    assert partial.lane_reason == "projection_outbox_partial_tail_pending"

    with outbox.open("ab") as stream:
        stream.write(payload[split:] + b"\n")
    completed = G.projection_health(profile)
    assert completed.current is False
    assert completed.pending_partial_bytes == 0
    assert completed.pending_delta_count == 1

    report = P.drain_once(profile, reason="deepsleep")
    assert report.cursor_settled
    settled = G.projection_health(profile)
    assert settled.pending_partial_bytes == 0
    assert settled.pending_delta_count == 0
    assert settled.current is True

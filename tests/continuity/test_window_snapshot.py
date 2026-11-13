"""WO-09 — a sealed cohort is bound to a snapshot, and its integrity is checked before inference.

CONTRACTS.md section 4: ``SnapshotRef`` binds the retained canonical generation, the coverage
clock, the visibility cutoff, the trajectory revision at start and at cutoff, the authority
snapshot and the reader-policy revision. ``WindowManifest`` binds the window id/version, the
configuration fingerprint, the ordered member references, scope, snapshot and completeness.

Acceptance cases covered here:

* **Snapshot integrity** — an altered source hash or a missing required member fails before
  any inference sees the window.
* **Readiness** — a record without the required publication readiness never enters a complete
  cohort; a pending one never enters a cohort at all.
* **Replay** — a saved manifest replays to identical membership and digests, and later memory
  activity mutates neither the old records nor the earlier snapshot.
"""

from __future__ import annotations

import json
import time

import pytest

from pressure_loop.episode import EpisodeStore
from schema.continuity_json import (
    CanonicalEpisodeUnavailable,
    CanonicalRef,
    HistoricalSnapshotUnavailable,
    WindowMembershipError,
)
from schema.window_evidence import WindowManifest
from tests.helpers import continuity_terminus as ct
from tests.helpers import continuity_window as cw


@pytest.fixture(autouse=True)
def _terminus(tmp_path, monkeypatch):
    from memory import episode_sram

    monkeypatch.setenv("OMEGA_WINDOW_LANE", "1")
    ct.isolate(monkeypatch, tmp_path)
    yield
    episode_sram.reset()


def _view():
    import memory.episode_commit_view as view

    return view


def _sealed_cohort():
    m = cw.membership()
    store = EpisodeStore()
    carriers = cw.close_cohort(store)
    staged = cw.staged_cohort()
    return store, carriers, staged, m.seal_window(staged, snapshot=m.window_snapshot(staged))


def _locked_window(ids, *, template, refs, last):
    """A locked window over *ids* with *refs* pinned — how a later producer could present one."""
    from pressure_loop.reflection_window import STATUS_LOCKED, ReflectionWindow

    return ReflectionWindow(
        window_id="window_readiness",
        started_at_episode=ids[0],
        trajectory_snapshot=dict(template.trajectory_snapshot),
        episodes=list(ids),
        cascade_status_by_episode={eid: "completed" for eid in ids},
        member_refs={eid: ref.to_dict() for eid, ref in zip(ids, refs)},
        configuration_fingerprint=template.configuration_fingerprint,
        trajectory_revision_at_start=template.trajectory_revision_at_start,
        trajectory_revision_at_cutoff=template.trajectory_revision_at_start,
        cutoff=cw.cutoff_of(last),
        status=STATUS_LOCKED,
    )


# ── the binding ──────────────────────────────────────────────────────────────


def test_the_snapshot_binds_projection_cutoff_trajectories_configuration_and_reader_policy():
    import pressure_loop.reflection_window as rw

    m = cw.membership()
    _store, carriers, staged, manifest = _sealed_cohort()
    snapshot = manifest.snapshot

    assert snapshot.generation_id == _view().published_projection_id()
    last = m.hydrate_window(manifest)[-1]
    assert snapshot.coverage_clock.to_dict() == dict(last.temporal["coverage_clock"])
    assert snapshot.coverage_clock.commit_seq == carriers[-1].timekeeper["commit_seq"]
    assert snapshot.visibility_cutoff == last.temporal["closed_at"].value
    for row in manifest.members:
        assert row["finish_line_commit_seq"] <= snapshot.coverage_clock.commit_seq

    assert snapshot.trajectory_revision_at_start == staged.trajectory_revision_at_start
    assert snapshot.trajectory_revision_at_cutoff == staged.trajectory_revision_at_cutoff
    assert snapshot.reader_policy_revision == m.READER_POLICY_REVISION
    assert snapshot.authority_snapshot_ref == m.AUTHORITY_SNAPSHOT_UNBOUND

    assert manifest.configuration_fingerprint == rw.window_configuration_fingerprint()
    assert manifest.configuration_fingerprint == staged.configuration_fingerprint
    assert manifest.window_version == 1 and manifest.window_id == staged.window_id


def test_the_trajectory_at_cutoff_is_what_held_when_the_cohort_filled(monkeypatch):
    """Recorded at the lock, never recomputed from whatever trajectory is current at the read."""
    import pressure_loop.reflection_window as rw
    import pressure_loop.temporal_store as temporal_store

    def trajectory(tag):
        return {"trajectory_id": f"traj-{tag}", "cycle_id": f"cycle-{tag}",
                "target_domain": "quantum_physics", "target_capability": tag}

    current = {"value": trajectory("start")}
    monkeypatch.setattr(temporal_store, "read_trajectory", lambda: dict(current["value"]))

    m = cw.membership()
    store = EpisodeStore()
    cw.close_member(store, "success")
    current["value"] = trajectory("cutoff")
    cw.close_cohort(store, cw.MIXED_PLAN[1:])
    current["value"] = trajectory("later")

    staged = cw.staged_cohort()
    manifest = m.seal_window(staged, snapshot=m.window_snapshot(staged))
    assert manifest.snapshot.trajectory_revision_at_start == rw.trajectory_revision(
        trajectory("start"))
    assert manifest.snapshot.trajectory_revision_at_cutoff == rw.trajectory_revision(
        trajectory("cutoff"))
    assert manifest.snapshot.trajectory_revision_at_cutoff != rw.trajectory_revision(
        trajectory("later"))


def test_an_unreadable_trajectory_is_recorded_as_unavailable_never_as_none(monkeypatch):
    """"Could not read" and "there is no trajectory" must never share a value (AGENTS.md 2.1)."""
    import pressure_loop.reflection_window as rw
    import pressure_loop.temporal_store as temporal_store

    assert rw.trajectory_revision({}) == "trajectory:none"

    def unreadable():
        raise RuntimeError("injected: lobe 9 is unreadable")

    monkeypatch.setattr(temporal_store, "read_trajectory", unreadable)
    window = rw.open_reflection_window("ep-unreadable")
    assert window.trajectory_revision_at_start.startswith("unavailable:")
    assert "injected: lobe 9 is unreadable" in window.trajectory_revision_at_start
    assert window.trajectory_revision_at_start != rw.trajectory_revision({})


# ── Readiness ────────────────────────────────────────────────────────────────


def test_a_pending_member_never_enters_a_cohort(monkeypatch):
    import deepsleep_loop.loop as loop

    view = _view()
    store = EpisodeStore()
    carriers = cw.close_cohort(store, cw.MIXED_PLAN[:9])
    template = store.get_active_window()

    tenth = ct.dress_learn(ct.open_carrier(store))

    def refuse(_context_id):
        raise RuntimeError("injected: the drain did not run")

    monkeypatch.setattr(loop, "_flush_episode_sram", refuse)
    with pytest.raises(RuntimeError, match="injected"):
        ct.close(store)
    pending = view.pending_episode_record_ref(tenth.episode_id)
    assert pending is not None

    ids = [carrier.episode_id for carrier in carriers] + [tenth.episode_id]
    refs = [view.episode_record_ref(eid) for eid in ids[:9]] + [pending]
    window = _locked_window(ids, template=template, refs=refs, last=tenth)
    m = cw.membership()
    with pytest.raises(WindowMembershipError, match="pending"):
        m.seal_window(window, snapshot=m.window_snapshot(window))


def test_a_committed_but_unpublished_member_seals_incomplete_and_is_never_hydrated(monkeypatch):
    import deepsleep_loop.loop as loop
    from pressure_loop import window_lane

    view = _view()
    store = EpisodeStore()
    carriers = cw.close_cohort(store, cw.MIXED_PLAN[:9])
    template = store.get_active_window()

    tenth = ct.dress_learn(ct.open_carrier(store))
    monkeypatch.setattr(
        loop, "_publish_projection_after_deepsleep",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("injected: publication failed")),
    )
    with pytest.raises(RuntimeError, match="publication failed"):
        ct.close(store)

    ids = [carrier.episode_id for carrier in carriers] + [tenth.episode_id]
    window = _locked_window(ids, template=template,
                            refs=[view.episode_record_ref(eid) for eid in ids], last=tenth)
    m = cw.membership()
    manifest = m.seal_window(window, snapshot=m.window_snapshot(window))
    assert manifest.complete is False
    statuses = {row["episode_id"]: row["memory_commit_status"] for row in manifest.members}
    assert statuses[tenth.episode_id] == "canonical_committed"
    assert all(statuses[eid] == "recall_published" for eid in ids[:9])
    with pytest.raises(WindowMembershipError, match="complete"):
        m.hydrate_window(manifest)

    # Through the real consumption seam too: the reflection never starts.
    assert window_lane.stage_locked_window(window)
    with pytest.raises(WindowMembershipError):
        window_lane.restore_window(ct.dress_learn(cw.open_window_episode(store)))


def test_window_lane_holds_new_member_until_deepsleep_program0_publication_finishes(monkeypatch):
    """The append is durable before Program 0 starts; release must wait for that publication."""
    import threading

    import deepsleep_loop.loop as deepsleep_loop
    from pressure_loop import window_lane

    view = _view()
    store = EpisodeStore()
    prior = cw.close_cohort(store, cw.MIXED_PLAN[:9])

    publish_entered = threading.Event()
    allow_publish = threading.Event()
    worker_done = threading.Event()
    worker_errors = []
    worker_episode = {}
    real_publish = deepsleep_loop._publish_projection_after_deepsleep

    def paused_publish(*, context_id, scope):
        publish_entered.set()
        if not allow_publish.wait(90):
            raise TimeoutError("test did not release the paused Program 0 publication")
        return real_publish(context_id=context_id, scope=scope)

    monkeypatch.setitem(
        deepsleep_loop.run_deepsleep_for_episode.__globals__,
        "_publish_projection_after_deepsleep",
        paused_publish,
    )
    assert (
        deepsleep_loop.run_deepsleep_for_episode.__globals__["_publish_projection_after_deepsleep"]
        is paused_publish
    )

    def close_tenth():
        try:
            # EpisodeStore binds its active carrier to the calling thread. Use a fresh
            # store for this worker so the real terminus runs on its owning cascade thread.
            worker_store = EpisodeStore()
            tenth = ct.dress_learn(ct.open_carrier(worker_store))
            worker_episode["id"] = tenth.episode_id
            deepsleep_loop.run_deepsleep_for_episode(worker_store)
        except BaseException as exc:  # surface worker failures in the test thread
            worker_errors.append(exc)
        finally:
            worker_done.set()

    worker = threading.Thread(target=close_tenth, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 90
        while (
            not publish_entered.wait(0.1)
            and not worker_done.is_set()
            and time.monotonic() < deadline
        ):
            pass
        assert publish_entered.is_set(), (
            "DeepSleep did not reach its Program 0 publication seam; "
            f"worker errors: {worker_errors!r}"
        )
        staged = cw.staged_cohort()
        tenth_id = worker_episode["id"]
        expected = [carrier.episode_id for carrier in prior] + [tenth_id]
        assert list(staged.episodes) == expected
        assert view.window_members_unpublished(staged.episodes) == (tenth_id,)
        assert window_lane.pressure_contributor() is None, (
            "the newly appended but unpublished member was released for reflection"
        )
    finally:
        allow_publish.set()
        worker.join(timeout=90)

    assert not worker.is_alive(), "DeepSleep did not finish after Program 0 was released"
    assert worker_done.is_set()
    assert not worker_errors, f"DeepSleep failed: {worker_errors!r}"

    staged = cw.staged_cohort()
    assert list(staged.episodes) == expected
    assert view.window_members_unpublished(staged.episodes) == ()
    assert window_lane.pressure_contributor() is not None, (
        "the fully published cohort remained held"
    )


# ── Snapshot integrity ──────────────────────────────────────────────────────


def test_an_altered_member_source_fails_before_inference():
    from pressure_loop import window_lane

    m = cw.membership()
    store, carriers, _staged, manifest = _sealed_cohort()
    victim = carriers[4].episode_id
    path = cw.canonical_document_path(manifest.member_refs[4].record_id)
    document = json.loads(path.read_text(encoding="utf-8"))
    body = json.loads(document["text"])
    body["episode_record"]["identity"]["thread_id"] = "thread-TAMPERED"
    document["text"] = json.dumps(body)
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CanonicalEpisodeUnavailable, match=victim):
        m.hydrate_window(manifest)
    with pytest.raises(CanonicalEpisodeUnavailable):
        window_lane.restore_window(ct.dress_learn(cw.open_window_episode(store)))


def test_a_missing_member_raises_and_never_shrinks_the_cohort():
    from pressure_loop import window_lane

    m = cw.membership()
    store, carriers, staged, manifest = _sealed_cohort()
    victim = carriers[2].episode_id
    cw.canonical_document_path(manifest.member_refs[2].record_id).unlink()

    with pytest.raises(CanonicalEpisodeUnavailable, match=victim):
        m.hydrate_window(manifest)
    with pytest.raises(CanonicalEpisodeUnavailable):
        m.seal_window(staged, snapshot=manifest.snapshot)
    with pytest.raises(CanonicalEpisodeUnavailable):
        window_lane.restore_window(ct.dress_learn(cw.open_window_episode(store)))
    assert len(cw.staged_cohort().episodes) == 10, "the cohort was shrunk around the loss"


def test_a_manifest_that_misstates_a_member_hash_is_refused():
    m = cw.membership()
    _store, carriers, _staged, manifest = _sealed_cohort()
    data = manifest.to_dict()
    data["members"][1]["ref"]["content_sha256"] = "f" * 64
    lying = WindowManifest.from_dict(data)

    with pytest.raises(CanonicalEpisodeUnavailable, match=carriers[1].episode_id):
        m.hydrate_window(lying)


# ── Replay ───────────────────────────────────────────────────────────────────


def test_a_saved_manifest_replays_to_identical_membership_and_digests():
    m = cw.membership()
    _store, _carriers, _staged, manifest = _sealed_cohort()
    saved = json.dumps(manifest.to_dict(), sort_keys=True)

    EpisodeStore()  # restart the reader
    replayed = WindowManifest.from_dict(json.loads(saved))
    assert replayed.to_dict() == manifest.to_dict()
    records = m.hydrate_window(replayed)
    assert [record.content_sha256() for record in records] == [
        ref.content_sha256 for ref in replayed.member_refs
    ]
    assert [record.episode_id for record in records] == list(replayed.member_ids)


def test_later_memory_activity_mutates_neither_old_members_nor_the_earlier_snapshot():
    m = cw.membership()
    store, _carriers, _staged, manifest = _sealed_cohort()
    saved = manifest.to_dict()
    before = [record.to_dict() for record in m.hydrate_window(manifest)]

    cw.close_cohort(store, ("success", "failure"))  # later episodes recompile recall

    replayed = WindowManifest.from_dict(saved)
    assert replayed.snapshot.to_dict() == saved["snapshot"]
    assert [record.to_dict() for record in m.hydrate_window(replayed)] == before
    ref = CanonicalRef.coerce(saved["members"][0]["ref"], where="test")
    with pytest.raises(HistoricalSnapshotUnavailable):
        _view().resolve_episode_commit(ref, snapshot=replayed.snapshot)

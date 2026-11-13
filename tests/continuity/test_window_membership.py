"""WO-09 — a reflected cohort is exactly ten fresh members, fixed when it fills.

The order's seed: drive the actual append and seal code with ten isolated canonical episodes
plus one window episode, and assert the manifest holds exactly the ten non-window identities in
finish-line order. Then restart the reader and assert identical membership and digests. A missing
member must raise, never reduce the count.

Every member below closes through the real DeepSleep terminus, so its membership, its place in
the finish-line order and its canonical reference are what the commit region actually produced —
nothing is appended to a window by the test.

Acceptance cases covered here (the rest are in test_window_snapshot / test_window_hydration):

* **Membership** — ten distinct ready non-window episodes, not nine and not eleven; failed,
  cancelled and idle work stays eligible.
* **No self-trigger** — a committed window without new experience fills nothing and ignites
  nothing.
* **Staging recovery** — a crash on either side of the double buffer leaves ONE cohort.
* **Checkpoint contract** — persistence holds references and control metadata, never bodies.
"""

from __future__ import annotations

import json

import pytest

from pressure_loop.episode import EpisodeStore
from schema.continuity_json import CanonicalRef, WindowMembershipError
from schema.window_evidence import FOCAL_MEMBER_COUNT
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


def _seal(window):
    m = cw.membership()
    return m.seal_window(window, snapshot=m.window_snapshot(window))


# ── Membership ───────────────────────────────────────────────────────────────


def test_ten_real_mixed_outcome_episodes_seal_as_exactly_ten_members_in_finish_line_order():
    m = cw.membership()
    store = EpisodeStore()
    carriers = cw.close_cohort(store)
    ids = [carrier.episode_id for carrier in carriers]

    staged = cw.staged_cohort()
    assert list(staged.episodes) == ids

    manifest = m.seal_window(staged, snapshot=m.window_snapshot(staged))
    assert manifest.scope == "focal"
    assert manifest.member_count == FOCAL_MEMBER_COUNT == 10
    assert manifest.member_ids == tuple(ids), "membership is not the ten closes, in order"

    # Exact, unique canonical references: the very record each terminus committed.
    refs = manifest.member_refs
    assert len(set(refs)) == 10
    assert list(refs) == [_view().episode_record_ref(eid) for eid in ids]

    # The existing finish-line authority orders the cohort; nothing re-sorts it.
    seqs = [row["finish_line_commit_seq"] for row in manifest.members]
    assert seqs == [carrier.timekeeper["commit_seq"] for carrier in carriers]
    assert seqs == sorted(seqs) and len(set(seqs)) == 10

    assert manifest.complete is True
    assert {row["memory_commit_status"] for row in manifest.members} == {"recall_published"}


def test_failed_cancelled_and_idle_work_remains_eligible_membership():
    """No success-only filter: every outcome the cohort closed with is a member."""
    m = cw.membership()
    store = EpisodeStore()
    cw.close_cohort(store)
    manifest = _seal(cw.staged_cohort())

    records = m.hydrate_window(manifest)
    task_outcomes = [record.outcome.task_outcome for record in records]
    assert {"success", "failure", "cancelled", "not_applicable"} <= set(task_outcomes), (
        task_outcomes
    )
    assert "failed" in {row["cascade_status"] for row in manifest.members}
    assert [row["cascade_status"] for row in manifest.members] == [
        record.outcome.cascade_status for record in records
    ]


def test_the_eleventh_close_opens_a_fresh_window_and_never_joins_the_sealed_cohort():
    store = EpisodeStore()
    carriers = cw.close_cohort(store, cw.MIXED_PLAN + ("success",))

    staged = cw.staged_cohort()
    assert list(staged.episodes) == [carrier.episode_id for carrier in carriers[:10]]
    collecting = store.get_active_window()
    assert collecting is not None
    assert list(collecting.episodes) == [carriers[10].episode_id]

    manifest = _seal(staged)
    assert carriers[10].episode_id not in manifest.member_ids
    assert manifest.member_count == 10


def test_nine_closes_are_never_sealed_as_a_cohort():
    store = EpisodeStore()
    cw.close_cohort(store, cw.MIXED_PLAN[:9])

    assert cw.staged_cohorts() == []
    collecting = store.get_active_window()
    assert collecting is not None and collecting.episode_count == 9
    with pytest.raises(WindowMembershipError):
        _seal(collecting)


def test_a_member_whose_reference_the_terminus_did_not_pin_cannot_be_sealed():
    """A missing member raises. It is never dropped to make a smaller cohort."""
    store = EpisodeStore()
    carriers = cw.close_cohort(store)
    staged = cw.staged_cohort()
    victim = carriers[4].episode_id
    staged.member_refs.pop(victim)

    with pytest.raises(WindowMembershipError, match=victim):
        _seal(staged)


def test_the_live_configured_window_size_is_asserted_at_the_producer(monkeypatch):
    import pressure_loop.reflection_window as rw

    store = EpisodeStore()
    cw.close_cohort(store)
    staged = cw.staged_cohort()

    monkeypatch.setattr(rw, "window_configuration", lambda: {"window_size": 12})
    with pytest.raises(WindowMembershipError, match="12"):
        _seal(staged)


def test_an_unreadable_window_configuration_fails_instead_of_defaulting_to_ten(monkeypatch):
    """BOUNDARY_MAP L11: a bare ``except Exception`` used to turn any config error into 10."""
    import config.loader as loader
    import pressure_loop.reflection_window as rw

    injected = OSError("injected: config/reflection_window.yaml is unreadable")

    def unreadable(_name):
        raise injected

    monkeypatch.setattr(loader, "load_subsystem_bundle", unreadable)
    with pytest.raises(RuntimeError, match="configuration") as caught:
        rw.configured_window_size()
    assert caught.value.__cause__ is injected, "the configuration error's cause was destroyed"

    monkeypatch.setattr(loader, "load_subsystem_bundle",
                        lambda _name: {"config": {"reflection_window": {}}})
    with pytest.raises(RuntimeError, match="window_size"):
        rw.configured_window_size()


# ── No self-trigger ──────────────────────────────────────────────────────────


def test_a_reflected_window_without_new_experience_fills_and_ignites_nothing():
    from pressure_loop import window_lane

    store = EpisodeStore()
    carriers = cw.close_cohort(store)
    window_episode = ct.dress_learn(cw.open_window_episode(store))
    window = window_lane.restore_window(window_episode)
    assert window is not None
    assert list(window.episodes) == [carrier.episode_id for carrier in carriers]

    assert window_lane.commit_reflection(window_episode, window, {"status": "reflected"})
    # A retried archive of the same window, inside the same episode, is not fresh evidence.
    assert window_lane.commit_reflection(window_episode, window, {"status": "reflected"})
    assert cw.staged_cohorts() == []
    ct.close(store)  # the window episode's own terminus: reflection work, never experience

    collecting = store.get_active_window()
    assert collecting is None or collecting.episode_count == 0, (
        "the reflection joined fresh membership"
    )
    assert cw.staged_cohorts() == []
    assert window_lane.pressure_contributor() is None
    assert store.get_locked_window() is None, "a reflected window left reflection work behind"


def test_a_reflection_record_is_refused_as_a_member():
    """Defence in depth: even a hand-built window cannot seal a reflection as experience."""
    from pressure_loop.reflection_window import STATUS_LOCKED, ReflectionWindow

    store = EpisodeStore()
    carriers = cw.close_cohort(store, cw.MIXED_PLAN[:9])
    reflection = ct.dress_learn(ct.open_carrier(store, pipeline_id=3, work_kind="reflection"))
    ct.close(store)

    ids = [carrier.episode_id for carrier in carriers] + [reflection.episode_id]
    view = _view()
    collecting = store.get_active_window()
    assert reflection.episode_id not in collecting.episodes, "the append guard admitted it"
    forged = ReflectionWindow(
        window_id="window_forged",
        started_at_episode=ids[0],
        trajectory_snapshot=dict(collecting.trajectory_snapshot),
        episodes=ids,
        cascade_status_by_episode={eid: "completed" for eid in ids},
        member_refs={eid: view.episode_record_ref(eid).to_dict() for eid in ids},
        configuration_fingerprint=collecting.configuration_fingerprint,
        trajectory_revision_at_start=collecting.trajectory_revision_at_start,
        trajectory_revision_at_cutoff=collecting.trajectory_revision_at_start,
        cutoff=cw.cutoff_of(reflection),
        status=STATUS_LOCKED,
    )
    with pytest.raises(WindowMembershipError, match="reflection"):
        _seal(forged)


# ── Staging recovery ─────────────────────────────────────────────────────────


def test_a_crash_between_the_staged_write_and_the_active_clear_leaves_one_cohort(monkeypatch):
    import pressure_loop.reflection_window as rw

    store = EpisodeStore()
    carriers = cw.close_cohort(store, cw.MIXED_PLAN[:9])
    monkeypatch.setattr(rw, "clear_reflection_window",
                        cw.one_shot_crash(rw.clear_reflection_window, "injected: crash after stage"))
    with pytest.raises(RuntimeError, match="crash after stage"):
        carriers.append(cw.close_member(store, "success"))

    assert rw.reflection_window_path().is_file(), "the crash did not leave the active copy"
    staged_before = cw.staged_cohort()

    EpisodeStore()  # restart the reader: boot reconciliation runs
    EpisodeStore()  # and again — recovery is idempotent
    staged = cw.staged_cohort()
    assert staged.window_id == staged_before.window_id
    assert len(staged.episodes) == 10 and len(set(staged.episodes)) == 10
    assert not rw.reflection_window_path().is_file()

    manifest = _seal(staged)
    assert manifest.member_ids == tuple(staged.episodes)


def test_a_crash_between_the_locked_save_and_the_staging_write_recovers_the_cohort(monkeypatch):
    """The locked window reached disk and staging did not. It used to be archived UNREFLECTED
    at the next boot, so ten real episodes were filed away and never looked back over."""
    import pressure_loop.reflection_window as rw
    from pressure_loop import window_lane

    store = EpisodeStore()
    cw.close_cohort(store, cw.MIXED_PLAN[:9])
    monkeypatch.setattr(window_lane, "stage_locked_window",
                        cw.one_shot_crash(window_lane.stage_locked_window,
                                          "injected: crash before stage"))
    with pytest.raises(RuntimeError, match="crash before stage"):
        cw.close_member(store, "success")

    locked = rw.load_reflection_window()
    assert locked is not None and locked.is_ready_for_reflection()
    assert cw.staged_cohorts() == []

    EpisodeStore()  # restart the reader
    staged = cw.staged_cohort()
    assert staged.window_id == locked.window_id
    assert list(staged.episodes) == list(locked.episodes)
    assert not rw.reflection_window_path().is_file()
    archive = rw.reflection_window_archive_dir() / f"{locked.window_id}.json"
    assert not archive.is_file(), "the recovered cohort was archived unreflected"

    EpisodeStore()
    assert len(cw.staged_cohorts()) == 1, "recovery duplicated the cohort"
    assert _seal(staged).member_ids == tuple(locked.episodes)


def test_restarting_the_reader_reproduces_identical_membership_and_digests():
    store = EpisodeStore()
    cw.close_cohort(store)
    first = _seal(cw.staged_cohort())

    EpisodeStore()  # restart the reader
    again = _seal(cw.staged_cohort())
    assert again.member_ids == first.member_ids
    assert again.member_refs == first.member_refs
    assert [row["finish_line_commit_seq"] for row in again.members] == [
        row["finish_line_commit_seq"] for row in first.members
    ]
    assert again.configuration_fingerprint == first.configuration_fingerprint


# ── Checkpoint contract ──────────────────────────────────────────────────────


_CHECKPOINT_FIELDS = {
    "window_id",
    "started_at_episode",
    "trajectory_snapshot",
    "episodes",
    "cascade_status_by_episode",
    "status",
    "locked_at",
    "episode_count",
    "member_refs",
    "configuration_fingerprint",
    "trajectory_revision_at_start",
    "trajectory_revision_at_cutoff",
    "cutoff",
    "manifest",
}


def test_the_checkpoint_pins_each_members_exact_reference_and_copies_no_body():
    import pressure_loop.reflection_window as rw

    store = EpisodeStore()
    carriers = cw.close_cohort(store)
    raw = rw.staged_window_path().read_text(encoding="utf-8")
    rows = json.loads(raw)
    assert len(rows) == 1
    row = rows[0]

    assert set(row) <= _CHECKPOINT_FIELDS, sorted(set(row) - _CHECKPOINT_FIELDS)
    assert "episode_bodies" not in row and "episode_mastery_deltas" not in row
    assert "episode_records" not in row

    view = _view()
    for carrier in carriers:
        pinned = CanonicalRef.from_dict(row["member_refs"][carrier.episode_id])
        assert pinned == view.episode_record_ref(carrier.episode_id)
    assert set(row["member_refs"]) == set(row["episodes"])
    assert len(row["configuration_fingerprint"]) == 64
    assert row["trajectory_revision_at_start"] and row["trajectory_revision_at_cutoff"]

    # Not one byte of any member's body travelled into the checkpoint.
    for sentinel in (ct.SENTINEL_ERROR, ct.SENTINEL_REASON, "basics summary"):
        assert sentinel not in raw, f"a member body leaked into the checkpoint: {sentinel!r}"


def test_the_archive_records_the_sealed_manifest_and_still_no_bodies():
    import pressure_loop.reflection_window as rw
    from pressure_loop import window_lane

    store = EpisodeStore()
    cw.close_cohort(store)
    window_episode = ct.dress_learn(cw.open_window_episode(store))
    window = window_lane.restore_window(window_episode)
    assert window_lane.commit_reflection(window_episode, window, {"status": "reflected"})

    raw = (rw.reflection_window_archive_dir() / f"{window.window_id}.json").read_text(
        encoding="utf-8"
    )
    archived = json.loads(raw)["window"]
    assert set(archived) <= _CHECKPOINT_FIELDS, sorted(set(archived) - _CHECKPOINT_FIELDS)
    from schema.window_evidence import WindowManifest

    manifest = WindowManifest.from_dict(archived["manifest"])
    assert manifest.member_ids == tuple(window.episodes)
    assert manifest.complete is True
    for sentinel in (ct.SENTINEL_ERROR, ct.SENTINEL_REASON, "basics summary"):
        assert sentinel not in raw, f"a member body leaked into the archive: {sentinel!r}"

"""Phase 3 cut 3 — the timekeeper at the deepsleep gate.

Operator design 2026-08-09: memory order is FINISH-LINE arrival, not
ignition order — "it doesn't matter which episode got started first, it
matters the order in which they finished." The gate stamps every arrival
(commit_seq, commit_ms, event_date, lane), ms-precise, total-ordered by a
durable monotonic counter, and the stamp rides the episode into the journal
so every consumer can recover the true sequence even after a same-ms storm.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from core import timekeeper


@pytest.fixture(autouse=True)
def _isolated_counter(tmp_path, monkeypatch):
    import core.paths as paths

    monkeypatch.setattr(paths, "GUARDIAN_STATE_ROOT", tmp_path / "guardian" / "state")
    # Cut 13: the terminus tests below now enter the real commit region, whose
    # epoch seal may scan the episode journal for causal parents. Point that at
    # tmp so a stamp in a test can never read the appliance's own history.
    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path",
        lambda policy=None: tmp_path / "episode_journal.jsonl",
    )
    timekeeper.reset_for_tests()
    yield
    timekeeper.reset_for_tests()


# ── the stamp ────────────────────────────────────────────────────────────────


def test_stamp_shape_carries_the_ratified_fields():
    stamp = timekeeper.finish_line_stamp(0, "operator_turn")

    assert stamp["commit_seq"] == 1
    assert isinstance(stamp["commit_ms"], int) and stamp["commit_ms"] > 0
    assert stamp["pipeline_id"] == 0
    assert stamp["work_kind"] == "operator_turn"
    assert "lane" not in stamp
    assert stamp["event_date"] == stamp["committed_at"][:10]
    assert len(stamp["event_date"]) == 10  # YYYY-MM-DD


def test_sequential_stamps_are_strictly_ordered():
    stamps = [timekeeper.finish_line_stamp(2, "operator_turn") for _ in range(5)]
    seqs = [s["commit_seq"] for s in stamps]
    assert seqs == [1, 2, 3, 4, 5]
    keys = [timekeeper.finish_line_key(s) for s in stamps]
    assert keys == sorted(keys)


def test_storm_of_concurrent_arrivals_gets_a_total_order():
    """A four-lane storm landing in the same millisecond still yields unique,
    gap-free sequence numbers — the stamp lock is the finish-line tape."""
    stamps: list[dict] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def arrive(pipeline_id: int):
        barrier.wait(timeout=5)
        for _ in range(25):
            s = timekeeper.finish_line_stamp(pipeline_id, "operator_turn")
            with lock:
                stamps.append(s)

    threads = [
        threading.Thread(target=arrive, args=(pid,))
        for pid in (0, 1, 2, 3) * 2
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    seqs = sorted(s["commit_seq"] for s in stamps)
    assert seqs == list(range(1, 201))  # unique AND gap-free


def test_counter_survives_a_restart():
    """Memory-organ Cut 13 (D-13-73, block reservation): a restart resumes strictly BEYOND
    the durable reservation boundary — the unissued tail of the old block is a lawful gap,
    a sequence is never reused and order never moves backward. (Revised from the pre-Cut-13
    dense-continuation assertion ``== 3``; density across restarts is no longer promised.)"""
    a = timekeeper.finish_line_stamp(2, "operator_turn")
    b = timekeeper.finish_line_stamp(2, "operator_turn")
    timekeeper.reset_for_tests()  # process restart: in-memory cache dropped

    stamp = timekeeper.finish_line_stamp(0, "operator_turn")
    assert stamp["commit_seq"] > b["commit_seq"] > a["commit_seq"]          # monotonic, never reused
    assert stamp["commit_seq"] == timekeeper.RESERVATION_BLOCK + 1          # resumes beyond the durable block boundary


def test_corrupt_counter_file_degrades_without_crashing(tmp_path, monkeypatch):
    import core.paths as paths

    state = tmp_path / "guardian" / "state"
    monkeypatch.setattr(paths, "GUARDIAN_STATE_ROOT", state)
    state.mkdir(parents=True)
    (state / "timekeeper.json").write_text("{not json", encoding="utf-8")
    timekeeper.reset_for_tests()

    stamp = timekeeper.finish_line_stamp(2, "operator_turn")
    assert stamp["commit_seq"] == 1  # reseeded; commit_ms still orders across the gap


def test_finish_line_key_orders_stamped_after_legacy():
    legacy = timekeeper.finish_line_key(None)
    stamped = timekeeper.finish_line_key(timekeeper.finish_line_stamp(2, "operator_turn"))
    assert legacy < stamped
    assert timekeeper.finish_line_key({"commit_ms": "junk"}) == (0, 0)


# ── the gate ─────────────────────────────────────────────────────────────────
#
# Phase 4 cut 13 moved the stamp OFF the gate's doorstep. It used to be
# ``run_deepsleep_for_episode``'s first statement, and that equalled the landed
# order only because one process-wide lock spanned the whole terminus. With the
# lane-local half unserialized, the stamp is the first statement inside the ONE
# commit lock instead (core.commit_region.serialized_commit). The gate is still
# the single place every episode passes; the finish-line tape now hangs one seam
# deeper, where the writes are.


def _committing_core(episode):
    """A terminus stub that does what the real one does: enter the region."""
    from core.commit_region import serialized_commit

    def _core(*_a, **_k):
        with serialized_commit(episode):
            return SimpleNamespace(pack=True)

    return _core


def test_the_terminus_stamps_inside_the_commit_region(monkeypatch):
    """The carrier reaches store.close UNSTAMPED and leaves the commit region
    stamped with its own lane."""
    from deepsleep_loop.loop import run_deepsleep_for_episode

    episode = SimpleNamespace(
        episode_id="ep-gate", pipeline_id=0, work_kind="operator_turn", timekeeper={}, closed_at="", opened_at=""
    )
    seen: dict = {}

    class FakeStore:
        def current(self):
            return episode

        # after_archive (cross-lane T6): accepted to match the real close() contract, and
        # deliberately never called — the real seam fires only after a successful archive,
        # and this double performs no archive.
        def close(self, *, cascade_status="completed", before_finalize=None, after_archive=None):
            seen["stamp_at_close"] = dict(episode.timekeeper)
            before_finalize(episode, {"episode_id": "ep-gate"})
            return episode

    monkeypatch.setattr(
        "deepsleep_loop.loop._run_deepsleep_episode_core", _committing_core(episode)
    )

    pack = run_deepsleep_for_episode(FakeStore())
    assert pack is not None
    assert seen["stamp_at_close"] == {}  # cut 13: not stamped at the door
    assert episode.timekeeper["pipeline_id"] == 0
    assert episode.timekeeper["commit_seq"] >= 1


def test_two_arrivals_order_by_finish_not_ignition(monkeypatch):
    """The episode that reaches the COMMIT REGION first carries the earlier
    stamp, regardless of which was opened first."""
    from deepsleep_loop.loop import run_deepsleep_for_episode

    opened_first = SimpleNamespace(
        episode_id="ep-a", pipeline_id=0, work_kind="operator_turn", timekeeper={}, closed_at="", opened_at="t0"
    )
    opened_second = SimpleNamespace(
        episode_id="ep-b", pipeline_id=1, work_kind="operator_lane_work", timekeeper={}, closed_at="", opened_at="t1"
    )

    def store_for(ep):
        class S:
            def current(self):
                return ep

            # after_archive (T6): matches the real contract; never called, since the
            # real seam fires only after a successful archive and this double archives nothing.
            def close(self, *, cascade_status="completed", before_finalize=None,
                      after_archive=None):
                before_finalize(ep, {"episode_id": ep.episode_id})
                return ep

        return S()

    # The LATER-opened episode finishes first (fast chat vs slow agent build).
    monkeypatch.setattr(
        "deepsleep_loop.loop._run_deepsleep_episode_core",
        _committing_core(opened_second),
    )
    run_deepsleep_for_episode(store_for(opened_second))
    monkeypatch.setattr(
        "deepsleep_loop.loop._run_deepsleep_episode_core",
        _committing_core(opened_first),
    )
    run_deepsleep_for_episode(store_for(opened_first))

    from core.timekeeper import finish_line_key

    assert finish_line_key(opened_second.timekeeper) < finish_line_key(
        opened_first.timekeeper
    )
    assert opened_first.timekeeper["pipeline_id"] == 0
    assert opened_second.timekeeper["pipeline_id"] == 1


# ── the journal ──────────────────────────────────────────────────────────────


def test_prior_episode_round_trips_lane_and_stamp():
    from pressure_loop.episode_journal import PriorEpisode, prior_episode_from_episode

    ep = SimpleNamespace(
        episode_id="ep-rt",
        opened_by="operator_query",
        pipeline_id=0, work_kind="operator_turn",
        timekeeper=timekeeper.finish_line_stamp(0, "operator_turn"),
        global_={},
        meta={},
        cascade_status="completed",
    )
    pe = prior_episode_from_episode(ep)
    assert pe.pipeline_id == 0
    assert pe.timekeeper["commit_seq"] == ep.timekeeper["commit_seq"]

    again = PriorEpisode.from_dict(pe.to_dict())
    assert again.pipeline_id == 0
    assert again.timekeeper == pe.timekeeper


def test_journal_window_resorts_by_finish_line(tmp_path, monkeypatch):
    """Appends land in close-completion order, which can lag arrival by a
    race — the window re-sorts stamped rows by (commit_ms, commit_seq),
    keeping unstamped legacy rows first (they predate the timekeeper)."""
    import json as _json

    from pressure_loop.episode_journal import journal_window

    path = tmp_path / "episode_journal.jsonl"
    s1 = timekeeper.finish_line_stamp(0, "operator_turn")
    s2 = timekeeper.finish_line_stamp(2, "operator_turn")
    s2 = dict(s2, commit_ms=s1["commit_ms"])  # same-ms storm: seq breaks the tie
    rows = [
        {"episode_id": "legacy", "ignition_reason": "curiosity_gap"},
        {"episode_id": "second", "ignition_reason": "omega_level", "pipeline_id": 2, "work_kind": "operator_turn", "timekeeper": s2},
        {"episode_id": "first", "ignition_reason": "operator_query", "pipeline_id": 0, "work_kind": "operator_turn", "timekeeper": s1},
    ]
    path.write_text("".join(_json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    window = journal_window(10, path=path)
    assert [pe.episode_id for pe in window] == ["legacy", "first", "second"]


def test_enriched_body_carries_lane_and_stamp():
    from pressure_loop.episode import CognitiveEpisode
    from pressure_loop.episode_journal import enriched_episode_from_episode

    ep = CognitiveEpisode(opened_by="operator_query", pipeline_id=0, work_kind="operator_turn")
    ep.timekeeper = timekeeper.finish_line_stamp(0, "operator_turn")
    body = enriched_episode_from_episode(ep)

    assert body["pipeline_id"] == 0
    assert body["timekeeper"]["pipeline_id"] == 0
    assert body["timekeeper"]["commit_seq"] == ep.timekeeper["commit_seq"]

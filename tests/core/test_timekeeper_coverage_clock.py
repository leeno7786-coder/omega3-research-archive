"""Memory-organ Cut 13 — coverage clock + block reservation (D-13-59/64/73/74/77/78,
review c21937bc errata 1–2): strict reservation authority, zero-I/O stamps, gaps lawful,
no reuse across restart, entrance headroom before any lane-local write, unconfirmed
reservation posture, landed frontier ≠ reserved, `_LOCK` never held during disk I/O."""

from __future__ import annotations

import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from core import timekeeper as TK
from core.timekeeper import CoverageClock, CoverageClockUnavailable, TimekeeperUnavailable


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    import core.paths as paths

    monkeypatch.setattr(paths, "GUARDIAN_STATE_ROOT", tmp_path / "guardian" / "state")
    monkeypatch.setattr("pressure_loop.policy.episode_journal_path", lambda policy=None: tmp_path / "episode_journal.jsonl")
    TK.reset_for_tests()
    yield
    TK.reset_for_tests()


def _authority():
    return json.loads(TK._state_path().read_text(encoding="utf-8"))


def test_constants_from_the_real_lane_topology():
    from core.pipelines import PIPELINE_IDS
    assert TK.MAX_CONCURRENT_TERMINI == len(PIPELINE_IDS) == 4
    assert TK.MIN_SAFE_STAMP_HEADROOM == 5
    assert TK.RESERVE_AHEAD_THRESHOLD == 128 and TK.RESERVATION_BLOCK == 256


def test_missing_state_mints_a_strictly_persisted_epoch_that_survives_a_cache_reset():
    clock = TK.last_coverage_clock()
    assert len(clock.clock_epoch_id) == 32 and clock.commit_seq == 0
    doc = _authority()
    assert doc == {"clock_epoch_id": clock.clock_epoch_id, "reserved_through": TK.RESERVATION_BLOCK}
    TK.reset_for_tests()
    assert TK.last_coverage_clock().clock_epoch_id == clock.clock_epoch_id      # durable identity survives


def test_legacy_last_seq_file_migrates_once_and_keeps_the_counter():
    TK._state_path().parent.mkdir(parents=True)
    TK._state_path().write_text(json.dumps({"last_seq": 41}), encoding="utf-8")
    stamp = TK.finish_line_stamp(2, work_kind="autonomous")
    assert stamp["commit_seq"] == 42                                            # first new stamp = last_seq + 1
    doc = _authority()
    epoch = doc["clock_epoch_id"]
    assert doc["reserved_through"] == 41 + TK.RESERVATION_BLOCK
    TK.reset_for_tests()
    assert TK.last_coverage_clock().clock_epoch_id == epoch                     # one id, minted once


def test_corrupt_state_mints_a_new_epoch_never_recreated_from_a_number():
    TK._state_path().parent.mkdir(parents=True)
    TK._state_path().write_text("{not json", encoding="utf-8")
    c1 = TK.last_coverage_clock()
    TK.reset_for_tests()
    TK._state_path().write_text("{not json", encoding="utf-8")                   # lost again ⇒ ANOTHER new epoch
    c2 = TK.last_coverage_clock()
    assert c1.clock_epoch_id != c2.clock_epoch_id


def test_block_reservation_gap_on_restart_never_reuse():
    TK._state_path().parent.mkdir(parents=True)
    TK._state_path().write_text(json.dumps({"clock_epoch_id": "e" * 32, "reserved_through": 10000}), encoding="utf-8")
    seqs = [TK.finish_line_stamp(2, work_kind="autonomous")["commit_seq"] for _ in range(173)]
    assert seqs[0] == 10001 and seqs[-1] == 10173                                # issued from the block, no disk write per stamp
    TK.reset_for_tests()                                                         # crash + restart
    assert TK.finish_line_stamp(2, work_kind="autonomous")["commit_seq"] == 10001 + TK.RESERVATION_BLOCK   # beyond the durable boundary: gap, never reuse


def test_stamp_performs_no_file_io_and_reservation_never_holds_LOCK_during_io(monkeypatch):
    TK.last_coverage_clock()                                                     # established
    io_events = []
    real_fsync = os.fsync

    def spy_fsync(fd):
        io_events.append(("fsync", TK._LOCK.locked()))
        return real_fsync(fd)

    real_replace = os.replace

    def spy_replace(a, b):
        io_events.append(("replace", TK._LOCK.locked()))
        return real_replace(a, b)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)
    for _ in range(50):
        TK.finish_line_stamp(2, work_kind="autonomous")
    assert io_events == []                                                       # zero-I/O stamps
    for _ in range(100):                                                         # cross the 128 threshold via the entrance
        TK.finish_line_stamp(2, work_kind="autonomous")
    TK.ensure_stamp_headroom()
    assert io_events and all(locked is False for _, locked in io_events)         # _LOCK never held during temp/fsync/replace/parent sync
    assert _authority()["reserved_through"] == 2 * TK.RESERVATION_BLOCK


def test_entrance_refuses_only_below_the_floor_and_before_lane_local_work(monkeypatch):
    TK.last_coverage_clock()
    # exhaust to 5 confirmed numbers, with reserve-ahead failing
    for _ in range(TK.RESERVATION_BLOCK - 7):
        TK.finish_line_stamp(2, work_kind="autonomous")
    monkeypatch.setattr(TK, "_strict_write_authority", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("disk full")))
    assert TK.ensure_stamp_headroom() == 7                                       # 7 >= 6: admissible although reserve-ahead failed
    TK.finish_line_stamp(2, work_kind="autonomous")
    assert TK.ensure_stamp_headroom() == 6                                       # exactly the floor is admissible (errata 1)
    TK.finish_line_stamp(2, work_kind="autonomous")
    assert TK.ensure_stamp_headroom() == 5
    TK.finish_line_stamp(2, work_kind="autonomous")
    with pytest.raises(TimekeeperUnavailable):                                   # 4 < 5: refuse before local writes
        TK.ensure_stamp_headroom()
    # already-admitted termini can still stamp without disk I/O
    for _ in range(4):
        TK.finish_line_stamp(2, work_kind="autonomous")
    with pytest.raises(TimekeeperUnavailable):                                   # block exhausted and reservation failing
        TK.finish_line_stamp(2, work_kind="autonomous")
    monkeypatch.setattr(os, "replace", real_replace)                             # disk back: the next entrance reserves ahead
    assert TK.ensure_stamp_headroom() >= TK.RESERVE_AHEAD_THRESHOLD


def test_deepsleep_terminus_refuses_at_the_entrance_with_nothing_written(monkeypatch):
    TK.last_coverage_clock()
    for _ in range(TK.RESERVATION_BLOCK - 3):
        TK.finish_line_stamp(2, work_kind="autonomous")
    from deepsleep_loop import loop as DL
    touched = []
    monkeypatch.setattr(DL, "_phase_runs", lambda *a, **k: touched.append("phase") or False)
    ep = SimpleNamespace(episode_id="e1", pipeline_id=0, work_kind="operator_turn", memory_epoch_start=0)
    with monkeypatch.context() as failure:
        failure.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(TimekeeperUnavailable):
            DL._run_deepsleep_episode_core(object(), "e1", lambda: touched.append("intake"), closing_episode=ep, episode_body={}, scope="episode")
    assert touched == []                                                         # intake / mastery / failure / SRAM never ran


def test_unconfirmed_reservation_after_replace_but_failed_parent_sync(monkeypatch):
    TK.last_coverage_clock()
    for _ in range(TK.RESERVATION_BLOCK - 120):                                  # confirmed headroom 120 < 128
        TK.finish_line_stamp(2, work_kind="autonomous")
    real = TK._fsync_dir
    monkeypatch.setattr(TK, "_fsync_dir", lambda d: (_ for _ in ()).throw(OSError("parent sync failed")))
    TK.ensure_stamp_headroom()                                                   # replace succeeded, parent sync failed
    st = TK.reservation_state()
    assert st["unconfirmed_through"] == 2 * TK.RESERVATION_BLOCK and st["reserved_through"] == TK.RESERVATION_BLOCK
    assert _authority()["reserved_through"] == 2 * TK.RESERVATION_BLOCK           # visible on disk, durability unknown
    for _ in range(120):                                                         # confirmed old-block slack still usable
        TK.finish_line_stamp(2, work_kind="autonomous")
    with pytest.raises(TimekeeperUnavailable):                                   # never issue beyond the confirmed boundary
        TK.finish_line_stamp(2, work_kind="autonomous")
    monkeypatch.setattr(TK, "_fsync_dir", real)
    TK.ensure_stamp_headroom()                                                   # parent sync retried and confirmed
    assert TK.reservation_state()["unconfirmed_through"] is None
    assert TK.finish_line_stamp(2, work_kind="autonomous")["commit_seq"] == TK.RESERVATION_BLOCK + 1
    # either restart outcome is safe: durable higher ⇒ start beyond it (gap only)
    TK.reset_for_tests()
    assert TK.finish_line_stamp(2, work_kind="autonomous")["commit_seq"] > 2 * TK.RESERVATION_BLOCK


def test_landed_frontier_is_not_the_reserved_counter():
    from core.commit_region import serialized_commit
    import memory.epoch as EP
    ep = SimpleNamespace(episode_id="e1", pipeline_id=0, work_kind="operator_turn", memory_epoch_start=0)
    with serialized_commit(ep) as stamp:                                         # stamped, body in progress: NOT landed
        assert TK.last_commit_seq() == stamp["commit_seq"] == 1
        assert TK.last_coverage_clock().commit_seq == 0
    assert TK.last_coverage_clock().commit_seq == 1                              # body exited normally ⇒ landed
    ep2 = SimpleNamespace(episode_id="e2", pipeline_id=0, work_kind="operator_turn", memory_epoch_start=1)
    with pytest.raises(RuntimeError):
        with serialized_commit(ep2):
            raise RuntimeError("body failed")                                    # stamped 2 then raised ⇒ BURNED
    assert TK.last_commit_seq() == 2 and TK.last_coverage_clock().commit_seq == 2   # frontier advances across the burned number
    ep3 = SimpleNamespace(episode_id="e3", pipeline_id=0, work_kind="operator_turn", memory_epoch_start=2)
    with serialized_commit(ep3):
        pass
    assert TK.last_coverage_clock().commit_seq == 3
    st = TK.reservation_state()
    assert st["last_issued"] == 3 and st["landed_seq"] == 3 and st["reserved_through"] == TK.RESERVATION_BLOCK


def test_reconcile_landed_only_same_epoch_and_never_above_issued():
    clock = TK.last_coverage_clock()
    TK.reconcile_landed(500, clock_epoch_id="other" * 6 + "ab")
    assert TK.last_coverage_clock().commit_seq == 0
    TK.reconcile_landed(500, clock_epoch_id=clock.clock_epoch_id)
    assert TK.last_coverage_clock().commit_seq == 0                              # capped at last_issued


def test_two_distinct_commits_never_share_a_clock_across_a_restart_and_seq_write_failure(monkeypatch):
    TK.last_coverage_clock()
    a = TK.finish_line_stamp(2, work_kind="autonomous")
    b = TK.finish_line_stamp(2, work_kind="autonomous")
    TK.reset_for_tests()
    c = TK.finish_line_stamp(2, work_kind="autonomous")
    assert len({a["commit_seq"], b["commit_seq"], c["commit_seq"]}) == 3
    assert c["commit_seq"] > b["commit_seq"]


def test_four_pipelines_entering_under_slow_reservation_io_never_stall_the_stamp(monkeypatch):
    TK.last_coverage_clock()
    for _ in range(TK.RESERVATION_BLOCK - 120):
        TK.finish_line_stamp(2, work_kind="autonomous")                                             # headroom 120 → next entrance reserves ahead
    real_fsync = os.fsync
    slow = threading.Event()

    def slow_fsync(fd):
        slow.set()
        time.sleep(1.5)                                                          # artificially slow reservation disk I/O
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", slow_fsync)
    t = threading.Thread(target=TK.ensure_stamp_headroom)
    t.start()
    assert slow.wait(timeout=5)
    started = time.monotonic()
    stamps = [TK.finish_line_stamp(pid, "autonomous")["commit_seq"] for pid in (0, 1, 2, 3)]
    elapsed = time.monotonic() - started
    assert elapsed < 0.5 and len(set(stamps)) == 4                                # stamps did not wait for the fsync
    # a safe entrance skips the busy writer (errata 2) rather than queueing behind it
    started = time.monotonic()
    assert TK.ensure_stamp_headroom() >= TK.MIN_SAFE_STAMP_HEADROOM
    assert time.monotonic() - started < 0.5
    t.join(timeout=10)
    assert _authority()["reserved_through"] == 2 * TK.RESERVATION_BLOCK           # exactly one strict write for one target


def test_reset_for_tests_clears_all_runtime_fields():
    TK.last_coverage_clock(); TK.finish_line_stamp(2, work_kind="autonomous")
    TK.reset_for_tests()
    assert TK._state is None and TK._landed_floor == 0


def test_coverage_clock_unavailable_blocks_before_any_candidate(monkeypatch):
    monkeypatch.setattr(TK, "_strict_write_authority", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs")))
    with pytest.raises(CoverageClockUnavailable):
        TK.last_coverage_clock()
    with pytest.raises(TimekeeperUnavailable):
        TK.finish_line_stamp(2, work_kind="autonomous")
    assert not TK._state_path().exists()

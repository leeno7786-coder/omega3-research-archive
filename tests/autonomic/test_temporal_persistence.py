"""Lifetime clock persistence — temporal_state.json across reboots (operator 2026-07-16).

Today's temporal grounding is session-only: every boot resets epoch/cycle/tick to 0, so
"how long has Omega existed" and "how long was it offline" are unanswerable across restarts.
This persists a lifetime clock: first_boot, last_boot, last_shutdown, boot_count, lifetime
epochs/cycles/seconds, and whether the last shutdown was clean. Pure + deterministic — `now`
and the file path are injected, so (state, now) → the same result every time.
"""

from __future__ import annotations

import json

from autonomic.temporal_grounding import temporal_persistence as tp


def _path(tmp_path):
    return tmp_path / "temporal_state.json"


def test_first_boot_seeds_state(tmp_path):
    p = _path(tmp_path)
    view = tp.record_boot(now=1000.0, path=p)
    assert view["first_boot_at"] == 1000.0
    assert view["last_boot_at"] == 1000.0
    assert view["boot_count"] == 1
    assert view["offline_seconds"] is None  # no prior shutdown to measure from
    assert view["prior_clean_shutdown"] is None  # first boot has no prior session at all
    # persisted
    saved = json.loads(p.read_text(encoding="utf-8"))
    assert saved["first_boot_at"] == 1000.0
    assert saved["boot_count"] == 1
    assert saved["last_was_clean_shutdown"] is False  # not clean until a clean shutdown


def test_second_boot_after_clean_shutdown_measures_offline(tmp_path):
    p = _path(tmp_path)
    tp.record_boot(now=1000.0, path=p)
    tp.record_shutdown(
        now=1100.0, session_epochs=5, session_cycles=20, session_seconds=100.0, path=p
    )
    view = tp.record_boot(now=1500.0, path=p)  # booted 400s after shutdown
    assert view["boot_count"] == 2
    assert view["first_boot_at"] == 1000.0  # preserved across boots
    assert view["offline_seconds"] == 400.0
    assert view["prior_clean_shutdown"] is True


def test_a_crash_that_still_tears_down_is_recorded_unclean(tmp_path):
    """Operator 2026-09-15: a crash walked the graceful teardown and was counted clean."""
    p = _path(tmp_path)
    tp.record_boot(now=1000.0, path=p)
    state = tp.record_shutdown(now=1100.0, session_seconds=100.0, path=p, clean=False)
    assert state["last_was_clean_shutdown"] is False and state["last_shutdown_at"] == 1100.0
    assert state["lifetime_seconds"] == 100.0  # uptime still counts
    view = tp.record_boot(now=1500.0, path=p)
    assert view["prior_clean_shutdown"] is False and view["offline_seconds"] is None


def test_unclean_shutdown_detected_on_next_boot(tmp_path):
    p = _path(tmp_path)
    tp.record_boot(now=1000.0, path=p)
    # no record_shutdown — process was killed. Next boot must detect it.
    view = tp.record_boot(now=1500.0, path=p)
    assert view["boot_count"] == 2
    assert view["prior_clean_shutdown"] is False
    # offline_seconds is unknown when there was no clean shutdown stamp
    assert view["offline_seconds"] is None


def test_lifetime_accumulates_across_sessions(tmp_path):
    p = _path(tmp_path)
    # session 1
    tp.record_boot(now=0.0, path=p)
    tp.record_shutdown(
        now=100.0, session_epochs=10, session_cycles=40, session_seconds=100.0, path=p
    )
    s1 = json.loads(p.read_text(encoding="utf-8"))
    assert s1["lifetime_epochs"] == 10
    assert s1["lifetime_seconds"] == 100.0
    # session 2 adds ON TOP
    tp.record_boot(now=200.0, path=p)
    tp.record_shutdown(now=250.0, session_epochs=3, session_cycles=12, session_seconds=50.0, path=p)
    s2 = json.loads(p.read_text(encoding="utf-8"))
    assert s2["lifetime_epochs"] == 13
    assert s2["lifetime_cycles"] == 52
    assert s2["lifetime_seconds"] == 150.0


def test_checkpoint_is_idempotent_within_a_session(tmp_path):
    # Re-checkpointing with the same session totals must NOT double-count (base snapshot model).
    p = _path(tmp_path)
    tp.record_boot(now=0.0, path=p)
    tp.record_shutdown(now=50.0, session_epochs=4, session_cycles=16, session_seconds=50.0, path=p)
    tp.record_boot(now=100.0, path=p)
    tp.checkpoint(session_epochs=2, session_cycles=8, session_seconds=20.0, path=p)
    tp.checkpoint(
        session_epochs=2, session_cycles=8, session_seconds=20.0, path=p
    )  # same totals again
    s = json.loads(p.read_text(encoding="utf-8"))
    assert s["lifetime_epochs"] == 6  # 4 (base) + 2, not 8
    assert s["lifetime_seconds"] == 70.0  # 50 + 20


def test_checkpoint_keeps_shutdown_unclean_until_recorded(tmp_path):
    p = _path(tmp_path)
    tp.record_boot(now=0.0, path=p)
    tp.checkpoint(session_epochs=1, session_cycles=4, session_seconds=10.0, path=p)
    s = json.loads(p.read_text(encoding="utf-8"))
    assert s["last_was_clean_shutdown"] is False  # a checkpoint is not a clean shutdown
    tp.record_shutdown(now=10.0, session_epochs=1, session_cycles=4, session_seconds=10.0, path=p)
    s = json.loads(p.read_text(encoding="utf-8"))
    assert s["last_was_clean_shutdown"] is True
    assert s["last_shutdown_at"] == 10.0


def test_corrupt_state_file_is_failsoft(tmp_path):
    p = _path(tmp_path)
    p.write_text("garbage {{{", encoding="utf-8")
    view = tp.record_boot(now=500.0, path=p)  # must not raise
    assert view["boot_count"] == 1  # treated as a fresh first boot
    assert view["first_boot_at"] == 500.0


def test_load_state_missing_returns_defaults(tmp_path):
    st = tp.load_state(path=_path(tmp_path))
    assert st["boot_count"] == 0
    assert st["first_boot_at"] is None
    assert st["lifetime_seconds"] == 0.0

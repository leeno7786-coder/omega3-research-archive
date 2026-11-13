"""A named drain must retain ownership and never discard later intake."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from memory import episode_sram as sram


def test_named_drain_binds_nested_intake_then_restores_the_caller():
    sram.open_bucket("episode-a", pipeline_id=0)
    sram.hold_turn("user", "original")
    sram.open_bucket("episode-b", pipeline_id=1)
    observed = []

    def consolidate(items):
        observed.append((sram.bound_episode_id(), sram.is_draining()))
        sram.hold_turn("assistant", "nested")
        return {"turns": [item.text for item in items]}

    sram.flush("episode-a", consolidate=consolidate)
    assert observed == [("episode-a", True)]
    assert sram.bound_episode_id() == "episode-b"
    assert not sram.is_draining()
    assert [item.text for item in sram.pending("episode-a")] == ["nested"]
    assert sram.pending("episode-b") == []


def test_close_preserves_items_appended_after_the_drain_snapshot():
    sram.open_bucket("episode-a", pipeline_id=0)
    sram.hold_turn("user", "original")

    def consolidate(items):
        sram.hold_turn("assistant", "arrived during drain")
        return {"turns": [item.text for item in items]}

    with pytest.raises(RuntimeError, match="pending"):
        sram.close_bucket("episode-a", consolidate=consolidate)
    assert [item.text for item in sram.pending("episode-a")] == ["arrived during drain"]
    sram.close_bucket("episode-a", consolidate=lambda items: {})
    assert not sram.is_open("episode-a")


def test_cleanup_cannot_erase_pending_intake():
    sram.open_bucket("episode-a", pipeline_id=0)
    sram.hold_turn("user", "not committed")
    with pytest.raises(RuntimeError, match="pending"):
        sram.clear_bucket("episode-a")
    assert [item.text for item in sram.pending("episode-a")] == ["not committed"]


def test_a_second_drain_cannot_execute_the_same_batch():
    sram.open_bucket("episode-a", pipeline_id=0)
    sram.hold_turn("user", "one batch")
    entered, release = Event(), Event()
    calls = []

    def consolidate(items):
        calls.append([item.text for item in items])
        entered.set()
        assert release.wait(5), "test did not release the drain"
        return {"turns": calls[-1]}

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(sram.flush, "episode-a", consolidate=consolidate)
        assert entered.wait(5), "first drain did not start"
        try:
            with pytest.raises(RuntimeError, match="drain"):
                sram.flush("episode-a", consolidate=lambda items: {})
        finally:
            release.set()
        first.result(timeout=5)
    assert calls == [["one batch"]]
    assert sram.pending("episode-a") == []

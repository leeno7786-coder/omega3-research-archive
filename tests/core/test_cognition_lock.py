"""Cognition entry serialization — HUD thread and pressure loop share one lock."""

from __future__ import annotations

import threading
import time

from core.cognition_lock import pipeline_cognition_lock


def test_cognition_lock_is_reentrant():
    lock = pipeline_cognition_lock(0)
    with lock:
        with lock:
            assert lock._is_owned()  # noqa: SLF001 — RLock ownership check


def test_cognition_lock_serializes_concurrent_holders():
    lock = pipeline_cognition_lock(0)
    order: list[str] = []
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        barrier.wait(timeout=2)
        with lock:
            order.append(f"{name}-enter")
            time.sleep(0.05)
            order.append(f"{name}-exit")

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert len(order) == 4
    assert order.index("a-enter") < order.index("a-exit")
    assert order.index("b-enter") < order.index("b-exit")
    # One worker fully completes before the other enters.
    assert (order[1] == "a-exit" and order[2] == "b-enter") or (
        order[1] == "b-exit" and order[2] == "a-enter"
    )


def test_test_reset_replaces_a_lock_left_by_a_failed_worker():
    import core.cognition_lock as locks

    stale = locks.pipeline_cognition_lock(0)
    acquired = threading.Event()

    def failed_worker() -> None:
        stale.acquire()
        acquired.set()
        # Simulate fail-hard process death: the worker exits without releasing.

    worker = threading.Thread(target=failed_worker)
    worker.start()
    worker.join(timeout=2)

    assert acquired.is_set() and not worker.is_alive()
    assert stale.acquire(blocking=False) is False

    locks.reset_for_tests()
    fresh = locks.pipeline_cognition_lock(0)
    assert fresh is not stale
    assert fresh.acquire(blocking=False) is True
    fresh.release()

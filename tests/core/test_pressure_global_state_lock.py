"""Medium #13 — pressure global state mutated outside any lock (HUD-thread vs
pressure-tick race).

The pressure tick runs on boot.py's main thread; the HUD runs on a separate
thread and, on a routine operator chat, calls evaluate_operator_ignition ->
gather_contributors() + evaluate() on that HUD thread. Those paths do
read-modify-write of three process-global stores with no lock:

  * pressure._RESEED_STAGING  (stage_reseed append  vs gather drain+clear)
  * pressure._RESEED_SUPPRESS (stage_reseed_suppress vs gather drain+clear)

A lost reseed-staging entry drops a DeepSleep-prepared contributor; a
double-decayed setpoint expires Omega's prepared pressure a window early.
Both are silent cognition-state corruptions (invariant 6 honest-signals).
operator_queue.py is correctly lock-guarded; these globals were missed.

These tests prove each critical path actually acquires its module lock: a
worker thread calling the guarded function must BLOCK while the test thread
holds that same lock. Without the lock the call completes immediately
(unguarded) and the assertion fails.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pressure_loop.pressure as p  # noqa: E402
from pressure_loop.contributors import make_contributor  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate():
    p._RESEED_STAGING.clear()
    p._RESEED_SUPPRESS.clear()
    yield
    p._RESEED_STAGING.clear()
    p._RESEED_SUPPRESS.clear()


def _completes_while_lock_held(lock: "threading.RLock", call) -> bool:
    """Run `call` on a worker thread while THIS thread holds `lock`.

    Returns True if `call` completed despite the lock being held (i.e. it did
    NOT acquire `lock` — unguarded). Returns False if it blocked on the lock
    (guarded). Deterministic: the worker is known-started before we time the
    completion wait.
    """
    started = threading.Event()
    done = threading.Event()

    def worker() -> None:
        started.set()
        try:
            call()
        finally:
            done.set()

    t = threading.Thread(target=worker, daemon=True)
    with lock:
        t.start()
        assert started.wait(1.0), "worker thread never started"
        completed_under_lock = done.wait(0.25)
    t.join(1.0)
    assert done.is_set(), "call never completed even after lock release"
    return completed_under_lock


def _contrib():
    return make_contributor("autonomous", "curiosity_gap", 0.5, {"id": "x"})


# ── _RESEED_STAGING / _RESEED_SUPPRESS guarded by _RESEED_LOCK ────────────────
def test_stage_reseed_acquires_reseed_lock():
    assert _completes_while_lock_held(p._RESEED_LOCK, lambda: p.stage_reseed([_contrib()])) is False


def test_read_reseed_staging_acquires_reseed_lock():
    assert _completes_while_lock_held(p._RESEED_LOCK, p._read_reseed_staging) is False


def test_release_staged_acquires_reseed_lock():
    assert (
        _completes_while_lock_held(p._RESEED_LOCK, lambda: p.release_staged(_contrib()))
        is False
    )


def test_drain_reseed_suppress_acquires_reseed_lock():
    assert _completes_while_lock_held(p._RESEED_LOCK, p._drain_reseed_suppress) is False


def test_reading_staging_does_not_consume_it():
    """Read no longer clears (operator 2026-08-15): a signal dies at IGNITION.

    Draining on read gave a staged signal exactly one contest — lose it and the
    signal was destroyed unheard, which parked autonomous cognition for 7m23s on
    2026-08-15. Note both _contrib() calls are the same (type, domain), so they
    are ONE signal: identity replaces rather than queueing a duplicate.
    """
    p.stage_reseed([_contrib(), _contrib()])
    assert len(p._read_reseed_staging()) == 1
    assert len(p._read_reseed_staging()) == 1  # still there for the next contest

    assert p.release_staged(_contrib()) is True  # ignition retires it
    assert p._RESEED_STAGING == {}


def test_drain_reseed_suppress_is_atomic_read_then_clear():
    p.stage_reseed_suppress("learning_debt", 0.4)
    drained = p._drain_reseed_suppress()
    assert drained == {"learning_debt": 0.4}
    assert p._RESEED_SUPPRESS == {}


# ── omega_levels._LEVELS guarded by _LEVELS_LOCK ──────────────────────────────
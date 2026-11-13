"""Per-store write locks for the DeepSleep JSON stores.

Phase 4 cut 13's named hazard. Every one of these stores is a read-modify-write
against a whole JSON file — ``load_x()`` parses the map, the caller mutates one
key, ``write_text_atomic`` replaces the file. ``write_text_atomic`` makes the
*file* safe (a crash never leaves a truncated half-write), and that was enough
while ONE process-wide lock meant only one deepsleep terminus could ever be
inside the sequence. Cut 13 makes the consolidation phases lane-local, so two
lanes consolidate at once — and two interleaved read-modify-writes lose one of
them silently: A reads, B reads, A writes, B writes over A. A mastery credit or
a promoted semantic entry simply never happened, with no error anywhere.

The lock is per STORE, not one lock over all of them: the stores are
independent files with no cross-store invariant, and a single lock would
re-serialize the consolidation half this cut just freed.

Seven stores (the eight ``deepsleep_loop/*_store.py`` modules minus
``identity_store``, whose durable owner is canonical memory rather than a JSON
map — its writes go through the canonical write path and never read-modify-write
a file here).

Re-entrant by necessity, not by taste: ``upsert_mastery`` calls
``load_mastery`` INSIDE the region it must hold, because the read-modify-write
is the thing being made atomic. A plain ``threading.Lock`` would deadlock on
the second acquisition, so the guard would have had to be a private unlocked
loader duplicated in seven modules — one more copy of the parse per store, and
one more place for the two loaders to drift apart.

**``guarded`` is not enough on its own** (cut 13 review, measured 2026-08-11).
It makes each ACCESSOR atomic, which stops one lane's whole-file write from
erasing another lane's unrelated keys. It cannot see the transaction the CALLER
is running, and every consolidation phase runs one: ``load_x()`` → compute from
what was read → ``upsert_x()``/``save_x()``. Two lanes inside that sequence read
the same map and the second write erases the first — the loss ``guarded``
exists to prevent, moved one frame out. Measured on this tree with the guards
in place: 6 threads × 12 trajectory writes landed 33 of 72 records, and 72
mastery credits against one domain landed 23. So a caller that reads a store
and writes back what it computed holds ``store_transaction`` across BOTH.

**One store per transaction.** Every wrapped region touches exactly one store,
and that is a rule, not an observation: two store locks held at once is a lock
pair, a pair is an order, and an order nobody wrote down is the deadlock this
phase's sibling module (``core.cognition_lock``) exists to make impossible. A
phase that needs two stores atomically does not belong here — it belongs in the
serialized commit region.
"""

from __future__ import annotations

import functools
import threading
from contextlib import contextmanager
from typing import Callable, Iterator, TypeVar

MASTERY = "mastery"
FAILURE = "failure"
SEMANTIC = "semantic"
QUERY = "query"
TRAJECTORY = "trajectory"
JOURNAL = "journal"
PROJECTION = "projection"

STORE_NAMES = (
    MASTERY,
    FAILURE,
    SEMANTIC,
    QUERY,
    TRAJECTORY,
    JOURNAL,
    PROJECTION,
)

_LOCKS: dict[str, threading.RLock] = {name: threading.RLock() for name in STORE_NAMES}


def store_lock(name: str) -> threading.RLock:
    """The lock for one named store. An unknown name is a programming error."""
    lock = _LOCKS.get(str(name))
    if lock is None:
        raise ValueError(
            f"no deepsleep store lock named {name!r} (known: {', '.join(STORE_NAMES)})"
        )
    return lock


@contextmanager
def store_transaction(name: str) -> Iterator[None]:
    """Hold one store's lock across a whole read-modify-write the accessors
    cannot see.

    ``guarded`` closes the file-level race inside an accessor. This closes the
    one the accessors are BOTH inside: a phase that calls ``load_x()``, computes
    a new record from what it read, and calls ``upsert_x()``/``save_x()`` has a
    window between the two acquisitions in which another lane reads the same
    state. Whatever the first lane then wrote is overwritten — a trajectory that
    was never recorded, a mastery credit that never happened, no error anywhere.
    Wrapping the sequence makes the transaction the unit of atomicity instead of
    the statement.

    Re-entrant (the underlying lock is an RLock), so a wrapped region may call
    a ``guarded`` accessor freely — which is the whole point, since the region
    is defined by those calls.

    ONE store per region. See the module docstring: two store locks held at once
    would be an undeclared lock order.
    """
    with store_lock(name):
        yield


_F = TypeVar("_F", bound=Callable)


def guarded(name: str) -> Callable[[_F], _F]:
    """Run this store accessor under its store's lock.

    A decorator rather than a ``with`` block inside each function so the
    guard is visible on the signature line — the seven stores are read by
    people auditing what is and is not synchronized, and an indented
    ``with`` five lines down inside a docstring-heavy loader is exactly the
    kind of detail an audit misses. It also keeps the accessors' bodies
    identical to their pre-cut-13 selves, so ``git blame`` still points at
    the reasoning that shaped them.
    """
    lock = store_lock(name)

    def decorate(fn: _F) -> _F:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with lock:
                return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate

"""Physical pipeline seams and one ordered canonical commit lock."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator, List

from core.pipelines import PIPELINE_IDS, validate_pipeline_id

PIPELINE_KIND = "pipeline"
COMMIT_KIND = "commit"


class LockOrderViolation(RuntimeError):
    """A lock acquisition that would break the ordering law.

    Fail-hard by design: the alternative is a deadlock, which produces no
    traceback, no crash, and no signal the pressure loop's fatal relay can
    act on — a brain that is simply never heard from again.
    """


# ── thread-local record of what this thread holds ───────────────────────────

_held = threading.local()


def _emit_lock_event(event: str, lock: "_OrderedLock", *, waited_ms=None) -> None:
    """Say that a pipeline is BLOCKED, on what, and behind whom. Never raises.

    A lock wait is a stop like any other, and until now it was the only stop on
    the whole path that made no sound. Both halves matter: the wait tells you a
    pipeline is stuck rather than idle, and the paired acquire tells you it got
    through — a wait with no acquire is a deadlock, in one line, instead of an
    afternoon of socket forensics.
    """
    try:
        from guardian.terminal_feedback import emit_data

        holder = str(getattr(lock, "owner", "") or "?")
        waiter = threading.current_thread().name
        emit_data(
            "cognition",
            event,
            status="degraded" if event != "lock_acquired" else "ok",
            summary=(
                f"{waiter} {'waiting on' if event == 'lock_wait' else 'took'} "
                f"{lock.kind} lock {lock.name!r} held by {holder}"
                + (f" after {waited_ms}ms" if waited_ms is not None else "")
            ),
            lock_name=lock.name,
            lock_kind=lock.kind,
            held_by=holder,
            waiter=waiter,
            waited_ms=waited_ms,
        )
    except Exception:
        pass


def _stack() -> List["_OrderedLock"]:
    stack = getattr(_held, "stack", None)
    if stack is None:
        stack = []
        _held.stack = stack
    return stack


def _in_barrier() -> bool:
    return int(getattr(_held, "barrier", 0) or 0) > 0


class _OrderedLock:
    """An RLock that refuses acquisitions which would break the ordering law.

    Wraps rather than subclasses ``threading.RLock`` (which is a factory, not a
    class) and keeps the RLock's own surface — ``acquire``/``release``/context
    manager — because ``cognition_chain`` holds the ignition seam with an
    explicit acquire/release pair and the terminus with another, and that block
    structure (and its audit history) predates this cut.
    """

    __slots__ = ("_lock", "name", "kind", "owner")

    def __init__(self, name: str, kind: str) -> None:
        self._lock = threading.RLock()
        self.name = str(name)
        self.kind = str(kind)
        # Who holds it right now, for a WAITER to name in its telemetry. Only
        # ever read while blocked, which is exactly when it is accurate.
        self.owner = ""

    # -- ordering ----------------------------------------------------------

    def _check_order(self) -> None:
        stack = _stack()
        if any(held is self for held in stack):
            return  # re-entrant: this thread already owns it, no new edge
        if self.kind == COMMIT_KIND:
            # The commit lock is the LAST rung. Reaching it from a pipeline lock is
            # the sanctioned pair; reaching it from nothing is a bare commit
            # (a barrier, a maintenance sweep) and equally legal.
            return
        for held in stack:
            if held.kind == COMMIT_KIND:
                raise LockOrderViolation(
                    f"lock order: commit lock held, cannot take pipeline lock {self.name!r} "
                    "(law: own pipeline lock → commit lock ONLY)"
                )
            if held.kind == PIPELINE_KIND:
                if not _in_barrier():
                    raise LockOrderViolation(
                        f"lock order: pipeline lock {held.name!r} held, cannot take pipeline "
                        f"lock {self.name!r} (law: never pipeline → pipeline outside a barrier)"
                    )
                if held.name >= self.name:
                    raise LockOrderViolation(
                        f"barrier lock order: {held.name!r} held before {self.name!r} — "
                        "a global barrier takes pipeline locks in NAME order"
                    )

    # -- RLock surface -----------------------------------------------------

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        self._check_order()
        # Fast path first: an uncontended lock emits NOTHING and costs one extra
        # non-blocking try. Only a pipeline that actually has to WAIT is worth a
        # line in the journal.
        acquired = self._lock.acquire(False)
        if not acquired and blocking:
            # THE stop that was invisible (operator 2026-08-15: "we put emit
            # feedback hooks at every stop of every pipeline"). A pipeline blocked on a
            # lock made no sound at all — it looked identical to a pipeline doing
            # nothing, which is why the agent hang at 12:12 could only be
            # diagnosed by reading sockets and finding zero of them.
            _emit_lock_event("lock_wait", self, waited_ms=None)
            t0 = time.perf_counter()
            acquired = self._lock.acquire(True, timeout)
            _emit_lock_event(
                "lock_acquired" if acquired else "lock_wait_timeout",
                self,
                waited_ms=int((time.perf_counter() - t0) * 1000),
            )
        elif not acquired:
            acquired = self._lock.acquire(blocking, timeout)
        if acquired:
            _stack().append(self)
            try:
                self.owner = threading.current_thread().name
            except Exception:  # noqa: BLE001
                pass
        return acquired

    def release(self) -> None:
        stack = _stack()
        for index in range(len(stack) - 1, -1, -1):
            if stack[index] is self:
                del stack[index]
                break
        self._lock.release()

    def __enter__(self) -> "_OrderedLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False

    def _is_owned(self) -> bool:  # parity with threading.RLock, used by tests
        return self._lock._is_owned()  # noqa: SLF001

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<_OrderedLock {self.kind}:{self.name}>"


_PIPELINE_LOCKS = {pid: _OrderedLock(str(pid), PIPELINE_KIND) for pid in PIPELINE_IDS}
_COMMIT_LOCK = _OrderedLock("commit", COMMIT_KIND)


def pipeline_cognition_lock(pipeline_id: int):
    return _PIPELINE_LOCKS[validate_pipeline_id(pipeline_id)]


def commit_lock():
    return _COMMIT_LOCK


@contextmanager
def global_memory_barrier() -> Iterator[None]:
    """Acquire every physical seam in ID order, then the commit lock."""
    held = []
    _held.barrier = int(getattr(_held, "barrier", 0) or 0) + 1
    try:
        for pid in PIPELINE_IDS:
            lock = pipeline_cognition_lock(pid)
            lock.acquire()
            held.append(lock)
        commit = commit_lock()
        commit.acquire()
        held.append(commit)
        yield
    finally:
        for lock in reversed(held):
            lock.release()
        _held.barrier -= 1


def held_lock_names() -> List[str]:
    """What this thread holds, outermost first — diagnostics and tests."""
    return [f"{lock.kind}:{lock.name}" for lock in _stack()]


def release_held_locks_for_tests() -> None:
    """Hand back every substrate lock this thread still holds.

    ``_ignite_episode_cascade`` exits with its pipeline lock HELD on any exception,
    deliberately: the brain is about to die and a briefly-held lock never
    outlives the process it condemns. In-process that reasoning does not hold —
    the next test waiting on that pipeline blocks forever — so a test exercising
    the fail-hard path gives the locks back with this.
    """
    for lock in list(reversed(_stack())):
        try:
            lock.release()
        except RuntimeError:  # pragma: no cover - not owned by this thread
            pass
    _held.stack = []


def reset_for_tests() -> None:
    """Replace lock objects after test workers have shut down.

    A fail-hard cascade deliberately leaves its substrate lock held on the worker
    thread that raised. The test thread cannot release another thread's RLock,
    and that worker is gone after the dispatcher shuts down. Replacing the lock
    registry gives the next test fresh locks without weakening production's
    process-fatal failure contract.
    """
    global _PIPELINE_LOCKS, _COMMIT_LOCK
    release_held_locks_for_tests()
    _PIPELINE_LOCKS = {pid: _OrderedLock(str(pid), PIPELINE_KIND) for pid in PIPELINE_IDS}
    _COMMIT_LOCK = _OrderedLock("commit", COMMIT_KIND)
    _held.stack = []
    _held.barrier = 0

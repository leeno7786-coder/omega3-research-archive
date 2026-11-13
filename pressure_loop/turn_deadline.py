"""ONE absolute deadline per operator turn, measured on the monotonic clock.

``runtime/brain_cloud.py`` and ``runtime/brain_local.py`` cap a single READ at 600 s, and
they are right to: a frontier seat doing a long fold is still working, and cutting its read
off records a lie (a model that was thinking becomes a model that refused). But a turn is not
one read. Global A may escalate, AUTO answers, AUTO's tool loop may take several rounds, Omega
folds — and nothing bounded the SUM. A blackholed provider therefore bought up to ten minutes
of silence *per stage*, repeatedly, and the operator's bubble had no terminal state at all.

The fix is not a smaller read cap. It is a budget that belongs to the TURN:

    accepted at the gate  ──►  episode/cycle adopts it  ──►  every model call gets what is LEFT

Three properties this exists for:

* **Absolute, not per call.** ``remaining()`` is ``budget - (now - started_at)``. Asking twice
  never buys more time. This is the entire defect the module closes.
* **Starts at ACCEPTANCE, not at ignition.** The one gate (AGENTS.md §4.0) stamps it, so the
  time a turn spends in line is time the operator spent in silence and counts against it.
  Anything else would let a busy queue hide an unbounded wait.
* **"Ungoverned" and "out of time" are different values** (§2.1). A turn nobody stamped
  resolves to ``None`` and every call runs exactly as before; a turn that HAS a budget and has
  none of it left raises ``TurnDeadlineExpired`` carrying the stage and the overrun. A zero
  standing in for "no budget" would silently refuse every autonomous inference in the system.

What it is NOT: a scheduler, a retry policy, or durable state. The budget is process-local by
construction — a monotonic clock does not survive a restart, and a turn whose process died is
not a turn still waiting on a model. It is the same posture as the queue's lease: the LINE is
durable, the claim on it is not.

The deadline is kept for every accepted turn until the terminus releases it. A fixed-size
registry would be unsafe: accepted turns may still be waiting in the durable queue, and
evicting one would silently make it ungoverned.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

#: The whole turn's ceiling. Deliberately the SAME number the backend already allows one read
#: (``runtime.brain_cloud._DEFAULT_TIMEOUT_S``): this change does not shorten any stage that
#: works today, it stops a turn from spending that ceiling more than once. Operators who want a
#: tighter conversational bound set the env override below.
DEFAULT_TURN_BUDGET_S = 600.0

#: Read live per call rather than cached, exactly like the backend read cap it mirrors, so a
#: deployment edit takes effect on restart without a code change.
BUDGET_ENV = "OMEGA_TURN_BUDGET_S"

def _now() -> float:
    """The monotonic clock. Indirected so tests can drive it exactly (no sleeps)."""
    return time.monotonic()


class TurnDeadlineExpired(RuntimeError):
    """The turn's whole budget is gone. Never raised for a turn that has no budget.

    Carries the cause so whoever handles it can say what ran out, where, and by how much —
    an expiry that reached the operator as a bare timeout would be the same silence in a
    different shape (§2.1: the cause must survive).
    """

    def __init__(self, *, key: str, stage: str, budget_s: float, elapsed_s: float) -> None:
        self.key = str(key or "")
        self.stage = str(stage or "")
        self.budget_s = float(budget_s)
        self.elapsed_s = float(elapsed_s)
        self.overrun_s = max(0.0, self.elapsed_s - self.budget_s)
        super().__init__(
            f"turn {self.key!r} exceeded its {self.budget_s:.1f}s whole-turn budget at stage "
            f"{self.stage!r} ({self.elapsed_s:.1f}s elapsed, {self.overrun_s:.1f}s over)"
        )


@dataclass(frozen=True)
class TurnDeadline:
    """An absolute instant on the monotonic clock, plus what it was born from."""

    key: str
    budget_s: float
    started_at: float

    @property
    def expires_at(self) -> float:
        return self.started_at + self.budget_s

    def elapsed(self, now: Optional[float] = None) -> float:
        return (_now() if now is None else float(now)) - self.started_at

    def remaining(self, now: Optional[float] = None) -> float:
        """Seconds left. May be zero or negative — the caller decides what that means."""
        return self.expires_at - (_now() if now is None else float(now))

    def expired(self, now: Optional[float] = None) -> bool:
        return self.remaining(now) <= 0.0

    def check(self, stage: str, now: Optional[float] = None) -> float:
        """The remaining budget, or ``TurnDeadlineExpired`` when there is none.

        Called BEFORE a stage commits to work, so a call that cannot finish inside the turn is
        never started — and the refusal names the stage that hit the wall.
        """
        at = _now() if now is None else float(now)
        left = self.remaining(at)
        if left <= 0.0:
            raise TurnDeadlineExpired(
                key=self.key, stage=stage, budget_s=self.budget_s, elapsed_s=self.elapsed(at)
            )
        return left


def default_budget_s() -> float:
    raw = (os.environ.get(BUDGET_ENV) or "").strip()
    if raw:
        try:
            parsed = float(raw)
        except ValueError:
            parsed = 0.0
        if parsed > 0:
            return parsed
    return DEFAULT_TURN_BUDGET_S


_LOCK = threading.RLock()
_DEADLINES: dict[str, TurnDeadline] = {}
_BOUND = threading.local()


def _clean(key: object) -> str:
    return str(key or "").strip()


def start(key: str, *, budget_s: Optional[float] = None, now: Optional[float] = None) -> Optional[TurnDeadline]:
    """Begin a turn's budget. The FIRST start wins.

    A retried admission is the same turn arriving again, not a fresh ten minutes — restarting
    here would make the budget extendable by anything that can re-enqueue.
    """
    identity = _clean(key)
    if not identity:
        return None
    budget = float(budget_s) if budget_s else default_budget_s()
    if budget <= 0:
        budget = default_budget_s()
    with _LOCK:
        existing = _DEADLINES.get(identity)
        if existing is not None:
            return existing
        deadline = TurnDeadline(
            key=identity, budget_s=budget, started_at=_now() if now is None else float(now)
        )
        # Every registered deadline may still belong to an accepted item waiting in the
        # durable cognition queue. Capacity-based eviction here turns that live turn into an
        # ungoverned one, silently removing its wall clock. ``finish`` releases every alias
        # at the terminus; a process restart clears the process-local registry.
        _DEADLINES[identity] = deadline
        return deadline


def adopt(alias: str, key: str) -> Optional[TurnDeadline]:
    """Let ``alias`` name the SAME budget as ``key`` — the episode joining its cycle.

    The episode does not get its own clock: the turn started when the operator's words were
    accepted, and the queue wait belongs to the operator's silence.
    """
    name = _clean(alias)
    with _LOCK:
        deadline = _DEADLINES.get(_clean(key))
        if deadline is None or not name:
            return deadline
        _DEADLINES[name] = deadline
        return deadline


def get(key: str) -> Optional[TurnDeadline]:
    with _LOCK:
        return _DEADLINES.get(_clean(key))


def current() -> Optional[TurnDeadline]:
    """The turn bound to THIS thread, if the cascade bound one."""
    return getattr(_BOUND, "deadline", None)


@contextmanager
def bind(deadline: Optional[TurnDeadline]) -> Iterator[Optional[TurnDeadline]]:
    """Make ``deadline`` the ambient turn for this thread.

    Keys carry the budget wherever a call site knows the cycle or episode. Several stages
    deliberately ask under a DERIVED id (``f"{episode_id}-omega-ep"``,
    ``f"{ep_id}-fetched-knowledge"``) which no registry can predict, so the cascade also binds
    the turn to the thread running it. Restored on exit, including on a raise, so a lane thread
    never inherits the previous turn's budget.
    """
    previous = getattr(_BOUND, "deadline", None)
    _BOUND.deadline = deadline
    try:
        yield deadline
    finally:
        _BOUND.deadline = previous


def resolve(*keys: str) -> Optional[TurnDeadline]:
    """The budget governing this call: the first key that names one, else the bound turn."""
    for key in keys:
        deadline = get(key)
        if deadline is not None:
            return deadline
    return current()


def remaining_for(*keys: str, now: Optional[float] = None) -> Optional[float]:
    """Seconds left for this turn, or ``None`` when no budget governs it.

    ``None`` is load-bearing: autonomous work has no operator waiting on a bubble and keeps
    exactly the timeouts it has today.
    """
    deadline = resolve(*keys)
    if deadline is None:
        return None
    return deadline.remaining(now)


def check(*keys: str, stage: str, now: Optional[float] = None) -> Optional[float]:
    """Remaining budget for a stage about to run; ``None`` when ungoverned."""
    deadline = resolve(*keys)
    if deadline is None:
        return None
    return deadline.check(stage, now)


def clamp_timeout(timeout_s: float, *keys: str, stage: str) -> float:
    """Limit a stage's requested timeout to the operator turn's remaining time."""
    timeout = float(timeout_s)
    remaining = check(*keys, stage=stage)
    return timeout if remaining is None else min(timeout, remaining)


def finish(*keys: str) -> Optional[TurnDeadline]:
    """Release the turn at its terminus, with every alias that named it.

    A turn that has been answered is not still spending time; leaving it tracked would let a
    later reuse of the same cycle id inherit an exhausted budget.
    """
    with _LOCK:
        released = None
        for key in keys:
            found = _DEADLINES.get(_clean(key))
            if found is not None:
                released = found
                break
        if released is None:
            return None
        for name in [name for name, value in _DEADLINES.items() if value is released]:
            _DEADLINES.pop(name, None)
        return released


def tracked_keys() -> tuple[str, ...]:
    with _LOCK:
        return tuple(_DEADLINES)


def reset_for_tests() -> None:
    with _LOCK:
        _DEADLINES.clear()
    _BOUND.deadline = None


__all__ = [
    "BUDGET_ENV",
    "DEFAULT_TURN_BUDGET_S",
    "TurnDeadline",
    "TurnDeadlineExpired",
    "adopt",
    "bind",
    "clamp_timeout",
    "check",
    "current",
    "default_budget_s",
    "finish",
    "get",
    "remaining_for",
    "reset_for_tests",
    "resolve",
    "start",
    "tracked_keys",
]

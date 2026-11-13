"""How long an operator turn took, measured rather than believed.

The demo bar names a number — median time-to-first-audio under 2 s on a conversational turn while
two lanes are leased — and until now nothing in this system measured it. The live four-pipeline
acceptance run *contained* those numbers inside its occupancy trace and nobody wrote them down, and
no test asserted a latency budget anywhere.

Four marks per turn, each recorded once, on the paths that already exist:

    arrival      the queue stamped its arrival_seq (the one gate, before any pipeline)
    admitted     an episode opened for it
    first_token  the first partial of the answer reached the waiting bubble
    reply        the durable answer was recorded

**This is an instrument, not a mechanism.** It is a bounded in-memory ring: nothing persists, no
canonical memory is touched, no episode state lives here, and it can never gate, delay or fail a
turn — a broken clock loses the measurement, not the answer. It exists so that a latency claim can
be checked instead of asserted.

Two rules it will not bend:

* **A missing mark is unknown, never zero.** A latency of 0 ms is the most flattering lie the system
  could tell about itself, and "could not measure" and "was instant" must never share a value
  (AGENTS.md section 2.1).
* **A negative interval is not a measurement.** Marks can arrive out of order under concurrency; an
  out-of-order phase is reported as unknown and named, not clamped to zero.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import OrderedDict
from typing import Any, Mapping, Optional

#: The order a turn passes through. Declared, so a typo is a refusal rather than a phantom phase.
#:
#: ``released`` is the capacity question rather than the answer question: the reply is published
#: mid-cascade, but the PIPELINE stays occupied through the terminus, and the live four-pipeline
#: trace showed three trivial chat termini queueing 6 s / 11.4 s / 18.2 s on the one commit lock —
#: holding lanes long after their answers had landed. A lane held is a lane the operator's next
#: sentence cannot have.
TURN_PHASES: tuple[str, ...] = ("arrival", "admitted", "first_token", "reply", "released")

#: How many recent turns are kept. Bounded on purpose: an unbounded diagnostic is a leak.
RING_SIZE = 256

_LOCK = threading.RLock()
_TURNS: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

_CONTEXT_FIELDS = ("pipeline_id", "lane", "work_kind", "episode_id", "lanes_busy_at_admission")


def _blank(cycle_id: str) -> dict[str, Any]:
    return {
        "cycle_id": cycle_id,
        "at": {},
        "out_of_order": [],
        **{field: None for field in _CONTEXT_FIELDS},
    }


def mark(cycle_id: str, phase: str, *, now: float | None = None, **context: Any) -> bool:
    """Record that *cycle_id* reached *phase*. Returns whether it was recorded.

    The first observation of a phase wins: a retried generation restarts the token stream, and the
    turn did not become faster because it had to try twice.

    Never raises for anything an operator can cause — a missing cycle id, a dead clock, a full ring
    are all dropped measurements. An unknown *phase* name IS refused, because every call site passes
    a constant and a silent typo would make this instrument lie.
    """
    if phase not in TURN_PHASES:
        raise ValueError(
            f"unknown turn phase {phase!r}; the measured phases are {TURN_PHASES}"
        )
    cid = str(cycle_id or "").strip()
    if not cid:
        return False
    try:
        stamp = time.monotonic() if now is None else float(now)
    except Exception:
        return False
    try:
        with _LOCK:
            row = _TURNS.get(cid)
            if row is None:
                row = _blank(cid)
                _TURNS[cid] = row
                while len(_TURNS) > RING_SIZE:
                    _TURNS.popitem(last=False)
            _TURNS.move_to_end(cid)
            row["at"].setdefault(phase, stamp)
            for field in _CONTEXT_FIELDS:
                if field in context and context[field] is not None and row.get(field) is None:
                    row[field] = context[field]
        return True
    except Exception:
        # An instrument that can fail a turn is worse than no instrument.
        return False


def _interval_ms(row: Mapping[str, Any], phase: str) -> Optional[int]:
    at = row["at"]
    arrival = at.get("arrival")
    stamp = at.get(phase)
    if arrival is None or stamp is None:
        return None
    delta = stamp - arrival
    if delta < 0:
        return None
    return int(round(delta * 1000))


def _between_ms(row: Mapping[str, Any], start: str, end: str) -> Optional[int]:
    """The interval between two marks, or ``None`` when either is missing or the order is
    impossible. Never a zero standing in for "not measured"."""
    at = row["at"]
    first, last = at.get(start), at.get(end)
    if first is None or last is None:
        return None
    delta = last - first
    if delta < 0:
        return None
    return int(round(delta * 1000))


def _render(row: Mapping[str, Any]) -> dict[str, Any]:
    since = {phase: _interval_ms(row, phase) for phase in TURN_PHASES}
    out_of_order = sorted(
        phase for phase in TURN_PHASES
        if row["at"].get(phase) is not None and since[phase] is None and phase != "arrival"
        and row["at"].get("arrival") is not None
    )
    missing = [phase for phase in TURN_PHASES if row["at"].get(phase) is None]
    return {
        "cycle_id": row["cycle_id"],
        "since_arrival_ms": since,
        "queue_wait_ms": since["admitted"],
        "time_to_first_token_ms": since["first_token"],
        "time_to_reply_ms": since["reply"],
        # How long this turn OWNED a lane, and how much of that it kept after the operator
        # already had their answer. The second number is the terminus tail that steals the
        # pipeline the next sentence needs.
        "capacity_held_ms": _between_ms(row, "admitted", "released"),
        "after_reply_ms": _between_ms(row, "reply", "released"),
        "missing": missing,
        "out_of_order": out_of_order,
        "complete": not missing and not out_of_order,
        **{field: row.get(field) for field in _CONTEXT_FIELDS},
    }


def snapshot(cycle_id: str) -> Optional[dict[str, Any]]:
    """What is known about one turn, or ``None`` for a turn nobody marked.

    ``None`` rather than an empty row: a turn that was never measured must not be presentable as a
    turn that took no time.
    """
    cid = str(cycle_id or "").strip()
    if not cid:
        return None
    with _LOCK:
        row = _TURNS.get(cid)
        if row is None:
            return None
        return _render(row)


def recent(limit: int = RING_SIZE) -> list[dict[str, Any]]:
    """The most recently marked turns, newest first."""
    with _LOCK:
        rows = [_render(row) for row in reversed(list(_TURNS.values()))]
    bound = max(0, int(limit))
    return rows[:bound]


def _distribution(values: list[int], unmeasured: int) -> dict[str, Any]:
    if not values:
        return {"median": None, "p95": None, "max": None, "observations": 0,
                "unmeasured": unmeasured}
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return {
        "median": int(round(statistics.median(ordered))),
        "p95": ordered[index],
        "max": ordered[-1],
        "observations": len(ordered),
        "unmeasured": unmeasured,
    }


def summary() -> dict[str, Any]:
    """Distributions over the turns in the ring, each with the population it came from.

    A median whose population is unstated is exactly the defect WO-11 exists to prevent, so every
    figure carries its observation count and how many turns could not be measured.
    """
    with _LOCK:
        rows = [_render(row) for row in _TURNS.values()]
    out: dict[str, Any] = {"turns": len(rows), "ring_size": RING_SIZE}
    for key in ("queue_wait_ms", "time_to_first_token_ms", "time_to_reply_ms",
                "capacity_held_ms", "after_reply_ms"):
        observed = [row[key] for row in rows if row[key] is not None]
        out[key] = _distribution(observed, len(rows) - len(observed))
    return out


def reset_for_tests() -> None:
    with _LOCK:
        _TURNS.clear()


__all__ = [
    "RING_SIZE",
    "TURN_PHASES",
    "mark",
    "recent",
    "reset_for_tests",
    "snapshot",
    "summary",
]

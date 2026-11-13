"""Cadence units for the event-driven pressure loop.

The pressure loop is EVENT-DRIVEN (mind-driven chaining + idle-wake timer,
2026-07-21) — it does not wake on a fixed clock to re-poll.
``tick_interval_seconds`` survives as a cadence unit: the pause-state
re-check while the loop is held by a dev/CLI agent (pressure_loop.loop) and
the memory-decay "cycle" unit (memory.confidence_decay / memory.demotion).

Phase 3 (concurrent-cognition spec 2026-08-08 §6, landed 2026-08-09): the
six 5s ``pause_after_cascade_phase`` sleeps between executive handoffs are
DELETED — they padded every episode by ~30s of pure wall-clock. The cascade
hands off at model speed; pacing between phases is not a safety mechanism
and never ordered any write.
"""

from __future__ import annotations

import logging

log = logging.getLogger("omega.cognition_pacing")


def pressure_tick_interval_seconds() -> float:
    from pressure_loop.policy import pressure_tick_interval_seconds as _policy_tick

    return _policy_tick()


def between_tick_seconds() -> float:
    """Cadence unit (seconds). Used for the pressure loop's pause-state re-check
    while a dev/CLI agent holds the runtime — NOT an autonomy tick (the loop is
    event-driven when active)."""
    return pressure_tick_interval_seconds()


def reset_cognition_pacing_for_tests() -> None:
    """Test helper — reserved for future pacing state."""

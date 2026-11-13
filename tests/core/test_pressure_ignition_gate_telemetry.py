"""evaluate() must emit a per-tick ignition_gate line carrying the aggregate
scores + the open/suppress decision — on NO-IGNITION ticks, not only on the
ticks that ignite. This is what lets the terminal show what every pressure
tick evaluated (top weight vs ceiling/margin, neuromod, why it did/didn't open).
"""

from __future__ import annotations

from unittest.mock import patch

from pressure_loop.contributors import make_contributor
from pressure_loop.pressure import clear_pending_contributors, evaluate


def _gate_events(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("kind") == "data" and e.get("phase") == "ignition_gate"]


def test_no_ignition_tick_emits_gate_scores():
    """A below-ceiling tick returns None but STILL emits the gate scores."""
    clear_pending_contributors()
    events: list[dict] = []
    low = make_contributor("autonomous", "curiosity_gap", 0.05, {})
    with patch("guardian.terminal_feedback._publish_event", side_effect=events.append):
        ign = evaluate(
            [low],
            active_episode=False,
            open_threshold=1.0,
            neuromod={"dopamine": -0.4, "serotonin": 0.82, "spike": 0.0},
        )
    assert ign is None  # no ignition this tick
    gates = _gate_events(events)
    assert len(gates) == 1, f"expected exactly 1 ignition_gate emit, got {len(gates)}"
    summary = gates[0].get("summary", "")
    # FAIL-MODE: the gate line must carry the real scores + the suppress reason,
    # never fire blank. (A blank/presence-only emit would pass a weaker test.)
    assert "below_ceiling" in summary, summary
    assert "top=" in summary and "ceil=" in summary, summary
    assert "dop=-0.40" in summary and "ser=0.82" in summary, summary


def test_ignition_tick_emits_gate_decision():
    """On ignition the gate line shows the IGNITE decision (alongside scores)."""
    clear_pending_contributors()
    events: list[dict] = []
    drive = make_contributor("autonomous", "curiosity_gap", 1.0, {})
    with patch("guardian.terminal_feedback._publish_event", side_effect=events.append):
        ign = evaluate([drive], active_episode=False, open_threshold=0.1, neuromod={})
    assert ign is not None
    gates = _gate_events(events)
    assert len(gates) == 1, f"expected exactly 1 ignition_gate emit, got {len(gates)}"
    assert "IGNITE" in gates[0].get("summary", ""), gates[0].get("summary")

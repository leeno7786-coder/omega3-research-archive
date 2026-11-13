"""Operator-directive seed hardening — timestamp + redirect false-positive guards.

Folded-in fixes from the operator fast-reply plan (Task 7):
  - seed_trajectory_from_operator_directive carries a timestamp.
  - detect_redirect_directive rejects interrogative / knowledge / self-referential
    false positives, and the "new priority" pattern requires a directive shape.

NOTE: the Task-6 resolver change was reverted after code review — _resolve_trajectory
reads the lobe-9 trajectory (pressure_loop.temporal_store.read_trajectory), whereas the
operator seed writes the LAST_OMEGA_TRAJECTORY SharedState slot (consumed by META_A), so
the resolver was the wrong target AND the fallback mis-steered normal Omega trajectories
(influencer_text is HUD-descriptive, not a steering field). See the review notes.
"""

from __future__ import annotations


# ── Seed timestamp ────────────────────────────────────────────────────────────


def test_seed_writes_timestamp():
    from core.shared_state import SharedState
    from core.shared_state_keys import LAST_OMEGA_TRAJECTORY
    from pressure_loop import operator_pressure as OP

    ss = SharedState()
    OP.seed_trajectory_from_operator_directive(ss, {"text": "focus on WCAG", "domain": ""})
    traj = ss.read(LAST_OMEGA_TRAJECTORY)
    assert isinstance(traj, dict)
    assert traj.get("timestamp")


# ── Redirect false-positive / true-positive guards ────────────────────────────


def test_redirect_false_positives_are_rejected():
    from pressure_loop import operator_pressure as OP

    for t in (
        "what is a 301 redirect",
        "how do I redirect stdout?",
        "should we focus on X?",
        "explain an HTTP redirect",
        "I want to focus on my own work",
        "the new priority queue is faster",
    ):
        assert OP.detect_redirect_directive(t) is False, t


def test_redirect_true_positives_still_detected():
    from pressure_loop import operator_pressure as OP

    for t in (
        "Change of plan - focus on WCAG accessibility compliance.",
        "switch to time-series forecasting",
        "pivot to the data pipeline now",
        "new priority: get the deploy pipeline green",
        # narrowed self-ref guard no longer over-suppresses a real directive
        "focus on my deploy pipeline now",
    ):
        assert OP.detect_redirect_directive(t) is True, t

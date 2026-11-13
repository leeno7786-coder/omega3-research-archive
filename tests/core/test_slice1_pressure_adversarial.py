"""Slice 1 — ADVERSARIAL coverage for pressure evaluation -> EpisodeIgnition.

Failure + gap testing: aggregate/dominant edges, threshold boundary, neuromod
extremes, the single-active-episode queue (incl. unbounded-growth hunt), the
hard guarantee that guardian/tool_creation_need can NEVER surface, and
FAIL-HARD cognition behavior (a swallowed compute failure is a mind running
on poison). Independent of the implementer's B2.
"""

from __future__ import annotations

import sys
import types

import pytest

from pressure_loop.contributors import make_contributor
from pressure_loop.pressure import (
    _neuromod_from_last_body,
    clear_pending_contributors,
    evaluate,
    gather_contributors,
    modulate_contributor_weight_map,
    pending_contributors,
    stage_reseed,
)
from core.shared_state import SharedState
from pressure_loop import cognition_queue as cq
from core.shared_state_keys import LAST_OMEGA_TRAJECTORY


@pytest.fixture(autouse=True)
def _clean_queue(tmp_path, monkeypatch):
    # Point the durable operator box at a per-test file. Without this the suite
    # reads and writes the REAL guardian/state/operator_input_queue.json, so a
    # turn enqueued by any earlier test leaks in here as a leased head and
    # silently ignites threshold tests that passed no operator input at all.
    import pressure_loop.cognition_queue as queue_mod

    monkeypatch.setattr(
        queue_mod, "_queue_path", lambda: tmp_path / "operator_input_queue.json"
    )
    queue_mod._reset_cache_for_tests()
    clear_pending_contributors()
    yield
    clear_pending_contributors()
    queue_mod._reset_cache_for_tests()


def _c(ctype, weight, msg=None, source="x"):
    # Operator-input tests must pass source="operator": the queue admits only
    # operator-AUTHORED turns (AGENTS.md §4.0), because `type` alone is not
    # identity — an Omega- or model-authored contributor typed operator_command
    # would otherwise join the operator's line and be persisted as something
    # the operator said. Aggregate/threshold tests keep the neutral "x".
    meta = {"message": msg} if msg else {}
    if source == "operator":
        meta["cycle_id"] = msg or ctype
    return make_contributor(source, ctype, weight, meta)


# ---- aggregate + dominant ------------------------------------------------


def test_empty_contributors_no_ignition():
    assert evaluate([], active_episode=False, neuromod={}) is None


def test_all_zero_weight_no_ignition():
    ign = evaluate([_c("a", 0.0)], active_episode=False, open_threshold=0.1, neuromod={})
    assert ign is None


def test_partial_sum_does_not_open_without_dominant_at_ceiling():
    """Hybrid: 0.6+0.7 would sum past 0.55 but neither reaches ceiling."""
    ign = evaluate(
        [_c("a", 0.6), _c("b", 0.7)],
        active_episode=False,
        open_threshold=1.0,
        neuromod={},
    )
    assert ign is None


def test_single_dominant_at_ceiling_opens():
    ign = evaluate(
        [_c("a", 1.0)],
        active_episode=False,
        open_threshold=1.0,
        neuromod={},
    )
    assert ign is not None
    assert ign.aggregate_pressure == 1.0


def test_dominant_is_highest_weight_at_ceiling():
    ign = evaluate(
        [_c("low", 0.2), _c("high", 1.0)],
        active_episode=False,
        open_threshold=1.0,
        neuromod={},
    )
    assert ign is not None
    assert ign.opened_by == "high"


def test_near_tie_at_ceiling_ignites_the_first_signal_in():
    """A near-tie is a QUEUE, not an ambiguity (operator 2026-08-15).

    "it is structurally impossible for a lane to have a tie because each lane is
    run from a single ignited episode from a single pressure input ... when the
    same loop sends signals back to back, its first in and the loser queues
    until next pressure ignition."

    Supersedes suppress-on-near-tie, which did not defer the choice but dropped
    BOTH signals: two of Omega's own drives at 0.850 suppressed each other and
    parked autonomous cognition for 7m23s.
    """
    ign = evaluate(
        [_c("bravo", 1.0), _c("alpha", 0.96)],
        active_episode=False,
        open_threshold=1.0,
        neuromod={},
    )
    assert ign is not None
    assert ign.opened_by == "bravo"  # first in, and the stronger of the two

    # Dead even: arrival order alone decides, and alpha's turn comes next.
    even = evaluate(
        [_c("alpha", 1.0), _c("bravo", 1.0)],
        active_episode=False,
        open_threshold=1.0,
        neuromod={},
    )
    assert even is not None and even.opened_by == "alpha"


# ---- threshold boundary --------------------------------------------------


def test_below_ceiling_no_ignition():
    ign = evaluate([_c("a", 0.9)], active_episode=False, open_threshold=1.0, neuromod={})
    assert ign is None


def test_at_ceiling_opens():
    ign = evaluate([_c("a", 1.0)], active_episode=False, open_threshold=1.0, neuromod={})
    assert ign is not None


# ---- neuromod is decision context, not pressure-gate authority ------------


def test_dopamine_cannot_create_ignition_from_subthreshold_pressure():
    base = evaluate([_c("a", 0.67)], active_episode=False, open_threshold=1.0, neuromod={})
    assert base is None
    boosted = evaluate(
        [_c("a", 0.67)],
        active_episode=False,
        open_threshold=1.0,
        neuromod={"dopamine": 1.0},
    )
    assert boosted is None


def test_legacy_modulation_helper_cannot_change_pressure_weights():
    weights = {"learning_debt": 0.4, "application_gap": 0.7}

    assert (
        modulate_contributor_weight_map(
            weights,
            {"dopamine": 1.0, "serotonin": 1.0, "stress": 1.0},
        )
        == weights
    )


def test_empty_neuromod_requires_ceiling():
    ign = evaluate([_c("a", 1.0)], active_episode=False, open_threshold=1.0, neuromod={})
    assert ign is not None
    assert ign.aggregate_pressure == pytest.approx(1.0)


def test_aversive_neuromod_cannot_veto_eligible_pressure():
    ign = evaluate(
        [_c("a", 1.0)],
        active_episode=False,
        open_threshold=1.0,
        neuromod={"dopamine": -0.8, "serotonin": 0.8},
    )
    assert ign is not None
    assert ign.aggregate_pressure == pytest.approx(1.0)
    assert ign.dominant_contributor.weight == pytest.approx(1.0)
    assert ign.neuromod_state == pytest.approx(
        {"dopamine": -0.8, "serotonin": 0.8, "stress": 0.0, "spike": 0.0}
    )


def test_serotonin_cannot_widen_gate_margin():
    ign = evaluate(
        [_c("bravo", 1.0), _c("alpha", 0.92)],
        active_episode=False,
        open_threshold=1.0,
        neuromod={"serotonin": 1.0},
    )
    assert ign is not None
    assert ign.opened_by == "bravo"


def test_low_serotonin_allows_clear_ceiling_winner():
    ign = evaluate(
        [_c("a", 1.0)],
        active_episode=False,
        open_threshold=1.0,
        neuromod={"serotonin": 0.0, "dopamine": 0.5},
    )
    assert ign is not None


# ---- single active episode rule ------------------------------------------


def test_active_episode_suppresses_ignition_and_queues():
    c = _c("operator_command", 1.0, "Learn Python", source="operator")
    cq.enqueue_input(c, operator_activity=False)
    ign = evaluate([c], active_episode=True, neuromod={})
    assert ign is None
    assert len(pending_contributors()) >= 1


def test_operator_message_while_active_does_not_interrupt():
    c = make_contributor("operator", "operator_command", 1.0, {"message": "urgent"})
    assert evaluate([c], active_episode=True, neuromod={}) is None  # no interrupt


def test_queued_contributor_surfaces_on_next_inactive_evaluate():
    """A stimulus queued while active must not vanish — the queue box applies
    it on the next inactive evaluate (ONE per pressure cycle), where it takes
    the cycle outright (operator directive 2026-06-12: order of input is law,
    every input gets its own episode)."""
    c = _c("operator_command", 1.0, "queued msg", source="operator")
    cq.enqueue_input(c, operator_activity=False)
    evaluate([c], active_episode=True, neuromod={})  # queued in the box
    ignition = evaluate([cq.lease_next_input(0)], active_episode=False, neuromod={})
    assert ignition is not None, "queued contributor was lost"
    assert ignition.dominant_contributor.metadata.get("message") == "queued msg"


def test_pending_queue_does_not_grow_unbounded_across_active_ticks():
    """GAP HUNT: re-reading the same stimulus each active tick must not pile
    up N duplicate copies in the queue (unbounded growth / flood risk)."""
    c = _c("operator_command", 1.0, "same", source="operator")
    for _ in range(5):
        cq.enqueue_input(c, operator_activity=False)
        evaluate([c], active_episode=True, neuromod={})
    copies = [p for p in pending_contributors() if p.metadata.get("message") == "same"]
    assert len(copies) == 1, (
        f"pending queue accumulated {len(copies)} copies of one stimulus "
        f"across 5 active ticks (unbounded-growth gap)"
    )


def test_back_to_back_distinct_operator_messages_both_queued():
    """The queue's REASON FOR EXISTING: a trigger-happy operator firing
    DISTINCT queries back-to-back during the brief active (inference) window
    must lose none. Dedup only collapses an identical double-fire."""
    q1 = make_contributor("operator", "operator_query", 1.0, {"message": "Q1?", "cycle_id": "q1"})
    q2 = make_contributor("operator", "operator_query", 1.0, {"message": "Q2?", "cycle_id": "q2"})
    cq.enqueue_input(q1, operator_activity=False)
    evaluate([q1], active_episode=True, neuromod={})
    cq.enqueue_input(q2, operator_activity=False)
    evaluate([q2], active_episode=True, neuromod={})
    msgs = {c.metadata.get("message") for c in pending_contributors()}
    assert msgs == {"Q1?", "Q2?"}


def test_identical_double_fire_is_deduped_to_one():
    same = make_contributor("operator", "operator_query", 1.0, {"message": "status?", "cycle_id": "status"})
    cq.enqueue_input(same, operator_activity=False)
    evaluate([same], active_episode=True, neuromod={})
    cq.enqueue_input(same, operator_activity=False)
    evaluate([same], active_episode=True, neuromod={})
    copies = [c for c in pending_contributors() if c.metadata.get("message") == "status?"]
    assert len(copies) == 1


def test_autonomous_drives_never_queue_during_active_episode():
    """The pressure loop is HALTED mid-episode, so autonomous drives are never
    read then; even if handed to evaluate while active they must NOT queue.
    Only an async operator input can arrive during the halt."""
    drives = [
        _c("curiosity_gap", 0.9),
        _c("learning_debt", 0.9),
        _c("epistemic_emptiness", 0.9),
        _c("idle_probe_aggregate", 0.9),
    ]
    assert evaluate(drives, active_episode=True, neuromod={}) is None
    assert len(pending_contributors()) == 0


# ---- removed / non-contributor signals can NEVER surface -----------------


def test_removed_and_observation_signals_never_emitted(monkeypatch):
    import pressure_loop.pressure_field as pf

    def _must_not_read(_shared_state):
        raise AssertionError("deterministic pressure reader was invoked")

    def fake_registry():
        return {
            "guardian": _must_not_read,
            "tool_creation_need": _must_not_read,
            "evidence_terminal": _must_not_read,
            "curiosity_gap": _must_not_read,
        }

    monkeypatch.setattr(pf, "build_registry", fake_registry)
    out = gather_contributors(SharedState())
    types = {c.type for c in out}
    assert "guardian" not in types
    assert "tool_creation_need" not in types
    assert "evidence_terminal" not in types
    assert "curiosity_gap" not in types


def test_gather_never_injects_periodic_memory_integrity_pressure(monkeypatch):
    """Memory audits are diagnostics, never an autonomous cognition source."""
    import pressure_loop.pressure_field as pf

    monkeypatch.setattr(pf, "build_registry", lambda: {})
    monkeypatch.setattr("memory.taxonomy.config.taxonomy_pressure_enabled", lambda: False)

    fake_module = types.ModuleType("pressure_loop.integrity_pressure")
    fake_module.integrity_pressure_contributor = lambda: make_contributor(
        "memory_integrity",
        "memory_reconciliation",
        0.55,
        {"issues": ["stale projection"]},
    )
    monkeypatch.setitem(sys.modules, "pressure_loop.integrity_pressure", fake_module)

    contributors = gather_contributors(SharedState())

    assert all(c.source != "memory_integrity" for c in contributors)


def test_gather_admits_only_omega_authored_autonomous_reseeds(monkeypatch):
    """Successor autonomy belongs to Omega, never a deterministic producer."""
    import pressure_loop.pressure_field as pf
    from pressure_loop import pressure as pressure_mod

    monkeypatch.setattr(pf, "build_registry", lambda: {})
    pressure_mod._RESEED_STAGING.clear()
    stage_reseed(
        [
            make_contributor("autonomous", "mastery_gap", 0.80, {"source": "deepsleep_reseed"}),
            make_contributor("omega", "application_gap", 0.70, {"set_by_episode": "ep-1"}),
        ]
    )

    contributors = gather_contributors(SharedState())

    assert any(c.source == "omega" and c.type == "application_gap" for c in contributors)
    assert all(c.source != "autonomous" for c in contributors)


# ---- fail-hard cognition -------------------------------------------------
# An admitted operator-ingress reader, the neuromod parse, the operator-message
# read, and the trajectory read are all cognition COMPUTE that feeds the ignition
# decision. A failure in any of them must HALT — silently dropping the signal
# (weight 0), zeroing neuromod, stubbing the message, or nulling the prior
# would let the mind decide on poison. (The telemetry emit, by contrast, is the
# isolated HUD surface and stays soft.)


def test_raising_reader_halts_gather(monkeypatch):
    import pressure_loop.pressure_field as pf

    def boom(ss):
        raise RuntimeError("reader exploded")

    def fake_registry():
        return {pf.CONTRIBUTOR_OPERATOR_MESSAGE: boom}

    monkeypatch.setattr(pf, "build_registry", fake_registry)
    with pytest.raises(RuntimeError, match="reader exploded"):
        gather_contributors(SharedState())


def test_corrupt_neuromod_in_last_body_never_raises():
    """Body-state modulation is never-raise: garbage neuromod → zeros."""
    from pressure_loop.episode import EpisodeStore

    store = EpisodeStore()
    store._last_closed_body = {"neuromod": {"stress": "not-a-number", "plasticity": 0.5}}
    result = _neuromod_from_last_body(store)
    assert result == {
        "dopamine": 0.0,
        "serotonin": 0.0,
        "stress": 0.0,
        "spike": 0.0,
    }


def test_gather_never_rebuilds_an_operator_turn_from_shared_state(monkeypatch):
    """RETIRED CONCERN, REPLACED BY A STRONGER ONE (AGENTS.md §4.0).

    Gather does not rebuild an operator turn from SharedState. The turn comes
    from the durable queue, leased by evaluate — one gate, one copy.
    """
    import pressure_loop.pressure_field as pf

    monkeypatch.setattr(
        pf, "build_registry", lambda: {pf.CONTRIBUTOR_OPERATOR_MESSAGE: lambda ss: 0.9}
    )
    ss = SharedState()
    assert [c for c in gather_contributors(ss) if c.source == "operator"] == []


class _RaisingTrajectoryState:
    """SharedState stand-in whose trajectory read is corrupt."""

    def read(self, key, default=None):
        if key == LAST_OMEGA_TRAJECTORY:
            raise RuntimeError("trajectory store corrupt")
        return default

    def write(self, *a, **k):
        pass


def test_corrupt_trajectory_read_halts_evaluate():
    # neuromod supplied so evaluate skips the neuromod read and reaches the
    # trajectory read; a corrupt prior must halt, not silently become None.
    with pytest.raises(RuntimeError, match="trajectory store corrupt"):
        evaluate(
            [_c("a", 1.0)],
            active_episode=False,
            open_threshold=1.0,
            neuromod={},
            shared_state=_RaisingTrajectoryState(),
        )


def test_evaluate_with_none_neuromod_reads_shared_state_safely():
    # ABSENCE is not failure: shared_state present but no neuromod/trajectory
    # slot -> read returns the default -> opens normally (no raise).
    ign = evaluate(
        [_c("a", 1.0)],
        active_episode=False,
        open_threshold=1.0,
        shared_state=SharedState(),
    )
    assert ign is not None

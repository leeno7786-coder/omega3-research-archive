"""§7-P1-3(e) — the cognitive_churn VALUE must REACH pressure evaluation.

Live wiring under test (`pressure_loop/pressure_field.py::evaluate_pressure`,
lines ~1871-1912):

    churn = assess_cognitive_churn()                       # the source seam
    if contributors[curiosity_gap] > 0:
        contributors[curiosity_gap] *= churn.curiosity_pressure_multiplier()
        contributor_details[curiosity_gap]["churn_scale"] = ...
    contributor_details["cognitive_churn"] = churn.to_dict()

`assess_cognitive_churn` reads two on-disk surfaces — autonomous outcome
history (`autonomous_outcomes.jsonl`) and the mastery substrate
(`knowledge_mastery.json`). The churn SOURCE SEAM is those files: repeated
satisfied work in a domain WITHOUT mastery growth marks the domain (and its
investigate-* goal) "saturated", which drops the curiosity multiplier from
1.0 to the goal-saturated floor (0.35).

The existing suite only has a churn FAILURE-path test
(`tests/core/test_failure_log_batch3.py::test_churn_fingerprints_failed_is_logged`
— that a churn read error is logged). There is NO test that a real churn
VALUE flows through `evaluate_pressure` and changes the contributor weight.
This file pins exactly that: the injected churn assessment reaches
`evaluate`, scales the curiosity_gap weight by the CONFIGURED floor, and
lands in the decision's snapshot — and the GATE'S OTHER DIRECTION
(un-saturated churn) leaves the weight untouched.

Injection is at the source seam: build the outcomes + mastery files, prime
`_LAST_SNAPSHOT` via `assess_cognitive_churn(<paths>, force_refresh=True)`
(the same builder `evaluate_pressure` calls with no args — which then takes
the cache hit), then call `evaluate_pressure`. No monkeypatch of the
multiplier itself: the value travels the real path from disk → snapshot →
evaluate.

RED doctrine (project): asserts the CONFIGURED VALUE (0.35 floor → 0.28
scaled weight) AND the fail-mode/both-gate-directions (saturated scales,
un-saturated does not).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import autonomic.cognitive_churn as cc
from autonomic.cognitive_churn import (
    CognitiveChurnSnapshot,
    assess_cognitive_churn,
)
from core.shared_state import SharedState
from pressure_loop.pressure_field import (
    CONTRIBUTOR_CURIOSITY_GAP,
    evaluate_pressure,
)

# The goal-saturated curiosity floor is the configured constant under test —
# read it off the class so this pins the LIVE value, not a copy that can drift.
_GOAL_SATURATED_FLOOR = CognitiveChurnSnapshot._CURIOSITY_GOAL_SATURATED_FLOOR  # 0.35
_PINNED_CURIOSITY_GAP = 0.8  # deterministic non-zero curiosity weight we inject


def _write_saturated_seam(tmp_path: Path, *, domain: str = "astronomy") -> tuple[Path, Path]:
    """Source-seam files that produce a SATURATED domain + goal.

    Saturation rule (assess_cognitive_churn):
      attempts >= 3 AND satisfied >= 3 AND satisfied_rate >= 0.55
      AND mastery < 0.30
    Four satisfied attempts tagged with goal `investigate-<domain>` while
    mastery sits at 0.05 → both the domain AND the goal saturate.
    """
    outcomes = tmp_path / "autonomous_outcomes.jsonl"
    mastery = tmp_path / "knowledge_mastery.json"
    rows = [
        {
            "score": {
                "cycle_kind": "success",
                "intent": f"investigate-{domain}",
                "domain": domain,
            }
        }
        for _ in range(4)
    ]
    outcomes.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    mastery.write_text(json.dumps({domain: {"mastery": 0.05}}), encoding="utf-8")
    return outcomes, mastery


def _write_unsaturated_seam(tmp_path: Path, *, domain: str = "astronomy") -> tuple[Path, Path]:
    """Source-seam files that DO NOT saturate (only one attempt < the
    3-attempt floor) → multiplier stays 1.0."""
    outcomes = tmp_path / "autonomous_outcomes.jsonl"
    mastery = tmp_path / "knowledge_mastery.json"
    outcomes.write_text(
        json.dumps(
            {
                "score": {
                    "cycle_kind": "success",
                    "intent": f"investigate-{domain}",
                    "domain": domain,
                }
            }
        ),
        encoding="utf-8",
    )
    mastery.write_text(json.dumps({domain: {"mastery": 0.05}}), encoding="utf-8")
    return outcomes, mastery


def _prime_churn_from_seam(outcomes: Path, mastery: Path) -> CognitiveChurnSnapshot:
    """Build + cache the churn snapshot from the source-seam files.

    `evaluate_pressure` calls `assess_cognitive_churn()` with no path args; it
    then takes the `_LAST_SNAPSHOT` cache hit. Priming here is exactly the
    source-seam injection: the value `evaluate` reads is the one computed from
    the files we wrote.
    """
    cc.reset_churn_cache_for_tests()
    return assess_cognitive_churn(
        outcomes_path=outcomes,
        mastery_path=mastery,
        force_refresh=True,
    )


@pytest.fixture(autouse=True)
def _reset_churn_cache():
    """Each test owns the churn cache — reset before and after so a stale
    snapshot from another test can never leak into evaluate_pressure."""
    cc.reset_churn_cache_for_tests()
    yield
    cc.reset_churn_cache_for_tests()


def _curiosity_only_registry():
    """Registry pinning curiosity_gap to a known non-zero weight, all other
    contributors silent — isolates the churn scaling on one contributor."""
    return {CONTRIBUTOR_CURIOSITY_GAP: lambda _ss: _PINNED_CURIOSITY_GAP}


def test_injected_churn_value_scales_curiosity_weight_in_evaluate(tmp_path):
    """The churn VALUE from the source seam reaches evaluate and scales the
    curiosity_gap weight by the configured goal-saturated floor (0.35)."""
    outcomes, mastery = _write_saturated_seam(tmp_path)
    primed = _prime_churn_from_seam(outcomes, mastery)

    # Sanity on the seam: it really produced the saturated multiplier we expect.
    assert primed.curiosity_pressure_multiplier() == pytest.approx(_GOAL_SATURATED_FLOOR)
    assert "astronomy" in primed.saturated_domains

    decision = evaluate_pressure(SharedState(), registry=_curiosity_only_registry())

    # CONFIGURED VALUE: 0.8 * 0.35 = 0.28 reaches evaluate's output weight.
    expected = _PINNED_CURIOSITY_GAP * _GOAL_SATURATED_FLOOR
    assert decision.contributors[CONTRIBUTOR_CURIOSITY_GAP] == pytest.approx(expected)
    # And it is strictly LESS than the un-scaled injected weight — the churn
    # value actually moved the number, it didn't pass through inert.
    assert decision.contributors[CONTRIBUTOR_CURIOSITY_GAP] < _PINNED_CURIOSITY_GAP


def test_injected_churn_scale_is_recorded_in_contributor_detail(tmp_path):
    """evaluate records the exact churn_scale it applied (the value, audited),
    plus the saturated goal that drove it — proving the snapshot's value
    (not a generic 'churn ran' flag) reached the contributor detail."""
    outcomes, mastery = _write_saturated_seam(tmp_path)
    _prime_churn_from_seam(outcomes, mastery)

    decision = evaluate_pressure(SharedState(), registry=_curiosity_only_registry())

    detail = decision.contributor_details.get(CONTRIBUTOR_CURIOSITY_GAP) or {}
    assert detail.get("churn_scale") == pytest.approx(_GOAL_SATURATED_FLOOR)
    assert "investigate-astronomy" in detail.get("churn_saturated_goals", [])


def test_injected_churn_snapshot_reaches_decision_and_carries_to_episode(tmp_path):
    """The full churn snapshot (its VALUE, the saturated domain we injected)
    lands in decision.contributor_details['cognitive_churn'] and survives into
    to_snapshot() (episode.pressure carrier) — the value reaches the episode,
    not just the in-function locals."""
    outcomes, mastery = _write_saturated_seam(tmp_path)
    _prime_churn_from_seam(outcomes, mastery)

    decision = evaluate_pressure(SharedState(), registry=_curiosity_only_registry())

    churn_detail = decision.contributor_details.get("cognitive_churn") or {}
    assert churn_detail.get("saturated_domains") == ["astronomy"]
    assert churn_detail.get("rotate_recommended") is True

    # to_snapshot() is the episode.pressure carrier — the injected churn value
    # must ride through it (the `if churn_detail:` branch must be taken).
    snap = decision.to_snapshot()
    assert snap.get("cognitive_churn", {}).get("saturated_domains") == ["astronomy"]


def test_unsaturated_churn_leaves_curiosity_weight_unchanged(tmp_path):
    """GATE — other direction (fail-mode): an un-saturated churn snapshot
    (multiplier 1.0) must NOT scale the curiosity weight and must NOT inject a
    churn_scale. This proves the scaling is GATED on the real value, not
    fired unconditionally whenever churn runs."""
    outcomes, mastery = _write_unsaturated_seam(tmp_path)
    primed = _prime_churn_from_seam(outcomes, mastery)

    assert primed.curiosity_pressure_multiplier() == pytest.approx(1.0)
    assert primed.saturated_domains == frozenset()

    decision = evaluate_pressure(SharedState(), registry=_curiosity_only_registry())

    # Weight is the un-scaled injected value — churn touched nothing.
    assert decision.contributors[CONTRIBUTOR_CURIOSITY_GAP] == pytest.approx(_PINNED_CURIOSITY_GAP)
    detail = decision.contributor_details.get(CONTRIBUTOR_CURIOSITY_GAP) or {}
    assert "churn_scale" not in detail
    # The snapshot is still always emitted (advisory), but it reports no
    # saturation — the value reached evaluate and correctly said "no scaling".
    churn_detail = decision.contributor_details.get("cognitive_churn") or {}
    assert churn_detail.get("saturated_domains") == []
    assert churn_detail.get("rotate_recommended") is False

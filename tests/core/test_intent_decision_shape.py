"""Global A emits an IntentDecision (intent-only) — no goal, no target.

Operator doctrine: Global A PARSES intent from pressure and opens an episode.
It never creates a goal and never sets a target. The episode carries
``global_['intent_decision']`` with exactly {episode_id,intent,confidence,reason}.
"""

from __future__ import annotations

import dataclasses

import global_loop.loop as gl
from pressure_loop.contributors import EpisodeIgnition, IntentDecision, make_contributor
from pressure_loop.episode import EpisodeStore


def _ignition(opened_by: str = "epistemic_emptiness", **meta) -> EpisodeIgnition:
    c = make_contributor("autonomous", opened_by, 0.75, meta)
    return EpisodeIgnition(
        episode_id="ep-intent",
        aggregate_pressure=0.75,
        opened_by=opened_by,
        dominant_contributor=c,
        contributors=(c,),
        trajectory_snapshot=None,
    )


def test_intent_decision_type_has_no_target_field():
    names = {f.name for f in dataclasses.fields(IntentDecision)}
    assert names == {"episode_id", "intent", "confidence", "reason", "omega_lane"}
    assert "target" not in names


def test_global_a_writes_intent_decision_with_no_target():
    store = EpisodeStore()
    store.open(opened_by="epistemic_emptiness", pipeline_id=0, work_kind="autonomous")
    ep = store.current()

    decision = gl.run_global_a(ep, _ignition())

    # writer wrote the new intent-only key
    assert "intent_decision" in ep.global_
    assert set(ep.global_["intent_decision"].keys()) == {
        "episode_id",
        "intent",
        "confidence",
        "reason",
        "omega_lane",
    }
    # the load-bearing removal: no target anywhere on the decision
    assert "target" not in ep.global_["intent_decision"]
    # the old goal key is gone — Global A no longer creates a goal
    assert "action_goal" not in ep.global_

    # return value is an intent-only IntentDecision
    assert isinstance(decision, IntentDecision)
    assert decision.intent == "LEARN"  # epistemic_emptiness -> LEARN
    assert not hasattr(decision, "target")

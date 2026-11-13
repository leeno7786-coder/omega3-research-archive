"""End-to-end ignite_episode writes v2 outcome shape — no goal, no goal_state, no target."""

from __future__ import annotations

import json

import cognition_chain as cl
from auto_loop.intake.run import auto_input_from_episode
from pressure_loop.contributors import EpisodeIgnition, make_contributor
from pressure_loop.episode import EpisodeStore
from tests.helpers.pipeline_ownership import bind_ignition


def _ignition():
    c = make_contributor(
        "operator",
        "operator_command",
        1.0,
        {"message": "Learn Python"},
    )
    return EpisodeIgnition(
        episode_id="ep-v2",
        aggregate_pressure=1.0,
        opened_by="operator_command",
        dominant_contributor=c,
        contributors=(c,),
        trajectory_snapshot={"target_domain": "python", "target_capability": ""},
    )


def test_ignite_episode_writes_v2_outcome_shape(monkeypatch):
    store = EpisodeStore()


    def _stub_auto(ep):
        pack = dict((ep.meta or {}).get("execution_action_pack") or {})
        ep.auto = {
            "episode_id": ep.episode_id,
            # Real AUTO's fold echoes Global A's full source goal.
            "source_action_goal": dict(auto_input_from_episode(ep).source_action_goal),
            "execution_pack": pack,
            "auto": {
                "cycle_kind": "skipped",
                "status": "skipped",
                "outputs": {},
                "telemetry": {},
            },
        }
        return ep.auto

    monkeypatch.setattr("auto_loop.auto.run_auto_front", _stub_auto)

    # The per-episode Omega cognition makes a fold-back model pass; inject a
    # reason_fn so it parses stub JSON instead of hitting the runtime (the
    # "zero runtime leakage" pattern from tests/omega_loop/test_episode_cognition.py).
    # Without this the test depends on a live, idle runtime and fails whenever
    # it is down or busy. The asserted v2 outcome shape is Global B's, built
    # before Omega runs.
    import omega_loop.episode.run as _omega_ep

    _real_omega = _omega_ep.run_omega_for_episode
    _omega_stub_json = json.dumps(
        {
            "control": {
                "assessment": {
                    **dict.fromkeys(
                        ("who", "what", "when", "where", "why", "how",
                         "desired_state", "observed_state"),
                        "test stub evidence",
                    ),
                    "gap": "",
                    "evidence": ["stub receipt"],
                },
                "decision": "DONE",
                "pressure": None,
                "synthesis": "test stub",
            },
            "mastery": {
                "knowledge_mastery": 0.2,
                "application_mastery": 0.1,
                "rationale": "test stub",
            },
        }
    )
    _omega_calls = {"reason": 0}

    def _stub_reason(_s, _u):
        _omega_calls["reason"] += 1
        return _omega_stub_json

    monkeypatch.setattr(
        _omega_ep,
        "run_omega_for_episode",
        lambda ep, **kw: _real_omega(ep, reason_fn=_stub_reason),
    )

    episode = cl.ignite_episode(store, bind_ignition(_ignition()))

    # Prove the mock intercepted: the per-episode Omega used the injected
    # reason_fn, so it never touched the runtime (deterministic regardless of
    # whether the runtime is up, down, or busy).
    assert _omega_calls["reason"] >= 1
    assert store.current() is None
    assert episode is not None

    global_block = dict(episode.global_ or {})
    intent_decision = global_block.get("intent_decision") or {}
    assert intent_decision
    assert "target" not in intent_decision
    # Global A stamps the foldback lane beside the intent (realignment Task 2).
    assert set(intent_decision.keys()) == {"episode_id", "intent", "confidence", "reason", "omega_lane"}

    outcome_pack = global_block.get("outcome") or {}
    inner = outcome_pack.get("outcome") or {}
    assert set(inner.keys()) >= {
        "intent",
        "trajectory_alignment",
        "divergence_score",
        "execution_outcome",
        "status",
    }
    assert "goal" not in outcome_pack
    assert "goal_state" not in inner

    serialized = json.dumps(global_block)
    assert '"goal_state"' not in serialized
    assert '"goal"' not in serialized

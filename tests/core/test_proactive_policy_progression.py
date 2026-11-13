from types import SimpleNamespace

import pytest

import pressure_loop.proactive_lifecycle as lifecycle
from cognition_chain import _advance_proactive_lifecycle
from global_loop.assemble.run import GlobalBInput
from omega_loop.episode.packet import build_omega_episode_input
from omega_loop.episode.types import OmegaEpisodeResult


@pytest.fixture()
def seeded_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "_ledger_path", lambda: tmp_path / "proactive_lifecycle.jsonl")
    lifecycle._reset_cache_for_tests()
    thread = lifecycle.record_ask(
        prompt_id="prompt-1",
        episode_id="ep-ask",
        question="Continue or pivot?",
        why="Need direction",
        pressure_winner="clarification_gap",
        expected_response="A curriculum policy",
        options=(),
    )
    policy = lifecycle.bind_operator_policy(
        prompt_id="prompt-1",
        answer_episode_id="ep-answer",
        operator_text="Continue one scenario pass, then classify ten scenarios.",
    )
    yield thread, policy
    lifecycle._reset_cache_for_tests()


def _episode(episode_id, intent, policy):
    return SimpleNamespace(
        episode_id=episode_id,
        global_={
            "omega_packet": {
                "episode_id": episode_id,
                "intent": intent,
                "proactive_policy": policy.to_dict(),
            }
        },
    )


def _result(episode_id, **overrides):
    values = {
        "episode_id": episode_id,
        "cognized": True,
        "knowledge_to_ingest": (),
        "mastery_effects": (),
        "pressure_params": {},
        "response_text": None,
        "synthesis": None,
        "reflection": None,
        "intent_decision": None,
        "episode_assessment": None,
        "what_changed": None,
    }
    values.update(overrides)
    return OmegaEpisodeResult(**values)


def test_resolve_query_structures_policy_then_learn_completion_schedules_dialogue_apply(
    seeded_policy,
):
    thread, policy = seeded_policy
    plan = {
        "objective": "Learn and apply distribution choice.",
        "constraints": ["Do not pivot while weak."],
        "learning_steps": ["Study simple scenarios."],
        "requested_demonstrations": ["Classify ten scenarios."],
        "continuation_rule": "Continue boundary practice when weak.",
    }
    _advance_proactive_lifecycle(
        _episode("ep-answer", "RESOLVE_QUERY", policy),
        _result("ep-answer", operator_policy_plan=plan),
    )
    active = lifecycle.active_policy(thread_id=thread.thread_id)
    assert active is not None
    assert active.status == "active"
    assert lifecycle.thread_state(thread.thread_id) == "learning"

    _advance_proactive_lifecycle(
        _episode("ep-learn", "LEARN", active),
        _result(
            "ep-learn",
            episode_assessment={"completed": True, "quality": 0.8, "reason": "done"},
            what_changed={
                "learned": ["fixed trials distinguish Binomial"],
                "did": ["studied scenarios"],
                "verified": ["source-backed examples"],
                "failed": [],
            },
        ),
    )
    dispatch = lifecycle.pending_dialogue_dispatch()
    assert dispatch is not None
    assert dispatch["operator_policy"]["requested_demonstrations"] == ["Classify ten scenarios."]


def test_incomplete_learning_does_not_schedule_application(seeded_policy):
    thread, policy = seeded_policy
    plan = {
        "objective": "Learn distributions.",
        "constraints": [],
        "learning_steps": ["Study scenarios."],
        "requested_demonstrations": ["Classify ten scenarios."],
        "continuation_rule": "Continue if weak.",
    }
    _advance_proactive_lifecycle(
        _episode("ep-answer", "RESOLVE_QUERY", policy),
        _result("ep-answer", operator_policy_plan=plan),
    )
    _advance_proactive_lifecycle(
        _episode("ep-learn", "LEARN", lifecycle.active_policy()),
        _result(
            "ep-learn",
            episode_assessment={"completed": False, "quality": 0.2, "reason": "no source"},
            what_changed={"learned": [], "did": [], "verified": [], "failed": ["fetch"]},
        ),
    )
    assert lifecycle.thread_state(thread.thread_id) == "learning"
    assert lifecycle.pending_dialogue_dispatch() is None


def test_unrelated_learning_episode_does_not_inherit_latest_active_policy(seeded_policy):
    """An active statistics thread cannot leak into an unrelated conversation."""
    thread, _policy = seeded_policy
    gbi = GlobalBInput(
        episode_id="ep-french-cooking",
        pressure={
            "contributors": [
                {
                    "id": "mastery_gap",
                    "weight": 0.8,
                    "detail": {"domain": "french_cooking"},
                }
            ]
        },
        source_action_goal={"intent": "LEARN", "reason": "learn sauces"},
        execution_pack={
            "action": "USE_SKILL",
            "execution_inputs": {"domain": "french_cooking", "objective": "Learn sauces"},
        },
        auto_outcome={"auto": {"outputs": {}}},
        meta_reflection={},
        trajectory_snapshot={},
    )

    packet = build_omega_episode_input(
        gbi,
        {"execution": {"action": "USE_SKILL", "result": "success"}},
    )

    assert lifecycle.thread_state(thread.thread_id) == "policy_bound"
    assert packet.proactive_policy is None


def test_invalid_result_packet_does_not_kill_episode_close(seeded_policy):
    thread, policy = seeded_policy

    _advance_proactive_lifecycle(
        _episode("ep-invalid-result", "PROACTIVE_RESPONSE", policy),
        _result("ep-invalid-result", proactive_result={"mastery_status": "passed"}),
    )

    assert lifecycle.thread_state(thread.thread_id) == "policy_bound"

import pytest

from omega_loop.episode.prompts import prompt_spec_for
from omega_loop.episode.types import OmegaEpisodeInput


@pytest.fixture()
def packet_with_raw_policy():
    policy = {
        "policy_id": "policy-1",
        "thread_id": "thread-1",
        "origin_prompt_id": "prompt-1",
        "origin_episode_id": "ep-ask",
        "answer_episode_id": "ep-answer",
        "operator_text": (
            "Continue one scenario-based pass, then classify 10 scenarios. "
            "Do not pivot while classification remains weak."
        ),
        "objective": "Continue one scenario-based pass, then classify 10 scenarios.",
        "constraints": [],
        "learning_steps": [],
        "requested_demonstrations": [],
        "continuation_rule": "",
        "status": "bound",
    }
    return OmegaEpisodeInput(
        episode_id="ep-answer",
        intent="RESOLVE_QUERY",
        action="RESPONSE",
        tool=None,
        domain="probability distributions",
        operator_text=(
            "You (Omega) asked the operator: Continue or pivot?\n"
            "Operator answered: Continue one scenario-based pass."
        ),
        outcome="success",
        failure_reason=None,
        raw_tool_result=None,
        recall=(),
        recall_error=None,
        prior_mastery=None,
        proactive_policy=policy,
    )


def test_resolve_query_prompt_requires_policy_plan(packet_with_raw_policy):
    spec = prompt_spec_for(packet_with_raw_policy)
    assert spec is not None
    system, user = spec.build(packet_with_raw_policy)
    assert "operator_policy_plan" in system
    assert "requested_demonstrations" in system
    assert packet_with_raw_policy.proactive_policy["operator_text"] in user
    assert "mastery_debt_delta" in system


def _control(**pressure):
    assessment = dict.fromkeys(
        ("who", "what", "when", "where", "why", "how", "desired_state", "observed_state"),
        "policy evidence",
    )
    if not pressure:
        return {"assessment": {**assessment, "gap": "", "evidence": ["policy bound"]},
                "decision": "DONE", "pressure": None, "synthesis": "Policy structured."}
    return {
        "assessment": {**assessment, "gap": "prerequisite learning is owed", "evidence": ["policy bound"]},
        "decision": "PRESSURE",
        "pressure": {"confidence": 0.9,
                     "reason": "One scenario-based pass is required before application.",
                     "setpoints": {"mastery_debt_delta": 0.9},
                     "desired_state": "ten scenarios classified",
                     "observed_state": "policy bound, learning owed",
                     "resolution_conditions": ["ten scenarios classified"], **pressure},
        "synthesis": "Policy structured; learning owed.",
    }


def test_response_parser_preserves_structured_policy(packet_with_raw_policy):
    spec = prompt_spec_for(packet_with_raw_policy)
    assert spec is not None
    result = spec.parse(
        {
            "control": _control(intent="LEARN", domain="probability distributions",
                                topic="Binomial Poisson Normal scenarios"),
            "response_text": "I will follow that curriculum.",
            "operator_policy_plan": {
                "objective": "Distinguish and apply Binomial, Poisson, and Normal.",
                "constraints": ["Do not pivot while scenario classification remains weak."],
                "learning_steps": ["Study one simple scenario-based pass."],
                "requested_demonstrations": ["Classify 10 scenarios and explain each choice."],
                "continuation_rule": ("Continue Binomial-vs-Poisson correction if weak."),
            },
        },
        packet_with_raw_policy,
    )
    assert result.operator_policy_plan is not None
    assert result.operator_policy_plan["requested_demonstrations"] == [
        "Classify 10 scenarios and explain each choice."
    ]
    assert result.intent_decision["intent"] == "LEARN"


def test_policy_plan_is_dropped_when_demonstration_is_missing(packet_with_raw_policy):
    spec = prompt_spec_for(packet_with_raw_policy)
    assert spec is not None
    result = spec.parse(
        {
            "control": _control(),
            "response_text": "I will continue.",
            "pressure_params": {},
            "intent_decision": {"intent": "IDLE"},
            "operator_policy_plan": {
                "objective": "Study distributions.",
                "constraints": [],
                "learning_steps": ["Read examples."],
                "requested_demonstrations": [],
                "continuation_rule": "Continue if weak.",
            },
        },
        packet_with_raw_policy,
    )
    assert result.operator_policy_plan is None

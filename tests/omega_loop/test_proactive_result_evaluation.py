import pytest

from omega_loop.episode.prompts import prompt_spec_for
from omega_loop.episode.types import MasteryView, OmegaEpisodeInput


@pytest.fixture()
def packet():
    policy = {
        "policy_id": "policy-1",
        "thread_id": "thread-1",
        "origin_prompt_id": "prompt-1",
        "origin_episode_id": "ep-ask",
        "answer_episode_id": "ep-answer",
        "operator_text": "Classify ten distribution scenarios and explain each.",
        "objective": "Apply Binomial, Poisson, and Normal distinctions.",
        "constraints": ["Do not pivot while weak."],
        "learning_steps": ["Study simple scenarios."],
        "requested_demonstrations": ["Classify 10 scenarios and explain each choice."],
        "continuation_rule": "Continue boundary practice if weak.",
        "status": "active",
    }
    return OmegaEpisodeInput(
        episode_id="ep-apply",
        intent="PROACTIVE_RESPONSE",
        action="PROACTIVE_RESPONSE",
        tool=None,
        domain="probability distributions",
        operator_text=None,
        outcome="success",
        failure_reason=None,
        raw_tool_result=None,
        recall=({"kind": "remembered_knowledge", "content": "distribution rules"},),
        recall_error=None,
        prior_mastery=MasteryView(
            domain="probability distributions",
            knowledge_mastery=0.75,
            application_mastery=0.45,
            target_mastery=0.7,
        ),
        response_text="Executor classified the ten scenarios with explanations.",
        proactive_policy=policy,
        proactive_dispatch={
            "presentation_kind": "PRESENT_RESULT",
            "proactive_thread_id": "thread-1",
            "proactive_policy_id": "policy-1",
            "proactive_application_id": "app-1",
            "operator_policy": policy,
        },
    )


def _rows(count=10):
    names = [
        ("Heads in 20 flips", "Binomial", "fixed trials"),
        ("Emails in an hour", "Poisson", "event count in an interval"),
        ("Adult heights", "Normal", "continuous many-factor measurement"),
    ]
    return [
        {
            "prompt": names[index % len(names)][0],
            "answer": names[index % len(names)][1],
            "expected": names[index % len(names)][1],
            "correct": True,
            "explanation": names[index % len(names)][2],
            "evidence": [f"executor:row-{index + 1}"],
        }
        for index in range(count)
    ]


def _payload(*, rows=None, application_mastery=0.75, mastery_status="passed"):
    rows = list(rows if rows is not None else _rows())
    return {
        "response_text": "I completed the classification pass.",
        "proactive_result": {
            "title": "Distribution scenario classification",
            "summary": "Completed the requested scenario application.",
            "practiced": ["Binomial", "Poisson", "Normal"],
            "learned": ["Check fixed trials before interval counts."],
            "application": rows,
            "evidence_refs": ["executor:ep-apply"],
            "confidence": 0.9,
            "mastery_status": mastery_status,
            "mastery_evidence": [f"{sum(r['correct'] is True for r in rows)}/{len(rows)} verified"],
            "mistakes": [],
            "weak_patterns": [],
            "next_step": "Move into estimation and hypothesis testing.",
            "operator_input_needed": False,
        },
        "mastery": {
            "knowledge_mastery": 0.75,
            "application_mastery": application_mastery,
            "rationale": "Verified scenario application.",
        },
        "intent_decision": {"intent": "IDLE", "confidence": 0.9, "reason": "policy satisfied"},
        "pressure_params": {},
        "control": {
            "assessment": {
                **dict.fromkeys(
                    ("who", "what", "when", "where", "why", "how", "desired_state", "observed_state"),
                    "executor application evidence",
                ),
                "gap": "",
                "evidence": ["executor:ep-apply"],
            },
            "decision": "DONE",
            "pressure": None,
            "synthesis": "Application result evaluated for presentation.",
        },
    }


def test_present_result_parser_promotes_only_fully_verified_pass(packet):
    spec = prompt_spec_for(packet)
    assert spec is not None
    result = spec.parse(_payload(), packet)
    assert result.proactive_result["mastery_status"] == "passed"
    assert len(result.proactive_result["application"]) == 10
    assert result.mastery_effects[0].application_mastery >= packet.prior_mastery.target_mastery


def test_weak_result_is_presentable_but_has_no_mastery_effect(packet):
    rows = _rows()
    rows[3] = {
        **rows[3],
        "answer": "Binomial",
        "expected": "Poisson",
        "correct": False,
        "explanation": "I failed to check for an interval count.",
    }
    payload = _payload(rows=rows, application_mastery=0.58, mastery_status="weak")
    payload["proactive_result"]["mistakes"] = ["Scenario 4"]
    payload["proactive_result"]["weak_patterns"] = [
        "fixed trials were not checked before choosing Binomial"
    ]
    payload["proactive_result"]["next_step"] = "Practice Binomial vs Poisson boundary cases."
    result = prompt_spec_for(packet).parse(payload, packet)
    assert result.proactive_result["mastery_status"] == "weak"
    assert result.proactive_result["mistakes"]
    assert result.proactive_result["weak_patterns"]
    assert result.mastery_effects == ()


def test_unverified_rows_cannot_promote_mastery(packet):
    rows = _rows()
    rows[3]["correct"] = None
    result = prompt_spec_for(packet).parse(_payload(rows=rows), packet)
    assert result.proactive_result["mastery_status"] == "weak"
    assert result.mastery_effects == ()


def test_missing_application_rows_fails_closed(packet):
    payload = _payload(rows=[])
    with pytest.raises(ValueError, match="application evidence"):
        prompt_spec_for(packet).parse(payload, packet)

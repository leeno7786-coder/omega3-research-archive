from dataclasses import FrozenInstanceError

import pytest

from schema.proactive_lifecycle import (
    ApplicationRow,
    OperatorPolicy,
    ProactiveFoldback,
    ProactiveResult,
    stable_proactive_id,
)


def _policy() -> OperatorPolicy:
    return OperatorPolicy(
        policy_id="policy-1",
        thread_id="thread-1",
        origin_prompt_id="prompt-1",
        origin_episode_id="ep-ask",
        answer_episode_id="ep-answer",
        operator_text="Continue one scenario pass, then classify 10 cases.",
        objective="Learn and apply distribution selection.",
        constraints=("Do not pivot while classification remains weak.",),
        learning_steps=("Study simple Binomial, Poisson, and Normal examples.",),
        requested_demonstrations=("Classify 10 scenarios and explain each choice.",),
        continuation_rule="Continue corrective boundary practice when weak.",
        status="active",
    )


def test_operator_policy_preserves_verbatim_text_and_round_trips():
    policy = _policy()
    assert OperatorPolicy.from_dict(policy.to_dict()) == policy
    with pytest.raises(FrozenInstanceError):
        policy.operator_text = "changed"  # type: ignore[misc]


def test_result_rejects_empty_application_evidence():
    with pytest.raises(ValueError, match="application evidence"):
        ProactiveResult(
            result_id="result-1",
            thread_id="thread-1",
            episode_id="ep-apply",
            origin_prompt_id="prompt-1",
            policy_id="policy-1",
            application_id="app-1",
            title="Distribution practice",
            summary="Completed.",
            practiced=("distribution selection",),
            learned=(),
            application=(),
            evidence_refs=("executor:ep-apply",),
            confidence=0.7,
            mastery_status="passed",
            mastery_evidence=("10/10 verified",),
            mistakes=(),
            weak_patterns=(),
            next_step="estimation",
            operator_input_needed=False,
            status="ready",
        )


def test_blocked_result_can_report_no_application_rows():
    result = ProactiveResult(
        result_id="result-blocked",
        thread_id="thread-1",
        episode_id="ep-apply",
        origin_prompt_id="prompt-1",
        policy_id="policy-1",
        application_id="app-1",
        title="Distribution practice blocked",
        summary="The executor returned no classifications.",
        practiced=("distribution selection",),
        learned=(),
        application=(),
        evidence_refs=("executor:error:runtime_unavailable",),
        confidence=0.0,
        mastery_status="blocked",
        mastery_evidence=(),
        mistakes=(),
        weak_patterns=("No application rows were produced.",),
        next_step="Retry the dialogue application.",
        operator_input_needed=False,
        status="ready",
    )
    assert result.application == ()


def test_weak_result_and_foldback_preserve_corrective_evidence():
    row = ApplicationRow(
        prompt="Emails arriving in an hour",
        answer="Binomial",
        expected="Poisson",
        correct=False,
        explanation="I treated the count as fixed trials.",
        evidence=("executor:row-4",),
    )
    result = ProactiveResult.from_dict(
        {
            "result_id": "result-1",
            "thread_id": "thread-1",
            "episode_id": "ep-apply",
            "origin_prompt_id": "prompt-1",
            "policy_id": "policy-1",
            "application_id": "app-1",
            "title": "Distribution practice",
            "summary": "Boundary confusion remains.",
            "practiced": ["Binomial vs Poisson"],
            "learned": [],
            "application": [row.to_dict()],
            "evidence_refs": ["executor:ep-apply"],
            "confidence": 0.55,
            "mastery_status": "weak",
            "mastery_evidence": [],
            "mistakes": ["row 4"],
            "weak_patterns": ["fixed trials were not checked first"],
            "next_step": "Practice Binomial vs Poisson boundary cases.",
            "operator_input_needed": False,
            "status": "ready",
        }
    )
    foldback = ProactiveFoldback(
        foldback_id="fold-1",
        thread_id=result.thread_id,
        episode_id=result.episode_id,
        origin_result_id=result.result_id,
        mastery_status="weak",
        mastery_delta=0.0,
        durable_rules=(),
        corrected_rules=("Check whether trial count is fixed before choosing Binomial.",),
        unresolved_gaps=result.weak_patterns,
        next_pressure_candidates=(result.next_step,),
        resolution="continue",
        status="ready",
    )
    assert foldback.mastery_delta == 0.0
    assert foldback.resolution == "continue"


def test_stable_ids_are_replay_deterministic_and_unambiguous():
    assert stable_proactive_id("result", "thread-1", "app-1") == stable_proactive_id(
        "result", "thread-1", "app-1"
    )
    assert stable_proactive_id("result", "ab", "c") != stable_proactive_id("result", "a", "bc")


@pytest.mark.parametrize(
    ("field", "value"),
    (("mastery_status", "unknown"), ("status", "missing")),
)
def test_result_rejects_unknown_status_values(field, value):
    base = {
        "result_id": "result-1",
        "thread_id": "thread-1",
        "episode_id": "ep-apply",
        "origin_prompt_id": "prompt-1",
        "policy_id": "policy-1",
        "application_id": "app-1",
        "title": "Practice",
        "summary": "Done",
        "practiced": ["x"],
        "learned": [],
        "application": [
            {
                "prompt": "x",
                "answer": "y",
                "expected": "y",
                "correct": True,
                "explanation": "verified",
                "evidence": ["executor:1"],
            }
        ],
        "evidence_refs": ["executor:ep"],
        "confidence": 1.0,
        "mastery_status": "passed",
        "mastery_evidence": ["verified"],
        "mistakes": [],
        "weak_patterns": [],
        "next_step": "next",
        "operator_input_needed": False,
        "status": "ready",
    }
    base[field] = value
    with pytest.raises(ValueError):
        ProactiveResult.from_dict(base)

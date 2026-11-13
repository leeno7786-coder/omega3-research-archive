from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from omega_loop.motor.run import attach_motor_program
from pressure_loop.motor_loop_guard import (
    evaluate_motor_loop,
    material_pathway_fingerprint,
)
from schema.execution_evidence import ExecutionEvidence
from schema.motor_program import MotorProgram


def _planned(payload: str) -> MotorProgram:
    """The contract a payload describes, bound to the host identity of episode ep1."""
    return MotorProgram.from_dict({
        "program_id": "mp_ep1_01", "parent_program_id": "", "outcome_id": "out1",
        "episode_id": "ep1", "attempt_number": 1, "created_at": "2026-08-03T18:00:00+00:00",
        **json.loads(payload),
    })


def _program(*, fingerprint: str):
    return SimpleNamespace(
        program_id=f"mp-{fingerprint}",
        outcome_id="out1",
        pathway=SimpleNamespace(method=f"method-{fingerprint}"),
        execution_plan=SimpleNamespace(
            capability_class="base", operations=(f"method-{fingerprint}",)
        ),
        outcome_contract=SimpleNamespace(constraints=("constraint",)),
        evidence_contract=SimpleNamespace(
            to_dict=lambda: {
                "required_artifacts": [],
                "required_source_proof": [],
                "required_checks": ["verified"],
                "completion_signals": ["verified"],
                "disqualifying_failures": ["contradiction"],
            }
        ),
        fingerprint=lambda: fingerprint,
    )


def _evidence(*, progress: bool, failure_kind: str) -> ExecutionEvidence:
    return ExecutionEvidence.from_dict(
        {
            "program_id": "mp-current",
            "episode_id": "ep1",
            "status": "partial" if progress else "failure",
            "tool_calls": [],
            "commands_run": [],
            "sources_consulted": [],
            "files_changed": [],
            "artifacts": [],
            "tests_run": [],
            "verification_results": ["progress"] if progress else [],
            "blockers": [],
            "environmental_contradictions": [],
            "bounded_local_deviations": [],
            "progress_digest": "progress" if progress else "",
            "failure_kind": failure_kind,
            "failure_reason": failure_kind,
        }
    )


def _attempt(fingerprint: str, progress: bool, **overrides: object) -> dict:
    evidence = _evidence(
        progress=progress,
        failure_kind="" if progress else "verification_failure",
    ).to_dict()
    evidence["program_id"] = f"mp-{fingerprint}"
    return {
        "program_id": f"mp-{fingerprint}",
        "program_fingerprint": fingerprint,
        "material_fingerprint": material_pathway_fingerprint(_program(fingerprint=fingerprint)),
        "verified_progress": progress,
        "failure_kind": "" if progress else "verification_failure",
        "execution_status": "success" if progress else "failure",
        "method": f"method-{fingerprint}",
        "required_capabilities": ["base"],
        "execution_evidence": evidence,
        **overrides,
    }


def test_two_equivalent_no_progress_programs_invalidate_pathway():
    decision = evaluate_motor_loop(
        current=_program(fingerprint="same"),
        evidence=_evidence(progress=False, failure_kind="irrelevant_evidence"),
        prior_attempts=[_attempt("same", progress=False)],
        equivalent_limit=2,
        distinct_limit=3,
    )
    assert decision.action == "invalidate"
    assert decision.failed_fingerprint == "same"


def test_three_distinct_valid_failures_ask_operator():
    decision = evaluate_motor_loop(
        current=_program(fingerprint="c"),
        evidence=_evidence(progress=False, failure_kind="verification_failure"),
        prior_attempts=[_attempt("a", False), _attempt("b", False)],
        equivalent_limit=2,
        distinct_limit=3,
    )
    assert decision.action == "ask_operator"
    assert decision.attempted_methods == ("method-a", "method-b", "method-c")
    assert "verification_failure" in decision.unresolved_contradiction


@pytest.mark.parametrize(
    "failure_kind",
    ["timeout", "bridge_unavailable", "runtime_unavailable", "rate_limited", "safety_hold"],
)
def test_transient_or_safety_failure_does_not_invalidate_pathway(failure_kind):
    decision = evaluate_motor_loop(
        current=_program(fingerprint="same"),
        evidence=_evidence(progress=False, failure_kind=failure_kind),
        prior_attempts=[_attempt("same", False)],
        equivalent_limit=2,
        distinct_limit=3,
    )
    assert decision.action == "allow"


def test_verified_progress_does_not_count_as_revolving_pathway():
    decision = evaluate_motor_loop(
        current=_program(fingerprint="same"),
        evidence=_proven_check_evidence(),
        prior_attempts=[_attempt("same", False)],
    )
    assert decision.action == "allow"


def _untrusted_summary_evidence(*, program_id: str = "mp-current") -> ExecutionEvidence:
    """A successful-looking model summary without an executor-backed check."""
    return ExecutionEvidence.from_dict(
        {
            "program_id": program_id,
            "episode_id": "ep1",
            "status": "success",
            "tool_calls": [],
            "commands_run": [],
            "sources_consulted": [],
            "files_changed": [],
            "artifacts": [],
            "tests_run": [],
            "verification_results": ["progress"],
            "blockers": [],
            "environmental_contradictions": [],
            "bounded_local_deviations": [],
            "progress_digest": "I made meaningful progress and everything looks good",
            "failure_kind": "",
            "failure_reason": "",
        }
    )


def _proven_check_evidence(*, program_id: str = "mp-current") -> ExecutionEvidence:
    return ExecutionEvidence.from_dict(
        {
            **_untrusted_summary_evidence(program_id=program_id).to_dict(),
            "verification_results": [
                {
                    "requested": "pytest",
                    "status": "passed",
                    "executor_provenance": {
                        "source": "qwen_tool_call",
                        "tool_call_id": "tc-pytest",
                        "tool_name": "run_command",
                        "command": "python -m pytest -q",
                        "result_digest": "factual-result-digest",
                        "exit_code": 0,
                    },
                }
            ],
        }
    )


def test_two_equivalent_prose_only_successes_invalidate_pathway():
    prior = _attempt("same", False)
    prior["execution_evidence"] = _untrusted_summary_evidence(program_id="mp-same").to_dict()
    prior["failure_kind"] = ""

    decision = evaluate_motor_loop(
        current=_program(fingerprint="same"),
        evidence=_untrusted_summary_evidence(),
        prior_attempts=[prior],
        equivalent_limit=2,
        distinct_limit=3,
    )

    assert decision.action == "invalidate"
    assert decision.equivalent_failures == 2
    assert decision.execution_evidence["failure_kind"] == "irrelevant_evidence"


def test_three_distinct_prose_only_successes_ask_operator():
    prior = []
    for fingerprint in ("a", "b"):
        row = _attempt(fingerprint, False)
        row["execution_evidence"] = _untrusted_summary_evidence(
            program_id=f"mp-{fingerprint}"
        ).to_dict()
        row["failure_kind"] = ""
        prior.append(row)

    decision = evaluate_motor_loop(
        current=_program(fingerprint="c"),
        evidence=_untrusted_summary_evidence(),
        prior_attempts=prior,
        equivalent_limit=2,
        distinct_limit=3,
    )

    assert decision.action == "ask_operator"
    assert decision.distinct_failures == 3


def test_provenance_backed_passed_check_is_factual_progress():
    decision = evaluate_motor_loop(
        current=_program(fingerprint="same"),
        evidence=_proven_check_evidence(),
        prior_attempts=[_attempt("same", False)],
    )

    assert decision.action == "allow"
    assert decision.equivalent_failures == 0


def test_empty_failure_kind_with_failed_check_normalizes_to_verification_failure():
    raw = _proven_check_evidence().to_dict()
    raw["verification_results"][0]["status"] = "failed"
    raw["progress_digest"] = "tests were attempted"
    evidence = ExecutionEvidence.from_dict(raw)

    decision = evaluate_motor_loop(
        current=_program(fingerprint="same"),
        evidence=evidence,
        prior_attempts=[_attempt("same", False)],
    )

    assert decision.action == "invalidate"
    assert decision.execution_evidence["failure_kind"] == "verification_failure"


def test_loop_decision_is_immutable():
    decision = evaluate_motor_loop(
        current=_program(fingerprint="same"),
        evidence=_evidence(progress=False, failure_kind="verification_failure"),
        prior_attempts=[_attempt("same", False)],
    )
    with pytest.raises(FrozenInstanceError):
        decision.action = "allow"


def _payload(
    *,
    method: str,
    operation: str | None = None,
    capability_class: str = "shell",
    required_capabilities: list[str] | None = None,
    pathway_name: str = "path",
    rationale: str = "test",
    change_evidence: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "outcome_contract": {
                "objective": "resolve the contradiction",
                "acceptance_criteria": ["verified"],
                "constraints": [],
                "priorities": ["correctness"],
            },
            "pathway": {
                "pathway_id": "path",
                "revision": 1,
                "name": pathway_name,
                "method": method,
                "operational_rationale": rationale,
                "failed_predecessor_id": "",
                "change_evidence": change_evidence or [],
            },
            "execution_plan": {
                "capability_class": capability_class,
                "required_capabilities": required_capabilities or [],
                "operations": [operation or method],
                "permitted_local_discretion": [],
                "prohibited_deviations": [],
                "return_to_mind_conditions": ["blocked"],
            },
            "evidence_contract": {
                "required_artifacts": [],
                "required_source_proof": [],
                "required_checks": ["verified"],
                "completion_signals": ["verified"],
                "disqualifying_failures": ["contradiction"],
            },
        }
    )


def _planner_episode(prior_attempts: list[dict]) -> SimpleNamespace:
    episode_id = "ep1"
    return SimpleNamespace(
        episode_id=episode_id,
        opened_at="2026-08-03T18:00:00+00:00",
        pressure={"opened_by": "execution_gap"},
        global_={
            "intent_decision": {
                "episode_id": episode_id,
                "intent": "PURSUE_OUTCOME",
            }
        },
        meta={
            "execution_action_pack": {
                "episode_id": episode_id,
                "action": "EXECUTE_MOTOR_PROGRAM",
                "execution_inputs": {"prior_attempts": prior_attempts},
                "source_action_goal": {
                    "episode_id": episode_id,
                    "intent": "PURSUE_OUTCOME",
                },
            }
        },
        auto={},
    )


@pytest.mark.parametrize("status", ["partial", "yielded"])
def test_partial_or_yielded_no_progress_keeps_failure_semantics(status):
    raw = _evidence(progress=False, failure_kind="verification_failure").to_dict()
    raw["status"] = status
    decision = evaluate_motor_loop(
        current=_program(fingerprint="same"),
        evidence=ExecutionEvidence.from_dict(raw),
        prior_attempts=[_attempt("same", False)],
    )
    assert decision.action == "invalidate"


def test_missing_nested_execution_evidence_does_not_count():
    decision = evaluate_motor_loop(
        current=_program(fingerprint="same"),
        evidence=_evidence(progress=False, failure_kind="verification_failure"),
        prior_attempts=[
            _attempt(
                "same",
                False,
                execution_evidence=None,
                verified_progress=False,
                execution_status="failure",
            )
        ],
    )
    assert decision.action == "allow"
    assert decision.equivalent_failures == 1


def test_nested_evidence_progress_overrides_untrusted_summary_flags():
    row = _attempt("same", False)
    row["execution_evidence"] = _proven_check_evidence(program_id="mp-same").to_dict()
    row["execution_evidence"]["program_id"] = "mp-same"
    decision = evaluate_motor_loop(
        current=_program(fingerprint="same"),
        evidence=_evidence(progress=False, failure_kind="verification_failure"),
        prior_attempts=[row],
    )
    assert decision.action == "allow"


def test_historical_evidence_without_failure_kind_is_factually_normalized():
    row = _attempt("same", False)
    row["execution_evidence"]["failure_kind"] = ""
    row["execution_evidence"]["failure_reason"] = ""
    decision = evaluate_motor_loop(
        current=_program(fingerprint="same"),
        evidence=_evidence(progress=False, failure_kind="verification_failure"),
        prior_attempts=[row],
    )
    assert decision.action == "invalidate"
    assert decision.equivalent_failures == 2


@pytest.mark.parametrize("status", ["partial", "yielded"])
def test_non_transient_blocked_no_progress_attempt_counts(status):
    row = _attempt("same", False)
    row["execution_evidence"]["status"] = status
    row["execution_evidence"]["blockers"] = ["required verification data absent"]
    decision = evaluate_motor_loop(
        current=_program(fingerprint="same"),
        evidence=_evidence(progress=False, failure_kind="verification_failure"),
        prior_attempts=[row],
    )
    assert decision.action == "invalidate"


def test_safety_hold_blocker_remains_non_pathway():
    row = _attempt("same", False)
    row["execution_evidence"]["status"] = "partial"
    row["execution_evidence"]["blockers"] = ["safety hold pending operator approval"]
    decision = evaluate_motor_loop(
        current=_program(fingerprint="same"),
        evidence=_evidence(progress=False, failure_kind="verification_failure"),
        prior_attempts=[row],
    )
    assert decision.action == "allow"


def test_new_required_capability_allows_one_third_failure_successor_only():
    current = _program(fingerprint="c")
    prior = [
        _attempt("a", False, required_capabilities=["base"]),
        _attempt("b", False, required_capabilities=["base"]),
    ]
    decision = evaluate_motor_loop(
        current=current,
        evidence=_evidence(progress=False, failure_kind="verification_failure"),
        prior_attempts=prior,
        proposed_required_capabilities=("new-capability",),
    )
    assert decision.action == "allow"
    assert decision.capability_exception_used is True
    assert decision.requires_material_change is True

    used = _attempt(
        "c",
        False,
        required_capabilities=["base"],
        capability_exception_used=True,
    )
    repeated = evaluate_motor_loop(
        current=_program(fingerprint="d"),
        evidence=_evidence(progress=False, failure_kind="verification_failure"),
        prior_attempts=[*prior, used],
        proposed_required_capabilities=("another-new-capability",),
    )
    assert repeated.action == "ask_operator"


def test_execution_plan_required_capabilities_is_additive_and_round_trips():
    defaulted = _planned(_payload(method="same"))
    required = _planned(_payload(method="same", required_capabilities=["new-capability"]))
    assert defaulted is not None and required is not None
    assert defaulted.execution_plan.required_capabilities == ()
    assert required.execution_plan.required_capabilities == ("new-capability",)
    assert required.to_dict()["execution_plan"]["required_capabilities"] == ["new-capability"]


def test_required_capability_set_is_canonical_for_round_trip_and_material_identity():
    reordered = _planned(_payload(
            method="same",
            required_capabilities=[" z-capability ", "a-capability", "z-capability"],
        ))
    canonical = _planned(_payload(
            method="same",
            required_capabilities=["a-capability", "z-capability"],
        ))
    assert reordered is not None and canonical is not None
    assert reordered.execution_plan.required_capabilities == (
        "a-capability",
        "z-capability",
    )
    assert reordered.to_dict() == canonical.to_dict()
    assert material_pathway_fingerprint(reordered) == material_pathway_fingerprint(canonical)


def test_execution_plan_rejects_blank_required_capability():
    with pytest.raises(ValueError, match="required_capabilities"):
        _planned(_payload(method="same", required_capabilities=["  "]))


def test_material_fingerprint_ignores_cosmetic_pathway_churn():
    first = _planned(_payload(method="same"))
    renamed = _planned(_payload(
            method="renamed descriptive method",
            operation="same",
            pathway_name="renamed path",
            rationale="fresh wording only",
            change_evidence=["renamed labels"],
        ))
    assert first is not None and renamed is not None
    assert first.fingerprint() != renamed.fingerprint()
    assert material_pathway_fingerprint(first) == material_pathway_fingerprint(renamed)


def test_attachment_persists_material_fingerprint(monkeypatch):
    episode = _planner_episode([])
    program = _planned(_payload(method="same"))
    assert program is not None
    monkeypatch.setattr("omega_loop.motor.run.motor_programs_shadow_mode", lambda: True)
    attached = attach_motor_program(episode, program)
    assert attached["material_fingerprint"] == material_pathway_fingerprint(program)


def test_three_failures_stage_existing_operator_priority_question(monkeypatch):
    import cognition_chain as chain

    staged = []
    monkeypatch.setattr("pressure_loop.pressure.stage_reseed", staged.extend)
    decision, successor = chain._apply_motor_loop_guard(
        _program(fingerprint="c"),
        _evidence(progress=False, failure_kind="contradictory_facts"),
        [_attempt("a", False), _attempt("b", False)],
        {"proposed_action": {"required_capabilities": ["base"]}},
        episode_id="ep1",
    )

    assert decision.action == "ask_operator"
    assert successor is None
    assert len(staged) == 1
    assert staged[0].type == "operator_priority"
    question = staged[0].metadata["question"]
    assert "method-a" in question and "method-b" in question and "c" in question
    assert "contradictory_facts" in question


def test_invalidation_carries_exact_attempt_into_successor_planning_state():
    import cognition_chain as chain

    evidence = _evidence(progress=False, failure_kind="verification_failure")
    decision, successor = chain._apply_motor_loop_guard(
        _program(fingerprint="same"),
        evidence,
        [_attempt("same", False)],
        {"proposed_action": {"required_capabilities": ["shell"]}},
        episode_id="ep1",
    )

    assert decision.action == "invalidate"
    assert successor is not None
    latest = successor["proposed_action"]["prior_attempts"][-1]
    assert latest["program_fingerprint"] == "same"
    assert latest["loop_action"] == "invalidate"
    assert latest["execution_evidence"] == evidence.to_dict()
    assert latest["material_fingerprint"]


def test_successor_history_stores_normalized_blank_failure_kind():
    import cognition_chain as chain

    decision, successor = chain._apply_motor_loop_guard(
        _program(fingerprint="same"),
        _evidence(progress=False, failure_kind=""),
        [_attempt("same", False)],
        {"proposed_action": {"required_capabilities": ["shell"]}},
        episode_id="ep1",
    )

    assert decision.action == "invalidate"
    assert successor is not None
    latest = successor["proposed_action"]["prior_attempts"][-1]
    assert latest["failure_kind"] == "irrelevant_evidence"
    assert latest["execution_evidence"]["failure_kind"] == "irrelevant_evidence"

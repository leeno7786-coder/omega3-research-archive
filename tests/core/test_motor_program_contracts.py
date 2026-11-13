from dataclasses import FrozenInstanceError

import pytest

from schema.emergent_pressure import EmergentPressure
from schema.execution_evidence import ExecutionEvidence
from schema.motor_program import MotorPlanningInput, MotorProgram
from schema.pathway_outcome import PathwayOutcome


def _program_payload() -> dict:
    return {
        "program_id": "mp_ep1_01",
        "parent_program_id": "",
        "outcome_id": "out_ep1",
        "episode_id": "ep1",
        "attempt_number": 1,
        "created_at": "2026-07-10T20:00:00Z",
        "outcome_contract": {
            "objective": "derive and verify backpropagation",
            "acceptance_criteria": ["derivation present", "gradient check passes"],
            "constraints": ["verified sources only"],
            "priorities": ["correctness", "evidence"],
        },
        "pathway": {
            "pathway_id": "derive-implement-verify",
            "revision": 1,
            "name": "derive implement verify",
            "method": "derive, implement, and compare analytical gradients",
            "operational_rationale": "retrieval produced zero mastery",
            "failed_predecessor_id": "retrieval-r4",
            "change_evidence": ["two irrelevant retrieval outcomes"],
        },
        "execution_plan": {
            "capability_class": "code_execution",
            "operations": ["derive equations", "implement example", "run gradient check"],
            "permitted_local_discretion": ["choose file names"],
            "prohibited_deviations": ["do not replace derivation with summary"],
            "return_to_mind_conditions": ["requirements conflict", "verification unavailable"],
        },
        "evidence_contract": {
            "required_artifacts": ["derivation", "implementation"],
            "required_source_proof": ["source_id"],
            "required_checks": ["gradient check"],
            "completion_signals": ["all checks pass"],
            "disqualifying_failures": ["unverified formula"],
        },
    }


def test_motor_program_round_trip_is_stable_and_frozen():
    program = MotorProgram.from_dict(_program_payload())
    assert MotorProgram.from_dict(program.to_dict()) == program
    assert program.fingerprint() == MotorProgram.from_dict(program.to_dict()).fingerprint()
    with pytest.raises(FrozenInstanceError):
        program.attempt_number = 2


def test_motor_program_requires_outcome_operations_and_evidence():
    payload = _program_payload()
    payload["execution_plan"]["operations"] = []
    with pytest.raises(ValueError, match="operations"):
        MotorProgram.from_dict(payload)


def test_code_motor_program_requires_a_factual_verification_check():
    payload = _program_payload()
    payload["execution_plan"]["capability_class"] = "code_execution"
    payload["evidence_contract"]["required_checks"] = []

    with pytest.raises(ValueError, match="verification check"):
        MotorProgram.from_dict(payload)


def test_code_motor_program_rejects_non_executor_addressable_check():
    payload = _program_payload()
    payload["evidence_contract"]["required_checks"] = [
        "Functional verification: add operates correctly for positive integers"
    ]

    with pytest.raises(ValueError, match="executor-addressable"):
        MotorProgram.from_dict(payload)


def test_source_proof_lane_rejects_test_receipts():
    payload = _program_payload()
    payload["evidence_contract"]["required_source_proof"] = [
        "test suite run evidence",
        "test passed count matches total test run count",
    ]

    with pytest.raises(ValueError, match="source proof"):
        MotorProgram.from_dict(payload)


@pytest.mark.parametrize(
    "source_proof",
    [
        "https://arxiv.org/abs/1706.03762",
        "10.48550/arXiv.1706.03762",
        "src_abcd1234",
    ],
)
def test_source_proof_lane_accepts_concrete_source_identifiers(source_proof):
    payload = _program_payload()
    payload["evidence_contract"]["required_source_proof"] = [source_proof]

    program = MotorProgram.from_dict(payload)

    assert program.evidence_contract.required_source_proof == (source_proof,)


def test_code_motor_program_rejects_unmatchable_artifact_prose():
    payload = _program_payload()
    payload["evidence_contract"]["required_artifacts"] = [
        "omega-smoke directory",
        "test_module.py (or equivalent test file)",
        "pytest execution receipt",
    ]

    with pytest.raises(ValueError, match="artifact paths"):
        MotorProgram.from_dict(payload)


def test_code_motor_program_rejects_test_receipt_in_artifact_lane():
    payload = _program_payload()
    payload["evidence_contract"]["required_artifacts"] = [
        "src/model.py",
        "pytest execution receipt",
    ]

    with pytest.raises(ValueError, match="artifact paths"):
        MotorProgram.from_dict(payload)


@pytest.mark.parametrize(
    "capability",
    [
        "python",
        "implementation",
        "file_editing",
        "software-engineering",
        "Software Engineering",
    ],
)
def test_code_capability_aliases_cannot_bypass_required_check(capability):
    payload = _program_payload()
    payload["execution_plan"]["capability_class"] = capability
    payload["execution_plan"]["operations"] = ["perform the requested work"]
    payload["evidence_contract"]["required_checks"] = []

    with pytest.raises(ValueError, match="verification check"):
        MotorProgram.from_dict(payload)


@pytest.mark.parametrize(
    "operation",
    [
        "write_file src/result.py",
        "edit file src/result.py",
        "build the software package",
        "test the implementation",
        "implement the requested module",
    ],
)
def test_code_operations_cannot_hide_under_generic_capability(operation):
    payload = _program_payload()
    payload["execution_plan"]["capability_class"] = "task_execution"
    payload["execution_plan"]["operations"] = [operation]
    payload["evidence_contract"]["required_checks"] = []

    with pytest.raises(ValueError, match="verification check"):
        MotorProgram.from_dict(payload)


def test_research_only_program_does_not_require_code_check():
    payload = _program_payload()
    payload["execution_plan"]["capability_class"] = "web_research"
    payload["execution_plan"]["required_capabilities"] = ["source_retrieval"]
    payload["execution_plan"]["operations"] = [
        "search primary sources",
        "read papers",
        "summarize findings",
    ]
    payload["evidence_contract"]["required_checks"] = []

    assert MotorProgram.from_dict(payload).execution_plan.capability_class == "web_research"


def test_web_research_rejects_a_plan_check_that_direct_fetch_cannot_prove():
    payload = _program_payload()
    payload["execution_plan"]["capability_class"] = "web_research"
    payload["execution_plan"]["required_capabilities"] = ["source_retrieval"]
    payload["execution_plan"]["operations"] = ["fetch structured source content"]
    payload["evidence_contract"]["required_checks"] = ["plan_present"]

    with pytest.raises(ValueError, match="fetch or fetch_verified"):
        MotorProgram.from_dict(payload)


@pytest.mark.parametrize(
    "operation",
    ["test hypothesis", "test", "build evidence map"],
)
def test_research_and_development_does_not_self_classify_as_code(operation):
    payload = _program_payload()
    payload["execution_plan"]["capability_class"] = "research_and_development"
    payload["execution_plan"]["required_capabilities"] = ["source_retrieval"]
    payload["execution_plan"]["operations"] = [operation]
    payload["evidence_contract"]["required_checks"] = []

    assert MotorProgram.from_dict(payload).execution_plan.operations == (operation,)


@pytest.mark.parametrize("operation", ["write test", "test code", "build package"])
def test_code_action_with_distinct_code_target_still_requires_check(operation):
    payload = _program_payload()
    payload["execution_plan"]["capability_class"] = "task_execution"
    payload["execution_plan"]["operations"] = [operation]
    payload["evidence_contract"]["required_checks"] = []

    with pytest.raises(ValueError, match="verification check"):
        MotorProgram.from_dict(payload)


def test_execution_evidence_preserves_partial_and_yielded():
    for status in ("partial", "yielded"):
        row = ExecutionEvidence.from_dict(
            {
                "program_id": "mp_ep1_01",
                "episode_id": "ep1",
                "status": status,
                "tool_calls": [],
                "commands_run": [],
                "sources_consulted": [],
                "files_changed": [],
                "artifacts": [],
                "tests_run": [],
                "verification_results": [],
                "blockers": [],
                "environmental_contradictions": [],
                "bounded_local_deviations": [],
                "progress_digest": "work remains",
                "failure_kind": "",
                "failure_reason": "",
            }
        )
        assert row.status == status


def test_emergent_pressure_requires_executable_semantics():
    with pytest.raises(ValueError, match="proposed_action"):
        EmergentPressure.from_dict(
            {
                "pressure_id": "p1",
                "name": "depth_gap",
                "source_episode_id": "ep1",
                "outcome_id": "out1",
                "desired_state": "technical proof",
                "observed_state": "definitions only",
                "gap_description": "no depth",
                "evidence": ["mastery delta 0"],
                "failed_program_ids": ["mp1"],
                "urgency": 0.8,
                "persistence": 2,
                "proposed_action": {},
                "activation_conditions": ["gap unresolved"],
                "resolution_conditions": ["proof verified"],
                "supersedes_pressure_id": "",
            }
        )


def test_pathway_outcome_never_claims_mastery_without_verification():
    with pytest.raises(ValueError, match="verified"):
        PathwayOutcome.from_dict(
            {
                "pathway_id": "derive",
                "revision": 1,
                "task_context": "learning",
                "domain": "machine-learning",
                "program_id": "mp1",
                "pre_mastery": {"knowledge": 0.0, "application": 0.0},
                "post_mastery": {"knowledge": 0.2, "application": 0.0},
                "knowledge_delta": 0.2,
                "application_delta": 0.0,
                "verification_verdict": "unverified",
                "progress_efficiency": 0.5,
                "failure_fingerprint": "",
                "neuromodulator_response": {},
            }
        )


def test_pathway_outcome_rejects_hidden_unverified_mastery_transition():
    with pytest.raises(ValueError, match="verified"):
        PathwayOutcome.from_dict(
            {
                "pathway_id": "derive",
                "revision": 1,
                "task_context": "learning",
                "domain": "machine-learning",
                "program_id": "mp1",
                "pre_mastery": {"knowledge": 0.0, "application": 0.0},
                "post_mastery": {"knowledge": 0.2, "application": 0.0},
                "knowledge_delta": 0.0,
                "application_delta": 0.0,
                "verification_verdict": "unverified",
                "progress_efficiency": 0.5,
                "failure_fingerprint": "",
                "neuromodulator_response": {},
            }
        )


def test_pathway_outcome_validates_claimed_deltas_against_mastery_transition():
    with pytest.raises(ValueError, match="knowledge_delta"):
        PathwayOutcome.from_dict(
            {
                "pathway_id": "derive",
                "revision": 1,
                "task_context": "learning",
                "domain": "machine-learning",
                "program_id": "mp1",
                "pre_mastery": {"knowledge": 0.0, "application": 0.0},
                "post_mastery": {"knowledge": 0.2, "application": 0.0},
                "knowledge_delta": 0.1,
                "application_delta": 0.0,
                "verification_verdict": "verified",
                "progress_efficiency": 0.5,
                "failure_fingerprint": "",
                "neuromodulator_response": {},
            }
        )


def test_pathway_outcome_rejects_duplicate_mastery_aliases():
    with pytest.raises(ValueError, match="duplicate.*knowledge"):
        PathwayOutcome.from_dict(
            {
                "pathway_id": "derive",
                "revision": 1,
                "task_context": "learning",
                "domain": "machine-learning",
                "program_id": "mp1",
                "pre_mastery": {
                    "knowledge": 0.0,
                    "knowledge_mastery": 0.0,
                    "application": 0.0,
                },
                "post_mastery": {
                    "knowledge": 0.0,
                    "knowledge_mastery": 0.0,
                    "application": 0.0,
                },
                "knowledge_delta": 0.0,
                "application_delta": 0.0,
                "verification_verdict": "unverified",
                "progress_efficiency": 0.0,
                "failure_fingerprint": "",
                "neuromodulator_response": {},
            }
        )


def test_pathway_outcome_rejects_unknown_mastery_dimensions():
    with pytest.raises(ValueError, match="unknown mastery dimension"):
        PathwayOutcome.from_dict(
            {
                "pathway_id": "derive",
                "revision": 1,
                "task_context": "learning",
                "domain": "machine-learning",
                "program_id": "mp1",
                "pre_mastery": {"knowledge": 0.0, "application": 0.0, "reasoning": 0.0},
                "post_mastery": {"knowledge": 0.0, "application": 0.0, "reasoning": 0.0},
                "knowledge_delta": 0.0,
                "application_delta": 0.0,
                "verification_verdict": "unverified",
                "progress_efficiency": 0.0,
                "failure_fingerprint": "",
                "neuromodulator_response": {},
            }
        )


def test_pathway_outcome_treats_missing_baseline_as_zero_for_unverified_gain():
    with pytest.raises(ValueError, match="verified"):
        PathwayOutcome.from_dict(
            {
                "pathway_id": "derive",
                "revision": 1,
                "task_context": "learning",
                "domain": "machine-learning",
                "program_id": "mp1",
                "pre_mastery": {"application": 0.0},
                "post_mastery": {"knowledge_mastery": 0.2, "application_mastery": 0.0},
                "knowledge_delta": 0.0,
                "application_delta": 0.0,
                "verification_verdict": "unverified",
                "progress_efficiency": 0.0,
                "failure_fingerprint": "",
                "neuromodulator_response": {},
            }
        )


def test_pathway_outcome_exports_one_canonical_mastery_vocabulary():
    outcome = PathwayOutcome.from_dict(
        {
            "pathway_id": "derive",
            "revision": 1,
            "task_context": "learning",
            "domain": "machine-learning",
            "program_id": "mp1",
            "pre_mastery": {"knowledge_mastery": 0.1, "application_mastery": 0.2},
            "post_mastery": {"knowledge_mastery": 0.1, "application_mastery": 0.2},
            "knowledge_delta": 0.0,
            "application_delta": 0.0,
            "verification_verdict": "unverified",
            "progress_efficiency": 0.0,
            "failure_fingerprint": "",
            "neuromodulator_response": {},
        }
    )
    assert outcome.to_dict()["pre_mastery"] == {"knowledge": 0.1, "application": 0.2}
    assert PathwayOutcome.from_dict(outcome.to_dict()) == outcome


def test_contracts_are_deeply_immutable_and_export_detached_json():
    planning = MotorPlanningInput.from_dict(
        {
            "episode_id": "ep1",
            "intent": "learn",
            "action": "EXECUTE_MOTOR_PROGRAM",
            "pressure": {"nested": {"items": ["a"]}},
            "execution_pack": {"inputs": {"attempt": 1}},
            "recalled_context": [{"source": {"id": "s1"}}],
            "prior_attempts": [{"evidence": ["failed"]}],
            "mastery_state": {"domain": {"knowledge": 0.1}},
            "neuromod_state": {"signals": [0.2]},
        }
    )
    evidence = ExecutionEvidence.from_dict(
        {
            "program_id": "mp1",
            "episode_id": "ep1",
            "status": "partial",
            "tool_calls": [{"args": {"paths": ["a.py"]}}],
        }
    )
    pressure = EmergentPressure.from_dict(
        {
            "pressure_id": "p1",
            "name": "gap",
            "source_episode_id": "ep1",
            "outcome_id": "out1",
            "desired_state": {"proof": ["verified"]},
            "observed_state": {"proof": []},
            "gap_description": "no proof",
            "evidence": [],
            "failed_program_ids": [],
            "urgency": 0.8,
            "persistence": 1,
            "proposed_action": {
                "intended_transformation": "produce proof",
                "candidate_pathway": "derive",
                "required_capabilities": ["reasoning"],
                "options": {"checks": ["verify"]},
            },
            "activation_conditions": ["gap unresolved"],
            "resolution_conditions": ["proof verified"],
            "supersedes_pressure_id": "",
        }
    )
    outcome = PathwayOutcome.from_dict(
        {
            "pathway_id": "derive",
            "revision": 1,
            "task_context": "learning",
            "domain": "machine-learning",
            "program_id": "mp1",
            "pre_mastery": {"knowledge": 0.0, "application": 0.0},
            "post_mastery": {"knowledge": 0.0, "application": 0.0},
            "knowledge_delta": 0.0,
            "application_delta": 0.0,
            "verification_verdict": "unverified",
            "progress_efficiency": 0.0,
            "failure_fingerprint": "f1",
            "neuromodulator_response": {"stress": 0.2},
        }
    )

    with pytest.raises(TypeError):
        planning.pressure["nested"]["items"] = ()
    with pytest.raises(TypeError):
        planning.execution_pack["inputs"]["attempt"] = 2
    with pytest.raises(TypeError):
        planning.recalled_context[0]["source"]["id"] = "s2"
    with pytest.raises(TypeError):
        planning.prior_attempts[0]["evidence"] = ()
    with pytest.raises(TypeError):
        planning.mastery_state["domain"]["knowledge"] = 0.9
    with pytest.raises(TypeError):
        planning.neuromod_state["signals"] = ()
    with pytest.raises(TypeError):
        evidence.tool_calls[0]["args"]["paths"] = ()
    with pytest.raises(TypeError):
        pressure.proposed_action["options"]["checks"] = ()
    with pytest.raises(TypeError):
        outcome.pre_mastery["knowledge"] = 1.0
    with pytest.raises(TypeError):
        outcome.neuromodulator_response["stress"] = 0.9

    exported = planning.to_dict()
    exported["pressure"]["nested"]["items"].append("detached")
    assert planning.to_dict()["pressure"]["nested"]["items"] == ["a"]
    evidence.to_dict()["tool_calls"][0]["args"]["paths"].append("b.py")
    assert evidence.to_dict()["tool_calls"][0]["args"]["paths"] == ["a.py"]
    pressure.to_dict()["proposed_action"]["options"]["checks"].append("detached")
    assert pressure.to_dict()["proposed_action"]["options"]["checks"] == ["verify"]
    outcome.to_dict()["pre_mastery"]["knowledge"] = 1.0
    assert outcome.to_dict()["pre_mastery"]["knowledge"] == 0.0


@pytest.mark.parametrize(
    ("section", "field"),
    (
        ("execution_plan", "operations"),
        ("evidence_contract", "required_artifacts"),
        ("evidence_contract", "required_source_proof"),
        ("evidence_contract", "required_checks"),
        ("evidence_contract", "completion_signals"),
    ),
)
def test_motor_program_rejects_blank_required_entries(section, field):
    payload = _program_payload()
    payload[section][field] = ["  "]
    with pytest.raises(ValueError, match=field):
        MotorProgram.from_dict(payload)


def test_emergent_pressure_rejects_blank_required_capabilities():
    with pytest.raises(ValueError, match="proposed_action"):
        EmergentPressure.from_dict(
            {
                "pressure_id": "p1",
                "name": "gap",
                "source_episode_id": "ep1",
                "outcome_id": "out1",
                "desired_state": "proof",
                "observed_state": "none",
                "gap_description": "no proof",
                "evidence": [],
                "failed_program_ids": [],
                "urgency": 0.8,
                "persistence": 1,
                "proposed_action": {
                    "intended_transformation": "produce proof",
                    "candidate_pathway": "derive",
                    "required_capabilities": ["  "],
                },
                "activation_conditions": ["gap unresolved"],
                "resolution_conditions": ["proof verified"],
                "supersedes_pressure_id": "",
            }
        )


@pytest.mark.parametrize(
    "field",
    ("outcome_contract", "pathway", "execution_plan", "evidence_contract"),
)
def test_motor_program_direct_construction_rejects_wrong_nested_types(field):
    program = MotorProgram.from_dict(_program_payload())
    values = dict(program.__dict__)
    values[field] = {}
    with pytest.raises(TypeError, match=field):
        MotorProgram(**values)

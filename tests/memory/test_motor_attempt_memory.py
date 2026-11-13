from __future__ import annotations

from types import SimpleNamespace

import pytest

from memory import episode_sram
from pressure_loop import pressure as pressure_mod
from memory.motor_experience import persist_attempt_sequence
from schema.execution_evidence import ExecutionEvidence
from schema.motor_program import MotorProgram


def _program() -> MotorProgram:
    return MotorProgram.from_dict(
        {
            "program_id": "mp1",
            "parent_program_id": "",
            "outcome_id": "out1",
            "episode_id": "ep1",
            "attempt_number": 1,
            "created_at": "2026-07-10T00:00:00+00:00",
            "outcome_contract": {
                "objective": "implement and verify",
                "acceptance_criteria": ["pytest passes"],
            },
            "pathway": {
                "pathway_id": "derive-implement-verify",
                "revision": 1,
                "name": "derive implement verify",
                "method": "derive, implement, test",
                "operational_rationale": "produces executable proof",
            },
            "execution_plan": {
                "capability_class": "code_execution",
                "operations": ["implement", "run tests"],
            },
            "evidence_contract": {
                "required_checks": ["pytest"],
                "completion_signals": ["tests pass"],
            },
        }
    )


def _evidence() -> ExecutionEvidence:
    return ExecutionEvidence.from_dict(
        {
            "program_id": "mp1",
            "episode_id": "ep1",
            "status": "failure",
            "tests_run": ["pytest tests/unit -q"],
            "verification_results": ["1 failed"],
            "files_changed": ["src/example.py"],
            "failure_kind": "verification_failure",
            "failure_reason": "test failed",
        }
    )


@pytest.fixture(autouse=True)
def _episode_bucket():
    episode_sram.reset()
    episode_sram.open_bucket("test-motor-attempt", pipeline_id=0)
    try:
        yield
    finally:
        episode_sram.reset()


def _origin_pressure():
    from schema.emergent_pressure import EmergentPressure

    return EmergentPressure.from_dict(
        {
            "pressure_id": "p1",
            "name": "origin",
            "source_episode_id": "ep0",
            "outcome_id": "out1",
            "desired_state": "pass",
            "observed_state": "fail",
            "gap_description": "verification is red",
            "evidence": ["prior failure"],
            "failed_program_ids": ["mp0"],
            "urgency": 0.8,
            "persistence": 1,
            "proposed_action": {
                "intended_transformation": "implement",
                "candidate_pathway": "old",
                "required_capabilities": ["testing"],
            },
            "activation_conditions": ["red"],
            "resolution_conditions": ["green"],
            "supersedes_pressure_id": "",
        }
    )


def test_attempt_memory_holds_evidence_before_assessment_with_shared_identity():
    receipts = persist_attempt_sequence(
        _program(),
        _evidence(),
        {"assessment": "the selected method failed verification"},
        {"pressure_id": "p1", "status": "revise", "reason": "test failed"},
    )
    writes = [(item.envelope, item.write_options) for item in episode_sram.pending()]

    assert len(receipts) == 2
    assert all(receipt.validation_status == "held" for receipt in receipts)
    assert [env.type for env, _ in writes] == [
        "motor_execution_evidence",
        "motor_attempt_assessment",
    ]
    assert [env.coord for env, _ in writes] == [[4, 5, 0], [4, 6, 0]]
    assert all(
        kw == {"propose": False, "bypass_guardian": True, "skip_embed": False}
        for _, kw in writes
    )
    evidence_meta = writes[0][0].payload["enrichment"]
    assessment_meta = writes[1][0].payload["enrichment"]
    for key in ("program_id", "outcome_id", "episode_id", "timestamp", "evidence_refs"):
        assert evidence_meta[key] == assessment_meta[key]
    assert evidence_meta["evidence_refs"] == [
        "src/example.py",
        "pytest tests/unit -q",
        "1 failed",
    ]


def test_attempt_memory_rejection_halts_before_assessment(monkeypatch):
    writes = []
    monkeypatch.setattr(
        "memory.motor_experience.intake_memory",
        lambda env, **kw: (
            writes.append(env.type)
            or SimpleNamespace(validation_status="rejected", path=None, message="invalid")
        ),
    )

    with pytest.raises(RuntimeError, match="motor_execution_evidence.*rejected"):
        persist_attempt_sequence(_program(), _evidence(), {}, {"status": "revise"})

    assert writes == ["motor_execution_evidence"]


def test_pressure_records_reuse_attempt_identity_and_factual_refs(monkeypatch):
    from memory.motor_experience import motor_attempt_identity
    from pressure_loop.emergent_pressure import resolve_emergent_pressure, stage_emergent_pressure
    from schema.emergent_pressure import EmergentPressure

    identity = motor_attempt_identity(_program(), _evidence())
    resolution = {"pressure_id": "p1", "status": "supersede", "reason": "test failed"}
    successor = EmergentPressure.from_dict(
        {
            "pressure_id": "p2",
            "name": "revised_gap",
            "source_episode_id": "ep1",
            "outcome_id": "out1",
            "desired_state": {"tests": "passing"},
            "observed_state": {"tests": "failing"},
            "gap_description": "verification remains red",
            "evidence": ["pytest tests/unit -q", "1 failed"],
            "failed_program_ids": ["mp1"],
            "urgency": 0.8,
            "persistence": 2,
            "proposed_action": {
                "intended_transformation": "repair the failing test",
                "candidate_pathway": "repair-verify",
                "required_capabilities": ["code_execution", "testing"],
            },
            "activation_conditions": ["verification is red"],
            "resolution_conditions": ["pytest passes"],
            "supersedes_pressure_id": "p1",
        }
    )

    resolve_emergent_pressure(
        "p1",
        resolution,
        identity,
        originating_pressure=_origin_pressure(),
        provenance_refs=["memory/motor_execution_evidence.json"],
    )
    stage_emergent_pressure(
        successor,
        identity,
        originating_pressure=_origin_pressure(),
        provenance_refs=["memory/motor_execution_evidence.json"],
    )
    writes = [item.envelope for item in episode_sram.pending()]

    assert [env.type for env in writes] == ["emergent_pressure_resolution", "emergent_pressure"]
    for env in writes:
        meta = env.payload["enrichment"]
        assert meta["program_id"] == "mp1"
        assert meta["outcome_id"] == "out1"
        assert meta["episode_id"] == "ep1"
        assert meta["timestamp"] == "2026-07-10T00:00:00+00:00"
        assert meta["evidence_refs"] == list(identity.evidence_refs)
        assert meta["provenance_refs"] == ["memory/motor_execution_evidence.json"]


def test_successor_pressure_identity_conflict_is_rejected_before_write(monkeypatch):
    from memory.motor_experience import motor_attempt_identity
    from pressure_loop.emergent_pressure import stage_emergent_pressure
    from schema.emergent_pressure import EmergentPressure

    before = list(episode_sram.pending())
    identity = motor_attempt_identity(_program(), _evidence())
    conflicting = EmergentPressure.from_dict(
        {
            "pressure_id": "p2",
            "name": "bad",
            "source_episode_id": "ep1",
            "outcome_id": "out1",
            "desired_state": "pass",
            "observed_state": "fail",
            "gap_description": "conflicting outcome",
            "evidence": ["1 failed"],
            "failed_program_ids": ["mp1"],
            "urgency": 0.8,
            "persistence": 1,
            "proposed_action": {
                "intended_transformation": "repair",
                "candidate_pathway": "repair",
                "required_capabilities": ["testing"],
            },
            "activation_conditions": ["red"],
            "resolution_conditions": ["green"],
            "supersedes_pressure_id": "invented",
        }
    )
    with pytest.raises(ValueError, match="supersed"):
        stage_emergent_pressure(conflicting, identity, originating_pressure=_origin_pressure())
    assert episode_sram.pending() == before


def test_first_pressure_create_enters_sram_without_fabricated_origin(monkeypatch):
    from memory.motor_experience import motor_attempt_identity
    from pressure_loop.emergent_pressure import create_emergent_pressure
    from schema.emergent_pressure import EmergentPressure

    identity = motor_attempt_identity(_program(), _evidence())
    first = EmergentPressure.from_dict(
        {
            "pressure_id": "p-first",
            "name": "technical_depth_gap",
            "source_episode_id": "ep1",
            "outcome_id": "out1",
            "desired_state": "derived and verified",
            "observed_state": "retrieval only",
            "gap_description": "retrieval was shallow",
            "evidence": ["depth check failed"],
            "failed_program_ids": ["mp1"],
            "urgency": 0.9,
            "persistence": 1,
            "proposed_action": {
                "intended_transformation": "derive and verify",
                "candidate_pathway": "derive-implement-verify",
                "required_capabilities": ["building", "testing"],
            },
            "activation_conditions": ["depth absent"],
            "resolution_conditions": ["executor verification passes"],
            "supersedes_pressure_id": "",
        }
    )

    create_emergent_pressure(
        first,
        identity,
        provenance_refs=["memory/motor_execution_evidence.json"],
    )
    writes = [(item.envelope, item.write_options) for item in episode_sram.pending()]

    assert [env.type for env, _ in writes] == ["emergent_pressure"]
    assert writes[0][1] == {
        "propose": False,
        "bypass_guardian": True,
        "skip_embed": False,
    }
    assert writes[0][0].payload["enrichment"]["supersedes_pressure_id"] == ""
    # The record is the mind's MEMORY of this pressure. It is deliberately NOT
    # staged as a contributor (operator 2026-08-15): a fold minting a rival drive
    # is what collided with the standing mastery_gap and parked cognition for
    # 7m23s. The returned contributor is the caller's handle, not a queued drive.
    assert pressure_mod.staged_signals() == ()

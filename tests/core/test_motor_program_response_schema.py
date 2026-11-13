"""The motor-planning response-format grammar must guarantee, structurally,
everything the MotorProgram dataclasses require — otherwise a grammar-valid
object still fails dataclass validation and only the repair loop can recover it
(the live 'model output was not parseable JSON' / re-parse trap)."""

from __future__ import annotations

from schema.motor_program import MotorProgram, motor_program_response_schema


def test_grammar_required_covers_evidence_dataclass_invariants():
    """EvidenceContract.__post_init__ requires completion_signals (non-empty) AND
    at least one of required_artifacts/source_proof/checks. The grammar's
    `required` + minItems must force both, so a grammar-valid evidence_contract is
    never dataclass-invalid."""
    schema = motor_program_response_schema()
    ev = schema["properties"]["evidence_contract"]
    assert "completion_signals" in ev["required"], "grammar must force completion_signals"
    assert "required_checks" in ev["required"]
    assert ev["properties"]["required_checks"].get("minItems", 0) >= 1, (
        "required_checks must be non-empty so the 'at least one evidence source' "
        "dataclass invariant is structurally guaranteed"
    )


def test_minimal_grammar_valid_program_passes_dataclass_validation():
    """An object carrying exactly the grammar-required fields (honoring the minItems
    the grammar enforces) builds a valid MotorProgram with no dataclass rejection."""
    prog = {
        "program_id": "mp1",
        "parent_program_id": "",
        "outcome_id": "o1",
        "episode_id": "ep1",
        "attempt_number": 1,
        "created_at": "2026-07-10T00:00:00+00:00",
        "outcome_contract": {"objective": "do x", "acceptance_criteria": ["done"]},
        "pathway": {
            "pathway_id": "pw1",
            "revision": 1,
            "name": "n",
            "method": "m",
            "operational_rationale": "r",
        },
        "execution_plan": {"capability_class": "code_execution", "operations": ["op1"]},
        "evidence_contract": {
            "required_checks": ["pytest"],
            "completion_signals": ["all tests pass"],
        },
    }
    program = MotorProgram.from_dict(prog)  # must not raise
    assert program.evidence_contract.completion_signals == ("all tests pass",)
    assert program.evidence_contract.required_checks == ("pytest",)

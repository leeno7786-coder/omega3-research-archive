"""Direct first-person memory of Omega-authored motor attempts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from memory.api import CanonicalEnvelope, WriteReceipt, intake_memory, intake_receipt_accepted
from schema.execution_evidence import ExecutionEvidence
from schema.motor_program import MotorProgram


MOTOR_EVIDENCE_COORD = [4, 5, 0]
MOTOR_ASSESSMENT_COORD = [4, 6, 0]


@dataclass(frozen=True)
class MotorAttemptIdentity:
    program_id: str
    outcome_id: str
    episode_id: str
    timestamp: str
    evidence_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "program_id": self.program_id,
            "outcome_id": self.outcome_id,
            "episode_id": self.episode_id,
            "timestamp": self.timestamp,
            "evidence_refs": list(self.evidence_refs),
        }


def _reference_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("ref", "path", "url", "id", "name"):
            text = str(value.get(key) or "").strip()
            if text:
                return text
        return json.dumps(dict(value), sort_keys=True, ensure_ascii=False, default=str)
    return str(value).strip()


def _evidence_refs(evidence: ExecutionEvidence) -> list[str]:
    refs: list[str] = []
    for field_name in (
        "files_changed",
        "artifacts",
        "sources_consulted",
        "tests_run",
        "verification_results",
    ):
        for item in getattr(evidence, field_name):
            ref = _reference_text(item)
            if ref and ref not in refs:
                refs.append(ref)
    return refs


def motor_attempt_identity(
    program: MotorProgram, evidence: ExecutionEvidence
) -> MotorAttemptIdentity:
    if not isinstance(program, MotorProgram):
        raise TypeError("program must be a canonical MotorProgram")
    if not isinstance(evidence, ExecutionEvidence):
        raise TypeError("evidence must be canonical ExecutionEvidence")
    if evidence.program_id != program.program_id or evidence.episode_id != program.episode_id:
        raise ValueError("execution evidence identity must match the motor program")
    return MotorAttemptIdentity(
        program_id=program.program_id,
        outcome_id=program.outcome_id,
        episode_id=program.episode_id,
        timestamp=program.created_at,
        evidence_refs=tuple(_evidence_refs(evidence)),
    )


def _require_intake_accepted(receipt: WriteReceipt, record_type: str) -> WriteReceipt:
    if not intake_receipt_accepted(receipt):
        message = str(getattr(receipt, "message", None) or "memory write rejected")
        raise RuntimeError(f"{record_type} continuity write rejected: {message}")
    return receipt


def _envelope(
    *,
    record_type: str,
    coord: list[int],
    identity: MotorAttemptIdentity,
    body: dict[str, Any],
) -> CanonicalEnvelope:
    identity_dict = identity.to_dict()
    record = {**identity_dict, **body}
    return CanonicalEnvelope(
        cycle_id=identity.episode_id,
        coord=list(coord),
        type=record_type,
        payload={
            "title": f"{record_type} {identity.program_id}",
            "summary": f"Omega motor attempt {identity.program_id} in {identity.episode_id}",
            "text": json.dumps(record, sort_keys=True, ensure_ascii=False, default=str),
            "subtopic": record_type,
            "source_session_id": identity.episode_id,
            "source_session_timestamp": identity.timestamp,
            "enrichment": {**identity_dict, "canonical": True},
        },
        source="omega.motor_experience",
        confidence=1.0,
        timestamp_created=identity.timestamp,
    )


def persist_attempt_sequence(
    program: MotorProgram,
    evidence: ExecutionEvidence,
    assessment: Mapping[str, Any],
    resolution: Mapping[str, Any],
    *,
    identity: MotorAttemptIdentity | None = None,
) -> tuple[WriteReceipt, ...]:
    """Commit factual evidence before Omega's assessment of the same attempt.

    These are first-person cognitive self-writes, so both use the Guardian-free
    canonical path. A rejection aborts immediately because assessment without its
    antecedent evidence would corrupt continuity.
    """
    canonical_identity = motor_attempt_identity(program, evidence)
    if identity is not None and identity != canonical_identity:
        raise ValueError("attempt identity must match the canonical program and evidence")
    identity = canonical_identity
    evidence_receipt = _require_intake_accepted(
        intake_memory(
            _envelope(
                record_type="motor_execution_evidence",
                coord=MOTOR_EVIDENCE_COORD,
                identity=identity,
                body={
                    "motor_program": program.to_dict(),
                    "execution_evidence": evidence.to_dict(),
                },
            ),
            propose=False,
            bypass_guardian=True,
        ),
        "motor_execution_evidence",
    )
    assessment_receipt = _require_intake_accepted(
        intake_memory(
            _envelope(
                record_type="motor_attempt_assessment",
                coord=MOTOR_ASSESSMENT_COORD,
                identity=identity,
                body={
                    "assessment": dict(assessment or {}),
                    "pressure_resolution": dict(resolution or {}),
                },
            ),
            propose=False,
            bypass_guardian=True,
        ),
        "motor_attempt_assessment",
    )
    return evidence_receipt, assessment_receipt

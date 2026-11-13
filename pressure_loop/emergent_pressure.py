"""Canonical persistence and next-cycle staging for Omega-authored pressures."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from memory.api import CanonicalEnvelope, WriteReceipt, intake_memory, intake_receipt_accepted
from memory.motor_experience import MotorAttemptIdentity
from pressure_loop.contributors import Contributor, make_contributor
from schema.emergent_pressure import EmergentPressure


EMERGENT_PRESSURE_COORD = [4, 4, 0]
_RESOLUTION_STATUSES = frozenset(
    {"satisfied", "continue", "revise", "split", "supersede", "retire"}
)


def _require_intake_accepted(receipt: WriteReceipt, record_type: str) -> WriteReceipt:
    if not intake_receipt_accepted(receipt):
        message = str(getattr(receipt, "message", None) or "memory write rejected")
        raise RuntimeError(f"{record_type} continuity write rejected: {message}")
    return receipt


def _write_pressure_record(
    *,
    record_type: str,
    record_id: str,
    attempt: MotorAttemptIdentity,
    body: Mapping[str, Any],
    enrichment: Mapping[str, Any],
    provenance_refs: Sequence[str] = (),
) -> WriteReceipt:
    identity = attempt.to_dict()
    provenance = [str(ref).strip() for ref in provenance_refs if str(ref).strip()]
    record = {**identity, **dict(body), "provenance_refs": provenance}
    envelope = CanonicalEnvelope(
        cycle_id=attempt.episode_id,
        coord=list(EMERGENT_PRESSURE_COORD),
        type=record_type,
        payload={
            "title": f"{record_type} {record_id}",
            "summary": str(body.get("gap_description") or body.get("reason") or record_id),
            "text": json.dumps(record, sort_keys=True, ensure_ascii=False, default=str),
            "subtopic": record_type,
            "source_session_id": attempt.episode_id,
            "source_session_timestamp": attempt.timestamp,
            "enrichment": {
                **identity,
                **dict(enrichment),
                "provenance_refs": provenance,
                "canonical": True,
            },
        },
        source="omega.emergent_pressure",
        confidence=1.0,
        timestamp_created=attempt.timestamp,
    )
    return _require_intake_accepted(
        intake_memory(envelope, propose=False, bypass_guardian=True),
        record_type,
    )


def stage_emergent_pressure(
    pressure: EmergentPressure,
    attempt: MotorAttemptIdentity,
    *,
    originating_pressure: EmergentPressure,
    provenance_refs: Sequence[str] = (),
) -> Contributor:
    """Persist a validated pressure before staging it for the next gather."""
    if not isinstance(pressure, EmergentPressure):
        raise TypeError("pressure must be a canonical EmergentPressure")
    if not isinstance(originating_pressure, EmergentPressure):
        raise TypeError("originating_pressure must be a canonical EmergentPressure")
    if originating_pressure.outcome_id != attempt.outcome_id:
        raise ValueError("originating pressure outcome_id must match the attempt")
    if pressure.source_episode_id != attempt.episode_id:
        raise ValueError("successor pressure source_episode_id must match the attempt")
    if pressure.outcome_id != attempt.outcome_id:
        raise ValueError("successor pressure outcome_id must match the attempt")
    if attempt.program_id not in pressure.failed_program_ids:
        raise ValueError(
            "successor pressure failed_program_ids must include the attempt program_id"
        )
    if pressure.supersedes_pressure_id != originating_pressure.pressure_id:
        raise ValueError("successor pressure must supersede the originating pressure")
    body = pressure.to_dict()
    _write_pressure_record(
        record_type="emergent_pressure",
        record_id=pressure.pressure_id,
        attempt=attempt,
        body=body,
        enrichment={
            "pressure_id": pressure.pressure_id,
            "outcome_id": pressure.outcome_id,
            "supersedes_pressure_id": pressure.supersedes_pressure_id,
        },
        provenance_refs=provenance_refs,
    )
    contributor = make_contributor(
        "omega",
        pressure.name,
        pressure.urgency,
        {
            "origin": "omega_emergent_pressure",
            "outcome_id": pressure.outcome_id,
            "emergent_pressure": body,
            "message": pressure.gap_description,
        },
    )
    # NOT staged as pressure (operator 2026-08-15: "there should be no minted
    # anything for pressure, there should only be the deterministic decisions
    # that the system calls, that's what the model needs to emit").
    #
    # The canonical record above is the mind's MEMORY of this pressure and is
    # kept. What is gone is minting a rival contributor out of a closing
    # episode's fold: it entered the very next gather under a fold-authored name
    # at a fold-authored urgency, contending with the standing drive it was
    # supposed to be a consequence of. Measured 2026-08-15 -- a fold minted
    # python_syntax_fundamentals_content_gap at 0.850 against mastery_gap at
    # 0.850, the ignition gate called ambiguous_winner on gap=0.000, and
    # autonomous cognition parked for 7m23s on a collision with its own residue.
    #
    # Omega raises pressure through the deterministic levers it EMITS
    # (the retired levels adapter's mastery_debt_delta, application_debt_delta,
    # clarification_debt_delta, ...) which are canonical types, keyed by
    # (type, domain), and die at ignition. One channel, one lifecycle.
    return contributor


def create_emergent_pressure(
    pressure: EmergentPressure,
    attempt: MotorAttemptIdentity,
    *,
    provenance_refs: Sequence[str] = (),
) -> Contributor:
    """Persist and stage the first pressure created by an originless attempt."""
    if not isinstance(pressure, EmergentPressure):
        raise TypeError("pressure must be a canonical EmergentPressure")
    if not isinstance(attempt, MotorAttemptIdentity):
        raise TypeError("attempt must be a MotorAttemptIdentity")
    if pressure.source_episode_id != attempt.episode_id:
        raise ValueError("created pressure source_episode_id must match the attempt")
    if pressure.outcome_id != attempt.outcome_id:
        raise ValueError("created pressure outcome_id must match the attempt")
    if attempt.program_id not in pressure.failed_program_ids:
        raise ValueError("created pressure failed_program_ids must include the attempt program_id")
    if pressure.supersedes_pressure_id:
        raise ValueError("created pressure supersedes_pressure_id must be empty")
    body = pressure.to_dict()
    _write_pressure_record(
        record_type="emergent_pressure",
        record_id=pressure.pressure_id,
        attempt=attempt,
        body=body,
        enrichment={
            "pressure_id": pressure.pressure_id,
            "outcome_id": pressure.outcome_id,
            "supersedes_pressure_id": "",
            "lifecycle_status": "create",
        },
        provenance_refs=provenance_refs,
    )
    contributor = make_contributor(
        "omega",
        pressure.name,
        pressure.urgency,
        {
            "origin": "omega_emergent_pressure",
            "outcome_id": pressure.outcome_id,
            "emergent_pressure": body,
            "message": pressure.gap_description,
        },
    )
    # NOT staged as pressure (operator 2026-08-15: "there should be no minted
    # anything for pressure, there should only be the deterministic decisions
    # that the system calls, that's what the model needs to emit").
    #
    # The canonical record above is the mind's MEMORY of this pressure and is
    # kept. What is gone is minting a rival contributor out of a closing
    # episode's fold: it entered the very next gather under a fold-authored name
    # at a fold-authored urgency, contending with the standing drive it was
    # supposed to be a consequence of. Measured 2026-08-15 -- a fold minted
    # python_syntax_fundamentals_content_gap at 0.850 against mastery_gap at
    # 0.850, the ignition gate called ambiguous_winner on gap=0.000, and
    # autonomous cognition parked for 7m23s on a collision with its own residue.
    #
    # Omega raises pressure through the deterministic levers it EMITS
    # (the retired levels adapter's mastery_debt_delta, application_debt_delta,
    # clarification_debt_delta, ...) which are canonical types, keyed by
    # (type, domain), and die at ignition. One channel, one lifecycle.
    return contributor


def resolve_emergent_pressure(
    pressure_id: str,
    resolution: Mapping[str, Any],
    attempt: MotorAttemptIdentity,
    *,
    originating_pressure: EmergentPressure,
    provenance_refs: Sequence[str] = (),
) -> WriteReceipt:
    """Commit an evidence-grounded record that supersedes a prior pressure."""
    pressure_id = str(pressure_id or "").strip()
    if not pressure_id:
        raise ValueError("pressure_id is required")
    if not isinstance(attempt, MotorAttemptIdentity):
        raise TypeError("attempt must be a MotorAttemptIdentity")
    if not isinstance(originating_pressure, EmergentPressure):
        raise TypeError("originating_pressure must be a canonical EmergentPressure")
    if pressure_id != originating_pressure.pressure_id:
        raise ValueError("pressure_id must equal the originating pressure")
    if originating_pressure.outcome_id != attempt.outcome_id:
        raise ValueError("originating pressure outcome_id must match the attempt")
    value = dict(resolution or {})
    if value.get("pressure_id") and str(value["pressure_id"]) != pressure_id:
        raise ValueError("resolution pressure_id must match pressure_id")
    status = str(value.get("status") or "").strip()
    reason = str(value.get("reason") or "").strip()
    if status not in _RESOLUTION_STATUSES:
        raise ValueError(f"invalid pressure resolution status: {status!r}")
    if not reason:
        raise ValueError("pressure resolution reason is required")
    body = {
        "pressure_id": pressure_id,
        "status": status,
        "reason": reason,
        "supersedes_pressure_id": pressure_id,
    }
    return _write_pressure_record(
        record_type="emergent_pressure_resolution",
        record_id=pressure_id,
        attempt=attempt,
        body=body,
        enrichment={
            "pressure_id": pressure_id,
            "status": status,
            "supersedes_pressure_id": pressure_id,
        },
        provenance_refs=provenance_refs,
    )

"""Canonical request-to-execution foldback visible to Omega cognition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from schema.execution_evidence import ExecutionEvidence
from schema.motor_program import _freeze, _thaw


EXECUTION_FOLDBACK_SCHEMA_VERSION = "omega.execution_foldback.v1"


def _mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"execution foldback {field_name} must be an object")
    return dict(value)


@dataclass(frozen=True)
class ExecutionFoldback:
    """One immutable lineage from the initiating request through factual execution."""

    episode_id: str
    request: Mapping[str, Any]
    motor: Mapping[str, Any]
    dispatch: Mapping[str, Any]
    execution_evidence: Mapping[str, Any]
    verdict: Mapping[str, Any]
    verification: Mapping[str, Any] | None = None
    schema_version: str = EXECUTION_FOLDBACK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        episode_id = self.episode_id.strip()
        if not episode_id:
            raise ValueError("execution foldback episode_id is required")
        if self.schema_version != EXECUTION_FOLDBACK_SCHEMA_VERSION:
            raise ValueError("unsupported execution foldback schema_version")

        request = _mapping(self.request, field_name="request")
        motor = _mapping(self.motor, field_name="motor")
        dispatch = _mapping(self.dispatch, field_name="dispatch")
        verdict = _mapping(self.verdict, field_name="verdict")
        verification = (
            _mapping(self.verification, field_name="verification")
            if self.verification is not None
            else None
        )
        evidence = ExecutionEvidence.from_dict(
            _mapping(self.execution_evidence, field_name="execution_evidence")
        )

        for key in ("origin", "text", "objective"):
            if not str(request.get(key) or "").strip():
                raise ValueError(f"execution foldback request.{key} is required")
        for key in ("program_id", "outcome_id"):
            if not str(motor.get(key) or "").strip():
                raise ValueError(f"execution foldback motor.{key} is required")
        if not str(dispatch.get("dispatch_id") or "").strip():
            raise ValueError("execution foldback dispatch.dispatch_id is required")
        if str(dispatch.get("episode_id") or "") != episode_id:
            raise ValueError("execution foldback dispatch episode_id must match")
        if evidence.episode_id != episode_id:
            raise ValueError("execution foldback evidence episode_id must match")
        if evidence.program_id != str(motor.get("program_id") or ""):
            raise ValueError("execution foldback evidence program_id must match")

        satisfied = bool(verdict.get("satisfied"))
        more_work = bool(verdict.get("more_work_needed"))
        blockers = tuple(str(row) for row in (verdict.get("blockers") or ()) if str(row))
        if satisfied and more_work:
            raise ValueError("a satisfied execution foldback cannot require more work")
        if satisfied and evidence.status != "success":
            raise ValueError("only successful factual execution evidence can satisfy a request")
        if satisfied and blockers:
            raise ValueError("a satisfied execution foldback cannot retain blockers")
        if satisfied and verification is not None and not bool(verification.get("all_green")):
            raise ValueError("a red deterministic verification cannot satisfy a request")

        request["origin"] = str(request["origin"])
        request["text"] = str(request["text"])
        request["objective"] = str(request["objective"])
        motor["program_id"] = str(motor["program_id"])
        motor["outcome_id"] = str(motor["outcome_id"])
        motor["acceptance_criteria"] = list(motor.get("acceptance_criteria") or ())
        dispatch["episode_id"] = episode_id
        dispatch["dispatch_id"] = str(dispatch["dispatch_id"])
        verdict["satisfied"] = satisfied
        verdict["more_work_needed"] = more_work
        verdict["blockers"] = list(blockers)

        object.__setattr__(self, "episode_id", episode_id)
        object.__setattr__(self, "request", _freeze(request))
        object.__setattr__(self, "motor", _freeze(motor))
        object.__setattr__(self, "dispatch", _freeze(dispatch))
        object.__setattr__(self, "execution_evidence", _freeze(evidence.to_dict()))
        object.__setattr__(self, "verdict", _freeze(verdict))
        object.__setattr__(
            self,
            "verification",
            _freeze(verification) if verification is not None else None,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionFoldback":
        return cls(
            episode_id=str(value.get("episode_id") or ""),
            request=_mapping(value.get("request"), field_name="request"),
            motor=_mapping(value.get("motor"), field_name="motor"),
            dispatch=_mapping(value.get("dispatch"), field_name="dispatch"),
            execution_evidence=_mapping(
                value.get("execution_evidence"), field_name="execution_evidence"
            ),
            verdict=_mapping(value.get("verdict"), field_name="verdict"),
            verification=(
                _mapping(value.get("verification"), field_name="verification")
                if value.get("verification") is not None
                else None
            ),
            schema_version=str(value.get("schema_version") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "episode_id": self.episode_id,
            "request": _thaw(self.request),
            "motor": _thaw(self.motor),
            "dispatch": _thaw(self.dispatch),
            "execution_evidence": _thaw(self.execution_evidence),
            "verification": (_thaw(self.verification) if self.verification is not None else None),
            "verdict": _thaw(self.verdict),
        }

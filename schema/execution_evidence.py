"""Immutable factual evidence returned by an execution limb."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from schema.motor_program import _freeze, _thaw


_VALID_STATUSES = frozenset({"success", "partial", "yielded", "blocked", "failure"})


def _rows(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(_freeze(item) for item in value)


@dataclass(frozen=True)
class ExecutionEvidence:
    program_id: str
    episode_id: str
    status: str
    tool_calls: tuple[Any, ...] = ()
    commands_run: tuple[Any, ...] = ()
    sources_consulted: tuple[Any, ...] = ()
    files_changed: tuple[Any, ...] = ()
    artifacts: tuple[Any, ...] = ()
    tests_run: tuple[Any, ...] = ()
    verification_results: tuple[Any, ...] = ()
    blockers: tuple[Any, ...] = ()
    environmental_contradictions: tuple[Any, ...] = ()
    bounded_local_deviations: tuple[Any, ...] = ()
    progress_digest: str = ""
    failure_kind: str = ""
    failure_reason: str = ""

    def __post_init__(self) -> None:
        if not self.program_id.strip() or not self.episode_id.strip():
            raise ValueError("execution evidence identity fields are required")
        if self.status not in _VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(_VALID_STATUSES)}")
        for field_name in (
            "tool_calls",
            "commands_run",
            "sources_consulted",
            "files_changed",
            "artifacts",
            "tests_run",
            "verification_results",
            "blockers",
            "environmental_contradictions",
            "bounded_local_deviations",
        ):
            object.__setattr__(self, field_name, _rows(getattr(self, field_name)))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExecutionEvidence:
        return cls(
            program_id=str(value.get("program_id") or ""),
            episode_id=str(value.get("episode_id") or ""),
            status=str(value.get("status") or ""),
            tool_calls=_rows(value.get("tool_calls")),
            commands_run=_rows(value.get("commands_run")),
            sources_consulted=_rows(value.get("sources_consulted")),
            files_changed=_rows(value.get("files_changed")),
            artifacts=_rows(value.get("artifacts")),
            tests_run=_rows(value.get("tests_run")),
            verification_results=_rows(value.get("verification_results")),
            blockers=_rows(value.get("blockers")),
            environmental_contradictions=_rows(value.get("environmental_contradictions")),
            bounded_local_deviations=_rows(value.get("bounded_local_deviations")),
            progress_digest=str(value.get("progress_digest") or ""),
            failure_kind=str(value.get("failure_kind") or ""),
            failure_reason=str(value.get("failure_reason") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "program_id": self.program_id,
            "episode_id": self.episode_id,
            "status": self.status,
            "tool_calls": _thaw(self.tool_calls),
            "commands_run": _thaw(self.commands_run),
            "sources_consulted": _thaw(self.sources_consulted),
            "files_changed": _thaw(self.files_changed),
            "artifacts": _thaw(self.artifacts),
            "tests_run": _thaw(self.tests_run),
            "verification_results": _thaw(self.verification_results),
            "blockers": _thaw(self.blockers),
            "environmental_contradictions": _thaw(self.environmental_contradictions),
            "bounded_local_deviations": _thaw(self.bounded_local_deviations),
            "progress_digest": self.progress_digest,
            "failure_kind": self.failure_kind,
            "failure_reason": self.failure_reason,
        }

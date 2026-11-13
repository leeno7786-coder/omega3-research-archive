"""Cold-resumable builder reports carried by episode SRAM.

The builder is stateless.  This record is therefore the complete boundary object
between execution and Omega's judgement: the same object is readable during the
episode and is persisted to the dev lobe by DeepSleep with every other SRAM item.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from typing import Any, Mapping, Sequence

from memory.api import CanonicalEnvelope
from memory import episode_sram
from memory.run_anchor import RUN_PROVENANCE_KEY, validate_run_provenance


def _text(value: Any, *, limit: int = 4000) -> str:
    return str(value or "").strip()[:limit]


def _text_tuple(values: Sequence[Any] | None, *, limit: int = 2000) -> tuple[str, ...]:
    return tuple(_text(value, limit=limit) for value in (values or ()) if _text(value, limit=limit))


@dataclass(frozen=True)
class ExecutionReport:
    """Everything a fresh builder and Omega need to judge or resume one run."""

    episode_id: str
    build_task: str
    pass_gate: tuple[str, ...]
    did: tuple[str, ...]
    did_not: tuple[str, ...]
    succeeded: tuple[str, ...]
    failed: tuple[str, ...]
    summary: str = ""
    progress_digest: str = ""
    files_changed: tuple[str, ...] = ()
    verification: Mapping[str, Any] | None = None
    original_request: str = ""
    origin_opened_by: str = ""
    rounds_used: int = 0
    omega_reason: str = ""
    omega_intent: str = ""
    recent_messages: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    # WO-05 carry-over: the receipts ``files_changed`` and ``verification`` cannot carry —
    # each artifact revision with its content digest, and each verification bound to the
    # criterion and the exact bytes it checked — read from this episode's recorded events.
    artifact_revisions: tuple[Mapping[str, Any], ...] = ()
    verification_receipts: tuple[Mapping[str, Any], ...] = ()
    # The Coding Agent run this report belongs to (memory.run_anchor), so run-scoped recall
    # can select it. None for a report no job produced; a malformed value is rejected.
    run_provenance: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.run_provenance is not None:
            object.__setattr__(self, "run_provenance", validate_run_provenance(self.run_provenance))
        object.__setattr__(self, "episode_id", _text(self.episode_id, limit=200))
        object.__setattr__(self, "build_task", _text(self.build_task))
        object.__setattr__(self, "pass_gate", _text_tuple(self.pass_gate))
        object.__setattr__(self, "did", _text_tuple(self.did))
        object.__setattr__(self, "did_not", _text_tuple(self.did_not))
        object.__setattr__(self, "succeeded", _text_tuple(self.succeeded))
        object.__setattr__(self, "failed", _text_tuple(self.failed))
        object.__setattr__(self, "summary", _text(self.summary))
        object.__setattr__(self, "progress_digest", _text(self.progress_digest, limit=8000))
        object.__setattr__(self, "files_changed", _text_tuple(self.files_changed))
        object.__setattr__(self, "original_request", _text(self.original_request))
        object.__setattr__(self, "origin_opened_by", _text(self.origin_opened_by, limit=200))
        object.__setattr__(self, "rounds_used", max(0, int(self.rounds_used or 0)))
        object.__setattr__(self, "omega_reason", _text(self.omega_reason))
        object.__setattr__(self, "omega_intent", _text(self.omega_intent, limit=200))
        object.__setattr__(self, "recent_messages", _text_tuple(self.recent_messages, limit=600))
        object.__setattr__(self, "verification", deepcopy(dict(self.verification or {})))
        object.__setattr__(self, "metadata", deepcopy(dict(self.metadata or {})))
        object.__setattr__(self, "artifact_revisions",
                           tuple(deepcopy(dict(row)) for row in self.artifact_revisions or ()))
        object.__setattr__(self, "verification_receipts",
                           tuple(deepcopy(dict(row)) for row in self.verification_receipts or ()))
        if not self.episode_id:
            raise ValueError("execution report episode_id is required")
        if not self.build_task:
            raise ValueError("execution report build_task is required")
        if not self.pass_gate:
            raise ValueError("execution report pass_gate is required")

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "episode_id": self.episode_id,
            "build_task": self.build_task,
            "pass_gate": list(self.pass_gate),
            "did": list(self.did),
            "did_not": list(self.did_not),
            "succeeded": list(self.succeeded),
            "failed": list(self.failed),
            "summary": self.summary,
            "progress_digest": self.progress_digest,
            "files_changed": list(self.files_changed),
            "verification": deepcopy(dict(self.verification or {})),
            "original_request": self.original_request,
            "origin_opened_by": self.origin_opened_by,
            "rounds_used": self.rounds_used,
            "omega_reason": self.omega_reason,
            "omega_intent": self.omega_intent,
            "recent_messages": list(self.recent_messages),
            "metadata": deepcopy(dict(self.metadata or {})),
            "artifact_revisions": [deepcopy(dict(row)) for row in self.artifact_revisions],
            "verification_receipts": [deepcopy(dict(row)) for row in self.verification_receipts],
        }
        if self.run_provenance is not None:
            payload[RUN_PROVENANCE_KEY] = dict(self.run_provenance)
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ExecutionReport":
        return cls(
            episode_id=payload.get("episode_id", ""),
            build_task=payload.get("build_task", ""),
            pass_gate=tuple(payload.get("pass_gate") or ()),
            did=tuple(payload.get("did") or ()),
            did_not=tuple(payload.get("did_not") or ()),
            succeeded=tuple(payload.get("succeeded") or ()),
            failed=tuple(payload.get("failed") or ()),
            summary=payload.get("summary", ""),
            progress_digest=payload.get("progress_digest", ""),
            files_changed=tuple(payload.get("files_changed") or ()),
            verification=payload.get("verification") if isinstance(payload.get("verification"), Mapping) else {},
            original_request=payload.get("original_request", ""),
            origin_opened_by=payload.get("origin_opened_by", ""),
            rounds_used=payload.get("rounds_used", 0),
            omega_reason=payload.get("omega_reason", ""),
            omega_intent=payload.get("omega_intent", ""),
            recent_messages=tuple(payload.get("recent_messages") or ()),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {},
            artifact_revisions=tuple(
                row for row in payload.get("artifact_revisions") or () if isinstance(row, Mapping)),
            verification_receipts=tuple(
                row for row in payload.get("verification_receipts") or ()
                if isinstance(row, Mapping)),
            run_provenance=payload.get("run_provenance"),
        )


def hold_execution_report(report: ExecutionReport) -> str:
    """Put a complete report in the current episode bucket; never touch disk here."""
    if not isinstance(report, ExecutionReport):
        raise TypeError("report must be an ExecutionReport")
    report_payload = report.to_payload()
    envelope = CanonicalEnvelope(
        cycle_id=report.episode_id,
        coord=[11, 1, 0],
        type="execution_report",
        payload={
            **report_payload,
            "title": f"Execution report: {report.build_task[:160]}",
            "summary": report.summary or f"Execution report for {report.build_task}",
            "text": json.dumps(report_payload, ensure_ascii=False, sort_keys=True),
            "tags": ["dev", "execution_report"],
            "subtopic": "execution_report",
            "enrichment": {
                "episode_id": report.episode_id,
                "build_task": report.build_task,
                "pass_gate": list(report.pass_gate),
                "files_changed": list(report.files_changed),
                "verification": deepcopy(dict(report.verification or {})),
                "artifact_revisions": report_payload["artifact_revisions"],
                "verification_receipts": report_payload["verification_receipts"],
                **({RUN_PROVENANCE_KEY: dict(report.run_provenance)}
                   if report.run_provenance is not None else {}),
            },
        },
        source="memory.execution_report",
        confidence=1.0,
        provenance=["builder", "episode_sram"],
        decay_policy="none",
        permissions={"read": True, "write": True},
        strength_index=1.0,
    )
    return episode_sram.hold_memory(
        envelope,
        propose=False,
        bypass_guardian=True,
        skip_embed=False,
    )


def current_execution_report() -> ExecutionReport | None:
    """Return the newest report in this episode, without reading filesystem recall."""
    for item in reversed(episode_sram.pending()):
        envelope = getattr(item, "envelope", None)
        if item.kind != "memory" or getattr(envelope, "type", None) != "execution_report":
            continue
        payload = getattr(envelope, "payload", None)
        if not isinstance(payload, Mapping):
            raise RuntimeError("execution_report SRAM envelope has no payload mapping")
        return ExecutionReport.from_payload(payload)
    return None

"""Deterministically stop executor-evidenced revolving motor pathways."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from schema.execution_evidence import ExecutionEvidence
from schema.motor_program import _freeze, _thaw


# The episode.meta key holding the attempt history INCLUDING the attempt this
# episode just closed (Phase 4 cut 9 review). ``prior_motor_attempts`` is the
# planner's view — the attempts that existed BEFORE this episode ran — and the
# journal's RPE expectation reads it as exactly that, so the closed attempt
# cannot be appended there. But a lane hop needs the post-close list: an agent
# continuation carries the history forward BY VALUE, and carrying the pre-close
# list means every hop re-arrives with the same history the previous episode
# planned against, so the equivalent/distinct limits never advance across a
# chain. One key, written at the close, read only by the thing that hops.
ATTEMPTS_THROUGH_CLOSE_KEY = "motor_attempts_through_close"

_TRANSIENT_FAILURES = frozenset(
    {"timeout", "bridge_unavailable", "runtime_unavailable", "rate_limited"}
)
_SAFETY_FAILURES = frozenset({"safety_hold", "policy_hold", "permission_hold", "guardian_hold"})
_EXECUTION_STATUSES = frozenset({"success", "partial", "yielded", "failure"})
_FACTUAL_PROVENANCE_SOURCES = frozenset(
    {
        "qwen_tool_call",
        "direct_tool",
        "bridge_ledger",
        "workspace_diff",
        "independent_workspace_diff",
    }
)


def _value(container: Any, name: str, default: Any = None) -> Any:
    if isinstance(container, Mapping):
        return container.get(name, default)
    return getattr(container, name, default)


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _normalized_ordered(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    values = (value,) if isinstance(value, str) else value
    return tuple(_normalized_text(item) for item in values if _normalized_text(item))


def material_pathway_signature(program: Any) -> dict[str, Any]:
    """Return operational pathway facts, excluding cosmetic lineage labels."""
    execution = _value(program, "execution_plan", {})
    outcome = _value(program, "outcome_contract", {})
    evidence = _value(program, "evidence_contract", {})
    evidence_dict = evidence.to_dict() if hasattr(evidence, "to_dict") else dict(evidence)
    return {
        "capability_class": _normalized_text(_value(execution, "capability_class", "")),
        "required_capabilities": _normalized_ordered(
            _value(execution, "required_capabilities", ())
        ),
        "operations": _normalized_ordered(_value(execution, "operations", ())),
        "permitted_local_discretion": _normalized_ordered(
            _value(execution, "permitted_local_discretion", ())
        ),
        "prohibited_deviations": _normalized_ordered(
            _value(execution, "prohibited_deviations", ())
        ),
        "return_to_mind_conditions": _normalized_ordered(
            _value(execution, "return_to_mind_conditions", ())
        ),
        "outcome_constraints": _normalized_ordered(_value(outcome, "constraints", ())),
        "evidence_contract": {
            key: _normalized_ordered(evidence_dict.get(key))
            for key in (
                "required_artifacts",
                "required_source_proof",
                "required_checks",
                "completion_signals",
                "disqualifying_failures",
            )
        },
    }


def material_pathway_fingerprint(program: Any) -> str:
    raw = json.dumps(
        material_pathway_signature(program),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class LoopDecision:
    """Immutable, factual decision about whether a pathway may continue."""

    action: str
    failed_fingerprint: str = ""
    failed_material_fingerprint: str = ""
    equivalent_failures: int = 0
    distinct_failures: int = 0
    capability_exception_used: bool = False
    requires_material_change: bool = False
    outstanding_required_capabilities: tuple[str, ...] = ()
    attempted_methods: tuple[str, ...] = ()
    unresolved_contradiction: str = ""
    execution_evidence: Any = None

    def __post_init__(self) -> None:
        if self.action not in {"allow", "invalidate", "ask_operator"}:
            raise ValueError("invalid motor loop action")
        object.__setattr__(self, "attempted_methods", tuple(self.attempted_methods))
        object.__setattr__(
            self,
            "outstanding_required_capabilities",
            tuple(self.outstanding_required_capabilities),
        )
        object.__setattr__(
            self,
            "execution_evidence",
            _freeze(self.execution_evidence or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "failed_fingerprint": self.failed_fingerprint,
            "failed_material_fingerprint": self.failed_material_fingerprint,
            "equivalent_failures": self.equivalent_failures,
            "distinct_failures": self.distinct_failures,
            "capability_exception_used": self.capability_exception_used,
            "requires_material_change": self.requires_material_change,
            "outstanding_required_capabilities": list(self.outstanding_required_capabilities),
            "attempted_methods": list(self.attempted_methods),
            "unresolved_contradiction": self.unresolved_contradiction,
            "execution_evidence": _thaw(self.execution_evidence),
        }


def _failure_kind(value: Any) -> str:
    return str(value or "").strip().lower()


def _is_non_pathway_failure(kind: str) -> bool:
    return kind in _TRANSIENT_FAILURES or kind in _SAFETY_FAILURES or kind.startswith("safety_")


def _verification_proves_progress(value: Any) -> bool:
    """Admit only a positive check/delta tied to executor-owned provenance.

    Prose summaries and legacy string rows are model reports, not observations.  A
    structured row must say ``passed`` and identify the real tool/ledger/diff fact
    from which it was derived.  ``factual_delta`` remains available for non-test
    progress, but it is held to the same provenance boundary.
    """
    if not isinstance(value, Mapping):
        return False
    if _failure_kind(value.get("status")) not in {"passed", "success", "verified"}:
        return False
    provenance = value.get("executor_provenance")
    if not isinstance(provenance, Mapping):
        return False
    source = _failure_kind(provenance.get("source"))
    if source not in _FACTUAL_PROVENANCE_SOURCES:
        return False
    has_identity = bool(
        str(provenance.get("tool_call_id") or "").strip()
        or str(provenance.get("result_digest") or "").strip()
        or str(provenance.get("diff_digest") or "").strip()
        or str(provenance.get("ledger_digest") or "").strip()
    )
    if not has_identity:
        return False
    factual_delta = value.get("factual_delta")
    if factual_delta is not None:
        if isinstance(factual_delta, Mapping):
            return any(bool(delta) for delta in factual_delta.values())
        return bool(factual_delta)
    return bool(str(value.get("requested") or value.get("check") or "").strip())


def _current_has_progress(evidence: ExecutionEvidence) -> bool:
    return any(_verification_proves_progress(row) for row in evidence.verification_results)


def _effective_failure_kind(evidence: ExecutionEvidence) -> str:
    explicit = _failure_kind(evidence.failure_kind)
    if explicit:
        return explicit
    if any(
        isinstance(row, Mapping)
        and _failure_kind(row.get("status")) in {"failed", "failure", "rejected"}
        for row in evidence.verification_results
    ):
        return "verification_failure"
    return "irrelevant_evidence"


def _normalized_failure_evidence(evidence: ExecutionEvidence) -> ExecutionEvidence:
    if evidence.failure_kind or _current_has_progress(evidence):
        return evidence
    payload = evidence.to_dict()
    payload["failure_kind"] = _effective_failure_kind(evidence)
    if not str(payload.get("failure_reason") or "").strip():
        payload["failure_reason"] = payload["failure_kind"]
    return ExecutionEvidence.from_dict(payload)


def _has_safety_hold(evidence: ExecutionEvidence) -> bool:
    markers = ("safety hold", "policy hold", "permission hold", "guardian hold")
    return any(
        any(marker in _normalized_text(blocker) for marker in markers)
        for blocker in evidence.blockers
    )


def _counts_as_pathway_failure(evidence: ExecutionEvidence) -> bool:
    """Admit only completed executor statuses with no factual progress."""
    return (
        isinstance(evidence, ExecutionEvidence)
        and evidence.status in _EXECUTION_STATUSES
        and not _current_has_progress(evidence)
        and not _has_safety_hold(evidence)
        and not _is_non_pathway_failure(_effective_failure_kind(evidence))
    )


def _attempt_evidence(row: Mapping[str, Any]) -> ExecutionEvidence | None:
    raw = row.get("execution_evidence")
    if not isinstance(raw, Mapping):
        return None
    try:
        evidence = _normalized_failure_evidence(ExecutionEvidence.from_dict(raw))
    except (TypeError, ValueError):
        return None
    program_id = str(row.get("program_id") or "").strip()
    if program_id and evidence.program_id != program_id:
        return None
    if not _counts_as_pathway_failure(evidence):
        return None
    return evidence


def _attempt_counts(row: Mapping[str, Any]) -> bool:
    return bool(
        str(row.get("program_fingerprint") or "").strip() and _attempt_evidence(row) is not None
    )


def _program_fingerprint(program: Any) -> str:
    fingerprint = getattr(program, "fingerprint", None)
    return str(fingerprint() if callable(fingerprint) else fingerprint or "").strip()


def _program_method(program: Any, fingerprint: str) -> str:
    pathway = getattr(program, "pathway", None)
    method = getattr(pathway, "method", None)
    if method is None and isinstance(pathway, Mapping):
        method = pathway.get("method")
    return str(method or fingerprint).strip()


def _program_capabilities(program: Any) -> frozenset[str]:
    values: list[Any] = []
    required = getattr(program, "required_capabilities", None)
    if required:
        values.extend(required if not isinstance(required, str) else (required,))
    execution_plan = getattr(program, "execution_plan", None)
    capability = getattr(execution_plan, "capability_class", None)
    if capability is None and isinstance(execution_plan, Mapping):
        capability = execution_plan.get("capability_class")
    if capability:
        values.append(capability)
    required = _value(execution_plan, "required_capabilities", ())
    if required:
        values.extend(required if not isinstance(required, str) else (required,))
    return frozenset(str(value).strip() for value in values if str(value).strip())


def _attempt_capabilities(row: Mapping[str, Any]) -> frozenset[str]:
    raw = row.get("required_capabilities") or row.get("capability_set") or ()
    if isinstance(raw, str):
        raw = (raw,)
    return frozenset(str(value).strip() for value in raw if str(value).strip())


def evaluate_motor_loop(
    current: Any,
    evidence: ExecutionEvidence,
    prior_attempts: Sequence[Mapping[str, Any]],
    equivalent_limit: int = 2,
    distinct_limit: int = 3,
    *,
    proposed_required_capabilities: Sequence[str] = (),
) -> LoopDecision:
    """Classify one factual attempt without inference or mutable loop state."""
    fingerprint = _program_fingerprint(current)
    material_fingerprint = material_pathway_fingerprint(current)
    if not fingerprint:
        raise ValueError("current motor program fingerprint is required")
    if not isinstance(evidence, ExecutionEvidence):
        raise TypeError("evidence must be canonical ExecutionEvidence")
    evidence = _normalized_failure_evidence(evidence)

    valid_prior = [dict(row) for row in prior_attempts if _attempt_counts(row)]
    if not _counts_as_pathway_failure(evidence):
        return LoopDecision(action="allow", execution_evidence=evidence.to_dict())

    material_fingerprints = [
        str(row.get("material_fingerprint") or row["program_fingerprint"]).strip()
        for row in valid_prior
    ]
    all_material_fingerprints = [*material_fingerprints, material_fingerprint]
    equivalent = sum(item == material_fingerprint for item in all_material_fingerprints)
    distinct = len(set(all_material_fingerprints))
    methods = tuple(
        [str(row.get("method") or row["program_fingerprint"]).strip() for row in valid_prior]
        + [_program_method(current, fingerprint)]
    )
    contradiction = str(
        evidence.failure_reason
        or evidence.failure_kind
        or (
            evidence.environmental_contradictions[0]
            if evidence.environmental_contradictions
            else ""
        )
    ).strip()

    attempted_capabilities = (
        frozenset().union(*(_attempt_capabilities(row) for row in valid_prior))
        if valid_prior
        else frozenset()
    )
    attempted_capabilities |= _program_capabilities(current)
    proposed_capabilities = frozenset(
        str(value).strip() for value in proposed_required_capabilities if str(value).strip()
    )
    outstanding_capabilities = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in proposed_required_capabilities
            if str(value).strip() and str(value).strip() not in attempted_capabilities
        )
    )
    exception_already_used = any(bool(row.get("capability_exception_used")) for row in valid_prior)
    new_capability_exception = (
        distinct >= max(1, int(distinct_limit))
        and not exception_already_used
        and bool(proposed_capabilities - attempted_capabilities)
    )
    if distinct >= max(1, int(distinct_limit)) and not new_capability_exception:
        action = "ask_operator"
    elif equivalent >= max(1, int(equivalent_limit)):
        action = "invalidate"
    else:
        action = "allow"

    return LoopDecision(
        action=action,
        failed_fingerprint=(fingerprint if action != "allow" or new_capability_exception else ""),
        failed_material_fingerprint=(
            material_fingerprint if action != "allow" or new_capability_exception else ""
        ),
        equivalent_failures=equivalent,
        distinct_failures=distinct,
        capability_exception_used=new_capability_exception,
        requires_material_change=new_capability_exception,
        outstanding_required_capabilities=(
            outstanding_capabilities if new_capability_exception else ()
        ),
        attempted_methods=methods,
        unresolved_contradiction=contradiction,
        execution_evidence=evidence.to_dict(),
    )

"""Immutable contracts for Omega-authored motor programs."""

from __future__ import annotations

from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Mapping


class MotorPlanningHalt(RuntimeError):
    """Motor planning exhausted its bounded attempts without a valid program.

    A failed PLAN is a cognitive outcome, not substrate poison (operator directive
    2026-07-11): the cascade catches this, stamps a failed AUTO-shaped outcome so
    Meta B -> Global B -> Omega fold over the failure like any other result, and
    the organism lives. Anything else raised from planning still fails fast.
    """

    def __init__(
        self,
        message: str,
        *,
        episode_id: str = "",
        attempts: int = 0,
        failure_reason: str = "",
        rejected_attempts: list[dict] | tuple[dict, ...] | None = None,
    ) -> None:
        super().__init__(message)
        self.episode_id = episode_id
        self.attempts = attempts
        self.failure_reason = failure_reason
        self.rejected_attempts = tuple(
            dict(row) for row in (rejected_attempts or ()) if isinstance(row, dict)
        )


_CODE_CAPABILITY_TOKENS = frozenset(
    {
        "build",
        "building",
        "code",
        "coding",
        "development",
        "implementation",
        "patching",
        "python",
        "testing",
    }
)
_CODE_CAPABILITY_PHRASES = frozenset(
    {
        "code execution",
        "file editing",
        "software development",
        "software engineering",
    }
)
_CODE_OPERATION_TARGETS = frozenset(
    {
        "application",
        "code",
        "file",
        "implementation",
        "module",
        "package",
        "project",
        "script",
        "software",
        "source",
        "test",
    }
)
_SOURCE_PROOF_TOKENS = frozenset(
    {
        "article",
        "citation",
        "document",
        "doi",
        "ledger",
        "paper",
        "provenance",
        "record",
        "reference",
        "source",
        "uri",
        "url",
        "webpage",
        "website",
    }
)
_EXECUTOR_CHECK_TOKENS = frozenset(
    {
        "audit",
        "bandit",
        "black",
        "build",
        "check",
        "checks",
        "compile",
        "fetch",
        "flake8",
        "isort",
        "lint",
        "mypy",
        "plan",
        "pylint",
        "pytest",
        "ruff",
        "scan",
        "summary",
        "test",
        "tests",
        "typecheck",
        "unittest",
        "validate",
        "validation",
    }
)
_DIRECT_EXECUTOR_CHECKS = frozenset(
    {
        "artifact_present",
        "artifacts_present",
        "content_present",
        "document_present",
        "fetch_verified",
        "plan_present",
        "results_present",
        "source_backed",
        "source_present",
        "structured_content",
        "summary_verified",
        "url_present",
    }
)

# Single source of truth for the executor-addressable check vocabulary. The model
# is structurally constrained (response_format json_schema enum) to ONLY emit these
# exact strings for required_checks — it can no longer coin names like
# 'plan_completeness_review' or 'skill_signature_validation' (operator 2026-07-16:
# the MTP drafter + a soft prompt were both bypassed under load). Keep this in lock
# step with _EXECUTOR_CHECK_TOKENS / _DIRECT_EXECUTOR_CHECKS below so the validator
# and the sampler enum never disagree.
_REQUIRED_CHECK_ENUM: tuple[str, ...] = (
    "pytest",
    "ruff",
    "flake8",
    "mypy",
    "pylint",
    "isort",
    "bandit",
    "black",
    "typecheck",
    "compile",
    "build",
    "test",
    "audit",
    "validate",
    "scan",
    "check",
    "gradient check",
    "unit test",
    "fetch",
    "plan",
    "summary",
    "fetch_verified",
    "document_present",
    "plan_present",
    "summary_verified",
)


def motor_program_response_schema() -> dict[str, Any]:
    """Strict JSON schema the motor-planning model is constrained to emit.

    The chat worker honors response_format.json_schema with enum items, so the
    sampler masks any token that would leave the vocabulary — required_checks can
    only contain the allowed executor check names. Free-text / nested-object fields
    stay open so the mind still authors the plan; the pins are the contract-critical
    enum PLUS the required-field / minItems invariants that mirror the dataclass
    __post_init__ checks, so a grammar-valid object never fails dataclass validation.
    Mirrors the four model fields in MotorProgram._MODEL_FIELDS."""
    return {
        "type": "object",
        "properties": {
            "outcome_contract": {
                "type": "object",
                "properties": {
                    "objective": {"type": "string", "minLength": 1},
                    "acceptance_criteria": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                    },
                    "constraints": {"type": "array", "items": {"type": "string"}},
                    "priorities": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["objective", "acceptance_criteria"],
            },
            "pathway": {
                "type": "object",
                "properties": {
                    "pathway_id": {"type": "string", "minLength": 1},
                    "revision": {"type": "integer", "minimum": 1},
                    "name": {"type": "string", "minLength": 1},
                    "method": {"type": "string", "minLength": 1},
                    "operational_rationale": {"type": "string", "minLength": 1},
                },
                "required": ["pathway_id", "revision", "name", "method", "operational_rationale"],
            },
            "execution_plan": {
                "type": "object",
                "properties": {
                    "capability_class": {"type": "string", "minLength": 1},
                    "required_capabilities": {"type": "array", "items": {"type": "string"}},
                    "operations": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                    },
                },
                "required": ["capability_class", "operations"],
            },
            "evidence_contract": {
                "type": "object",
                "properties": {
                    "required_artifacts": {"type": "array", "items": {"type": "string"}},
                    "required_source_proof": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1, "pattern": "^(?!\\[\\]$).+$"},
                    },
                    "required_checks": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(_REQUIRED_CHECK_ENUM)},
                        "minItems": 1,
                    },
                    "completion_signals": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                    },
                    "disqualifying_failures": {"type": "array", "items": {"type": "string"}},
                },
                # Aligned with EvidenceContract.__post_init__ (operator 2026-07-21):
                # the dataclass requires non-empty completion_signals AND at least
                # one evidence source. required_checks minItems=1 guarantees the
                # latter; completion_signals in `required` guarantees the former.
                # Without both, a grammar-valid object could still fail dataclass
                # validation and only the repair loop could recover it.
                "required": ["required_checks", "completion_signals"],
            },
        },
        "required": [
            "outcome_contract",
            "pathway",
            "execution_plan",
            "evidence_contract",
        ],
    }


def _normalized_terms(value: Any) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _is_code_capability(value: Any) -> bool:
    terms = _normalized_terms(value)
    phrase = " ".join(terms)
    if phrase == "research and development":
        return False
    return bool(
        set(terms) & _CODE_CAPABILITY_TOKENS
        or any(marker in phrase for marker in _CODE_CAPABILITY_PHRASES)
    )


def _is_code_operation(value: Any) -> bool:
    terms = set(_normalized_terms(value))
    if terms & {"compile", "implement", "patch", "pytest", "refactor"}:
        return True
    return any(
        action in terms and bool((terms - {action}) & _CODE_OPERATION_TARGETS)
        for action in ("build", "edit", "test", "write")
    )


def _is_source_proof_requirement(value: Any) -> bool:
    raw = str(value or "").strip()
    if re.match(r"^https?://\S+$", raw, re.IGNORECASE):
        return True
    if re.match(r"^10\.\d{4,9}/\S+$", raw, re.IGNORECASE):
        return True
    if re.match(r"^(?:src|source)[_:.-][a-z0-9][a-z0-9_.:-]*$", raw, re.IGNORECASE):
        return True
    return bool(set(_normalized_terms(value)) & _SOURCE_PROOF_TOKENS)


def _is_executor_addressable_check(value: Any) -> bool:
    terms = _normalized_terms(value)
    return "_".join(terms) in _DIRECT_EXECUTOR_CHECKS or bool(set(terms) & _EXECUTOR_CHECK_TOKENS)


def _is_matchable_code_artifact(value: Any) -> bool:
    raw = str(value or "").strip()
    terms = set(_normalized_terms(value))
    phrase = " ".join(_normalized_terms(value))
    path_like = bool(
        "/" in raw
        or "\\" in raw
        or re.search(r"(?:^|\s)[^\s/\\]+\.[a-z0-9]{1,10}(?:$|\s)", raw.lower())
    )
    execution_receipt_terms = {
        "exit",
        "log",
        "logs",
        "output",
        "receipt",
        "receipts",
        "report",
        "reports",
        "result",
        "results",
        "stdout",
    }
    # The bridge's factual ledger records touched files and declared artifacts,
    # not directory-creation claims or model-selected alternatives.  Those facts
    # belong in required_checks and completion_signals respectively.
    return (
        "directory" not in terms
        and "or equivalent" not in phrase
        and (path_like or not terms.intersection(execution_receipt_terms))
    )


def evidence_lane(requirement: Any) -> str:
    """The contract lane a declared evidence requirement belongs in: check, source_proof or artifact."""
    if _is_executor_addressable_check(requirement):
        return "check"
    if _is_source_proof_requirement(requirement):
        return "source_proof"
    return "artifact"


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    values = (value,) if isinstance(value, str) else value
    return tuple(text for item in values if (text := str(item).strip()))


def _required_string_tuple(
    value: Any, field_name: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    if value is None:
        values: tuple[Any, ...] = ()
    elif isinstance(value, str):
        values = (value,)
    else:
        values = tuple(value)
    if any(not str(item).strip() for item in values):
        raise ValueError(f"{field_name} cannot contain blank entries")
    normalized = tuple(str(item).strip() for item in values)
    if not allow_empty and not normalized:
        raise ValueError(f"{field_name} must contain at least one entry")
    return normalized


def _freeze(value: Any) -> Any:
    """Recursively detach and freeze JSON-shaped data."""
    if isinstance(value, MappingABC):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    """Return a fully detached JSON-compatible copy of frozen data."""
    if isinstance(value, MappingABC):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_thaw(item) for item in value]
    return value


def _dict_tuple(value: Any) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    return tuple(_freeze(dict(item)) for item in value)


@dataclass(frozen=True)
class MotorPlanningInput:
    episode_id: str
    intent: str
    action: str
    pressure: dict[str, Any]
    execution_pack: dict[str, Any]
    recalled_context: tuple[dict[str, Any], ...]
    prior_attempts: tuple[dict[str, Any], ...]
    mastery_state: dict[str, Any]
    neuromod_state: dict[str, Any]
    outstanding_required_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.episode_id.strip():
            raise ValueError("episode_id is required")
        if not self.intent.strip():
            raise ValueError("intent is required")
        if not self.action.strip():
            raise ValueError("action is required")
        object.__setattr__(self, "pressure", _freeze(self.pressure))
        object.__setattr__(self, "execution_pack", _freeze(self.execution_pack))
        object.__setattr__(self, "recalled_context", _dict_tuple(self.recalled_context))
        object.__setattr__(self, "prior_attempts", _dict_tuple(self.prior_attempts))
        object.__setattr__(self, "mastery_state", _freeze(self.mastery_state))
        object.__setattr__(self, "neuromod_state", _freeze(self.neuromod_state))
        object.__setattr__(
            self,
            "outstanding_required_capabilities",
            _string_tuple(self.outstanding_required_capabilities),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MotorPlanningInput:
        return cls(
            episode_id=str(value.get("episode_id") or ""),
            intent=str(value.get("intent") or ""),
            action=str(value.get("action") or ""),
            pressure=dict(value.get("pressure") or {}),
            execution_pack=dict(value.get("execution_pack") or {}),
            recalled_context=_dict_tuple(value.get("recalled_context")),
            prior_attempts=_dict_tuple(value.get("prior_attempts")),
            mastery_state=dict(value.get("mastery_state") or {}),
            neuromod_state=dict(value.get("neuromod_state") or {}),
            outstanding_required_capabilities=_string_tuple(
                value.get("outstanding_required_capabilities")
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "intent": self.intent,
            "action": self.action,
            "pressure": _thaw(self.pressure),
            "execution_pack": _thaw(self.execution_pack),
            "recalled_context": _thaw(self.recalled_context),
            "prior_attempts": _thaw(self.prior_attempts),
            "mastery_state": _thaw(self.mastery_state),
            "neuromod_state": _thaw(self.neuromod_state),
            "outstanding_required_capabilities": list(self.outstanding_required_capabilities),
        }


@dataclass(frozen=True)
class OutcomeContract:
    objective: str
    acceptance_criteria: tuple[str, ...]
    constraints: tuple[str, ...] = ()
    priorities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "acceptance_criteria",
            _required_string_tuple(self.acceptance_criteria, "acceptance_criteria"),
        )
        object.__setattr__(self, "constraints", _string_tuple(self.constraints))
        object.__setattr__(self, "priorities", _string_tuple(self.priorities))
        if not self.objective.strip():
            raise ValueError("objective is required")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> OutcomeContract:
        return cls(
            objective=str(value.get("objective") or ""),
            acceptance_criteria=value.get("acceptance_criteria") or (),
            constraints=_string_tuple(value.get("constraints")),
            priorities=_string_tuple(value.get("priorities")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "acceptance_criteria": list(self.acceptance_criteria),
            "constraints": list(self.constraints),
            "priorities": list(self.priorities),
        }


@dataclass(frozen=True)
class PathwaySpec:
    pathway_id: str
    revision: int
    name: str
    method: str
    operational_rationale: str
    failed_predecessor_id: str = ""
    change_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "change_evidence", _string_tuple(self.change_evidence))
        if not all((self.pathway_id.strip(), self.name.strip(), self.method.strip())):
            raise ValueError("pathway identity and method fields are required")
        if not self.operational_rationale.strip():
            raise ValueError("operational_rationale is required")
        if self.revision < 1:
            raise ValueError("revision must be >= 1")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PathwaySpec:
        return cls(
            pathway_id=str(value.get("pathway_id") or ""),
            revision=int(value.get("revision") or 0),
            name=str(value.get("name") or ""),
            method=str(value.get("method") or ""),
            operational_rationale=str(value.get("operational_rationale") or ""),
            failed_predecessor_id=str(value.get("failed_predecessor_id") or ""),
            change_evidence=_string_tuple(value.get("change_evidence")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pathway_id": self.pathway_id,
            "revision": self.revision,
            "name": self.name,
            "method": self.method,
            "operational_rationale": self.operational_rationale,
            "failed_predecessor_id": self.failed_predecessor_id,
            "change_evidence": list(self.change_evidence),
        }


@dataclass(frozen=True)
class ExecutionPlan:
    capability_class: str
    operations: tuple[str, ...]
    required_capabilities: tuple[str, ...] = ()
    permitted_local_discretion: tuple[str, ...] = ()
    prohibited_deviations: tuple[str, ...] = ()
    return_to_mind_conditions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "operations", _required_string_tuple(self.operations, "operations")
        )
        required_capabilities = _required_string_tuple(
            self.required_capabilities,
            "required_capabilities",
            allow_empty=True,
        )
        object.__setattr__(
            self,
            "required_capabilities",
            tuple(sorted(set(required_capabilities))),
        )
        object.__setattr__(
            self, "permitted_local_discretion", _string_tuple(self.permitted_local_discretion)
        )
        object.__setattr__(self, "prohibited_deviations", _string_tuple(self.prohibited_deviations))
        object.__setattr__(
            self, "return_to_mind_conditions", _string_tuple(self.return_to_mind_conditions)
        )
        if not self.capability_class.strip():
            raise ValueError("capability_class is required")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExecutionPlan:
        return cls(
            capability_class=str(value.get("capability_class") or ""),
            operations=value.get("operations") or (),
            required_capabilities=value.get("required_capabilities") or (),
            permitted_local_discretion=_string_tuple(value.get("permitted_local_discretion")),
            prohibited_deviations=_string_tuple(value.get("prohibited_deviations")),
            return_to_mind_conditions=_string_tuple(value.get("return_to_mind_conditions")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_class": self.capability_class,
            "operations": list(self.operations),
            "required_capabilities": list(self.required_capabilities),
            "permitted_local_discretion": list(self.permitted_local_discretion),
            "prohibited_deviations": list(self.prohibited_deviations),
            "return_to_mind_conditions": list(self.return_to_mind_conditions),
        }


@dataclass(frozen=True)
class EvidenceContract:
    required_artifacts: tuple[str, ...] = ()
    required_source_proof: tuple[str, ...] = ()
    required_checks: tuple[str, ...] = ()
    completion_signals: tuple[str, ...] = ()
    disqualifying_failures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("required_artifacts", "required_source_proof", "required_checks"):
            object.__setattr__(
                self,
                field_name,
                _required_string_tuple(getattr(self, field_name), field_name, allow_empty=True),
            )
        object.__setattr__(
            self,
            "completion_signals",
            _required_string_tuple(self.completion_signals, "completion_signals"),
        )
        object.__setattr__(
            self, "disqualifying_failures", _string_tuple(self.disqualifying_failures)
        )
        if not any((self.required_artifacts, self.required_source_proof, self.required_checks)):
            raise ValueError("evidence contract must require artifacts, source proof, or checks")
        invalid_source_proof = tuple(
            value for value in self.required_source_proof if not _is_source_proof_requirement(value)
        )
        if invalid_source_proof:
            raise ValueError(
                "required_source_proof must name external source proof, citation, "
                f"provenance, or ledger evidence: {list(invalid_source_proof)}"
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvidenceContract:
        return cls(
            required_artifacts=value.get("required_artifacts") or (),
            required_source_proof=value.get("required_source_proof") or (),
            required_checks=value.get("required_checks") or (),
            completion_signals=value.get("completion_signals") or (),
            disqualifying_failures=_string_tuple(value.get("disqualifying_failures")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_artifacts": list(self.required_artifacts),
            "required_source_proof": list(self.required_source_proof),
            "required_checks": list(self.required_checks),
            "completion_signals": list(self.completion_signals),
            "disqualifying_failures": list(self.disqualifying_failures),
        }


@dataclass(frozen=True)
class MotorProgram:
    program_id: str
    parent_program_id: str
    outcome_id: str
    episode_id: str
    attempt_number: int
    created_at: str
    outcome_contract: OutcomeContract
    pathway: PathwaySpec
    execution_plan: ExecutionPlan
    evidence_contract: EvidenceContract

    def __post_init__(self) -> None:
        nested_types = (
            ("outcome_contract", self.outcome_contract, OutcomeContract),
            ("pathway", self.pathway, PathwaySpec),
            ("execution_plan", self.execution_plan, ExecutionPlan),
            ("evidence_contract", self.evidence_contract, EvidenceContract),
        )
        for field_name, value, expected_type in nested_types:
            if not isinstance(value, expected_type):
                raise TypeError(f"{field_name} must be {expected_type.__name__}")
        if not all((self.program_id, self.outcome_id, self.episode_id, self.created_at)):
            raise ValueError("motor program identity fields are required")
        if self.attempt_number < 1:
            raise ValueError("attempt_number must be >= 1")
        capability_key = "_".join(_normalized_terms(self.execution_plan.capability_class))
        if capability_key == "web_research":
            invalid_web_checks = tuple(
                value
                for value in self.evidence_contract.required_checks
                if "_".join(_normalized_terms(value)) not in {"fetch", "fetch_verified"}
            )
            if invalid_web_checks:
                raise ValueError(
                    "web_research required_checks must use fetch or fetch_verified: "
                    f"{list(invalid_web_checks)}"
                )
        code_or_build = any(
            _is_code_capability(value)
            for value in (
                self.execution_plan.capability_class,
                *self.execution_plan.required_capabilities,
            )
        ) or any(_is_code_operation(value) for value in self.execution_plan.operations)
        if code_or_build and not self.evidence_contract.required_checks:
            raise ValueError(
                "code/build motor programs require at least one factual verification check"
            )
        unaddressable_checks = tuple(
            value
            for value in self.evidence_contract.required_checks
            if not _is_executor_addressable_check(value)
        )
        if code_or_build and unaddressable_checks:
            raise ValueError(
                "code/build required_checks must be executor-addressable check names "
                f"or commands: {list(unaddressable_checks)}"
            )
        unmatchable_artifacts = tuple(
            value
            for value in self.evidence_contract.required_artifacts
            if not _is_matchable_code_artifact(value)
        )
        if code_or_build and unmatchable_artifacts:
            raise ValueError(
                "code/build required_artifacts must be exact artifact paths, stable "
                "path substrings, or factual artifact names: "
                f"{list(unmatchable_artifacts)}"
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MotorProgram:
        return cls(
            program_id=str(value.get("program_id") or ""),
            parent_program_id=str(value.get("parent_program_id") or ""),
            outcome_id=str(value.get("outcome_id") or ""),
            episode_id=str(value.get("episode_id") or ""),
            attempt_number=int(value.get("attempt_number") or 0),
            created_at=str(value.get("created_at") or ""),
            outcome_contract=OutcomeContract.from_dict(value.get("outcome_contract") or {}),
            pathway=PathwaySpec.from_dict(value.get("pathway") or {}),
            execution_plan=ExecutionPlan.from_dict(value.get("execution_plan") or {}),
            evidence_contract=EvidenceContract.from_dict(value.get("evidence_contract") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "program_id": self.program_id,
            "parent_program_id": self.parent_program_id,
            "outcome_id": self.outcome_id,
            "episode_id": self.episode_id,
            "attempt_number": self.attempt_number,
            "created_at": self.created_at,
            "outcome_contract": self.outcome_contract.to_dict(),
            "pathway": self.pathway.to_dict(),
            "execution_plan": self.execution_plan.to_dict(),
            "evidence_contract": self.evidence_contract.to_dict(),
        }

    def fingerprint(self) -> str:
        semantic = {
            "outcome": self.outcome_contract.to_dict(),
            "pathway": self.pathway.to_dict(),
            "execution_plan": self.execution_plan.to_dict(),
            "evidence_contract": self.evidence_contract.to_dict(),
        }
        raw = json.dumps(semantic, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

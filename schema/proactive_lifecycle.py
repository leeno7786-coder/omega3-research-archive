"""Immutable contracts for Omega's durable proactive lifecycle."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
from typing import Any


THREAD_STATES = frozenset(
    {
        "asked",
        "policy_bound",
        "learning",
        "applying",
        "result_ready",
        "result_presented",
        "folded",
        "resolved",
        "continuing",
    }
)
POLICY_STATES = frozenset({"bound", "active", "satisfied", "superseded"})
MASTERY_STATES = frozenset({"passed", "weak", "blocked"})
RESULT_STATES = frozenset({"ready", "presented", "folded"})
FOLDBACK_STATES = frozenset({"ready", "folded"})
FOLDBACK_RESOLUTIONS = frozenset({"resolve", "continue", "ask_operator"})
ASK_STATES = frozenset({"pending", "presented", "answered"})


def stable_proactive_id(kind: str, *parts: str) -> str:
    """Return a replay-stable ID with unambiguous part boundaries."""
    clean_kind = str(kind or "").strip().lower()
    if not clean_kind:
        raise ValueError("kind is required")
    material = "|".join(f"{len(str(part))}:{part}" for part in parts)
    digest = hashlib.sha256(f"{clean_kind}|{material}".encode("utf-8")).hexdigest()[:20]
    return f"{clean_kind}_{digest}"


def _required(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _strings(value: Any, *, cap: int | None = None) -> tuple[str, ...]:
    if value is None:
        return ()
    values: Sequence[Any]
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, Sequence):
        values = value
    else:
        raise TypeError("expected a string sequence")
    out = tuple(str(item).strip() for item in values if str(item).strip())
    return out[:cap] if cap is not None else out


def _unit(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be a number in [0,1]") from exc
    if not 0.0 <= number <= 1.0:
        raise ValueError("value must be in [0,1]")
    return number


def _status(value: Any, allowed: frozenset[str], field_name: str) -> str:
    text = str(value or "").strip().lower()
    if text not in allowed:
        raise ValueError(f"invalid {field_name}: {text!r}")
    return text


@dataclass(frozen=True)
class ProactiveThread:
    thread_id: str
    origin_prompt_id: str
    origin_episode_id: str
    active_episode_id: str
    state: str
    created_at: str
    updated_at: str
    version: int = 1

    def __post_init__(self) -> None:
        for name in (
            "thread_id",
            "origin_prompt_id",
            "origin_episode_id",
            "created_at",
            "updated_at",
        ):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        object.__setattr__(self, "state", _status(self.state, THREAD_STATES, "state"))
        if int(self.version) < 1:
            raise ValueError("version must be >= 1")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProactiveThread:
        return cls(**{key: value.get(key) for key in cls.__dataclass_fields__})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProactiveAsk:
    prompt_id: str
    thread_id: str
    episode_id: str
    question: str
    why: str
    pressure_winner: str
    expected_response: str
    options: tuple[str, ...]
    status: str

    def __post_init__(self) -> None:
        for name in ("prompt_id", "thread_id", "episode_id", "question"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        object.__setattr__(self, "options", _strings(self.options, cap=8))
        object.__setattr__(self, "status", _status(self.status, ASK_STATES, "status"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProactiveAsk:
        return cls(
            prompt_id=value.get("prompt_id"),
            thread_id=value.get("thread_id"),
            episode_id=value.get("episode_id"),
            question=value.get("question"),
            why=str(value.get("why") or ""),
            pressure_winner=str(value.get("pressure_winner") or ""),
            expected_response=str(value.get("expected_response") or ""),
            options=_strings(value.get("options"), cap=8),
            status=value.get("status"),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["options"] = list(self.options)
        return data


@dataclass(frozen=True)
class OperatorPolicy:
    policy_id: str
    thread_id: str
    origin_prompt_id: str
    origin_episode_id: str
    answer_episode_id: str
    operator_text: str
    objective: str
    constraints: tuple[str, ...]
    learning_steps: tuple[str, ...]
    requested_demonstrations: tuple[str, ...]
    continuation_rule: str
    status: str

    def __post_init__(self) -> None:
        for name in (
            "policy_id",
            "thread_id",
            "origin_prompt_id",
            "origin_episode_id",
            "answer_episode_id",
            "operator_text",
            "objective",
        ):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        for name in ("constraints", "learning_steps", "requested_demonstrations"):
            object.__setattr__(self, name, _strings(getattr(self, name), cap=8))
        object.__setattr__(self, "status", _status(self.status, POLICY_STATES, "status"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> OperatorPolicy:
        return cls(
            policy_id=value.get("policy_id"),
            thread_id=value.get("thread_id"),
            origin_prompt_id=value.get("origin_prompt_id"),
            origin_episode_id=value.get("origin_episode_id"),
            answer_episode_id=value.get("answer_episode_id"),
            operator_text=value.get("operator_text"),
            objective=value.get("objective"),
            constraints=_strings(value.get("constraints"), cap=8),
            learning_steps=_strings(value.get("learning_steps"), cap=8),
            requested_demonstrations=_strings(value.get("requested_demonstrations"), cap=8),
            continuation_rule=str(value.get("continuation_rule") or ""),
            status=value.get("status"),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("constraints", "learning_steps", "requested_demonstrations"):
            data[key] = list(data[key])
        return data


@dataclass(frozen=True)
class ApplicationRow:
    prompt: str
    answer: str
    expected: str
    correct: bool | None
    explanation: str
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("prompt", "answer", "explanation"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if self.correct is not None and not isinstance(self.correct, bool):
            raise TypeError("correct must be bool or None")
        object.__setattr__(self, "evidence", _strings(self.evidence, cap=8))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ApplicationRow:
        return cls(
            prompt=value.get("prompt"),
            answer=value.get("answer"),
            expected=str(value.get("expected") or ""),
            correct=value.get("correct"),
            explanation=value.get("explanation"),
            evidence=_strings(value.get("evidence"), cap=8),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence"] = list(self.evidence)
        return data


@dataclass(frozen=True)
class ProactiveResult:
    result_id: str
    thread_id: str
    episode_id: str
    origin_prompt_id: str
    policy_id: str
    application_id: str
    title: str
    summary: str
    practiced: tuple[str, ...]
    learned: tuple[str, ...]
    application: tuple[ApplicationRow, ...]
    evidence_refs: tuple[str, ...]
    confidence: float
    mastery_status: str
    mastery_evidence: tuple[str, ...]
    mistakes: tuple[str, ...]
    weak_patterns: tuple[str, ...]
    next_step: str
    operator_input_needed: bool
    status: str

    def __post_init__(self) -> None:
        for name in (
            "result_id",
            "thread_id",
            "episode_id",
            "origin_prompt_id",
            "policy_id",
            "application_id",
            "title",
            "summary",
            "next_step",
        ):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        rows = tuple(
            row if isinstance(row, ApplicationRow) else ApplicationRow.from_dict(row)
            for row in self.application
        )
        mastery_status = _status(self.mastery_status, MASTERY_STATES, "mastery_status")
        if not rows and mastery_status != "blocked":
            raise ValueError("application evidence must contain at least one row")
        object.__setattr__(self, "application", rows)
        for name in (
            "practiced",
            "learned",
            "evidence_refs",
            "mastery_evidence",
            "mistakes",
            "weak_patterns",
        ):
            object.__setattr__(self, name, _strings(getattr(self, name), cap=20))
        if not self.evidence_refs:
            raise ValueError("evidence_refs must contain at least one reference")
        object.__setattr__(self, "confidence", _unit(self.confidence))
        object.__setattr__(self, "mastery_status", mastery_status)
        object.__setattr__(self, "status", _status(self.status, RESULT_STATES, "status"))
        if not isinstance(self.operator_input_needed, bool):
            raise TypeError("operator_input_needed must be bool")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProactiveResult:
        return cls(
            result_id=value.get("result_id"),
            thread_id=value.get("thread_id"),
            episode_id=value.get("episode_id"),
            origin_prompt_id=value.get("origin_prompt_id"),
            policy_id=value.get("policy_id"),
            application_id=value.get("application_id"),
            title=value.get("title"),
            summary=value.get("summary"),
            practiced=_strings(value.get("practiced"), cap=20),
            learned=_strings(value.get("learned"), cap=20),
            application=tuple(
                ApplicationRow.from_dict(row)
                for row in value.get("application") or ()
                if isinstance(row, Mapping)
            ),
            evidence_refs=_strings(value.get("evidence_refs"), cap=20),
            confidence=value.get("confidence"),
            mastery_status=value.get("mastery_status"),
            mastery_evidence=_strings(value.get("mastery_evidence"), cap=20),
            mistakes=_strings(value.get("mistakes"), cap=20),
            weak_patterns=_strings(value.get("weak_patterns"), cap=20),
            next_step=value.get("next_step"),
            operator_input_needed=value.get("operator_input_needed"),
            status=value.get("status"),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["application"] = [row.to_dict() for row in self.application]
        for key in (
            "practiced",
            "learned",
            "evidence_refs",
            "mastery_evidence",
            "mistakes",
            "weak_patterns",
        ):
            data[key] = list(data[key])
        return data


@dataclass(frozen=True)
class ProactiveFoldback:
    foldback_id: str
    thread_id: str
    episode_id: str
    origin_result_id: str
    mastery_status: str
    mastery_delta: float
    durable_rules: tuple[str, ...]
    corrected_rules: tuple[str, ...]
    unresolved_gaps: tuple[str, ...]
    next_pressure_candidates: tuple[str, ...]
    resolution: str
    status: str

    def __post_init__(self) -> None:
        for name in ("foldback_id", "thread_id", "episode_id", "origin_result_id"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        object.__setattr__(
            self, "mastery_status", _status(self.mastery_status, MASTERY_STATES, "mastery_status")
        )
        object.__setattr__(self, "mastery_delta", _unit(self.mastery_delta))
        for name in (
            "durable_rules",
            "corrected_rules",
            "unresolved_gaps",
            "next_pressure_candidates",
        ):
            object.__setattr__(self, name, _strings(getattr(self, name), cap=20))
        object.__setattr__(
            self, "resolution", _status(self.resolution, FOLDBACK_RESOLUTIONS, "resolution")
        )
        object.__setattr__(self, "status", _status(self.status, FOLDBACK_STATES, "status"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProactiveFoldback:
        return cls(
            foldback_id=value.get("foldback_id"),
            thread_id=value.get("thread_id"),
            episode_id=value.get("episode_id"),
            origin_result_id=value.get("origin_result_id"),
            mastery_status=value.get("mastery_status"),
            mastery_delta=value.get("mastery_delta"),
            durable_rules=_strings(value.get("durable_rules"), cap=20),
            corrected_rules=_strings(value.get("corrected_rules"), cap=20),
            unresolved_gaps=_strings(value.get("unresolved_gaps"), cap=20),
            next_pressure_candidates=_strings(value.get("next_pressure_candidates"), cap=20),
            resolution=value.get("resolution"),
            status=value.get("status"),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in (
            "durable_rules",
            "corrected_rules",
            "unresolved_gaps",
            "next_pressure_candidates",
        ):
            data[key] = list(data[key])
        return data


@dataclass(frozen=True)
class ProactiveLifecycleEvent:
    event_id: str
    event_type: str
    thread_id: str
    episode_id: str
    timestamp: str
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        for name in ("event_id", "event_type", "thread_id", "timestamp"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        object.__setattr__(self, "payload", dict(self.payload or {}))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProactiveLifecycleEvent:
        return cls(
            event_id=value.get("event_id"),
            event_type=value.get("event_type"),
            thread_id=value.get("thread_id"),
            episode_id=str(value.get("episode_id") or ""),
            timestamp=value.get("timestamp"),
            payload=dict(value.get("payload") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "ApplicationRow",
    "OperatorPolicy",
    "ProactiveAsk",
    "ProactiveFoldback",
    "ProactiveLifecycleEvent",
    "ProactiveResult",
    "ProactiveThread",
    "stable_proactive_id",
]

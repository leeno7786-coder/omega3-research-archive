"""The sealed episode: ten blocks and five independent outcome axes.

CONTRACTS.md section 3. The axes are why this exists. The live episode collapses the whole
question into one field — ``GlobalOutcomePack.outcome["status"]`` plus a ``cascade_status``
on the carrier — so "the cascade finished", "the action worked", "the task succeeded", "a
check passed" and "the operator agreed" are one sentence. On 2026-09-20 a window folded over
ten episodes of a build going round in a circle and called them hollow, because the only
field it could read was empty at the level it read.

:func:`cascade_and_task_from_outcome_pack` is the migration boundary from that shape: it
reads the NESTED pack the real producer writes, refuses the flat shape an older fixture
invented, and reports unknown rather than guessing when the producer recorded nothing.

Sealing is pure content assembly. This module owns no clock, no writer and no store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from schema.continuity_json import (
    CONTINUITY_SCHEMA_VERSION,
    CanonicalRef,
    ContinuityContractError,
    Observation,
    SourceRef,
    content_hash,
    freeze,
    require_enum,
    require_keys,
    require_non_negative_int,
    require_text,
    register_contract_type,
    unfreeze,
)
from schema.episode_event import EpisodeEvent

SCHEMA_VERSION = f"{CONTINUITY_SCHEMA_VERSION}.episode"

EPISODE_RECORD_BLOCKS: tuple[str, ...] = (
    "schema_version",
    "identity",
    "temporal",
    "origin",
    "objective",
    "events",
    "outcome",
    "metacognition",
    "learning",
    "coverage",
)

CASCADE_STATUSES: tuple[str, ...] = ("completed", "failed", "deferred", "interrupted", "unknown")
ACTION_STATUSES: tuple[str, ...] = (
    "succeeded",
    "failed",
    "partial",
    "blocked",
    "cancelled",
    "skipped",
    "not_attempted",
    "unknown",
)
TASK_OUTCOMES: tuple[str, ...] = (
    "success",
    "failure",
    "partial",
    "blocked",
    "cancelled",
    "not_applicable",
    "unknown",
)
VERIFICATION_STATUSES: tuple[str, ...] = (
    "passed",
    "failed",
    "partial",
    "not_performed",
    "not_applicable",
    "unknown",
)
FEEDBACK_STATUSES: tuple[str, ...] = ("not_received", "supports", "contradicts", "mixed", "unknown")

#: Exposed by EpisodeCommitView (WO-07) from the existing terminal, intake and projection
#: authorities — never guessed inside the sealed content, which cannot know its own future.
MEMORY_COMMIT_STATUSES: tuple[str, ...] = (
    "pending",
    "canonical_committed",
    "recall_published",
    "failed",
    "unknown",
)

OUTCOME_AXES: tuple[str, ...] = (
    "cascade_status",
    "action_status",
    "task_outcome",
    "verification_status",
    "feedback_status",
)

_AXIS_VOCABULARY = {
    "cascade_status": CASCADE_STATUSES,
    "action_status": ACTION_STATUSES,
    "task_outcome": TASK_OUTCOMES,
    "verification_status": VERIFICATION_STATUSES,
    "feedback_status": FEEDBACK_STATUSES,
}

_ASSESSMENT_REQUIRED = ("author",)
_ASSESSMENT_OPTIONAL = (
    "verifier",
    "evidence_refs",
    "check_refs",
    "criteria_revision",
    "artifact_revision",
    "note",
)

#: A verification result the host did not witness is not a receipt.
_VERIFIED_STATES = frozenset({"passed", "partial"})

#: Every PERFORMED verification verdict needs its own attribution and at least one check or
#: evidence reference. ``not_performed``, ``not_applicable`` and ``unknown`` need none —
#: they assert no check, which is the distinction the axis exists to preserve.
#:
#: WO-02A C5: the predecessor enforced this only when an assessment happened to be present,
#: so omitting the assessment entirely bypassed the rule and
#: ``EpisodeOutcome('completed','succeeded','success','passed','not_received')`` was accepted
#: with no verifier at all.
_PERFORMED_VERIFICATION = frozenset({"passed", "partial", "failed"})

_IDENTITY_REQUIRED = ("episode_id", "thread_id", "pipeline_id", "lane", "work_kind")
_IDENTITY_OBSERVATIONS = ("turn_id", "continuation_id", "job_id", "attempt_id")

_TEMPORAL_OBSERVATIONS = ("opened_at", "closed_at", "queue_arrival_seq")
_TEMPORAL_REQUIRED = _TEMPORAL_OBSERVATIONS + (
    "finish_line",
    "coverage_clock",
    "read_epoch_start",
    "read_pin",
    "concurrent_commits",
)

_ORIGIN_OBSERVATIONS = ("authority_snapshot_ref", "initial_trajectory_ref")
_ORIGIN_REQUIRED = ("trigger",) + _ORIGIN_OBSERVATIONS

_OBJECTIVE_REQUIRED = ("original_request", "acceptance_criteria", "criteria_revision", "preconditions")

_EVENTS_REQUIRED = ("refs", "by_value", "integrity", "coverage")
_EVENTS_COVERAGE_REQUIRED = ("retained", "referenced", "unavailable_refs", "reason")

_METACOGNITION_REQUIRED = ("control", "model_assessment", "proposals")

_LEARNING_REQUIRED = ("proposals", "effects", "applied_receipts")

_COVERAGE_REQUIRED = ("captured", "unrecorded", "inaccessible", "redacted", "causal_gaps")


def _checked_assessment(axis: str, status: str, value: Any) -> dict[str, Any]:
    data = require_keys(
        value,
        required=_ASSESSMENT_REQUIRED,
        optional=_ASSESSMENT_OPTIONAL,
        where=f"outcome.assessments.{axis}",
    )
    author = require_text(data["author"], where=f"outcome.assessments.{axis}.author", limit=128)
    for name in ("evidence_refs", "check_refs"):
        if name in data and not isinstance(data[name], (list, tuple)):
            raise ContinuityContractError(
                f"outcome.assessments.{axis}.{name} must be a JSON array; a string is not a "
                "reference array"
            )
    refs = tuple(data.get("evidence_refs") or ()) + tuple(data.get("check_refs") or ())
    for index, ref in enumerate(refs):
        if not isinstance(ref, str) or not ref.strip():
            raise ContinuityContractError(
                f"outcome.assessments.{axis} reference [{index}] must be a nonempty string"
            )
    if axis == "verification_status" and status in _PERFORMED_VERIFICATION:
        if not refs:
            raise ContinuityContractError(
                f"verification_status {status!r} requires at least one check or evidence reference"
            )
        # A model may report a failure it observed; it may never certify a success.
        if status in _VERIFIED_STATES and author.strip().lower() == "model":
            raise ContinuityContractError(
                "a model self-report is not a verification receipt; a verified status needs "
                "a host or verifier attribution"
            )
    return data


@dataclass(frozen=True)
class EpisodeOutcome:
    """Five axes that move independently, each with its own attribution."""

    cascade_status: str
    action_status: str
    task_outcome: str
    verification_status: str
    feedback_status: str
    assessments: Mapping[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        for axis in OUTCOME_AXES:
            require_enum(
                getattr(self, axis), allowed=_AXIS_VOCABULARY[axis], where=f"outcome.{axis}"
            )
        raw = {} if self.assessments is None else self.assessments
        if not isinstance(raw, Mapping):
            raise ContinuityContractError("outcome.assessments must be a JSON object")
        unknown = sorted(set(raw) - set(OUTCOME_AXES))
        if unknown:
            raise ContinuityContractError(f"outcome.assessments names unknown axes: {unknown}")
        # A PERFORMED verification must be attributed, whether or not an assessment happens
        # to be supplied (WO-02A C5). Checked before the per-axis pass so an absent
        # assessment cannot slip past a rule that only fires on a present one.
        if self.verification_status in _PERFORMED_VERIFICATION and "verification_status" not in raw:
            raise ContinuityContractError(
                f"verification_status {self.verification_status!r} asserts a performed check "
                "but carries no assessment; a verdict with no attribution and no check "
                "reference is structurally unsupported. An unperformed check is "
                "not_performed, and an unknown one is unknown."
            )
        checked = {
            axis: _checked_assessment(axis, getattr(self, axis), value)
            for axis, value in raw.items()
        }
        object.__setattr__(self, "assessments", freeze(checked))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {axis: getattr(self, axis) for axis in OUTCOME_AXES}
        out["assessments"] = unfreeze(self.assessments)
        return out

    @classmethod
    def from_dict(cls, value: Any) -> "EpisodeOutcome":
        data = require_keys(
            value, required=OUTCOME_AXES, optional=("assessments",), where="outcome"
        )
        return cls(
            cascade_status=data["cascade_status"],
            action_status=data["action_status"],
            task_outcome=data["task_outcome"],
            verification_status=data["verification_status"],
            feedback_status=data["feedback_status"],
            assessments=data.get("assessments") or {},
        )

    @classmethod
    def coerce(cls, value: Any) -> "EpisodeOutcome":
        return value if isinstance(value, cls) else cls.from_dict(value)


def _historical_absence(value: Any, *, where: str) -> Observation | None:
    """A required field a HISTORICAL source never recorded, stated as an absence with its cause.

    WO-08 (contract decision 19). A record converted from an era that did not capture this
    field carries a non-observed ``Observation`` — never a date taken from a file time, a thread
    made up from the episode id, or a finish-line position nobody stamped. Returns the absence,
    or ``None`` when *value* is a plain value for the caller to check as before. An ``observed``
    observation is refused: a field is either its plain value or an absence, never both forms.
    The live sealer always writes plain values.
    """
    if isinstance(value, Observation) or (
        isinstance(value, Mapping) and set(value) == {"state", "value", "reason"}
    ):
        absence = Observation.coerce(value, where=where)
        if absence.is_observed:
            raise ContinuityContractError(
                f"{where} is either a plain value or an absence with its cause; an observed "
                "observation is neither"
            )
        return absence
    return None


def _checked_identity(value: Any) -> dict[str, Any]:
    data = require_keys(
        value, required=_IDENTITY_REQUIRED, optional=_IDENTITY_OBSERVATIONS, where="identity"
    )
    require_text(data["episode_id"], where="identity.episode_id", limit=256)
    for name, limit in (("thread_id", 256), ("lane", 64), ("work_kind", 64)):
        absence = _historical_absence(data[name], where=f"identity.{name}")
        if absence is not None:
            data[name] = absence
        else:
            require_text(data[name], where=f"identity.{name}", limit=limit)
    if isinstance(data["pipeline_id"], bool) or not isinstance(data["pipeline_id"], int):
        raise ContinuityContractError("identity.pipeline_id must be an integer")
    for name in _IDENTITY_OBSERVATIONS:
        if name in data:
            data[name] = Observation.coerce(data[name], where=f"identity.{name}")
    return data


def _checked_temporal(value: Any) -> dict[str, Any]:
    data = require_keys(value, required=_TEMPORAL_REQUIRED, where="temporal")
    for name in _TEMPORAL_OBSERVATIONS:
        data[name] = Observation.coerce(data[name], where=f"temporal.{name}")
    checks = {
        "finish_line": lambda v: require_keys(
            v, required=("commit_seq", "commit_ms", "event_date", "pipeline_id", "work_kind"),
            where="temporal.finish_line"),
        "coverage_clock": lambda v: require_keys(
            v, required=("clock_epoch_id", "commit_seq"), where="temporal.coverage_clock"),
        "read_epoch_start": lambda v: require_non_negative_int(v, where="temporal.read_epoch_start"),
        "read_pin": lambda v: require_keys(
            v, required=("graph_snapshot_hash", "coord_index_generation", "dme_pack_hash"),
            optional=("unpinnable_surfaces", "attribution"), where="temporal.read_pin"),
    }
    for name, check in checks.items():
        absence = _historical_absence(data[name], where=f"temporal.{name}")
        if absence is not None:
            data[name] = absence
        else:
            check(data[name])
    concurrent = require_keys(
        data["concurrent_commits"],
        required=("episode_ids", "complete"),
        where="temporal.concurrent_commits",
    )
    if not isinstance(concurrent["complete"], bool):
        raise ContinuityContractError(
            "temporal.concurrent_commits.complete must be a boolean: a set that can be short "
            "without saying so is a silent under-report"
        )
    return data


def _checked_origin(value: Any) -> dict[str, Any]:
    data = require_keys(value, required=_ORIGIN_REQUIRED, where="origin")
    trigger = require_keys(
        data["trigger"], required=("opened_by",), optional=("source_ref",), where="origin.trigger"
    )
    absence = _historical_absence(trigger["opened_by"], where="origin.trigger.opened_by")
    if absence is not None:
        trigger["opened_by"] = absence
    else:
        require_text(trigger["opened_by"], where="origin.trigger.opened_by", limit=128)
    if "source_ref" in trigger and not isinstance(trigger["source_ref"], SourceRef):
        trigger["source_ref"] = SourceRef.from_dict(trigger["source_ref"])
    data["trigger"] = trigger
    for name in _ORIGIN_OBSERVATIONS:
        data[name] = Observation.coerce(data[name], where=f"origin.{name}")
    return data


def _checked_objective(value: Any) -> dict[str, Any]:
    data = require_keys(value, required=_OBJECTIVE_REQUIRED, where="objective")
    for name, limit in (("original_request", 16000), ("criteria_revision", 256)):
        absence = _historical_absence(data[name], where=f"objective.{name}")
        if absence is not None:
            data[name] = absence
        else:
            require_text(data[name], where=f"objective.{name}", limit=limit)
    for name in ("acceptance_criteria", "preconditions"):
        if not isinstance(data[name], (list, tuple)):
            raise ContinuityContractError(f"objective.{name} must be a JSON array")
    return data


def _checked_events(value: Any) -> dict[str, Any]:
    data = require_keys(value, required=_EVENTS_REQUIRED, where="events")
    refs = tuple(
        CanonicalRef.coerce(row, where=f"events.refs[{index}]")
        for index, row in enumerate(data["refs"])
    )
    by_value = tuple(
        row if isinstance(row, EpisodeEvent) else EpisodeEvent.from_dict(row)
        for row in data["by_value"]
    )
    integrity = data["integrity"]
    if not isinstance(integrity, Mapping):
        raise ContinuityContractError("events.integrity must be a JSON object")
    for event in by_value:
        if integrity.get(event.event_id) != event.content_sha256():
            raise ContinuityContractError(
                f"events.integrity is missing or disagrees with the retained bytes of "
                f"{event.event_id!r}"
            )
    coverage = require_keys(
        data["coverage"], required=_EVENTS_COVERAGE_REQUIRED, where="events.coverage"
    )
    require_non_negative_int(coverage["retained"], where="events.coverage.retained")
    require_non_negative_int(coverage["referenced"], where="events.coverage.referenced")
    if coverage["retained"] != len(by_value):
        raise ContinuityContractError(
            "events.coverage.retained must equal the number of retained by-value events"
        )
    unavailable = tuple(coverage["unavailable_refs"])
    if unavailable and not str(coverage["reason"]).strip():
        raise ContinuityContractError(
            "an unavailable event reference must say why its bytes were not retained"
        )
    data["refs"] = refs
    data["by_value"] = by_value
    data["coverage"] = coverage
    return data


def _checked_metacognition(value: Any) -> dict[str, Any]:
    from schema.omega_control_decision import OmegaControlDecision

    data = require_keys(value, required=_METACOGNITION_REQUIRED, where="metacognition")
    control = data["control"]
    if isinstance(control, Observation) or (
        isinstance(control, Mapping) and set(control) == {"state", "value", "reason"}
    ):
        # WO-07: a fold that failed or never ran has NO validated control. The record says so
        # with the cause, as an observation that is not ``observed``; it never fabricates a
        # decision, and nothing may be assessed or proposed under a control that does not exist.
        control = Observation.coerce(control, where="metacognition.control")
        if control.is_observed:
            raise ContinuityContractError(
                "metacognition.control is either a validated control decision or an absence "
                "with its cause; an observed observation is neither"
            )
        if data["model_assessment"] or data["proposals"]:
            raise ContinuityContractError(
                "metacognition without a control decision cannot carry an assessment or "
                "proposals: nothing was decided for them to belong to"
            )
    elif not isinstance(control, OmegaControlDecision):
        try:
            control = OmegaControlDecision.from_dict(control)
        except ValueError as exc:
            raise ContinuityContractError(f"metacognition.control: {exc}") from exc
    data["control"] = control
    if not isinstance(data["model_assessment"], Mapping):
        raise ContinuityContractError("metacognition.model_assessment must be a JSON object")
    if not isinstance(data["proposals"], (list, tuple)):
        raise ContinuityContractError("metacognition.proposals must be a JSON array")
    return data


def _checked_learning(value: Any) -> dict[str, Any]:
    data = require_keys(value, required=_LEARNING_REQUIRED, where="learning")
    for name in _LEARNING_REQUIRED:
        if not isinstance(data[name], (list, tuple)):
            raise ContinuityContractError(f"learning.{name} must be a JSON array")
    return data


def _checked_coverage(value: Any) -> dict[str, Any]:
    data = require_keys(value, required=_COVERAGE_REQUIRED, where="coverage")
    for name in _COVERAGE_REQUIRED:
        if not isinstance(data[name], (list, tuple)):
            raise ContinuityContractError(f"coverage.{name} must be a JSON array")
    return data


@dataclass(frozen=True)
class EpisodeRecord:
    """One sealed episode. Assembled by DeepSleep; written through the existing memory lane."""

    identity: Mapping[str, Any]
    temporal: Mapping[str, Any]
    origin: Mapping[str, Any]
    objective: Mapping[str, Any]
    events: Mapping[str, Any]
    outcome: EpisodeOutcome
    metacognition: Mapping[str, Any]
    learning: Mapping[str, Any]
    coverage: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "identity", freeze(_checked_identity(self.identity)))
        object.__setattr__(self, "temporal", freeze(_checked_temporal(self.temporal)))
        object.__setattr__(self, "origin", freeze(_checked_origin(self.origin)))
        object.__setattr__(self, "objective", freeze(_checked_objective(self.objective)))
        object.__setattr__(self, "events", freeze(_checked_events(self.events)))
        object.__setattr__(self, "outcome", EpisodeOutcome.coerce(self.outcome))
        object.__setattr__(self, "metacognition", freeze(_checked_metacognition(self.metacognition)))
        object.__setattr__(self, "learning", freeze(_checked_learning(self.learning)))
        object.__setattr__(self, "coverage", freeze(_checked_coverage(self.coverage)))

    @property
    def episode_id(self) -> str:
        return str(self.identity["episode_id"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "identity": unfreeze(self.identity),
            "temporal": unfreeze(self.temporal),
            "origin": unfreeze(self.origin),
            "objective": unfreeze(self.objective),
            "events": unfreeze(self.events),
            "outcome": self.outcome.to_dict(),
            "metacognition": unfreeze(self.metacognition),
            "learning": unfreeze(self.learning),
            "coverage": unfreeze(self.coverage),
        }

    def content_sha256(self) -> str:
        """The record's own content hash. It never contains a hash of itself."""
        return content_hash(self.to_dict())

    @classmethod
    def from_dict(cls, value: Any) -> "EpisodeRecord":
        data = require_keys(value, required=EPISODE_RECORD_BLOCKS, where="episode_record")
        if data.pop("schema_version") != SCHEMA_VERSION:
            raise ContinuityContractError("episode_record.schema_version does not match this contract")
        return cls(**data)


# ── the migration boundary from the live nested outcome pack ──────────────────

#: Where the real producer puts the status. `write_outcome` stores asdict(GlobalOutcomePack),
#: so the verdict is one level deeper than a flat reader expects.
OUTCOME_PACK_STATUS_PATH = "outcome.outcome.status"

_PACK_STATUS_TO_TASK = {
    "completed": "success",
    "success": "success",
    "ok": "success",
    "failed": "failure",
    "fail": "failure",
    "error": "failure",
    "partial": "partial",
    "blocked": "blocked",
    "cancelled": "cancelled",
    "skipped": "not_applicable",
    "idle": "not_applicable",
    "pending_input": "blocked",
}


def cascade_and_task_from_outcome_pack(pack: Any, *, cascade_status: str) -> dict[str, Any]:
    """Read the NESTED GlobalOutcomePack and separate cascade completion from task outcome.

    The pack is ``asdict(GlobalOutcomePack)``, so its verdict lives at
    ``outcome["outcome"]["status"]``. A flat mapping with a top-level ``status`` is refused
    outright rather than read: that shape is what a stale fixture produced while the live
    reader looked one level too shallow, and accepting it would let the same disagreement
    back in. A pack whose producer recorded no status yields ``unknown`` — never a guess.
    """
    if not isinstance(pack, Mapping):
        raise ContinuityContractError(
            f"an outcome pack must be a JSON object, got {type(pack).__name__}"
        )
    inner = pack.get("outcome")
    if not isinstance(inner, Mapping):
        raise ContinuityContractError(
            "this is not a GlobalOutcomePack: the producer nests its verdict under "
            f"{OUTCOME_PACK_STATUS_PATH!r}, and a flat status is the shape that hid the real one"
        )
    require_enum(cascade_status, allowed=CASCADE_STATUSES, where="cascade_status")
    raw_status = inner.get("status")
    status = str(raw_status).strip().lower() if isinstance(raw_status, str) else ""
    task_outcome = _PACK_STATUS_TO_TASK.get(status, "unknown")
    return {
        "cascade_status": cascade_status,
        "task_outcome": task_outcome,
        "evidence": {
            "status_path": OUTCOME_PACK_STATUS_PATH,
            "producer_status": status,
            "summary": str(inner.get("summary") or ""),
            "failure_kind": str(inner.get("failure_kind") or ""),
        },
    }


__all__ = [
    "ACTION_STATUSES",
    "CASCADE_STATUSES",
    "EPISODE_RECORD_BLOCKS",
    "FEEDBACK_STATUSES",
    "MEMORY_COMMIT_STATUSES",
    "OUTCOME_AXES",
    "OUTCOME_PACK_STATUS_PATH",
    "SCHEMA_VERSION",
    "TASK_OUTCOMES",
    "VERIFICATION_STATUSES",
    "EpisodeOutcome",
    "EpisodeRecord",
    "cascade_and_task_from_outcome_pack",
]


# Nestable inside an accepted record: the outcome block, and the shared control object the
# metacognition block carries. Registering OmegaControlDecision here (rather than editing the
# shared module) keeps that contract untouched while letting `freeze` pass it through.
from schema.omega_control_decision import OmegaControlDecision as _OmegaControlDecision  # noqa: E402

for _contract_type in (EpisodeOutcome, _OmegaControlDecision):
    register_contract_type(_contract_type)
del _contract_type

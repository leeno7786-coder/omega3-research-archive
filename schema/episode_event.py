"""One recorded fact inside an episode: EpisodeEvent (CONTRACTS.md section 2).

Today nothing like this exists. Phase facts survive only in the final enriched body and in
``guardian.terminal_feedback.emit_data`` calls wrapped in bare excepts, so a lost emission
and an absent fact are the same observation. WO-01 recorded that as the program's second
missing capability; this is its contract.

Three properties are load-bearing:

* **occurrence identity is separate from content identity.** Two real operations with equal
  payload bytes are different events (``occurrence_id``), while a persistence retry of one
  operation must be recognisable as the same content (``content_sha256``). Conflating them
  is how a replay becomes a second occurrence.
* **the producer names itself, including its model, prompt and tool revisions.** No live
  episode records any of these today, which is why no two attempts can be compared.
* **a class is not self-assigned.** External content cannot label itself a commit receipt:
  the class is checked against the producer's authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from schema.continuity_json import (
    CONTINUITY_SCHEMA_VERSION,
    ContinuityContractError,
    EventIdentityConflict,
    Observation,
    content_hash,
    freeze,
    require_enum,
    require_keys,
    require_positive_int,
    require_text,
    require_texts,
    register_contract_type,
    unfreeze,
)

SCHEMA_VERSION = f"{CONTINUITY_SCHEMA_VERSION}.event"

# Admission is the durable identity source for every later event in an episode.
ADMISSION_ORIGIN = "admission.origin"

EPISODE_PHASES: tuple[str, ...] = (
    "admission",
    "pressure",
    "global_a",
    "meta_a",
    "auto",
    "meta_b",
    "global_b",
    "omega",
    "deepsleep",
)

EVIDENCE_CLASSES: tuple[str, ...] = (
    "host_observation",
    "operator_statement",
    "external_content",
    "model_conclusion",
    "verification_result",
    "commit_receipt",
)

EVENT_STATUSES: tuple[str, ...] = (
    "succeeded",
    "failed",
    "partial",
    "blocked",
    "cancelled",
    "skipped",
    "not_attempted",
    "unknown",
)

PRODUCER_AUTHORITIES: tuple[str, ...] = ("host", "model", "external", "provider")

_PRODUCER_REQUIRED = ("component", "component_version", "authority")
_PRODUCER_OPTIONAL = (
    "model",
    "prompt_revision",
    "tool_revision",
    "config_revision",
    "verification_revision",
)

#: The host is the only authority that may assert these classes about itself.
_HOST_ONLY_CLASSES = frozenset({"commit_receipt", "verification_result"})

MAX_REFS = 32
MAX_EVENT_KIND = 128

#: THE definition of "the same persisted event", in one place (WO-02A C3).
#:
#: Replay equality compares every one of these. Semantic content (``content_view``) is a
#: strictly smaller view and is not equality: two different operations may share it. Grouped
#: by family so a test can cover each:
#:
#:   * source identity — episode, thread, event, operation
#:   * ordering — sequence and phase attempt
#:   * placement — phase, kind
#:   * time — observation and occurrence, plus the boot that produced it
#:   * attribution — producer (component, version, authority, model/prompt/tool revisions)
#:   * assertion — evidence class, status, payload, references
#:   * optional host identities — recorded only when the host had them
SEALED_EVENT_FIELDS: tuple[str, ...] = (
    "episode_id",
    "thread_id",
    "event_id",
    "operation_id",
    "event_seq",
    "phase_attempt",
    "phase",
    "event_kind",
    "observed_at",
    "occurred_at",
    "boot_id",
    "elapsed_ns",
    "producer",
    "evidence_class",
    "status",
    "payload",
    "evidence_refs",
    "artifact_refs",
    "turn_id",
    "continuation_id",
    "job_id",
    "attempt_id",
    "arrival_seq",
    "pipeline_id",
)


def _checked_producer(value: Any) -> dict[str, str]:
    data = require_keys(
        value, required=_PRODUCER_REQUIRED, optional=_PRODUCER_OPTIONAL, where="event.producer"
    )
    require_text(data["component"], where="event.producer.component", limit=256)
    require_text(data["component_version"], where="event.producer.component_version", limit=128)
    require_enum(data["authority"], allowed=PRODUCER_AUTHORITIES, where="event.producer.authority")
    for name in _PRODUCER_OPTIONAL:
        if name in data:
            require_text(data[name], where=f"event.producer.{name}", limit=256)
    return {key: str(value) for key, value in data.items()}


@dataclass(frozen=True)
class EpisodeEvent:
    """One attributable fact, produced by one component, at one phase of one episode."""

    episode_id: str
    thread_id: str
    event_seq: int
    event_id: str
    operation_id: str
    phase_attempt: int
    phase: str
    event_kind: str
    observed_at: str
    occurred_at: Observation
    boot_id: str
    elapsed_ns: Observation
    producer: Mapping[str, str]
    evidence_class: str
    status: str
    payload: Mapping[str, Any]
    evidence_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    # Optional identities, each originating with its existing host owner. Absent means the
    # host did not record it, which is not the same as a relationship that does not exist —
    # a fabricated UUID cannot repair a missing historical link.
    turn_id: str = ""
    continuation_id: str = ""
    job_id: str = ""
    attempt_id: str = ""
    arrival_seq: int = 0
    pipeline_id: int = -1

    _OPTIONAL_TEXT = ("turn_id", "continuation_id", "job_id", "attempt_id")

    def __post_init__(self) -> None:
        require_text(self.episode_id, where="event.episode_id", limit=256)
        require_text(self.thread_id, where="event.thread_id", limit=256)
        require_positive_int(self.event_seq, where="event.event_seq")
        require_text(self.event_id, where="event.event_id", limit=256)
        require_text(self.operation_id, where="event.operation_id", limit=256)
        require_positive_int(self.phase_attempt, where="event.phase_attempt")
        require_enum(self.phase, allowed=EPISODE_PHASES, where="event.phase")
        require_text(self.event_kind, where="event.event_kind", limit=MAX_EVENT_KIND)
        require_text(self.observed_at, where="event.observed_at", limit=64)
        require_text(self.boot_id, where="event.boot_id", limit=128)
        require_enum(self.evidence_class, allowed=EVIDENCE_CLASSES, where="event.evidence_class")
        require_enum(self.status, allowed=EVENT_STATUSES, where="event.status")

        object.__setattr__(self, "occurred_at", Observation.coerce(self.occurred_at, where="event.occurred_at"))
        object.__setattr__(self, "elapsed_ns", Observation.coerce(self.elapsed_ns, where="event.elapsed_ns"))
        if self.occurred_at.is_observed:
            require_text(self.occurred_at.value, where="event.occurred_at.value", limit=64)
        if self.elapsed_ns.is_observed and (
            isinstance(self.elapsed_ns.value, bool) or not isinstance(self.elapsed_ns.value, int)
        ):
            raise ContinuityContractError("event.elapsed_ns observed value must be an integer")

        producer = _checked_producer(self.producer)
        if self.evidence_class in _HOST_ONLY_CLASSES and producer["authority"] != "host":
            raise ContinuityContractError(
                f"evidence class {self.evidence_class!r} requires host authority; a "
                f"{producer['authority']!r} producer cannot assign it to its own content"
            )
        object.__setattr__(self, "producer", freeze(producer, where="event.producer"))

        if not isinstance(self.payload, Mapping):
            raise ContinuityContractError("event.payload must be a JSON object")
        object.__setattr__(self, "payload", freeze(self.payload, where="event.payload"))

        object.__setattr__(
            self,
            "evidence_refs",
            require_texts(self.evidence_refs, where="event.evidence_refs", limit=512, maximum=MAX_REFS),
        )
        object.__setattr__(
            self,
            "artifact_refs",
            require_texts(self.artifact_refs, where="event.artifact_refs", limit=512, maximum=MAX_REFS),
        )

        for name in self._OPTIONAL_TEXT:
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ContinuityContractError(f"event.{name} must be text")
            if value:
                require_text(value, where=f"event.{name}", limit=256)
        if self.arrival_seq:
            require_positive_int(self.arrival_seq, where="event.arrival_seq")
        elif isinstance(self.arrival_seq, bool) or not isinstance(self.arrival_seq, int):
            raise ContinuityContractError("event.arrival_seq must be an integer and never a boolean")
        if isinstance(self.pipeline_id, bool) or not isinstance(self.pipeline_id, int):
            raise ContinuityContractError("event.pipeline_id must be an integer and never a boolean")
        if self.pipeline_id != -1:
            from core.pipelines import PIPELINE_IDS

            if self.pipeline_id not in PIPELINE_IDS:
                raise ContinuityContractError(
                    f"event.pipeline_id must be one of {list(PIPELINE_IDS)} or absent"
                )

    # ── identity ───────────────────────────────────────────────────────────

    def content_view(self) -> dict[str, Any]:
        """What this event SAYS: semantic content only, with no identity, attribution or time.

        Two genuinely different operations may legitimately share this. It is a similarity
        view, NOT the equality of a persisted occurrence — see :data:`SEALED_EVENT_FIELDS`.
        """
        return {
            "schema_version": SCHEMA_VERSION,
            "phase": self.phase,
            "event_kind": self.event_kind,
            "evidence_class": self.evidence_class,
            "status": self.status,
            "payload": unfreeze(self.payload),
            "evidence_refs": list(self.evidence_refs),
            "artifact_refs": list(self.artifact_refs),
        }

    def content_sha256(self) -> str:
        """Equal for equal semantic content. Deliberately blind to identity and attribution."""
        return content_hash(self.content_view())

    def sealed_view(self) -> dict[str, Any]:
        """Every immutable field of this occurrence — the set replay equality compares.

        Declared once, in :data:`SEALED_EVENT_FIELDS`, so no two call sites can disagree
        about what "the same event" means.
        """
        return {name: unfreeze(getattr(self, name)) for name in SEALED_EVENT_FIELDS}

    def sealed_sha256(self) -> str:
        """The hash of every sealed field. Changing any of them is a different occurrence."""
        return content_hash(self.sealed_view())

    def occurrence_id(self) -> str:
        """Distinct per real operation, even when the payload bytes are identical."""
        return content_hash(
            {
                "episode_id": self.episode_id,
                "event_id": self.event_id,
                "operation_id": self.operation_id,
                "phase_attempt": self.phase_attempt,
                "event_seq": self.event_seq,
            }
        )

    def require_same_occurrence(self, other: "EpisodeEvent") -> "EpisodeEvent":
        """Return self when *other* is the same sealed occurrence; raise when any field moved.

        This is the seam a persistence retry passes through. Comparing semantic content alone
        was not enough (WO-02A C3): preserving ``event_id`` while changing ``event_seq``
        produced a different ``occurrence_id`` and was still accepted, so a replay could
        renumber an event's place in its episode — or restate who produced it — without a
        conflict. Equality is now the whole sealed field set, and the first field that
        differs is named.

        Recovery ownership is separate operational metadata and does not live here: a future
        retry must RETRIEVE the existing event and validate its supplied immutable inputs
        against it, never mint a new sequence or timestamp for the same identity.
        """
        if not isinstance(other, EpisodeEvent):
            raise ContinuityContractError("an occurrence comparison needs another event")
        if self.event_id != other.event_id:
            raise ContinuityContractError(
                f"events {self.event_id!r} and {other.event_id!r} are different identities"
            )
        if self.sealed_sha256() != other.sealed_sha256():
            mine, theirs = self.sealed_view(), other.sealed_view()
            moved = [name for name in SEALED_EVENT_FIELDS if mine[name] != theirs[name]]
            raise EventIdentityConflict(
                f"event {self.event_id!r} was already recorded with different immutable "
                f"content; these sealed field(s) differ: {moved}"
            )
        return self

    # ── serialization ──────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "episode_id": self.episode_id,
            "thread_id": self.thread_id,
            "event_seq": self.event_seq,
            "event_id": self.event_id,
            "operation_id": self.operation_id,
            "phase_attempt": self.phase_attempt,
            "phase": self.phase,
            "event_kind": self.event_kind,
            "observed_at": self.observed_at,
            "occurred_at": self.occurred_at.to_dict(),
            "boot_id": self.boot_id,
            "elapsed_ns": self.elapsed_ns.to_dict(),
            "producer": unfreeze(self.producer),
            "evidence_class": self.evidence_class,
            "status": self.status,
            "payload": unfreeze(self.payload),
            "evidence_refs": list(self.evidence_refs),
            "artifact_refs": list(self.artifact_refs),
        }
        for name in self._OPTIONAL_TEXT:
            value = getattr(self, name)
            if value:
                out[name] = value
        if self.arrival_seq:
            out["arrival_seq"] = self.arrival_seq
        if self.pipeline_id != -1:
            out["pipeline_id"] = self.pipeline_id
        return out

    @classmethod
    def from_dict(cls, value: Any) -> "EpisodeEvent":
        data = require_keys(
            value,
            required=(
                "schema_version",
                "episode_id",
                "thread_id",
                "event_seq",
                "event_id",
                "operation_id",
                "phase_attempt",
                "phase",
                "event_kind",
                "observed_at",
                "occurred_at",
                "boot_id",
                "elapsed_ns",
                "producer",
                "evidence_class",
                "status",
                "payload",
                "evidence_refs",
                "artifact_refs",
            ),
            optional=("turn_id", "continuation_id", "job_id", "attempt_id", "arrival_seq", "pipeline_id"),
            where="event",
        )
        if data.pop("schema_version") != SCHEMA_VERSION:
            raise ContinuityContractError("event.schema_version does not match this contract")
        data["occurred_at"] = Observation.coerce(data["occurred_at"], where="event.occurred_at")
        data["elapsed_ns"] = Observation.coerce(data["elapsed_ns"], where="event.elapsed_ns")
        # NOT tuple(...) (WO-02A C2): tuple('abc') is ('a','b','c'), so a string silently
        # became three references. The declared type is checked in __post_init__, which is
        # the same check direct construction gets — so both paths agree.
        return cls(**data)


__all__ = [
    "EPISODE_PHASES",
    "EVENT_STATUSES",
    "EVIDENCE_CLASSES",
    "MAX_REFS",
    "PRODUCER_AUTHORITIES",
    "SCHEMA_VERSION",
    "EpisodeEvent",
]


register_contract_type(EpisodeEvent)

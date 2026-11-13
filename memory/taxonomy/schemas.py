"""Phase 7B — canonical memory object model for the deterministic taxonomy layer.

House style (the schema/ idiom): pure-stdlib ``@dataclass(frozen=True)`` — no pydantic.
Every record carries a ``type`` discriminator and a ``schema_version`` (last to_dict key).
Enums are module-level ``frozenset`` constants; ``validate() -> list[str]`` returns errors
([] == valid) and NEVER raises; ``to_dict()`` is hand-written (type-first, schema_version-last,
tuples -> list). These are sidecar PROJECTION records, not cognition-engine I/O contracts,
and import nothing upward (stdlib + dataclasses only) — leaf-level + Guardian-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── schema version constants ───────────────────────────────────────────────────
MEMORY_EVENT_SCHEMA_VERSION = "omega.memory_event.v1"
MEMORY_ENTITY_SCHEMA_VERSION = "omega.memory_entity.v1"
MEMORY_CLAIM_SCHEMA_VERSION = "omega.memory_claim.v1"
MEMORY_EDGE_SCHEMA_VERSION = "omega.memory_edge.v1"
MEMORY_EPISODE_SCHEMA_VERSION = "omega.memory_episode.v1"
MEMORY_ACTION_SCHEMA_VERSION = "omega.memory_action.v1"
MEMORY_OUTCOME_SCHEMA_VERSION = "omega.memory_outcome.v1"
MEMORY_QUESTION_SCHEMA_VERSION = "omega.memory_question.v1"
MEMORY_FAILURE_SCHEMA_VERSION = "omega.memory_failure.v1"
MEMORY_SKILL_USE_SCHEMA_VERSION = "omega.memory_skill_use.v1"
MEMORY_GRAPH_SNAPSHOT_SCHEMA_VERSION = "omega.memory_graph_snapshot.v1"
MEMORY_LEAF_V2_SCHEMA_VERSION = "omega.memory_leaf.v2"
RECALL_PACKET_SCHEMA_VERSION = "omega.recall_packet.v1"
RECALL_VERDICT_SCHEMA_VERSION = "omega.recall_verdict.v1"

# ── controlled vocabularies (single source of truth; 7E/7F import these) ───────
EVENT_KINDS: frozenset[str] = frozenset(
    {
        "operator_input",
        "pressure_ignition",
        "global_a_intent",
        "meta_a_action_pack",
        "meta_a_skill_selection",
        "skill_execution_pack",
        "qwen_skill_dispatch",
        "qwen_skill_result",
        "auto_outcome",
        "meta_b_outcome",
        "meta_b_skill_outcome",
        "global_b_update",
        "global_b_skill_update",
        "omega_consolidation",
        "deepsleep_summary",
        "deepsleep_skill_summary",
        "capability_signal",
        "capability_reinforced",
        "capability_weakened",
        "tool_result",
        "tool_failure",
        "skill_failure",
        "safety_block",
        "bridge_failure",
        "recall_query",
        "recall_result",
        "correction",
        "reflection",
        "graph_snapshot",
    }
)

ENTITY_KINDS: frozenset[str] = frozenset(
    {
        "component",
        "layer",
        "skill",
        "tool",
        "file",
        "module",
        "function",
        "class",
        "schema",
        "episode",
        "action",
        "dispatch",
        "outcome",
        "failure",
        "question",
        "policy",
        "invariant",
        "trajectory",
        "operator",
        "model",
        "artifact",
        "memory_record",
        "named_entity",  # compiler proper-name projection (canonicalize.py alias lane)
    }
)

CLAIM_STATUSES: frozenset[str] = frozenset({"active", "superseded", "rejected"})
MEMORY_LEAF_STATUSES: frozenset[str] = frozenset(
    {
        "active",
        "superseded",
        "rejected",
        "quarantined",
    }
)

EDGE_TYPES: frozenset[str] = frozenset(
    {
        "OBSERVED_IN",
        "DERIVED_FROM",
        "MENTIONS",
        "SUPPORTS",
        "CONTRADICTS",
        "REFINES",
        "SUPERSEDES",
        "DEPENDS_ON",
        "CAUSED_BY",
        "FAILED_BECAUSE",
        "ANSWERS",
        "ASKS",
        "REOPENS",
        "STRENGTHENS",
        "WEAKENS",
        "OCCURRED_IN",
        "PRODUCED",
        "SELECTED_SKILL",
        "USED_TOOL",
        "RESULTED_IN",
        "SATISFIED",
        "DID_NOT_SATISFY",
        "CREATED_PRESSURE",
        "CLOSED_BY",
        "CONSOLIDATED_BY",
        "CO_LOCATED",
        "SHARES_DOMAIN",
        "SHARES_TAG",
        # SEMANTIC_SIMILARITY: RETIRED at compiler v7 (memory-organ Cut 5A) — the
        # graph is structural-only; see memory.dme.hop_engine.RETIRED_EDGE_TYPES.
        # Persona/knowledge relation edges projected from record enrichment
        # (memory.taxonomy.extract_persona) — the graph's semantic wiring.
        "FAMILY_OF",
        "OWNS",
        "STUDIES",
        "WORKS_AT",
        "WORKS_AS",
        "LIVES_IN",
        "LIKES",
        "SAME_AS",  # entity-resolution alias: operator == "Noah" (extract_persona)
    }
)


def _missing(value: Any) -> bool:
    """True when a required string field is absent/blank."""
    return not (isinstance(value, str) and value.strip())


# ── MemoryEvent — the append-only truth unit ──────────────────────────────────
@dataclass(frozen=True)
class MemoryEvent:
    event_id: str
    event_kind: str
    episode_id: str
    source: str
    created_at: str
    content_hash: str
    payload: dict = field(default_factory=dict)
    type: str = "memory_event"
    schema_version: str = MEMORY_EVENT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "event_id": self.event_id,
            "event_kind": self.event_kind,
            "episode_id": self.episode_id,
            "source": self.source,
            "created_at": self.created_at,
            "content_hash": self.content_hash,
            "payload": dict(self.payload),
            "schema_version": self.schema_version,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if _missing(self.event_id):
            errors.append("missing event_id")
        if self.event_kind not in EVENT_KINDS:
            errors.append(f"unknown event_kind: {self.event_kind!r}")
        if _missing(self.episode_id):
            errors.append("missing episode_id")
        if _missing(self.source):
            errors.append("missing source")
        if _missing(self.created_at):
            errors.append("missing created_at")
        if _missing(self.content_hash):
            errors.append("missing content_hash")
        if not isinstance(self.payload, dict):
            errors.append("payload must be a dict")
        return errors


# ── MemoryEntity — a stable anchor ────────────────────────────────────────────
@dataclass(frozen=True)
class MemoryEntity:
    entity_id: str
    entity_kind: str
    canonical_name: str
    source_event_id: str
    aliases: tuple = ()
    type: str = "memory_entity"
    schema_version: str = MEMORY_ENTITY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "entity_id": self.entity_id,
            "entity_kind": self.entity_kind,
            "canonical_name": self.canonical_name,
            "aliases": list(self.aliases),
            "source_event_id": self.source_event_id,
            "schema_version": self.schema_version,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if _missing(self.entity_id):
            errors.append("missing entity_id")
        if self.entity_kind not in ENTITY_KINDS:
            errors.append(f"unknown entity_kind: {self.entity_kind!r}")
        if _missing(self.canonical_name):
            errors.append("missing canonical_name")
        if _missing(self.source_event_id):
            errors.append("missing source_event_id")
        return errors


# ── MemoryClaim — a deterministic semantic assertion ──────────────────────────
@dataclass(frozen=True)
class MemoryClaim:
    claim_id: str
    subject: str
    predicate: str
    object: str
    source_event_id: str
    evidence_event_ids: tuple = ()
    confidence: float = 1.0
    status: str = "active"
    type: str = "memory_claim"
    schema_version: str = MEMORY_CLAIM_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "claim_id": self.claim_id,
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object,
            "confidence": self.confidence,
            "status": self.status,
            "source_event_id": self.source_event_id,
            "evidence_event_ids": list(self.evidence_event_ids),
            "schema_version": self.schema_version,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if _missing(self.claim_id):
            errors.append("missing claim_id")
        if _missing(self.subject):
            errors.append("missing subject")
        if _missing(self.predicate):
            errors.append("missing predicate")
        if _missing(self.object):
            errors.append("missing object")
        if self.status not in CLAIM_STATUSES:
            errors.append(f"unknown status: {self.status!r}")
        if _missing(self.source_event_id):
            errors.append("missing source_event_id")
        if not self.evidence_event_ids:
            errors.append("claim must cite evidence (evidence_event_ids empty)")
        if not (0.0 <= self.confidence <= 1.0):
            errors.append(f"confidence out of range: {self.confidence}")
        return errors


# ── MemoryEdge — a graph relation ─────────────────────────────────────────────
@dataclass(frozen=True)
class MemoryEdge:
    edge_id: str
    source_id: str
    target_id: str
    edge_type: str
    evidence_event_id: str
    confidence: float = 1.0
    payload: dict = field(default_factory=dict)
    type: str = "memory_edge"
    schema_version: str = MEMORY_EDGE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "edge_id": self.edge_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "edge_type": self.edge_type,
            "confidence": self.confidence,
            "evidence_event_id": self.evidence_event_id,
            "payload": dict(self.payload),
            "schema_version": self.schema_version,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if _missing(self.edge_id):
            errors.append("missing edge_id")
        if _missing(self.source_id):
            errors.append("missing source_id")
        if _missing(self.target_id):
            errors.append("missing target_id")
        if self.edge_type not in EDGE_TYPES:
            errors.append(f"unknown edge_type: {self.edge_type!r}")
        if _missing(self.evidence_event_id):
            errors.append("missing evidence_event_id")
        if not (0.0 <= self.confidence <= 1.0):
            errors.append(f"confidence out of range: {self.confidence}")
        return errors


# ── MemoryEpisode / Action / Outcome / Question / Failure / SkillUse (node payloads) ──
@dataclass(frozen=True)
class MemoryEpisode:
    episode_id: str
    intent: str
    action: str
    outcome: str
    cascade_status: str
    opened_by: str
    source_event_id: str
    type: str = "memory_episode"
    schema_version: str = MEMORY_EPISODE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "episode_id": self.episode_id,
            "intent": self.intent,
            "action": self.action,
            "outcome": self.outcome,
            "cascade_status": self.cascade_status,
            "opened_by": self.opened_by,
            "source_event_id": self.source_event_id,
            "schema_version": self.schema_version,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if _missing(self.episode_id):
            errors.append("missing episode_id")
        if _missing(self.source_event_id):
            errors.append("missing source_event_id")
        return errors


@dataclass(frozen=True)
class MemoryAction:
    episode_id: str
    action_kind: str
    tool: str
    skill_name: str
    source_event_id: str
    type: str = "memory_action"
    schema_version: str = MEMORY_ACTION_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "episode_id": self.episode_id,
            "action_kind": self.action_kind,
            "tool": self.tool,
            "skill_name": self.skill_name,
            "source_event_id": self.source_event_id,
            "schema_version": self.schema_version,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if _missing(self.episode_id):
            errors.append("missing episode_id")
        if _missing(self.action_kind):
            errors.append("missing action_kind")
        if _missing(self.source_event_id):
            errors.append("missing source_event_id")
        return errors


@dataclass(frozen=True)
class MemoryOutcome:
    episode_id: str
    status: str
    satisfied: bool
    summary: str
    source_event_id: str
    type: str = "memory_outcome"
    schema_version: str = MEMORY_OUTCOME_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "episode_id": self.episode_id,
            "status": self.status,
            "satisfied": self.satisfied,
            "summary": self.summary,
            "source_event_id": self.source_event_id,
            "schema_version": self.schema_version,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if _missing(self.episode_id):
            errors.append("missing episode_id")
        if _missing(self.source_event_id):
            errors.append("missing source_event_id")
        return errors


@dataclass(frozen=True)
class MemoryQuestion:
    question_id: str
    text: str
    status: str
    source_event_id: str
    type: str = "memory_question"
    schema_version: str = MEMORY_QUESTION_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "question_id": self.question_id,
            "text": self.text,
            "status": self.status,
            "source_event_id": self.source_event_id,
            "schema_version": self.schema_version,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if _missing(self.question_id):
            errors.append("missing question_id")
        if _missing(self.source_event_id):
            errors.append("missing source_event_id")
        return errors


@dataclass(frozen=True)
class MemoryFailure:
    failure_id: str
    failure_class: str
    kind: str
    context: str
    episode_id: str
    dispatch_id: str
    source_event_id: str
    type: str = "memory_failure"
    schema_version: str = MEMORY_FAILURE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "failure_id": self.failure_id,
            "failure_class": self.failure_class,
            "kind": self.kind,
            "context": self.context,
            "episode_id": self.episode_id,
            "dispatch_id": self.dispatch_id,
            "source_event_id": self.source_event_id,
            "schema_version": self.schema_version,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if _missing(self.failure_class):
            errors.append("missing failure_class")
        if _missing(self.kind):
            errors.append("missing kind")
        if _missing(self.source_event_id):
            errors.append("missing source_event_id")
        return errors


@dataclass(frozen=True)
class MemorySkillUse:
    skill_name: str
    skill_id: str
    execution_kind: str
    objective: str
    status: str
    source_event_id: str
    tool_sequence: tuple = ()
    files_changed: tuple = ()
    tests_run: tuple = ()
    outcome: str = ""
    episode_id: str = ""
    dispatch_id: str = ""
    type: str = "memory_skill_use"
    schema_version: str = MEMORY_SKILL_USE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "skill_name": self.skill_name,
            "skill_id": self.skill_id,
            "execution_kind": self.execution_kind,
            "objective": self.objective,
            "status": self.status,
            "tool_sequence": list(self.tool_sequence),
            "files_changed": list(self.files_changed),
            "tests_run": list(self.tests_run),
            "outcome": self.outcome,
            "episode_id": self.episode_id,
            "dispatch_id": self.dispatch_id,
            "source_event_id": self.source_event_id,
            "schema_version": self.schema_version,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if _missing(self.skill_name):
            errors.append("missing skill_name")
        if _missing(self.source_event_id):
            errors.append("missing source_event_id")
        return errors


# ── MemoryGraphSnapshot — the compiled-projection fingerprint ─────────────────
@dataclass(frozen=True)
class MemoryGraphSnapshot:
    snapshot_hash: str
    event_count: int
    entity_count: int
    claim_count: int
    node_count: int
    edge_count: int
    compiler_version: str
    schema_version_compiled: str
    config_hash: str
    type: str = "memory_graph_snapshot"
    schema_version: str = MEMORY_GRAPH_SNAPSHOT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "snapshot_hash": self.snapshot_hash,
            "event_count": self.event_count,
            "entity_count": self.entity_count,
            "claim_count": self.claim_count,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "compiler_version": self.compiler_version,
            "schema_version_compiled": self.schema_version_compiled,
            "config_hash": self.config_hash,
            "schema_version": self.schema_version,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if _missing(self.snapshot_hash):
            errors.append("missing snapshot_hash")
        if _missing(self.compiler_version):
            errors.append("missing compiler_version")
        if _missing(self.config_hash):
            errors.append("missing config_hash")
        for name in ("event_count", "entity_count", "claim_count", "node_count", "edge_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                errors.append(f"invalid count {name}: {value!r}")
        return errors


@dataclass(frozen=True)
class MemoryLeafV2:
    memory_id: str
    coord: tuple
    title: str = ""
    summary: str = ""
    source_id: str = ""
    source_type: str = ""
    span_start: int = 0
    span_end: int = 0
    span_hash: str = ""
    source_session_id: str = ""
    source_session_timestamp: str = ""
    sense_ids: tuple = ()
    status: str = "active"
    domain: str = ""
    subtopic: str = ""
    tags: tuple = ()
    contradiction: str = ""
    flagged: bool = False
    flag_reason: str = ""
    flag_response_mode: str = ""
    via_link: bool = False
    version: int = 1
    source_content_hash: str = ""
    workspace_source: dict = field(default_factory=dict)
    type: str = "memory_leaf_v2"
    schema_version: str = MEMORY_LEAF_V2_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "memory_id": self.memory_id,
            "coord": list(self.coord),
            "title": self.title,
            "summary": self.summary,
            "source_id": self.source_id,
            "source_type": self.source_type,
            "span_start": self.span_start,
            "span_end": self.span_end,
            "span_hash": self.span_hash,
            "source_session_id": self.source_session_id,
            "source_session_timestamp": self.source_session_timestamp,
            "sense_ids": list(self.sense_ids),
            "status": self.status,
            "domain": self.domain,
            "subtopic": self.subtopic,
            "tags": list(self.tags),
            "contradiction": self.contradiction,
            "flagged": self.flagged,
            "flag_reason": self.flag_reason,
            "flag_response_mode": self.flag_response_mode,
            "via_link": self.via_link,
            "version": self.version,
            "source_content_hash": self.source_content_hash,
            "workspace_source": dict(self.workspace_source),
            "schema_version": self.schema_version,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if _missing(self.memory_id):
            errors.append("missing memory_id")
        if not isinstance(self.coord, tuple) or len(self.coord) < 3:
            errors.append("coord must be a tuple with at least 3 items")
        if self.status not in MEMORY_LEAF_STATUSES:
            errors.append(f"unknown status: {self.status!r}")
        if not isinstance(self.span_start, int) or self.span_start < 0:
            errors.append("span_start must be a non-negative int")
        if not isinstance(self.span_end, int) or self.span_end < self.span_start:
            errors.append("span_end must be >= span_start")
        if not isinstance(self.sense_ids, tuple):
            errors.append("sense_ids must be a tuple")
        if not isinstance(self.workspace_source, dict):
            errors.append("workspace_source must be a dict")
        return errors


# ── RecallPacket — the deterministic recall result ────────────────────────────
@dataclass(frozen=True)
class RecallPacket:
    query: str
    query_hash: str
    query_kind: str
    snapshot_hash: str
    resolved_entities: tuple = ()
    results: tuple = ()
    evidence_event_ids: tuple = ()
    truncation_receipts: tuple = ()
    type: str = "recall_packet"
    schema_version: str = RECALL_PACKET_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "query": self.query,
            "query_hash": self.query_hash,
            "query_kind": self.query_kind,
            "resolved_entities": list(self.resolved_entities),
            "results": [dict(r) for r in self.results],
            "snapshot_hash": self.snapshot_hash,
            "evidence_event_ids": list(self.evidence_event_ids),
            "truncation_receipts": [dict(item) for item in self.truncation_receipts],
            "schema_version": self.schema_version,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if _missing(self.query_hash):
            errors.append("missing query_hash")
        if _missing(self.snapshot_hash):
            errors.append("missing snapshot_hash")
        # doctrine: a packet that returns results must cite evidence event IDs
        if self.results and not self.evidence_event_ids:
            errors.append("recall results without evidence_event_ids")
        return errors


@dataclass(frozen=True)
class RecallVerdict:
    status: str
    query: str
    answer_payload: dict | None = None
    evidence: tuple = ()
    rejected_hits: tuple = ()
    reason: str = ""
    # Contradicting singular claims quarantined out of the answer (the "operator name is
    # Kim" class) so one conflict never suppresses the co-recalled clean facts. First-class
    # + serialized so the planner and the pressure loop (memory_reconciliation) can read it.
    blocked_conflicts: tuple = ()
    # Bounded retrieval and candidate-truncation provenance for this verdict.
    retrieval_trace: dict = field(default_factory=dict)
    type: str = "recall_verdict"
    schema_version: str = RECALL_VERDICT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "status": self.status,
            "query": self.query,
            "answer_payload": dict(self.answer_payload or {})
            if isinstance(self.answer_payload, dict)
            else None,
            "evidence": [dict(item) if isinstance(item, dict) else item for item in self.evidence],
            "rejected_hits": [
                dict(item) if isinstance(item, dict) else item for item in self.rejected_hits
            ],
            "blocked_conflicts": [
                dict(item) if isinstance(item, dict) else item for item in self.blocked_conflicts
            ],
            "retrieval_trace": dict(self.retrieval_trace),
            "reason": self.reason,
            "schema_version": self.schema_version,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.status not in {"verified", "unknown", "ambiguous", "conflict"}:
            errors.append(f"unknown status: {self.status!r}")
        if _missing(self.query):
            errors.append("missing query")
        if not isinstance(self.evidence, tuple):
            errors.append("evidence must be a tuple")
        if not isinstance(self.rejected_hits, tuple):
            errors.append("rejected_hits must be a tuple")
        if not isinstance(self.blocked_conflicts, tuple):
            errors.append("blocked_conflicts must be a tuple")
        return errors

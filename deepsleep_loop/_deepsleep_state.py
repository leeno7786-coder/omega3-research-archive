"""DeepSleep carriers and outcome types (Slice 6a)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class DeepSleepInput:
    window_id: str
    window_reflection: dict
    episode_window: list
    trajectory_snapshot: dict
    omega_reasoning: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DeepSleepInput":
        return cls(
            window_id=str(d.get("window_id", "")),
            window_reflection=dict(d.get("window_reflection") or {}),
            episode_window=list(d.get("episode_window") or []),
            trajectory_snapshot=dict(d.get("trajectory_snapshot") or {}),
            omega_reasoning=dict(d.get("omega_reasoning") or {}),
        )

    @property
    def episode_id(self) -> str:
        """Compatibility alias — window-level DeepSleep uses window_id."""
        return self.window_id

    @property
    def omega_outcome(self) -> dict:
        return self.window_reflection

    @property
    def action_goal(self) -> dict:
        reflection = dict(self.window_reflection.get("reflection") or {})
        trajectory = dict(reflection.get("trajectory") or {})
        update = dict(self.window_reflection.get("trajectory_update") or {})
        dominant_intent = None
        for entry in self.episode_window or []:
            payload = entry if isinstance(entry, dict) else {}
            intent = payload.get("intent")
            if isinstance(intent, str) and intent.strip():
                dominant_intent = intent.strip()
                break
        return {
            "intent": dominant_intent or "WINDOW_REFLECT",
            "target": str(update.get("target_domain") or trajectory.get("target_domain") or ""),
            "target_domain": str(
                update.get("target_domain") or trajectory.get("target_domain") or ""
            ),
            "target_capability": str(
                update.get("target_capability") or trajectory.get("target_capability") or ""
            ),
        }

    @property
    def execution_pack(self) -> dict:
        for entry in self.episode_window or []:
            payload = entry if isinstance(entry, dict) else {}
            action = payload.get("action")
            if isinstance(action, str) and action.strip():
                return {"action": action.strip()}
        return {"action": "WINDOW_CONSOLIDATE"}

    @property
    def auto_outcome(self) -> dict:
        total = len(self.episode_window or [])
        successes = sum(1 for ep in self.episode_window if _episode_entry_success(ep))
        ratio = successes / total if total else 0.0
        result = {
            "status": "success" if ratio >= 0.5 else "partial",
            "cycle_kind": "success" if successes > 0 else "failure",
            "window_success_ratio": ratio,
            "window_episode_count": total,
        }
        output_candidates = []
        for entry in self.episode_window or []:
            payload = entry if isinstance(entry, dict) else {}
            outputs = payload.get("outputs")
            if isinstance(outputs, dict):
                output_candidates.append(outputs)
            auto = payload.get("auto")
            if isinstance(auto, dict) and isinstance(auto.get("outputs"), dict):
                output_candidates.append(dict(auto["outputs"]))

        for outputs in output_candidates:
            if outputs.get("knowledge_artifact_id"):
                result["outputs"] = dict(outputs)
                break
        else:
            for outputs in output_candidates:
                if any(
                    outputs.get(key) is not None
                    for key in (
                        "plugin_plan",
                        "plugin_step_results",
                        "plugin_permission_request",
                        "plugin_result",
                        "plugin_failure",
                    )
                ):
                    result["outputs"] = dict(outputs)
                    break
            else:
                if output_candidates:
                    result["outputs"] = dict(output_candidates[0])
        return result

    @property
    def meta_reflection(self) -> dict:
        return {}

    @property
    def global_outcome(self) -> dict:
        total = len(self.episode_window or [])
        successes = sum(1 for ep in self.episode_window if _episode_entry_success(ep))
        return {
            "outcome": {
                "goal_state": "completed" if successes >= max(1, total // 2) else "partial"
            },
            "observations": {
                "window_episode_count": total,
                "window_successes": successes,
            },
            "artifacts": {"episode_window": list(self.episode_window or [])},
        }


def _episode_entry_success(entry: Any) -> bool:
    """Whether DeepSleep may credit this episode: its TASK succeeded (WO-08, operator-assigned).

    The one field that answers it is ``outcome_axes.task_outcome`` (WO-06). Cascade completion
    does not, and neither does a completed action: a build that ran to the end and failed its
    own acceptance check is a failed task, and crediting it is the misreading the five axes
    exist to prevent. The old nested pack is not read here — there is no runtime dual-shape
    accessor. A body without the axes predates them and is an explicit migration requirement
    (``scripts/migrate_episode_continuity.py``), never a guessed verdict.
    """
    payload = entry if isinstance(entry, dict) else {}
    axes = payload.get("outcome_axes")
    if not isinstance(axes, dict) or "task_outcome" not in axes:
        from schema.continuity_json import CanonicalEpisodeUnavailable

        raise CanonicalEpisodeUnavailable(
            f"migration required: episode {payload.get('episode_id')!r} carries no outcome axes; "
            "a body written before WO-06 must be converted (scripts/migrate_episode_continuity.py) "
            "before DeepSleep can decide whether its task succeeded"
        )
    return str(axes.get("task_outcome") or "") == "success"


@dataclass(frozen=True)
class JournalEntry:
    episode_id: str
    timestamp: str
    intent: str | None
    action: str | None
    outcome: dict | None
    trajectory_snapshot: dict
    omega_reasoning: dict
    observations: dict
    artifacts: dict
    mastery_effects: dict = field(default_factory=dict)
    identity_effects: dict = field(default_factory=dict)
    semantic_effects: dict = field(default_factory=dict)
    trajectory_effects: dict = field(default_factory=dict)
    failure_effects: dict = field(default_factory=dict)
    query_effects: list = field(default_factory=list)
    scope: str | None = None
    global_outcome: dict = field(default_factory=dict)
    omega_result: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "JournalEntry":
        return cls(
            episode_id=str(d.get("episode_id", "")),
            timestamp=str(d.get("timestamp", "")),
            intent=d.get("intent"),
            action=d.get("action"),
            outcome=d.get("outcome") if isinstance(d.get("outcome"), dict) else None,
            trajectory_snapshot=dict(d.get("trajectory_snapshot") or {}),
            omega_reasoning=dict(d.get("omega_reasoning") or {}),
            observations=dict(d.get("observations") or {}),
            artifacts=dict(d.get("artifacts") or {}),
            mastery_effects=dict(d.get("mastery_effects") or {}),
            identity_effects=dict(d.get("identity_effects") or {}),
            semantic_effects=dict(d.get("semantic_effects") or {}),
            trajectory_effects=dict(d.get("trajectory_effects") or {}),
            failure_effects=dict(d.get("failure_effects") or {}),
            query_effects=list(d.get("query_effects") or []),
            scope=d.get("scope"),
            global_outcome=dict(d.get("global_outcome") or {}),
            omega_result=dict(d.get("omega_result") or {}),
        )


@dataclass(frozen=True)
class Trait:
    name: str
    strength: float = 0.0
    evidence_count: int = 0
    # POST-CHAT REFLECTION (2026-07-01): the most recent evidence sentence + status
    # ("new"|"confirmed"|"tensions_with_prior") for a trait sourced from an identity
    # reflection. Additive defaults keep window-emergent traits byte-identical.
    evidence: str = ""
    status: str = ""
    # Canonical trait-observation records already folded into this promoted
    # preference. Retries can then be recognized without adding fresh support.
    observation_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Trait":
        return cls(
            name=str(d.get("name", "")),
            strength=float(d.get("strength", 0.0) or 0.0),
            evidence_count=int(d.get("evidence_count", 0) or 0),
            evidence=str(d.get("evidence", "") or ""),
            status=str(d.get("status", "") or ""),
            observation_count=int(d.get("observation_count", 0) or 0),
        )


@dataclass(frozen=True)
class SemanticEntry:
    domain: str
    statement: str
    reinforcement_count: int = 0
    promoted_episode: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SemanticEntry":
        return cls(
            domain=str(d.get("domain", "")),
            statement=str(d.get("statement", "")),
            reinforcement_count=int(d.get("reinforcement_count", 0) or 0),
            promoted_episode=str(d.get("promoted_episode", "")),
        )


@dataclass(frozen=True)
class TrajectoryRecord:
    target_domain: str
    target_capability: str
    confidence: float = 0.0
    reinforcement_count: int = 0
    supporting_episodes: list = field(default_factory=list)
    status: str = "active"
    passes_since_reinforcement: int = 0
    # WO-07: the terminus pass that reinforced this record, keyed by stable effect id. The whole
    # pass (decay, reinforcement, supersession) is one atomic save, so the receipt on the record
    # it reinforced proves the entire pass landed; a retried terminus skips it.
    effect_receipts: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TrajectoryRecord":
        return cls(
            target_domain=str(d.get("target_domain", "")),
            target_capability=str(d.get("target_capability", "")),
            confidence=float(d.get("confidence", 0.0) or 0.0),
            reinforcement_count=int(d.get("reinforcement_count", 0) or 0),
            supporting_episodes=list(d.get("supporting_episodes") or []),
            status=str(d.get("status", "active") or "active"),
            passes_since_reinforcement=int(d.get("passes_since_reinforcement", 0) or 0),
            effect_receipts=dict(d.get("effect_receipts") or {}),
        )


@dataclass(frozen=True)
class FailurePattern:
    kind: str
    context: str
    domain_id: str = ""
    frequency: int = 0
    last_episode: str = ""
    # WO-07: the effect receipts this pattern carries, keyed by stable effect id and written in
    # the same atomic upsert as the count they account for. A retried terminus that finds its
    # receipt reconciles instead of counting the same failure twice.
    effect_receipts: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FailurePattern":
        context = str(d.get("context", ""))
        domain_id = str(d.get("domain_id", "") or "").strip()
        if not domain_id and context:
            from cognition.topic_resolution.domain_key import canonical_domain_key

            domain_id = canonical_domain_key(context)
        return cls(
            kind=str(d.get("kind", "")),
            context=context,
            domain_id=domain_id,
            frequency=int(d.get("frequency", 0) or 0),
            last_episode=str(d.get("last_episode", "")),
            effect_receipts=dict(d.get("effect_receipts") or {}),
        )


@dataclass(frozen=True)
class OpenQuery:
    query_id: str
    question: str
    domain: str
    created_episode: str
    confidence: float = 0.0
    status: str = "open"
    created_timestamp: str = ""
    resolution_episode: str | None = None
    resolution_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OpenQuery":
        resolution_episode = d.get("resolution_episode")
        resolution_reason = d.get("resolution_reason")
        return cls(
            query_id=str(d.get("query_id", "")),
            question=str(d.get("question", "")),
            domain=str(d.get("domain", "")),
            created_episode=str(d.get("created_episode", "")),
            confidence=float(d.get("confidence", 0.0) or 0.0),
            status=str(d.get("status", "open") or "open"),
            created_timestamp=str(d.get("created_timestamp", "") or ""),
            resolution_episode=(
                str(resolution_episode) if resolution_episode is not None else None
            ),
            resolution_reason=(str(resolution_reason) if resolution_reason is not None else None),
        )


@dataclass(frozen=True)
class MasteryRecord:
    domain_id: str
    domain_name: str
    knowledge_mastery: float = 0.0
    application_mastery: float = 0.0
    confidence: float = 0.0
    evidence_count: int = 0
    subdomains: dict = field(default_factory=dict)
    target_mastery: float = 0.70
    status: str = "learning"
    # Step 5 — bounded rolling per-domain improvement trace (Er(t)), the substrate
    # for the learning-progress plateau signal. Appended at _apply_grounded_mastery,
    # read by pressure_loop.learning_progress.plateau_from. evidence_count is the
    # sample counter n_i. All default-safe so old mastery.json loads unchanged.
    recent_knowledge_deltas: tuple = ()
    recent_application_deltas: tuple = ()
    last_plateau_flag: bool = False
    last_plateau_episode: str = ""
    # DeepSleep can retry after a later maintenance failure; this key prevents
    # the same accepted practice report from crediting mastery twice.
    last_practice_episode: str = ""
    # Mastery-as-subcat tree: the parent capability slug this subcat lives UNDER
    # (e.g. "async python" -> parent "python coding"). "" = a top-level subcat. The
    # mind GROWS the tree by naming a child subtopic under a broad parent; onboarding
    # seeds top-level subcats. Default-safe so old mastery.json loads unchanged.
    parent: str = ""
    # WO-07: effect receipts keyed by stable effect id (deepsleep_loop.effect_receipts), each
    # written in the SAME atomic upsert as the credit it accounts for. A retried terminus that
    # finds its receipt reconciles from it — no second credit, and the effect it reports is the
    # one the first pass applied, not a zero delta recomputed from the moved store.
    effect_receipts: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MasteryRecord":
        return cls(
            domain_id=str(d.get("domain_id", "")),
            domain_name=str(d.get("domain_name", "")),
            knowledge_mastery=float(d.get("knowledge_mastery", 0.0) or 0.0),
            application_mastery=float(d.get("application_mastery", 0.0) or 0.0),
            confidence=float(d.get("confidence", 0.0) or 0.0),
            evidence_count=int(d.get("evidence_count", 0) or 0),
            subdomains=dict(d.get("subdomains") or {}),
            target_mastery=float(d.get("target_mastery", 0.70) or 0.70),
            status=str(d.get("status", "learning") or "learning"),
            recent_knowledge_deltas=tuple(
                float(x) for x in (d.get("recent_knowledge_deltas") or ())
            ),
            recent_application_deltas=tuple(
                float(x) for x in (d.get("recent_application_deltas") or ())
            ),
            last_plateau_flag=bool(d.get("last_plateau_flag", False)),
            last_plateau_episode=str(d.get("last_plateau_episode", "") or ""),
            last_practice_episode=str(d.get("last_practice_episode", "") or ""),
            parent=str(d.get("parent", "") or ""),
            effect_receipts=dict(d.get("effect_receipts") or {}),
        )


@dataclass(frozen=True)
class ProjectionState:
    identity_projection: dict = field(default_factory=dict)
    mastery_projection: dict = field(default_factory=dict)
    trajectory_projection: dict = field(default_factory=dict)
    memory_integrity_projection: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProjectionState":
        return cls(
            identity_projection=dict(d.get("identity_projection") or {}),
            mastery_projection=dict(d.get("mastery_projection") or {}),
            trajectory_projection=dict(d.get("trajectory_projection") or {}),
            memory_integrity_projection=dict(d.get("memory_integrity_projection") or {}),
        )


def _optional_dict_field(d: dict[str, Any], key: str) -> dict | None:
    if key not in d:
        return None
    value = d.get(key)
    if value is None:
        return None
    return dict(value or {})


@dataclass(frozen=True)
class DeepSleepOutcomePack:
    episode_id: str
    journal_entry: dict
    updated_memory: dict
    updated_identity: dict
    updated_mastery: dict
    updated_open_queries: dict
    updated_trajectory: dict
    pressure_seed: list
    projection_state: dict
    episode_closed: bool
    pressure_reset: bool
    temporal: dict = None
    learning_state: dict = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DeepSleepOutcomePack":
        journal_entry = dict(d.get("updated_memory") or d.get("journal_entry") or {})
        return cls(
            episode_id=str(d.get("episode_id", "")),
            journal_entry=journal_entry,
            updated_memory=journal_entry,
            updated_identity=dict(d.get("updated_identity") or {}),
            updated_mastery=dict(d.get("updated_mastery") or {}),
            updated_open_queries=dict(d.get("updated_open_queries") or {}),
            updated_trajectory=dict(d.get("updated_trajectory") or {}),
            pressure_seed=list(d.get("pressure_seed") or []),
            projection_state=dict(d.get("projection_state") or {}),
            episode_closed=bool(d.get("episode_closed", False)),
            pressure_reset=bool(d.get("pressure_reset", False)),
            temporal=_optional_dict_field(d, "temporal"),
            learning_state=_optional_dict_field(d, "learning_state"),
        )

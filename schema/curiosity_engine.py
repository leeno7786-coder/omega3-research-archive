"""
Curiosity Engine Schema (v3 — canonical)
=========================================

The output schema of the Curiosity Engine — Omega's gap-detection
+ unresolved-pressure-generation layer. Per
`docs/contracts/03_curiosity_development.md` § 12 (Curiosity Engine,
v3 — supersedes the v1 `curiosity_state.json` from
`01_schema_contract.md` § 7).

Curiosity exists to answer: *"What is unresolved, and why should I
care?"*

Per the Cognitive Doctrine:

    *Curiosity is not random exploration. Curiosity is weighted
    incompleteness resolution. Unresolved states create pressure.
    Pressure creates action. That is synthetic curiosity.*

Each instance represents one identified incompleteness — a memory
gap, a contradiction, a repeated failure, a missing tool, a
low-confidence reasoning, etc. — together with its scored
significance and a recommendation for how to resolve it (or why to
defer).

Used by:
    - Curiosity Engine substrate (canonical producer; one per
      detected gap)
    - Autonomous Loop (consumes high-tension entries to decide
      proactive activation per `autonomous_activation` schema)
    - Development Engine (consumes recurring entries to propose
      capability creation)
    - DeepSleep (consumes the open-question store for consolidation)

ARCHITECTURAL RULES:
    - All four scores (tension, resolution, urgency, persistence)
      are computed per the deterministic formulas in
      `docs/contracts/04_scoring_contract.md`. No model-driven
      scoring.
    - `should_pursue == False` MUST be accompanied by a non-empty
      `ignore_reason` per the Failure Flow Law: non-pursuit is still
      a decision, and "do nothing without explanation is drift."
    - `recommended_path` MUST be one of the allowed values from
      `config/curiosity.yaml :: resolution_paths.allowed`.
      Forbidden paths (policy_override, identity_mutation, etc.)
      cannot appear here even if produced — Guardian rejects them.
    - This schema MUST NOT be redefined elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional
import logging

log = logging.getLogger("omega.schema.curiosity_engine")


TriggerSource = Literal[
    "memory_gap",
    "contradiction",
    "repeated_failure",
    "uncertainty",
    "operator_pattern",
    "anomaly",
]


GapType = Literal[
    "knowledge",
    "capability",
    "tool",
    "identity",
    "policy",
    "verification",
]


ResolutionPath = Literal[
    "ask_user",
    "web_search",
    "internal_test",
    "simulation",
    "tool_creation",
    "defer",
]


CuriosityStatus = Literal[
    "open",
    "active",
    "resolved",
    "deferred",
    # SLICE-10: a brain cycle initiated for this curiosity hit a posture
    # that requires operator attention (security_block, identity_boundary,
    # omega_lock pause). The entry stays out of the live sweep until the
    # operator surface re-routes it.
    "escalated",
]


RegistryScope = Literal[
    "cognitive",
    "dev",
]


# Reflective Lineage Conservation Doctrine — Law 2 Resolution Class
# Separation (the false-mastery firewall). HARD doctrine:
# `provisional_internal` can NEVER overwrite/become `operator_verified`
# — no merge, no inference upgrade, no "confidence becomes truth". The
# upgrade pathway must not exist (enforced structurally in
# core.reflective_lineage). None = unresolved.
ResolutionClass = Literal[
    "operator_verified",  # externally ground-truth-confirmed; reopenable:false
    "provisional_internal",  # internal coherence only; reopenable; lower confidence
]


def damp_pressure(
    pressure_prev: float,
    pressure_next: float,
    bounded_delta: float,
    *,
    catastrophic: bool = False,
) -> float:
    """Curiosity acceleration damping — FINAL VERDICT hardening #3.

    Enforces the invariant
    ``pressure_next <= pressure_prev + bounded_delta`` UNLESS a
    catastrophic failure class overrides it. Prevents runaway pressure
    saturation while a catastrophe is still allowed to spike.

    Deterministic / pure (CURIOSITY authority = deterministic pressure
    computation ONLY). This is an ASYMMETRIC upper bound: a pressure
    *decrease* is never damped (damping a drop would trap the system
    in false high-pressure) — ``min`` returns ``pressure_next``
    untouched when it is below ``pressure_prev``.
    """
    if bounded_delta < 0:
        # A negative increment is a caller bug, not a value to coerce
        # away (Failure-Is-Signal). Stay total AND honest.
        raise ValueError(f"bounded_delta must be >= 0, got {bounded_delta!r}")
    if catastrophic:
        return float(pressure_next)
    return min(float(pressure_next), float(pressure_prev) + float(bounded_delta))


@dataclass(frozen=True)
class CuriosityEngineEntry:
    """One identified incompleteness, scored and routed."""

    # Identity
    cycle_id: str
    curiosity_id: str
    created_at: str  # ISO-8601 UTC

    # What was detected
    trigger_source: TriggerSource
    gap_type: GapType
    problem_statement: str

    # Scoring (all [0, 1] per docs/contracts/04_scoring_contract.md)
    tension_score: float
    resolution_value: float
    urgency_score: float
    persistence_score: float
    resource_cost_estimate: float

    # Decision
    recommended_path: ResolutionPath
    should_pursue: bool
    status: CuriosityStatus = "open"

    # Required iff should_pursue is False (Failure Flow Law)
    ignore_reason: Optional[str] = None

    # Routing
    requires_operator_input: bool = False

    # SLICE-10: how many brain cycles have been attempted for this entry.
    # Incremented by `mark_failed_retry`. When the count reaches the
    # configured retry cap the entry transitions to `deferred` instead
    # of being re-opened for another sweep.
    attempt_count: int = 0

    # Tool synthesis target. Defaults to cognitive for backward
    # compatibility; dev/coding tool curiosities explicitly set "dev" so
    # Omega can synthesize without guessing from prose.
    registry_scope: RegistryScope = "cognitive"

    # ── Reflective Lineage metadata (S1) ──────────────────────────────
    # ADDITIVE + defaulted: CuriosityEngineEntry(**old_blob) (curiosity_
    # store.py:119) still loads — pre-lineage entries get these defaults
    # (forward-compat, the #733 from_dict lesson). Lineage is METADATA
    # extending the cured mortality substrate; it does NOT own this
    # entry's lifecycle — `curiosity_store.update_status` remains the
    # singular lifecycle seam. core.reflective_lineage reads/sets these
    # for the 5 Hard Laws; the existing mark_resolved/deferred/escalated
    # paths stay the constitutional terminal authority.
    #
    # Law 1 — Shared Lineage Budget. lineage_id groups a root reflective
    # question + all its split/refine descendants onto ONE finite pool;
    # every member consumes from the same `lineage_consumed`/budget.
    lineage_id: Optional[str] = None
    lineage_parent_id: Optional[str] = None  # ancestry (Law 4 collapse)
    lineage_max_budget: int = 12  # Law 1 (config override)
    lineage_consumed: int = 0  # Law 1 shared pool counter
    # Law 2 — Resolution Class. None until resolved. provisional_internal
    # NEVER upgrades to operator_verified (firewall in reflective_lineage).
    resolution_class: Optional[ResolutionClass] = None
    # Law 4 — Bounded Held Escalation Pool.
    lineage_held: bool = False
    lineage_salience: float = 0.0  # held sort / Law 5 slope input
    # Law 3 — Refractory Escalation Windows.
    escalation_count: int = 0
    last_escalation_at: Optional[str] = None  # ISO-8601 UTC; refractory gate

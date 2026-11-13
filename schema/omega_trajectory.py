"""
Omega Trajectory — long-arc directional pressure
==================================================

A trajectory is OMEGA's mechanism for steering the system across
multiple cycles. Where global emits per-cycle goals, omega emits
multi-epoch trajectories — "push toward X for the next N cycles."

Per operator directive 2026-05-14:

    "make a trajectory goal that omega puts in the shared state cycle
     that global sees every cycle, that deep records to the journal so
     omega can influence its goal driven trajectory long term."

The trajectory acts as INFLUENCE on global's goal formulation, not
as override. Global retains autonomy; the trajectory boosts the
score of candidates aligned with its target_domain / target_capability.

Cascade flow:

    OMEGA emits trajectory
       ├─ writes identity lobe [0, 6, *] (type=omega_trajectory, immutable)
       └─ writes LAST_OMEGA_TRAJECTORY shared-state slot (live signal)

    NEXT EPOCH:
      AUTO   unaware
      META   unaware
      GLOBAL reads LAST_OMEGA_TRAJECTORY → boosts aligned candidates
             records trajectory_attempt_outcome in envelope.telemetry
      DEEPSLEEP snapshots LAST_OMEGA_TRAJECTORY into journal upstream
                does NOT clear (trajectory is multi-epoch by design)
      OMEGA reads journal + identity → decides KEEP/MODIFY/SUPERSEDE/COMPLETE

Lifecycle rules:
    * One active trajectory at a time (focus over breadth)
    * Omega is the SOLE writer (global reads, deepsleep snapshots)
    * Decrement-on-emit: cycles_remaining decrements once per omega cycle
    * Influence not override (configurable boost magnitude in policy)
    * Tool-synth → trajectory is the primary emission path

ARCHITECTURAL RULES:
    * The dataclass shape is FROZEN. Add fields only at end.
    * `intent` and `decisions_available` use string Literals so the set
      is enforceable; new values require a schema edit.
    * to_dict / from_dict round-trip losslessly.
    * Validators in core.shared_state_keys check shape on slot writes.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional, Tuple
import logging

log = logging.getLogger("omega.schema.omega_trajectory")


TrajectoryIntent = Literal[
    "exercise_new_tool",  # tool just synthesized — go use it
    "explore_domain",  # mastery gap — push toward unexplored area
    "investigate_anomaly",  # something looked off — pursue it
    "deepen_capability",  # known territory — go deeper, not wider
]


TrajectoryCompletionReason = Literal[
    "",  # still active
    "persistence_exhausted",  # cycles_remaining hit 0 naturally
    "superseded",  # omega replaced with a new trajectory
    "stalled_no_alignment",  # global ignored for too many cycles
    "target_reached",  # omega evaluated success
    "operator_closed",  # operator explicitly closed it
]


# Per-window trajectory decision (operator spec 2026-06-12): the trajectory is
# a SETPOINT Omega owns via an explicit decision each reflection window;
# pressure is the steering. Carried on the reflection JSON as
# trajectory.decision and on TrajectoryUpdate.decision.
TrajectoryDecision = Literal[
    "establish",  # window 1 — no trajectory exists yet; create the initial one
    "idle",  # aligned / on-track — keep the trajectory unchanged
    "alter",  # new input shifts the target — modify in place (same id)
    "abandon",  # drift too substantial — supersede the old + create a new one
    "complete",  # mind judges the focus MASTERED — verified vs the mastery store,
    # then completed (target_reached) + reported to the operator. The
    # terminal-by-judgment transition (abandon = drift, NOT done). The
    # transition is mastery-gated: the model judges and deterministic code verifies.
]

TRAJECTORY_DECISION_VALUES: Tuple[str, ...] = (
    "establish",
    "idle",
    "alter",
    "abandon",
    "complete",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class OmegaTrajectory:
    """One trajectory — multi-epoch directional pressure from omega.

    Identity:
        trajectory_id   stable uuid; supersedes chain via superseded_by
        cycle_id        omega cycle that emitted it
        epoch_emitted   epoch number at emission
        timestamp       ISO-8601 UTC

    Direction:
        intent              what kind of push (see TrajectoryIntent)
        target_domain       e.g., "computer_science", "biology" (matches
                            global goal.domain)
        target_capability   e.g., "web_fetch", "sandbox_exec" (matches
                            tool name; substring-matched in candidate
                            descriptions for fuzzy alignment)
        rationale           why omega wants this direction (free text)
        influencer_text     short string global interpolates into its
                            synthesizer's reasoning context

    Lifecycle:
        persistence_cycles  total epochs the trajectory should ride
        cycles_remaining    decremented each omega cycle; 0 → auto-complete
        superseded_by       trajectory_id of replacement, if any
        completed           True when terminal
        completion_reason   why it ended (see TrajectoryCompletionReason)
    """

    trajectory_id: str
    cycle_id: str
    epoch_emitted: int
    timestamp: str

    intent: TrajectoryIntent
    target_domain: str
    target_capability: str
    rationale: str
    influencer_text: str

    persistence_cycles: int
    cycles_remaining: int

    superseded_by: Optional[str] = None
    completed: bool = False
    completion_reason: TrajectoryCompletionReason = ""

    def __post_init__(self) -> None:
        if not isinstance(self.trajectory_id, str) or not self.trajectory_id:
            raise ValueError("OmegaTrajectory.trajectory_id must be a non-empty string")
        if not isinstance(self.cycle_id, str) or not self.cycle_id:
            raise ValueError("OmegaTrajectory.cycle_id must be a non-empty string")
        if not isinstance(self.epoch_emitted, int) or self.epoch_emitted < 0:
            raise ValueError("OmegaTrajectory.epoch_emitted must be a non-negative int")
        if self.intent not in (
            "exercise_new_tool",
            "explore_domain",
            "investigate_anomaly",
            "deepen_capability",
        ):
            raise ValueError(f"OmegaTrajectory.intent unrecognized: {self.intent!r}")
        if not isinstance(self.persistence_cycles, int) or self.persistence_cycles < 1:
            raise ValueError("OmegaTrajectory.persistence_cycles must be >= 1")
        if not isinstance(self.cycles_remaining, int) or self.cycles_remaining < 0:
            raise ValueError("OmegaTrajectory.cycles_remaining must be >= 0")
        if self.completed and not self.completion_reason:
            raise ValueError("OmegaTrajectory.completion_reason required when completed=True")

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict — what gets written to identity
        lobe payload + shared_state slot. Round-trips losslessly via
        from_dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, blob: Dict[str, Any]) -> "OmegaTrajectory":
        """Reconstruct from a serialised dict. Tolerates extra fields
        (forward-compat)."""
        if not isinstance(blob, dict):
            raise TypeError(f"OmegaTrajectory.from_dict expects dict, got {type(blob).__name__}")
        return cls(
            trajectory_id=str(blob.get("trajectory_id") or ""),
            cycle_id=str(blob.get("cycle_id") or ""),
            epoch_emitted=int(blob.get("epoch_emitted") or 0),
            timestamp=str(blob.get("timestamp") or _now_iso()),
            intent=str(blob.get("intent") or "explore_domain"),  # type: ignore
            target_domain=str(blob.get("target_domain") or ""),
            target_capability=str(blob.get("target_capability") or ""),
            rationale=str(blob.get("rationale") or ""),
            influencer_text=str(blob.get("influencer_text") or ""),
            persistence_cycles=int(blob.get("persistence_cycles") or 5),
            cycles_remaining=int(blob.get("cycles_remaining") or 0),
            superseded_by=blob.get("superseded_by") or None,
            completed=bool(blob.get("completed") or False),
            completion_reason=str(blob.get("completion_reason") or ""),  # type: ignore
        )

    def is_active(self) -> bool:
        """True iff trajectory should still influence global selection."""
        return (not self.completed) and self.cycles_remaining > 0

    def is_expiring_next_cycle(self) -> bool:
        """True iff this is the last cycle before auto-completion."""
        return self.is_active() and self.cycles_remaining <= 1


def make_trajectory(
    *,
    cycle_id: str,
    epoch_emitted: int,
    intent: TrajectoryIntent,
    target_domain: str,
    target_capability: str,
    rationale: str,
    influencer_text: str = "",
    persistence_cycles: int = 5,
    timestamp: Optional[str] = None,
) -> OmegaTrajectory:
    """Construct an OmegaTrajectory with sensible defaults.

    Auto-generates trajectory_id (uuid4 hex first 12 chars).
    cycles_remaining initialises to persistence_cycles.
    influencer_text defaults to a generated sentence when empty.

    Use this from omega phase_4_commit / trajectory_store; do not
    instantiate OmegaTrajectory directly.
    """
    import uuid

    tid = uuid.uuid4().hex[:12]

    if not influencer_text:
        if intent == "exercise_new_tool":
            influencer_text = (
                f"A new {target_capability!r} tool was just synthesized; "
                f"bias goal formation toward {target_domain or 'its target territory'} "
                f"for the next {persistence_cycles} cycles to validate the tool."
            )
        elif intent == "explore_domain":
            influencer_text = (
                f"Push toward {target_domain!r} for the next "
                f"{persistence_cycles} cycles — capability mastery gap detected."
            )
        elif intent == "investigate_anomaly":
            influencer_text = (
                f"Investigate {target_domain or target_capability!r} for the "
                f"next {persistence_cycles} cycles — anomaly surfaced."
            )
        elif intent == "deepen_capability":
            influencer_text = (
                f"Go deeper on {target_capability or target_domain!r} for the "
                f"next {persistence_cycles} cycles — known territory worth deepening."
            )

    return OmegaTrajectory(
        trajectory_id=tid,
        cycle_id=cycle_id,
        epoch_emitted=epoch_emitted,
        timestamp=timestamp or _now_iso(),
        intent=intent,
        target_domain=target_domain,
        target_capability=target_capability,
        rationale=rationale,
        influencer_text=influencer_text,
        persistence_cycles=persistence_cycles,
        cycles_remaining=persistence_cycles,
    )


def modify_trajectory(old: OmegaTrajectory, **fields: Any) -> OmegaTrajectory:
    """Return a copy of `old` with the given fields changed IN PLACE — the
    `alter` decision. The trajectory keeps its identity: same trajectory_id,
    same supersedes chain. Use supersede_trajectory for the `abandon`
    decision (new identity)."""
    from dataclasses import replace

    if "trajectory_id" in fields:
        raise ValueError(
            "modify_trajectory cannot change trajectory_id — alter keeps the "
            "trajectory's identity; use supersede_trajectory to replace it"
        )
    return replace(old, **fields)


def supersede_trajectory(
    old: OmegaTrajectory,
    new: OmegaTrajectory,
) -> OmegaTrajectory:
    """Return a copy of `old` marked superseded by `new`. Caller persists
    the result to identity lobe (history) AND persists `new` (active).
    `new.superseded_by` is intentionally NOT linked back to old — the
    chain points old→new only, so omega can walk the supersedes graph
    by reading old records and following superseded_by forward."""
    from dataclasses import replace

    return replace(
        old,
        completed=True,
        completion_reason="superseded",
        superseded_by=new.trajectory_id,
    )


def complete_trajectory(
    trajectory: OmegaTrajectory,
    *,
    reason: TrajectoryCompletionReason,
) -> OmegaTrajectory:
    """Return a copy marked complete with the given reason."""
    from dataclasses import replace

    if not reason:
        raise ValueError("complete_trajectory requires non-empty reason")
    return replace(
        trajectory,
        completed=True,
        completion_reason=reason,
        cycles_remaining=0,
    )


def decrement_trajectory(trajectory: OmegaTrajectory) -> OmegaTrajectory:
    """Return a copy with cycles_remaining decremented by 1. If the
    decrement reaches 0, also marks completed with reason
    "persistence_exhausted"."""
    from dataclasses import replace

    new_remaining = max(0, trajectory.cycles_remaining - 1)
    if new_remaining == 0:
        return replace(
            trajectory,
            cycles_remaining=0,
            completed=True,
            completion_reason="persistence_exhausted",
        )
    return replace(trajectory, cycles_remaining=new_remaining)

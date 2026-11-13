"""DeepSleep Phase 8 — pressure reseed contributor generation."""

from __future__ import annotations

from typing import Any, List, Sequence

from deepsleep_loop._deepsleep_state import DeepSleepInput, MasteryRecord
from pressure_loop.contributors import Contributor, make_contributor


_RESEED_LANE_BY_TYPE = {
    "application_gap": "agent",
    "capability_gap": "agent",
    "confidence_reinforcement": "agent",
    "curiosity_gap": "agent",
    "epistemic_emptiness": "agent",
    "execution_continuation": "agent",
    "execution_gap": "agent",
    "incomplete_task": "agent",
    "learning_debt": "agent",
    "mastery_gap": "agent",
    "memory_gap": "agent",
    "repair_needed": "agent",
    "tool_failure": "agent",
    "verification_needed": "agent",
    "clarification_gap": "chat",
    "identity_gap": "chat",
    "memory_reconciliation": "chat",
    "operator_priority": "chat",
    "proactive_update": "chat",
    "requires_operator_input": "chat",
    "sense_resolution": "chat",
    "trajectory_gap": "chat",
    "window_reflection_ready": "window",
}


def _reseed_lane(ctype: str, metadata: dict[str, Any]) -> str:
    """Bind each reseeded contributor to an explicit, validated semantic lane."""
    from core.pipelines import validate_lane

    lane = metadata.get("lane", _RESEED_LANE_BY_TYPE.get(ctype))
    if lane is None:
        raise ValueError(f"Omega reseed contributor {ctype!r} has no semantic lane")
    return validate_lane(lane)


def _emit_reseed(phase: str, **fields: Any) -> None:
    try:
        from guardian.terminal_feedback import emit_data

        emit_data("deepsleep", phase, **fields)
    except Exception:
        pass


def _trajectory_pressure_enabled() -> bool:
    """DECOUPLED by default (operator 2026-06-30): the window's self-authored trajectory must
    NOT adjust pressure — it INFLUENCES via the per-episode prompt (foldback trajectory_influencer)
    + steers TOPIC SELECTION (resolve_topic). Omega drives all pressure per episode. Set
    OMEGA_TRAJECTORY_PRESSURE=1 to restore the legacy trajectory_gap / next_inquiry reseed
    coupling. The steer telemetry (trajectory_steer_rule) is unaffected — only the contributor
    emission is gated."""
    import os

    return os.environ.get("OMEGA_TRAJECTORY_PRESSURE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def generate_reseed(
    ds_input: DeepSleepInput,
    episode: Any = None,
    *,
    mastery_records: Sequence[MasteryRecord] | None = None,
) -> List[Contributor]:
    """Project only Omega-authored successor decisions into the next cycle.

    DeepSleep updates mastery, memory, taxonomy, and integrity state, but those
    maintenance results do not own cognition.  ``mastery_records`` remains in
    the signature for compatibility with the window sequencer; the records are
    context for Omega's fold, not deterministic pressure producers.
    """
    del episode, mastery_records
    contributors: list[Contributor] = []
    omega = ds_input.omega_outcome or {}
    reflection = dict(omega.get("reflection") or {})
    trajectory = dict(reflection.get("trajectory") or {})
    trajectory_update = dict(omega.get("trajectory_update") or {})

    # Steer by Omega's owned trajectory decision (operator spec 2026-06-12,
    # per-window refresh): establish/alter/abandon point pressure at the
    # (new) target; idle is minimal when the window FOLLOWED but reinforces
    # toward the set trajectory when the window drifted off-path. Legacy
    # packs without a decision fall back to continuation_recommended.
    decision = str(trajectory_update.get("decision") or "").strip().lower()
    trajectory_diff = str(omega.get("trajectory_diff") or "").strip().upper()
    if decision in {"establish", "alter", "abandon"}:
        steer = True
        steer_rule = "explicit_decision"
    elif decision == "idle":
        steer = trajectory_diff in {"PARTIAL_FOLLOWING", "DRIFTING"}
        steer_rule = "idle_reinforce" if steer else "idle_minimal"
    else:
        steer = bool(
            trajectory_update.get("continuation_recommended")
            or trajectory.get("continuation_recommended")
        )
        steer_rule = "legacy_continuation" if steer else "legacy_none"
    _emit_reseed(
        "trajectory_steer_rule",
        summary=f"rule={steer_rule}",
        rule=steer_rule,
        steer=steer,
        trajectory_decision=decision or "(none)",
        trajectory_diff=trajectory_diff or "(none)",
    )
    if steer and _trajectory_pressure_enabled():
        weight_raw = trajectory.get("next_learning_pressure", 0.5)
        try:
            weight = max(0.0, min(1.0, float(weight_raw)))
        except (TypeError, ValueError):
            weight = 0.5
        if weight <= 0.001:
            # Steering was decided — a zero-weight contributor is a non-action
            # (prompt-compliant idle packs carry next_learning_pressure 0.0).
            weight = 0.5
            _emit_reseed(
                "trajectory_weight_floor",
                summary="zero steering weight floored to 0.5",
                original_weight=weight_raw,
                floored_weight=0.5,
            )
        # Prompt-compliant idle packs leave the trajectory targets EMPTY (the
        # prompt fills them on non-idle only) — the reinforce case steers
        # toward the SET trajectory from the window snapshot.
        snapshot = dict(ds_input.trajectory_snapshot or {}) if decision == "idle" else {}
        contributors.append(
            make_contributor(
                "omega",
                "trajectory_gap",
                weight,
                {
                    "lane": "chat",
                    "target_domain": str(
                        trajectory_update.get("target_domain")
                        or trajectory.get("target_domain")
                        or snapshot.get("target_domain")
                        or ""
                    ),
                    "target_capability": str(
                        trajectory_update.get("target_capability")
                        or trajectory.get("target_capability")
                        or snapshot.get("target_capability")
                        or ""
                    ),
                    "fallback_to_snapshot": bool(snapshot)
                    and not (
                        trajectory_update.get("target_domain") or trajectory.get("target_domain")
                    ),
                    "source": "omega_trajectory_fold",
                    "origin": "omega_trajectory_update",
                },
            )
        )

    try:
        inquiry_raw = trajectory.get("next_inquiry_pressure", 0.0)
        inquiry_weight = max(0.0, min(1.0, float(inquiry_raw)))
    except (TypeError, ValueError):
        inquiry_weight = 0.0
    if inquiry_weight > 0.001 and _trajectory_pressure_enabled():
        contributors.append(
            make_contributor(
                "omega",
                "curiosity_gap",
                inquiry_weight,
                {
                    "lane": "agent",
                    "source": "omega_trajectory_fold",
                    "origin": "next_inquiry_pressure",
                },
            )
        )

    feedback = dict(reflection.get("pressure_feedback") or {})
    omega_raise_create = 0
    for key in ("contributors_to_raise", "contributors_to_create"):
        for entry in feedback.get(key) or []:
            if not isinstance(entry, dict):
                continue
            ctype = str(entry.get("type") or "").strip()
            if not ctype:
                continue
            try:
                weight = max(0.0, min(1.0, float(entry.get("weight", 0.0) or 0.0)))
            except (TypeError, ValueError):
                continue
            if weight <= 0.001:
                continue
            meta: dict[str, Any] = {"origin": "omega_pressure_feedback"}
            # The mind's actual words ride the contributor: an
            # operator_priority entry carries the QUESTION the reflection
            # formed for the operator, so a winning ask reaches Meta A /
            # the prompt pack with real content (metadata["message"] is the
            # canonical dominant-text key). Nothing is fabricated when the
            # model attached no question.
            question = str(entry.get("question") or entry.get("message") or "").strip()
            if question:
                meta["message"] = question
                meta["question"] = question
            if "lane" in entry:
                meta["lane"] = entry["lane"]
            meta["lane"] = _reseed_lane(ctype, meta)
            contributors.append(make_contributor("omega", ctype, weight, meta))
            omega_raise_create += 1
    if omega_raise_create:
        _emit_reseed(
            "omega_pressure_feedback",
            summary=f"staged={omega_raise_create}",
            staged_count=omega_raise_create,
            action="raise_or_create",
        )

    suppress_count = 0
    for entry in feedback.get("contributors_to_lower") or []:
        if not isinstance(entry, dict):
            continue
        ctype = str(entry.get("type") or "").strip()
        if not ctype:
            continue
        try:
            weight = max(0.0, min(1.0, float(entry.get("weight", 0.0) or 0.0)))
        except (TypeError, ValueError):
            continue
        if weight <= 0.001:
            continue
        from pressure_loop.pressure import stage_reseed_suppress

        stage_reseed_suppress(ctype, weight)
        suppress_count += 1
    if suppress_count:
        _emit_reseed(
            "stage_reseed_suppress",
            summary=f"suppressed={suppress_count}",
            suppress_count=suppress_count,
        )

    _emit_reseed(
        "generate_reseed",
        summary=f"total={len(contributors)}",
        contributor_count=len(contributors),
        mastery_gap_count=sum(1 for c in contributors if c.type == "mastery_gap"),
        application_gap_count=sum(1 for c in contributors if c.type == "application_gap"),
        trajectory_gap=steer,
        trajectory_decision=decision or "legacy",
        proactive_update_count=sum(1 for c in contributors if c.type == "proactive_update"),
    )
    return contributors

"""Shared helpers for rebuild-path tests (pressure → global_loop → meta_loop)."""

from __future__ import annotations

from global_loop import intent as intent_phase
from pressure_loop.contributors import EpisodeIgnition, IntentDecision


def form_intent(ignition: EpisodeIgnition) -> IntentDecision:
    """Global A intent routing — intent-only (no goal, no target)."""
    mapped = intent_phase.run(ignition)
    dom_weight = ignition.dominant_contributor.weight
    confidence = max(0.0, min(1.0, float(dom_weight or ignition.aggregate_pressure)))
    reason = f"opened_by={ignition.opened_by} aggregate={ignition.aggregate_pressure:.3f}"
    return IntentDecision(
        episode_id=ignition.episode_id,
        intent=mapped,
        confidence=confidence,
        reason=reason,
    )

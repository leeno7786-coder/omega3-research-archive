"""Global A phase — pass through canonical EpisodeIgnition from pressure."""

from __future__ import annotations

from pressure_loop.contributors import EpisodeIgnition


def run(ignition: EpisodeIgnition) -> EpisodeIgnition:
    """Validate shape only; never rebuild or re-derive the ignition."""
    if ignition is None:
        raise ValueError("ignition is required")
    if not str(getattr(ignition, "opened_by", "") or "").strip():
        raise ValueError("ignition.opened_by is required")
    if getattr(ignition, "dominant_contributor", None) is None:
        raise ValueError("ignition.dominant_contributor is required")
    return ignition

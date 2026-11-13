"""Slice 4 — Global B compat re-exports (logic in phase sub-packages + loop)."""

from __future__ import annotations

from typing import Any, Mapping, Optional

from pressure_loop.contributors import ALLOWED_INTENTS as GLOBAL_INTENTS

from global_loop.assemble.run import GlobalBInput, global_b_input_from_episode
from global_loop.followups.run import run as followups_for
from global_loop.ground.run import run as ground
from global_loop.loop import run_global_b
from global_loop.package.run import GlobalOutcomePack
from global_loop.summarize.run import run as summary_for

FOLLOWUP_TYPES = ("none", "needs_clarification", "needs_retry")


def run_global_b_front(episode, meta_reflection: Mapping[str, Any]) -> Optional[GlobalOutcomePack]:
    """Meta B reflection pack up → Global B chain; writes episode.global_['outcome']."""
    return run_global_b(episode, meta_reflection)


__all__ = [
    "FOLLOWUP_TYPES",
    "GLOBAL_INTENTS",
    "GlobalBInput",
    "GlobalOutcomePack",
    "followups_for",
    "global_b_input_from_episode",
    "ground",
    "run_global_b",
    "run_global_b_front",
    "summary_for",
]

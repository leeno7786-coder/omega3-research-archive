"""Slice 2 — Meta A compat re-exports (logic in phase sub-packages + loop)."""

from __future__ import annotations

from typing import Any, Dict, Optional

from meta_loop import action as action_phase
from meta_loop import memory as memory_phase
from meta_loop import pack as pack_phase
from meta_loop import tool as tool_phase
from meta_loop.intake.run import MetaAInput, meta_a_input_from_episode
from meta_loop.loop import run_meta_a
from meta_loop.pack.run import (
    ExecutionActionPack,
    execution_action_pack_to_dict,
    validate_execution_action_pack,
)

META_ACTIONS = action_phase.META_ACTIONS
_INTENT_TO_ACTION = action_phase._INTENT_TO_ACTION
from meta_loop.tool.run import _CAPABILITY_TOOL_MAP


def build_execution_action_pack(mi: MetaAInput) -> ExecutionActionPack:
    """Deterministic IntentDecision → ExecutionActionPack (spec cases A–H)."""
    state = action_phase.run(mi)
    state = tool_phase.run(state)
    state = memory_phase.run(state)
    return pack_phase.run(state)


def run_meta_a_front(episode) -> Optional[Dict[str, Any]]:
    """Build, validate, and persist ExecutionActionPack on episode.meta."""
    return run_meta_a(episode)


__all__ = [
    "META_ACTIONS",
    "_INTENT_TO_ACTION",
    "_CAPABILITY_TOOL_MAP",
    "MetaAInput",
    "ExecutionActionPack",
    "meta_a_input_from_episode",
    "build_execution_action_pack",
    "validate_execution_action_pack",
    "execution_action_pack_to_dict",
    "run_meta_a_front",
    "run_meta_a",
]

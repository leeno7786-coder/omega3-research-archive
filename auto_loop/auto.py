"""Slice 3 — AUTO compat re-exports (logic in phase sub-packages + loop)."""

from __future__ import annotations

from auto_loop._auto_state import (
    MODEL_PROFILES,
    PROMPT_TYPES,
    RESPONSE_MODES,
    AutoInput,
    AutoOutcomePack,
    MemoryEnvelope,
    PromptPack,
)
from auto_loop.execute.run import (
    _build_failure_outcome,
    _default_tool_invoker,
    execute,
)
from auto_loop.fold.run import _fold_episode_auto, auto_outcome_from_fold
from auto_loop.intake.run import auto_input_from_episode
from auto_loop.loop import run_auto, run_auto_front
from auto_loop.mril_tools import (
    ALL_REMEMBER_TOOL_NAMES,
    denied_lobes_for_scope,
    format_remembered_knowledge_block,
    invoke_remember_tool,
    memory_recall_required,
    route_memory_tool,
    route_memory_tools,
    run_forced_memory_tools,
    scope_to_lobes,
)
from auto_loop.promptpack.run import _dominant_text, build_prompt_pack
from auto_loop.recall.run import memory_envelope_before_execute
from auto_loop.route.run import select_model_profile, tool_call_is_direct
from auto_loop.validate.run import validate_for_execution

__all__ = [
    "PROMPT_TYPES",
    "MODEL_PROFILES",
    "RESPONSE_MODES",
    "AutoInput",
    "MemoryEnvelope",
    "PromptPack",
    "AutoOutcomePack",
    "auto_input_from_episode",
    "validate_for_execution",
    "scope_to_lobes",
    "denied_lobes_for_scope",
    "memory_envelope_before_execute",
    "build_prompt_pack",
    "_dominant_text",
    "tool_call_is_direct",
    "select_model_profile",
    "execute",
    "_default_tool_invoker",
    "_build_failure_outcome",
    "_fold_episode_auto",
    "auto_outcome_from_fold",
    "run_auto",
    "run_auto_front",
    "ALL_REMEMBER_TOOL_NAMES",
    "format_remembered_knowledge_block",
    "invoke_remember_tool",
    "route_memory_tool",
    "route_memory_tools",
    "run_forced_memory_tools",
    "memory_recall_required",
]

"""Shared AUTO carriers, constants, and helpers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from memory.recall_lanes import (
    LANE_CODING,
    LANE_DEV,
    LANE_IDENTITY,
    LANE_JOURNAL,
    LANE_KNOWLEDGE,
    LANE_PERSONA,
    LANE_PROJECT,
    LANE_REASONING,
    LANE_SITUATIONAL,
    LANE_SKILLS,
    LANE_TEMPORAL,
    LANE_TOOLS,
    LANE_VISION,
    LANE_WORKFLOWS,
)

PROMPT_TYPES = (
    "RESPONSE_REASONING",
    "RESPONSE_QUESTION",
    "RESPONSE_ACK",
    "RESPONSE_FREEFORM",
    "RESPONSE_TEMPORAL",
    "PROACTIVE_REASONING",
    "PROACTIVE_ACK",
    "TOOL_CALL_SYSTEM",
    "TOOL_RESULT_SYNTHESIS",
    "SKILL_BUILD_SYSTEM",
    "IMAGE_SYSTEM",
    "DOCUMENT_SYSTEM",
    "IDLE_SYSTEM",
)

MODEL_PROFILES = (
    "TEXT_REASONING",
    "TEXT_FAST",
    "TOOL_EXECUTOR",
    "IMAGE_GENERATION",
)

RESPONSE_MODES = ("reasoning", "question", "ack", "freeform", "summary")

_LANE_LOBE: Dict[str, int] = {
    LANE_IDENTITY: 0,
    LANE_PERSONA: 1,
    LANE_KNOWLEDGE: 2,
    LANE_REASONING: 3,
    LANE_SITUATIONAL: 4,
    LANE_TOOLS: 5,
    LANE_SKILLS: 5,
    LANE_JOURNAL: 6,
    LANE_VISION: 7,
    LANE_CODING: 8,
    LANE_TEMPORAL: 9,
    LANE_WORKFLOWS: 10,
    LANE_PROJECT: 10,
    LANE_DEV: 11,
}

_SCOPE_ALIASES: Dict[str, Tuple[str, ...]] = {
    "operator_history": (LANE_PERSONA, LANE_SITUATIONAL),
    "conversation": (LANE_PERSONA, LANE_SITUATIONAL),
    "trajectory": (LANE_TEMPORAL,),
}

# Ceilings, not targets (operator 2026-07-12) — keep in sync with
# config/auto.yaml execute.profile_max_tokens and policy defaults.
_PROFILE_MAX_TOKENS = {
    "TEXT_REASONING": 2048,
    "TEXT_FAST": 1024,
    "TOOL_EXECUTOR": 512,
}


@dataclass(frozen=True)
class AutoInput:
    episode_id: str
    pressure: dict
    source_action_goal: dict
    execution_pack: dict
    trajectory_snapshot: dict
    motor_program: dict | None = None
    # Per-episode temporal envelope (core.episode_temporal) captured at ignition — carries the
    # canonical wall-clock so the promptpack can surface "now" to the mind without a fresh read.
    temporal: dict | None = None


@dataclass(frozen=True)
class MemoryEnvelope:
    memory_required: bool
    memory_status: str
    memory_reason: Optional[str]
    memory_scope: tuple
    items: tuple
    error: Optional[str]
    # Cut 1: bounded diagnostic receipts from the remember tools (e.g. DME
    # activation traces), kept SEPARATE from ``items`` — items remain the exact
    # engram provenance set; receipts never enter the prompt. Additive default
    # keeps positional six-argument construction working.
    receipts: tuple = ()


@dataclass(frozen=True)
class PromptPack:
    prompt_type: str
    system_prompt: str
    developer_prompt: Optional[str]
    user_prompt: Optional[str]
    context_blocks: dict
    response_mode: Optional[str]
    tool_call_spec: Optional[dict]
    image_spec: Optional[dict]


@dataclass(frozen=True)
class AutoOutcomePack:
    episode_id: str
    source_action_goal: dict
    execution_pack: dict
    auto: dict


def _hash_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

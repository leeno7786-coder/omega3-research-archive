"""MRIL v2 remember_* tool registry — exact lobe per tool."""

from __future__ import annotations

from typing import Dict, Tuple

from memory.recall_lanes import (
    LANE_CODING,
    LANE_DEV,
    LANE_IDENTITY,
    LANE_JOURNAL,
    LANE_KNOWLEDGE,
    LANE_PERSONA,
    LANE_REASONING,
    LANE_SITUATIONAL,
    LANE_SKILLS,
    LANE_TEMPORAL,
    LANE_VISION,
    LANE_WORKFLOWS,
)

# tool_name -> (lane, lobe, default_memory_type)
REMEMBER_TOOL_REGISTRY: Dict[str, Tuple[str, int, str]] = {
    "remember_identity": (LANE_IDENTITY, 0, "episodic"),
    "remember_persona": (LANE_PERSONA, 1, "episodic"),
    "remember_knowledge": (LANE_KNOWLEDGE, 2, "episodic"),
    "remember_reasoning": (LANE_REASONING, 3, "episodic"),
    "remember_situation": (LANE_SITUATIONAL, 4, "episodic"),
    "remember_procedure": (LANE_SKILLS, 5, "procedural"),
    "remember_journal": (LANE_JOURNAL, 6, "episodic"),
    "remember_vision": (LANE_VISION, 7, "episodic"),
    "remember_code": (LANE_CODING, 8, "episodic"),
    "remember_time": (LANE_TEMPORAL, 9, "episodic"),
    "remember_workflow": (LANE_WORKFLOWS, 10, "procedural"),
    "remember_dev": (LANE_DEV, 11, "episodic"),
}

ALL_REMEMBER_TOOL_NAMES = tuple(REMEMBER_TOOL_REGISTRY.keys())

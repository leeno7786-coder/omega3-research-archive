"""Memory gates — per-lobe access-controlled interfaces to the memory engine.

Per `project_guardian_sole_writer_doctrine` (2026-05-08): each gate
names the lobe by its CURRENT canonical identity. No backwards-compat
aliases — a system booted under a stale name is a lie. Pre-rename
imports of SkillsGate / PersonalGate / ContextGate must update to
ToolsGate / PersonasGate / SituationalGate respectively.
"""

from memory.gates.lobe_gates import (
    IdentityGate,
    PersonasGate,
    KnowledgeGate,
    ReasoningGate,
    SituationalGate,
    ToolsGate,
    JournalGate,
    VisionGate,
    CodingGate,
    TemporalGate,
    WorkflowsGate,
    DevGate,
)

__all__ = [
    "IdentityGate",
    "PersonasGate",
    "KnowledgeGate",
    "ReasoningGate",
    "SituationalGate",
    "ToolsGate",
    "JournalGate",
    "VisionGate",
    "CodingGate",
    "TemporalGate",
    "WorkflowsGate",
    "DevGate",
]

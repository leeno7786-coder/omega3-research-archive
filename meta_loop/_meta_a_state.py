"""Shared Meta A phase carrier (structure refactor only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class MetaAState:
    mi: Any
    episode_id: str
    intent: str
    action: str
    source: Dict[str, Any]
    text: str
    target_str: str
    constraints: Dict[str, bool]
    meta: Dict[str, Any]
    tool: Optional[str] = None
    tool_reason: Optional[str] = None
    memory_required: bool = False
    memory_answer_required: bool = False
    memory_reason: Optional[str] = None
    memory_scope: tuple = ()
    memory_query: Optional[str] = None
    memory_delivery_mode: Optional[str] = None
    memory_tools: tuple = ()
    execution_intent: str = ""
    execution_inputs: Dict[str, Any] = field(default_factory=dict)

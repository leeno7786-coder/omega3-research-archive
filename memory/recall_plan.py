"""
recall_plan.py
RecallPlan dataclass — the compiled query-side prior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import logging

log = logging.getLogger("omega.memory.recall_plan")


@dataclass(frozen=True)
class RecallPlan:
    original_query: str
    posture: Any  # RecallPosture (avoid circular import at module load)
    secondary_postures: list[Any]
    intent: str
    query_type: str
    coord_hypotheses: list[tuple[int, int | None, str | None]]
    exact_terms: list[str]
    expanded_terms: list[str]
    entities: list[str]
    task_tags: list[str]
    lane_weights: dict[str, float]
    required_evidence_roles: list[str]
    allowed_evidence_roles: list[str]
    min_strength: float
    limit_per_lane: int
    final_limit: int
    suppressed_lobes: list[int]
    include_historical_sources: bool = False

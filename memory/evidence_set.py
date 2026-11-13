"""
evidence_set.py
Merge lane hits into deduplicated memory evidence records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import logging

log = logging.getLogger("omega.memory.evidence_set")


@dataclass
class MemoryEvidence:
    memory_id: str
    lanes: List[str] = field(default_factory=list)
    lane_scores: Dict[str, float] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    meta: Optional[Dict[str, Any]] = None
    # Structured final score from posture-aware arbitration (set by
    # `recall_arbitrator.arbitrate_evidence`). Replaces the practice
    # of grepping the value out of the synthesis reason string. Used
    # by `memory.api.read_memory` to populate the deterministic
    # `meta["arbitration"]` shape that downstream consumers
    # (Curiosity / DeepSleep / Guardian) read — same shape the BGE
    # arbitrator used to produce, now sourced from the walker-native
    # lane arbitration. 0.0 until arbitration runs.
    final_score: float = 0.0


@dataclass
class EvidenceSet:
    records: Dict[str, MemoryEvidence] = field(default_factory=dict)

    def add_hit(self, hit: Any) -> None:
        """Add a LaneHit, merging if memory_id already exists."""
        mid = hit.memory_id
        if mid not in self.records:
            self.records[mid] = MemoryEvidence(
                memory_id=mid,
                lanes=[],
                lane_scores={},
                reasons=[],
                meta=None,
            )
        ev = self.records[mid]
        if hit.lane not in ev.lanes:
            ev.lanes.append(hit.lane)
        ev.lane_scores[hit.lane] = max(ev.lane_scores.get(hit.lane, 0.0), hit.score)
        if hit.reason and hit.reason not in ev.reasons:
            ev.reasons.append(hit.reason)
        # Eager-attach lane-emitted metadata — lane runners produce
        # `LaneHit.metadata` carrying coord/title/type so the
        # arbitrator can score lobe/type suppressions without a
        # second filesystem walk. Later add_meta() calls (with
        # richer loaded records) override.
        if ev.meta is None and getattr(hit, "metadata", None):
            ev.meta = dict(hit.metadata)

    def add_meta(self, memory_id: str, meta: Dict[str, Any]) -> None:
        if memory_id in self.records:
            self.records[memory_id].meta = meta

    def to_list(self) -> List[MemoryEvidence]:
        return list(self.records.values())

    def get(self, memory_id: str) -> Optional[MemoryEvidence]:
        return self.records.get(memory_id)

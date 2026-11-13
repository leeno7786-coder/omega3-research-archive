"""
recall_receipt.py
Build a JSON-serializable recall receipt from plan, evidence, and winners.

The receipt is the audit trail for a recall pass. It explains:
    - which posture was chosen and why
    - which lanes contributed evidence
    - which coord hypotheses guided the search
    - which memories won
    - the per-winner scoring reason (lane breakdown + suppressions
      + answerability + multi-lane bonus)

Receipts are surfaced in `RetrievalResult.meta["recall_receipt"]` so
the brain (and any downstream auditor) can show "here's why I picked
THIS memory" instead of "trust the score".
"""

from __future__ import annotations

from typing import Any, Dict, List

from memory.evidence_set import EvidenceSet
import logging

log = logging.getLogger("omega.memory.recall_receipt")


def _serializable_coord_hypothesis(
    hyp: Any,
) -> List[Any]:
    """coord_hypotheses are tuples like (X, Y|None, domain|None).
    JSON serializers handle tuples fine but dump them as lists; we
    pre-flatten so the receipt's shape is stable across consumers."""
    if isinstance(hyp, (tuple, list)):
        return list(hyp)
    return [hyp]


def _winner_reason(evidence: Any) -> str:
    """Pick the most specific reason from the evidence's accumulated
    reasons list. The arbitrator appends a synthesis reason last; if
    present we use it. Otherwise we concatenate up to 3 lane reasons.
    """
    reasons = list(getattr(evidence, "reasons", []) or [])
    if not reasons:
        return "(no reasons recorded)"
    # The arbitrator's synthesis reason starts with "final="
    synthesis = next((r for r in reasons if r.startswith("final=")), None)
    lane_reasons = [r for r in reasons if not r.startswith("final=")]
    parts: List[str] = []
    if synthesis:
        parts.append(synthesis)
    if lane_reasons:
        parts.append("via lanes: " + " | ".join(lane_reasons[:3]))
    return "  ".join(parts)


def build_recall_receipt(
    query_spec: Any,
    plan: Any,
    evidence_set: EvidenceSet,
    winners: List[Any],
) -> Dict[str, Any]:
    """Build the recall receipt dict.

    Schema:
        {
            "query":                 str,
            "posture":               str,
            "secondary_postures":    list[str],
            "lanes_used":            list[str]    (deduped, in discovery order),
            "coord_hypotheses":      list[list],
            "winning_memory_ids":    list[str],
            "why_selected":          dict[memory_id -> reason],
            "evidence_count":        int          (memories considered),
            "winner_count":          int,
        }

    Stable for a given (query, plan, evidence_set, winners) tuple.
    """
    query_text = getattr(query_spec, "text", "") or ""

    primary_name = getattr(getattr(plan, "posture", None), "name", "UNKNOWN")
    secondary_names = [
        getattr(sp, "name", "UNKNOWN") for sp in (getattr(plan, "secondary_postures", []) or [])
    ]

    # Discover order is "first appearance across evidence iteration";
    # dict insertion order is stable in Python 3.7+, so this gives
    # a deterministic, reproducible lanes_used list.
    lanes_used = list(
        dict.fromkeys([lane for ev in evidence_set.to_list() for lane in (ev.lanes or [])])
    )

    coord_hypotheses = [
        _serializable_coord_hypothesis(h) for h in (getattr(plan, "coord_hypotheses", []) or [])
    ]

    winning_ids = [getattr(w, "memory_id", str(w)) for w in winners]

    why_selected: Dict[str, str] = {}
    for w in winners:
        mid = getattr(w, "memory_id", str(w))
        why_selected[mid] = _winner_reason(w)

    return {
        "query": query_text,
        "posture": primary_name,
        "secondary_postures": secondary_names,
        "lanes_used": lanes_used,
        "coord_hypotheses": coord_hypotheses,
        "winning_memory_ids": winning_ids,
        "why_selected": why_selected,
        "evidence_count": len(evidence_set.records),
        "winner_count": len(winning_ids),
    }

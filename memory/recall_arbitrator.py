"""
recall_arbitrator.py
Posture-aware scoring and final arbitration of evidence.

Inputs:
    EvidenceSet — merged lane hits per memory_id, with optional
                  loaded `meta` for coord/type/etc.
    RecallPlan  — posture, suppressions, lane_weights, exact/expanded
                  terms, evidence-role requirements.

Output:
    Sorted list of MemoryEvidence (winners), bounded by plan.final_limit.
    Each winner carries `lanes`, `lane_scores`, and `reasons` so the
    receipt builder can explain the choice.

Scoring formula (per-evidence):

    base   = max(lane_scores)                     # strongest lane wins
    bonus  = multi_lane_bonus(evidence)           # +0.05..+0.20
    answer = answerability_score(meta, plan)      # +0..+1.0 (scaled 0.20)
    sal    = salience_term(evidence)              # ±0.075 (see below)
    pen_l  = suppressed_lobe_penalty(...)         # -0.30 if suppressed
    pen_t  = suppressed_memory_type_penalty(...)  # -0.25 if suppressed

    score = clamp(base + bonus + 0.20*answer + sal + pen_l + pen_t, 0.0, 1.0)

Salience term (neuromod-coupled ranking, 2026-07-08):
    StrengthIndex.get_strength(mid) already folds together the whole
    memory chemistry — encode-time neuromod gain (dopamine/plasticity
    scale, stress/serotonin tau), age decay with floor, dopamine-RPE
    reinforcement, and touch refresh. The arbitrator centers it:

        sal = _SALIENCE_WEIGHT * (get_strength(mid) - _SALIENCE_NEUTRAL)

    so fresh/hot memories nudge UP, decayed ones nudge DOWN, and a
    memory UNKNOWN to the index (or an unavailable index) contributes
    exactly 0.0 — no uniform penalty for unindexed memories, byte-
    compatible behavior when the index isn't wired. Enrichment-scale
    by design: salience reorders near-ties; it can never surface a
    memory that earned no lane hits (lanes are the only entry).

Tie-break:
    1) score desc
    2) memory_id asc (deterministic, stable across runs)
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from memory.evidence_set import EvidenceSet, MemoryEvidence
from memory.answerability_index import answerability_score
import logging

log = logging.getLogger("omega.memory.recall_arbitrator")


# =====================================================================
# Penalties and bonuses
# =====================================================================

# Multi-lane convergence — when several independent lanes agree on a
# memory, that's strong signal the memory IS the answer (multiple
# routes reached it). Bonus brackets are linear in lane count, capped
# at 5+ lanes (more = diminishing returns).
_MULTI_LANE_BONUS_TABLE = {
    2: 0.05,
    3: 0.10,
    4: 0.15,
}
_MULTI_LANE_BONUS_MAX = 0.20

# Suppressions — posture explicitly says "this lobe / type isn't the
# answer shape". The penalty has to be steep enough to kick a
# wrong-shape memory out of the top, but not so steep it produces
# negative-score noise.
_SUPPRESSED_LOBE_PENALTY = -0.30
_SUPPRESSED_MEMORY_TYPE_PENALTY = -0.25

# Answerability contribution scale. answerability_score returns [0, 1];
# we scale it down so it's an ENRICHMENT signal, not the dominant
# component (lane scores are the primary signal).
_ANSWERABILITY_WEIGHT = 0.20

# Salience contribution — centered so the default engram (write_strength
# 0.5, fresh) sits at exactly 0.0 and the term spans roughly ±0.075:
# enough to reorder near-ties by freshness/reinforcement, never enough
# to outvote lane relevance.
_SALIENCE_WEIGHT = 0.15
_SALIENCE_NEUTRAL = 0.5


def salience_term(evidence: MemoryEvidence) -> float:
    """Centered StrengthIndex salience for one evidence record.

    Returns 0.0 (exact neutral) when the index isn't wired, the lookup
    fails, or the memory has no strength record — unindexed memories
    must not eat a uniform penalty.
    """
    mid = getattr(evidence, "memory_id", "") or ""
    if not mid:
        return 0.0
    try:
        from memory.strength_index import get_strength_index

        si = get_strength_index()
        if si is None:
            return 0.0
        if si.get_record(mid) is None:
            return 0.0
        return _SALIENCE_WEIGHT * (si.get_strength(mid) - _SALIENCE_NEUTRAL)
    except Exception:
        return 0.0


def multi_lane_bonus(evidence: MemoryEvidence) -> float:
    """Bonus for memories matched by multiple lanes."""
    n = len(evidence.lanes)
    if n >= 5:
        return _MULTI_LANE_BONUS_MAX
    return _MULTI_LANE_BONUS_TABLE.get(n, 0.0)


def suppressed_lobe_penalty(
    evidence: MemoryEvidence,
    plan: Any,
) -> float:
    """Penalty when memory's coord lobe is in the posture's
    suppressed_lobes list. Returns 0.0 when suppression doesn't apply."""
    suppressed = list(getattr(getattr(plan, "posture", None), "suppressed_lobes", []) or [])
    if not suppressed or not evidence.meta:
        return 0.0
    coord = evidence.meta.get("coord") or []
    if not coord:
        return 0.0
    try:
        if int(coord[0]) in suppressed:
            return _SUPPRESSED_LOBE_PENALTY
    except (TypeError, ValueError):
        pass
    return 0.0


def suppressed_memory_type_penalty(
    evidence: MemoryEvidence,
    plan: Any,
) -> float:
    """Penalty when memory's `type` is in the posture's
    suppressed_memory_types list."""
    suppressed = list(getattr(getattr(plan, "posture", None), "suppressed_memory_types", []) or [])
    if not suppressed or not evidence.meta:
        return 0.0
    mtype = evidence.meta.get("type") or ""
    if mtype in suppressed:
        return _SUPPRESSED_MEMORY_TYPE_PENALTY
    return 0.0


# =====================================================================
# Per-evidence posture scoring
# =====================================================================


def posture_score_evidence(
    evidence: MemoryEvidence,
    plan: Any,
) -> float:
    """Score a single evidence record against the plan.

    See module docstring for the formula. Returns a value in [0, 1].
    """
    base = max(evidence.lane_scores.values()) if evidence.lane_scores else 0.0

    bonus = multi_lane_bonus(evidence)

    # Answerability scoring — uses the loaded meta if available; falls
    # back to an empty dict so the function never raises.
    answer_meta = evidence.meta or {}
    try:
        answer = answerability_score(answer_meta, plan)
    except Exception:
        answer = 0.0

    sal = salience_term(evidence)
    pen_lobe = suppressed_lobe_penalty(evidence, plan)
    pen_type = suppressed_memory_type_penalty(evidence, plan)

    raw = base + bonus + (_ANSWERABILITY_WEIGHT * answer) + sal + pen_lobe + pen_type
    return max(0.0, min(1.0, raw))


def _score_breakdown(
    evidence: MemoryEvidence,
    plan: Any,
) -> Dict[str, float]:
    """Return the component-by-component breakdown for receipts /
    tests / debugging. Same formula as posture_score_evidence."""
    base = max(evidence.lane_scores.values()) if evidence.lane_scores else 0.0
    bonus = multi_lane_bonus(evidence)
    answer = 0.0
    if evidence.meta:
        try:
            answer = answerability_score(evidence.meta, plan)
        except Exception:
            answer = 0.0
    sal = salience_term(evidence)
    pen_lobe = suppressed_lobe_penalty(evidence, plan)
    pen_type = suppressed_memory_type_penalty(evidence, plan)

    raw = base + bonus + (_ANSWERABILITY_WEIGHT * answer) + sal + pen_lobe + pen_type
    final = max(0.0, min(1.0, raw))
    return {
        "base_lane": round(base, 4),
        "multi_lane": round(bonus, 4),
        "answerability": round(_ANSWERABILITY_WEIGHT * answer, 4),
        "salience": round(sal, 4),
        "lobe_penalty": round(pen_lobe, 4),
        "type_penalty": round(pen_type, 4),
        "raw": round(raw, 4),
        "final": round(final, 4),
    }


# =====================================================================
# Final arbitration
# =====================================================================


def arbitrate_evidence(
    evidence_set: EvidenceSet,
    plan: Any,
    *,
    truncation_receipts: List[Dict[str, Any]] | None = None,
) -> List[MemoryEvidence]:
    """Rank all evidence by posture-aware score; return top
    `plan.final_limit` deterministically.

    Tie-break: score desc, then memory_id asc.

    HARD SUPPRESSION (added 2026-05-08, FIX-4):
        When the plan's posture has `hard_suppress=True`, evidence
        whose memory coord lobe is in `posture.suppressed_lobes` is
        DROPPED before scoring. The standard -0.30 lobe penalty is
        still in play for postures without the hard flag, so default
        behavior is unchanged. Hard suppression matters when a
        wrong-shape memory has strong multi-lane convergence — the
        soft penalty isn't enough to keep it out of the top, but
        absolute filtering is. Used by IDENTITY_RESOLUTION and
        CAPABILITY_ENUMERATION (postures where misroute would put
        an unambiguously-wrong memory at candidates[0]).

    Side effect: each returned MemoryEvidence has a synthesis reason
    appended to its `reasons` so the receipt builder can quote the
    math without recomputing.
    """
    posture = getattr(plan, "posture", None)
    hard = bool(getattr(posture, "hard_suppress", False))
    suppressed_lobes = set(getattr(posture, "suppressed_lobes", []) or []) if hard else set()

    scored: List[Tuple[float, str, MemoryEvidence]] = []
    for ev in evidence_set.to_list():
        # Hard-suppression filter: drop evidence in suppressed lobes
        # before any scoring runs. Skipping here means the evidence
        # never appears in the returned list AND never contributes
        # to multi-lane bonuses against other evidence.
        if hard and ev.meta:
            coord = ev.meta.get("coord") or []
            if coord:
                try:
                    if int(coord[0]) in suppressed_lobes:
                        continue
                except (TypeError, ValueError):
                    pass

        breakdown = _score_breakdown(ev, plan)
        final = breakdown["final"]
        # Stash the final score on the evidence so api.py can build the
        # `meta["arbitration"]` shape without re-parsing the synthesis
        # reason string. Walker-native arbitration replaces the BGE
        # path; this is the structured handoff. (2026-05-08)
        ev.final_score = final
        # Annotate the evidence with a structured reason for the
        # receipt. We don't mutate lane_scores (those are per-lane
        # raw signals); we append a synthesis reason.
        synthesis_reason = (
            f"final={final:.3f} = base_lane({breakdown['base_lane']:.2f})"
            f" + multi_lane({breakdown['multi_lane']:.2f})"
            f" + answer({breakdown['answerability']:.2f})"
            f" + salience({breakdown['salience']:+.2f})"
            f" + lobe_pen({breakdown['lobe_penalty']:.2f})"
            f" + type_pen({breakdown['type_penalty']:.2f})"
        )
        if synthesis_reason not in ev.reasons:
            ev.reasons.append(synthesis_reason)
        scored.append((final, ev.memory_id, ev))

    scored.sort(key=lambda x: (-x[0], x[1]))

    final_limit = int(getattr(plan, "final_limit", 10) or 10)
    winners = [ev for _, _, ev in scored[: max(0, final_limit)]]
    if truncation_receipts is not None:
        from memory.recall_truncation import build_truncation_receipt

        truncation_receipts.append(
            build_truncation_receipt(
                "lane_arbitration:final_limit",
                [ev.memory_id for ev in evidence_set.to_list()],
                [ev.memory_id for ev in winners],
                details={"limit": max(0, final_limit)},
            )
        )
    return winners

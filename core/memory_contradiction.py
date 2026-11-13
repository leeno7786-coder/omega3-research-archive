"""Deterministic contradiction adjudication from governed memory config."""

from __future__ import annotations

from typing import Any

from core.memory_config import contradiction_rules


def _confidence(doc: dict[str, Any]) -> float:
    provenance = doc.get("provenance") if isinstance(doc.get("provenance"), dict) else {}
    raw = provenance.get("confidence", doc.get("confidence"))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _evidence_count(doc: dict[str, Any]) -> int:
    refs = doc.get("evidence_refs")
    if isinstance(refs, list):
        return len(refs)
    provenance = doc.get("provenance") if isinstance(doc.get("provenance"), dict) else {}
    prov_refs = provenance.get("evidence_refs")
    if isinstance(prov_refs, list):
        return len(prov_refs)
    return 0


def _recency_key(doc: dict[str, Any]) -> str:
    if doc.get("written_at"):
        return str(doc["written_at"])
    audit = doc.get("audit")
    if isinstance(audit, list) and audit:
        last = audit[-1]
        if isinstance(last, dict) and last.get("when"):
            return str(last["when"])
    return ""


def _canonical(doc: dict[str, Any]) -> bool:
    flags = doc.get("flags") if isinstance(doc.get("flags"), dict) else {}
    return bool(flags.get("canonical") or doc.get("canonical"))


def _subjects(doc: dict[str, Any]) -> set[str]:
    """Resolved subjects a record asserts, from enrichment.entities (the SAME parsed relations
    extract_persona hangs graph edges off). Empty for non-relational records. Used to REJECT the
    title-based partitioner's false positives: two records only conflict if a shared subject
    disagrees — an 'abel' name-fact and an 'operator' name-fact are different subjects, not a
    contradiction."""
    enrichment = doc.get("enrichment") if isinstance(doc.get("enrichment"), dict) else {}
    entities = enrichment.get("entities") if isinstance(enrichment, dict) else None
    if not isinstance(entities, list):
        return set()
    return {
        str(e.get("subject") or "operator").strip().lower()
        for e in entities
        if isinstance(e, dict) and e.get("facet")
    }


# A loser is TOMBSTONED only when decisively out-corroborated (a lone assertion losing to a
# multiply-corroborated value, or a non-canonical value losing to a canonical one). A near-tie
# takes the softer demotion path — never a hard removal on a coin-flip (operator 2026-07-15).
_DECISIVE_MIN_WINNER_CORROBORATION = 2
_DECISIVE_MAX_LOSER_CORROBORATION = 1


def adjudicate_contradiction_pair(
    canonical: dict[str, Any],
    challenger: dict[str, Any],
    *,
    corroboration: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Adjudicate a candidate contradiction pair.

    SUBJECT-AWARE (H1): the recall-time partitioner groups by title only, which is over-inclusive
    post-restructuring. If both records carry resolved subjects and those subject sets are DISJOINT
    the pair is NOT a real contradiction (different people/things sharing a title) -> ``conflict``
    is False and nothing is acted on. Records without subject facets fall back to the proven
    confidence adjudication (backward compatible).

    CORROBORATION-WEIGHTED (H2): the winner is the value corroborated by the most records ("Noah"
    across the profile beats "Kim" once), then canonical, then confidence/evidence/recency. Pass
    ``corroboration`` = {memory_id: lobe-wide corroboration count}; absent counts default to 0.

    ``decisive`` reports whether the loser is decisively weak (safe to TOMBSTONE) vs a near-tie
    (softer demotion). Pure + deterministic."""
    rules = contradiction_rules()
    resolution = str(rules.get("resolution") or "confidence_then_evidence_then_recency")
    penalty = float(rules.get("loser_confidence_penalty") or 0.30)
    corr = corroboration or {}

    left_id = str(canonical.get("id") or "canonical")
    right_id = str(challenger.get("id") or "challenger")

    subs_left, subs_right = _subjects(canonical), _subjects(challenger)
    if subs_left and subs_right and subs_left.isdisjoint(subs_right):
        return {
            "conflict": False,
            "reason": "subject_mismatch",
            "resolution": resolution,
        }

    # rank: corroboration -> canonical -> confidence -> evidence -> recency -> id (max wins)
    left = (
        int(corr.get(left_id, 0)),
        1 if _canonical(canonical) else 0,
        _confidence(canonical),
        _evidence_count(canonical),
        _recency_key(canonical),
        left_id,
    )
    right = (
        int(corr.get(right_id, 0)),
        1 if _canonical(challenger) else 0,
        _confidence(challenger),
        _evidence_count(challenger),
        _recency_key(challenger),
        right_id,
    )
    winner, loser = (left, right) if left >= right else (right, left)
    winner_doc = canonical if winner[-1] == left_id else challenger
    loser_doc = challenger if winner[-1] == left_id else canonical

    decisive = (
        winner[0] >= _DECISIVE_MIN_WINNER_CORROBORATION
        and loser[0] <= _DECISIVE_MAX_LOSER_CORROBORATION
    ) or (_canonical(winner_doc) and not _canonical(loser_doc))

    return {
        "conflict": True,
        "winner_id": winner[-1],
        "loser_id": loser[-1],
        "loser_penalty": penalty,
        "resolution": resolution,
        "decisive": bool(decisive),
        "winner_corroboration": winner[0],
        "loser_corroboration": loser[0],
    }

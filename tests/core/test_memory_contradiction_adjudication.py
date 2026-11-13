"""Subject-aware + corroboration-weighted contradiction adjudication.

The recall-time partitioner (frozen verifier) groups conflict candidates by TITLE only, which
post-restructuring is over-inclusive: an 'abel' name-fact and an 'operator' name-fact share a
title but are NOT a conflict (different subjects). Adjudication is where a destructive delete is
gated, so subject-awareness lives HERE: two records conflict only when they assert the SAME
subject with a DIFFERENT value. The winner is the corroboration-weighted value ("Noah" across the
profile beats "Kim" once) — operator 2026-07-15. `decisive` gates tombstone (clear loser) vs the
softer demotion (near-tie).
"""

from __future__ import annotations

from core.memory_contradiction import adjudicate_contradiction_pair


def _rec(mid, *, subject=None, facet="name", value="", confidence=0.5, canonical=False):
    doc = {"id": mid, "provenance": {"confidence": confidence}, "flags": {"canonical": canonical}}
    if subject is not None:
        doc["enrichment"] = {"entities": [{"subject": subject, "facet": facet, "name": value}]}
    return doc


def test_subject_mismatch_is_not_a_conflict():
    # abel's name-fact vs operator's name-fact share a title but are different subjects -> the
    # title-based partition is a FALSE positive; adjudication must reject it (no delete).
    abel = _rec("1.01.abel", subject="abel", facet="name", value="abel")
    operator = _rec("1.00.op", subject="operator", facet="name", value="noah")
    out = adjudicate_contradiction_pair(abel, operator)
    assert out["conflict"] is False


def test_same_subject_different_value_is_a_conflict():
    noah = _rec("1.00.noah", subject="operator", facet="name", value="noah", confidence=1.0)
    kim = _rec("1.00.kim", subject="operator", facet="name", value="kim", confidence=0.3)
    out = adjudicate_contradiction_pair(noah, kim)
    assert out["conflict"] is True
    assert out["winner_id"] == "1.00.noah"
    assert out["loser_id"] == "1.00.kim"


def test_corroboration_decides_the_winner_over_confidence():
    # Even at EQUAL confidence, the value corroborated across more records wins (the operator's
    # "Noah across the whole profile vs Kim once" signal).
    noah = _rec("1.00.noah", subject="operator", facet="name", value="noah", confidence=0.5)
    kim = _rec("1.00.kim", subject="operator", facet="name", value="kim", confidence=0.5)
    out = adjudicate_contradiction_pair(noah, kim, corroboration={"1.00.noah": 5, "1.00.kim": 1})
    assert out["winner_id"] == "1.00.noah"
    assert out["loser_id"] == "1.00.kim"
    assert out["decisive"] is True  # lone value (1) vs multiply-corroborated (5) -> tombstone


def test_lone_typo_losing_to_canonical_profile_is_decisive():
    noah = _rec("1.00.noah", subject="operator", value="noah", confidence=1.0, canonical=True)
    kim = _rec("1.00.kim", subject="operator", value="kim", confidence=0.3, canonical=False)
    out = adjudicate_contradiction_pair(noah, kim)
    assert out["winner_id"] == "1.00.noah"
    assert out["decisive"] is True  # canonical winner vs non-canonical loser -> decisive


def test_near_tie_is_not_decisive():
    # Two singly-asserted values with equal confidence: a winner is still chosen (deterministic),
    # but it is NOT decisive -> the softer demotion path, never a tombstone on a coin-flip.
    a = _rec("1.00.a", subject="operator", value="alpha", confidence=0.5)
    b = _rec("1.00.b", subject="operator", value="beta", confidence=0.5)
    out = adjudicate_contradiction_pair(a, b, corroboration={"1.00.a": 1, "1.00.b": 1})
    assert out["conflict"] is True
    assert out["decisive"] is False


def test_no_subject_facets_falls_back_to_confidence():
    # Non-relational records (no enrichment.entities) can't be subject-confirmed -> preserve the
    # existing confidence-based adjudication (backward compatible with the proven path).
    hi = {"id": "2.00.hi", "provenance": {"confidence": 0.9}}
    lo = {"id": "2.00.lo", "provenance": {"confidence": 0.2}}
    out = adjudicate_contradiction_pair(hi, lo)
    assert out["conflict"] is True
    assert out["winner_id"] == "2.00.hi"
    assert out["loser_id"] == "2.00.lo"


def test_backward_compatible_keys_present():
    out = adjudicate_contradiction_pair(
        _rec("a", subject="operator", value="x", confidence=0.9),
        _rec("b", subject="operator", value="y", confidence=0.1),
    )
    assert {"winner_id", "loser_id", "loser_penalty", "resolution"} <= set(out)


def test_deterministic():
    args = (
        _rec("a", subject="operator", value="x", confidence=0.5),
        _rec("b", subject="operator", value="y", confidence=0.5),
    )
    kw = {"corroboration": {"a": 3, "b": 1}}
    outs = [adjudicate_contradiction_pair(*args, **kw) for _ in range(3)]
    assert outs[0] == outs[1] == outs[2]

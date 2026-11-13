"""Lobe-wide, AXIS-SCOPED corroboration for contradiction adjudication.

The winner of a contradiction is the value corroborated by the most records on the axis the pair
DISAGREES on ("Noah" asserted across the operator profile beats a one-off "Kim" typo). Two halves:

  scan_fingerprint_counts  — one bounded scan of the candidates' lobes -> {(subject,facet,value):
                             count}. The coord index carries no values, so it reads record files.
  pairwise_corroboration   — reduces that scan to a per-pair {id: count} scoped to the (subject,
                             facet) axes the pair SHARES, so a record can't win a city-conflict on
                             the strength of an unrelated, well-corroborated employer fact.

Under-counting (unresolved aliases) is the SAFE failure direction: it only weakens decisiveness
(demote instead of tombstone), never flips a winner. Exact-match can only count AGREEING records,
so it can never wrongly inflate a loser.
"""

from __future__ import annotations

from memory.contradiction_resolution import (
    pairwise_corroboration,
    scan_fingerprint_counts,
)


def _doc(mid, coord, *entities):
    return {
        "id": mid,
        "coord": list(coord),
        "enrichment": {
            "entities": [{"subject": s, "facet": f, "name": v} for (s, f, v) in entities]
        },
    }


def _snap(corpus):
    return {mid: {"coord": d["coord"]} for mid, d in corpus.items()}


def _counts(corpus, candidates):
    return scan_fingerprint_counts(candidates, index_snapshot=_snap(corpus), load_doc=corpus.get)


def test_recurring_value_outcounts_a_one_off():
    noah = _doc("1.00.noah", [1, 0, 0], ("operator", "name", "noah"))
    kim = _doc("1.00.kim", [1, 0, 0], ("operator", "name", "kim"))
    corpus = {
        "1.00.noah": noah,
        "1.00.kim": kim,
        "1.00.rel1": _doc("1.00.rel1", [1, 0, 0], ("operator", "name", "noah")),
        "1.00.rel2": _doc("1.00.rel2", [1, 0, 0], ("operator", "name", "noah")),
    }
    fp = _counts(corpus, [noah, kim])
    corr = pairwise_corroboration(noah, kim, fp)
    assert corr["1.00.noah"] == 3  # asserted by itself + rel1 + rel2
    assert corr["1.00.kim"] == 1  # only itself


def test_scan_is_scoped_to_candidate_lobes():
    noah = _doc("1.00.noah", [1, 0, 0], ("operator", "name", "noah"))
    corpus = {
        "1.00.noah": noah,
        "5.02.other": _doc("5.02.other", [5, 2, 0], ("operator", "name", "noah")),
    }
    fp = _counts(corpus, [noah])
    assert fp[("operator", "name", "noah")] == 1  # lobe-5 record is out of scope


def test_pairwise_is_scoped_to_the_shared_conflicting_axis():
    # REGRESSION: the winner must not inflate its corroboration via an UNRELATED facet. Both records
    # tie 1-1 on the conflicting CITY axis; the winner also has a heavily-corroborated EMPLOYER fact.
    # Axis-scoping must ignore employer -> the pair stays a 1-1 near-tie (no wrong tombstone).
    a = _doc("1.00.a", [1, 0, 0], ("operator", "city", "austin"), ("operator", "employer", "omega"))
    b = _doc("1.00.b", [1, 0, 0], ("operator", "city", "dallas"))
    corpus = {
        "1.00.a": a,
        "1.00.b": b,
        "1.00.e1": _doc("1.00.e1", [1, 0, 0], ("operator", "employer", "omega")),
        "1.00.e2": _doc("1.00.e2", [1, 0, 0], ("operator", "employer", "omega")),
    }
    fp = _counts(corpus, [a, b])
    corr = pairwise_corroboration(a, b, fp)
    assert corr["1.00.a"] == 1  # scoped to the shared CITY axis, NOT the 3x employer fact
    assert corr["1.00.b"] == 1  # -> a genuine near-tie, resolves to demotion not delete


def test_pairwise_zero_when_no_shared_axis():
    a = _doc("1.00.a", [1, 0, 0], ("operator", "name", "x"))
    b = _doc("1.00.b", [1, 0, 0], ("operator", "city", "y"))
    fp = _counts({"1.00.a": a, "1.00.b": b}, [a, b])
    corr = pairwise_corroboration(a, b, fp)
    assert corr == {"1.00.a": 0, "1.00.b": 0}  # nothing shared -> fall back to confidence


def test_facet_scoped_values_do_not_collide():
    name_x = _doc("1.00.n", [1, 0, 0], ("operator", "name", "sam"))
    city_x = _doc("1.00.c", [1, 0, 0], ("operator", "city", "sam"))
    corpus = {"1.00.n": name_x, "1.00.c": city_x}
    fp = _counts(corpus, [name_x, city_x])
    assert fp[("operator", "name", "sam")] == 1
    assert fp[("operator", "city", "sam")] == 1


def test_no_entities_yields_empty_scan():
    plain = {"id": "2.00.plain", "coord": [2, 0, 0]}
    assert _counts({"2.00.plain": plain}, [plain]) == {}
    assert pairwise_corroboration(plain, plain, {}) == {"2.00.plain": 0}


def test_deterministic_and_pure():
    a = _doc("1.00.a", [1, 0, 0], ("operator", "name", "x"))
    b = _doc("1.00.b", [1, 0, 0], ("operator", "name", "y"))
    corpus = {"1.00.a": a, "1.00.b": b}
    outs = [pairwise_corroboration(a, b, _counts(corpus, [a, b])) for _ in range(3)]
    assert outs[0] == outs[1] == outs[2]

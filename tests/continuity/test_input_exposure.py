"""WO-05 Step 2 — what was RETRIEVED, what was ADMITTED, and why the rest was not.

Acceptance case "Exposure": *record retrieved A/B/C, admitted A/C, excluded B with reason;
later readers cannot claim B was supplied.*

Today `auto_loop/mril_tools/candidate_hygiene.finalize_recall_results` is the real
input-admission owner: it drops non-responsive evidence, prefers qualified edges, dedups
across tools and enforces a shared candidate cap. It tracks the losses as `dropped_counts`
— **a count**. The identities and the reasons are discarded, so nothing downstream can say
which candidate was withheld or why, and a reader of the prompt cannot tell a record that
was never retrieved from one that was retrieved and then excluded.

The order also forbids standing in a general `memory_used` boolean
(`global_loop/package/run.py` derives one from a status string). A boolean cannot answer
"was B supplied?", which is the question that matters when the model's answer looks like it
used something it never saw.
"""

from __future__ import annotations

import pytest

from auto_loop.mril_tools import candidate_hygiene

QUERY = "todo app crud routes"


def _evidence(memory_id: str, text: str, confidence: float = 0.9) -> dict:
    return {"memory_id": memory_id, "remembered_as": text, "confidence": confidence}


def _one_tool_result(evidence: list[dict]) -> dict:
    return {
        "tool": "remember_knowledge",
        "lobe": 2,
        "verdict": "verified",
        "evidence": evidence,
    }


#: Three real candidates where the middle one is the "B" of the acceptance case: genuinely
#: retrieved, genuinely withheld. B restates A, so cross-tool de-duplication — this function's
#: documented purpose — excludes it.
#:
#: A first draft used an off-topic row expecting `not_query_responsive`, and it was admitted:
#: `_is_query_responsive` ends in a default `return True`, so with no relation concept in the
#: query and no subject scoping, an unrelated row is still responsive. Recorded because the
#: mistake is instructive — "obviously irrelevant to a human" is not this filter's contract.
CANDIDATES = [
    _evidence("m-A", "the todo app still needs crud routes in app.py", 0.95),
    _evidence("m-B", "the todo app still needs crud routes in app.py", 0.93),
    _evidence("m-C", "crud routes for the todo app were added to app.py", 0.91),
]


def _exposure():
    api = getattr(candidate_hygiene, "finalize_recall_results_with_exposure", None)
    assert api is not None, (
        "candidate_hygiene exposes no way to report retrieved-versus-admitted evidence. "
        "finalize_recall_results returns only the survivors and counts the rest, so an "
        "excluded candidate is indistinguishable from one that was never retrieved"
    )
    return api([_one_tool_result(list(CANDIDATES))], QUERY)


def test_the_admission_owner_reports_what_it_retrieved_and_what_it_admitted():
    results, exposure = _exposure()

    retrieved = [row["memory_id"] for row in exposure["retrieved"]]
    admitted = [row["memory_id"] for row in exposure["admitted"]]

    assert retrieved == ["m-A", "m-B", "m-C"], (
        f"the retrieved set must be every candidate the tools returned, in order; got {retrieved}"
    )
    assert "m-B" not in admitted, "the withheld duplicate must not be reported as admitted"
    assert set(admitted) == {"m-A", "m-C"}, f"expected A and C admitted, got {admitted}"

    # The admitted set is the evidence that actually survived into the results, not a
    # parallel bookkeeping list that could disagree with the prompt.
    survived = {
        ev.get("memory_id") for result in results for ev in (result.get("evidence") or [])
    }
    assert set(admitted) == survived, (
        f"the admitted receipt {sorted(admitted)} disagrees with the evidence that survived "
        f"into the results {sorted(survived)}; one of the two is lying to its reader"
    )


def test_an_excluded_candidate_carries_a_reason():
    _results, exposure = _exposure()

    excluded = {row["memory_id"]: row for row in exposure["excluded"]}

    assert "m-B" in excluded, "the withheld candidate is absent from the exclusion record"
    reason = str(excluded["m-B"].get("reason") or "")
    assert reason.strip(), (
        "the candidate was excluded with no reason recorded; 'it was dropped' is not evidence "
        "a later reader can act on"
    )
    assert reason in candidate_hygiene.EXCLUSION_REASONS, (
        f"the reason {reason!r} is not one of the declared exclusion reasons "
        f"{candidate_hygiene.EXCLUSION_REASONS}; a free-text reason cannot be reasoned about"
    )
    assert reason in ("duplicate_memory_id", "duplicate_semantics", "duplicate_content"), (
        f"B restates A, so it should be excluded as a duplicate; got {reason!r}"
    )


def test_a_reader_cannot_claim_an_excluded_record_was_supplied():
    """The acceptance case's real point, asserted as a property rather than a phrase."""
    _results, exposure = _exposure()

    admitted = {row["memory_id"] for row in exposure["admitted"]}
    excluded = {row["memory_id"] for row in exposure["excluded"]}
    retrieved = {row["memory_id"] for row in exposure["retrieved"]}

    assert admitted & excluded == set(), (
        "a candidate is reported as both admitted and excluded, so 'was it supplied?' has two "
        f"answers: {sorted(admitted & excluded)}"
    )
    assert admitted | excluded == retrieved, (
        "every retrieved candidate must be accounted for as either admitted or excluded; "
        f"unaccounted: {sorted(retrieved - (admitted | excluded))}"
    )


def test_retrieval_is_not_admission_and_neither_is_a_boolean():
    """A returned record is not necessarily admitted (the order's words).

    Pinned as a contract, not a coincidence of this fixture: with at least one exclusion the
    two sets must differ in size, so a caller cannot treat "we recalled something" as "this
    was supplied to the model" — which is exactly what a single memory_used flag does.
    """
    _results, exposure = _exposure()

    assert len(exposure["retrieved"]) > len(exposure["admitted"]), (
        "this fixture retrieves more than it admits; if the counts match, the exclusion was "
        "silently dropped rather than recorded"
    )
    assert not isinstance(exposure.get("admitted"), bool)
    assert "memory_used" not in exposure, (
        "a general memory_used boolean is exactly the summary the order forbids standing in "
        "for retrieval-versus-admission evidence"
    )


def test_the_plain_finalizer_still_returns_only_the_results():
    """The existing signature has many callers and must not change shape under them."""
    plain = candidate_hygiene.finalize_recall_results([_one_tool_result(list(CANDIDATES))], QUERY)

    assert isinstance(plain, list)
    assert plain and isinstance(plain[0], dict)
    with pytest.raises((TypeError, ValueError, KeyError, AttributeError)):
        # A tuple would unpack; a list of results must not.
        _a, _b = plain

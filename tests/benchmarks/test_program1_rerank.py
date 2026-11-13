from __future__ import annotations

import pytest

from benchmarks.memory_organ.program1.rerank import (
    AFTER,
    BEFORE,
    LinguisticReranker,
    NEGATIVE,
    POSITIVE,
    order_agreement,
)


@pytest.fixture(scope="module")
def reranker() -> LinguisticReranker:
    return LinguisticReranker()


# ── polarity ───────────────────────────────────────────────────────────────────

def test_polarity_detects_explicit_negation(reranker: LinguisticReranker) -> None:
    assert reranker.analyze("Unit-A is permitted to activate Relay-B.").polarity == POSITIVE
    assert reranker.analyze("Unit-A is not permitted to activate Relay-B.").polarity == NEGATIVE


@pytest.mark.parametrize(
    "text",
    [
        "Unit-A never completed the calibration of Relay-B.",
        "Unit-A has no standing access to Relay-B.",
        "Which memory says Unit-A is forbidden from activating Relay-B?",
        "Which memory says Unit-A lacks access to Relay-B?",
    ],
)
def test_polarity_detects_lexical_negation(reranker: LinguisticReranker, text: str) -> None:
    assert reranker.analyze(text).polarity == NEGATIVE


# ── temporal ───────────────────────────────────────────────────────────────────

def test_temporal_polarity(reranker: LinguisticReranker) -> None:
    assert reranker.analyze("Before revision 74, Unit-A routed telemetry through Route-74A.").temporal == BEFORE
    assert reranker.analyze("After revision 74, Unit-A now routes telemetry through Route-74B.").temporal == AFTER
    assert reranker.analyze("Which route did Unit-A use before revision 74?").temporal == BEFORE
    assert reranker.analyze("Which route does Unit-A use now after revision 74?").temporal == AFTER
    assert reranker.analyze("The chef roasted the lamb in the kitchen.").temporal == 0


# ── argument order ─────────────────────────────────────────────────────────────

def test_order_agreement_separates_a_role_reversal_pair(reranker: LinguisticReranker) -> None:
    """The discriminating signal is order: the word multisets are identical."""
    query = reranker.analyze("Which memory says Unit-7D77 gave Unit-BF06 permission to inspect Relay-808C?")
    right = reranker.analyze("Unit-7D77 authorized Unit-BF06 to inspect Relay-808C during maintenance.")
    wrong = reranker.analyze("Unit-BF06 authorized Unit-7D77 to inspect Relay-808C during maintenance.")
    assert sorted(right.tokens) == sorted(wrong.tokens), "pair must be an order-only contrast"
    assert order_agreement(query, right) > order_agreement(query, wrong)


def test_order_agreement_is_neutral_without_shared_pairs(reranker: LinguisticReranker) -> None:
    query = reranker.analyze("Which record keeper placed the account book in the strongroom?")
    candidate = reranker.analyze("The archivist stored the ledger in the vault.")
    assert order_agreement(query, candidate) == 0.0


# ── reranking ──────────────────────────────────────────────────────────────────

def _ids(ranked):
    return [candidate_id for candidate_id, _score in ranked]


def test_rerank_fixes_a_role_reversal_inversion(reranker: LinguisticReranker) -> None:
    query = "Which memory says Unit-7D77 gave Unit-BF06 permission to inspect Relay-808C?"
    candidates = [
        ("wrong", "Unit-BF06 authorized Unit-7D77 to inspect Relay-808C during maintenance.", 0.90),
        ("right", "Unit-7D77 authorized Unit-BF06 to inspect Relay-808C during maintenance.", 0.88),
    ]
    assert _ids(reranker.rerank(query, candidates))[0] == "right"


def test_rerank_fixes_a_negation_inversion(reranker: LinguisticReranker) -> None:
    query = "Which memory says Unit-FA02 is forbidden from activating Relay-808C during maintenance?"
    candidates = [
        ("wrong", "Unit-FA02 is permitted to activate Relay-808C during the maintenance window.", 0.90),
        ("right", "Unit-FA02 is not permitted to activate Relay-808C during the maintenance window.", 0.88),
    ]
    assert _ids(reranker.rerank(query, candidates))[0] == "right"


def test_rerank_fixes_a_temporal_inversion(reranker: LinguisticReranker) -> None:
    query = "Which route did Unit-FA02 use before revision 74?"
    candidates = [
        ("wrong", "After revision 74, Unit-FA02 now routes telemetry through Route-74B.", 0.90),
        ("right", "Before revision 74, Unit-FA02 routed telemetry through Route-74A.", 0.88),
    ]
    assert _ids(reranker.rerank(query, candidates))[0] == "right"


def test_rerank_preserves_base_order_when_no_signal_fires(reranker: LinguisticReranker) -> None:
    """Without polarity, temporal or order evidence the retriever's ranking stands."""
    query = "Which record keeper placed the account book in the strongroom?"
    candidates = [
        ("a", "The archivist stored the ledger in the vault.", 0.80),
        ("b", "The chef roasted the lamb in the kitchen.", 0.70),
        ("c", "The sailor furled the mainsail on the deck.", 0.60),
    ]
    assert _ids(reranker.rerank(query, candidates)) == ["a", "b", "c"]


def test_rerank_is_deterministic_and_total(reranker: LinguisticReranker) -> None:
    query = "Which memory says Unit-A is forbidden from activating Relay-B?"
    candidates = [
        ("x", "Unit-A is permitted to activate Relay-B.", 0.5),
        ("y", "Unit-A is not permitted to activate Relay-B.", 0.5),
        ("z", "The chef roasted the lamb in the kitchen.", 0.5),
    ]
    first = reranker.rerank(query, candidates)
    assert first == reranker.rerank(query, candidates)
    assert sorted(_ids(first)) == ["x", "y", "z"]


def test_rerank_rejects_a_negative_or_zero_window(reranker: LinguisticReranker) -> None:
    with pytest.raises(ValueError):
        reranker.rerank("q", [("a", "text", 1.0)], top_k=0)


def test_order_feature_is_withheld_on_low_overlap_candidates(
    reranker: LinguisticReranker,
) -> None:
    """Incidentally shared tokens must not perturb ordinary ranking.

    An earlier draft applied argument order to any candidate sharing two tokens,
    which cost ordinary_synonyms Recall@10 0.9433 -> 0.8967 on development.
    """
    query = reranker.analyze("Which physician assessed the x-ray plate in the infirmary?")
    unrelated = reranker.analyze("The gardener planted the saplings in the orchard yesterday.")
    assert order_agreement(query, unrelated) == 0.0


def test_generic_past_tense_is_not_a_temporal_marker(reranker: LinguisticReranker) -> None:
    """"was"/"did"/"used" appear throughout ordinary prose and are not signals."""
    assert reranker.analyze("The ledger was stored in the vault.").temporal == 0
    assert reranker.analyze("Which unit did the technician inspect?").temporal == 0
    assert reranker.analyze("The chef used the skillet in the kitchen.").temporal == 0


def test_contextual_temporal_markers_need_a_time_noun(reranker: LinguisticReranker) -> None:
    """"from"/"through" count only when they govern a time noun."""
    assert reranker.analyze("From week 12, Unit-A stores backups on Volume-B.").temporal == AFTER
    assert reranker.analyze("Through cycle 12, Unit-A ran profile Alpha.").temporal == BEFORE
    # Ordinary prose uses of the same words must stay inert.
    assert reranker.analyze("The technician removed the bolt from the panel.").temporal == 0
    assert reranker.analyze("The cable runs through the conduit.").temporal == 0


def test_incidental_negation_in_unrelated_prose_is_not_a_contradiction(
    reranker: LinguisticReranker,
) -> None:
    """43.5% of omega candidates contain a negation word incidentally.

    Penalising them against an unmarked query cost Recall@10 0.9329 -> 0.9074 on
    development, so polarity is gated on lexical coverage.
    """
    query = reranker.analyze("Which physician assessed the x-ray plate in the infirmary?")
    unrelated = reranker.analyze("The pressure gate does not open an episode without a cue.")
    assert unrelated.polarity == NEGATIVE
    base = 0.5
    # No polarity twin in the window, so polarity never activates.
    assert reranker.score(query, unrelated, base) == pytest.approx(base)
    assert reranker.polarity_contrast_set([query, unrelated]) == set()


def test_polarity_still_fires_on_a_genuine_negation_contrast(
    reranker: LinguisticReranker,
) -> None:
    query = reranker.analyze(
        "Which memory says Unit-FA02 is forbidden from activating Relay-808C during maintenance?"
    )
    negated = reranker.analyze(
        "Unit-FA02 is not permitted to activate Relay-808C during the maintenance window."
    )
    affirmed = reranker.analyze(
        "Unit-FA02 is permitted to activate Relay-808C during the maintenance window."
    )
    active = reranker.polarity_contrast_set([negated, affirmed])
    assert active == {0, 1}, "a near-duplicate polarity pair must activate"
    assert reranker.score(query, negated, 0.5, polarity_active=True) > reranker.score(
        query, affirmed, 0.5, polarity_active=True
    )


def test_paraphrased_query_still_gets_its_negation_contrast(
    reranker: LinguisticReranker,
) -> None:
    """The case that broke the old query-coverage gate.

    "finished calibrating" vs "completed the calibration" sits at only 0.50-0.67
    query-candidate coverage, so a query-side floor silenced it. The contrast
    lives between the two candidates, not between query and candidate.
    """
    query = "Which memory says Dr. EAE9 finished calibrating Sample-1BC4 before shipping?"
    candidates = [
        ("wrong", "Dr. EAE9 never completed the calibration of Sample-1BC4 before shipping.", 0.90),
        ("right", "Dr. EAE9 completed the calibration of Sample-1BC4 before shipping.", 0.88),
    ]
    assert _ids(reranker.rerank(query, candidates))[0] == "right"


def test_polarity_contrast_needs_near_duplicates_not_merely_opposite_polarity(
    reranker: LinguisticReranker,
) -> None:
    negated = reranker.analyze("The chef never roasted the lamb in the kitchen.")
    unrelated_positive = reranker.analyze("The astronomer aligned the telescope in the dome.")
    assert negated.polarity == NEGATIVE
    assert unrelated_positive.polarity == POSITIVE
    assert reranker.polarity_contrast_set([negated, unrelated_positive]) == set()


def test_polarity_penalises_contradiction_without_rewarding_agreement(
    reranker: LinguisticReranker,
) -> None:
    """Agreement is not evidence of rightness.

    Rewarding it promoted whichever member of a contrast pair matched an
    unmarked query, costing zero_token_overlap_paraphrase 0.5167 -> 0.5067.
    """
    query = reranker.analyze("Which memory says Unit-A completed the calibration of Relay-B?")
    agreeing = reranker.analyze("Unit-A completed the calibration of Relay-B before shipping.")
    contradicting = reranker.analyze("Unit-A never completed the calibration of Relay-B before shipping.")
    base = 0.5
    assert reranker.score(query, agreeing, base, polarity_active=True) == pytest.approx(
        reranker.score(query, agreeing, base, polarity_active=False)
    )
    assert reranker.score(query, contradicting, base, polarity_active=True) < base

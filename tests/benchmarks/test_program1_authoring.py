from __future__ import annotations

import pytest

from benchmarks.memory_organ.program1.authoring import (
    AUTHOR_REQUIRED_STRATA,
    assemble_corpus,
    build_authoring_jobs,
    synthetic_pair_payload,
    zero_overlap_ok,
)


def _row(
    candidate_id: str,
    *,
    partition: str = "development",
    stratum: str,
    source_class: str = "omega",
    text: str,
    pair_id: str | None = None,
    pair_role: str | None = None,
) -> dict:
    return {
        "source_id": f"src:{candidate_id}",
        "source_class": source_class,
        "candidate_id": candidate_id,
        "partition": partition,
        "stratum": stratum,
        "record": {"title": "", "summary": "", "text": text},
        "source_provenance": {"kind": "test"},
        "structured_fact": {"kind": "test"} if source_class == "synthetic" else None,
        "contrast_pair_id": pair_id,
        "contrast_role": pair_role,
    }


def test_synthetic_role_reversal_pair_is_controlled_and_symmetric() -> None:
    pair = [
        _row("a", stratum="role_reversal", source_class="synthetic", text="seed a",
             pair_id="pair-1", pair_role="contrast_a"),
        _row("b", stratum="role_reversal", source_class="synthetic", text="seed b",
             pair_id="pair-1", pair_role="contrast_b"),
    ]
    payload = synthetic_pair_payload(pair)
    assert set(payload) == {"a", "b"}
    assert payload["a"]["candidate_text"] != payload["b"]["candidate_text"]
    # The contract is a PURE argument swap, whichever surface template was drawn:
    # both sides must use the identical multiset of words in a different order.
    assert sorted(payload["a"]["candidate_text"].split()) == sorted(
        payload["b"]["candidate_text"].split()
    )
    assert payload["a"]["query"] != payload["b"]["query"]
    assert payload["a"]["hard_negative_id"] == "b"
    assert payload["b"]["hard_negative_id"] == "a"


def test_natural_authoring_jobs_never_rewrite_candidate_truth() -> None:
    original = "Pressure may open an episode, but it never chooses a concrete tool."
    allocation = [
        _row("syn", stratum="ordinary_synonyms", text=original),
        _row("exact", stratum="exact_lexical_restatement", text="A canonical fact stays unchanged."),
    ]
    jobs = build_authoring_jobs(allocation)
    assert AUTHOR_REQUIRED_STRATA.issuperset({"ordinary_synonyms"})
    assert [job["candidate_id"] for job in jobs] == ["syn"]
    assert jobs[0]["candidate_text"] == original
    assert "candidate_text" not in jobs[0]["expected_output_fields"]


def test_zero_overlap_uses_omega_content_token_contract() -> None:
    assert zero_overlap_ok("Which telescope did I use?", "Observed Saturn through the Dobsonian") is True
    assert zero_overlap_ok("Which Dobsonian did I use?", "Observed Saturn through the Dobsonian") is False


def test_assemble_corpus_sets_hard_negative_and_keeps_no_signal_targetless() -> None:
    allocation = [
        _row("hard-a", stratum="negation", source_class="synthetic", text="seed a",
             pair_id="pair-n", pair_role="contrast_a"),
        _row("hard-b", stratum="negation", source_class="synthetic", text="seed b",
             pair_id="pair-n", pair_role="contrast_b"),
        _row("no-signal", stratum="no_signal", text="This remains a distractor-only candidate."),
    ]
    candidates, cases = assemble_corpus(allocation, authored_queries={})
    by_case = {case["primary_target_id"]: case for case in cases if case["primary_target_id"] is not None}
    assert by_case["hard-a"]["hard_negative_ids"] == ["hard-b"]
    assert by_case["hard-b"]["hard_negative_ids"] == ["hard-a"]
    no_signal = next(case for case in cases if case["stratum"] == "no_signal")
    assert no_signal["primary_target_id"] is None
    assert no_signal["query"].strip() == ""
    assert len(candidates) == 3


# ── zero_token_overlap_paraphrase becomes a CONTROLLED synthetic stratum ─────────────────────
# Measured: natural candidates could not support it (median 27.5 content tokens to avoid; only 11
# of 500 were <=15 tokens and identifier-free; author pass rate 0/12 on both a 4B and a 9B).
# Generating the pair makes zero overlap true BY CONSTRUCTION instead of hoping a model complies.

from benchmarks.memory_organ.program1.authoring import synthetic_zero_overlap_payloads  # noqa: E402
from benchmarks.memory_organ.program1.query_author import content_tokens  # noqa: E402


def _zo_rows(count: int) -> list[dict]:
    return [
        {"candidate_id": f"p1c:zo{index:04d}", "partition": "development",
         "stratum": "zero_token_overlap_paraphrase", "source_class": "synthetic"}
        for index in range(count)
    ]


def test_synthetic_zero_overlap_query_shares_no_content_token_with_its_candidate() -> None:
    payloads = synthetic_zero_overlap_payloads(_zo_rows(500))
    assert len(payloads) == 500
    for candidate_id, payload in payloads.items():
        candidate, query = payload["candidate_text"], payload["query"]
        assert candidate and query and len(query) <= 120, candidate_id
        overlap = content_tokens(query) & content_tokens(candidate)
        assert overlap == set(), f"{candidate_id} shares {sorted(overlap)}"


def test_synthetic_zero_overlap_facts_are_unique_and_deterministic() -> None:
    rows = _zo_rows(500)
    first = synthetic_zero_overlap_payloads(rows)
    again = synthetic_zero_overlap_payloads(list(reversed(rows)))
    assert first == again                                            # row order cannot change the output
    texts = [p["candidate_text"] for p in first.values()]
    queries = [p["query"] for p in first.values()]
    assert len(set(texts)) == len(texts)                             # every fact is distinct...
    assert len(set(queries)) == len(queries)                         # ...and so is every query


def test_synthetic_zero_overlap_refuses_non_synthetic_or_wrong_stratum_rows() -> None:
    natural = _zo_rows(1)
    natural[0]["source_class"] = "omega"
    with pytest.raises(ValueError, match="synthetic"):
        synthetic_zero_overlap_payloads(natural)
    wrong = _zo_rows(1)
    wrong[0]["stratum"] = "ordinary_synonyms"
    with pytest.raises(ValueError, match="zero_token_overlap"):
        synthetic_zero_overlap_payloads(wrong)


# ── synthetic surface diversity (development diagnosis 2026-08-18) ──────────────
# The first synthetic cohort walked its slot lexicon with mixed-radix enumeration,
# so consecutive candidates differed in exactly ONE word. Every candidate had
# ~5-15 near twins, the cohort's nearest-neighbour cosine was 0.96 (natural
# material sits at 0.64), and zero-overlap Recall@10 collapsed to 0.38 because
# top-10 is a nearest-neighbour question.


def _zero_overlap_rows(count: int) -> list[dict]:
    return [
        {
            "candidate_id": f"p1c:zo{index:05d}",
            "source_class": "synthetic",
            "stratum": "zero_token_overlap_paraphrase",
        }
        for index in range(count)
    ]


def test_zero_overlap_neighbours_differ_in_every_slot() -> None:
    """Adjacent candidates must not be one-word variants of each other."""
    from benchmarks.memory_organ.program1.authoring import synthetic_zero_overlap_payloads

    rows = _zero_overlap_rows(60)
    payloads = synthetic_zero_overlap_payloads(rows)
    texts = [payloads[row["candidate_id"]]["candidate_text"] for row in rows]
    for left, right in zip(texts, texts[1:], strict=False):
        shared = content_tokens(left) & content_tokens(right)
        assert not shared, f"adjacent candidates share {sorted(shared)}:\n  {left}\n  {right}"


def test_zero_overlap_cohort_has_few_near_twins() -> None:
    """No candidate may have a twin sharing most of its content tokens."""
    from benchmarks.memory_organ.program1.authoring import synthetic_zero_overlap_payloads

    rows = _zero_overlap_rows(300)
    payloads = synthetic_zero_overlap_payloads(rows)
    tokens = [content_tokens(payloads[row["candidate_id"]]["candidate_text"]) for row in rows]
    worst = 0.0
    for i, left in enumerate(tokens):
        for right in tokens[i + 1 :]:
            union = left | right
            if union:
                worst = max(worst, len(left & right) / len(union))
    # Slot sizes are distinct primes (2/3/5/7), so two facts in one domain coincide
    # on k slots only when their local indices differ by a multiple of those k
    # primes. Coinciding on 3 of 4 needs a multiple of 30 and is rare; coinciding
    # on 2 of 4 is expected and yields Jaccard ~0.5. What must never recur is the
    # original defect: near-twins differing in a single slot (Jaccard ~0.86).
    assert worst <= 0.70, f"max pairwise content-token Jaccard {worst:.3f} is twin-like"


def test_zero_overlap_uses_several_sentence_frames() -> None:
    """A single frame shared by every candidate inflates baseline similarity."""
    from benchmarks.memory_organ.program1.authoring import synthetic_zero_overlap_payloads

    from benchmarks.memory_organ.program1.authoring import _ZERO_OVERLAP_FRAMES

    assert len(_ZERO_OVERLAP_FRAMES) >= 3
    rows = _zero_overlap_rows(120)
    payloads = synthetic_zero_overlap_payloads(rows)
    openings = {payloads[row["candidate_id"]]["candidate_text"].split()[0] for row in rows}
    assert len(openings) >= 3, f"frames all open the same way: {openings}"


def test_zero_overlap_lexicon_sides_are_globally_disjoint() -> None:
    """Every A-side word must be stem-disjoint from every B-side word."""
    from benchmarks.memory_organ.program1.authoring import _ZERO_OVERLAP_SLOTS

    left_stems: set[str] = set()
    right_stems: set[str] = set()
    for slot in _ZERO_OVERLAP_SLOTS:
        for a_side, b_side in slot:
            left_stems |= content_tokens(a_side)
            right_stems |= content_tokens(b_side)
    collision = left_stems & right_stems
    assert not collision, f"lexicon sides collide on {sorted(collision)}"


def test_zero_overlap_slot_sizes_are_distinct_primes() -> None:
    """Distinct prime sizes make index % size advance every slot at once."""
    from benchmarks.memory_organ.program1.authoring import _ZERO_OVERLAP_SLOTS

    from benchmarks.memory_organ.program1.authoring import (
        _ZERO_OVERLAP_DOMAINS,
        _ZERO_OVERLAP_SLOT_KEYS,
    )

    for domain in _ZERO_OVERLAP_DOMAINS:
        sizes = [len(domain[key]) for key in _ZERO_OVERLAP_SLOT_KEYS]
        assert len(set(sizes)) == len(sizes), f"slot sizes repeat within a domain: {sizes}"
        for size in sizes:
            assert size > 1 and all(size % d for d in range(2, int(size**0.5) + 1)), f"{size} not prime"


def test_zero_overlap_survives_a_spacy_lemma_audit() -> None:
    """Independent second opinion on the zero-overlap contract.

    ``content_tokens`` enforces the contract with a light stemmer, which can miss
    irregular pairs that share a lemma. spaCy caught exactly that: the geology
    domain once paired the place "quarry" (A side) with the action "quarried"
    (B side), which the stemmer separated and the lemmatizer correctly merged.
    """
    spacy = pytest.importorskip("spacy")
    try:
        nlp = spacy.load("en_core_web_sm", disable=["ner", "parser"])
    except OSError:  # pragma: no cover - model not installed
        pytest.skip("en_core_web_sm is not installed")

    from benchmarks.memory_organ.program1.authoring import synthetic_zero_overlap_payloads

    def lemmas(text: str) -> set[str]:
        return {
            token.lemma_.lower()
            for token in nlp(text)
            if not token.is_stop and not token.is_punct and not token.is_space
            and len(token.lemma_) > 1
        }

    payloads = synthetic_zero_overlap_payloads(_zero_overlap_rows(160))
    for candidate_id, payload in sorted(payloads.items()):
        shared = lemmas(payload["query"]) & lemmas(payload["candidate_text"])
        assert not shared, (
            f"{candidate_id} shares lemma(s) {sorted(shared)}\n"
            f"  candidate: {payload['candidate_text']}\n  query: {payload['query']}"
        )

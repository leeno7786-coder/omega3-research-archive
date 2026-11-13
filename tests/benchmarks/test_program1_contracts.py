from __future__ import annotations

import copy

import pytest

from benchmarks.memory_organ.program1.contracts import (
    DIAGNOSTIC_K,
    G0_DEFERRED_TO_RERANK,
    RERANK_THRESHOLDS,
    RETRIEVAL_K,
    CASE_COUNTS_BY_PARTITION,
    CANDIDATE_COUNTS_BY_SOURCE,
    G0_THRESHOLDS,
    NO_SIGNAL_STRATUM,
    PARTITIONS,
    RENDERER_VERSION,
    STRATA,
    CorpusContractError,
    render_candidate,
    validate_corpus,
)


def test_program1_operator_amendment_constants_are_frozen() -> None:
    assert RENDERER_VERSION == "omega.semantic_retina.render.v1"
    assert len(STRATA) == 15
    assert NO_SIGNAL_STRATUM == "no_signal"
    assert CASE_COUNTS_BY_PARTITION == {"development": 4500, "validation": 1500, "locked_test": 1500}
    # ratified 2026-08-18: 700 candidates moved to the controlled synthetic path (temporal_update,
    # zero_token_overlap_paraphrase); the omega share stays at 40%
    assert CANDIDATE_COUNTS_BY_SOURCE == {"omega": 3000, "public": 1550, "synthetic": 2950}
    assert G0_THRESHOLDS["ordinary_positive_pairwise"] == 0.95
    assert G0_THRESHOLDS["zero_overlap_pairwise"] == 0.90
    # ratified 2026-08-18: the retina is gated as a FIRST STAGE at K=32, the width
    # of the rerank window. The numeric bars did not move -- only K. Development
    # measured Recall@32 0.840 against Recall@10 0.517 on the binding stratum.
    assert RETRIEVAL_K == 32
    assert DIAGNOSTIC_K == 10
    assert G0_THRESHOLDS["semantic_family_recall_at_32"] == 0.90
    assert G0_THRESHOLDS["stratum_floor_at_32"] == 0.80
    # Upper bound: the one first-stage failure a reranker cannot repair.
    assert G0_THRESHOLDS["hard_negative_displacement_at_32"] == 0.01
    # Separating a target from its near-identical hard negative is a rerank
    # obligation, deferred rather than deleted: still computed, still reported,
    # gated at 0.90 when a reranker exists.
    assert G0_DEFERRED_TO_RERANK == ("hard_distinction_pairwise",)
    assert RERANK_THRESHOLDS["hard_distinction_pairwise"] == 0.90
    assert "hard_distinction_pairwise" not in G0_THRESHOLDS


def test_render_v1_is_ordered_exact_deduplicated_and_metadata_free() -> None:
    record = {
        "id": "2.00.secret",
        "title": "  Alpha  ",
        "summary": "Beta",
        "text": "Beta",
        "tags": ["leak-me-not"],
        "domain": "private-domain",
    }
    assert render_candidate(record) == "Alpha\nBeta"
    assert render_candidate({"title": "", "summary": "  Gamma ", "text": "Delta"}) == "Gamma\nDelta"
    assert render_candidate({"title": "  ", "summary": "", "text": ""}) == ""


def _source_for(index: int) -> str:
    if index < 3000:
        return "omega"
    if index < 4550:
        return "public"
    return "synthetic"


def _complete_corpus() -> tuple[list[dict], list[dict]]:
    candidates: list[dict] = []
    cases: list[dict] = []
    global_index = 0
    per_partition = {"development": 300, "validation": 100, "locked_test": 100}
    for stratum in STRATA:
        for partition in PARTITIONS:
            for local_index in range(per_partition[partition]):
                token = f"{partition}:{stratum}:{local_index}"
                candidate_id = f"cand:{global_index:04d}"
                semantic_family = f"sem:{token}"
                candidates.append(
                    {
                        "candidate_id": candidate_id,
                        "partition": partition,
                        "source_class": _source_for(global_index),
                        "semantic_family_id": semantic_family,
                        "fact_family_id": f"fact:{token}",
                        "source_memory_id": f"src:{token}",
                        "template_family_id": f"template:{token}",
                        "record": {
                            "title": f"Title {global_index}",
                            "summary": f"Summary {global_index}",
                            "text": f"Text {global_index}",
                        },
                        "provenance": {"source": "test"},
                    }
                )
                no_signal = stratum == NO_SIGNAL_STRATUM
                cases.append(
                    {
                        "case_id": f"case:{global_index:04d}",
                        "partition": partition,
                        "stratum": stratum,
                        "query": "" if no_signal else f"Query {global_index}",
                        "primary_target_id": None if no_signal else candidate_id,
                        "hard_negative_ids": [],
                        "unrelated_negative_ids": [],
                        "semantic_family_id": semantic_family,
                        "fact_family_id": f"fact:{token}",
                        "source_memory_id": f"src:{token}",
                        "template_family_id": f"template:{token}",
                        "expected_relation": "no_signal" if no_signal else "target",
                        "provenance": {"source": "test"},
                    }
                )
                global_index += 1

    by_partition: dict[str, list[str]] = {partition: [] for partition in PARTITIONS}
    for candidate in candidates:
        by_partition[str(candidate["partition"])].append(str(candidate["candidate_id"]))
    for case in cases:
        if case["stratum"] == NO_SIGNAL_STRATUM:
            continue
        pool = by_partition[str(case["partition"])]
        target = str(case["primary_target_id"])
        target_index = pool.index(target)
        comparator = pool[(target_index + 1) % len(pool)]
        if case["stratum"] in {
            "exact_lexical_restatement", "morphology_variation", "ordinary_synonyms",
            "zero_token_overlap_paraphrase", "domain_term_vs_lay_term",
        }:
            case["unrelated_negative_ids"] = [comparator]
        if case["stratum"] in {
            "role_reversal", "negation", "temporal_update",
            "polysemy_context_shift", "adversarial_semantic_near_neighbor",
        }:
            case["hard_negative_ids"] = [comparator]
    return candidates, cases


@pytest.fixture(scope="module")
def complete_corpus() -> tuple[list[dict], list[dict]]:
    return _complete_corpus()


def test_complete_operator_locked_corpus_contract_passes(complete_corpus) -> None:
    candidates, cases = complete_corpus
    report = validate_corpus(candidates, cases)
    assert report["candidate_count"] == 7500
    assert report["case_count"] == 7500
    assert report["target_bearing_cases"] == 7000
    assert report["no_signal_cases"] == 500
    assert report["distractor_only_candidates"] == 500
    assert report["case_counts_by_partition"] == CASE_COUNTS_BY_PARTITION
    assert report["candidate_counts_by_source"] == CANDIDATE_COUNTS_BY_SOURCE


def test_cross_partition_fact_family_is_refused(complete_corpus) -> None:
    candidates, cases = copy.deepcopy(complete_corpus)
    dev = next(row for row in cases if row["partition"] == "development")
    val = next(row for row in cases if row["partition"] == "validation")
    val["fact_family_id"] = dev["fact_family_id"]
    with pytest.raises(CorpusContractError, match="fact_family_id crosses partitions"):
        validate_corpus(candidates, cases)


def test_primary_targets_are_one_to_one_and_partition_local(complete_corpus) -> None:
    candidates, cases = copy.deepcopy(complete_corpus)
    target_cases = [row for row in cases if row["primary_target_id"] is not None]
    assert len({row["primary_target_id"] for row in target_cases}) == 7000

    first, second = target_cases[0], target_cases[1]
    second["primary_target_id"] = first["primary_target_id"]
    with pytest.raises(CorpusContractError, match="primary target reused"):
        validate_corpus(candidates, cases)


def test_no_signal_case_must_have_no_target_or_candidate_labels(complete_corpus) -> None:
    candidates, cases = copy.deepcopy(complete_corpus)
    row = next(item for item in cases if item["stratum"] == NO_SIGNAL_STRATUM)
    row["primary_target_id"] = candidates[0]["candidate_id"]
    with pytest.raises(CorpusContractError, match="no_signal case has a target"):
        validate_corpus(candidates, cases)


def test_source_mix_is_exact_not_advisory(complete_corpus) -> None:
    candidates, cases = copy.deepcopy(complete_corpus)
    candidates[-1]["source_class"] = "omega"
    with pytest.raises(CorpusContractError, match="candidate source mix"):
        validate_corpus(candidates, cases)


def test_g0_scored_strata_require_their_preregistered_comparators(complete_corpus) -> None:
    candidates, cases = copy.deepcopy(complete_corpus)
    ordinary = next(row for row in cases if row["stratum"] == "ordinary_synonyms")
    zero = next(row for row in cases if row["stratum"] == "zero_token_overlap_paraphrase")
    hard = next(row for row in cases if row["stratum"] == "negation")

    ordinary["unrelated_negative_ids"] = []
    with pytest.raises(CorpusContractError, match="ordinary_synonyms requires unrelated_negative_ids"):
        validate_corpus(candidates, cases)

    candidates, cases = copy.deepcopy(complete_corpus)
    zero = next(row for row in cases if row["stratum"] == "zero_token_overlap_paraphrase")
    zero["unrelated_negative_ids"] = []
    with pytest.raises(CorpusContractError, match="zero_token_overlap_paraphrase requires unrelated_negative_ids"):
        validate_corpus(candidates, cases)

    candidates, cases = copy.deepcopy(complete_corpus)
    hard = next(row for row in cases if row["stratum"] == "negation")
    hard["hard_negative_ids"] = []
    with pytest.raises(CorpusContractError, match="negation requires hard_negative_ids"):
        validate_corpus(candidates, cases)


# ── A11: K is sized to the pool (operator ratification 2026-08-18) ─────────────

def test_retrieval_k_is_sized_to_the_pool() -> None:
    """K=32 was calibrated for ~2,700 candidates and never re-sized.

    Measured smallest K holding a 0.90 floor: 18/36/48/61 at pools
    1500/3000/4500/6000, i.e. 12.0 -> 10.17 K per 1000 candidates, growing
    sublinearly. The rule uses the WORST (largest) observed ratio so it never
    under-sizes.
    """
    from benchmarks.memory_organ.program1.contracts import (
        K_PER_1000,
        MIN_RETRIEVAL_K,
        retrieval_k_for_pool,
    )

    assert MIN_RETRIEVAL_K == 32
    assert K_PER_1000[0.90] == 12.0

    # Never below the legacy contract value.
    assert retrieval_k_for_pool(500) == 32
    assert retrieval_k_for_pool(1500) == 32
    # Above ~2700 candidates K must grow.
    assert retrieval_k_for_pool(6000) >= 61, "must cover the measured requirement"
    assert retrieval_k_for_pool(6000) == 72
    # Stricter floor target demands a wider window.
    assert retrieval_k_for_pool(6000, floor_target=0.95) > retrieval_k_for_pool(6000)


def test_retrieval_k_covers_every_measured_requirement() -> None:
    """The rule must dominate the measurements it was fitted to."""
    from benchmarks.memory_organ.program1.contracts import retrieval_k_for_pool

    measured = {1500: 18, 3000: 36, 4500: 48, 6000: 61}
    for pool, needed in measured.items():
        assert retrieval_k_for_pool(pool) >= needed, (pool, needed)


def test_legacy_k32_remains_correct_for_the_frozen_g0_evidence() -> None:
    """The frozen FP32 G0 evidence used K=32 on a 1500-candidate pool.

    The rule endorses that: 1500 candidates need K=18 for a 0.90 floor, and the
    measured floor at K=32 was 0.98. The amendment does not invalidate it.
    """
    from benchmarks.memory_organ.program1.contracts import retrieval_k_for_pool

    assert retrieval_k_for_pool(1500) == 32


def test_retrieval_k_refuses_a_nonsense_pool_or_target() -> None:
    from benchmarks.memory_organ.program1.contracts import retrieval_k_for_pool

    with pytest.raises(ValueError):
        retrieval_k_for_pool(0)
    with pytest.raises(ValueError):
        retrieval_k_for_pool(1000, floor_target=0.5)

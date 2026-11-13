from __future__ import annotations

import numpy as np
import pytest

from benchmarks.memory_organ.program1.g0 import evaluate_g0


def _unit(index: int, dim: int = 256) -> np.ndarray:
    row = np.zeros(dim, dtype=np.float32)
    row[index] = 1.0
    return row


def test_g0_scores_full_partition_ranking_and_pairwise_groups() -> None:
    candidate_ids = ["a", "b", "c", "d", "e", "f"]
    candidate_embeddings = np.stack([_unit(i) for i in range(6)])
    candidate_families = {
        "a": "family-a", "b": "family-b", "c": "family-c",
        "d": "family-d", "e": "family-e", "f": "family-f",
    }
    cases = [
        {"case_id": "ordinary", "stratum": "ordinary_synonyms", "primary_target_id": "a",
         "semantic_family_id": "family-a", "unrelated_negative_ids": ["b"], "hard_negative_ids": []},
        {"case_id": "zero", "stratum": "zero_token_overlap_paraphrase", "primary_target_id": "c",
         "semantic_family_id": "family-c", "unrelated_negative_ids": ["d"], "hard_negative_ids": []},
        {"case_id": "hard", "stratum": "negation", "primary_target_id": "e",
         "semantic_family_id": "family-e", "unrelated_negative_ids": [], "hard_negative_ids": ["f"]},
    ]
    query_embeddings = np.stack([_unit(0), _unit(2), _unit(4)])

    report = evaluate_g0(
        candidate_ids=candidate_ids,
        candidate_embeddings=candidate_embeddings,
        candidate_families=candidate_families,
        cases=cases,
        query_embeddings=query_embeddings,
        zero_vectors=0,
        non_finite_vectors=0,
        repeat_mismatches=0,
    )

    assert report["metrics"]["ordinary_positive_pairwise"] == 1.0
    assert report["metrics"]["zero_overlap_pairwise"] == 1.0
    assert report["metrics"]["hard_distinction_pairwise"] == 1.0
    assert report["metrics"]["semantic_family_recall_at_10"] == 1.0
    assert report["per_stratum_recall_at_10"] == {
        "negation": 1.0,
        "ordinary_synonyms": 1.0,
        "zero_token_overlap_paraphrase": 1.0,
    }
    assert report["query_results"][0]["primary_rank"] == 1


def test_g0_fails_when_target_loses_to_hard_negative() -> None:
    candidate_ids = ["target", "trap"]
    candidates = np.stack([_unit(0), _unit(1)])
    query = (_unit(1) * 0.95 + _unit(0) * 0.05).reshape(1, -1)
    cases = [{"case_id": "hard", "stratum": "negation", "primary_target_id": "target",
              "semantic_family_id": "target-family", "unrelated_negative_ids": [],
              "hard_negative_ids": ["trap"]}]
    report = evaluate_g0(candidate_ids=candidate_ids, candidate_embeddings=candidates,
                         candidate_families={"target": "target-family", "trap": "trap-family"},
                         cases=cases, query_embeddings=query, zero_vectors=0,
                         non_finite_vectors=0, repeat_mismatches=0)
    assert report["metrics"]["hard_distinction_pairwise"] == 0.0
    assert report["query_results"][0]["primary_rank"] == 2


def _no_signal_fixture() -> dict:
    return {
        "candidate_ids": ["c1", "c2"],
        "candidate_embeddings": np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
        "candidate_families": {"c1": "f1", "c2": "f2"},
        "cases": [
            {"case_id": "q1", "stratum": "ordinary_synonyms", "primary_target_id": "c1",
             "semantic_family_id": "f1", "unrelated_negative_ids": ["c2"], "hard_negative_ids": []},
            {"case_id": "q2", "stratum": "no_signal", "primary_target_id": None,
             "semantic_family_id": "", "unrelated_negative_ids": [], "hard_negative_ids": []},
        ],
        "query_embeddings": np.array([[1.0, 0.0], [0.5, 0.5]], dtype=np.float32),
        "zero_vectors": 0,
        "non_finite_vectors": 0,
        "repeat_mismatches": 0,
    }


def test_g0_excludes_targetless_no_signal_from_retrieval_denominators() -> None:
    result = evaluate_g0(**_no_signal_fixture())

    assert result["target_bearing_case_count"] == 1
    assert result["no_signal_case_count"] == 1
    assert "no_signal" not in result["per_stratum_recall_at_10"]


def test_g0_no_signal_row_is_recorded_but_unscored() -> None:
    result = evaluate_g0(**_no_signal_fixture())

    rows = {row["case_id"]: row for row in result["query_results"]}
    assert set(rows) == {"q1", "q2"}

    control = rows["q2"]
    assert control["scored"] is False
    assert control["primary_rank"] is None
    assert control["primary_target_id"] is None
    assert not control["top_10"]
    assert not control["top_32"]

    scored = rows["q1"]
    assert scored["scored"] is True
    assert scored["primary_rank"] == 1


def test_g0_family_recall_denominator_counts_target_bearing_cases_only() -> None:
    """The lone target-bearing case hits its family, so recall is 1.0, not 0.5."""
    result = evaluate_g0(**_no_signal_fixture())
    assert result["metrics"]["semantic_family_recall_at_10"] == 1.0


def test_g0_rejects_a_targetless_case_outside_the_no_signal_stratum() -> None:
    fixture = _no_signal_fixture()
    fixture["cases"][1] = dict(fixture["cases"][1], stratum="ordinary_synonyms")
    with pytest.raises(ValueError, match="q2"):
        evaluate_g0(**fixture)


def test_g0_rejects_a_no_signal_case_that_carries_a_target() -> None:
    """no_signal is targetless by definition; a target there is malformed input."""
    fixture = _no_signal_fixture()
    fixture["cases"][1] = dict(fixture["cases"][1], primary_target_id="c2")
    with pytest.raises(ValueError, match="q2"):
        evaluate_g0(**fixture)


def test_g0_still_counts_every_case_when_none_are_no_signal() -> None:
    fixture = _no_signal_fixture()
    fixture["cases"][1] = dict(
        fixture["cases"][1],
        stratum="ordinary_synonyms",
        primary_target_id="c2",
        semantic_family_id="f2",
        unrelated_negative_ids=["c1"],
    )
    result = evaluate_g0(**fixture)
    assert result["target_bearing_case_count"] == 2
    assert result["no_signal_case_count"] == 0


# ── first-stage gate at K=32 (operator ratification 2026-08-18) ────────────────
# The retina is a FIRST STAGE feeding a reranker, so it is gated on getting the
# target into the rerank window (K=32), not on winning a 10-way contest.
# hard_distinction_pairwise is still computed and reported, but it is a rerank
# obligation: measured pair cosines of 0.94-0.99 put it beyond any mean-pooled
# bi-encoder. The first stage keeps a real obligation in its place --
# displacement, the one failure a reranker cannot repair.


def _two_stage_fixture() -> dict:
    """One query, three readings of it.

    12 unrelated candidates score 0.9, the hard negative c31 scores 0.6 and the
    true target c30 scores 0.5 -- so c30 lands at rank 14: outside the top 10,
    inside the rerank window, and behind its own hard negative.
    """
    ids = [f"c{i}" for i in range(40)]
    embeddings = np.stack([_unit(i, dim=64) for i in range(40)])
    query = _unit(30, dim=64) * 0.5 + _unit(31, dim=64) * 0.6
    for i in range(12):
        query = query + _unit(i, dim=64) * 0.9
    cases = [
        {"case_id": "q_ordinary", "stratum": "ordinary_synonyms", "primary_target_id": "c30",
         "semantic_family_id": "f30", "unrelated_negative_ids": ["c35"], "hard_negative_ids": []},
        {"case_id": "q_zero", "stratum": "zero_token_overlap_paraphrase", "primary_target_id": "c30",
         "semantic_family_id": "f30", "unrelated_negative_ids": ["c36"], "hard_negative_ids": []},
        {"case_id": "q_hard", "stratum": "negation", "primary_target_id": "c30",
         "semantic_family_id": "f30", "unrelated_negative_ids": [], "hard_negative_ids": ["c31"]},
    ]
    return {
        "candidate_ids": ids,
        "candidate_embeddings": embeddings,
        "candidate_families": {f"c{i}": f"f{i}" for i in range(40)},
        "cases": cases,
        "query_embeddings": np.stack([query, query, query]),
        "zero_vectors": 0,
        "non_finite_vectors": 0,
        "repeat_mismatches": 0,
    }


def test_g0_reports_recall_at_both_k10_and_k32() -> None:
    report = evaluate_g0(**_two_stage_fixture())
    assert "per_stratum_recall_at_10" in report
    assert "per_stratum_recall_at_32" in report
    assert report["metrics"]["stratum_floor_at_10"] == 0.0
    assert report["metrics"]["stratum_floor_at_32"] == 1.0


def test_g0_gates_on_k32_not_k10() -> None:
    """A target inside the rerank window is a first-stage success."""
    report = evaluate_g0(**_two_stage_fixture())
    assert report["metrics"]["semantic_family_recall_at_32"] == 1.0
    assert report["metrics"]["semantic_family_recall_at_10"] == 0.0
    assert report["gate_pass"] is True


def test_g0_still_reports_hard_distinction_but_does_not_gate_on_it() -> None:
    report = evaluate_g0(**_two_stage_fixture())
    assert report["deferred_to_rerank"] == ["hard_distinction_pairwise"]
    # c31 outscores c30, so the pairwise check fails -- the first-stage gate must
    # survive it, because separating that pair is the reranker's job.
    assert report["metrics"]["hard_distinction_pairwise"] == 0.0
    assert report["gate_pass"] is True


def test_g0_fails_when_the_target_is_displaced_by_its_own_hard_negative() -> None:
    """Target outside top-32 while its hard negative is inside: unrepairable."""
    ids = [f"c{i}" for i in range(80)]
    embeddings = np.stack([_unit(i, dim=96) for i in range(80)])
    query = _unit(1, dim=96) * 1.0
    for i in range(2, 40):
        query = query + _unit(i, dim=96) * 0.9
    cases = [
        {"case_id": "q", "stratum": "negation", "primary_target_id": "c70",
         "semantic_family_id": "f70", "unrelated_negative_ids": [], "hard_negative_ids": ["c1"]},
    ]
    report = evaluate_g0(
        candidate_ids=ids, candidate_embeddings=embeddings,
        candidate_families={f"c{i}": f"f{i}" for i in range(80)},
        cases=cases, query_embeddings=query.reshape(1, -1),
        zero_vectors=0, non_finite_vectors=0, repeat_mismatches=0,
    )
    assert report["metrics"]["hard_negative_displacement_at_32"] == 1.0
    assert report["gate_pass"] is False

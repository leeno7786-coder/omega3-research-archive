from __future__ import annotations

import numpy as np
import pytest

from benchmarks.memory_organ.program1.hqnp_metrics import (
    gaussian_geometry_metrics,
    hamming_fraction_matrix,
    pack_codes,
    rademacher_geometry_metrics,
    top_k_by_hamming,
)


# ── packing and Hamming ────────────────────────────────────────────────────────

def test_packed_hamming_matches_direct_xor() -> None:
    q = np.array([[0, 1, 1, 0, 1, 0, 0, 1]], dtype=np.uint8)
    c = np.array([[1, 1, 0, 0, 1, 1, 0, 1]], dtype=np.uint8)
    got = hamming_fraction_matrix(q, c, bit_width=8)
    assert got[0, 0] == 3 / 8


def test_hamming_of_identical_and_complementary_codes() -> None:
    bits = np.array([[0, 1, 1, 0, 1, 0, 0, 1]], dtype=np.uint8)
    assert hamming_fraction_matrix(bits, bits, bit_width=8)[0, 0] == 0.0
    assert hamming_fraction_matrix(bits, 1 - bits, bit_width=8)[0, 0] == 1.0


def test_query_chunking_does_not_change_the_result() -> None:
    rng = np.random.default_rng(7)
    q = rng.integers(0, 2, size=(19, 64), dtype=np.uint8)
    c = rng.integers(0, 2, size=(23, 64), dtype=np.uint8)
    reference = hamming_fraction_matrix(q, c, bit_width=64, query_chunk=1)
    for chunk in (2, 5, 8, 19, 64):
        assert np.array_equal(hamming_fraction_matrix(q, c, bit_width=64, query_chunk=chunk), reference)


def test_bit_widths_not_divisible_by_eight_are_refused() -> None:
    bits = np.zeros((1, 12), dtype=np.uint8)
    with pytest.raises(ValueError):
        hamming_fraction_matrix(bits, bits, bit_width=12)


def test_already_packed_codes_are_accepted_without_repacking() -> None:
    rng = np.random.default_rng(3)
    bits = rng.integers(0, 2, size=(6, 64), dtype=np.uint8)
    packed = pack_codes(bits)
    assert packed.shape == (6, 8)
    assert np.array_equal(
        hamming_fraction_matrix(packed, packed, bit_width=64),
        hamming_fraction_matrix(bits, bits, bit_width=64),
    )


def test_popcount_never_overflows_at_the_primary_width() -> None:
    """A uint8 accumulator would wrap at 256; D=16384 needs a wide sum."""
    ones = np.ones((1, 16384), dtype=np.uint8)
    zeros = np.zeros((1, 16384), dtype=np.uint8)
    assert hamming_fraction_matrix(ones, zeros, bit_width=16384)[0, 0] == 1.0


# ── top-k ordering ─────────────────────────────────────────────────────────────

def test_top_k_takes_lowest_hamming_with_stable_id_order_on_ties() -> None:
    hamming = np.array([[0.5, 0.1, 0.5, 0.2, 0.1]])
    got = top_k_by_hamming(hamming, k=3)
    # 0.1 at index 1 and 4 -> lower index (ascending candidate id) first
    assert got.tolist() == [[1, 4, 3]]


# ── Gaussian gate metrics ──────────────────────────────────────────────────────

def test_gaussian_metrics_from_hand_built_arrays() -> None:
    source_cosine = np.array([[1.0, 0.0, -1.0]])
    # angular = [0.0, 0.5, 1.0]
    hamming = np.array([[0.02, 0.5, 0.97]])
    metrics = gaussian_geometry_metrics(source_cosine, hamming)
    assert metrics["mae"] == pytest.approx((0.02 + 0.0 + 0.03) / 3)
    assert metrics["p99_absolute_error"] == pytest.approx(np.percentile([0.02, 0.0, 0.03], 99))
    assert metrics["spearman"] == pytest.approx(1.0)
    assert metrics["pair_count"] == 3


def test_gaussian_metrics_clip_cosine_into_the_arccos_domain() -> None:
    """Float error can push a cosine just past +/-1; arccos must not emit NaN."""
    source_cosine = np.array([[1.0000000002, -1.0000000002]])
    hamming = np.array([[0.0, 1.0]])
    metrics = gaussian_geometry_metrics(source_cosine, hamming)
    assert np.isfinite(metrics["mae"])
    assert metrics["mae"] == pytest.approx(0.0)


def test_gaussian_spearman_detects_inverted_ordering() -> None:
    source_cosine = np.array([[1.0, 0.0, -1.0]])
    inverted = np.array([[1.0, 0.5, 0.0]])
    assert gaussian_geometry_metrics(source_cosine, inverted)["spearman"] == pytest.approx(-1.0)


# ── Rademacher comparison metrics ──────────────────────────────────────────────

def _code_stats(densities, ties):
    return {"densities": np.array(densities, dtype=np.float64), "tie_fractions": np.array(ties, dtype=np.float64)}


def test_rademacher_metrics_report_every_preregistered_field() -> None:
    source_cosine = np.array([[1.0, 0.0, -1.0]])
    gaussian = np.array([[0.0, 0.5, 1.0]])
    rademacher = np.array([[0.01, 0.51, 0.99]])
    metrics = rademacher_geometry_metrics(
        source_cosine=source_cosine,
        gaussian_hamming=gaussian,
        rademacher_hamming=rademacher,
        gaussian_top10=np.array([[0, 1, 2]]),
        rademacher_top10=np.array([[0, 1, 2]]),
        code_stats=_code_stats([0.50, 0.49, 0.51], [0.0, 0.01, 0.02]),
    )
    for key in (
        "spearman_source_vs_rademacher",
        "spearman_gaussian_vs_rademacher",
        "median_knn10_jaccard",
        "rademacher_minus_gaussian_mae",
        "mean_density",
        "density_in_band_fraction",
        "median_tie_fraction",
        "p95_tie_fraction",
    ):
        assert key in metrics, key
    assert metrics["median_knn10_jaccard"] == pytest.approx(1.0)
    assert metrics["mean_density"] == pytest.approx(0.50)
    assert metrics["density_in_band_fraction"] == pytest.approx(1.0)


def test_knn_jaccard_is_computed_per_query_then_medianed() -> None:
    metrics = rademacher_geometry_metrics(
        source_cosine=np.zeros((2, 4)),
        gaussian_hamming=np.zeros((2, 4)),
        rademacher_hamming=np.zeros((2, 4)),
        gaussian_top10=np.array([[0, 1], [0, 1]]),
        rademacher_top10=np.array([[0, 1], [2, 3]]),  # perfect, then disjoint
        code_stats=_code_stats([0.5], [0.0]),
    )
    assert metrics["median_knn10_jaccard"] == pytest.approx(0.5)


def test_density_band_fraction_counts_only_the_preregistered_band() -> None:
    metrics = rademacher_geometry_metrics(
        source_cosine=np.zeros((1, 2)),
        gaussian_hamming=np.zeros((1, 2)),
        rademacher_hamming=np.zeros((1, 2)),
        gaussian_top10=np.array([[0]]),
        rademacher_top10=np.array([[0]]),
        code_stats=_code_stats([0.47, 0.53, 0.46, 0.54], [0.0, 0.0, 0.0, 0.0]),
    )
    # band is inclusive [0.47, 0.53]
    assert metrics["density_in_band_fraction"] == pytest.approx(0.5)


# ── A12: G3 is referenced to the SOURCE, not to Gaussian ──────────────────────

def test_topk_overlap_is_evenly_spaced_unlike_jaccard() -> None:
    """Jaccard on two K-sets takes values k/(2K-k) and cannot express 0.85 at K=10.

    Overlap fraction k/K is evenly spaced, so a threshold near 0.85 is reachable.
    """
    from benchmarks.memory_organ.program1.hqnp_metrics import topk_overlap

    ref = np.array([[0, 1, 2, 3, 4, 5, 6, 7, 8, 9]])
    nine = np.array([[0, 1, 2, 3, 4, 5, 6, 7, 8, 99]])
    assert topk_overlap(ref, nine) == pytest.approx(0.9)
    assert topk_overlap(ref, ref) == pytest.approx(1.0)


def test_rademacher_metrics_report_source_referenced_overlap() -> None:
    from benchmarks.memory_organ.program1.hqnp_metrics import rademacher_geometry_metrics

    source = np.array([[1.0, 0.0, -1.0]])
    metrics = rademacher_geometry_metrics(
        source_cosine=source,
        gaussian_hamming=np.array([[0.0, 0.5, 1.0]]),
        rademacher_hamming=np.array([[0.01, 0.51, 0.99]]),
        gaussian_top10=np.array([[0, 1, 2]]),
        rademacher_top10=np.array([[0, 1, 2]]),
        source_top10=np.array([[0, 1, 2]]),
        code_stats={"densities": np.array([0.5]), "tie_fractions": np.array([0.0])},
    )
    assert metrics["rademacher_topk_overlap_vs_source"] == pytest.approx(1.0)
    assert metrics["gaussian_topk_overlap_vs_source"] == pytest.approx(1.0)
    # Kept for continuity, no longer the gating criterion.
    assert "median_knn10_jaccard" in metrics

from __future__ import annotations

import numpy as np
import pytest

from benchmarks.memory_organ.program1.hqnp_metrics import hamming_fraction_matrix, pack_codes
from benchmarks.memory_organ.program1.hqnp_pipeline import (
    bits_to_pm1,
    window_composition,
)


def test_pm1_cosine_ordering_is_identical_to_hamming_ordering() -> None:
    """The whole regression rests on this.

    For sign codes mapped to {-1,+1}, cosine = 1 - 2*hamming_fraction, a strictly
    decreasing linear map. Ranking by cosine over pm1 vectors is therefore exactly
    ranking by Hamming distance, which lets the existing FP32 G0 evaluator score
    binary retrieval unchanged.
    """
    rng = np.random.default_rng(4)
    q = rng.integers(0, 2, size=(9, 64), dtype=np.uint8)
    c = rng.integers(0, 2, size=(25, 64), dtype=np.uint8)

    hamming = hamming_fraction_matrix(pack_codes(q), pack_codes(c), bit_width=64)
    cosine = bits_to_pm1(q) @ bits_to_pm1(c).T / 64.0

    assert np.allclose(cosine, 1.0 - 2.0 * hamming, atol=1e-6)
    for row in range(q.shape[0]):
        by_hamming = np.argsort(hamming[row], kind="stable")
        by_cosine = np.argsort(-cosine[row], kind="stable")
        assert np.array_equal(by_hamming, by_cosine)


def test_bits_to_pm1_maps_zero_and_one_exactly() -> None:
    bits = np.array([[0, 1, 1, 0]], dtype=np.uint8)
    assert bits_to_pm1(bits).tolist() == [[-1.0, 1.0, 1.0, -1.0]]


def test_window_composition_measures_overlap_against_the_fp32_window() -> None:
    fp32 = np.array([[0, 1, 2, 3], [0, 1, 2, 3]])
    hqnp = np.array([[0, 1, 2, 3], [4, 5, 6, 7]])
    stats = window_composition(fp32, hqnp)
    assert stats["mean_overlap"] == pytest.approx(0.5)
    assert stats["identical_window_fraction"] == pytest.approx(0.5)
    assert stats["median_overlap_count"] == pytest.approx(2.0)

from __future__ import annotations

import numpy as np
import pytest

from benchmarks.memory_organ.program1.int8_latent import (
    INT8_MAX,
    INT8_SCHEMES,
    Int8Latent,
    collapsed_rows,
    dequantize,
    quantize_latent,
)


def _unit(m: np.ndarray) -> np.ndarray:
    return m / np.linalg.norm(m, axis=1, keepdims=True)


# ── domain ─────────────────────────────────────────────────────────────────────

def test_schemes_are_exactly_per_tensor_and_per_row() -> None:
    assert set(INT8_SCHEMES) == {"per_tensor", "per_row"}


def test_unknown_scheme_is_refused() -> None:
    with pytest.raises(ValueError, match="scheme"):
        quantize_latent(np.ones((2, 4), dtype=np.float32), scheme="asymmetric")


def test_codes_are_int8_and_never_reach_negative_128() -> None:
    """Symmetric quantization has no -128 counterpart; allowing it skews the sign."""
    rng = np.random.default_rng(0)
    z = rng.standard_normal((64, 256)).astype(np.float32)
    for scheme in INT8_SCHEMES:
        latent = quantize_latent(z, scheme=scheme)
        assert latent.codes.dtype == np.int8
        assert latent.codes.min() >= -INT8_MAX
        assert latent.codes.max() <= INT8_MAX
        assert INT8_MAX == 127


def test_extreme_values_map_to_the_rail() -> None:
    z = np.array([[-5.0, 0.0, 5.0]], dtype=np.float32)
    latent = quantize_latent(z, scheme="per_row")
    assert latent.codes.tolist() == [[-127, 0, 127]]


# ── scale ──────────────────────────────────────────────────────────────────────

def test_scale_is_strictly_positive_for_any_non_zero_latent() -> None:
    rng = np.random.default_rng(1)
    z = rng.standard_normal((8, 32)).astype(np.float32)
    for scheme in INT8_SCHEMES:
        assert np.all(quantize_latent(z, scheme=scheme).scale > 0.0)


def test_per_row_scale_has_one_entry_per_row() -> None:
    z = np.ones((5, 16), dtype=np.float32)
    assert quantize_latent(z, scheme="per_row").scale.shape == (5, 1)
    assert quantize_latent(z, scheme="per_tensor").scale.shape == (1, 1)


# ── round trip ─────────────────────────────────────────────────────────────────

def test_round_trip_is_close_and_direction_preserving() -> None:
    rng = np.random.default_rng(2)
    z = rng.standard_normal((128, 256)).astype(np.float32)
    for scheme in INT8_SCHEMES:
        back = dequantize(quantize_latent(z, scheme=scheme))
        cos = np.sum(_unit(z.astype(np.float64)) * _unit(back), axis=1)
        assert cos.min() > 0.999, f"{scheme} lost direction: {cos.min()}"


def test_per_row_preserves_direction_at_least_as_well_as_per_tensor() -> None:
    """Per-row gives every row the full int8 range, so its relative rounding
    error is uniform; per-tensor starves rows whose magnitude is below the
    global maximum."""
    rng = np.random.default_rng(3)
    z = rng.standard_normal((256, 256)).astype(np.float32)
    z[:64] *= 0.01  # a cohort far below the global max

    def worst_cos(scheme: str) -> float:
        back = dequantize(quantize_latent(z, scheme=scheme))
        return float(np.min(np.sum(_unit(z.astype(np.float64)) * _unit(back), axis=1)))

    assert worst_cos("per_row") >= worst_cos("per_tensor")


# ── determinism ────────────────────────────────────────────────────────────────

def test_quantization_is_deterministic() -> None:
    rng = np.random.default_rng(4)
    z = rng.standard_normal((32, 64)).astype(np.float32)
    for scheme in INT8_SCHEMES:
        a = quantize_latent(z, scheme=scheme)
        b = quantize_latent(z, scheme=scheme)
        assert np.array_equal(a.codes, b.codes)
        assert np.array_equal(a.scale, b.scale)


def test_quantizing_a_dequantized_latent_is_idempotent() -> None:
    rng = np.random.default_rng(5)
    z = rng.standard_normal((16, 64)).astype(np.float32)
    for scheme in INT8_SCHEMES:
        once = quantize_latent(z, scheme=scheme)
        twice = quantize_latent(dequantize(once), scheme=scheme)
        assert np.array_equal(once.codes, twice.codes)


def test_rounding_is_half_to_even_not_half_away_from_zero() -> None:
    """Platform-stable rounding. 0.5 -> 0 and 1.5 -> 2 under banker's rounding."""
    z = np.array([[0.5, 1.5, 2.5, 127.0]], dtype=np.float64) * (1.0 / 127.0) * 127.0
    latent = quantize_latent(np.array([[0.5, 1.5, 2.5, 127.0]], dtype=np.float64), scheme="per_row")
    # scale = 127/127 = 1.0, so codes are the rounded values themselves
    assert latent.codes.tolist() == [[0, 2, 2, 127]]


# ── no-signal versus collapse ──────────────────────────────────────────────────

def test_zero_latent_quantizes_to_zero_codes_and_is_not_searchable() -> None:
    z = np.zeros((3, 32), dtype=np.float32)
    for scheme in INT8_SCHEMES:
        latent = quantize_latent(z, scheme=scheme)
        assert not latent.codes.any()
        assert np.all(latent.scale > 0.0), "scale must stay positive so dequantize is safe"
        assert collapsed_rows(latent, z).tolist() == [False, False, False], (
            "an all-zero input is declared no-signal, not a quantization collapse"
        )


def test_a_non_zero_latent_that_quantizes_to_zero_is_a_collapse() -> None:
    """Preregistration 7.2: valid non-empty inputs yielding a zero INT8 latent = 0.

    Per-tensor scaling can starve a row far below the global maximum. This must
    be reported as a collapse, distinguishable from a declared no-signal control.
    """
    z = np.zeros((2, 8), dtype=np.float64)
    z[0] = 1000.0
    z[1] = 1e-9  # non-zero, but ~1e-12 of the global max
    latent = quantize_latent(z, scheme="per_tensor")
    assert not latent.codes[1].any()
    assert collapsed_rows(latent, z).tolist() == [False, True]


def test_per_row_scaling_structurally_cannot_collapse_a_non_zero_row() -> None:
    """Each row's own maximum maps to the rail, so no non-zero row can vanish."""
    rng = np.random.default_rng(6)
    z = rng.standard_normal((200, 256)).astype(np.float64)
    z[:50] *= 1e-9
    latent = quantize_latent(z, scheme="per_row")
    assert not collapsed_rows(latent, z).any()
    assert np.all(np.abs(latent.codes).max(axis=1) == 127)


def test_single_vector_input_is_accepted() -> None:
    z = np.array([1.0, -2.0, 4.0], dtype=np.float32)
    latent = quantize_latent(z, scheme="per_row")
    assert latent.codes.shape == (1, 3)


def test_non_finite_latent_is_refused() -> None:
    for bad in (np.array([[np.nan, 1.0]]), np.array([[np.inf, 1.0]])):
        with pytest.raises(ValueError):
            quantize_latent(bad, scheme="per_row")


def test_latent_record_is_frozen() -> None:
    latent = quantize_latent(np.ones((1, 4), dtype=np.float32), scheme="per_row")
    assert isinstance(latent, Int8Latent)
    with pytest.raises(Exception):
        latent.scheme = "tampered"

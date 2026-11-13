from __future__ import annotations

import hashlib

import numpy as np
import pytest

from benchmarks.memory_organ.program1.hqnp import (
    HQNP_DIMENSIONS,
    HQNP_SEED_INDICES,
    bank_sha256,
    gaussian_bank,
    projection_seed,
    rademacher_bank,
)

# Program I pins 256-dimensional Ettin latents.
LATENT_DIM = 256


# ── seed derivation ────────────────────────────────────────────────────────────

def test_projection_seed_matches_preregistered_sha_rule() -> None:
    expected = int.from_bytes(
        hashlib.sha256(b"omega-hqnp-program1-v1:0").digest()[:8], "big"
    )
    assert projection_seed(0) == expected


def test_only_seed_indices_zero_through_four_are_lawful() -> None:
    with pytest.raises(ValueError):
        projection_seed(5)
    with pytest.raises(ValueError):
        projection_seed(-1)


def test_all_five_seeds_are_distinct_and_declared() -> None:
    assert tuple(HQNP_SEED_INDICES) == (0, 1, 2, 3, 4)
    seeds = [projection_seed(index) for index in HQNP_SEED_INDICES]
    assert len(set(seeds)) == 5


# ── bank shape and domain ──────────────────────────────────────────────────────

def test_lawful_dimensions_are_exactly_4096_and_16384() -> None:
    assert tuple(HQNP_DIMENSIONS) == (4096, 16384)


@pytest.mark.parametrize("dimension", [1024, 8192, 32768, 0, -4096])
def test_banks_refuse_unlawful_dimensions(dimension: int) -> None:
    with pytest.raises(ValueError):
        gaussian_bank(seed_index=0, dimension=dimension)
    with pytest.raises(ValueError):
        rademacher_bank(seed_index=0, dimension=dimension)


def test_gaussian_bank_shape_and_dtype() -> None:
    bank = gaussian_bank(seed_index=0, dimension=4096)
    assert bank.shape == (4096, LATENT_DIM)
    assert bank.dtype == np.float64


def test_rademacher_bank_is_exactly_plus_or_minus_one() -> None:
    bank = rademacher_bank(seed_index=0, dimension=4096)
    assert bank.shape == (4096, LATENT_DIM)
    assert set(np.unique(bank).tolist()) == {-1, 1}


def test_gaussian_rows_are_not_normalised() -> None:
    """Sign hyperplanes are invariant to positive row scale; do not normalise."""
    bank = gaussian_bank(seed_index=0, dimension=4096)
    norms = np.linalg.norm(bank, axis=1)
    assert not np.allclose(norms, 1.0)


# ── determinism and separation ─────────────────────────────────────────────────

def test_same_seed_and_dimension_regenerate_byte_identically() -> None:
    for builder in (gaussian_bank, rademacher_bank):
        first = builder(seed_index=2, dimension=4096)
        second = builder(seed_index=2, dimension=4096)
        assert bank_sha256(first) == bank_sha256(second)
        assert np.array_equal(first, second)


def test_different_seed_changes_the_bank_hash() -> None:
    for builder in (gaussian_bank, rademacher_bank):
        hashes = {bank_sha256(builder(seed_index=i, dimension=4096)) for i in range(5)}
        assert len(hashes) == 5


def test_gaussian_and_rademacher_banks_are_domain_separated() -> None:
    """The two families must not share a seed stream."""
    left = gaussian_bank(seed_index=0, dimension=4096)
    right = rademacher_bank(seed_index=0, dimension=4096)
    assert bank_sha256(left) != bank_sha256(right)
    assert not np.array_equal(np.sign(left), right.astype(np.float64))


def test_bank_hash_binds_shape_and_dtype_not_only_raw_bytes() -> None:
    """A reshape must not collide with the original identity."""
    bank = rademacher_bank(seed_index=0, dimension=4096)
    reshaped = bank.reshape(LATENT_DIM, 4096)
    assert bank.tobytes(order="C") == reshaped.tobytes(order="C")
    assert bank_sha256(bank) != bank_sha256(reshaped)


def test_dimensions_are_nested_draws_from_one_seed_stream() -> None:
    """D=4096 is the first 4096 rows of D=16384, and that is intentional.

    The preregistered construction is PCG64(projection_seed(i)) with no
    dimension separation, so standard_normal((D,256)) draws row-major and the
    smaller bank is a prefix of the larger. The two dimensions are therefore a
    NESTED comparison: D is varied while the projection family and seed stream
    are held fixed, which isolates the effect of width rather than confounding
    it with a different random draw.
    """
    small = gaussian_bank(seed_index=0, dimension=4096)
    large = gaussian_bank(seed_index=0, dimension=16384)
    assert large.shape == (16384, LATENT_DIM)
    assert np.array_equal(large[:4096], small)

    small_r = rademacher_bank(seed_index=0, dimension=4096)
    large_r = rademacher_bank(seed_index=0, dimension=16384)
    assert np.array_equal(large_r[:4096], small_r)


def test_bank_hashes_still_differ_across_dimensions() -> None:
    """Nesting must not make the two widths share an identity."""
    assert bank_sha256(gaussian_bank(seed_index=0, dimension=4096)) != bank_sha256(
        gaussian_bank(seed_index=0, dimension=16384)
    )


def test_balanced_tie_bits_are_deterministic_and_roughly_balanced() -> None:
    from benchmarks.memory_organ.program1.hqnp import balanced_tie_bits

    first = balanced_tie_bits(0, 16384)
    assert np.array_equal(first, balanced_tie_bits(0, 16384))
    assert set(np.unique(first).tolist()) <= {0, 1}
    # A tie rule that always resolved to 1 would bias code density.
    assert 0.45 < first.mean() < 0.55
    assert not np.array_equal(first, balanced_tie_bits(1, 16384))


# ── sign projection and tie semantics ──────────────────────────────────────────

from benchmarks.memory_organ.program1.hqnp import (  # noqa: E402
    NO_SIGNAL,
    OK,
    HQNPCode,
    project_gaussian,
    project_rademacher_float,
)

# Hand-built 2-D banks: the bit mapping is proven exactly, not sampled.
TOY_BANK = np.array([[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0], [1.0, 1.0]], dtype=np.float64)
TOY_LATENT = np.array([2.0, -3.0], dtype=np.float32)


def test_positive_scores_are_one_and_negative_scores_are_zero() -> None:
    code = project_gaussian(TOY_LATENT, TOY_BANK, seed_index=0)
    # scores = [+2, -3, -2, -1]
    assert code.status == OK
    assert code.bits.tolist() == [1, 0, 0, 0]


def test_same_vector_and_bank_give_an_identical_code() -> None:
    first = project_gaussian(TOY_LATENT, TOY_BANK, seed_index=0)
    second = project_gaussian(TOY_LATENT, TOY_BANK, seed_index=0)
    assert np.array_equal(first.bits, second.bits)
    assert first.density == second.density


def test_negated_latent_gives_exact_bit_complement_when_no_ties() -> None:
    positive = project_gaussian(TOY_LATENT, TOY_BANK, seed_index=0)
    negated = project_gaussian(-TOY_LATENT, TOY_BANK, seed_index=0)
    assert positive.tie_fraction == 0.0
    assert np.array_equal(negated.bits, 1 - positive.bits)


def test_zero_latent_is_no_signal_and_carries_no_searchable_code() -> None:
    """Preregistration 7.5: a zero latent must never become a searchable code."""
    for project in (
        lambda z: project_gaussian(z, TOY_BANK, seed_index=0),
        lambda z: project_rademacher_float(z, TOY_BANK.astype(np.int8), seed_index=0),
    ):
        code = project(np.zeros(2, dtype=np.float32))
        assert code.status == NO_SIGNAL
        assert code.bits is None
        assert code.density is None


def test_exact_tie_uses_the_balanced_tie_bit_not_always_one() -> None:
    """An all-ones tie rule would bias code density."""
    from benchmarks.memory_organ.program1.hqnp import balanced_tie_bits

    # Every row is orthogonal to the latent, so every score is an exact zero.
    bank = np.array([[0.0, 1.0]] * 4096, dtype=np.float64)
    latent = np.array([1.0, 0.0], dtype=np.float32)
    code = project_gaussian(latent, bank, seed_index=0)
    assert code.status == OK
    assert code.tie_fraction == 1.0
    assert np.array_equal(code.bits, balanced_tie_bits(0, 4096))
    assert code.bits.mean() != 1.0


def test_tie_fraction_and_density_are_reported() -> None:
    bank = np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 0.0], [0.0, 1.0]], dtype=np.float64)
    latent = np.array([1.0, 0.0], dtype=np.float32)  # scores [+1, 0, +1, 0]
    code = project_gaussian(latent, bank, seed_index=0)
    assert code.tie_fraction == 0.5
    assert code.density == code.bits.mean()


def test_rademacher_accepts_an_int8_bank_and_matches_the_sign_rule() -> None:
    bank = np.array([[1, -1], [-1, 1], [1, 1], [-1, -1]], dtype=np.int8)
    latent = np.array([3.0, 1.0], dtype=np.float32)  # scores [+2, -2, +4, -4]
    code = project_rademacher_float(latent, bank, seed_index=0)
    assert code.status == OK
    assert code.bits.tolist() == [1, 0, 1, 0]


def test_projection_refuses_a_latent_that_does_not_match_the_bank() -> None:
    with pytest.raises(ValueError):
        project_gaussian(np.ones(3, dtype=np.float32), TOY_BANK, seed_index=0)


def test_projection_refuses_a_non_finite_latent() -> None:
    for bad in (np.array([np.nan, 1.0]), np.array([np.inf, 1.0])):
        with pytest.raises(ValueError):
            project_gaussian(bad.astype(np.float32), TOY_BANK, seed_index=0)


def test_code_is_a_frozen_record() -> None:
    code = project_gaussian(TOY_LATENT, TOY_BANK, seed_index=0)
    assert isinstance(code, HQNPCode)
    with pytest.raises(Exception):
        code.status = "tampered"


# ── batched projection ─────────────────────────────────────────────────────────

from benchmarks.memory_organ.program1.hqnp import project_matrix  # noqa: E402


def test_batched_projection_is_bit_identical_to_the_per_latent_reference() -> None:
    """The batch path is an optimisation; it must not become a second algorithm."""
    rng = np.random.default_rng(11)
    latents = rng.standard_normal((37, 2)).astype(np.float32)
    result = project_matrix(latents, TOY_BANK, seed_index=0)
    assert result.bits.shape == (37, TOY_BANK.shape[0])
    for row, latent in enumerate(latents):
        reference = project_gaussian(latent, TOY_BANK, seed_index=0)
        assert np.array_equal(result.bits[row], reference.bits)
        assert result.densities[row] == reference.density
        assert result.tie_fractions[row] == reference.tie_fraction


def test_batched_projection_flags_no_signal_rows_without_emitting_bits() -> None:
    latents = np.array([[1.0, 2.0], [0.0, 0.0], [-3.0, 1.0]], dtype=np.float32)
    result = project_matrix(latents, TOY_BANK, seed_index=0)
    assert result.no_signal.tolist() == [False, True, False]
    # A no-signal row carries no searchable code and no synthetic density.
    assert np.isnan(result.densities[1])
    assert not result.bits[1].any()
    assert result.valid_indices.tolist() == [0, 2]


def test_batched_projection_respects_chunking() -> None:
    rng = np.random.default_rng(5)
    latents = rng.standard_normal((20, 2)).astype(np.float32)
    reference = project_matrix(latents, TOY_BANK, seed_index=0, chunk=64)
    for chunk in (1, 3, 7, 20):
        got = project_matrix(latents, TOY_BANK, seed_index=0, chunk=chunk)
        assert np.array_equal(got.bits, reference.bits)


# ── exact INT32 Rademacher projection (P1-D Task 2) ───────────────────────────

from benchmarks.memory_organ.program1.hqnp import project_rademacher_int8  # noqa: E402
from benchmarks.memory_organ.program1.int8_latent import (  # noqa: E402
    Int8Latent,
    quantize_latent,
)

TOY_INT8_BANK = np.array([[1, -1], [-1, 1], [1, 1], [-1, -1]], dtype=np.int8)


def test_int8_projection_matches_the_sign_rule_exactly() -> None:
    latent = Int8Latent(
        codes=np.array([[3, 1]], dtype=np.int8),
        scale=np.array([[1.0]]),
        scheme="per_row",
    )
    # scores = [3-1, -3+1, 3+1, -3-1] = [+2, -2, +4, -4]
    code = project_rademacher_int8(latent, TOY_INT8_BANK, seed_index=0)
    assert code.bits.tolist() == [1, 0, 1, 0]


def test_int8_projection_ignores_the_scale() -> None:
    """Sign hyperplanes are invariant to positive scaling, so the code depends
    only on the integer codes. A quantizer that changed bits when the scale
    changed would be mixing magnitude into a sign decision."""
    codes = np.array([[3, 1]], dtype=np.int8)
    small = Int8Latent(codes=codes, scale=np.array([[1e-9]]), scheme="per_row")
    large = Int8Latent(codes=codes, scale=np.array([[1e9]]), scheme="per_row")
    assert np.array_equal(
        project_rademacher_int8(small, TOY_INT8_BANK, seed_index=0).bits,
        project_rademacher_int8(large, TOY_INT8_BANK, seed_index=0).bits,
    )


def test_int8_accumulator_does_not_wrap_at_full_rail() -> None:
    """NumPy's int8 @ int8 returns int8 and wraps silently.

    With every code at the +127 rail and a bank row of all +1, the true sum is
    256*127 = 32512. An int8 accumulator would wrap to a small or negative value
    and flip the bit.
    """
    width = 256
    codes = np.full((1, width), 127, dtype=np.int8)
    bank = np.ones((8, width), dtype=np.int8)
    latent = Int8Latent(codes=codes, scale=np.array([[1.0]]), scheme="per_row")
    code = project_rademacher_int8(latent, bank, seed_index=0)
    assert code.bits.tolist() == [1] * 8
    assert code.density == 1.0


def test_int8_projection_is_bit_identical_to_the_float_path() -> None:
    """The INT8 path is an exact reimplementation, not an approximation.

    For a latent that is exactly representable in INT8, dequantizing and running
    the float projector must produce the same bits, because positive scaling
    cannot move a sign.
    """
    rng = np.random.default_rng(21)
    z = rng.standard_normal((40, 256)).astype(np.float32)
    latent = quantize_latent(z, scheme="per_row")
    bank = rademacher_bank(seed_index=0, dimension=4096)
    for row in range(z.shape[0]):
        single = Int8Latent(
            codes=latent.codes[row : row + 1],
            scale=latent.scale[row : row + 1],
            scheme=latent.scheme,
        )
        int8_code = project_rademacher_int8(single, bank, seed_index=0)
        float_code = project_rademacher_float(
            single.codes[0].astype(np.float64), bank, seed_index=0
        )
        assert np.array_equal(int8_code.bits, float_code.bits), row


def test_int8_projection_reports_no_signal_for_all_zero_codes() -> None:
    latent = Int8Latent(
        codes=np.zeros((1, 2), dtype=np.int8), scale=np.array([[1.0]]), scheme="per_row"
    )
    code = project_rademacher_int8(latent, TOY_INT8_BANK, seed_index=0)
    assert code.status == NO_SIGNAL
    assert code.bits is None


def test_int8_exact_ties_use_the_balanced_tie_bit() -> None:
    """Integer accumulators hit exact zero far more often than float ones do."""
    from benchmarks.memory_organ.program1.hqnp import balanced_tie_bits

    # codes [1, 1] against bank rows [1, -1] -> score 0 for every row
    latent = Int8Latent(
        codes=np.array([[1, 1]], dtype=np.int8), scale=np.array([[1.0]]), scheme="per_row"
    )
    bank = np.array([[1, -1]] * 4096, dtype=np.int8)
    code = project_rademacher_int8(latent, bank, seed_index=0)
    assert code.tie_fraction == 1.0
    assert np.array_equal(code.bits, balanced_tie_bits(0, 4096))


def test_batched_int8_projection_is_bit_identical_to_the_single_reference() -> None:
    from benchmarks.memory_organ.program1.hqnp import project_matrix_int8

    rng = np.random.default_rng(31)
    z = rng.standard_normal((25, 256)).astype(np.float32)
    latent = quantize_latent(z, scheme="per_row")
    bank = rademacher_bank(seed_index=1, dimension=4096)
    batched = project_matrix_int8(latent, bank, seed_index=1)
    for row in range(z.shape[0]):
        single = Int8Latent(
            codes=latent.codes[row : row + 1],
            scale=latent.scale[row : row + 1],
            scheme=latent.scheme,
        )
        reference = project_rademacher_int8(single, bank, seed_index=1)
        assert np.array_equal(batched.bits[row], reference.bits), row
        assert batched.tie_fractions[row] == reference.tie_fraction

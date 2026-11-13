"""Deterministic semantic plane — HQ-NP binary codes over an injected encoder.

This is the retrieval surface Program I validated, reduced to the path that
actually deploys:

    FP32 latent -> per-row INT8 -> Rademacher {-1,+1} sign projection
                -> packed D-bit code -> popcount scan -> top-K window

Everything here is a *fixed function of the record's own text*. There is no
training, no clustering, no rebalancing and no corpus-dependent state, so a
record's code cannot be perturbed by its neighbours. That is what makes the
plane replayable: the same records and the same query produce bit-identical
results on any machine, in any insertion order, forever.

Scope is deliberate. The Gaussian theorem-reference family, the geometry metric
suite and the seed/dimension sweeps are experiment surfaces and stay in the
frozen Program I tree; production carries only what it runs. The bank derivation
below is byte-for-byte the frozen one, and a drift test pins the two together.

The encoder is injected rather than constructed. The plane is pure geometry and
has no opinion about which model produces the latent, which keeps this module
testable without loading weights.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

import numpy as np

# Bank identity is part of the frozen Program I artifact. Changing this string
# changes every code ever produced, so it is pinned, not configured.
PROJECTION_NAMESPACE = "omega-hqnp-program1-v1"
ALGORITHM = "rademacher_int8_sign_v1"

# Postures, not scores. A no-signal input must never acquire a searchable code:
# inventing one would make an empty record look like a point in the space.
STORED = "stored"
NO_SIGNAL = "semantic_retina_no_signal"
OK = "ok"

LATENT_DIM = 256
LAWFUL_DIMENSIONS = (4096, 16384)
LAWFUL_SEED_INDICES = (0, 1, 2, 3, 4)
INT8_MAX = 127

# A11 (operator ratification, 2026-08-18): the window is SIZED TO THE POOL.
# K=32 was calibrated against 1,500 candidates; a frozen K decays as the pool
# grows. Measured to 6,000 candidates only — beyond that this extrapolates.
K_PER_1000 = {0.80: 7.7, 0.90: 12.0, 0.95: 16.7}
MIN_RETRIEVAL_K = 32

# One 256-entry table, built once: popcount of every possible byte.
_POPCOUNT = np.array([bin(value).count("1") for value in range(256)], dtype=np.uint8)


def retrieval_k_for_pool(n_candidates: int, *, floor_target: float = 0.90) -> int:
    """Window width for a candidate pool of ``n_candidates`` (A11)."""
    count = int(n_candidates)
    if count < 0:
        raise ValueError(f"pool size must be >= 0, got {n_candidates}")
    if floor_target not in K_PER_1000:
        raise ValueError(
            f"floor_target must be one of {sorted(K_PER_1000)}, got {floor_target}"
        )
    scaled = math.ceil(K_PER_1000[floor_target] * count / 1000.0)
    return max(MIN_RETRIEVAL_K, scaled)


def _derived_seed(label: str, index: int) -> int:
    """The frozen seed derivation: first 64 bits of a namespaced SHA-256."""
    digest = hashlib.sha256(f"{PROJECTION_NAMESPACE}:{label}:{index}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def rademacher_bank(*, seed_index: int, dimension: int) -> np.ndarray:
    """Bank of exactly {-1,+1}, shape (D, 256), int8.

    Regenerated from the seed rather than stored — a 33 MB file that must stay in
    sync with the code is a liability, and the derivation is cheap.
    """
    rng = np.random.Generator(np.random.PCG64(_derived_seed("rademacher", seed_index)))
    draws = rng.integers(0, 2, size=(int(dimension), LATENT_DIM), dtype=np.int8)
    return (draws * 2 - 1).astype(np.int8)


def balanced_tie_bits(*, seed_index: int, dimension: int) -> np.ndarray:
    """Deterministic tie bits, derived only from seed and row index.

    An exact-zero accumulator must not always resolve to 1: that biases code
    density away from the 0.5 the theorem assumes. The bit depends on the seed
    and the row, never on model output, so it carries no data signal.
    """
    rng = np.random.Generator(np.random.PCG64(_derived_seed("ties", seed_index)))
    return rng.integers(0, 2, size=int(dimension), dtype=np.uint8)


_BANK_CACHE: dict[tuple[int, int], tuple[np.ndarray, np.ndarray]] = {}


def _bank_for(seed_index: int, dimension: int) -> tuple[np.ndarray, np.ndarray]:
    """Cached (float64 bank, tie bits). Arrays are shared, so they are read-only.

    The bank is held as float64 rather than int8 because that is what the
    projection multiplies by — see :func:`_project` for why float64 is exact
    here, not an approximation.
    """
    key = (int(seed_index), int(dimension))
    cached = _BANK_CACHE.get(key)
    if cached is None:
        bank = rademacher_bank(seed_index=key[0], dimension=key[1]).astype(np.float64)
        ties = balanced_tie_bits(seed_index=key[0], dimension=key[1])
        bank.flags.writeable = False
        ties.flags.writeable = False
        cached = (bank, ties)
        _BANK_CACHE[key] = cached
    return cached


@dataclass(frozen=True)
class PlaneConfig:
    """Frozen because the bank identity is part of every code already stored.

    Mutating the dimension or seed of a live plane would silently invalidate
    every record in it, so the type refuses rather than trusting the caller.
    """

    dimension: int = 16384
    seed_index: int = 0
    floor_target: float = 0.90

    def __post_init__(self) -> None:
        if int(self.dimension) not in LAWFUL_DIMENSIONS:
            raise ValueError(
                f"dimension must be one of {list(LAWFUL_DIMENSIONS)}, got {self.dimension}"
            )
        if int(self.seed_index) not in LAWFUL_SEED_INDICES:
            raise ValueError(
                f"seed_index must be 0..4, got {self.seed_index}"
            )
        if self.floor_target not in K_PER_1000:
            raise ValueError(
                f"floor_target must be one of {sorted(K_PER_1000)}, got {self.floor_target}"
            )


def quantize_row(latent: np.ndarray) -> np.ndarray:
    """Symmetric per-row INT8 quantization; the only lossy step in the path.

    Codes are clipped to [-127, +127] rather than the full int8 range: -128 has
    no positive counterpart under a symmetric scale, and admitting it biases the
    sign. The scale itself is discarded by the caller — a sign hyperplane is
    invariant to positive scaling, so consulting magnitude would only mix it into
    a decision that is about direction.
    """
    vector = np.asarray(latent, dtype=np.float64).reshape(-1)
    if vector.shape[0] != LATENT_DIM:
        raise ValueError(f"latent width must be {LATENT_DIM}, got {vector.shape[0]}")
    if not np.all(np.isfinite(vector)):
        raise ValueError("latent contains non-finite values")
    magnitude = np.abs(vector).max()
    scale = magnitude / INT8_MAX if magnitude > 0.0 else 1.0
    # np.round is half-to-even, which is stable across platforms; half-away-from-
    # zero would make the codes depend on the C library.
    codes = np.round(vector / scale)
    return np.clip(codes, -INT8_MAX, INT8_MAX).astype(np.int8)


def _project(codes: np.ndarray, bank: np.ndarray, tie_bits: np.ndarray) -> np.ndarray:
    """Sign-project INT8 codes against a {-1,+1} bank into packed bits.

    The accumulation is EXACT. Every term is a product of a {-1,+1} bank entry
    and an INT8 code, so every partial sum is an integer bounded by
    256 * 127 = 32,512 — far inside the 2**53 range where float64 represents
    every integer exactly. Exact integer addition is associative, so BLAS is free
    to reorder or fuse the reduction and still return bit-identical results on
    any machine. That is why this uses float64 (and therefore fast BLAS) without
    giving up the determinism that is the whole point of the plane; a test pins
    it equal to an explicit INT32 reduction.
    """
    scores = bank @ codes.astype(np.float64)
    bits = (scores > 0.0).astype(np.uint8)
    ties = scores == 0.0
    if ties.any():
        bits[ties] = tie_bits[ties]
    return np.packbits(bits, bitorder="little")


class SemanticPlane:
    """An insertion-order-independent binary retrieval plane.

    Records are addressed by caller-supplied id. Writes are idempotent, reads are
    total-ordered, and nothing about one record depends on any other.
    """

    def __init__(self, encoder, config: PlaneConfig | None = None) -> None:
        self._encoder = encoder
        self._config = config if config is not None else PlaneConfig()
        self._bank, self._tie_bits = _bank_for(
            self._config.seed_index, self._config.dimension
        )
        self._codes: dict[str, np.ndarray] = {}
        self._texts: dict[str, str] = {}
        # Query-time scan matrix, rebuilt lazily so a burst of writes costs one
        # rebuild rather than one per write.
        self._matrix: np.ndarray | None = None
        self._order: list[str] = []

    # ── identity ──────────────────────────────────────────────────────────────

    @property
    def config(self) -> PlaneConfig:
        return self._config

    def __len__(self) -> int:
        return len(self._codes)

    def __contains__(self, record_id: str) -> bool:
        return record_id in self._codes

    # ── write path ────────────────────────────────────────────────────────────

    def put(self, record_id: str, text: str) -> str:
        """Store ``text`` under ``record_id``. Returns STORED or NO_SIGNAL.

        No-signal is decided from the TEXT, never from the latent. A real encoder
        maps whitespace to a perfectly ordinary non-zero vector, so testing the
        latent for emptiness would admit blank records into the searchable set
        with a code that means nothing. An existing record is left untouched by a
        no-signal write rather than being silently deleted.
        """
        if not str(text).strip():
            return NO_SIGNAL

        if self._texts.get(record_id) == text:
            # Idempotent: the code is a pure function of the text, so re-encoding
            # would burn a forward pass to arrive at the same bits.
            return STORED

        latent = self._encode_one(text)
        codes = quantize_row(latent)
        if not np.any(codes):
            # Defensive: a latent that quantizes to all zeros has no direction to
            # project. Declared rather than stored as an arbitrary code.
            return NO_SIGNAL

        self._codes[record_id] = _project(codes, self._bank, self._tie_bits)
        self._texts[record_id] = text
        self._invalidate()
        return STORED

    def put_code(self, record_id: str, text: str, code: np.ndarray) -> str:
        """Seat a code that was ALREADY computed for ``text`` — a persisted
        generation's row — without a forward pass.

        The code is a pure function of the text and the bank, so a persisted
        row for the same text under the same bank is byte-for-byte what the
        encoder would produce again. This is what lets a boot reseat hundreds
        of unchanged records in milliseconds instead of re-encoding them all
        (measured 2026-09-05: 258 records, ~7 s per publish, linear).
        """
        if not str(text).strip():
            return NO_SIGNAL
        arr = np.asarray(code, dtype=np.uint8).reshape(-1)
        expected = self._config.dimension // 8
        if arr.shape[0] != expected:
            raise ValueError(f"code width {arr.shape[0]} bytes != expected {expected}")
        self._codes[record_id] = arr.copy()
        self._texts[record_id] = text
        self._invalidate()
        return STORED

    def remove(self, record_id: str) -> bool:
        """Delete a record. True if it was present."""
        if record_id not in self._codes:
            return False
        del self._codes[record_id]
        del self._texts[record_id]
        self._invalidate()
        return True

    def code(self, record_id: str) -> np.ndarray:
        """The stored packed code. A copy — callers must not reach into state."""
        return self._codes[record_id].copy()

    # ── query path ────────────────────────────────────────────────────────────

    def query_status(self, text: str) -> str:
        return NO_SIGNAL if not str(text).strip() else OK

    def retrieval_k(self) -> int:
        return retrieval_k_for_pool(len(self._codes), floor_target=self._config.floor_target)

    def window(self, text: str) -> list[tuple[str, int]]:
        """The top-K nearest records as (record_id, Hamming distance).

        Ordered by distance, then by record id. The id tiebreak is not cosmetic:
        Hamming distance over a discrete code produces ties constantly, and
        without a total order the same query could return the same records in a
        different sequence on a different run.
        """
        if not self._codes or self.query_status(text) == NO_SIGNAL:
            return []

        codes = quantize_row(self._encode_one(text))
        if not np.any(codes):
            return []
        probe = _project(codes, self._bank, self._tie_bits)

        matrix, order = self._scan_matrix()
        # Explicit wide accumulator: a uint8 sum wraps at 256 and D=16384 needs
        # to count up to 16384 differing bits.
        distances = _POPCOUNT[matrix ^ probe].sum(axis=1, dtype=np.int64)

        ranked = sorted(zip(order, distances.tolist()), key=lambda hit: (hit[1], hit[0]))
        return [(rid, int(dist)) for rid, dist in ranked[: self.retrieval_k()]]

    def fingerprint(self) -> str:
        """Content hash for replay, independent of insertion order.

        Binds the bank identity as well as the records: two planes holding the
        same texts under different banks are not the same plane, because their
        codes — and therefore every window they return — differ.
        """
        digest = hashlib.sha256()
        digest.update(
            f"semantic-plane-v1|{ALGORITHM}|D={self._config.dimension}"
            f"|seed={self._config.seed_index}".encode()
        )
        for record_id in sorted(self._codes):
            digest.update(record_id.encode())
            digest.update(self._codes[record_id].tobytes())
        return digest.hexdigest()

    # ── internals ─────────────────────────────────────────────────────────────

    def _encode_one(self, text: str) -> np.ndarray:
        """One text per call, deliberately. Do not batch this for speed.

        A transformer forward is not batch-invariant: a different batch shape
        selects different GEMM tiling, so the reduction order and therefore the
        last ulp of the latent depend on which other texts were encoded
        alongside it. Measured against real Ettin weights that perturbation is
        ~1e-7 and INT8 quantisation absorbs it completely -- but absorbed at the
        scale we measured is not the same as cannot happen. Encoding singly
        removes the variable rather than bounding it, which is what lets a
        record's code depend on its own text and nothing else.
        """
        matrix = np.asarray(self._encoder.encode([text]))
        if matrix.shape != (1, LATENT_DIM):
            raise ValueError(
                f"encoder must return one {LATENT_DIM}-d row, got shape={matrix.shape}"
            )
        return matrix[0]

    def _invalidate(self) -> None:
        self._matrix = None
        self._order = []

    def _scan_matrix(self) -> tuple[np.ndarray, list[str]]:
        if self._matrix is None:
            self._order = sorted(self._codes)
            self._matrix = np.stack([self._codes[rid] for rid in self._order])
        return self._matrix, self._order


__all__ = [
    "ALGORITHM",
    "K_PER_1000",
    "LAWFUL_DIMENSIONS",
    "MIN_RETRIEVAL_K",
    "NO_SIGNAL",
    "OK",
    "PROJECTION_NAMESPACE",
    "STORED",
    "PlaneConfig",
    "SemanticPlane",
    "balanced_tie_bits",
    "quantize_row",
    "rademacher_bank",
    "retrieval_k_for_pool",
]

"""Live FP32 Ettin integration test against the pinned local snapshot.

Uses the SSD's pinned snapshot; ``OMEGA_PROGRAM1_ETTIN_SNAPSHOT`` can select a
different local copy explicitly. The probe strings are deliberately
generic public sentences: no Program-I corpus text may appear here, so this test
cannot leak sealed material or tune anything against it.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from benchmarks.memory_organ.program1.ettin_fp32 import ETTIN_DIM, ETTIN_SNAPSHOT, load_ettin_fp32
from core.paths import OMEGA_ROOT

PROBE_TEXTS = (
    "The quick brown fox jumps over the lazy dog.",
    "Water boils at one hundred degrees Celsius at sea level.",
)


@pytest.fixture(scope="module")
def snapshot() -> Path:
    raw = os.environ.get("OMEGA_PROGRAM1_ETTIN_SNAPSHOT")
    path = Path(raw) if raw else (
        OMEGA_ROOT / "memory" / "models" / "models--tasksource--ettin-17m-embed"
        / "snapshots" / ETTIN_SNAPSHOT
    )
    if not path.is_dir():
        if raw:
            pytest.fail(f"configured Ettin snapshot does not exist: {path}")
        pytest.skip(f"pinned SSD Ettin snapshot is absent: {path}")
    return path


def test_pinned_snapshot_encodes_to_deterministic_fp32_unit_vectors(
    snapshot: Path,
) -> None:
    encoder = load_ettin_fp32(snapshot)

    first = encoder.encode(PROBE_TEXTS, batch_size=2)
    second = encoder.encode(PROBE_TEXTS, batch_size=2)

    assert first.shape == (2, ETTIN_DIM)
    assert first.dtype == np.float32
    assert np.all(np.isfinite(first))

    norms = np.linalg.norm(first, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-6), norms

    assert np.array_equal(first, second)


def test_batching_does_not_change_the_geometry(snapshot: Path) -> None:
    """Padding width varies with batch composition; vectors must not."""
    encoder = load_ettin_fp32(snapshot)
    batched = encoder.encode(PROBE_TEXTS, batch_size=2)
    one_at_a_time = np.concatenate(
        [encoder.encode([text], batch_size=1) for text in PROBE_TEXTS], axis=0
    )
    assert np.allclose(batched, one_at_a_time, atol=1e-6)


def test_distinct_sentences_are_not_the_same_vector(snapshot: Path) -> None:
    encoder = load_ettin_fp32(snapshot)
    vectors = encoder.encode(PROBE_TEXTS, batch_size=2)
    cosine = float(np.dot(vectors[0], vectors[1]))
    assert -1.0 <= cosine <= 1.0
    assert not np.allclose(vectors[0], vectors[1])

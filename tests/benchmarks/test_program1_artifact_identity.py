from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from core.paths import OMEGA_ROOT
from benchmarks.memory_organ.program1.artifact_identity import (
    PROBE_TEXTS,
    ArtifactIdentityError,
    array_fingerprint,
    build_fingerprint,
    compare_fingerprints,
)
from benchmarks.memory_organ.program1.ettin_fp32 import ETTIN_SNAPSHOT


def test_probe_texts_are_fixed_public_and_non_empty() -> None:
    assert len(PROBE_TEXTS) >= 8
    assert all(isinstance(t, str) and t.strip() for t in PROBE_TEXTS)
    assert len(set(PROBE_TEXTS)) == len(PROBE_TEXTS)


def test_probe_texts_are_disjoint_from_the_sealed_corpus() -> None:
    """A probe drawn from the corpus would leak evaluation text into a build
    artifact and make the identity check corpus-dependent."""
    corpus = OMEGA_ROOT / "benchmarks" / "memory_organ" / "program1_private" / "corpus_v3"
    if not corpus.exists():
        pytest.skip("private corpus not present")
    from benchmarks.memory_organ.program1.corpus_pipeline import load_jsonl_objects
    from benchmarks.memory_organ.program1.contracts import render_candidate

    sealed = {render_candidate(c["record"]).strip()
              for c in load_jsonl_objects(corpus / "candidates.jsonl")}
    sealed |= {str(c.get("query") or "").strip()
               for c in load_jsonl_objects(corpus / "cases.jsonl")}
    for probe in PROBE_TEXTS:
        assert probe.strip() not in sealed


def test_array_fingerprint_is_deterministic_and_binds_dtype_and_shape() -> None:
    a = np.arange(12, dtype=np.int8).reshape(3, 4)
    assert array_fingerprint(a) == array_fingerprint(a.copy())
    # same bytes, different shape must not collide
    assert array_fingerprint(a) != array_fingerprint(a.reshape(4, 3))
    # same shape, different dtype must not collide
    assert array_fingerprint(a) != array_fingerprint(a.astype(np.int16))


def test_array_fingerprint_detects_a_single_bit_change() -> None:
    a = np.zeros((4, 8), dtype=np.int8)
    b = a.copy()
    b[2, 5] = 1
    assert array_fingerprint(a) != array_fingerprint(b)


def test_compare_accepts_identical_fingerprints() -> None:
    left = {"fp32_sha256": "a", "int8_codes_sha256": "b", "int8_scale_sha256": "c",
            "probe_count": 8, "versions": {"torch": "2.7.1+cpu"}}
    assert compare_fingerprints(left, dict(left))["identical"] is True


@pytest.mark.parametrize("field", ["fp32_sha256", "int8_codes_sha256", "int8_scale_sha256"])
def test_compare_refuses_any_differing_digest(field: str) -> None:
    left = {"fp32_sha256": "a", "int8_codes_sha256": "b", "int8_scale_sha256": "c",
            "probe_count": 8, "versions": {}}
    right = dict(left, **{field: "DIFFERENT"})
    with pytest.raises(ArtifactIdentityError, match=field):
        compare_fingerprints(left, right)


def test_compare_refuses_a_different_probe_count() -> None:
    left = {"fp32_sha256": "a", "int8_codes_sha256": "b", "int8_scale_sha256": "c",
            "probe_count": 8, "versions": {}}
    with pytest.raises(ArtifactIdentityError, match="probe_count"):
        compare_fingerprints(left, dict(left, probe_count=9))


def test_version_skew_is_reported_but_does_not_by_itself_fail() -> None:
    """Different library versions that still produce identical bits are fine.

    Bit-identity is the property under test; matching version strings is
    supporting evidence, so skew is surfaced as a warning rather than treated as
    a failure that identical outputs would contradict.
    """
    left = {"fp32_sha256": "a", "int8_codes_sha256": "b", "int8_scale_sha256": "c",
            "probe_count": 8, "versions": {"torch": "2.7.1+cpu"}}
    right = dict(left, versions={"torch": "2.8.0"})
    result = compare_fingerprints(left, right)
    assert result["identical"] is True
    assert result["version_skew"], "skew must be reported"


def test_build_fingerprint_on_the_live_snapshot_is_reproducible() -> None:
    snapshot = (
        OMEGA_ROOT
        / "memory"
        / "models"
        / "models--tasksource--ettin-17m-embed"
        / "snapshots"
        / ETTIN_SNAPSHOT
    )
    if not snapshot.exists():
        pytest.skip("pinned snapshot absent")
    first = build_fingerprint(snapshot)
    second = build_fingerprint(snapshot)
    assert compare_fingerprints(first, second)["identical"] is True
    assert first["probe_count"] == len(PROBE_TEXTS)
    json.dumps(first)  # must be serialisable for cross-interpreter transport

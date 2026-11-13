from __future__ import annotations

import hashlib
import inspect
import json
import os
from collections import Counter
from pathlib import Path

import pytest

from core.paths import OMEGA_ROOT

from benchmarks.memory_organ.program1.calibration import (
    CALIBRATION_COUNT,
    CALIBRATION_MIX,
    CalibrationLeakError,
    assert_disjoint_from_corpus,
    build_calibration_set,
    calibration_manifest,
    content_signature,
)

PRIVATE_ROOT = OMEGA_ROOT / "benchmarks" / "memory_organ" / "program1_private"
CORPUS = PRIVATE_ROOT / "corpus_v3"
RESEARCH_ROOT = OMEGA_ROOT / "docs" / "engineering" / "memory-organ"


def _longmemeval_path() -> Path:
    override = os.environ.get("OMEGA_PROGRAM1_LONGMEMEVAL")
    path = Path(override) if override else PRIVATE_ROOT / "longmemeval_s_cleaned.json"
    if override is not None and (not override.strip() or not path.is_file()):
        pytest.fail(f"OMEGA_PROGRAM1_LONGMEMEVAL does not name a dataset file: {override!r}")
    return path


@pytest.mark.parametrize("override", ["missing.json", ""])
def test_invalid_explicit_dataset_override_fails(monkeypatch, tmp_path, override):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMEGA_PROGRAM1_LONGMEMEVAL", override)
    with pytest.raises(pytest.fail.Exception, match="does not name a dataset file"):
        _longmemeval_path()


@pytest.fixture
def calibration_sources(tmp_path):
    """Exercise the real readers and sampler without private evaluation data."""
    research = tmp_path / "research"
    research.mkdir()
    omega_texts = [
        f"Research entry {i}: The observatory keeps detailed calibration records "
        "so independent investigators can reproduce every telescope measurement."
        for i in range(CALIBRATION_MIX["omega"] + 3)
    ]
    public_texts = [
        f"Public exchange {i}: The museum archivist described how conservation "
        "teams document the humidity and temperature of each storage chamber."
        for i in range(CALIBRATION_MIX["public"] + 3)
    ]
    (research / "notes.md").write_text("\n\n".join(omega_texts), encoding="utf-8")
    dataset = tmp_path / "longmemeval.json"
    dataset.write_text(json.dumps([{"haystack_sessions": [[
        {"role": "user", "content": text} for text in public_texts
    ]]}]), encoding="utf-8")
    consumed = tmp_path / "sources.jsonl"
    consumed.write_text("\n".join(json.dumps({"provenance": {
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }}) for text in (
        min(omega_texts, key=lambda text: hashlib.sha256(text.encode()).hexdigest()),
        min(public_texts, key=lambda text: hashlib.sha256(text.encode()).hexdigest()),
    )), encoding="utf-8")
    return {"omega_root": research, "longmemeval_path": dataset, "source_pool": consumed}


def _sealed():
    from benchmarks.memory_organ.program1.contracts import render_candidate
    from benchmarks.memory_organ.program1.corpus_pipeline import load_jsonl_objects

    texts = {render_candidate(c["record"]).strip()
             for c in load_jsonl_objects(CORPUS / "candidates.jsonl")}
    texts |= {str(c.get("query") or "").strip()
              for c in load_jsonl_objects(CORPUS / "cases.jsonl")}
    return {t for t in texts if t}


def test_mix_matches_the_corpus_source_proportions() -> None:
    assert sum(CALIBRATION_MIX.values()) == CALIBRATION_COUNT
    assert set(CALIBRATION_MIX) == {"omega", "public", "synthetic"}


def test_missing_explicit_source_pool_fails(calibration_sources, tmp_path) -> None:
    inputs = {**calibration_sources, "source_pool": tmp_path / "missing.jsonl"}
    with pytest.raises(FileNotFoundError, match="missing.jsonl"):
        build_calibration_set(**inputs)


def test_default_source_pool_is_anchored_to_portable_product_root() -> None:
    default = inspect.signature(build_calibration_set).parameters["source_pool"].default
    assert default == PRIVATE_ROOT / "source_pool_v1" / "sources.jsonl"


def test_content_signature_ignores_order_and_case() -> None:
    assert content_signature("The archivist stored the ledger") == content_signature(
        "LEDGER stored the ARCHIVIST the"
    )
    assert content_signature("alpha beta") != content_signature("alpha gamma")


def test_leak_detector_catches_an_exact_reused_string() -> None:
    sealed = {"The physician examined the x-ray in the ward."}
    with pytest.raises(CalibrationLeakError, match="exact"):
        assert_disjoint_from_corpus(
            [{"text": "The physician examined the x-ray in the ward."}], sealed
        )


def test_leak_detector_catches_a_reordered_near_duplicate() -> None:
    """Exact-string comparison alone would miss a shuffled restatement."""
    sealed = {"The physician examined the x-ray in the ward."}
    with pytest.raises(CalibrationLeakError, match="signature"):
        assert_disjoint_from_corpus(
            [{"text": "In the ward the physician examined the x-ray"}], sealed
        )


def test_leak_detector_accepts_genuinely_unrelated_text() -> None:
    sealed = {"The physician examined the x-ray in the ward."}
    assert_disjoint_from_corpus([{"text": "Snow settled on the observatory roof."}], sealed)


def test_built_calibration_set_is_disjoint_from_the_sealed_corpus() -> None:
    dataset = _longmemeval_path()
    if not CORPUS.exists():
        pytest.skip("private corpus absent")
    if not dataset.is_file():
        pytest.skip("LongMemEval data absent; set OMEGA_PROGRAM1_LONGMEMEVAL to the dataset")
    source_pool = PRIVATE_ROOT / "source_pool_v1" / "sources.jsonl"
    if not source_pool.is_file():
        pytest.skip("original private source pool absent")
    rows = build_calibration_set(
        omega_root=RESEARCH_ROOT, longmemeval_path=dataset, source_pool=source_pool,
    )
    assert len(rows) == CALIBRATION_COUNT
    assert_disjoint_from_corpus(rows, _sealed())


def test_calibration_set_is_deterministic(calibration_sources) -> None:
    a = build_calibration_set(**calibration_sources)
    b = build_calibration_set(**calibration_sources)
    assert len(a) == CALIBRATION_COUNT
    assert Counter(row["source_class"] for row in a) == CALIBRATION_MIX
    assert [r["text_sha256"] for r in a] == [r["text_sha256"] for r in b]
    consumed = {
        json.loads(line)["provenance"]["text_sha256"]
        for line in calibration_sources["source_pool"].read_text(encoding="utf-8").splitlines()
    }
    assert consumed.isdisjoint(row["text_sha256"] for row in a)
    unfiltered = build_calibration_set(**{**calibration_sources, "source_pool": None})
    assert consumed.issubset(row["text_sha256"] for row in unfiltered)


def test_manifest_pins_hashes_without_carrying_text(calibration_sources) -> None:
    rows = build_calibration_set(**calibration_sources)
    manifest = calibration_manifest(rows)
    blob = str(manifest)
    for row in rows:
        assert row["text"] not in blob, "manifest must not carry calibration text"
    assert manifest["count"] == CALIBRATION_COUNT
    assert len(manifest["text_sha256_list"]) == CALIBRATION_COUNT
    assert manifest["text_sha256_list"] == [
        hashlib.sha256(row["text"].encode()).hexdigest() for row in rows
    ]

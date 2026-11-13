from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pytest

import benchmarks.memory_organ.program1.fp32_run as fp32_run
from benchmarks.memory_organ.program1.contracts import render_candidate
from benchmarks.memory_organ.program1.fp32_run import (
    LAWFUL_PARTITIONS,
    REQUIRED_RUN_ARTIFACTS,
    run_fp32_partition,
)

# Distinctive strings planted in the private corpus. No emitted artifact may
# contain any of them: the run publishes IDs, ranks and scores, never text.
SECRET_TITLE = "ZZTITLESECRETZZ"
SECRET_SUMMARY = "ZZSUMMARYSECRETZZ"
SECRET_TEXT = "ZZTEXTSECRETZZ"
SECRET_QUERY = "ZZQUERYSECRETZZ"

PARTITION_SIZES = {"development": 6, "validation": 4, "locked_test": 4}


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


class _TableEncoder:
    """Deterministic stand-in: each known text owns a fixed basis vector."""

    def __init__(self, table: dict[str, np.ndarray]) -> None:
        self._table = table
        self.calls = 0

    def encode(self, texts: Sequence[str], *, batch_size: int = 32) -> np.ndarray:
        self.calls += 1
        rows = [self._table[text] for text in texts]
        if not rows:
            return np.zeros((0, 8), dtype=np.float32)
        return np.stack(rows).astype(np.float32)


@dataclass
class SealedFixture:
    corpus_dir: Path
    seal_dir: Path
    output_dir: Path
    candidates_path: Path
    cases_path: Path
    run_kwargs: dict[str, Any]
    encoder: _TableEncoder


@pytest.fixture
def sealed_fp32_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SealedFixture:
    corpus_dir = tmp_path / "corpus_v1"
    seal_dir = tmp_path / "corpus_seal_v1"
    corpus_dir.mkdir()
    seal_dir.mkdir()

    candidates: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    index = 0
    for partition, size in sorted(PARTITION_SIZES.items()):
        for slot in range(size):
            cid = f"{partition}-cand-{slot:03d}"
            candidates.append(
                {
                    "candidate_id": cid,
                    "partition": partition,
                    "semantic_family_id": f"{partition}-fam-{slot:03d}",
                    "record": {
                        "title": f"{SECRET_TITLE} {cid}",
                        "summary": f"{SECRET_SUMMARY} {cid}",
                        "text": f"{SECRET_TEXT} {cid}",
                    },
                }
            )
            index += 1

        for slot in range(size):
            cid = f"{partition}-cand-{slot:03d}"
            other = f"{partition}-cand-{(slot + 1) % size:03d}"
            if slot == 0:
                cases.append(
                    {
                        "case_id": f"{partition}-case-{slot:03d}",
                        "partition": partition,
                        "stratum": "no_signal",
                        "primary_target_id": None,
                        "semantic_family_id": "",
                        "query": "   ",
                        "hard_negative_ids": [],
                        "unrelated_negative_ids": [],
                    }
                )
                continue
            cases.append(
                {
                    "case_id": f"{partition}-case-{slot:03d}",
                    "partition": partition,
                    "stratum": "ordinary_synonyms",
                    "primary_target_id": cid,
                    "semantic_family_id": f"{partition}-fam-{slot:03d}",
                    "query": f"{SECRET_QUERY} {cid}",
                    "hard_negative_ids": [],
                    "unrelated_negative_ids": [other],
                }
            )

    candidates_path = corpus_dir / "candidates.jsonl"
    cases_path = corpus_dir / "cases.jsonl"
    candidates_path.write_bytes(b"".join(_canonical(row) for row in candidates))
    cases_path.write_bytes(b"".join(_canonical(row) for row in cases))

    manifest = {
        "schema_version": "omega.semantic_retina.program1.corpus.v1",
        "renderer_version": "omega.semantic_retina.render.v1",
        "candidate_source_jsonl_sha256": hashlib.sha256(candidates_path.read_bytes()).hexdigest(),
        "case_source_jsonl_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
        "candidate_counts_by_partition": dict(PARTITION_SIZES),
        "case_counts_by_partition": dict(PARTITION_SIZES),
    }
    manifest_path = seal_dir / "corpus_manifest.json"
    manifest_path.write_bytes(_canonical(manifest))
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (seal_dir / "corpus_manifest.sha256").write_text(manifest_sha + "\n", encoding="utf-8")
    (seal_dir / "SEALED.json").write_bytes(
        _canonical(
            {
                "schema_version": "omega.semantic_retina.program1.corpus_seal.v1",
                "sealed": True,
                "corpus_manifest_sha256": manifest_sha,
            }
        )
    )
    (seal_dir / "split_manifest.json").write_bytes(
        _canonical(
            {
                "schema_version": "omega.semantic_retina.program1.split.v1",
                "candidate_counts_by_partition": dict(PARTITION_SIZES),
                "case_counts_by_partition": dict(PARTITION_SIZES),
            }
        )
    )

    # One basis vector per candidate; each query reuses its target's vector so the
    # target ranks first and the run produces a well-formed report.
    dim = len(candidates)
    table: dict[str, np.ndarray] = {}
    for position, row in enumerate(candidates):
        vector = np.zeros(dim, dtype=np.float32)
        vector[position] = 1.0
        table[render_candidate(row["record"])] = vector
    by_id = {row["candidate_id"]: position for position, row in enumerate(candidates)}
    for row in cases:
        target = row["primary_target_id"]
        vector = np.zeros(dim, dtype=np.float32)
        if target is None:
            vector[0] = 1.0
        else:
            vector[by_id[target]] = 1.0
        table[row["query"]] = vector

    encoder = _TableEncoder(table)
    monkeypatch.setattr(fp32_run, "load_ettin_fp32", lambda snapshot, **kw: encoder)

    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    output_dir = tmp_path / "run"

    return SealedFixture(
        corpus_dir=corpus_dir,
        seal_dir=seal_dir,
        output_dir=output_dir,
        candidates_path=candidates_path,
        cases_path=cases_path,
        encoder=encoder,
        run_kwargs={
            "corpus_dir": corpus_dir,
            "seal_dir": seal_dir,
            "snapshot": snapshot,
            "partition": "development",
            "output_dir": output_dir,
            "repo_commit": "0" * 40,
        },
    )


def test_runner_refuses_locked_test_partition(sealed_fp32_fixture: SealedFixture) -> None:
    kwargs = dict(sealed_fp32_fixture.run_kwargs)
    kwargs["partition"] = "locked_test"
    with pytest.raises(ValueError, match="locked_test"):
        run_fp32_partition(**kwargs)


def test_runner_refuses_corpus_hash_mismatch(sealed_fp32_fixture: SealedFixture) -> None:
    sealed_fp32_fixture.candidates_path.write_bytes(
        sealed_fp32_fixture.candidates_path.read_bytes() + b" "
    )
    with pytest.raises(ValueError, match="corpus seal"):
        run_fp32_partition(**sealed_fp32_fixture.run_kwargs)


def test_locked_test_is_not_a_lawful_partition() -> None:
    assert "locked_test" not in LAWFUL_PARTITIONS
    assert set(LAWFUL_PARTITIONS) == {"development", "validation"}


def test_runner_refuses_an_unknown_partition(sealed_fp32_fixture: SealedFixture) -> None:
    kwargs = dict(sealed_fp32_fixture.run_kwargs)
    kwargs["partition"] = "training"
    with pytest.raises(ValueError, match="training"):
        run_fp32_partition(**kwargs)


def test_runner_refuses_a_broken_seal(sealed_fp32_fixture: SealedFixture) -> None:
    sealed = sealed_fp32_fixture.seal_dir / "SEALED.json"
    sealed.write_bytes(
        _canonical(
            {
                "schema_version": "omega.semantic_retina.program1.corpus_seal.v1",
                "sealed": True,
                "corpus_manifest_sha256": "0" * 64,
            }
        )
    )
    with pytest.raises(ValueError, match="corpus seal"):
        run_fp32_partition(**sealed_fp32_fixture.run_kwargs)


def test_gates_run_before_the_encoder_is_ever_loaded(
    sealed_fp32_fixture: SealedFixture,
) -> None:
    """A refused run must not touch the model."""
    kwargs = dict(sealed_fp32_fixture.run_kwargs)
    kwargs["partition"] = "locked_test"
    with pytest.raises(ValueError):
        run_fp32_partition(**kwargs)
    assert sealed_fp32_fixture.encoder.calls == 0


def test_runner_emits_every_required_artifact(sealed_fp32_fixture: SealedFixture) -> None:
    result = run_fp32_partition(**sealed_fp32_fixture.run_kwargs)
    emitted = {path.name for path in sealed_fp32_fixture.output_dir.iterdir()}
    assert set(REQUIRED_RUN_ARTIFACTS) <= emitted
    assert result["partition"] == "development"

    for name in ("quark_manifest.json", "projection_manifest.json", "seed_manifest.json"):
        payload = json.loads((sealed_fp32_fixture.output_dir / name).read_text())
        assert payload["status"] == "not_run"
        assert payload["reason"]


def test_runner_scores_only_the_selected_partition(
    sealed_fp32_fixture: SealedFixture,
) -> None:
    result = run_fp32_partition(**sealed_fp32_fixture.run_kwargs)
    size = PARTITION_SIZES["development"]
    assert result["candidate_count"] == size
    assert result["case_count"] == size

    rows = [
        json.loads(line)
        for line in (sealed_fp32_fixture.output_dir / "per_query_results.jsonl")
        .read_text()
        .splitlines()
    ]
    assert len(rows) == size
    assert all(row["case_id"].startswith("development-") for row in rows)


def test_runner_encodes_twice_and_reports_determinism(
    sealed_fp32_fixture: SealedFixture,
) -> None:
    result = run_fp32_partition(**sealed_fp32_fixture.run_kwargs)
    assert result["repeat_mismatches"] == 0
    # candidates + queries, each encoded twice
    assert sealed_fp32_fixture.encoder.calls == 4


def test_runner_counts_nondeterminism_instead_of_hiding_it(
    sealed_fp32_fixture: SealedFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = sealed_fp32_fixture.encoder

    class Drifting:
        def __init__(self) -> None:
            self.calls = 0

        def encode(self, texts: Sequence[str], *, batch_size: int = 32) -> np.ndarray:
            self.calls += 1
            vectors = base.encode(texts, batch_size=batch_size).copy()
            if self.calls == 3 and len(vectors):
                vectors[0] = vectors[0][::-1]
            return vectors

    drifting = Drifting()
    monkeypatch.setattr(fp32_run, "load_ettin_fp32", lambda snapshot, **kw: drifting)
    result = run_fp32_partition(**sealed_fp32_fixture.run_kwargs)
    assert result["repeat_mismatches"] >= 1
    assert result["gate_pass"] is False


def test_no_emitted_artifact_leaks_private_corpus_text(
    sealed_fp32_fixture: SealedFixture,
) -> None:
    run_fp32_partition(**sealed_fp32_fixture.run_kwargs)
    secrets = (SECRET_TITLE, SECRET_SUMMARY, SECRET_TEXT, SECRET_QUERY)
    for path in sorted(sealed_fp32_fixture.output_dir.rglob("*")):
        if not path.is_file():
            continue
        blob = path.read_text(encoding="utf-8", errors="replace")
        for secret in secrets:
            assert secret not in blob, f"{path.name} leaked {secret}"


def test_runner_refuses_to_overwrite_an_existing_run(
    sealed_fp32_fixture: SealedFixture,
) -> None:
    run_fp32_partition(**sealed_fp32_fixture.run_kwargs)
    with pytest.raises(FileExistsError):
        run_fp32_partition(**sealed_fp32_fixture.run_kwargs)


def test_runner_surfaces_both_k10_and_k32_results(sealed_fp32_fixture: SealedFixture) -> None:
    """The gating K=32 view must reach the caller, not only the K=10 diagnostic."""
    result = run_fp32_partition(**sealed_fp32_fixture.run_kwargs)
    assert "per_stratum_recall_at_10" in result
    assert "per_stratum_recall_at_32" in result
    assert result["deferred_to_rerank"] == ["hard_distinction_pairwise"]

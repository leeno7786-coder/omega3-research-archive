from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pytest

import benchmarks.memory_organ.program1.hqnp_run as hqnp_run
from benchmarks.memory_organ.program1.hqnp_run import (
    HQNP_RUN_ARTIFACTS,
    LAWFUL_PARTITIONS,
    run_hqnp_partition,
)

SECRET_TEXT = "ZZHQNPTEXTSECRETZZ"
SECRET_QUERY = "ZZHQNPQUERYSECRETZZ"
PARTITION_SIZES = {"development": 8, "validation": 6, "locked_test": 6}
LATENT_DIM = 256


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


class _TableEncoder:
    """Deterministic stand-in emitting 256-d unit latents."""

    def __init__(self, table: dict[str, np.ndarray]) -> None:
        self._table = table
        self.calls = 0

    def encode(self, texts: Sequence[str], *, batch_size: int = 32) -> np.ndarray:
        self.calls += 1
        rows = [self._table[t] for t in texts]
        if not rows:
            return np.zeros((0, LATENT_DIM), dtype=np.float32)
        return np.stack(rows).astype(np.float32)


@dataclass
class HQNPFixture:
    corpus_dir: Path
    seal_dir: Path
    output_dir: Path
    candidates_path: Path
    g0_dir: Path
    encoder: _TableEncoder
    run_kwargs: dict[str, Any]


@pytest.fixture
def hqnp_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HQNPFixture:
    corpus_dir, seal_dir, g0_dir = tmp_path / "corpus", tmp_path / "seal", tmp_path / "g0"
    for d in (corpus_dir, seal_dir, g0_dir / "validation"):
        d.mkdir(parents=True)

    candidates: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    for partition, size in sorted(PARTITION_SIZES.items()):
        for slot in range(size):
            cid = f"{partition}-c{slot:03d}"
            candidates.append({
                "candidate_id": cid, "partition": partition,
                "semantic_family_id": f"{partition}-f{slot:03d}",
                "record": {"title": "", "summary": "", "text": f"{SECRET_TEXT} {cid}"},
            })
            no_signal = slot == 0
            cases.append({
                "case_id": f"{partition}-q{slot:03d}", "partition": partition,
                "stratum": "no_signal" if no_signal else "ordinary_synonyms",
                "primary_target_id": None if no_signal else cid,
                "semantic_family_id": "" if no_signal else f"{partition}-f{slot:03d}",
                "query": "   " if no_signal else f"{SECRET_QUERY} {cid}",
                "hard_negative_ids": [], "unrelated_negative_ids": [],
            })

    candidates_path, cases_path = corpus_dir / "candidates.jsonl", corpus_dir / "cases.jsonl"
    candidates_path.write_bytes(b"".join(_canonical(r) for r in candidates))
    cases_path.write_bytes(b"".join(_canonical(r) for r in cases))

    manifest = {
        "schema_version": "omega.semantic_retina.program1.corpus.v1",
        "candidate_source_jsonl_sha256": hashlib.sha256(candidates_path.read_bytes()).hexdigest(),
        "case_source_jsonl_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
    }
    (seal_dir / "corpus_manifest.json").write_bytes(_canonical(manifest))
    sha = hashlib.sha256((seal_dir / "corpus_manifest.json").read_bytes()).hexdigest()
    (seal_dir / "corpus_manifest.sha256").write_text(sha + "\n")
    (seal_dir / "SEALED.json").write_bytes(_canonical(
        {"schema_version": "x", "sealed": True, "corpus_manifest_sha256": sha}))
    (seal_dir / "split_manifest.json").write_bytes(_canonical({"schema_version": "y"}))
    (g0_dir / "validation" / "metrics.json").write_bytes(_canonical({"gate_pass": True}))

    rng = np.random.default_rng(19)
    table: dict[str, np.ndarray] = {}
    for row in candidates:
        v = rng.standard_normal(LATENT_DIM)
        table[row["record"]["text"]] = (v / np.linalg.norm(v)).astype(np.float32)
    for row in cases:
        if row["primary_target_id"] is None:
            table[row["query"]] = np.zeros(LATENT_DIM, dtype=np.float32)  # no-signal
        else:
            target = next(c for c in candidates if c["candidate_id"] == row["primary_target_id"])
            base = table[target["record"]["text"]]
            noise = rng.standard_normal(LATENT_DIM) * 0.10
            v = base + noise
            table[row["query"]] = (v / np.linalg.norm(v)).astype(np.float32)

    encoder = _TableEncoder(table)
    monkeypatch.setattr(hqnp_run, "load_ettin_fp32", lambda snapshot, **kw: encoder)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()

    return HQNPFixture(
        corpus_dir=corpus_dir, seal_dir=seal_dir, output_dir=tmp_path / "run",
        candidates_path=candidates_path, g0_dir=g0_dir, encoder=encoder,
        run_kwargs={
            "corpus_dir": corpus_dir, "seal_dir": seal_dir, "snapshot": snapshot,
            "partition": "development", "output_dir": tmp_path / "run",
            "repo_commit": "0" * 40, "g0_evidence_dir": g0_dir,
            "seed_indices": (0,), "dimensions": (4096,),
        },
    )


# ── gate ordering ──────────────────────────────────────────────────────────────

def test_locked_test_is_not_lawful(hqnp_fixture: HQNPFixture) -> None:
    assert "locked_test" not in LAWFUL_PARTITIONS
    kwargs = dict(hqnp_fixture.run_kwargs, partition="locked_test")
    with pytest.raises(ValueError, match="locked_test"):
        run_hqnp_partition(**kwargs)


def test_unknown_partition_is_refused(hqnp_fixture: HQNPFixture) -> None:
    with pytest.raises(ValueError, match="training"):
        run_hqnp_partition(**dict(hqnp_fixture.run_kwargs, partition="training"))


def test_missing_fp32_g0_evidence_is_refused(hqnp_fixture: HQNPFixture) -> None:
    (hqnp_fixture.g0_dir / "validation" / "metrics.json").unlink()
    with pytest.raises(ValueError, match="G0"):
        run_hqnp_partition(**hqnp_fixture.run_kwargs)


def test_failed_fp32_g0_evidence_is_refused(hqnp_fixture: HQNPFixture) -> None:
    (hqnp_fixture.g0_dir / "validation" / "metrics.json").write_bytes(_canonical({"gate_pass": False}))
    with pytest.raises(ValueError, match="G0"):
        run_hqnp_partition(**hqnp_fixture.run_kwargs)


def test_corpus_seal_mismatch_is_refused(hqnp_fixture: HQNPFixture) -> None:
    hqnp_fixture.candidates_path.write_bytes(hqnp_fixture.candidates_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="corpus seal"):
        run_hqnp_partition(**hqnp_fixture.run_kwargs)


@pytest.mark.parametrize("seeds", [(5,), (-1,), (0, 7)])
def test_unlawful_seed_indices_are_refused(hqnp_fixture: HQNPFixture, seeds) -> None:
    with pytest.raises(ValueError):
        run_hqnp_partition(**dict(hqnp_fixture.run_kwargs, seed_indices=seeds))


@pytest.mark.parametrize("dims", [(1024,), (8192,), (4096, 32768)])
def test_unlawful_dimensions_are_refused(hqnp_fixture: HQNPFixture, dims) -> None:
    with pytest.raises(ValueError):
        run_hqnp_partition(**dict(hqnp_fixture.run_kwargs, dimensions=dims))


def test_every_gate_runs_before_the_encoder_is_loaded(hqnp_fixture: HQNPFixture) -> None:
    for kwargs in (
        dict(hqnp_fixture.run_kwargs, partition="locked_test"),
        dict(hqnp_fixture.run_kwargs, seed_indices=(9,)),
        dict(hqnp_fixture.run_kwargs, dimensions=(999,)),
    ):
        with pytest.raises(ValueError):
            run_hqnp_partition(**kwargs)
    assert hqnp_fixture.encoder.calls == 0


# ── execution ──────────────────────────────────────────────────────────────────

def test_runner_emits_every_required_artifact(hqnp_fixture: HQNPFixture) -> None:
    result = run_hqnp_partition(**hqnp_fixture.run_kwargs)
    emitted = {p.name for p in hqnp_fixture.output_dir.iterdir()}
    assert set(HQNP_RUN_ARTIFACTS) <= emitted
    assert result["partition"] == "development"
    quark = json.loads((hqnp_fixture.output_dir / "quark_manifest.json").read_text())
    assert quark["status"] == "not_run"


def test_seed_manifest_lists_all_five_seeds_even_when_one_runs(
    hqnp_fixture: HQNPFixture,
) -> None:
    """Preregistration forbids omitting seeds from the record."""
    run_hqnp_partition(**hqnp_fixture.run_kwargs)
    seeds = json.loads((hqnp_fixture.output_dir / "seed_manifest.json").read_text())
    assert [entry["seed_index"] for entry in seeds["seeds"]] == [0, 1, 2, 3, 4]


def test_projection_manifest_pins_algorithm_bank_and_tie_rule(
    hqnp_fixture: HQNPFixture,
) -> None:
    run_hqnp_partition(**hqnp_fixture.run_kwargs)
    manifest = json.loads((hqnp_fixture.output_dir / "projection_manifest.json").read_text())
    entry = manifest["projections"][0]
    for key in ("algorithm", "dimension", "seed_index", "derived_seed",
                "bank_sha256", "bank_dtype", "bank_shape", "tie_rule", "bit_order"):
        assert key in entry, key
    assert {p["algorithm"] for p in manifest["projections"]} == {
        "gaussian_sign_v1", "rademacher_float_sign_v1"
    }


def test_no_signal_queries_are_excluded_from_geometry_and_recorded(
    hqnp_fixture: HQNPFixture,
) -> None:
    result = run_hqnp_partition(**hqnp_fixture.run_kwargs)
    assert result["no_signal_query_count"] == 1
    assert result["valid_query_count"] == PARTITION_SIZES["development"] - 1
    controls = json.loads((hqnp_fixture.output_dir / "no_signal_controls.json").read_text())
    assert controls["routed_into_candidate_search"] == 0


def test_nondeterministic_encoding_is_a_hard_failure(
    hqnp_fixture: HQNPFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = hqnp_fixture.encoder

    class Drifting:
        def __init__(self) -> None:
            self.calls = 0

        def encode(self, texts, *, batch_size: int = 32):
            self.calls += 1
            out = base.encode(texts, batch_size=batch_size).copy()
            # Perturb the LAST row: row 0 of the query batch is the no-signal
            # control (all zeros), and reversing zeros is a no-op.
            if self.calls == 3 and len(out):
                out[-1] = out[-1] + 1.0
            return out

    monkeypatch.setattr(hqnp_run, "load_ettin_fp32", lambda snapshot, **kw: Drifting())
    with pytest.raises(ValueError, match="repeat"):
        run_hqnp_partition(**hqnp_fixture.run_kwargs)


def test_no_artifact_leaks_private_corpus_text(hqnp_fixture: HQNPFixture) -> None:
    run_hqnp_partition(**hqnp_fixture.run_kwargs)
    for path in sorted(hqnp_fixture.output_dir.rglob("*")):
        if path.is_file():
            blob = path.read_text(encoding="utf-8", errors="replace")
            for secret in (SECRET_TEXT, SECRET_QUERY):
                assert secret not in blob, f"{path.name} leaked {secret}"


def test_runner_refuses_to_overwrite_an_existing_run(hqnp_fixture: HQNPFixture) -> None:
    run_hqnp_partition(**hqnp_fixture.run_kwargs)
    with pytest.raises(FileExistsError):
        run_hqnp_partition(**hqnp_fixture.run_kwargs)


def test_no_signal_is_decided_by_corpus_metadata_not_by_a_zero_latent(
    hqnp_fixture: HQNPFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ettin maps a whitespace query to a NON-zero vector.

    Detecting no-signal by all-zero latent therefore lets metadata-declared
    controls into the geometry surface, which preregistration 7.5 forbids. The
    declaration in the corpus is authoritative; the zero-latent check is only an
    additional safeguard.
    """
    base = hqnp_fixture.encoder
    rng = np.random.default_rng(2)
    filler = rng.standard_normal(LATENT_DIM)
    filler /= np.linalg.norm(filler)

    class NonZeroWhitespace:
        def encode(self, texts, *, batch_size: int = 32):
            out = base.encode(texts, batch_size=batch_size).copy()
            for row, text in enumerate(texts):
                if not text.strip():
                    out[row] = filler.astype(np.float32)
            return out

    monkeypatch.setattr(hqnp_run, "load_ettin_fp32", lambda snapshot, **kw: NonZeroWhitespace())
    result = run_hqnp_partition(**hqnp_fixture.run_kwargs)
    assert result["no_signal_query_count"] == 1
    assert result["valid_query_count"] == PARTITION_SIZES["development"] - 1
    controls = json.loads((hqnp_fixture.output_dir / "no_signal_controls.json").read_text())
    assert controls["routed_into_candidate_search"] == 0

"""Program I P1-A Task 1 — the private working-file contract: every Program-I corpus artifact
lives under the ignored ``program1_private/`` workspace, and JSONL loading is STRICT (a blank or
non-object row is a refusal, never a skipped line). Model-blind: nothing here imports or runs a
semantic-retina encoder."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.memory_organ.program1.corpus_pipeline import load_jsonl_objects, require_private_path


def test_require_private_path_refuses_paths_outside_program1_private(tmp_path: Path):
    with pytest.raises(ValueError, match="program1_private"):
        require_private_path(tmp_path / "allocation.jsonl")


def test_require_private_path_accepts_a_private_workspace_path(tmp_path: Path):
    private = tmp_path / "program1_private" / "allocation_v1"
    private.mkdir(parents=True)
    target = private / "allocation.jsonl"
    target.write_text("")
    assert require_private_path(target) == target.resolve()


def test_load_jsonl_objects_rejects_blank_or_non_object_rows(tmp_path: Path):
    private = tmp_path / "program1_private"
    private.mkdir()
    path = private / "bad.jsonl"
    path.write_text('{"x":1}\n\n')
    with pytest.raises(ValueError, match="blank JSONL row"):
        load_jsonl_objects(path)
    path.write_text('{"x":1}\n[1,2]\n')
    with pytest.raises(ValueError, match="not an object"):
        load_jsonl_objects(path)


def test_load_jsonl_objects_reads_every_row_in_order(tmp_path: Path):
    private = tmp_path / "program1_private"
    private.mkdir()
    path = private / "rows.jsonl"
    path.write_text("".join(json.dumps({"candidate_id": f"c{i}"}) + "\n" for i in range(5)))
    rows = load_jsonl_objects(path)
    assert [row["candidate_id"] for row in rows] == [f"c{i}" for i in range(5)]
    # a file with no trailing newline is still complete
    path.write_text('{"candidate_id":"c0"}\n{"candidate_id":"c1"}')
    assert [row["candidate_id"] for row in load_jsonl_objects(path)] == ["c0", "c1"]


def test_load_jsonl_objects_refuses_a_public_path_before_reading(tmp_path: Path):
    outside = tmp_path / "public.jsonl"
    outside.write_text('{"x":1}\n')
    with pytest.raises(ValueError, match="program1_private"):
        load_jsonl_objects(outside)


# ── P1-A Task 2: full-size assembly from validated author output ──────────────
# The fixture drives the REAL landed modules end-to-end at the operator-locked size
# (allocate_sources → build_authoring_jobs → 3,250 validated queries → assemble/validate),
# so no production constant is weakened or monkeypatched anywhere in this file.

from benchmarks.memory_organ.program1.allocation import allocate_sources          # noqa: E402
from benchmarks.memory_organ.program1.authoring import build_authoring_jobs       # noqa: E402
from benchmarks.memory_organ.program1.corpus_pipeline import assemble_private_corpus   # noqa: E402
from benchmarks.memory_organ.program1.query_author import content_tokens          # noqa: E402

_TEMPORAL_ROLE_WORD = {"earlier": "earlier", "historical": "historical", "later": "later", "current": "current"}
_FIXTURE_TOPICS = ('scorer planes', 'lease namespace', 'coverage seal', 'publication lineage', 'hop engine', 'coordinate registry', 'outbox cursor', 'generation manifest', 'reservation block', 'retina codes', 'landed frontier', 'quarantine tree', 'dirty marker')


def _pool() -> list[dict]:
    """The source-mix pool shape (3000 omega / 1550 public / 2950 synthetic, after the 2026-08-18 shift) used by
    the landed allocation tests — it yields the exact 7,500-row allocation and 3,250 author jobs."""
    rows: list[dict] = []
    for source_class, count in (("omega", 3000), ("public", 1550), ("synthetic", 2950)):
        for index in range(count):
            provenance: dict = {"ordinal": index}
            if source_class == "public" and index < 198:
                provenance.update({
                    "kind": "longmemeval_message", "question_type": "temporal-reasoning",
                    "question_id": f"temporal-{index // 2:03d}", "session_id": f"session-{index // 2:03d}",
                    "session_date": f"2026-01-{(index % 28) + 1:02d}", "turn_index": index % 2,
                    "role": "user", "has_answer": True,
                })
            if source_class == "omega" and index < 52:
                provenance.update({"kind": "omega_research_paragraph",
                                   "logical_path": f"research/near-{index // 2:03d}.md", "paragraph_index": index % 2})
            if source_class == "omega" and index < 2:
                provenance.update({"logical_path": "architecture/currentness.md", "paragraph_index": index})
            text = (
                "The legacy path is retired and superseded by the newer architecture."
                if source_class == "omega" and index == 0
                else "The current active replacement is now the authoritative architecture."
                if source_class == "omega" and index == 1
                else f"{source_class} source {index} documents the {_FIXTURE_TOPICS[index % len(_FIXTURE_TOPICS)]} behaviour"
                if source_class == "omega" and index < 52
                else f"{source_class} source {index}"
            )
            rows.append({
                "source_id": f"{source_class}:{index:04d}", "source_class": source_class,
                "record": {"title": "", "summary": "", "text": text}, "provenance": provenance,
            })
    return rows


def _authored_query_for(job: dict) -> str:
    """One VALID query per job under the landed query_author rules (never a candidate copy)."""
    stratum = str(job["stratum"])
    candidate_text = str(job.get("candidate_text") or "")
    hard = str(job.get("hard_negative_text") or "")
    if stratum == "zero_token_overlap_paraphrase":
        return "Zebra quilt vortex jargon"                                  # shares no content token with the pool text
    # pick a RAW word from the candidate whose stem is genuinely distinguishing — inserting a stem
    # into prose would re-stem to a different token and fail the landed validator
    unique = content_tokens(candidate_text) - content_tokens(hard)
    words = [w.strip(".,:;!?()[]{}\"'`") for w in candidate_text.split()]
    detail = next((w for w in words if w and content_tokens(w) and content_tokens(w) <= unique), "")
    if not detail:
        own = content_tokens(candidate_text)
        detail = next((w for w in words if w and content_tokens(w) and content_tokens(w) <= own), "note")
    if stratum == "temporal_update":
        word = _TEMPORAL_ROLE_WORD[str(job.get("contrast_role") or "current")]
        return f"Which note is the {word} one about {detail}?"[:120]
    if stratum == "adversarial_semantic_near_neighbor":
        return f"Which note is specifically about {detail}?"[:120]
    return f"Which note covers {detail}?"[:120]


@pytest.fixture(scope="module")
def _allocation_and_jobs() -> tuple[list[dict], list[dict]]:
    allocation = allocate_sources(_pool())
    return allocation, build_authoring_jobs(allocation)


def _private_inputs(tmp_path: Path, allocation: list[dict], jobs: list[dict], *, results: list[dict] | None = None) -> dict:
    private = tmp_path / "program1_private"
    private.mkdir(parents=True, exist_ok=True)
    rows = results if results is not None else [
        {"candidate_id": job["candidate_id"], "query": _authored_query_for(job)} for job in jobs
    ]
    paths = {
        "allocation_path": private / "allocation.jsonl",
        "author_jobs_path": private / "author_jobs.jsonl",
        "author_results_path": private / "authored_queries.jsonl",
        "candidates_path": private / "candidates.jsonl",
        "cases_path": private / "cases.jsonl",
    }
    paths["allocation_path"].write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in allocation))
    paths["author_jobs_path"].write_text("".join(json.dumps(dict(j, expected_output_fields=list(j["expected_output_fields"])), sort_keys=True) + "\n" for j in jobs))
    paths["author_results_path"].write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    return paths


def test_assemble_refuses_incomplete_author_results(tmp_path, _allocation_and_jobs):
    allocation, jobs = _allocation_and_jobs
    partial = [{"candidate_id": job["candidate_id"], "query": _authored_query_for(job)} for job in jobs[:-1]]
    paths = _private_inputs(tmp_path, allocation, jobs, results=partial)
    with pytest.raises(ValueError, match="2550"):
        assemble_private_corpus(**paths)
    assert not paths["candidates_path"].exists() and not paths["cases_path"].exists()   # nothing written


def test_assemble_writes_exact_validated_counts(tmp_path, _allocation_and_jobs):
    allocation, jobs = _allocation_and_jobs
    paths = _private_inputs(tmp_path, allocation, jobs)
    report = assemble_private_corpus(**paths)
    assert report["candidate_count"] == 7500
    assert report["case_count"] == 7500
    assert report["target_bearing_cases"] == 7000
    assert report["no_signal_cases"] == 500
    assert report["distractor_only_candidates"] == 500
    assert report["candidate_counts_by_partition"] == {"development": 4500, "validation": 1500, "locked_test": 1500}
    assert report["candidate_counts_by_source"] == {"omega": 3000, "public": 1550, "synthetic": 2950}
    assert report["authored_jobs"] == report["validated_queries"] == 2550
    candidates = load_jsonl_objects(paths["candidates_path"])
    cases = load_jsonl_objects(paths["cases_path"])
    assert len(candidates) == len(cases) == 7500
    assert [row["candidate_id"] for row in candidates] == sorted(row["candidate_id"] for row in candidates)
    assert [row["case_id"] for row in cases] == sorted(row["case_id"] for row in cases)


def test_assembly_is_byte_identical_across_runs(tmp_path, _allocation_and_jobs):
    allocation, jobs = _allocation_and_jobs
    first = _private_inputs(tmp_path / "run-a", allocation, jobs)
    second = _private_inputs(tmp_path / "run-b", allocation, jobs)
    assemble_private_corpus(**first)
    assemble_private_corpus(**second)
    assert first["candidates_path"].read_bytes() == second["candidates_path"].read_bytes()
    assert first["cases_path"].read_bytes() == second["cases_path"].read_bytes()


def test_assemble_writes_nothing_when_the_corpus_fails_validation(tmp_path, _allocation_and_jobs):
    allocation, jobs = _allocation_and_jobs
    broken = [dict(row) for row in allocation]
    broken[0]["partition"] = "validation" if broken[0]["partition"] != "validation" else "development"   # breaks the split law
    paths = _private_inputs(tmp_path, broken, jobs)
    with pytest.raises(Exception):
        assemble_private_corpus(**paths)
    assert not paths["candidates_path"].exists() and not paths["cases_path"].exists()


# ── P1-A Task 3: bind the seal to the committed preregistration + A'-full identity ────────────

from benchmarks.memory_organ.program1.corpus_pipeline import (   # noqa: E402
    APRIME_COMPILER_VERSION,
    APRIME_GRAPH_SNAPSHOT_HASH,
    APRIME_SOURCE_CODE_SHA,
    seal_private_corpus,
)

_REPO_COMMIT = "0" * 40


def _aprime_manifest(tmp_path: Path, **overrides) -> Path:
    payload = {
        "source_code_sha": APRIME_SOURCE_CODE_SHA,
        "compiler_version": APRIME_COMPILER_VERSION,
        "graph_snapshot_hash": APRIME_GRAPH_SNAPSHOT_HASH,
        "memory_edge_schema_version": "omega.memory_edge.v1",
        "records_total": 115,
    }
    payload.update(overrides)
    path = tmp_path / "aprime_manifest.json"
    path.write_text(json.dumps(payload, sort_keys=True))
    return path


def _assembled(tmp_path: Path, allocation, jobs) -> dict:
    paths = _private_inputs(tmp_path, allocation, jobs)
    assemble_private_corpus(**paths)
    return paths


def test_seal_refuses_wrong_repo_commit(tmp_path, _allocation_and_jobs):
    allocation, jobs = _allocation_and_jobs
    paths = _assembled(tmp_path, allocation, jobs)
    out = tmp_path / "corpus_seal_v1"
    with pytest.raises(ValueError, match="repo commit"):
        seal_private_corpus(candidates_path=paths["candidates_path"], cases_path=paths["cases_path"],
                            aprime_manifest_path=_aprime_manifest(tmp_path), output_dir=out, repo_commit="deadbeef")
    assert not out.exists() or not any(out.iterdir())          # refused BEFORE anything was sealed


@pytest.mark.parametrize("field,value", [
    ("graph_snapshot_hash", "f" * 64),
    ("source_code_sha", "1" * 40),
    ("compiler_version", "omega.memory_compiler.v7"),
])
def test_seal_refuses_a_wrong_aprime_full_identity(tmp_path, _allocation_and_jobs, field, value):
    allocation, jobs = _allocation_and_jobs
    paths = _assembled(tmp_path, allocation, jobs)
    out = tmp_path / f"seal-{field}"
    with pytest.raises(ValueError, match="A'-full"):
        seal_private_corpus(candidates_path=paths["candidates_path"], cases_path=paths["cases_path"],
                            aprime_manifest_path=_aprime_manifest(tmp_path, **{field: value}),
                            output_dir=out, repo_commit=_REPO_COMMIT)
    assert not out.exists() or not any(out.iterdir())
    assert not (out / "SEALED.json").exists()


def test_seal_writes_exactly_the_four_committed_artifacts_and_no_private_text(tmp_path, _allocation_and_jobs):
    allocation, jobs = _allocation_and_jobs
    paths = _assembled(tmp_path, allocation, jobs)
    out = tmp_path / "corpus_seal_v1"
    report = seal_private_corpus(candidates_path=paths["candidates_path"], cases_path=paths["cases_path"],
                                 aprime_manifest_path=_aprime_manifest(tmp_path), output_dir=out, repo_commit=_REPO_COMMIT)
    assert sorted(p.name for p in out.iterdir()) == [
        "SEALED.json", "corpus_manifest.json", "corpus_manifest.sha256", "split_manifest.json",
    ]
    assert report["sealed"] is True and report["repo_commit"] == _REPO_COMMIT
    for key in ("corpus_manifest_sha256", "canonical_text_set_sha256", "query_set_sha256", "split_assignment_sha256"):
        assert len(report[key]) == 64
    manifest = json.loads((out / "corpus_manifest.json").read_text())
    assert manifest["repo_commit"] == _REPO_COMMIT and manifest["model_blind_required"] is True
    assert manifest["aprime_full"]["graph_snapshot_hash"] == APRIME_GRAPH_SNAPSHOT_HASH
    assert manifest["candidate_count"] == manifest["case_count"] == 7500
    # committed bytes carry hashes/counts only — never private candidate text or an authored query
    sealed_bytes = b"".join(p.read_bytes() for p in sorted(out.iterdir()))
    candidates = load_jsonl_objects(paths["candidates_path"])
    cases = load_jsonl_objects(paths["cases_path"])
    for row in candidates[:50]:
        text = str(row["record"].get("text") or "")
        assert text and text.encode("utf-8") not in sealed_bytes
    for row in cases[:50]:
        query = str(row.get("query") or "").strip()
        if len(query) >= 8:                                     # no_signal queries are deliberate whitespace controls
            assert query.encode("utf-8") not in sealed_bytes
    for row in (candidates[0], cases[0]):
        assert json.dumps(row, sort_keys=True).encode("utf-8") not in sealed_bytes


def test_seal_refuses_to_overwrite_a_non_empty_seal_directory(tmp_path, _allocation_and_jobs):
    allocation, jobs = _allocation_and_jobs
    paths = _assembled(tmp_path, allocation, jobs)
    out = tmp_path / "corpus_seal_v1"
    out.mkdir()
    (out / "SEALED.json").write_text("{}")
    with pytest.raises(FileExistsError):
        seal_private_corpus(candidates_path=paths["candidates_path"], cases_path=paths["cases_path"],
                            aprime_manifest_path=_aprime_manifest(tmp_path), output_dir=out, repo_commit=_REPO_COMMIT)
    assert (out / "SEALED.json").read_text() == "{}"           # the existing seal is untouched


# ── P1-A Task 4: the reproducible CLI (assemble / seal) + deterministic shard merge ──────────

from benchmarks.memory_organ.program1.corpus_pipeline import main, merge_author_result_shards  # noqa: E402


def _shard_files(tmp_path: Path, jobs: list[dict], shards: int = 4) -> tuple[list[Path], Path]:
    private = tmp_path / "program1_private"
    private.mkdir(parents=True, exist_ok=True)
    ordered = sorted(jobs, key=lambda j: (str(j["partition"]), str(j["stratum"]), str(j["candidate_id"])))
    size = (len(ordered) + shards - 1) // shards
    paths = []
    for index in range(shards):
        chunk = ordered[index * size:(index + 1) * size]
        path = private / f"shard_{index}.jsonl"
        path.write_text("".join(json.dumps({"candidate_id": j["candidate_id"], "query": _authored_query_for(j)}, sort_keys=True) + "\n" for j in chunk))
        paths.append(path)
    return paths, private / "authored_queries.jsonl"


def test_merge_shards_is_deterministic_and_requires_the_complete_job_set(tmp_path, _allocation_and_jobs):
    allocation, jobs = _allocation_and_jobs
    shards, merged = _shard_files(tmp_path, jobs)
    jobs_path = tmp_path / "program1_private" / "author_jobs.jsonl"
    jobs_path.write_text("".join(json.dumps(dict(j, expected_output_fields=list(j["expected_output_fields"])), sort_keys=True) + "\n" for j in jobs))
    report = merge_author_result_shards(shard_paths=shards, author_jobs_path=jobs_path, output_path=merged)
    assert report["validated_queries"] == 2550 and report["shards"] == 4
    rows = load_jsonl_objects(merged)
    assert len(rows) == 2550
    assert [r["candidate_id"] for r in rows] == sorted(r["candidate_id"] for r in rows)   # canonical order
    first = merged.read_bytes()
    merge_author_result_shards(shard_paths=list(reversed(shards)), author_jobs_path=jobs_path, output_path=merged)
    assert merged.read_bytes() == first                                                    # shard order cannot change the bytes
    # an incomplete shard set is refused and the previous merge is left intact
    with pytest.raises(ValueError, match="2550"):
        merge_author_result_shards(shard_paths=shards[:2], author_jobs_path=jobs_path, output_path=merged)
    assert merged.read_bytes() == first
    # a duplicate candidate across shards is refused
    shards[1].write_text(shards[0].read_text())
    with pytest.raises(ValueError, match="duplicate"):
        merge_author_result_shards(shard_paths=shards, author_jobs_path=jobs_path, output_path=merged)


def test_cli_assemble_then_seal_round_trip(tmp_path, _allocation_and_jobs, capsys):
    allocation, jobs = _allocation_and_jobs
    paths = _private_inputs(tmp_path, allocation, jobs)
    rc = main([
        "assemble",
        "--allocation", str(paths["allocation_path"]),
        "--author-jobs", str(paths["author_jobs_path"]),
        "--author-results", str(paths["author_results_path"]),
        "--candidates", str(paths["candidates_path"]),
        "--cases", str(paths["cases_path"]),
    ])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["candidate_count"] == report["case_count"] == 7500 and report["target_bearing_cases"] == 7000
    out = tmp_path / "corpus_seal_v1"
    rc = main([
        "seal",
        "--candidates", str(paths["candidates_path"]),
        "--cases", str(paths["cases_path"]),
        "--aprime-manifest", str(_aprime_manifest(tmp_path)),
        "--output", str(out),
        "--repo-commit", _REPO_COMMIT,
    ])
    assert rc == 0
    seal_report = json.loads(capsys.readouterr().out)
    assert seal_report["sealed"] is True and seal_report["repo_commit"] == _REPO_COMMIT
    assert sorted(p.name for p in out.iterdir()) == ["SEALED.json", "corpus_manifest.json", "corpus_manifest.sha256", "split_manifest.json"]


def test_cli_refuses_a_bad_repo_commit_with_a_nonzero_exit(tmp_path, _allocation_and_jobs, capsys):
    allocation, jobs = _allocation_and_jobs
    paths = _private_inputs(tmp_path, allocation, jobs)
    main(["assemble", "--allocation", str(paths["allocation_path"]), "--author-jobs", str(paths["author_jobs_path"]),
          "--author-results", str(paths["author_results_path"]), "--candidates", str(paths["candidates_path"]),
          "--cases", str(paths["cases_path"])])
    capsys.readouterr()
    out = tmp_path / "seal_bad"
    rc = main(["seal", "--candidates", str(paths["candidates_path"]), "--cases", str(paths["cases_path"]),
               "--aprime-manifest", str(_aprime_manifest(tmp_path)), "--output", str(out), "--repo-commit", "deadbeef"])
    assert rc == 2 and "repo commit" in capsys.readouterr().err
    assert not out.exists() or not any(out.iterdir())

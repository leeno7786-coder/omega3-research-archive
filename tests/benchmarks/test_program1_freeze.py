from __future__ import annotations

import hashlib
import json

from benchmarks.memory_organ.program1.contracts import (
    HARD_COMPARATOR_STRATA,
    NO_SIGNAL_STRATUM,
    PARTITIONS,
    STRATA,
    UNRELATED_COMPARATOR_STRATA,
)
from benchmarks.memory_organ.program1.freeze import freeze_corpus


def _source_for(index: int) -> str:
    if index < 3000:
        return "omega"
    if index < 4550:
        return "public"
    return "synthetic"


def _corpus() -> tuple[list[dict], list[dict]]:
    candidates: list[dict] = []
    cases: list[dict] = []
    global_index = 0
    per_partition = {"development": 300, "validation": 100, "locked_test": 100}
    for stratum in STRATA:
        for partition in PARTITIONS:
            for local_index in range(per_partition[partition]):
                token = f"{partition}:{stratum}:{local_index}"
                cid = f"cand:{global_index:04d}"
                family = f"sem:{token}"
                candidates.append({
                    "candidate_id": cid,
                    "partition": partition,
                    "source_class": _source_for(global_index),
                    "semantic_family_id": family,
                    "fact_family_id": f"fact:{token}",
                    "source_memory_id": f"src:{token}",
                    "template_family_id": f"template:{token}",
                    "record": {"title": f"T {global_index}", "summary": f"S {global_index}", "text": ""},
                    "provenance": {"source": "test"},
                })
                no_signal = stratum == NO_SIGNAL_STRATUM
                cases.append({
                    "case_id": f"case:{global_index:04d}",
                    "partition": partition,
                    "stratum": stratum,
                    "query": "" if no_signal else f"Q {global_index}",
                    "primary_target_id": None if no_signal else cid,
                    "hard_negative_ids": [],
                    "unrelated_negative_ids": [],
                    "semantic_family_id": family,
                    "fact_family_id": f"fact:{token}",
                    "source_memory_id": f"src:{token}",
                    "template_family_id": f"template:{token}",
                    "expected_relation": "no_signal" if no_signal else "target",
                    "provenance": {"source": "test"},
                })
                global_index += 1

    by_partition = {partition: [] for partition in PARTITIONS}
    for candidate in candidates:
        by_partition[str(candidate["partition"])].append(str(candidate["candidate_id"]))
    for case in cases:
        if case["stratum"] == NO_SIGNAL_STRATUM:
            continue
        pool = by_partition[str(case["partition"])]
        target = str(case["primary_target_id"])
        index = pool.index(target)
        comparator = pool[(index + 1) % len(pool)]
        if case["stratum"] in UNRELATED_COMPARATOR_STRATA:
            case["unrelated_negative_ids"] = [comparator]
        if case["stratum"] in HARD_COMPARATOR_STRATA:
            case["hard_negative_ids"] = [comparator]
    return candidates, cases


def _write_jsonl(path, rows) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def test_freeze_writes_self_contained_pre_model_manifests(tmp_path) -> None:
    candidates, cases = _corpus()
    candidates_path = tmp_path / "candidates.jsonl"
    cases_path = tmp_path / "cases.jsonl"
    _write_jsonl(candidates_path, candidates)
    _write_jsonl(cases_path, cases)

    aprime = tmp_path / "aprime.json"
    aprime.write_text(json.dumps({
        "source_code_sha": "468c5771",
        "compiler_version": "omega.memory_compiler.v6",
        "graph_snapshot_hash": "graph-hash",
    }, sort_keys=True))
    output = tmp_path / "sealed"
    report = freeze_corpus(
        candidates_path=candidates_path,
        cases_path=cases_path,
        aprime_manifest_path=aprime,
        output_dir=output,
        repo_commit="b58ede1a",
    )

    assert report["sealed"] is True
    assert (output / "corpus_manifest.json").is_file()
    assert (output / "corpus_manifest.sha256").is_file()
    assert (output / "split_manifest.json").is_file()
    assert (output / "SEALED.json").is_file()

    manifest = json.loads((output / "corpus_manifest.json").read_text())
    assert manifest["schema_version"] == "omega.semantic_retina.program1.corpus.v1"
    assert manifest["renderer_version"] == "omega.semantic_retina.render.v1"
    assert manifest["model_blind_required"] is True
    assert manifest["candidate_count"] == 7500
    assert manifest["case_count"] == 7500
    assert manifest["aprime_full"]["graph_snapshot_hash"] == "graph-hash"
    assert len(manifest["canonical_text_set_sha256"]) == 64
    assert len(manifest["query_set_sha256"]) == 64
    assert len(manifest["split_assignment_sha256"]) == 64

    raw = (output / "corpus_manifest.json").read_bytes()
    assert (output / "corpus_manifest.sha256").read_text().strip() == hashlib.sha256(raw).hexdigest()


def test_freeze_identity_is_stable_when_input_row_order_changes(tmp_path) -> None:
    candidates, cases = _corpus()
    a_candidates, a_cases = tmp_path / "a-c.jsonl", tmp_path / "a-q.jsonl"
    b_candidates, b_cases = tmp_path / "b-c.jsonl", tmp_path / "b-q.jsonl"
    _write_jsonl(a_candidates, candidates); _write_jsonl(a_cases, cases)
    _write_jsonl(b_candidates, list(reversed(candidates))); _write_jsonl(b_cases, list(reversed(cases)))
    aprime = tmp_path / "aprime.json"; aprime.write_text('{"graph_snapshot_hash":"g"}')
    one = freeze_corpus(candidates_path=a_candidates, cases_path=a_cases, aprime_manifest_path=aprime,
                        output_dir=tmp_path / "one", repo_commit="x")
    two = freeze_corpus(candidates_path=b_candidates, cases_path=b_cases, aprime_manifest_path=aprime,
                        output_dir=tmp_path / "two", repo_commit="x")
    assert one["canonical_text_set_sha256"] == two["canonical_text_set_sha256"]
    assert one["query_set_sha256"] == two["query_set_sha256"]
    assert one["split_assignment_sha256"] == two["split_assignment_sha256"]

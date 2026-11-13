from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.memory_organ.program1.query_author_runner import (
    AuthoringRunError,
    deterministic_author_seed,
    load_validated_results,
    run_authoring,
)


def _job(candidate_id: str, *, stratum: str = "ordinary_synonyms") -> dict:
    job = {
        "candidate_id": candidate_id,
        "partition": "development",
        "stratum": stratum,
        "candidate_text": f"Canonical memory text for {candidate_id} describes a stable operational fact.",
        "contrast_role": None,
        "hard_negative_text": None,
        "expected_output_fields": ("query",),
    }
    if stratum == "zero_token_overlap_paraphrase":
        job["candidate_text"] = "Observed Saturn through the Dobsonian"
        job["forbidden_content_tokens"] = ["observ", "saturn", "dobsonian"]
    return job


def _envelope(items: list[dict]) -> dict:
    return {"choices": [{"message": {"content": json.dumps({"items": items})}}]}


def _payload_jobs(payload: dict) -> list[dict]:
    user = payload["messages"][1]["content"]
    return json.loads(user.split("\n", 1)[1])


def test_deterministic_seed_is_order_independent_and_attempt_sensitive() -> None:
    assert deterministic_author_seed(["b", "a"], attempt=0) == deterministic_author_seed(["a", "b"], attempt=0)
    assert deterministic_author_seed(["a", "b"], attempt=0) != deterministic_author_seed(["a", "b"], attempt=1)


def test_runner_persists_only_valid_results_and_resumes(tmp_path: Path) -> None:
    jobs = [_job("c1"), _job("c2")]
    calls: list[list[str]] = []

    def requester(payload: dict) -> dict:
        batch = _payload_jobs(payload)
        id_by_text = {job["candidate_text"]: job["candidate_id"] for job in jobs}
        ids = [id_by_text[row["A"]] for row in batch]
        calls.append(ids)
        return _envelope([
            {"i": row["i"], "query": f"What fact applies to {candidate_id}?"}
            for row, candidate_id in zip(batch, ids, strict=True)
        ])

    results_path = tmp_path / "results.jsonl"
    report = run_authoring(jobs, results_path=results_path, requester=requester, batch_size=2)
    assert report["completed"] == 2
    assert calls == [["c1", "c2"]]
    saved = load_validated_results(results_path, jobs)
    assert set(saved) == {"c1", "c2"}

    calls.clear()
    resumed = run_authoring(jobs, results_path=results_path, requester=requester, batch_size=2)
    assert resumed["completed"] == 2 and resumed["resumed"] == 2
    assert calls == []


def test_runner_splits_bad_batch_then_retries_invalid_single_with_feedback(tmp_path: Path) -> None:
    jobs = [
        _job("c1", stratum="zero_token_overlap_paraphrase"),
        _job("z1", stratum="zero_token_overlap_paraphrase"),
    ]
    jobs[0]["candidate_text"] = "Recorded Jupiter with the refractor"
    jobs[0]["forbidden_content_tokens"] = ["record", "jupiter", "refractor"]
    calls: list[tuple[list[str], list[str]]] = []

    def requester(payload: dict) -> dict:
        batch = _payload_jobs(payload)
        id_by_text = {job["candidate_text"]: job["candidate_id"] for job in jobs}
        ids = [id_by_text[row["A"]] for row in batch]
        feedback = [str(row.get("retry_feedback") or "") for row in batch]
        calls.append((ids, feedback))
        if len(batch) > 1:
            return _envelope([{"i": 0, "query": "What stable fact applies?"}])
        row = batch[0]
        candidate_id = id_by_text[row["A"]]
        if candidate_id == "z1" and not row.get("retry_feedback"):
            return _envelope([{"i": row["i"], "query": "Which Dobsonian observed Saturn?"}])
        if candidate_id == "z1":
            return _envelope([{"i": row["i"], "query": "Which telescope did I use?"}])
        return _envelope([{"i": row["i"], "query": "What stable fact applies?"}])

    report = run_authoring(
        jobs,
        results_path=tmp_path / "results.jsonl",
        requester=requester,
        batch_size=2,
        max_single_retries=2,
    )
    assert report["completed"] == 2
    assert report["batch_splits"] >= 1
    assert report["single_retries"] == 1
    assert any(ids == ["z1"] and feedback[0] for ids, feedback in calls)


def test_existing_results_are_fail_closed_on_malformed_or_duplicate_rows(tmp_path: Path) -> None:
    jobs = [_job("c1")]
    path = tmp_path / "results.jsonl"
    path.write_text('{"candidate_id":"c1","query":"What fact applies?"}\nnot-json\n')
    with pytest.raises(AuthoringRunError, match="malformed result row"):
        load_validated_results(path, jobs)

    path.write_text(
        '{"candidate_id":"c1","query":"What fact applies?"}\n'
        '{"candidate_id":"c1","query":"Which fact applies?"}\n'
    )
    with pytest.raises(AuthoringRunError, match="duplicate result"):
        load_validated_results(path, jobs)


def test_single_retry_exhaustion_raises_and_persists_nothing(tmp_path: Path) -> None:
    job = _job("z1", stratum="zero_token_overlap_paraphrase")

    def requester(payload: dict) -> dict:
        row = _payload_jobs(payload)[0]
        return _envelope([{"i": row["i"], "query": "Which Dobsonian observed Saturn?"}])

    path = tmp_path / "results.jsonl"
    with pytest.raises(AuthoringRunError, match="exhausted retries"):
        run_authoring([job], results_path=path, requester=requester, batch_size=1, max_single_retries=1)
    assert not path.exists() or path.read_text() == ""

"""MRIL run-scoped recall for an active Coding Agent run.

The tool takes host-owned run identities, never operator wording, resolves the exact active
job through ``job_control``, and returns canonical memory IDs, excerpts, source citations and
provenance. Anchor and retrieval failures raise; a real empty recall is a distinct status.
"""

from __future__ import annotations

import pytest

from auto_loop.execute import job_control as jobs
from auto_loop.mril_tools import run_scoped as mril
from memory import run_anchor as ra
from memory import run_scoped_recall as rsr


@pytest.fixture
def running_job(tmp_path):
    workspace = (tmp_path / "ws").resolve()
    (workspace / "proj-a").mkdir(parents=True)
    jobs.open_job("job-a", "cycle-a", "build parser", workspace,
                  project_root=workspace / "proj-a", thread_id="thread-a")
    return workspace


def _record(memory_id="11.06.aaaa0001", **overrides):
    base = dict(
        memory_id=memory_id,
        coord=(11, 6, 0),
        title="Dev strings: proj-a (src/parser.py)",
        excerpt="Verified dev observation: read_files (success)\n# parser module",
        source_citation={"source_id": "src_1", "source_type": "memory_write"},
        source_provenance={"origin_id": "promotion:ep-a", "author": "promotion:ep-a"},
        run_provenance={"job_id": "job-a", "project_root": "P", "thread_id": "thread-a",
                        "instruction_revision": 0},
        score=2.0,
    )
    base.update(overrides)
    return rsr.RunScopedRecord(**base)


def test_active_run_recall_returns_provenance_bearing_mril_envelope(running_job, monkeypatch):
    seen = {}

    def fake_recall(anchor, query, *, allowed_lobes, limit):
        seen.update(anchor=anchor, query=query, allowed_lobes=allowed_lobes, limit=limit)
        return rsr.RunScopedRecall(anchor=anchor, status="verified", records=(_record(),))

    monkeypatch.setattr(mril, "recall_run_scoped", fake_recall)
    out = mril.recall_for_active_run(
        job_id="job-a", thread_id="thread-a", instruction_revision=0,
        operator_update="switch the parser to tabs", allowed_lobes=(8, 11), limit=4,
    )

    anchor = seen["anchor"]
    assert anchor.job_id == "job-a" and anchor.thread_id == "thread-a"
    assert seen["allowed_lobes"] == (8, 11) and seen["limit"] == 4
    # the query is the operator's update plus the host-owned anchors
    assert "switch the parser to tabs" in seen["query"]
    assert "job-a" in seen["query"] and anchor.project_root in seen["query"]

    assert out["kind"] == "run_scoped_recall"
    assert out["status"] == "verified"
    assert out["anchor"] == {
        "job_id": "job-a", "project_root": anchor.project_root,
        "thread_id": "thread-a", "instruction_revision": 0,
    }
    [item] = out["evidence"]
    assert item["memory_id"] == "11.06.aaaa0001"
    assert item["coord"] == [11, 6, 0]
    assert item["excerpt"].startswith("Verified dev observation")
    assert item["source_citation"] == {"source_id": "src_1", "source_type": "memory_write"}
    assert item["source_provenance"]["origin_id"] == "promotion:ep-a"
    assert item["run_provenance"]["job_id"] == "job-a"


def test_real_empty_is_a_status_not_an_error(running_job, monkeypatch):
    monkeypatch.setattr(
        mril, "recall_run_scoped",
        lambda anchor, query, **_: rsr.RunScopedRecall(anchor=anchor, status="empty", records=()),
    )
    out = mril.recall_for_active_run(
        job_id="job-a", thread_id="thread-a", instruction_revision=0,
        operator_update="anything new?", allowed_lobes=(11,), limit=3,
    )
    assert out["status"] == "empty" and out["evidence"] == []


def test_anchor_failures_raise_before_any_recall(running_job, monkeypatch):
    def must_not_run(*_a, **_k):
        raise AssertionError("recall must not run without a resolved anchor")

    monkeypatch.setattr(mril, "recall_run_scoped", must_not_run)
    cases = [
        (dict(job_id="", thread_id="thread-a", instruction_revision=0), ra.RunAnchorMissing),
        (dict(job_id="job-x", thread_id="thread-a", instruction_revision=0), ra.RunAnchorUnknownJob),
        (dict(job_id="job-a", thread_id="thread-z", instruction_revision=0), ra.RunAnchorThreadMismatch),
        (dict(job_id="job-a", thread_id="thread-a", instruction_revision=99), ra.RunAnchorUnknownRevision),
    ]
    for kwargs, error in cases:
        with pytest.raises(error):
            mril.recall_for_active_run(operator_update="use tabs", allowed_lobes=(11,), limit=3, **kwargs)


def test_retrieval_failure_propagates_with_its_cause(running_job, monkeypatch):
    def broken(anchor, query, **_):
        try:
            raise OSError("disk gone")
        except OSError as exc:
            raise rsr.RunScopedRecallUnavailable("canonical read failed") from exc

    monkeypatch.setattr(mril, "recall_run_scoped", broken)
    with pytest.raises(rsr.RunScopedRecallUnavailable) as caught:
        mril.recall_for_active_run(
            job_id="job-a", thread_id="thread-a", instruction_revision=0,
            operator_update="use tabs", allowed_lobes=(11,), limit=3,
        )
    assert isinstance(caught.value.__cause__, OSError)


def test_excerpts_are_bounded_so_no_transcript_is_copied(running_job, monkeypatch):
    long_text = "line of agent transcript\n" * 2000
    monkeypatch.setattr(
        mril, "recall_run_scoped",
        lambda anchor, query, **_: rsr.RunScopedRecall(
            anchor=anchor, status="verified", records=(_record(excerpt=long_text),)),
    )
    out = mril.recall_for_active_run(
        job_id="job-a", thread_id="thread-a", instruction_revision=0,
        operator_update="use tabs", allowed_lobes=(11,), limit=3,
    )
    assert len(out["evidence"][0]["excerpt"]) <= mril.MAX_EXCERPT_CHARS

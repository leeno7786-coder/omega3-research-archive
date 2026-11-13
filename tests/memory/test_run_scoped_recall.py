"""Run-scoped coding/dev recall: provenance written by the run, recalled only for that run.

Contract between the dev-lobe writers (artifact snapshots, tool observations, execution
reports) and the run-scoped reader: the writers stamp the exact job/project/thread/revision
into canonical ``enrichment.run_provenance``; the reader scopes candidates to that exact
identity BEFORE top-k and re-verifies every admitted canonical record afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from auto_loop.execute import job_control as jobs
from auto_loop.execute.brain_skill_executor import ExecutionContext
from cognition_chain import _hold_execution_report_from_fold
from memory import episode_sram
from memory import run_anchor as ra
from memory import run_scoped_recall as rsr
from memory.dev_artifact_intake import hold_dev_artifact_snapshots
from memory.dev_observation_intake import hold_dev_tool_observation
from memory.execution_report import ExecutionReport, current_execution_report, hold_execution_report


pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = (tmp_path / "workspace").resolve()
    for project in ("proj-a", "proj-b"):
        (root / project / "src").mkdir(parents=True)
    monkeypatch.setattr("core.hud_path_guard._operator_workspace_root", lambda: root)
    return root


def _open(job_id, workspace, project, thread):
    return jobs.open_job(
        job_id, f"cycle-{job_id}", f"build {project}", workspace,
        project_root=workspace / project, thread_id=thread,
    )


def _ctx(workspace, job_id, episode_id):
    return ExecutionContext(
        workspace_root=workspace,
        trusted_roots=(workspace,),
        objective="build",
        timeout_s=30,
        episode_id=episode_id,
        job_id=job_id,
        workspace_available=True,
    )


def _observe(workspace, job_id, episode_id, project, marker):
    target = workspace / project / "src" / f"{marker}.py"
    target.write_text(f"# {marker}\n", encoding="utf-8")
    return hold_dev_tool_observation(
        episode_id=episode_id,
        objective="build",
        record={
            "tool_name": "read_files",
            "args": {"paths": [str(target)]},
            "status": "success",
            "output": f"{target}\n# {marker} parser module",
            "error": "",
            "touched_files": [],
        },
        ctx=_ctx(workspace, job_id, episode_id),
    )


# ── writers stamp exact run provenance ────────────────────────────────────────


def test_dev_observation_carries_exact_run_provenance(workspace):
    _open("job-a", workspace, "proj-a", "thread-a")
    episode_sram.open_bucket("ep-obs", pipeline_id=2)
    receipt = _observe(workspace, "job-a", "ep-obs", "proj-a", "alpha")
    assert receipt["status"] == "held", receipt
    held = [item.envelope for item in episode_sram.pending("ep-obs")]
    assert held, "observation must be held"
    for env in held:
        assert env.payload["enrichment"]["run_provenance"] == {
            "job_id": "job-a",
            "project_root": str((workspace / "proj-a").resolve()),
            "thread_id": "thread-a",
            "instruction_revision": 0,
        }
        assert env.payload["run_provenance"] == env.payload["enrichment"]["run_provenance"]


def test_dev_observation_without_a_job_carries_no_run_provenance(workspace):
    episode_sram.open_bucket("ep-chat", pipeline_id=0)
    receipt = _observe(workspace, "", "ep-chat", "proj-a", "beta")
    assert receipt["status"] == "held", receipt
    for item in episode_sram.pending("ep-chat"):
        assert "run_provenance" not in item.envelope.payload["enrichment"]


def test_dev_observation_for_an_unknown_job_fails_with_its_cause(workspace):
    episode_sram.open_bucket("ep-ghost", pipeline_id=2)
    receipt = _observe(workspace, "job-ghost", "ep-ghost", "proj-a", "gamma")
    assert receipt["status"] == "failed"
    assert "job-ghost" in receipt["error"]
    assert episode_sram.pending("ep-ghost") == [] or all(
        "run_provenance" not in item.envelope.payload.get("enrichment", {})
        for item in episode_sram.pending("ep-ghost")
    )


def test_dev_artifact_snapshot_carries_run_provenance_when_a_job_is_named(workspace):
    _open("job-a", workspace, "proj-a", "thread-a")
    (workspace / "proj-a" / "src" / "widget.py").write_text("VALUE = 1\n", encoding="utf-8")
    episode_sram.open_bucket("ep-art", pipeline_id=2)
    refs = hold_dev_artifact_snapshots(
        episode_id="ep-art", objective="build", paths=["proj-a/src/widget.py"], job_id="job-a",
    )
    assert len(refs) == 1
    env = episode_sram.pending("ep-art")[-1].envelope
    assert env.payload["enrichment"]["run_provenance"]["job_id"] == "job-a"
    assert env.payload["enrichment"]["run_provenance"]["project_root"] == str((workspace / "proj-a").resolve())


def test_execution_report_carries_validated_run_provenance(workspace):
    _open("job-a", workspace, "proj-a", "thread-a")
    episode_sram.open_bucket("ep-rep", pipeline_id=2)
    provenance = ra.run_provenance_for_job("job-a")
    hold_execution_report(
        ExecutionReport(
            episode_id="ep-rep", build_task="build parser", pass_gate=("tests pass",),
            did=("changed parser.py",), did_not=(), succeeded=("tests pass",), failed=(),
            run_provenance=provenance,
        )
    )
    env = episode_sram.pending("ep-rep")[-1].envelope
    assert env.payload["enrichment"]["run_provenance"] == provenance
    round_trip = ExecutionReport.from_payload(env.payload)
    assert round_trip.run_provenance == provenance
    with pytest.raises(ValueError):
        ExecutionReport(
            episode_id="ep-rep", build_task="t", pass_gate=("g",), did=(), did_not=(),
            succeeded=(), failed=(), run_provenance={"job_id": "job-a"},
        )


def test_live_fold_execution_report_is_anchored_to_its_host_job(workspace):
    _open("job-a", workspace, "proj-a", "thread-a")
    episode_sram.open_bucket("ep-live-report", pipeline_id=2)
    episode = type("Episode", (), {
        "episode_id": "ep-live-report", "thread_id": "thread-a", "global_": {},
    })()

    _hold_execution_report_from_fold(
        episode,
        {"job_id": "job-a", "summary": "parser updated", "files_changed": []},
        {"global_b": {"objective_complete": False, "more_work_needed": True}},
        build_task="build parser", pass_gate=("parser works",), verification=None,
        original_request="build parser", origin_opened_by="operator_query",
    )

    report = current_execution_report()
    assert report is not None
    assert report.run_provenance == {
        "job_id": "job-a", "project_root": str((workspace / "proj-a").resolve()),
        "thread_id": "thread-a", "instruction_revision": 0,
    }


# ── reader: exact scope before top-k, verification after merge ───────────────


def _commit_two_runs(workspace):
    """Two coding runs on two projects; both committed to canonical memory."""
    _open("job-a", workspace, "proj-a", "thread-a")
    _open("job-b", workspace, "proj-b", "thread-b")
    episode_sram.open_bucket("ep-a", pipeline_id=2)
    assert _observe(workspace, "job-a", "ep-a", "proj-a", "parser")["status"] == "held"
    episode_sram.flush("ep-a")
    episode_sram.open_bucket("ep-b", pipeline_id=2)
    # the unrelated run shares the query's vocabulary on purpose
    assert _observe(workspace, "job-b", "ep-b", "proj-b", "parser")["status"] == "held"
    episode_sram.flush("ep-b")


def test_recall_returns_only_the_active_runs_records_with_provenance(workspace):
    _commit_two_runs(workspace)
    anchor = ra.resolve_run_anchor(job_id="job-a", thread_id="thread-a", instruction_revision=0)

    result = rsr.recall_run_scoped(anchor, "change the parser module", allowed_lobes=(11,), limit=1)

    assert result.status == "verified"
    assert result.records, "the active run's own records must be recalled"
    assert len(result.records) == 1, "top-k applies after the run scope, not before"
    for record in result.records:
        assert record.run_provenance["job_id"] == "job-a"
        assert record.run_provenance["project_root"] == anchor.project_root
        assert record.memory_id and record.coord[0] == 11
        assert record.excerpt
        assert record.source_provenance.get("origin_id")
        assert "proj-b" not in record.excerpt
    wide = rsr.recall_run_scoped(anchor, "change the parser module", allowed_lobes=(11,), limit=50)
    assert all(r.run_provenance["job_id"] == "job-a" for r in wide.records)
    assert {r.memory_id for r in result.records} <= {r.memory_id for r in wide.records}


def test_real_empty_recall_is_empty_not_failure(workspace):
    _commit_two_runs(workspace)
    (workspace / "proj-c").mkdir()
    jobs.open_job("job-c", "cycle-c", "build c", workspace,
                  project_root=workspace / "proj-c", thread_id="thread-c")
    anchor = ra.resolve_run_anchor(job_id="job-c", thread_id="thread-c", instruction_revision=0)
    result = rsr.recall_run_scoped(anchor, "change the parser module", allowed_lobes=(8, 11), limit=5)
    assert result.status == "empty"
    assert result.records == ()


def test_lobe_policy_is_explicit_and_limited_to_coding_and_dev(workspace):
    _open("job-a", workspace, "proj-a", "thread-a")
    anchor = ra.resolve_run_anchor(job_id="job-a", thread_id="thread-a", instruction_revision=0)
    for lobes in ((), None, (1,), (9,), (11, 6), (4, 8)):
        with pytest.raises(rsr.RunScopedLobeDenied):
            rsr.recall_run_scoped(anchor, "anything", allowed_lobes=lobes, limit=5)


def test_unreadable_canonical_record_is_a_retrieval_failure_not_empty(workspace, monkeypatch):
    _commit_two_runs(workspace)
    anchor = ra.resolve_run_anchor(job_id="job-a", thread_id="thread-a", instruction_revision=0)
    from memory.engine import walker

    walker.load_coord_index()
    victim = next(mid for mid, entry in walker.coord_index_snapshot().items() if entry["coord"][0] == 11)
    Path(walker.path_for_id(victim)).write_text("{broken", encoding="utf-8")
    with pytest.raises(rsr.RunScopedRecallUnavailable) as caught:
        rsr.recall_run_scoped(anchor, "parser", allowed_lobes=(11,), limit=5)
    assert caught.value.__cause__ is not None


def test_missing_coord_index_is_a_retrieval_failure(workspace, monkeypatch):
    _open("job-a", workspace, "proj-a", "thread-a")
    anchor = ra.resolve_run_anchor(job_id="job-a", thread_id="thread-a", instruction_revision=0)
    from memory.engine import walker

    def unavailable():
        raise walker.CoordIndexUnavailable("missing", walker.INDEX_PATH)

    monkeypatch.setattr(walker, "load_coord_index", unavailable)
    with pytest.raises(rsr.RunScopedRecallUnavailable) as caught:
        rsr.recall_run_scoped(anchor, "parser", allowed_lobes=(11,), limit=5)
    assert isinstance(caught.value.__cause__, walker.CoordIndexUnavailable)


def test_malformed_run_provenance_on_a_canonical_record_fails_hard(workspace):
    _commit_two_runs(workspace)
    anchor = ra.resolve_run_anchor(job_id="job-a", thread_id="thread-a", instruction_revision=0)
    from memory.engine import walker

    walker.load_coord_index()
    victim = next(mid for mid, entry in walker.coord_index_snapshot().items() if entry["coord"][0] == 11)
    path = Path(walker.path_for_id(victim))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["enrichment"]["run_provenance"] = {"job_id": "job-a"}
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(rsr.RunScopedProvenanceInvalid):
        rsr.recall_run_scoped(anchor, "parser", allowed_lobes=(11,), limit=5)


def test_post_merge_verification_rejects_a_record_changed_after_scoping(workspace, monkeypatch):
    _commit_two_runs(workspace)
    anchor = ra.resolve_run_anchor(job_id="job-a", thread_id="thread-a", instruction_revision=0)
    real_read = rsr._read_canonical
    reads: dict[str, int] = {}

    def swapping_read(memory_id, path):
        reads[memory_id] = reads.get(memory_id, 0) + 1
        record = real_read(memory_id, path)
        prov = (record.get("enrichment") or {}).get("run_provenance") or {}
        if reads[memory_id] > 1 and prov.get("job_id") == "job-a":
            record = json.loads(json.dumps(record))
            record["enrichment"]["run_provenance"]["job_id"] = "job-b"
        return record

    monkeypatch.setattr(rsr, "_read_canonical", swapping_read)
    with pytest.raises(rsr.RunScopedVerificationFailed):
        rsr.recall_run_scoped(anchor, "parser", allowed_lobes=(11,), limit=5)


def test_recall_errors_are_one_family_distinct_from_anchor_errors():
    for cls in (rsr.RunScopedLobeDenied, rsr.RunScopedRecallUnavailable,
                rsr.RunScopedProvenanceInvalid, rsr.RunScopedVerificationFailed):
        assert issubclass(cls, rsr.RunScopedRecallError)
        assert not issubclass(cls, ra.RunAnchorError)

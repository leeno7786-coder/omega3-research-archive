"""Host-resolved Coding Agent run anchors: exact job, project, thread and revision.

The anchor is resolved from ``job_control`` only. Every way it cannot be resolved is a
distinct hard error carrying its cause; nothing is inferred from operator wording and no
sole running job is picked by guess.
"""

from __future__ import annotations

import pytest

from auto_loop.execute import job_control as jobs
from memory import run_anchor as ra


@pytest.fixture
def workspace(tmp_path):
    root = (tmp_path / "workspace").resolve()
    (root / "proj-a").mkdir(parents=True)
    (root / "proj-b").mkdir(parents=True)
    return root


def _open(job_id, workspace, project, *, thread="thread-a", cycle=None):
    return jobs.open_job(
        job_id,
        cycle or f"cycle-{job_id}",
        f"build {project}",
        workspace,
        project_root=workspace / project,
        thread_id=thread,
    )


def test_exact_running_job_resolves_full_anchor(workspace):
    _open("job-a", workspace, "proj-a")
    revision = jobs.request_instruction("job-a", "use tabs", source_cycle_id="op-1", arrival_seq=7)

    anchor = ra.resolve_run_anchor(job_id="job-a", thread_id="thread-a", instruction_revision=revision.revision)

    assert anchor == ra.RunAnchor(
        job_id="job-a",
        project_root=str((workspace / "proj-a").resolve()),
        thread_id="thread-a",
        instruction_revision=7,
    )
    # the original objective (no revision yet) is revision zero
    assert ra.resolve_run_anchor(job_id="job-a", thread_id="thread-a", instruction_revision=0).instruction_revision == 0


def test_missing_job_identity_is_its_own_error_and_never_picks_the_sole_job(workspace):
    _open("job-a", workspace, "proj-a")
    for missing in ("", "   ", None):
        with pytest.raises(ra.RunAnchorMissing):
            ra.resolve_run_anchor(job_id=missing, thread_id="thread-a", instruction_revision=0)
    with pytest.raises(ra.RunAnchorMissing):
        ra.resolve_run_anchor(job_id="job-a", thread_id="", instruction_revision=0)
    with pytest.raises(ra.RunAnchorMissing):
        ra.resolve_run_anchor(job_id="job-a", thread_id="thread-a", instruction_revision=None)


def test_unknown_job_preserves_the_lookup_cause(workspace):
    with pytest.raises(ra.RunAnchorUnknownJob) as caught:
        ra.resolve_run_anchor(job_id="job-nope", thread_id="thread-a", instruction_revision=0)
    assert isinstance(caught.value.__cause__, KeyError)


def test_corrupt_job_record_preserves_the_integrity_cause(workspace):
    _open("job-a", workspace, "proj-a")
    (jobs.jobs_root() / "job-a.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ra.RunAnchorUnreadable) as caught:
        ra.resolve_run_anchor(job_id="job-a", thread_id="thread-a", instruction_revision=0)
    assert isinstance(caught.value.__cause__, jobs.JobRecordInvalid)


def test_non_running_job_is_rejected(workspace):
    _open("job-a", workspace, "proj-a")
    jobs.finish_job("job-a", status="completed", summary="done")
    with pytest.raises(ra.RunAnchorNotRunning):
        ra.resolve_run_anchor(job_id="job-a", thread_id="thread-a", instruction_revision=0)


def test_project_unbound_job_is_rejected(workspace):
    jobs.open_job("job-u", "cycle-u", "build", workspace, thread_id="thread-a")
    with pytest.raises(ra.RunAnchorProjectUnbound):
        ra.resolve_run_anchor(job_id="job-u", thread_id="thread-a", instruction_revision=0)


def test_thread_mismatch_and_unrecorded_thread_are_rejected(workspace):
    _open("job-a", workspace, "proj-a")
    with pytest.raises(ra.RunAnchorThreadMismatch):
        ra.resolve_run_anchor(job_id="job-a", thread_id="thread-other", instruction_revision=0)
    _open("job-n", workspace, "proj-b", thread="")
    with pytest.raises(ra.RunAnchorThreadMismatch):
        ra.resolve_run_anchor(job_id="job-n", thread_id="thread-a", instruction_revision=0)


def test_unknown_or_stop_revision_is_rejected(workspace):
    _open("job-a", workspace, "proj-a")
    jobs.request_instruction("job-a", "rename it", source_cycle_id="op-1", arrival_seq=5)
    stop = jobs.request_stop("job-a", source_cycle_id="op-2", arrival_seq=9)
    for revision in (4, 6, stop.revision, -1, "5", True):
        with pytest.raises(ra.RunAnchorUnknownRevision):
            ra.resolve_run_anchor(job_id="job-a", thread_id="thread-a", instruction_revision=revision)


def test_two_running_jobs_on_one_thread_are_ambiguous(workspace):
    _open("job-a", workspace, "proj-a")
    _open("job-b", workspace, "proj-b")
    with pytest.raises(ra.RunAnchorAmbiguous):
        ra.resolve_run_anchor(job_id="job-a", thread_id="thread-a", instruction_revision=0)


def test_every_anchor_error_is_one_family_distinct_from_empty():
    for cls in (
        ra.RunAnchorMissing,
        ra.RunAnchorUnknownJob,
        ra.RunAnchorUnreadable,
        ra.RunAnchorNotRunning,
        ra.RunAnchorProjectUnbound,
        ra.RunAnchorThreadMismatch,
        ra.RunAnchorUnknownRevision,
        ra.RunAnchorAmbiguous,
    ):
        assert issubclass(cls, ra.RunAnchorError)
    assert len({
        ra.RunAnchorMissing, ra.RunAnchorUnknownJob, ra.RunAnchorUnreadable,
        ra.RunAnchorNotRunning, ra.RunAnchorProjectUnbound, ra.RunAnchorThreadMismatch,
        ra.RunAnchorUnknownRevision, ra.RunAnchorAmbiguous,
    }) == 8


def test_run_provenance_for_job_is_the_writer_side_of_the_anchor(workspace):
    _open("job-a", workspace, "proj-a")
    jobs.request_instruction("job-a", "use tabs", source_cycle_id="op-1", arrival_seq=3)
    jobs.acknowledge_job_controls("job-a", through_revision=3)

    provenance = ra.run_provenance_for_job("job-a")

    assert provenance == {
        "job_id": "job-a",
        "project_root": str((workspace / "proj-a").resolve()),
        "thread_id": "thread-a",
        "instruction_revision": 3,
    }
    with pytest.raises(ra.RunAnchorUnknownJob):
        ra.run_provenance_for_job("job-missing")

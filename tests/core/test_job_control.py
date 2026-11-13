"""Durable operator controls, immutable arrival receipts and exclusive writers."""

from concurrent.futures import ThreadPoolExecutor
import json

import pytest


@pytest.fixture(autouse=True)
def _authorized_operator_tool_state(tmp_path, monkeypatch):
    """Keep writer/control tests independent of live chat permission state."""
    from auto_loop.execute import chat_permissions

    monkeypatch.setattr(chat_permissions, "CHAT_PERMISSION_PATH", tmp_path / "permission-mode.json")
    chat_permissions.set_chat_permission_mode("always-allow")


@pytest.fixture
def job_control(tmp_path, monkeypatch):
    from auto_loop.execute import job_control as module
    from pressure_loop import operator_chat_responses as replies

    monkeypatch.setattr(module, "jobs_root", lambda: tmp_path / "jobs")
    monkeypatch.setattr(replies, "_store_path", lambda: tmp_path / "responses.json")
    replies._reset_cache_for_tests()
    module.reset_for_tests()
    yield module
    module.reset_for_tests()
    replies._reset_cache_for_tests()


def test_arrival_receipts_survive_out_of_order_append_and_reopen(job_control, tmp_path):
    j = job_control
    j.open_job("job-a", "cycle-a", "build", tmp_path)
    late = j.request_stop("job-a", source_cycle_id="c13", arrival_seq=13)
    early = j.request_stop("job-a", source_cycle_id="c12", arrival_seq=12)
    j.reset_for_tests()
    reopened = j.open_job("job-a", "cycle-a", "build", tmp_path)
    assert reopened.revisions == (early, late)
    assert [(r.revision, r.source_cycle_id) for r in j.revisions_after("job-a", 0)] == [(12, "c12"), (13, "c13")]
    assert late.revision == 13


def test_poll_waits_and_acknowledges_only_observed_boundary(job_control, tmp_path, monkeypatch):
    j = job_control
    j.open_job("j", "c", "build", tmp_path)
    j.request_stop("j", source_cycle_id="c2", arrival_seq=2)
    monkeypatch.setattr(j, "earlier_operator_turns_interpreted", lambda seq: False)
    assert j.poll_job_controls([], "j", 0) == j.JobControlPoll(0, False)
    monkeypatch.setattr(j, "earlier_operator_turns_interpreted", lambda seq: True)
    assert j.poll_job_controls([], "j", 0) == j.JobControlPoll(2, True)
    j.acknowledge_job_stop("j", through_revision=2)
    with pytest.raises(ValueError, match="arrival"):
        j.request_stop("j", source_cycle_id="c1", arrival_seq=1)


def test_stop_is_durable_and_does_not_modify_tool_result(job_control, tmp_path):
    j = job_control
    j.open_job("j", "c", "build", tmp_path)
    stop = j.request_stop("j", source_cycle_id="stop", arrival_seq=3)
    messages = [{"role": "tool", "content": "command completed normally"}]
    assert j.poll_job_controls(messages, "j", 0) == j.JobControlPoll(3, True)
    assert stop.kind == "stop"
    assert len(messages) == 1


def test_concurrent_append_dedup_and_conflict(job_control, tmp_path):
    j = job_control
    j.open_job("j", "c", "build", tmp_path)
    def append(seq):
        return j.request_stop("j", source_cycle_id=f"c{seq}", arrival_seq=seq)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, list(range(2, 30)) * 2))
    assert len(j.revisions_after("j", 0)) == 28
    assert append(2) == j.revisions_after("j", 0)[0]
    with pytest.raises(ValueError, match="conflict"):
        j.request_stop("j", source_cycle_id="someone-else", arrival_seq=2)


@pytest.mark.parametrize("name", ["../j", "a/b", "a\\b", "..", "", "a" * 129])
def test_job_path_is_opaque(job_control, tmp_path, name):
    with pytest.raises(ValueError):
        job_control.open_job(name, "c", "build", tmp_path)


def test_unknown_and_malformed_fail_loudly(job_control, tmp_path):
    with pytest.raises(KeyError):
        job_control.request_stop("missing", source_cycle_id="c", arrival_seq=1)
    job_control.open_job("j", "c", "build", tmp_path)
    path = job_control.jobs_root() / "j.json"
    payload = json.loads(path.read_text())
    payload["status"] = "probably-running"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        job_control.get_job("j")


@pytest.mark.parametrize("after_replace", [False, True])
def test_uncertain_publication_reopens_authoritative_disk_and_retry_deduplicates(job_control, tmp_path, monkeypatch, after_replace):
    j = job_control
    j.open_job("j", "c", "build", tmp_path)
    original = j.os.replace
    def uncertain(source, destination):
        if after_replace:
            original(source, destination)
        raise OSError("injected publication interruption")
    monkeypatch.setattr(j.os, "replace", uncertain)
    with pytest.raises(OSError, match="publication"):
        j.request_stop("j", source_cycle_id="r", arrival_seq=2)
    assert j.get_job("j").latest_revision == (2 if after_replace else 0)
    monkeypatch.setattr(j.os, "replace", original)
    receipt = j.request_stop("j", source_cycle_id="r", arrival_seq=2)
    assert j.get_job("j").revisions == (receipt,)


def test_boot_retires_only_unrestorable_interpretation_receipts(job_control):
    from pressure_loop import cognition_queue as queue
    from pressure_loop.contributors import make_contributor
    orphan_a = "9570f4a0-45d8-4459-8b1d-b855bb581f7d"
    orphan_b = "7d3ec2cd-f7ca-41f3-bb96-37a9c723c45a"
    for seq, cycle in ((37, orphan_a), (38, orphan_b)):
        queue.enqueue_input(make_contributor("operator", "operator_query", 1.0,
                            {"cycle_id": cycle, "arrival_seq": seq, "message": "old"}), operator_activity=False)
        carrier = queue.lease_next_input(0)
        queue.acknowledge_input(carrier, 0)
    queue.enqueue_input(make_contributor("operator", "operator_query", 1.0,
                        {"cycle_id": "recoverable", "arrival_seq": 39, "message": "hello"}), operator_activity=False)
    receipts = queue.reconcile_operator_turns_at_boot(set())
    assert {(r["arrival_seq"], r["cycle_id"]) for r in receipts} == {(37, orphan_a), (38, orphan_b)}
    assert all(r["outcome"] == "boot_unrestorable_no_carrier" for r in receipts)
    queue._reset_cache_for_tests()
    assert queue.earlier_operator_turns_interpreted(39)
    assert not queue.earlier_operator_turns_interpreted(40)


def test_boot_stops_orphaned_writers_and_keeps_restored_ones(job_control, tmp_path):
    """Unfinished agent work continues through Omega successors, never a job continuation."""
    j = job_control
    j.open_job("done", "c", "build", tmp_path)
    j.finish_job("done", status="completed", summary="done")
    j.open_job("restored", "r", "build", tmp_path)
    j.open_job("orphan", "o", "build", tmp_path)

    assert [job.job_id for job in j.reconcile_jobs_at_boot({"restored"})] == ["restored"]

    assert (j.get_job("done").status, j.get_job("restored").status, j.get_job("orphan").status) == (
        "completed", "running", "stopped")


def test_finishing_a_job_never_queues_a_continuation(job_control, tmp_path):
    j = job_control
    j.open_job("j", "origin", "build", tmp_path)
    j.request_stop("j", source_cycle_id="stop", arrival_seq=2)

    assert j.finish_job("j", status="completed", summary="done").status == "completed"
    assert j.active_jobs() == ()
    assert not any(hasattr(j, name) for name in ("admit_ready_jobs", "claim_job", "append_revision"))


def test_a_continuation_reclaims_the_job_whose_writer_died_with_the_process(job_control, tmp_path):
    """Boot stops every job whose writer died and clears its owner, and the work continues
    through an Omega successor — whose batch reached ``enter_writer`` first and was refused,
    because the status was settled and the owner gone. It answered "this job already has an
    active writer" and the operator's task stalled (review 2026-09-19, found through T14).
    """
    j = job_control
    j.open_job("j", "bubble", "build", tmp_path, project_root="project")
    j.reconcile_jobs_at_boot(())
    assert j.get_job("j").status == "stopped" and not j.get_job("j").owner_cycle_id
    assert not j.enter_writer("j", "continuation"), "the writer seat alone cannot take it back"

    reclaimed = j.reclaim_writer("j", "continuation")

    assert reclaimed is not None and reclaimed.status == "running"
    assert reclaimed.owner_cycle_id == "continuation", "the continuation owns the seat now"
    assert reclaimed.project_root == j.get_job("j").project_root, "the same project, unchanged"
    assert j.enter_writer("j", "continuation"), "and the batch may write"


def test_a_job_the_operator_stopped_is_never_reclaimed(job_control, tmp_path):
    """The distinction ``reclaim_writer`` exists to make: the HOST stopping a job is recovery,
    the OPERATOR stopping one is authority. Both stops leave the same status behind, so the
    operator's own witness — the stop revision, and the receipt the batch wrote when it
    acknowledged it — is what separates them."""
    j = job_control
    for job_id, acknowledge in (("pending-stop", False), ("acknowledged-stop", True)):
        j.open_job(job_id, "bubble", "build", tmp_path)
        stop = j.request_stop(job_id, source_cycle_id="operator", arrival_seq=4)
        if acknowledge:
            j.acknowledge_job_stop(job_id, through_revision=stop.revision)
        j.finish_job(job_id, status="stopped", summary="stopped at the operator's request")
        j.reconcile_jobs_at_boot(())

        assert j.reclaim_writer(job_id, "continuation") is None, f"{job_id} is not recoverable work"
        assert j.get_job(job_id).status == "stopped", "and it stays stopped"


def test_reclaim_reports_project_contention_as_a_conflict_that_names_its_owner(job_control, tmp_path):
    """The project is UNOWNED while a job is stopped, so another build can take it. That is
    ordinary contention, not corruption: it is raised as its own type, carrying the owner, so
    the caller can refuse and say whose work is in the way. Both records stay as they were —
    nothing is written before the check (review 2026-09-19)."""
    j = job_control
    j.open_job("j", "bubble", "build", tmp_path, project_root="project")
    j.reconcile_jobs_at_boot(())
    competing = j.open_job("competing", "other", "another build", tmp_path, project_root="project")

    with pytest.raises(j.ProjectClaimConflict) as raised:
        j.reclaim_writer("j", "continuation")

    assert raised.value.owner_job_id == "competing", "the owner is on the exception, not only in prose"
    assert "competing" in str(raised.value)
    assert isinstance(raised.value, ValueError), "still the type the tool handlers already read"
    assert not isinstance(raised.value, j.JobRecordInvalid), "and never confused with corruption"
    after = j.get_job("j")
    assert (after.status, after.owner_cycle_id) == ("stopped", ""), "the stopped job is untouched"
    assert j.get_job("competing") == competing, "and so is the job that owns the project"


@pytest.mark.parametrize("state", ["completed", "failed", "running", "executing", "owned"])
def test_reclaim_refuses_every_job_that_is_not_abandoned_work(job_control, tmp_path, state):
    """Only a job with no live writer, settled by the host, is reclaimable. A finished job is
    reopened by its own turn through ``open_job``, and a job someone still holds is untouched
    — reclaiming one would hand two writers the same workspace."""
    j = job_control
    j.open_job("j", "bubble", "build", tmp_path)
    if state in {"completed", "failed"}:
        j.finish_job("j", status=state, summary=state)
    elif state == "executing":
        assert j.enter_writer("j", "bubble"), "its own batch is running"
        j.reconcile_jobs_at_boot(())          # a host stop landing under a live writer
    elif state == "owned":
        pass                                      # running, owned by its original cycle
    before = j.get_job("j")

    assert j.reclaim_writer("j", "second") is None

    after = j.get_job("j")
    assert (after.status, after.owner_cycle_id) == (before.status, before.owner_cycle_id), \
        "and the record is not touched"


def test_a_finished_job_is_taken_back_by_its_own_chat_turn_for_the_next_batch(job_control, tmp_path):
    """An Omega successor's next tool batch reopens the operator turn's project under the same writer."""
    j = job_control
    first = j.open_job("j", "bubble", "build", tmp_path, project_root="project")
    j.finish_job("j", status="completed", summary="first batch done")

    again = j.open_job("j", "bubble", "build", tmp_path, project_root="project")

    assert again.status == "running" and again.owner_cycle_id == "bubble"
    assert again.project_root == first.project_root
    with pytest.raises(ValueError, match="identity conflict"):
        j.open_job("j", "someone-else", "build", tmp_path, project_root="project")


@pytest.mark.parametrize("status", ["completed", "failed", "stopped"])
def test_fold_settlement_keeps_the_live_writer_and_project_until_exit(job_control, tmp_path, monkeypatch, status):
    jobs = job_control
    handoffs = []
    monkeypatch.setattr(jobs, "_hand_off_held_mail", lambda: handoffs.append(jobs.get_job("job").status))
    original = jobs.open_job("job", "root", "build", tmp_path, project_root="project")
    assert jobs.enter_writer("job", "root")

    pending = jobs.settle_job_when_idle("job", status=status, summary="fold settled")

    assert pending == original
    assert jobs.get_job("job") == original
    assert jobs.owned_project("job", "root") == tmp_path / "project"
    assert not jobs.enter_writer("job", "root")
    with pytest.raises(jobs.ProjectClaimConflict):
        jobs.open_job("other", "other-root", "competing build", tmp_path, project_root="project")
    assert handoffs == []

    jobs.leave_writer("job", "wrong-root")
    assert jobs.get_job("job") == original
    jobs.leave_writer("job", "root")

    settled = jobs.get_job("job")
    assert (settled.status, settled.owner_cycle_id, settled.summary) == (status, "", "fold settled")
    assert handoffs == [status]
    assert jobs.open_job("other", "other-root", "competing build", tmp_path, project_root="project")


def test_fold_settlement_without_a_writer_is_immediate_and_conditional(job_control, tmp_path):
    jobs = job_control
    original = jobs.open_job("job", "root", "build", tmp_path)
    assert jobs.settle_job_when_idle(
        "job", status="completed", summary="stale", expected_owner_cycle_id="other") is None
    assert jobs.settle_job_when_idle(
        "job", status="completed", summary="stale", expected_revision=1) is None
    assert jobs.get_job("job") == original

    settled = jobs.settle_job_when_idle(
        "job", status="completed", summary="done", expected_owner_cycle_id="root", expected_revision=0)

    assert settled.status == "completed"
    assert jobs.settle_job_when_idle("job", status="failed", summary="late fold") == settled
    assert jobs.get_job("job") == settled


def test_executor_finish_supersedes_deferred_fold_settlement(job_control, tmp_path):
    jobs = job_control
    jobs.open_job("job", "root", "build", tmp_path, project_root="project")
    assert jobs.enter_writer("job", "root")
    jobs.settle_job_when_idle("job", status="completed", summary="older fold")

    settled = jobs.finish_job("job", status="stopped", summary="executor observed stop")

    assert settled.status == "stopped"
    jobs.leave_writer("job", "root")
    assert jobs.get_job("job") == settled
    jobs.open_job("job", "root", "build", tmp_path, project_root="project")
    assert jobs.enter_writer("job", "root")
    jobs.leave_writer("job", "root")
    assert jobs.get_job("job").status == "running"


def test_a_new_revision_invalidates_deferred_fold_settlement(job_control, tmp_path):
    jobs = job_control
    jobs.open_job("job", "root", "build", tmp_path, project_root="project")
    assert jobs.enter_writer("job", "root")
    jobs.settle_job_when_idle("job", status="completed", summary="before instruction")
    jobs.request_instruction("job", "new requirement", source_cycle_id="operator", arrival_seq=7)

    jobs.leave_writer("job", "root")

    assert jobs.get_job("job").status == "running"
    with pytest.raises(jobs.ProjectClaimConflict):
        jobs.open_job("other", "other-root", "competing build", tmp_path, project_root="project")
    assert jobs.enter_writer("job", "root")
    jobs.leave_writer("job", "root")
    assert jobs.get_job("job").status == "running"


def test_a_crash_during_deferred_settlement_never_reports_completion(job_control, tmp_path):
    jobs = job_control
    jobs.open_job("job", "root", "build", tmp_path, project_root="project")
    assert jobs.enter_writer("job", "root")
    jobs.settle_job_when_idle("job", status="completed", summary="fold finished before writer")

    jobs.reset_for_tests()
    jobs.reconcile_jobs_at_boot(())

    interrupted = jobs.get_job("job")
    assert interrupted.status == "stopped"
    assert interrupted.summary != "fold finished before writer"
    assert jobs.reclaim_writer("job", "restored") is not None
    assert jobs.enter_writer("job", "restored")
    jobs.leave_writer("job", "restored")
    assert jobs.get_job("job").status == "running"


def test_failed_deferred_publication_keeps_the_project_claim_until_retry(job_control, tmp_path, monkeypatch):
    jobs = job_control
    jobs.open_job("job", "root", "build", tmp_path, project_root="project")
    assert jobs.enter_writer("job", "root")
    jobs.settle_job_when_idle("job", status="completed", summary="done")
    publish = jobs._publish

    def fail_publication(job):
        raise OSError("settlement unavailable")

    monkeypatch.setattr(jobs, "_publish", fail_publication)
    with pytest.raises(OSError, match="settlement unavailable"):
        jobs.leave_writer("job", "root")
    assert jobs.get_job("job").status == "running"
    assert not jobs.enter_writer("job", "root")
    with pytest.raises(jobs.ProjectClaimConflict):
        jobs.open_job("other", "other-root", "competing build", tmp_path, project_root="project")

    monkeypatch.setattr(jobs, "_publish", publish)
    jobs.leave_writer("job", "root")
    assert jobs.get_job("job").status == "completed"


@pytest.mark.parametrize("deferred", [False, True])
def test_fold_settlement_handoff_runs_outside_job_locks(job_control, tmp_path, monkeypatch, deferred):
    from threading import Event, Thread

    jobs = job_control
    jobs.open_job("job", "root", "build", tmp_path)
    observed = []

    def handoff():
        finished = Event()

        def read_from_another_thread():
            observed.append(jobs.get_job("job").status)
            finished.set()

        reader = Thread(target=read_from_another_thread, daemon=True)
        reader.start()
        assert finished.wait(5), "handoff must not hold the job or project-scope lock"
        reader.join(5)

    monkeypatch.setattr(jobs, "_hand_off_held_mail", handoff)
    if deferred:
        assert jobs.enter_writer("job", "root")
    jobs.settle_job_when_idle("job", status="completed", summary="done")
    if deferred:
        assert observed == []
        jobs.leave_writer("job", "root")
    assert observed == ["completed"]


@pytest.mark.parametrize("status", ["completed", "failed", "stopped"])
def test_idle_settlement_cas_never_calls_handoff_or_wake(job_control, tmp_path, monkeypatch, status):
    jobs = job_control
    jobs.open_job("job", "root", "build", tmp_path, project_root="project")

    def forbidden_callback(*args, **kwargs):
        pytest.fail("the caller owns notification after releasing its successor ledger lock")

    monkeypatch.setattr(jobs, "_hand_off_held_mail", forbidden_callback)
    monkeypatch.setattr(jobs, "_wake", forbidden_callback)

    settled = jobs.settle_if_idle(
        "job", expected_owner="root", expected_revision=0, status=status, summary="retired")

    assert (settled.status, settled.owner_cycle_id, settled.summary) == (status, "", "retired")
    assert jobs.get_job("job") == settled
    assert jobs.open_job("other", "other-root", "competing build", tmp_path, project_root="project")


@pytest.mark.parametrize("conflict", ["owner", "revision", "executing", "terminal"])
def test_idle_settlement_refuses_stale_or_live_work_without_deferred_effects(job_control, tmp_path, conflict):
    jobs = job_control
    jobs.open_job("job", "root", "build", tmp_path, project_root="project")
    expected_owner = "other-root" if conflict == "owner" else "root"
    expected_revision = 1 if conflict == "revision" else 0
    if conflict == "executing":
        assert jobs.enter_writer("job", "root")
    elif conflict == "terminal":
        jobs.finish_job("job", status="completed", summary="already done")
        expected_owner = ""
    original = jobs.get_job("job")

    assert jobs.settle_if_idle(
        "job", expected_owner=expected_owner, expected_revision=expected_revision,
        status="stopped", summary="orphan cleanup") is None

    assert jobs.get_job("job") == original
    if conflict == "executing":
        assert not jobs.enter_writer("job", "root")
        with pytest.raises(jobs.ProjectClaimConflict):
            jobs.open_job("other", "other-root", "competing build", tmp_path, project_root="project")
        jobs.leave_writer("job", "root")
        assert jobs.get_job("job") == original


def test_an_old_invocations_exit_cannot_settle_a_new_writer_with_the_same_cycle(job_control, tmp_path):
    from threading import Event

    jobs = job_control
    jobs.open_job("job", "root", "build", tmp_path, project_root="project")
    assert jobs.enter_writer("job", "root")
    jobs.finish_job("job", status="completed", summary="old invocation returned")
    started = Event()
    release = Event()

    def next_invocation():
        jobs.open_job("job", "root", "build", tmp_path, project_root="project")
        assert jobs.enter_writer("job", "root")
        started.set()
        try:
            assert release.wait(10)
        finally:
            jobs.leave_writer("job", "root")

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(next_invocation)
        try:
            assert started.wait(5)
            jobs.settle_job_when_idle("job", status="completed", summary="new fold")

            jobs.leave_writer("job", "root")

            assert jobs.get_job("job").status == "running"
            assert not jobs.enter_writer("job", "root")
            with pytest.raises(jobs.ProjectClaimConflict):
                jobs.open_job("other", "other-root", "competing build", tmp_path, project_root="project")
        finally:
            release.set()
        worker.result(timeout=5)
    assert jobs.get_job("job").status == "completed"

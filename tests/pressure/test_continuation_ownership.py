from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from auto_loop.execute import job_control as jobs
from pressure_loop import pressure
from pressure_loop.omega_successor import build_omega_successor, stage_omega_successor
from tests.pressure.test_omega_successor_pressure import control


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from pressure_loop.operator_chat_responses import clear_work_halt

    clear_work_halt("operator-root")
    monkeypatch.setattr(pressure, "_successor_path", lambda: tmp_path / "successors.json")
    monkeypatch.setattr(pressure, "_RESEED_STAGING", {})
    monkeypatch.setattr(jobs, "jobs_root", lambda: tmp_path / "jobs")
    jobs.reset_for_tests()
    yield
    jobs.reset_for_tests()
    clear_work_halt("operator-root")


def parent(identity, job_id="job-a", thread_id="thread-a"):
    return SimpleNamespace(
        episode_id=identity, thread_id=thread_id, global_={}, opened_by="operator_query",
        pressure={"contributors": [{"id": "operator_query", "detail": {
            "cycle_id": "operator-root", "agent_job_id": job_id,
            "job_cycle_id": "operator-root"}}]},
        auto={"outputs": {"agent_run": {"job_id": job_id, "status": "batch_ran"}}})


def test_two_parents_cannot_claim_the_same_continuation():
    with ThreadPoolExecutor(2) as workers:
        rows = list(workers.map(lambda name: stage_omega_successor(parent(name), control()),
                                ["parent-a", "parent-b"]))
    assert sum(row is not None for row in rows) == 1
    assert len(pressure.staged_signals()) == 1


def test_specialist_successor_cannot_bind_an_intent_to_another_lane():
    episode = SimpleNamespace(
        episode_id="browser-parent", thread_id="thread-a", global_={},
        opened_by="lane_work_dispatch",
        pressure={"contributors": [{"id": "lane_work_dispatch", "detail": {
            "lane_work_handoff": True, "lane": "browser", "handoff_id": "a" * 64,
            "original_cycle_id": "operator-root", "thread_id": "thread-a",
            "original_objective": "browse the catalog"}}]},
        auto={"outputs": {}},
    )
    with pytest.raises(ValueError, match="lane conflicts with its validated intent"):
        build_omega_successor(episode, control())


def test_two_jobs_on_one_thread_can_both_continue():
    first = stage_omega_successor(parent("parent-a", "job-a"), control())
    second = stage_omega_successor(parent("parent-b", "job-b"), control())
    assert first is not None and second is not None
    assert len(pressure.staged_signals()) == 2


def test_four_jobs_on_one_thread_can_all_continue():
    """Spec section 8: four episodes of one thread, four distinct jobs, stay legal."""
    rows = [stage_omega_successor(parent(f"parent-{index}", f"job-{index}"), control())
            for index in range(4)]
    assert all(row is not None for row in rows)
    assert len(pressure.staged_signals()) == 4


def test_stale_revision_cannot_stage(tmp_path):
    jobs.open_job("job-a", "operator-root", "build", tmp_path)
    candidate = build_omega_successor(parent("parent-a"), control())
    jobs.request_instruction("job-a", "use sqlite", source_cycle_id="note", arrival_seq=10)
    pressure.stage_reseed([candidate])
    assert pressure.staged_signals() == ()


def test_admission_holds_claim_until_the_continuation_folds():
    first = stage_omega_successor(parent("parent-a"), control())
    assert pressure.release_staged(first)
    assert stage_omega_successor(parent("parent-b"), control()) is None
    pressure.complete_continuation(first.metadata)
    assert stage_omega_successor(parent("parent-c"), control()) is not None


def test_completed_continuations_over_the_cap_still_refuse_reclaim(monkeypatch):
    monkeypatch.setattr(pressure, "_MAX_TERMINAL_CONTINUATIONS", 0)
    first = stage_omega_successor(parent("parent-a"), control())
    assert pressure.release_staged(first)
    pressure.complete_continuation(first.metadata)
    second = stage_omega_successor(parent("parent-c"), control())
    assert second is not None
    assert pressure.release_staged(second)
    pressure.complete_continuation(second.metadata)

    data = pressure._read_successors()
    live = [row for row in data["continuations"].values() if row["state"] == "completed"]
    assert live == []
    first_key = __import__("hashlib").sha256(first.metadata["successor_id"].encode()).hexdigest()
    second_key = __import__("hashlib").sha256(second.metadata["successor_id"].encode()).hexdigest()
    assert data["continuation_retired"][first_key] == "completed"
    assert data["continuation_retired"][second_key] == "completed"
    assert pressure._claim_continuation_into(data, first.metadata) == "the continuation is completed"
    assert pressure._claim_continuation_into(data, second.metadata) == "the continuation is completed"


def test_retirement_releases_the_claim_but_never_restages_its_row():
    first = stage_omega_successor(parent("parent-a"), control())
    pressure.retire_successor(first, "workflow gone")
    pressure.stage_reseed([first])
    assert pressure.staged_signals() == ()
    assert stage_omega_successor(parent("parent-b"), control()) is not None


def test_a_durable_claim_survives_a_reload():
    first = stage_omega_successor(parent("parent-a"), control())
    pressure._RESEED_STAGING.clear()
    assert stage_omega_successor(parent("parent-b"), control()) is None
    assert pressure.staged_signals() == (first,)


def test_recovery_releases_a_dead_owner_and_reclaims_the_pending_successor(monkeypatch):
    first = stage_omega_successor(parent("parent-a"), control())
    monkeypatch.setattr(pressure, "_CONTINUATION_EPOCH", "restarted-process")
    pressure.recover_continuations()
    assert pressure.staged_signals() == (first,)
    assert stage_omega_successor(parent("parent-b"), control()) is None
    assert pressure.release_staged(first)


def test_completed_old_owner_cannot_complete_the_new_owner():
    first = stage_omega_successor(parent("parent-a"), control())
    pressure.release_staged(first)
    pressure.complete_continuation(first.metadata)
    second = stage_omega_successor(parent("parent-b"), control())
    pressure.complete_continuation(first.metadata)
    assert stage_omega_successor(parent("parent-c"), control()) is None
    assert pressure.staged_signals() == (second,)


def test_losing_publication_withholds_mail_and_preserves_the_winning_job(tmp_path):
    import cognition_chain
    from global_loop import intersections

    jobs.open_job("job-a", "operator-root", "build", tmp_path)
    winner = parent("parent-a")
    loser = parent("parent-b")
    injection = {"target_lane": "agent", "objective": "finish the build", "constraints": [],
                 "evidence_refs": [], "requirement": "required"}
    for episode in (winner, loser):
        episode.meta = {}
        episode.global_["omega_result"] = {"lane_injections": [injection]}
        cognition_chain._settle_operator_turn(episode, control())
    assert len(pressure.staged_signals()) == 1
    assert len(intersections.all_mail()) == 1
    assert jobs.get_job("job-a").status == "running"


def test_boot_preserves_an_unpublished_records_claim(monkeypatch):
    from omega_loop.episode import transaction

    original = transaction._complete

    def crash(_fold_id):
        raise RuntimeError("death before pin")

    monkeypatch.setattr(transaction, "_complete", crash)
    with pytest.raises(RuntimeError, match="death before pin"):
        transaction.publish_cognitive_transaction(parent("parent-a"), control())
    monkeypatch.setattr(transaction, "_complete", original)
    monkeypatch.setattr(pressure, "_CONTINUATION_EPOCH", "restarted-process")
    pressure.recover_continuations()
    assert stage_omega_successor(parent("parent-b"), control()) is None
    transaction.reconcile_transactions_at_boot()
    assert len(pressure.staged_signals()) == 1


def test_boot_withholds_an_unpublished_successor_when_its_revision_is_stale(tmp_path, monkeypatch):
    from omega_loop.episode import transaction

    jobs.open_job("job-a", "operator-root", "build", tmp_path)
    complete = transaction._complete
    monkeypatch.setattr(transaction, "_complete",
                        lambda _fold: (_ for _ in ()).throw(RuntimeError("B2 interruption")))
    with pytest.raises(RuntimeError, match="B2 interruption"):
        transaction.publish_cognitive_transaction(parent("before-revision"), control())

    jobs.request_instruction("job-a", "changed requirement", source_cycle_id="new-request",
                             arrival_seq=22)
    monkeypatch.setattr(transaction, "_complete", complete)
    monkeypatch.setattr(pressure, "_CONTINUATION_EPOCH", "new-process")
    pressure.recover_continuations()
    transaction.reconcile_transactions_at_boot()

    record = next(iter(pressure.transactions().values()))
    assert record["published"]
    assert record["staging"] == "none"
    assert record["successor"] is None
    claim = next(iter(pressure._read_successors()["continuations"].values()))
    assert claim["state"] == "cancelled"
    assert claim["reason"] == "the continued work revision changed"
    assert not pressure.unpublished_job_continuation("job-a")


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_boot_cancels_an_unpublished_successor_for_a_terminal_job(tmp_path, monkeypatch, status):
    from omega_loop.episode import transaction

    jobs.open_job("job-a", "operator-root", "build", tmp_path)
    complete = transaction._complete
    monkeypatch.setattr(transaction, "_complete",
                        lambda _fold: (_ for _ in ()).throw(RuntimeError("B2 interruption")))
    with pytest.raises(RuntimeError, match="B2 interruption"):
        transaction.publish_cognitive_transaction(parent("before-terminal"), control())

    jobs.settle_job_when_idle("job-a", status=status, summary="finished",
                              expected_owner_cycle_id="operator-root", expected_revision=0)
    monkeypatch.setattr(transaction, "_complete", complete)
    monkeypatch.setattr(pressure, "_CONTINUATION_EPOCH", "new-process")
    pressure.recover_continuations()
    transaction.reconcile_transactions_at_boot()

    record = next(iter(pressure.transactions().values()))
    claim = next(iter(pressure._read_successors()["continuations"].values()))
    assert record["published"] and record["successor"] is None
    assert claim["state"] == "cancelled"
    assert claim["reason"] == f"the continued job is {status}"
    assert not pressure.unpublished_job_continuation("job-a")


def test_stale_work_is_refused_at_actual_admission(tmp_path):
    from pressure_loop import cognition_queue
    from pressure_loop.loop import _lease_runnable_input

    jobs.open_job("job-a", "operator-root", "build", tmp_path)
    candidate = stage_omega_successor(parent("parent-a"), control())
    cognition_queue.enqueue_input(candidate, operator_activity=False)
    jobs.request_instruction("job-a", "use sqlite", source_cycle_id="note", arrival_seq=10)
    assert _lease_runnable_input(2) == (None, False)
    assert cognition_queue.peek_inputs() == ()
    assert pressure.staged_signals() == ()
    assert jobs.get_job("job-a").status == "stopped"


def test_a_restored_queue_only_successor_completes_its_claim(monkeypatch):
    from pressure_loop import cognition_queue

    candidate = stage_omega_successor(parent("parent-a"), control())
    cognition_queue.enqueue_input(candidate, operator_activity=False)
    candidate = cognition_queue.lease_next_input(2)
    cognition_queue.acknowledge_input(candidate, 2)
    pressure.release_staged(candidate)
    cognition_queue.enqueue_restore(candidate)
    monkeypatch.setattr(pressure, "_CONTINUATION_EPOCH", "restarted-process")
    pressure.recover_continuations()
    pressure.release_staged(candidate)
    pressure.complete_continuation(candidate.metadata)
    assert stage_omega_successor(parent("parent-b"), control()) is not None


def test_a_losing_fold_does_not_close_the_admitted_winners_job(tmp_path):
    import cognition_chain

    jobs.open_job("job-a", "operator-root", "build", tmp_path)
    winner = stage_omega_successor(parent("parent-a"), control())
    pressure.release_staged(winner)
    loser = parent("parent-b")
    loser.meta = {}
    cognition_chain._settle_operator_turn(loser, control())
    assert jobs.get_job("job-a").status == "running"


def test_retiring_a_restored_queue_only_successor_releases_its_claim(monkeypatch):
    from pressure_loop import cognition_queue

    candidate = stage_omega_successor(parent("parent-a"), control())
    cognition_queue.enqueue_input(candidate, operator_activity=False)
    candidate = cognition_queue.lease_next_input(2)
    cognition_queue.acknowledge_input(candidate, 2)
    pressure.release_staged(candidate)
    cognition_queue.enqueue_restore(candidate)
    monkeypatch.setattr(pressure, "_CONTINUATION_EPOCH", "restarted-process")
    pressure.recover_continuations()
    pressure.retire_successor(candidate, "the workflow disappeared before restoration")
    assert not pressure.job_has_continuation("job-a")
    assert stage_omega_successor(parent("parent-b"), control()) is not None


def test_a_failed_fold_releases_the_claim_carried_by_its_ignition():
    import cognition_chain

    candidate = stage_omega_successor(parent("parent-a"), control())
    pressure.release_staged(candidate)
    episode = parent("failed-child")
    episode.opened_by = "omega_successor"
    episode.pressure = {"contributors": [{"id": "omega_successor", "detail": candidate.metadata}]}
    episode.meta = {}
    cognition_chain._settle_operator_turn(episode, None)
    assert not pressure.job_has_continuation("job-a")
    assert stage_omega_successor(parent("parent-b"), control()) is not None


def test_a_restored_successor_with_the_bound_job_completes(monkeypatch):
    from dataclasses import replace
    from pressure_loop import cognition_queue

    candidate = stage_omega_successor(parent("parent-a"), control())
    cognition_queue.enqueue_input(candidate, operator_activity=False)
    candidate = cognition_queue.lease_next_input(2)
    cognition_queue.acknowledge_input(candidate, 2)
    pressure.release_staged(candidate)
    restored = replace(candidate, metadata={**candidate.metadata, "job_id": "job-a"})
    cognition_queue.enqueue_restore(restored)
    monkeypatch.setattr(pressure, "_CONTINUATION_EPOCH", "restarted-process")
    pressure.recover_continuations()
    pressure.release_staged(restored)
    pressure.complete_continuation(restored.metadata)
    assert stage_omega_successor(parent("parent-b"), control()) is not None


def test_a_done_fold_cancels_its_jobs_queued_continuation(tmp_path):
    import cognition_chain
    from pressure_loop import cognition_queue
    from pressure_loop.loop import _lease_runnable_input

    jobs.open_job("job-a", "operator-root", "build", tmp_path)
    candidate = stage_omega_successor(parent("parent-a"), control())
    cognition_queue.enqueue_input(candidate, operator_activity=False)
    settled = parent("parent-b")
    settled.meta = {}
    cognition_chain._settle_operator_turn(settled, control(decision="DONE"))
    assert _lease_runnable_input(2) == (None, False)
    assert pressure.staged_signals() == ()
    assert not pressure.job_has_continuation("job-a")
    assert jobs.get_job("job-a").status == "completed"


def test_boot_does_not_handoff_mail_owed_to_an_unpublished_successor(tmp_path, monkeypatch):
    from core.pipeline_state import restore_pipelines_at_boot
    from global_loop import intersections as mail
    from omega_loop.episode import transaction
    from pressure_loop import cognition_queue

    jobs.open_job("job-a", "operator-root", "build", tmp_path)
    complete = transaction._complete
    monkeypatch.setattr(transaction, "_complete", lambda _fold: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        transaction.publish_cognitive_transaction(parent("parent-a"), control())
    message = mail.pin_mail(thread_id="thread-a", delivery_point="omega_reason",
                            purpose="operator_instruction", requirement="required",
                            source_actor="operator", source_episode_id="later-turn",
                            target_job_id="job-a", payload={"text": "use sqlite"})
    monkeypatch.setattr(transaction, "_complete", complete)
    restore_pipelines_at_boot()
    assert mail.all_mail()[0]["mail_id"] == message["mail_id"]
    assert mail.all_mail()[0]["status"] == "ready"
    assert not any(row.metadata.get("held_instruction") for row in cognition_queue.peek_inputs())
    assert len(pressure.staged_signals()) == 1


def test_cancelled_unpublished_successor_stays_cancelled_after_another_owner(tmp_path, monkeypatch):
    from global_loop import intersections as mail
    from omega_loop.episode import transaction

    jobs.open_job("job-a", "operator-root", "build", tmp_path)
    first = parent("parent-a")
    first.meta = {}
    first.global_["omega_result"] = {"lane_injections": [{
        "target_lane": "agent", "objective": "finish", "constraints": [],
        "evidence_refs": [], "requirement": "required"}]}
    complete = transaction._complete
    monkeypatch.setattr(transaction, "_complete", lambda _fold: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        transaction.publish_cognitive_transaction(first, control())
    pressure.cancel_job_continuations("job-a", "completed elsewhere")
    second = stage_omega_successor(parent("parent-b"), control())
    assert second is not None
    pressure.release_staged(second)
    pressure.release_continuation(second.metadata, "failed cascade")
    monkeypatch.setattr(transaction, "_complete", complete)
    monkeypatch.setattr(pressure, "_CONTINUATION_EPOCH", "new-process")
    pressure.recover_continuations()
    transaction.reconcile_transactions_at_boot()
    assert pressure.staged_signals() == ()
    assert mail.all_mail() == []
    record = next(iter(pressure.transactions().values()))
    assert record["published"] and record["staging"] == "none"
    assert record["withheld"][0]["disposition"] == "undeliverable"


def test_retirement_keeps_a_job_while_another_fold_is_alive(tmp_path, monkeypatch):
    from core import pipeline_state

    jobs.open_job("job-a", "operator-root", "build", tmp_path)
    candidate = stage_omega_successor(parent("parent-a"), control())
    monkeypatch.setattr(pipeline_state, "envelopes", lambda: (
        SimpleNamespace(state="INFERENCE", job_id="job-a", continuation=None),))
    pressure.retire_successor(candidate, "revision changed")
    assert jobs.get_job("job-a").status == "running"
    monkeypatch.setattr(pipeline_state, "envelopes", lambda: ())
    pressure.settle_retired_jobs()
    assert jobs.get_job("job-a").status == "stopped"


def test_retirement_keeps_an_executing_writer(tmp_path):
    jobs.open_job("job-a", "operator-root", "build", tmp_path)
    candidate = stage_omega_successor(parent("parent-a"), control())
    assert jobs.enter_writer("job-a", "operator-root")
    pressure.retire_successor(candidate, "revision changed")
    assert jobs.get_job("job-a").status == "running"
    jobs.leave_writer("job-a", "operator-root")
    pressure.settle_retired_jobs()
    assert jobs.get_job("job-a").status == "stopped"


def test_a_done_fold_does_not_release_another_batches_project(tmp_path):
    from threading import Event
    import cognition_chain

    jobs.open_job("job-a", "operator-root", "build", tmp_path, project_root="project")
    entered, leave = Event(), Event()

    def running_batch():
        assert jobs.enter_writer("job-a", "operator-root")
        entered.set()
        try:
            assert leave.wait(20)
        finally:
            jobs.leave_writer("job-a", "operator-root")

    with ThreadPoolExecutor(1) as workers:
        batch = workers.submit(running_batch)
        try:
            assert entered.wait(5)
            episode = parent("settling-parent")
            episode.meta = {}
            cognition_chain._settle_operator_turn(episode, control(decision="DONE"))
            assert jobs.get_job("job-a").status == "running"
            with pytest.raises(jobs.ProjectClaimConflict):
                jobs.open_job("job-b", "another-root", "other build", tmp_path, project_root="project")
        finally:
            leave.set()
        batch.result(timeout=5)
    assert jobs.get_job("job-a").status == "completed"


def test_stopping_the_last_queued_batch_releases_its_project(tmp_path):
    from pressure_loop import cognition_queue
    from pressure_loop.loop import _lease_runnable_input
    from pressure_loop.operator_chat import halt_operator_work

    jobs.open_job("job-a", "operator-root", "build", tmp_path, project_root="project")
    candidate = stage_omega_successor(parent("parent-a"), control())
    cognition_queue.enqueue_input(candidate, operator_activity=False)
    halt_operator_work("operator-root")
    assert _lease_runnable_input(2) == (None, False)
    assert jobs.get_job("job-a").status == "stopped"
    assert jobs.open_job("job-b", "another-root", "other build", tmp_path, project_root="project").status == "running"


def test_a_late_fold_cannot_claim_completed_work(tmp_path):
    import cognition_chain

    jobs.open_job("job-a", "operator-root", "build", tmp_path)
    episode = parent("settled-parent")
    episode.meta = {}
    cognition_chain._settle_operator_turn(episode, control(decision="DONE"))

    assert stage_omega_successor(parent("late-parent"), control()) is None
    assert not pressure.job_has_continuation("job-a")
    assert pressure.staged_signals() == ()


def test_claim_published_during_done_cancellation_cannot_be_admitted(tmp_path, monkeypatch):
    import cognition_chain
    from pressure_loop import cognition_queue
    from pressure_loop.loop import _lease_runnable_input

    jobs.open_job("job-a", "operator-root", "build", tmp_path)
    cancel = pressure.cancel_job_continuations

    def competing_fold(job_id, reason):
        cancel(job_id, reason)
        late = stage_omega_successor(parent("late-parent"), control())
        assert late is not None  # The other fold wins publication before job settlement.
        cognition_queue.enqueue_input(late, operator_activity=False)

    monkeypatch.setattr(pressure, "cancel_job_continuations", competing_fold)
    episode = parent("settled-parent")
    episode.meta = {}
    cognition_chain._settle_operator_turn(episode, control(decision="DONE"))

    assert jobs.get_job("job-a").status == "completed"
    assert _lease_runnable_input(2) == (None, False)
    assert cognition_queue.peek_inputs() == ()
    assert not pressure.job_has_continuation("job-a")

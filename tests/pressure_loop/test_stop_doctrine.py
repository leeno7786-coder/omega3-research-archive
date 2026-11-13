"""T12: hard stops are explicit controls; chat text enters the Chat lane first.

A stop button is host-owned execution control. Natural-language instructions enter the durable
Chat FIFO and are classified inside Global A before any specialist lane receives work. A stop
that lands after a successor was staged must not resurrect the work: it is refused at
publication and again at admission.

Boundary tests over the real surfaces: the real lane tool table and per-call gate, the real
chat ingress (``handle_operator_chat``), real job records, the real mail store,
the real Global B binder and agent pack, and the real pressure gather.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from auto_loop.execute import brain_skill_executor as bse
from auto_loop.execute import job_control as jobs
from global_loop import intersections as mail
from pressure_loop import cognition_queue as cq
from pressure_loop import loop
from pressure_loop import operator_chat_responses as replies
from pressure_loop import pressure
from pressure_loop.omega_successor import (
    build_omega_successor,
    cancellation_reason,
    cancellation_state,
    stage_omega_successor,
)
from pressure_loop.operator_chat import halt_operator_work, handle_operator_chat
from schema.omega_control_decision import OmegaControlDecision

OBJECTIVE = "build a todo app"
THREAD = "t-build"
JOB = "job-a"
JOB_CYCLE = "bubble-build"
CHAT_CYCLE = "bubble-chat"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Nothing here may reach the live queue, the live successor ledger or a stale work halt.

    The in-memory caches are cleared as well, because these boundaries are about what the
    operator's words are TAKEN FOR: a question left pending by another test would make every
    turn here an answer to it (T7's own rail), and stale mail would answer for this pin.
    """
    monkeypatch.setattr(pressure, "_successor_path", lambda: tmp_path / "successors.json")
    monkeypatch.setattr(pressure, "_RESEED_STAGING", {})
    monkeypatch.setattr(cq, "_queue_path", lambda: tmp_path / "queue.json")
    cq._reset_cache_for_tests()
    pressure._UNRESOLVED_STOPS.clear()
    mail.reset_cache_for_tests()
    for cycle in (JOB_CYCLE, CHAT_CYCLE, "note-cycle", "bubble"):
        replies.clear_work_halt(cycle)
    yield
    for cycle in (JOB_CYCLE, CHAT_CYCLE, "note-cycle", "bubble"):
        replies.clear_work_halt(cycle)
    cq.clear_inputs()
    pressure._UNRESOLVED_STOPS.clear()
    mail.reset_cache_for_tests()


@pytest.fixture
def queued(monkeypatch):
    """Turns that took the ordinary path — captured instead of entering the real line."""
    turns: list = []
    monkeypatch.setattr(cq, "enqueue_input", lambda contributor, **_kw: turns.append(contributor) or True)
    return turns


def _open(job_id=JOB, *, thread_id=THREAD, cycle_id=JOB_CYCLE):
    root = jobs.jobs_root().parent.parent.parent
    project = root / "projects" / job_id
    project.mkdir(parents=True, exist_ok=True)
    return jobs.open_job(job_id, cycle_id, OBJECTIVE, root, project_root=project,
                         thread_id=thread_id)


def _chat(text, **kwargs):
    """The real public ingress, which reserves the turn's arrival exactly as the HUD send
    does — an unreserved turn is refused, so the reservation is part of the path under test."""
    kwargs.setdefault("cycle_id", "note-cycle")
    return handle_operator_chat(text, **kwargs)


def _ready():
    return [row for row in mail.pending_mail() if row["status"] == "ready"]


def _revision_kinds(job_id=JOB):
    return [row.kind for row in jobs.get_job(job_id).revisions]


def _queue():
    return list(cq.peek_inputs())


# ── 1. chat holds no job control ──────────────────────────────────────────────

def test_the_chat_lane_does_not_hold_job_control():
    """Job control remains available only on the Coding Agent lane."""
    assert "stop_job" not in bse.lane_tool_names("chat")
    assert "stop_job" in bse.lane_tool_names("agent"), "job control belongs to the agent lane"
    names = [spec["function"]["name"] for spec in
             bse.operator_tool_specs("always_allow", operator_route="chat")]
    assert names == ["web_search", "web_fetch", "discover_plugin_tools", "call_plugin_tool"]
    assert "stop_job" in bse.OPERATOR_TOOL_NAMES, "the tool still exists, for the lane that owns it"


def test_the_chat_lane_refuses_to_run_job_control_even_when_a_model_asks(tmp_path):
    """Offering is not the only gate: the per-call gate refuses it too."""
    _open()
    ctx = bse.ExecutionContext(tmp_path, OBJECTIVE, 10, trusted_roots=(tmp_path,),
                               operator_route="chat")

    result, _args = bse.run_operator_tool("stop_job", {"job_id": JOB}, ctx)

    assert not result.ok and "not available in the chat lane" in (result.error or "")
    assert _revision_kinds() == [], "the build was not touched by the refused call"


# ── 2. natural-language stops enter Chat before any lane routing ─────────────

def test_a_plain_stop_is_queued_for_global_a_classification(queued):
    _open()

    result = _chat("stop")

    assert result["ok"] is True and result["queued"] is True
    assert len(queued) == 1
    assert cq.peek_inputs() == (), "the fixture captured ingress instead of the durable store"
    assert queued[0].source == "operator"
    assert queued[0].metadata["message"] == "stop"
    assert not replies.work_halt_requested(JOB_CYCLE), "chat ingress does not execute a stop"
    assert _revision_kinds() == [] and _ready() == []


def test_esc_halts_every_running_build_with_no_turn_named():
    """Esc and the explicit stop control reach ``halt_operator_work`` directly."""
    _open()
    _open("job-b", thread_id="t-other", cycle_id="bubble-other")

    halted = halt_operator_work("")

    assert sorted(halted["stopped_jobs"]) == ["job-a", "job-b"]
    assert _revision_kinds("job-a") == ["stop"] and _revision_kinds("job-b") == ["stop"]
    assert _ready() == [], "a hard stop is never mail"

# ── 4. a cancel that lands after staging is refused twice ─────────────────────

def _control(intent="USE_TOOL", decision="PRESSURE"):
    assessment = dict.fromkeys(("who", "what", "when", "where", "why", "how",
                                "desired_state", "observed_state"), "fact")
    assessment.update(gap="" if decision == "DONE" else "unfinished", evidence=[])
    return OmegaControlDecision.from_dict(dict(
        assessment=assessment, decision=decision, synthesis="summary",
        pressure=dict(intent=intent, confidence=0.5, reason="finish", setpoints={},
                      desired_state="complete", observed_state="partial",
                      resolution_conditions=["verified"])))


def _agent_episode(identity="parent", *, job_id=JOB, cycle_id=JOB_CYCLE):
    """An agent episode whose batch ran the job, so its successor continues that job."""
    return SimpleNamespace(
        episode_id=identity, thread_id=THREAD, opened_by="operator_query",
        global_={"omega_packet": {"original_objective": OBJECTIVE, "omega_lane": "agent"}},
        pressure={"contributors": [{"id": "operator_query", "detail": {
            "cycle_id": cycle_id, "job_cycle_id": JOB_CYCLE,
        }}]},
        auto={"outputs": {"agent_run": {"status": "batch_ran", "steps": 1, "job_id": job_id}}})


def _chat_episode(identity="chat-parent"):
    """A chat episode: its successor carries no job, so no job record can carry the stop."""
    return SimpleNamespace(
        episode_id=identity, thread_id="t-chat", opened_by="operator_query",
        global_={"omega_packet": {"original_objective": "explain it", "omega_lane": "chat"}},
        pressure={"contributors": [{"id": "operator_query", "detail": {"cycle_id": CHAT_CYCLE}}]},
        auto={"outputs": {}})


def _outstanding_work(where: str):
    """Work of JOB_CYCLE waiting in one of the places a continuation can wait."""
    from core import pipeline_state

    if where == "staged":
        assert stage_omega_successor(_agent_episode(), _control()) is not None
        return
    if where == "queued":
        row = stage_omega_successor(_agent_episode(), _control())
        assert row is not None and cq.enqueue_input(row, operator_activity=False)
        assert pressure.release_staged(row), "queued and no longer staged"
        return
    if where == "in_flight":
        pipeline_state._write(0, state="INFERENCE", cycle_id=JOB_CYCLE, work_kind="operator_turn")
        return
    row = stage_omega_successor(_agent_episode(), _control())
    assert row is not None
    opened, created = pressure.open_transaction(_transaction_record(row))
    assert opened is not None and created
    assert pressure.release_staged(row), "the record carries it now, not the staging"


def _transaction_record(successor_row):
    """One open (unpublished, unclosed) cognitive transaction carrying this successor."""
    from pressure_loop.pressure import _successor_row

    return {"fold_id": "fold-stop", "episode_id": "parent", "thread_id": THREAD,
            "decision_digest": "d" * 64, "receipt_ids": [], "mail_cutoff_seq": 0,
            "bound_mail_ids": [], "outgoing": [], "continuation_key": "k-stop",
            "staging": "pending", "published": False, "pending_canonical": [],
            "successor": _successor_row(successor_row), "opened_at": "2026-09-18T00:00:00+00:00",
            "closed": False, "origin_seq": 0, "withheld": [], "minted_forgotten": True,
            "deliveries": {}}


def _gather():
    return pressure.gather_contributors(None)


@pytest.fixture(autouse=True)
def _quiet_gather(monkeypatch):
    monkeypatch.setattr("pressure_loop.pressure_field.build_registry", lambda: {})
    monkeypatch.setattr("memory.taxonomy.config.taxonomy_pressure_enabled", lambda: False)
    monkeypatch.setattr(pressure, "_rollback_event_contributor", lambda: None)


def test_a_successor_the_operator_never_stopped_is_published_and_admitted():
    """The control: nothing stopped this work, so both checks pass it through."""
    _open()

    row = stage_omega_successor(_agent_episode(), _control())

    assert row is not None and row.metadata["agent_job_id"] == JOB
    assert cancellation_reason(row.metadata) is None
    assert row in _gather() and pressure.staged_signals() == (row,)


@pytest.mark.parametrize("stop", ["job_stop", "work_halt"])
def test_a_stop_that_lands_before_publication_stages_nothing(stop):
    """First check: the fold was still thinking about continuing when the stop landed, so
    publication declines and no successor is ever staged."""
    _open()
    if stop == "job_stop":
        jobs.request_stop(JOB, source_cycle_id="note-cycle", arrival_seq=1)
    else:
        replies.request_work_halt(JOB_CYCLE)

    assert build_omega_successor(_agent_episode(), _control()) is None
    assert stage_omega_successor(_agent_episode(), _control()) is None
    assert pressure.staged_signals() == ()


@pytest.mark.parametrize("stop", ["job_stop", "job_stop_settled", "work_halt"])
def test_a_stop_that_lands_after_staging_is_retired_at_admission(stop):
    """Second check: the successor was already staged when the stop landed — the case a
    publication check alone cannot see. The gather retires it loudly instead of igniting it,
    and the retirement takes the admitted slot so replaying the staging resurrects nothing.

    ``job_stop_settled`` is the whole operator-stop lifecycle as production writes it: the
    stop is requested, the batch acknowledges it at its next boundary, and the job settles
    ``stopped``. The acknowledgement is what remains once the batch is gone, so it is
    authority in its own right.
    """
    _open()
    row = stage_omega_successor(_agent_episode(), _control())
    assert row is not None and row in _gather(), "runnable until the operator stops it"

    if stop == "job_stop":
        jobs.request_stop(JOB, source_cycle_id="note-cycle", arrival_seq=1)
    elif stop == "job_stop_settled":
        revision = jobs.request_stop(JOB, source_cycle_id="note-cycle", arrival_seq=1)
        jobs.acknowledge_job_stop(JOB, through_revision=revision.revision)
        jobs.finish_job(JOB, status="stopped", summary="stopped at the operator's request")
        assert jobs.get_job(JOB).stop_receipts, "the batch left the acknowledgement behind"
    else:
        replies.request_work_halt(JOB_CYCLE)

    gathered = _gather()

    assert row not in gathered and pressure.staged_signals() == ()
    assert row.metadata["successor_id"] in pressure._read_successors()["admitted"]


def test_a_job_the_host_stopped_at_boot_does_not_cancel_its_continuation():
    """The distinction the stop doctrine has to make: WHO stopped the job.

    An operator's agent task keeps its job open between batches (``auto_loop.execute.run``:
    "the job is its workflow"), so every restart finds one running and
    ``reconcile_jobs_at_boot`` stops all of them — its own contract being that "unfinished
    work continues through Omega successors", and ``open_job`` reopening a non-running job for
    exactly that successor. Reading the ``stopped`` STATUS as the operator's stop retired that
    continuation instead, so one restart cancelled work nobody stopped and the accepted
    decisions it carried were never acted on (review 2026-09-19, found through T14).

    The real boot path is used, not a hand-written status, so this cannot pass against a
    record production would never write.
    """
    from pressure_loop.omega_successor import unrunnable_successor_reason

    _open()
    row = stage_omega_successor(_agent_episode(), _control())
    assert row is not None and row in _gather(), "runnable before the restart"

    jobs.reconcile_jobs_at_boot(())

    job = jobs.get_job(JOB)
    assert job.status == "stopped", "boot stopped the job whose writer died"
    assert not job.revisions and not job.stop_receipts,         "and left no operator stop behind, because there was none"
    assert cancellation_state(row.metadata) == ("", ""), "not stopped, and not ambiguous either"
    assert unrunnable_successor_reason(row.metadata) is None
    assert row in _gather(), "so the continuation still runs after the restart"
    assert pressure.staged_signals() == (row,), "still staged"
    assert row.metadata["successor_id"] not in pressure._read_successors()["admitted"],         "and not retired into the admitted map, which is where a stop would have put it"


def test_publication_continues_through_a_boot_stop_but_not_through_an_operator_stop():
    """The same distinction at the FIRST check: a fold that concludes after the restart may
    still publish its continuation, while an operator stop still declines it."""
    _open()
    jobs.reconcile_jobs_at_boot(())
    assert jobs.get_job(JOB).status == "stopped"

    assert build_omega_successor(_agent_episode(), _control()) is not None,         "the host's boot stop is not the operator's stop"

    jobs.request_stop(JOB, source_cycle_id="note-cycle", arrival_seq=1)
    assert build_omega_successor(_agent_episode(), _control()) is None,         "the operator's own stop still declines publication"


def test_a_halt_of_a_different_turn_leaves_this_continuation_alone():
    """The halt is addressed: another turn's stop is not this work's."""
    _open()
    row = stage_omega_successor(_agent_episode(), _control())

    replies.request_work_halt("some-other-bubble")

    assert row in _gather()


def test_a_queued_continuation_is_retired_at_actual_admission():
    """Reviewer's probe: the successor was ENQUEUED while capacity was unavailable, and the
    stop landed after that. The gather retires the STAGED row, but the FIFO holds its own
    copy, which reached ``dispatch_cascade`` on a later tick and resurrected the stopped work.
    Cancellation is rechecked at actual admission and the exact leased item is consumed —
    releasing its lease would only postpone it, since the row keeps its queue position."""
    _open()
    row = stage_omega_successor(_agent_episode(), _control())
    assert cq.enqueue_input(row, operator_activity=False)
    assert [c.metadata["successor_id"] for c in _queue()] == [row.metadata["successor_id"]]
    jobs.request_stop(JOB, source_cycle_id="note-cycle", arrival_seq=1)

    admitted, deferred = loop._lease_runnable_input(2)

    assert (admitted, deferred) == (None, False), "not handed to the cascade, not deferred"
    assert _queue() == [], "the queued copy is gone, not left for the next tick"
    assert row.metadata["successor_id"] in pressure._read_successors()["admitted"]


def test_a_stopped_continuation_never_reaches_dispatch_cascade(monkeypatch):
    """The same probe through the real admission pass: the gather retires the staged row, and
    the dispatcher is still never called with the queued copy."""
    import pressure_loop.pipeline_dispatch as dispatch

    _open()
    row = stage_omega_successor(_agent_episode(), _control())
    assert cq.enqueue_input(row, operator_activity=False)
    jobs.request_stop(JOB, source_cycle_id="note-cycle", arrival_seq=1)
    dispatched: list = []
    monkeypatch.setattr(dispatch, "dispatch_cascade", lambda *a, **k: dispatched.append(a))
    monkeypatch.setattr(dispatch, "free_pipeline_ids", lambda: [2])
    monkeypatch.setattr(dispatch, "raise_pending_fatal", lambda *_a, **_k: None)
    monkeypatch.setattr(loop, "_log_tick_summary", lambda *_a, **_k: None)

    admitted = loop._run_pressure_tick_body(SimpleNamespace(episode_store=None, shared_state=None))

    assert dispatched == [] and not admitted
    assert _queue() == [] and pressure.staged_signals() == ()


def test_a_queued_continuation_nobody_stopped_is_still_admitted():
    _open()
    row = stage_omega_successor(_agent_episode(), _control())
    assert cq.enqueue_input(row, operator_activity=False)

    admitted, deferred = loop._lease_runnable_input(2)

    assert admitted is not None and not deferred
    assert admitted.metadata["successor_id"] == row.metadata["successor_id"]


def test_a_stopped_browser_handoff_is_retired_before_its_lane_can_run(monkeypatch):
    from pressure_loop.specialist_lane_handoff import stage_operator_lane_work
    from pressure_loop.operator_chat_responses import get_lane_updates

    monkeypatch.setattr(jobs, "active_jobs", lambda: ())
    receipt = stage_operator_lane_work(
        lane="browser", request="Open the catalog", cycle_id="bubble",
        thread_id=THREAD, route_context="the current request",
        browser_workflow={"workflow_id": "workflow-stop"},
    )
    pressure.record_work_stopped(["bubble"], reason="operator stop")

    assert loop._lease_runnable_input(1) == (None, False)
    assert _queue() == []
    assert mail.all_mail() == []
    updates = get_lane_updates("bubble")["updates"]
    assert updates[-1]["status"] == "failed"
    assert "stopped before it started" in updates[-1]["text"]
    assert not cq.handoff_accepted(receipt["handoff_id"])


def test_the_operators_stop_outlives_the_execution_flag_it_raised():
    """Reviewer's probe: a JOBLESS continuation — no job record to carry the stop — was
    staged, its lineage halted, and the halt's process-local flag then lapsed with its 900s
    TTL; the successor gathered successfully. The authority is durable now, so the flag
    lapsing changes nothing: that flag is an execution signal, never the authority."""
    chat_row = stage_omega_successor(_chat_episode(), _control(intent="ASK_OPERATOR"))
    assert chat_row is not None and not chat_row.metadata.get("agent_job_id")

    halt_operator_work(CHAT_CYCLE)
    replies.clear_work_halt(CHAT_CYCLE)                # the TTL lapses; the record does not

    assert not replies.work_halt_requested(CHAT_CYCLE)
    assert pressure.work_stopped(CHAT_CYCLE)
    assert cancellation_reason(chat_row.metadata)
    assert chat_row not in _gather() and pressure.staged_signals() == ()


def test_the_durable_stop_forbids_republication_when_nothing_is_left_in_memory():
    """A transaction replay after a restart must not publish the continuation of stopped work,
    with the flag gone and the job record no longer saying stop."""
    _open()
    halt_operator_work(JOB_CYCLE)
    replies.clear_work_halt(JOB_CYCLE)
    jobs.finish_job(JOB, status="completed", summary="done")

    assert pressure.work_stopped(JOB_CYCLE)
    assert build_omega_successor(_agent_episode(), _control()) is None


def _many_later_stops(count=320):
    """Enough stops to have evicted anything a bounded recent-history tail kept."""
    pressure.record_work_stopped([f"bubble-{n}" for n in range(count)], reason="operator stop")


@pytest.mark.parametrize("where", ["staged", "queued", "in_flight", "open_transaction"])
def test_a_stop_record_outlives_later_stops_while_its_work_is_outstanding(where):
    """Reviewer's probes (2026-09-18): a bounded tail evicted the record of work that was
    merely WAITING — for capacity in the FIFO, or for the fold that would publish it — and the
    continuation was then admitted. Retention is by lifecycle now, over every store outstanding
    work waits in, and a size cap never drops a record."""
    _open()
    _outstanding_work(where)
    pressure.record_work_stopped([JOB_CYCLE], reason="operator stop")

    _many_later_stops()

    assert pressure.work_stopped(JOB_CYCLE), f"{where} work still has its stop"
    assert cancellation_reason(_agent_episode().pressure["contributors"][0]["detail"] |
                               {"original_cycle_id": JOB_CYCLE})


def test_a_queued_continuation_is_still_refused_after_later_stops():
    """The whole point of retaining it: the FIFO copy the gather could not reach is refused at
    admission, however many stops have been recorded since."""
    _open()
    row = stage_omega_successor(_agent_episode(), _control())
    assert cq.enqueue_input(row, operator_activity=False)
    halt_operator_work(JOB_CYCLE)
    assert _gather() == [] or row not in _gather()      # the staged row is retired
    replies.clear_work_halt(JOB_CYCLE)
    _many_later_stops()

    assert loop._lease_runnable_input(2) == (None, False)
    assert _queue() == []


def test_an_unpublished_fold_of_stopped_work_still_cannot_publish_after_later_stops():
    """Reviewer's probe: the fold was HALTED before it published, so no staged row and no
    transaction record protected its stop. Its pipeline envelope is the one trace of it, and
    that is what keeps the record."""
    from core import pipeline_state

    _open()
    halt_operator_work(JOB_CYCLE)
    replies.clear_work_halt(JOB_CYCLE)
    pipeline_state._write(2, state="INFERENCE", cycle_id=JOB_CYCLE,
                          work_kind="agent_continuation")
    _many_later_stops()

    assert pressure.work_stopped(JOB_CYCLE)
    assert build_omega_successor(_agent_episode(), _control()) is None


def test_a_continued_folds_root_lineage_survives_auto():
    """Reviewer's probe: the CONTINUATION's own cycle is its successor id, and ``mark_acted``
    drops the continuation that carried the root lineage — so after AUTO nothing named the
    turn the work descends from, one unrelated stop pruned its cancellation, and the running
    fold could publish again. The envelope keeps that lineage through foldback now."""
    from core import pipeline_state
    from pressure_loop.contributors import EpisodeIgnition

    _open()
    successor = stage_omega_successor(_agent_episode(), _control())
    assert successor is not None
    assert cq.enqueue_input(successor, operator_activity=False)
    leased = cq.lease_next_input(2)
    assert leased is not None
    ignition = EpisodeIgnition(pipeline_id=2, work_kind="agent_continuation", episode_id="ep-next",
                               aggregate_pressure=1.0, opened_by="omega_successor",
                               dominant_contributor=leased, contributors=(leased,),
                               trajectory_snapshot=None)
    pipeline_state.mark_ready(2, ignition)
    envelope = pipeline_state.read_envelope(2)
    assert envelope.cycle_id == successor.metadata["successor_id"], "its own identity"
    assert envelope.origin_cycle_id == JOB_CYCLE, "and the turn it continues"

    # AUTO acts: the continuation is dropped, the lineage is not.
    acted = pipeline_state.mark_acted(SimpleNamespace(pipeline_id=2, episode_id="ep-next"))
    assert acted.continuation is None and acted.origin_cycle_id == JOB_CYCLE

    halt_operator_work(JOB_CYCLE)
    replies.clear_work_halt(JOB_CYCLE)
    assert cq.acknowledge_input(leased, 2), "the queued copy is consumed by the running Agent episode"
    assert pressure.release_staged(successor), "and its staging released at open"
    _many_later_stops()

    assert pressure.work_stopped(JOB_CYCLE), "the running fold's root stop is still on record"
    assert build_omega_successor(_agent_episode(), _control()) is None


def test_an_envelope_written_before_the_stop_doctrine_still_loads():
    from core.pipeline_state import PipelineEnvelope

    envelope = PipelineEnvelope.from_dict(
        {"pipeline_id": 0, "queue_id": "", "cycle_id": "c", "job_id": "", "work_kind": "autonomous",
         "state": "INFERENCE", "phase": "", "active_episode_id": "", "continuation": None})

    assert envelope.origin_cycle_id == ""


def test_a_record_is_dropped_once_its_work_is_provably_finished():
    """Not kept forever either: with nothing staged, queued, in flight or open for that turn,
    the record has nothing left to govern."""
    _open()
    pressure.record_work_stopped([JOB_CYCLE], reason="operator stop")
    assert pressure.work_stopped(JOB_CYCLE)

    _many_later_stops()

    assert pressure.work_stopped(JOB_CYCLE) == "", "finished work keeps no stop record"


def test_retention_keeps_everything_when_completion_cannot_be_established(monkeypatch):
    """Undecidable is never "finished": a store that cannot be read must not be taken as proof
    that the work it holds is over."""
    _open()
    pressure.record_work_stopped([JOB_CYCLE], reason="operator stop")

    def unreadable():
        raise OSError("queue unreadable")

    monkeypatch.setattr(cq, "peek_inputs", unreadable)
    _many_later_stops()

    assert pressure.work_stopped(JOB_CYCLE)


def test_an_explicit_stop_control_reports_when_the_stop_is_not_durable(monkeypatch):
    """The HUD stop control is execution control; ordinary chat text still enters Global A."""
    _open()
    monkeypatch.setattr(pressure, "_write_successors",
                        lambda _data: (_ for _ in ()).throw(OSError("ledger unwritable")))

    result = halt_operator_work(JOB_CYCLE)

    assert result["stop_recorded"] is False
    assert result["unresolved_stops"] == [JOB_CYCLE] or JOB_CYCLE in result["unresolved_stops"]
    assert _revision_kinds() == ["stop"], "and the halt itself still landed"


def test_an_explicit_stop_control_reports_a_durable_stop():
    _open()

    result = halt_operator_work(JOB_CYCLE)

    assert result["stop_recorded"] is True and result["unresolved_stops"] == []


def test_a_stop_whose_write_failed_is_reported_retained_and_retried(monkeypatch):
    """Reviewer's probe: the ledger write failed for jobless work, the halt returned normally,
    and once the transient signal lapsed the queued successor was admitted. Suppressing the
    exception must not imply durable cancellation: the halt still lands, the failure is
    REPORTED, the stop holds in this process, and the tick retries it until it is durable."""
    row = stage_omega_successor(_chat_episode(), _control(intent="ASK_OPERATOR"))
    assert cq.enqueue_input(row, operator_activity=False)
    real_write = pressure._write_successors
    monkeypatch.setattr(pressure, "_write_successors",
                        lambda _data: (_ for _ in ()).throw(OSError("ledger unwritable")))

    halted = halt_operator_work(CHAT_CYCLE)

    assert halted["stop_recorded"] is False, "the halt does not claim a durability it lacks"
    assert halted["unresolved_stops"] == [CHAT_CYCLE]
    assert pressure.unresolved_stops() == (CHAT_CYCLE,)
    # Authority in this process even with nothing on disk, and the flag it raised is not it.
    replies.clear_work_halt(CHAT_CYCLE)
    assert pressure.work_stopped(CHAT_CYCLE) and cancellation_reason(row.metadata)
    assert loop._lease_runnable_input(0) == (None, False)
    assert _queue() == []

    monkeypatch.setattr(pressure, "_write_successors", real_write)
    assert pressure.retry_unresolved_stops()["recorded"] == (CHAT_CYCLE,)

    assert pressure.unresolved_stops() == ()
    assert pressure._read_successors()["stopped"][CHAT_CYCLE]["reason"] == "operator stop"
    assert pressure.work_stopped(CHAT_CYCLE), "now durable, and it reads the same either way"


def test_the_pressure_tick_is_what_retries_an_unresolved_stop(monkeypatch):
    import pressure_loop.pipeline_dispatch as dispatch

    _open()
    real_write = pressure._write_successors
    broken = {"on": True}
    monkeypatch.setattr(pressure, "_write_successors",
                        lambda data: (_ for _ in ()).throw(OSError("ledger unwritable"))
                        if broken["on"] else real_write(data))
    halt_operator_work(JOB_CYCLE)
    assert pressure.unresolved_stops() == (JOB_CYCLE,)
    broken["on"] = False
    monkeypatch.setattr(dispatch, "free_pipeline_ids", lambda: [])
    monkeypatch.setattr(dispatch, "raise_pending_fatal", lambda *_a, **_k: None)
    monkeypatch.setattr(loop, "_log_tick_summary", lambda *_a, **_k: None)

    loop._run_pressure_tick_body(SimpleNamespace(episode_store=None, shared_state=None))

    assert pressure.unresolved_stops() == ()
    assert pressure.work_stopped(JOB_CYCLE)


def _unreadable_job(monkeypatch):
    """Only the job lookup fails — the narrowest way to make cancellation state unknown.

    Counts the failed reads, so a caller that re-selects the deferred row pipeline after
    pipeline is visible rather than merely slower.
    """
    real_get = jobs.get_job
    broken = {"on": True, "reads": 0}

    def get_job(job_id):
        if broken["on"] and job_id == JOB:
            broken["reads"] += 1
            raise OSError("job record unreadable")
        return real_get(job_id)

    monkeypatch.setattr(jobs, "get_job", get_job)
    return broken


def _unreadable_ledger(monkeypatch):
    """Switchable, never undone: ``monkeypatch.undo()`` would also revert the isolation this
    suite installs and point the reads at the live ledger."""
    real_read = pressure._read_successors
    broken = {"on": True}

    def read():
        if broken["on"]:
            raise OSError("ledger unreadable")
        return real_read()

    monkeypatch.setattr(pressure, "_read_successors", read)
    return broken


def test_an_unreadable_stop_record_is_unknown_not_a_cancellation(monkeypatch):
    """Reviewer's probe (2026-09-18): a read failure became a cancellation REASON, and
    admission acted on it — acknowledging and removing a queued item nobody had cancelled. The
    state is three-valued now: unknown says so, and only a confirmed stop is a reason."""
    _unreadable_ledger(monkeypatch)

    standing, why = pressure.stop_standing(JOB_CYCLE)

    assert (standing, "cannot be read" in why) == ("unknown", True)
    assert pressure.work_stopped(JOB_CYCLE) == "", "unknown is not a confirmed stop"
    assert cancellation_reason({"original_cycle_id": JOB_CYCLE}) is None
    assert cancellation_state({"original_cycle_id": JOB_CYCLE})[0] == "unknown"


def test_unknown_cancellation_defers_the_queued_work_without_consuming_it(monkeypatch):
    """The queue-only probe: with no staged row left, the FIFO copy is the only record of this
    work. An unreadable ledger must not destroy it — the lease is released, the row keeps its
    position, and the next tick judges it again."""
    _open()
    row = stage_omega_successor(_agent_episode(), _control())
    assert cq.enqueue_input(row, operator_activity=False)
    assert pressure.release_staged(row), "queued and no longer staged"
    broken = _unreadable_ledger(monkeypatch)

    assert loop._lease_runnable_input(2) == (None, True), \
        "deferred, which is not an empty queue"

    assert [c.metadata["successor_id"] for c in _queue()] == [row.metadata["successor_id"]], \
        "the work nobody cancelled is still there"
    broken["on"] = False
    admitted, deferred = loop._lease_runnable_input(2)
    assert admitted is not None and not deferred
    assert admitted.metadata["successor_id"] == row.metadata["successor_id"]


def test_unknown_cancellation_retires_nothing_at_the_gather(monkeypatch):
    """The same rule for the staged row: retiring on a guess is as destructive as admitting."""
    _open()
    row = stage_omega_successor(_agent_episode(), _control())
    assert row is not None

    from pressure_loop.omega_successor import unrunnable_successor_reason

    real_read = pressure._read_successors
    monkeypatch.setattr(pressure, "_read_successors",
                        lambda: (_ for _ in ()).throw(OSError("ledger unreadable")))
    assert unrunnable_successor_reason(row.metadata) is None
    monkeypatch.setattr(pressure, "_read_successors", real_read)

    assert pressure.staged_signals() == (row,)


def test_unknown_cancellation_does_not_suppress_a_confirmed_retirement(monkeypatch):
    """Unknown means "do not act on the STOP question", not "do not act at all": a browser
    successor whose workflow is gone can never run, whatever the stop record says, and keeping
    it staged for ever would be its own kind of leak."""
    from pressure_loop.omega_successor import unrunnable_successor_reason

    _unreadable_ledger(monkeypatch)
    browse = {"directive_intent": "BROWSE", "original_cycle_id": JOB_CYCLE,
              "browser_workflow_id": "wf-gone", "browser_workflow_revision": 1}

    reason = unrunnable_successor_reason(browse)

    assert reason and "wf-gone" in reason


def test_a_retirement_the_ledger_refuses_does_not_end_the_tick(monkeypatch):
    """Reviewer's probe: with a staged successor to retire and an unwritable ledger, the gather
    raised — out of the very tick that retries the unresolved stop, so the promised later
    retries never came. The row is simply not admitted, and both retries come back."""
    import pressure_loop.pipeline_dispatch as dispatch

    _open()
    row = stage_omega_successor(_agent_episode(), _control())
    assert row is not None
    jobs.request_stop(JOB, source_cycle_id="note-cycle", arrival_seq=1)   # it must be retired
    real_write = pressure._write_successors
    broken = {"on": True}
    monkeypatch.setattr(pressure, "_write_successors",
                        lambda data: (_ for _ in ()).throw(OSError("ledger unwritable"))
                        if broken["on"] else real_write(data))
    pressure._UNRESOLVED_STOPS["some-halted-turn"] = "operator stop"
    monkeypatch.setattr(dispatch, "free_pipeline_ids", lambda: [])
    monkeypatch.setattr(dispatch, "raise_pending_fatal", lambda *_a, **_k: None)
    monkeypatch.setattr(loop, "_log_tick_summary", lambda *_a, **_k: None)

    admitted = loop._run_pressure_tick_body(SimpleNamespace(episode_store=None, shared_state=None))

    assert not admitted, "the stopped row is not admitted"
    assert pressure.unresolved_stops() == ("some-halted-turn",), "still owed, not lost"
    broken["on"] = False

    loop._run_pressure_tick_body(SimpleNamespace(episode_store=None, shared_state=None))

    assert pressure.unresolved_stops() == (), "the later retry the loop promised"
    assert pressure.staged_signals() == (), "and the retirement caught up too"


def test_unknown_cancellation_keeps_the_publication_obligation(monkeypatch):
    """Reviewer's probe (2026-09-18): declining publication on UNKNOWN finalized the
    uncertainty — the record said published, ``staging: "none"``, the injection withheld — and
    no retry could undo it, so a transient unreadable file lost valid work for good. The
    successor is published as usual now; it simply cannot be ADMITTED until the stop question
    is answered."""
    _open()
    broken = _unreadable_job(monkeypatch)

    row = stage_omega_successor(_agent_episode(), _control())

    assert row is not None, "the obligation is kept, durably, as the staged row"
    assert pressure.staged_signals() == (row,)
    assert cq.enqueue_input(row, operator_activity=False)
    assert loop._lease_runnable_input(2) == (None, True), \
        "and it waits: unknown is not permission to run"
    broken["on"] = False

    admitted, _deferred = loop._lease_runnable_input(2)

    assert admitted is not None, "once the state reads again, the work continues"


def test_a_stop_confirmed_after_an_unknown_publication_still_retires_it(monkeypatch):
    """The other half: publishing under uncertainty resurrects nothing, because admission
    answers the stop question before anything runs."""
    _open()
    broken = _unreadable_job(monkeypatch)
    row = stage_omega_successor(_agent_episode(), _control())
    assert row is not None
    assert cq.enqueue_input(row, operator_activity=False)
    broken["on"] = False
    jobs.request_stop(JOB, source_cycle_id="note-cycle", arrival_seq=1)

    assert loop._lease_runnable_input(2) == (None, False)

    assert _queue() == [] and pressure.staged_signals() == ()


def test_a_deferred_continuation_holds_only_its_agent_lane(monkeypatch):
    """Unknown cancellation holds Agent while Chat continues on its own lane.

    The deferred row is not consumed or re-selected. It remains in the Agent FIFO while a Chat
    turn uses its independent free pipeline. Once the job record is readable, the Agent row
    proceeds in its original lane.
    """
    import pressure_loop.pipeline_dispatch as dispatch
    from pressure_loop.contributors import from_operator_message

    _open()
    deferred_row = stage_omega_successor(_agent_episode(), _control())
    assert deferred_row is not None and cq.enqueue_input(deferred_row, operator_activity=False)
    turn = from_operator_message({"message": "what is the status?", "cycle_id": "bubble-next",
                                  "arrival_seq": 99})
    assert cq.enqueue_input(turn, operator_activity=True)
    broken = _unreadable_job(monkeypatch)
    dispatched: list = []
    monkeypatch.setattr(dispatch, "dispatch_cascade",
                        lambda _store, _ignition, contributor, **_kw: dispatched.append(contributor))
    monkeypatch.setattr(dispatch, "free_pipeline_ids", lambda: [0, 1, 2, 3])
    monkeypatch.setattr(dispatch, "raise_pending_fatal", lambda *_a, **_k: None)
    monkeypatch.setattr(loop, "_log_tick_summary", lambda *_a, **_k: None)

    admitted = loop._run_pressure_tick_body(SimpleNamespace(episode_store=None, shared_state=None))

    assert admitted, "Chat has an independent free lane"
    assert [item.metadata.get("cycle_id") for item in dispatched] == ["bubble-next"]
    assert [c.metadata["successor_id"] for c in _queue()
            if c.type == "omega_successor"] == [deferred_row.metadata["successor_id"]], \
        "the deferred continuation is still there, untouched"
    # Two judgements in this pass and no more: the gather judging the staged row, and
    # admission judging its queued copy once. A pass that re-selected the deferred row for
    # every free pipeline would read it four times over.
    assert broken["reads"] == 2, "judged once at the gather and once at admission"

    # The next tick reevaluates the Agent row and that lane proceeds.
    broken["on"] = False
    assert loop._run_pressure_tick_body(
        SimpleNamespace(episode_store=None, shared_state=None)) is True
    assert [item.metadata.get("cycle_id") for item in dispatched] == [
        "bubble-next", deferred_row.metadata["successor_id"]
    ]


def test_an_unreadable_job_record_is_unknown_but_a_missing_one_is_not():
    """A job that is GONE never stopped this work; a job that cannot be READ decides nothing."""
    assert cancellation_state({"agent_job_id": "job-that-never-existed"}) == ("", "")

    _open()
    real_get = jobs.get_job

    def unreadable(job_id):
        raise OSError("job record unreadable")

    jobs.get_job = unreadable
    try:
        standing, why = cancellation_state({"agent_job_id": JOB})
    finally:
        jobs.get_job = real_get
    assert (standing, "cannot be read" in why) == ("unknown", True)


def test_a_ledger_written_before_the_stop_doctrine_still_loads():
    """Store upgrade: a T8-era ledger records no stopped work rather than failing to read."""
    import json

    path = pressure._successor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pending": {}, "admitted": {}, "transactions": {},
                                "transaction_horizon": 0}), encoding="utf-8")

    assert pressure._read_successors()["stopped"] == {}
    assert pressure.work_stopped("bubble-anything") == ""


def test_a_missing_job_record_is_not_a_cancellation():
    """Failure-soft: a job record that cannot be read stops nothing by itself — the successor
    is judged by the rules that own it, not by an unreadable file."""
    assert cancellation_reason({"agent_job_id": "job-that-never-existed"}) is None

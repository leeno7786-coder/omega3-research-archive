"""Chat Global A's specialist route becomes durable work on its own lane FIFO."""

import json
import re

import pytest

from pressure_loop import cognition_queue as cq
from pressure_loop import specialist_lane_handoff as handoff
from pressure_loop.contributors import make_contributor
from global_loop import intersections as mail

WORKFLOW = {"workflow_id": "workflow-1", "objective": "Find current pricing", "status": "active"}


def _stage(lane="browser", **overrides):
    kwargs = {
        "lane": lane,
        "request": "Find current pricing",
        "cycle_id": "operator-cycle",
        "thread_id": "conversation-1",
        "route_context": "the current request",
        "browser_workflow": WORKFLOW if lane == "browser" else None,
    }
    kwargs.update(overrides)
    return handoff.stage_operator_lane_work(**kwargs)


def test_handoff_identity_is_deterministic_hex_bound_to_lane_thread_and_cycle():
    one = handoff.lane_work_handoff_id("browser", "conversation-1", "operator-cycle")
    assert re.fullmatch(r"[0-9a-f]{64}", one)
    assert one == handoff.lane_work_handoff_id("browser", "conversation-1", "operator-cycle")
    assert len({
        one,
        handoff.lane_work_handoff_id("agent", "conversation-1", "operator-cycle"),
        handoff.lane_work_handoff_id("browser", "conversation-2", "operator-cycle"),
        handoff.lane_work_handoff_id("browser", "conversation-1", "operator-cycle-2"),
    }) == 4


@pytest.mark.parametrize(
    ("lane", "pipeline_id", "intent"),
    [("browser", 1, "BROWSE"), ("agent", 2, "RESPOND")],
)
def test_specialist_work_is_admitted_to_its_own_lane_with_the_chat_turn_identity(lane, pipeline_id, intent):
    receipt = _stage(lane)

    assert receipt["status"] == "queued"
    assert receipt["handoff_id"] == handoff.lane_work_handoff_id(lane, "conversation-1", "operator-cycle")
    assert cq.lease_next_input(0) is None, "specialist work never waits in the Chat FIFO"
    work = cq.lease_next_input(pipeline_id)
    assert work is not None
    assert (work.source, work.type) == ("omega", "lane_work_dispatch")
    assert work.lane == lane
    meta = work.metadata
    assert meta["lane"] == lane
    assert meta["directive_intent"] == intent
    assert meta["operator_route"] == lane
    assert meta["lane_work_handoff"] is True
    assert meta["original_cycle_id"] == "operator-cycle"
    assert meta["original_objective"] == "Find current pricing"
    assert meta["thread_id"] == "conversation-1"
    assert meta["message"] == "Find current pricing"
    assert meta["operator_route_context"] == "the current request"
    assert meta["queue_id"] == receipt["handoff_id"]
    if lane == "browser":
        assert meta["browser_workflow_id"] == "workflow-1"
    else:
        assert "browser_workflow_id" not in meta
    assert cq.handoff_receipts()[receipt["handoff_id"]]["origin_ref"] == receipt["origin_ref"]
    assert "operator-cycle" in receipt["origin_ref"]


def test_selected_skill_is_persisted_in_agent_mail_and_fifo_metadata():
    receipt = _stage("agent", selected_skill="repo-helper")

    [work] = cq.peek_inputs()
    assert work.metadata["directive_intent"] == "USE_SKILL"
    assert work.metadata["selected_skill"] == "repo-helper"
    claimed = mail.claim_mail(
        delivery_point="omega_reason", lane="agent", thread_id="conversation-1",
        episode_id="agent-skill-fold", lease_s=60,
    )
    assert claimed[0]["payload"]["selected_skill"] == "repo-helper"
    assert claimed[0]["mail_id"] == receipt["mail_id"]


def test_the_admitted_handoff_survives_a_restart():
    receipt = _stage("agent")
    cq._reset_cache_for_tests()

    [work] = cq.peek_inputs()
    assert work.metadata["original_cycle_id"] == "operator-cycle"
    assert cq.handoff_accepted(receipt["handoff_id"])


def test_specialist_handoff_pins_ordered_prompt_mail_before_chat_ack(monkeypatch):
    monkeypatch.setattr("auto_loop.execute.job_control.active_jobs", lambda: ())

    receipt = _stage("browser")
    claimed = mail.claim_mail(
        delivery_point="omega_reason", lane="browser", thread_id="conversation-1",
        episode_id="browser-fold", lease_s=60,
    )

    assert receipt["mail_id"] == claimed[0]["mail_id"]
    assert claimed[0]["purpose"] == "specialist_lane_handoff"
    assert claimed[0]["payload"] == {
        "lane": "browser",
        "text": "Find current pricing",
        "route_context": "the current request",
        "handoff_id": receipt["handoff_id"],
        "original_cycle_id": "operator-cycle",
        "run_memory": {"status": "not_applicable", "evidence": []},
        "browser_workflow_id": "workflow-1",
    }


def test_coding_handoff_attaches_host_resolved_run_memory(monkeypatch):
    from types import SimpleNamespace

    captured = {}
    run = SimpleNamespace(job_id="job-a", cycle_id="agent-owner-cycle",
                          thread_id="conversation-1", status="running",
                          objective="Update the parser to Rust",
                          project_root="J:/workspace/project", applied_revision=4)
    monkeypatch.setattr("auto_loop.execute.job_control.active_jobs", lambda: (run,))

    def recall(**kwargs):
        captured.update(kwargs)
        return {
            "kind": "run_scoped_recall", "tool": "remember_active_run", "status": "verified",
            "anchor": {"job_id": "job-a", "project_root": run.project_root,
                       "thread_id": run.thread_id, "instruction_revision": 4},
            "evidence": [{"memory_id": "mem-code-1", "coord": [8, 1],
                          "title": "Existing parser", "excerpt": "Use the parser module.",
                          "source_citation": {"source_id": "src-1"},
                          "source_provenance": {"source_id": "src-1"},
                          "run_provenance": {"job_id": "job-a"}, "score": 2.0}],
        }

    monkeypatch.setattr("auto_loop.mril_tools.run_scoped.recall_for_active_run", recall)

    receipt = _stage("agent", request="Switch this module to Rust")
    claimed = mail.claim_mail(
        delivery_point="omega_reason", lane="agent", thread_id="conversation-1",
        episode_id="agent-fold", lease_s=60, job_id="job-a",
    )

    assert captured == {
        "job_id": "job-a", "thread_id": "conversation-1", "instruction_revision": 4,
        "operator_update": "Switch this module to Rust", "allowed_lobes": (8, 11), "limit": 8,
    }
    assert claimed[0]["payload"]["run_memory"]["evidence"][0]["memory_id"] == "mem-code-1"
    assert claimed[0]["payload"]["run_memory"]["anchor"]["job_id"] == "job-a"
    assert claimed[0]["payload"]["run_memory"]["recall_method"] == "remember_active_run"
    assert "tool" not in claimed[0]["payload"]["run_memory"]
    [work] = cq.peek_inputs()
    assert work.metadata["job_cycle_id"] == "agent-owner-cycle"
    assert work.metadata["original_objective"] == "Update the parser to Rust"
    assert receipt["mail_id"] == claimed[0]["mail_id"]
    admitted = cq.lease_next_input(2)
    assert cq.acknowledge_input(admitted, 2) is True
    monkeypatch.setattr("auto_loop.mril_tools.run_scoped.recall_for_active_run",
                        lambda **_kwargs: pytest.fail("retry repeated MRIL recall"))
    monkeypatch.setattr(mail, "pin_mail", lambda **_kwargs: pytest.fail("retry repinned mail"))
    retry = _stage("agent", request="Switch this module to Rust")
    assert retry["status"] == "already_accepted"


def test_a_running_agent_fold_absorbs_its_queued_update_before_the_mail_is_retired(monkeypatch):
    """The active run owns the update through Omega mail; its queued copy must not run again."""
    monkeypatch.setattr("auto_loop.execute.job_control.active_jobs", lambda: ())
    active_episode_input = make_contributor("omega", "test_active_agent_episode", 1.0, {
        "lane": "agent", "cycle_id": "active-agent-cycle", "thread_id": "conversation-1",
    })
    assert cq.enqueue_input(active_episode_input, operator_activity=False) is True
    active = cq.lease_next_input(2)
    assert active is not None

    receipt = _stage("agent", request="Switch this module to Rust")

    prepared = cq.prepare_handoff_mail_absorption([receipt["handoff_id"]])

    assert prepared == (receipt["handoff_id"],)
    queued = next(row for row in cq.peek_inputs() if row.metadata.get("handoff_id") == receipt["handoff_id"])
    assert queued.metadata["mail_absorption_pending"] is True
    assert cq.owns_input_lease(active, 2), "the active fold retains its lane lease"

    assert cq.complete_handoff_mail_absorption([receipt["handoff_id"]]) == (receipt["handoff_id"],)
    cq._reset_cache_for_tests()
    queued = next(row for row in cq.peek_inputs() if row.metadata.get("handoff_id") == receipt["handoff_id"])
    assert queued.metadata["mail_absorbed"] is True
    assert "mail_absorption_pending" not in queued.metadata


def _large_verified_run_memory(monkeypatch):
    """The real MRIL shape, including source proof and long portable project paths."""
    from types import SimpleNamespace

    anchor = {
        "job_id": "job-a", "thread_id": "conversation-1", "instruction_revision": 4,
        "project_root": "J:/" + "portable-project/" * 12,
    }
    run = SimpleNamespace(**anchor, cycle_id="original-agent-cycle", status="running",
                          objective="Update the parser", applied_revision=4)
    monkeypatch.setattr("auto_loop.execute.job_control.active_jobs", lambda: (run,))
    evidence = []
    for index in range(8):
        citation = {
            "source_id": f"src-{index}", "source_type": "memory.dev_artifact_intake",
            "source_session_id": "original-agent-episode",
            "source_session_timestamp": "2026-09-28T12:00:00+00:00",
            "span_start": 0, "span_end": 1600,
            "span_hash": "a" * 64, "source_content_hash": "b" * 64,
        }
        evidence.append({
            "memory_id": f"coding-memory-{index}", "coord": [11, 1, index],
            "title": f"Verified build observation {index}",
            "excerpt": ('<code> & "quoted" ' * 100)[:1600],
            "source_citation": citation,
            "source_provenance": {**citation, "author": "promotion:original-agent-episode",
                                  "projection_receipt_required": True},
            "run_provenance": dict(anchor), "score": float(8 - index),
        })
    context = {
        "kind": "run_scoped_recall", "tool": "remember_active_run", "status": "verified",
        "anchor": anchor, "query": "update the current run", "allowed_lobes": [8, 11],
        "evidence": evidence,
    }
    monkeypatch.setattr("auto_loop.mril_tools.run_scoped.recall_for_active_run",
                        lambda **_kwargs: context)
    return context


def test_large_verified_run_context_keeps_the_ranked_prefix_that_fits_exact_mail(monkeypatch):
    from copy import deepcopy

    from omega_loop.episode import prompts
    from omega_loop.episode.packet import omega_mail_fits
    from omega_loop.episode.types import bounded_mail

    context = _large_verified_run_memory(monkeypatch)
    original = deepcopy(context)
    instruction = 'Change the language to Rust; retain <schema> & "column order".'
    receipt = _stage("agent", request=instruction, route_context="the complete prior discussion")

    [row] = mail.pending_mail()
    selected = row["payload"]["run_memory"]
    count = len(selected["evidence"])
    assert 0 < count < len(context["evidence"])
    assert selected["status"] == "verified"
    assert selected["anchor"] == context["anchor"]
    assert selected["evidence"] == context["evidence"][:count], "proof and excerpts stay intact"
    assert selected["evidence_selection"] == {
        "retrieved_count": 8, "included_count": count,
        "omitted_memory_ids": [one["memory_id"] for one in context["evidence"][count:]],
        "reason": "omega_prompt_capacity",
    }
    assert row["payload"]["text"] == instruction
    assert row["payload"]["route_context"] == "the complete prior discussion"
    assert row["provenance"] == [f"memory:{one['memory_id']}" for one in selected["evidence"]]
    assert len(prompts.render_cross_lane_block(bounded_mail([row]))) <= prompts.CROSS_LANE_MAIL_BLOCK_CHARS
    larger = deepcopy(row)
    larger_context = larger["payload"]["run_memory"]
    larger_context["evidence"] = context["evidence"][:count + 1]
    larger_context["evidence_selection"].update(
        included_count=count + 1,
        omitted_memory_ids=[one["memory_id"] for one in context["evidence"][count + 1:]],
    )
    larger["provenance"] = [f"memory:{one['memory_id']}" for one in larger_context["evidence"]]
    assert not omega_mail_fits([larger]), "selection retains the longest fitting ranked prefix"
    assert context == original, "prompt composition must not alter verified retrieval output"
    [work] = cq.peek_inputs()
    assert work.metadata["agent_job_id"] == "job-a"
    assert work.metadata["job_cycle_id"] == "original-agent-cycle"
    assert work.metadata["original_objective"] == "Update the parser"
    assert receipt["run_memory_status"] == "verified"


def test_budgeted_run_context_retry_keeps_the_durable_selection_after_queue_failure(monkeypatch):
    _large_verified_run_memory(monkeypatch)
    real_enqueue = cq.enqueue_handoff

    def refused(*_args, **_kwargs):
        raise OSError("queue admission unavailable")

    monkeypatch.setattr(cq, "enqueue_handoff", refused)
    with pytest.raises(OSError, match="queue admission unavailable"):
        _stage("agent", request="Use Rust")
    [original] = mail.pending_mail()
    assert original["payload"]["run_memory"]["evidence_selection"]["included_count"] > 0
    assert cq.peek_inputs() == ()

    cq._reset_cache_for_tests()
    mail.reset_cache_for_tests()
    monkeypatch.setattr(cq, "enqueue_handoff", real_enqueue)
    monkeypatch.setattr(handoff, "_active_run_memory", lambda *_args: pytest.fail(
        "retry must use the already verified and durably selected run context"
    ))
    assert _stage("agent", request="Use Rust")["status"] == "queued"
    [recovered] = mail.pending_mail()
    assert recovered["payload"] == original["payload"]
    assert recovered["provenance"] == original["provenance"]


def test_run_context_that_cannot_fit_one_record_reports_all_omitted_without_empty_recall(monkeypatch):
    context = _large_verified_run_memory(monkeypatch)
    context["evidence"] = context["evidence"][:2]
    context["evidence"][0]["source_provenance"]["author"] = "large-source-" * 1200
    _stage("agent", request="Use Rust")

    [row] = mail.pending_mail()
    selected = row["payload"]["run_memory"]
    assert selected["status"] == "verified", "retrieval succeeded; prompt selection is separate"
    assert selected["evidence"] == []
    assert selected["evidence_selection"] == {
        "retrieved_count": 2, "included_count": 0,
        "omitted_memory_ids": [one["memory_id"] for one in context["evidence"]],
        "reason": "omega_prompt_capacity",
    }
    assert row["provenance"] == []


def test_request_that_exceeds_mail_capacity_without_evidence_still_fails_explicitly(monkeypatch):
    _large_verified_run_memory(monkeypatch)
    with pytest.raises(ValueError, match="specialist_lane_mail_exceeds_omega_prompt_capacity"):
        _stage("agent", request="instruction " * 2000)
    assert mail.pending_mail() == []
    assert cq.peek_inputs() == ()


@pytest.mark.parametrize(("lane", "pipeline_id"), [("browser", 1), ("agent", 2)])
def test_active_specialist_mail_ack_prevents_the_same_update_from_starting_another_run(
    monkeypatch, lane, pipeline_id,
):
    from types import SimpleNamespace

    from omega_loop.episode.packet import acknowledge_omega_mail
    from pressure_loop import loop

    monkeypatch.setattr("auto_loop.execute.job_control.active_jobs", lambda: ())
    if lane == "agent":
        monkeypatch.setattr(handoff, "_active_run_memory",
                            lambda *_args: ("job-a", "job-cycle", "Update parser", {
                                "status": "verified", "evidence": [],
                            }))
    active_input = make_contributor("omega", "test_active_agent_episode", 1.0, {
        "lane": lane, "cycle_id": "active-lane-cycle", "thread_id": "conversation-1",
    })
    assert cq.enqueue_input(active_input, operator_activity=False) is True
    active = cq.lease_next_input(pipeline_id)
    receipt = _stage(lane, request="Switch this module to Rust")
    claim_kwargs = {
        "delivery_point": "omega_reason", "lane": lane, "thread_id": "conversation-1",
        "episode_id": "active-lane-fold", "lease_s": 60,
    }
    if lane == "agent":
        claim_kwargs["job_id"] = "job-a"
    claimed = mail.claim_mail(**claim_kwargs)
    assert len(claimed) == 1
    episode = SimpleNamespace(
        episode_id="active-lane-fold",
        global_={"omega_mail": {
            "disposition": "bound",
            "thread_id": "conversation-1",
            "receipts": [{
                "mail_id": claimed[0]["mail_id"],
                "lease_id": claimed[0]["lease_id"],
                "generation": claimed[0]["generation"],
            }],
            "mail": [claimed[0]],
        }},
    )

    assert acknowledge_omega_mail(episode) == 1
    assert cq.handoff_receipts()[receipt["handoff_id"]]["mail_acknowledged"] is True
    assert cq.acknowledge_input(active, pipeline_id) is True
    monkeypatch.setattr("pressure_loop.omega_successor.cancellation_state",
                        lambda _metadata: pytest.fail("absorbed update started a second run"))

    assert loop._lease_runnable_input(pipeline_id) == (None, False)
    assert cq.peek_inputs() == ()
    assert receipt["handoff_id"] not in cq.handoff_receipts()


def test_a_handoff_already_leased_as_the_current_lane_episode_is_not_absorbed(monkeypatch):
    monkeypatch.setattr("auto_loop.execute.job_control.active_jobs", lambda: ())
    receipt = _stage("agent", request="Switch this module to Rust")
    current = cq.lease_next_input(2)
    assert current.metadata["handoff_id"] == receipt["handoff_id"]

    prepared = cq.prepare_handoff_mail_absorption([receipt["handoff_id"]])

    assert prepared == ()
    assert current.metadata.get("mail_absorbed") is None


def test_absorbed_handoff_is_acknowledged_without_running_a_second_episode(monkeypatch):
    from pressure_loop import loop

    monkeypatch.setattr("auto_loop.execute.job_control.active_jobs", lambda: ())
    receipt = _stage("agent", request="Switch this module to Rust")
    assert cq.prepare_handoff_mail_absorption([receipt["handoff_id"]]) == (receipt["handoff_id"],)
    assert cq.complete_handoff_mail_absorption([receipt["handoff_id"]]) == (receipt["handoff_id"],)
    monkeypatch.setattr("pressure_loop.omega_successor.cancellation_state",
                        lambda _metadata: pytest.fail("absorbed work reached execution admission"))

    assert loop._lease_runnable_input(2) == (None, False)
    assert cq.peek_inputs() == ()


def test_retry_while_an_absorbed_copy_waits_in_the_lane_is_already_accepted(monkeypatch):
    monkeypatch.setattr("auto_loop.execute.job_control.active_jobs", lambda: ())
    first = _stage("agent", request="Switch this module to Rust")
    cq.prepare_handoff_mail_absorption([first["handoff_id"]])
    cq.complete_handoff_mail_absorption([first["handoff_id"]])
    cq.mark_handoff_mail_acknowledged([first["handoff_id"]])
    assert first["handoff_id"] in cq.handoff_receipts()
    monkeypatch.setattr(mail, "pin_mail", lambda **_kwargs: pytest.fail("retry repinned acknowledged mail"))

    retry = _stage("agent", request="Switch this module to Rust")

    assert retry["status"] == "already_accepted"
    assert retry["handoff_id"] == first["handoff_id"]
    assert len(cq.peek_inputs()) == 1


def test_pending_handoff_absorption_replays_mail_or_retires_after_mail_deletion(monkeypatch):
    from pressure_loop import loop

    monkeypatch.setattr("auto_loop.execute.job_control.active_jobs", lambda: ())
    receipt = _stage("agent", request="Switch this module to Rust")
    cq.prepare_handoff_mail_absorption([receipt["handoff_id"]])
    monkeypatch.setattr(mail, "all_mail", lambda: [{
        "mail_id": receipt["mail_id"], "status": "ready",
    }])
    monkeypatch.setattr("pressure_loop.omega_successor.cancellation_state",
                        lambda _metadata: (False, "not_stopped"))

    pending = loop._lease_runnable_input(2)

    assert pending[0].metadata["handoff_id"] == receipt["handoff_id"]
    assert pending[1] is False
    assert pending[0].metadata["mail_absorption_pending"] is True
    cq.release_input_lease(2)

    monkeypatch.setattr(mail, "all_mail", lambda: [])

    assert loop._lease_runnable_input(2) == (None, False)
    assert cq.peek_inputs() == ()


def test_mail_ack_receipt_retires_atomically_when_its_lane_item_is_acknowledged(monkeypatch):
    monkeypatch.setattr("auto_loop.execute.job_control.active_jobs", lambda: ())
    receipt = _stage("agent", request="Switch this module to Rust")
    current = cq.lease_next_input(2)

    assert cq.mark_handoff_mail_acknowledged([receipt["handoff_id"]]) == (receipt["handoff_id"],)
    assert receipt["handoff_id"] in cq.handoff_receipts()
    assert cq.acknowledge_input(current, 2) is True
    assert receipt["handoff_id"] not in cq.handoff_receipts()


def test_boot_keeps_acknowledged_mail_receipt_while_absorbed_lane_row_remains(monkeypatch):
    from pressure_loop import held_instruction_handoff as held

    monkeypatch.setattr("auto_loop.execute.job_control.active_jobs", lambda: ())
    receipt = _stage("agent", request="Switch this module to Rust")
    cq.prepare_handoff_mail_absorption([receipt["handoff_id"]])
    cq.complete_handoff_mail_absorption([receipt["handoff_id"]])
    cq.mark_handoff_mail_acknowledged([receipt["handoff_id"]])
    cq._reset_cache_for_tests()
    monkeypatch.setattr(mail, "all_mail", lambda: [])
    monkeypatch.setattr(held, "hand_off_orphaned_instructions", lambda: [])

    assert held.reconcile_handoffs_at_boot() == []
    assert receipt["handoff_id"] in cq.handoff_receipts()


def test_a_retry_after_the_work_was_consumed_is_already_accepted_not_admitted_twice(monkeypatch):
    first = _stage("browser")
    work = cq.lease_next_input(1)
    assert cq.acknowledge_input(work, 1) is True
    monkeypatch.setattr(mail, "pin_mail", lambda **_kwargs: pytest.fail("retry repinned mail"))

    retry = _stage("browser")

    assert retry["status"] == "already_accepted"
    assert retry["handoff_id"] == first["handoff_id"]
    assert cq.peek_inputs() == ()


def test_queue_handoff_rejects_missing_cycle_before_persisting():
    contributor = make_contributor("omega", "lane_work_dispatch", 1.0, {"lane": "browser"})
    with pytest.raises(ValueError, match="cycle_required"):
        cq.enqueue_handoff(contributor, handoff_id="a" * 64, origin_ref="source")
    assert cq.peek_inputs() == ()
    assert cq.handoff_receipts() == {}


def test_boot_reconciliation_retires_specialist_receipts_after_mail_was_acknowledged(monkeypatch):
    from pressure_loop import held_instruction_handoff as held

    live_id, retired_id = "c" * 64, "d" * 64
    receipts = {
        live_id: {"origin_ref": "operator_cycle:live"},
        retired_id: {"origin_ref": "operator_cycle:retired"},
    }
    forgotten = []
    monkeypatch.setattr(mail, "all_mail", lambda: [{
        "mail_id": "live-mail",
        "purpose": "specialist_lane_handoff", "payload": {"handoff_id": live_id},
    }])
    monkeypatch.setattr(cq, "handoff_receipts", lambda: receipts)
    monkeypatch.setattr(cq, "forget_handoff_receipts",
                        lambda identities: forgotten.extend(identities) or len(identities))
    monkeypatch.setattr(held, "hand_off_orphaned_instructions", lambda: [])

    assert held.reconcile_handoffs_at_boot() == []
    assert forgotten == [retired_id]


def test_the_receipt_is_bounded_to_identities():
    receipt = _stage("browser", request="x" * 10_000)
    assert set(receipt) == {
        "status", "handoff_id", "lane", "original_cycle_id", "thread_id", "origin_ref",
        "mail_id", "run_memory_status", "active_job_id",
    }
    assert all(len(str(value)) <= 512 for value in receipt.values())


@pytest.mark.parametrize(
    "overrides",
    [
        {"lane": "chat"},
        {"lane": "window"},
        {"lane": "BROWSER"},
        {"lane": None},
        {"request": "   "},
        {"request": None},
        {"cycle_id": ""},
        {"cycle_id": None},
        {"thread_id": " "},
        {"thread_id": 7},
        {"route_context": None},
        {"browser_workflow": {"objective": "no identity"}},
        {"browser_workflow": {"workflow_id": ""}},
        {"browser_workflow": "workflow-1"},
    ],
)
def test_invalid_handoffs_fail_before_anything_is_admitted(overrides):
    with pytest.raises(ValueError):
        _stage(**{"lane": "browser", **overrides})
    assert cq.peek_inputs() == ()
    assert cq.handoff_receipts() == {}


def test_agent_work_rejects_a_browser_workflow():
    with pytest.raises(ValueError):
        _stage("agent", browser_workflow=WORKFLOW)
    assert cq.peek_inputs() == ()


def test_mail_is_durable_before_queue_admission_and_queue_failure_propagates(monkeypatch):
    calls = []

    def refused(contributor, *, handoff_id, origin_ref):
        calls.append((contributor, handoff_id, origin_ref))
        raise RuntimeError("queue refused")

    monkeypatch.setattr(cq, "enqueue_handoff", refused)

    with pytest.raises(RuntimeError, match="queue refused"):
        _stage("browser")
    assert len(calls) == 1
    contributor, handoff_id, origin_ref = calls[0]
    assert handoff_id == handoff.lane_work_handoff_id("browser", "conversation-1", "operator-cycle")
    assert "arrival_seq" not in contributor.metadata, "the gate stamps arrival, not the caller"
    assert "ttl" not in contributor.metadata and "expires_at" not in contributor.metadata
    [mail_row] = mail.pending_mail()
    assert mail_row["payload"]["handoff_id"] == handoff_id


def test_unadmitted_specialist_mail_cannot_be_claimed(monkeypatch):
    def refused(*_args, **_kwargs):
        raise OSError("queue unavailable")

    monkeypatch.setattr(cq, "enqueue_handoff", refused)
    with pytest.raises(OSError, match="queue unavailable"):
        _stage()

    assert mail.claim_mail(
        delivery_point="omega_reason", lane="browser", thread_id="conversation-1",
        episode_id="already-running-browser", lease_s=60,
    ) == []
    assert len(mail.pending_mail()) == 1


@pytest.mark.parametrize("recovery", ["retry", "boot"])
def test_failed_admission_recovers_frozen_job_context_after_reload(monkeypatch, recovery):
    from pressure_loop import held_instruction_handoff as held

    real_enqueue = cq.enqueue_handoff
    monkeypatch.setattr(handoff, "_active_run_memory", lambda *_args: (
        "original-job", "original-job-cycle", "Original objective",
        {"status": "verified", "evidence": [{"memory_id": "original-memory"}]},
    ))

    def refused(*_args, **_kwargs):
        raise OSError("queue unavailable")

    monkeypatch.setattr(cq, "enqueue_handoff", refused)
    with pytest.raises(OSError, match="queue unavailable"):
        _stage("agent", selected_skill="repo-helper")
    [original_mail] = mail.pending_mail()
    assert cq.peek_inputs() == ()

    cq._reset_cache_for_tests()
    mail.reset_cache_for_tests()
    monkeypatch.setattr(cq, "enqueue_handoff", real_enqueue)
    monkeypatch.setattr(handoff, "_active_run_memory", lambda *_args: pytest.fail(
        "recovery must not recall a different active job or replace the original context"
    ))
    if recovery == "retry":
        assert _stage("agent", selected_skill="repo-helper")["status"] == "queued"
    else:
        held.reconcile_handoffs_at_boot()

    [restored] = cq.peek_inputs()
    assert restored.metadata["agent_job_id"] == "original-job"
    assert restored.metadata["job_cycle_id"] == "original-job-cycle"
    assert restored.metadata["original_objective"] == "Original objective"
    assert restored.metadata["selected_skill"] == "repo-helper"
    assert restored.metadata["mail_id"] == original_mail["mail_id"]
    assert cq.handoff_accepted(restored.metadata["handoff_id"])
    [recovered_mail] = mail.pending_mail()
    assert recovered_mail["payload"] == original_mail["payload"]
    claimed = mail.claim_mail(
        delivery_point="omega_reason", lane="agent", thread_id="conversation-1",
        job_id="original-job", episode_id="recovered-agent", lease_s=60,
    )
    assert [row["mail_id"] for row in claimed] == [original_mail["mail_id"]]


@pytest.mark.parametrize("store,committed", [
    ("mail", False), ("mail", True), ("queue", False), ("queue", True),
])
def test_partial_store_writes_recover_without_duplicate_work(monkeypatch, store, committed):
    from pressure_loop import held_instruction_handoff as held

    if store == "mail":
        real_write = mail._write

        def fail_write(state):
            if committed:
                real_write(state)
            raise OSError("mail publication interrupted")

        target, name = mail, "_write"
    else:
        real_write = cq._write_encoded

        def fail_write(path, encoded):
            if json.loads(encoded)["queue"]:
                if committed:
                    real_write(path, encoded)
                raise OSError("queue publication interrupted")
            real_write(path, encoded)

        target, name = cq, "_write_encoded"
    monkeypatch.setattr(target, name, fail_write)
    with pytest.raises(OSError, match="publication interrupted"):
        _stage()
    monkeypatch.setattr(target, name, real_write)
    cq._reset_cache_for_tests()
    mail.reset_cache_for_tests()
    held.reconcile_handoffs_at_boot()
    if store == "mail" and not committed:
        assert cq.peek_inputs() == ()
        assert mail.pending_mail() == []
        return
    [restored] = cq.peek_inputs()
    assert restored.metadata["message"] == "Find current pricing"
    assert cq.handoff_accepted(restored.metadata["handoff_id"])
    assert len(mail.pending_mail()) == 1
    held.reconcile_handoffs_at_boot()
    assert len(cq.peek_inputs()) == 1
    assert _stage()["status"] == "already_accepted"


def test_unfinished_specialist_admission_retries_through_existing_pressure_sweep(monkeypatch):
    from pressure_loop import held_instruction_handoff as held

    real_enqueue = cq.enqueue_handoff

    def refused(*_args, **_kwargs):
        raise OSError("queue temporarily unavailable")

    monkeypatch.setattr(cq, "enqueue_handoff", refused)
    with pytest.raises(OSError, match="temporarily unavailable"):
        _stage()
    assert held.seconds_until_retry() is not None
    monkeypatch.setattr(cq, "enqueue_handoff", real_enqueue)
    assert held.retry_if_due(now=10 ** 18) == []
    assert len(cq.peek_inputs()) == 1


def test_mail_publication_error_after_commit_is_recovered_without_restart(monkeypatch):
    from pressure_loop import held_instruction_handoff as held

    real_write = mail._write

    def interrupted(state):
        real_write(state)
        raise OSError("publication completed before interruption")

    monkeypatch.setattr(mail, "_write", interrupted)
    with pytest.raises(OSError, match="publication completed"):
        _stage()
    monkeypatch.setattr(mail, "_write", real_write)
    held.hand_off_orphaned_instructions()
    assert len(cq.peek_inputs()) == 1
    assert len(mail.pending_mail()) == 1


def _settle_specialist(receipt):
    work = cq.lease_next_input(1)
    assert cq.acknowledge_input(work, 1)
    claimed = mail.claim_mail(
        delivery_point="omega_reason", lane="browser", thread_id="conversation-1",
        episode_id="settling-browser", lease_s=60,
    )
    assert mail.acknowledge_mail(claimed, episode_id="settling-browser") == 1
    cq.mark_handoff_mail_acknowledged([receipt["handoff_id"]])
    assert cq.handoff_receipts() == {}


def test_settled_specialist_retries_are_already_accepted_without_recalling_new_context(monkeypatch):
    first = _stage()
    _settle_specialist(first)
    cq._reset_cache_for_tests()
    mail.reset_cache_for_tests()
    monkeypatch.setattr(handoff, "_active_run_memory", lambda *_args: pytest.fail(
        "settled handoff replay must not recall current run context"
    ))
    retry = _stage()
    assert retry["status"] == "already_accepted"
    assert retry["handoff_id"] == first["handoff_id"]
    assert cq.peek_inputs() == ()
    assert mail.pending_mail() == []


def test_settled_specialist_replay_evidence_does_not_consume_active_pin_capacity(monkeypatch):
    monkeypatch.setattr(mail, "_MAX_MINTED", 2)
    for index in range(6):
        receipt = _stage(cycle_id=f"operator-cycle-{index}")
        _settle_specialist(receipt)
    mail.reset_cache_for_tests()
    assert _stage(cycle_id="operator-cycle-0")["status"] == "already_accepted"
    assert cq.peek_inputs() == ()


def test_unacknowledged_specialist_mail_still_obeys_active_pin_capacity(monkeypatch):
    monkeypatch.setattr(mail, "_MAX_MINTED", 2)
    _stage(cycle_id="pending-1")
    _stage(cycle_id="pending-2")
    with pytest.raises(ValueError, match="pending minted mail capacity"):
        _stage(cycle_id="pending-3")
    assert len(cq.peek_inputs()) == 2
    assert len(mail.pending_mail()) == 2


def test_failed_specialist_recovery_does_not_block_other_lanes_or_held_instructions(monkeypatch):
    from pressure_loop import held_instruction_handoff as held

    real_enqueue = cq.enqueue_handoff

    def unavailable(*_args, **_kwargs):
        raise OSError("initial outage")

    monkeypatch.setattr(cq, "enqueue_handoff", unavailable)
    with pytest.raises(OSError, match="initial outage"):
        _stage("browser")
    with pytest.raises(OSError, match="initial outage"):
        _stage("browser", cycle_id="later-browser-cycle")
    with pytest.raises(OSError, match="initial outage"):
        _stage("agent", cycle_id="agent-cycle")
    held_mail = mail.pin_mail(
        thread_id="held-conversation", delivery_point="omega_reason",
        purpose="operator_instruction", requirement="required", source_actor="operator",
        source_episode_id="held-cycle", target_job_id="missing-job",
        payload={"text": "Keep the earlier instructions"},
        origin={"cycle_id": "held-cycle", "ingress_class": "operator_command"},
    )

    def browser_refused(contributor, **kwargs):
        if contributor.metadata.get("lane") == "browser":
            raise ValueError("browser recovery remains invalid")
        return real_enqueue(contributor, **kwargs)

    monkeypatch.setattr(cq, "enqueue_handoff", browser_refused)
    with pytest.raises(ExceptionGroup, match="specialist handoff recovery") as caught:
        held.hand_off_orphaned_instructions()
    assert len(caught.value.exceptions) == 1, "later same-address mail must wait"
    assert any("browser recovery remains invalid" in str(exc) for exc in caught.value.exceptions)
    queued = cq.peek_inputs()
    assert len(queued) == 2
    assert {item.lane for item in queued} == {"agent", "chat"}
    assert any(item.metadata.get("message") == "Keep the earlier instructions" for item in queued)
    assert held_mail["mail_id"] not in {row["mail_id"] for row in mail.pending_mail()}
    assert not any(item.lane == "browser" for item in queued)
    assert held.seconds_until_retry() is not None

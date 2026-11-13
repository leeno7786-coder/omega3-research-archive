"""The chat and agent lanes intersect.

Operator design 2026-09-15: a message sent while a build runs belongs to the build, not to a
second episode racing it; the agent can ask one numbered question that pauses the task until the
operator answers; and it can declare the task finished, which ends the loop.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


def _ctx(tmp_path, **overrides):
    from auto_loop.execute import brain_skill_executor as executor

    fields = dict(job_id="build", cycle_id="bubble-1", episode_id="ep-1",
                  trusted_roots=(tmp_path,))
    fields.update(overrides)
    return executor.ExecutionContext(tmp_path, "build a basic todo app using flask", 10, **fields)


def _record(tool, *, result=None, ok=True):
    return {
        "tool_call_id": f"call-{tool}",
        "tool_name": tool,
        "args": {},
        "status": "success" if ok else "failure",
        "output": "",
        "error": "",
        "touched_files": [],
        "job_id": "build",
        "result": dict(result or {}),
    }


@pytest.fixture
def receipt_job(tmp_path):
    from auto_loop.execute import job_control as jobs

    return jobs.open_job(
        "build", "bubble-1", "build a basic todo app using flask", tmp_path,
        project_root=tmp_path,
    )


def test_ask_operator_records_a_numbered_question_for_the_operator(tmp_path):
    from auto_loop.execute import brain_skill_executor as executor
    from pressure_loop.system_messages import current_outreach

    result = executor._ask_operator(
        {"question": "Which storage should the todo list use?",
         "options": ["in-memory", "SQLite file"]},
        _ctx(tmp_path),
    )

    assert result.ok, result.error
    assert result.meta["options"] == ["in-memory", "SQLite file"]
    assert "paused" in result.output
    pending = current_outreach()
    assert pending is not None and pending["kind"] == "agent_question"
    assert pending["question_text"] == "Which storage should the todo list use?"
    assert pending["meta"]["options"] == ["in-memory", "SQLite file"]
    assert pending["meta"]["job_id"] == "build"


def test_ask_operator_needs_a_question(tmp_path):
    from auto_loop.execute import brain_skill_executor as executor

    assert not executor._ask_operator({"options": ["a", "b"]}, _ctx(tmp_path)).ok


@pytest.mark.usefixtures("receipt_job")
def test_the_receipt_reports_the_question_and_its_options():
    from auto_loop.execute.run import _agent_run_receipt

    receipt = _agent_run_receipt(
        [_record("ask_operator", result={"agent_question": "Which storage?",
                                         "options": ["in-memory", "SQLite"]})],
        operator_route="agent", text="", stop_reason="",
    )

    assert receipt["status"] == "ask_operator"
    assert receipt["question"] == "Which storage?"
    assert receipt["options"] == ["in-memory", "SQLite"]


@pytest.mark.usefixtures("receipt_job")
def test_the_receipt_reports_a_finish_claim():
    from auto_loop.execute.run import _agent_run_receipt

    receipt = _agent_run_receipt(
        [_record("write_file"),
         _record("finish_task", result={"agent_finished": True, "summary": "app.py and templates"})],
        operator_route="agent", text="", stop_reason="",
    )

    assert receipt["status"] == "done_claimed"
    assert receipt["summary"] == "app.py and templates"


@pytest.mark.usefixtures("receipt_job")
def test_the_receipt_carries_the_operators_held_words():
    from auto_loop.execute.run import _agent_run_receipt

    receipt = _agent_run_receipt(
        [_record("write_file")], operator_route="agent", text="", stop_reason="",
        operator_notes=["Job revision 7: also add a delete button"],
    )

    assert receipt["operator_notes"] == ["Job revision 7: also add a delete button"]


# Held chat no longer becomes the running job's instruction: cross-lane T7 made ordinary chat
# during a build operator_instruction MAIL, never a job revision the batch in flight reads.
# Its boundary tests live in tests/pressure_loop/test_operator_mail_ingress.py.


def test_an_answer_to_the_agents_question_is_pinned_to_the_agent_lane(tmp_path):
    from auto_loop.execute import job_control as jobs
    from pressure_loop.operator_chat import _agent_answer_metadata

    jobs.open_job("build", "original-build-cycle", "build the app", tmp_path)
    pending = {"kind": "agent_question", "question_id": "q1",
               "question_text": "Which storage?", "episode_id": "agent-episode",
               "meta": {"options": ["in-memory", "SQLite"], "job_id": "build"}}

    metadata = _agent_answer_metadata("option 2", pending)

    assert metadata["operator_route_pin"] == "CODING_AGENT"
    assert metadata["job_id"] == "build"
    assert metadata["job_cycle_id"] == "original-build-cycle"
    assert metadata["agent_question_answer"] == "option 2"
    assert metadata["system_query_id"] == "q1"
    assert metadata["system_query_episode_id"] == "agent-episode"


def test_a_pinned_route_never_reaches_the_router(monkeypatch):
    from global_loop.intent import route as route_module

    def no_router():
        raise AssertionError("a pinned route must not pay for a router call")

    monkeypatch.setattr(route_module, "get_router", no_router, raising=False)
    monkeypatch.setattr("global_loop.intent.router.get_router", no_router)
    monkeypatch.setattr("global_loop.intent.context.previous_action", lambda: "")
    ignition = SimpleNamespace(dominant_contributor=SimpleNamespace(
        source="operator", metadata={"message": "option 2", "operator_route_pin": "CODING_AGENT"}))

    resolved = route_module.resolve_operator_route(SimpleNamespace(episode_id="e1"), ignition)

    assert resolved.route == "CODING_AGENT" and resolved.lane == "agent"
    assert resolved.source == "operator"


def test_a_task_paused_on_its_question_keeps_its_job(tmp_path):
    from auto_loop.execute import job_control as jobs
    from cognition_chain import _close_settled_agent_job

    project = tmp_path / "todo_app"
    project.mkdir(parents=True)
    jobs.open_job("build", "bubble-1", "build a todo app", tmp_path, project_root=project)
    episode = SimpleNamespace(auto={"outputs": {"agent_run": {"job_id": "build"}}})

    _close_settled_agent_job(episode, SimpleNamespace(decision="ASK_OPERATOR", synthesis="which storage?"))
    assert jobs.get_job("build").status == "running", "the operator's answer resumes this job"

    _close_settled_agent_job(episode, SimpleNamespace(decision="DONE", synthesis="built it"))
    assert jobs.get_job("build").status == "completed"


def test_the_bounded_receipt_keeps_the_options_and_the_notes():
    from omega_loop.episode.types import bounded_agent_run

    bounded = bounded_agent_run({
        "status": "ask_operator", "steps": 1, "actions": [], "project_files": [],
        "touched_files": [], "options": ["in-memory", "SQLite"],
        "operator_notes": ["also add a delete button"],
    })

    assert bounded["options"] == ["in-memory", "SQLite"]
    assert bounded["operator_notes"] == ["also add a delete button"]


def test_both_new_tools_are_agent_lane_tools_in_every_mode():
    from auto_loop.execute.brain_skill_executor import lane_tool_names, operator_tool_specs

    assert {"ask_operator", "finish_task"} <= lane_tool_names("agent")
    read_only = {spec["function"]["name"] for spec in
                 operator_tool_specs("read_only", operator_route="agent")}
    assert {"ask_operator", "finish_task"} <= read_only, "asking is never a write"


@pytest.fixture
def answer_ingress(monkeypatch, tmp_path):
    from core.shared_state import SharedState
    from global_loop.intent import context
    from pressure_loop import operator_chat

    monkeypatch.setenv("OMEGA_BROWSER_WORKFLOW_PATH", str(tmp_path / "browser-workflow.json"))
    monkeypatch.setattr("guardian.external_ingress.screen_text",
                        lambda text, **kwargs: (text, "allow"))
    context.reset()
    runtime = SimpleNamespace(shared_state=SharedState())

    def send(text="sqlite", *, paused=False, **kwargs):
        monkeypatch.setattr("pressure_loop.pressure_loop_pause.is_pressure_loop_paused",
                            lambda: paused)
        return operator_chat.handle_operator_chat(text, runtime=runtime,
                                                 cycle_id="answer-cycle", **kwargs)

    yield send
    context.reset()


def _pending_agent_question(tmp_path, *, job_id="build", cycle_id="build-cycle",
                            thread_id="build-thread", create_job=True):
    from auto_loop.execute import brain_skill_executor as executor, job_control as jobs

    if job_id and create_job:
        jobs.open_job(job_id, cycle_id, "build the app", tmp_path, thread_id=thread_id)
    result = executor._ask_operator(
        {"question": "Which storage?", "options": ["sqlite", "postgres"]},
        _ctx(tmp_path, job_id=job_id, cycle_id=cycle_id, thread_id=thread_id))
    assert result.ok, result.error
    return result.meta["question_id"]


def _question_records():
    from pressure_loop import system_messages

    system_messages._reset_cache_for_tests()
    return {record["id"]: record for record in system_messages.list_messages()}


def _queued_agent_answer():
    from global_loop.loop import run_global_a
    from pressure_loop import cognition_queue
    from pressure_loop.contributors import EpisodeIgnition

    row = cognition_queue.lease_next_input(0)
    assert row is not None
    assert row.type == "operator_response"
    ignition = EpisodeIgnition(
        episode_id="answer-episode", opened_by=row.type, dominant_contributor=row,
        contributors=(row,), aggregate_pressure=1.0, trajectory_snapshot=None,
        pipeline_id=0, work_kind="operator_turn")
    decision = run_global_a(SimpleNamespace(episode_id="answer-episode", global_={}), ignition)
    assert (decision.intent, decision.omega_lane) == ("RESPOND", "chat")
    return row.metadata


@pytest.mark.parametrize("paused", [False, True])
def test_answer_losing_question_claim_never_queues_as_the_winning_answer(
        answer_ingress, tmp_path, monkeypatch, paused):
    from pressure_loop import cognition_queue, system_messages

    question_id = _pending_agent_question(tmp_path)
    attach = system_messages.attach_chat_answer

    def competing_answer(message_id, response_text, **kwargs):
        attach(message_id, "postgres", answer_episode_id="winning-answer")
        return attach(message_id, response_text, **kwargs)

    monkeypatch.setattr(system_messages, "attach_chat_answer", competing_answer)
    result = answer_ingress(paused=paused, question_id=question_id)

    assert result["ok"] is False and result["queued"] is False
    assert result["error"] == "question_not_pending"
    assert cognition_queue.peek_inputs() == ()
    assert _question_records()[question_id]["response"]["text"] == "postgres"


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("job_state", ["missing", "corrupt"])
@pytest.mark.parametrize("explicit", [False, True])
def test_unavailable_question_job_preserves_unanswered_question(
        answer_ingress, tmp_path, paused, job_state, explicit):
    from auto_loop.execute import job_control as jobs
    from pressure_loop import cognition_queue, system_messages

    question_id = _pending_agent_question(tmp_path, create_job=job_state != "missing")
    if job_state == "corrupt":
        (jobs.jobs_root() / "build.json").write_text("{broken", encoding="utf-8")
    before = _question_records()[question_id]

    result = answer_ingress(paused=paused, **({"question_id": question_id} if explicit else {}))

    assert result["ok"] is False and result["queued"] is False
    assert result["error"] == "agent_question_job_unavailable"
    assert result["cycle_id"] == "answer-cycle"
    assert result["message"] and len(result["message"]) <= 600
    assert ("KeyError" if job_state == "missing" else "JobRecordInvalid") in result["message"]
    assert cognition_queue.peek_inputs() == ()
    assert _question_records()[question_id] == before
    assert system_messages.current_outreach()["question_id"] == question_id


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_question_before_start_job_resumes_agent(answer_ingress, tmp_path, paused, explicit):
    question_id = _pending_agent_question(tmp_path, job_id="")

    result = answer_ingress(paused=paused, **({"question_id": question_id} if explicit else {}))

    assert result["ok"] and result["queued"]
    metadata = _queued_agent_answer()
    assert metadata["thread_id"] == "build-thread"
    assert metadata["system_query_id"] == question_id
    assert metadata["agent_question_answer"] == "sqlite"
    assert not metadata.get("job_id")


@pytest.mark.parametrize("paused", [False, True])
def test_explicit_question_resumes_its_job_without_consuming_newer_question(
        answer_ingress, tmp_path, paused):
    from pressure_loop import system_messages

    question_a = _pending_agent_question(tmp_path, job_id="build-a", cycle_id="cycle-a",
                                         thread_id="thread-a")
    question_b = _pending_agent_question(tmp_path, job_id="build-b", cycle_id="cycle-b",
                                         thread_id="thread-b")
    assert system_messages.current_outreach()["question_id"] == question_b
    before_b = _question_records()[question_b]

    result = answer_ingress(paused=paused, question_id=question_a)

    assert result["ok"] and result["queued"]
    metadata = _queued_agent_answer()
    assert (metadata["job_id"], metadata["job_cycle_id"], metadata["thread_id"]) == (
        "build-a", "cycle-a", "thread-a")
    assert metadata["system_query_id"] == question_a
    records = _question_records()
    assert records[question_b] == before_b
    assert records[question_a]["status"] == "responded"
    assert system_messages.current_outreach()["question_id"] == question_b


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("question_id", ["unknown-question", ""])
def test_unknown_explicit_question_refuses_without_latest_fallback(
        answer_ingress, tmp_path, paused, question_id):
    from pressure_loop import cognition_queue

    _pending_agent_question(tmp_path)
    before = _question_records()

    result = answer_ingress(paused=paused, question_id=question_id)

    assert result["ok"] is False and result["queued"] is False
    assert result["error"] == "question_not_pending"
    assert cognition_queue.peek_inputs() == ()
    assert _question_records() == before


@pytest.mark.parametrize("paused", [False, True])
def test_answered_explicit_question_never_falls_back_to_pending_question(
        answer_ingress, tmp_path, paused):
    from pressure_loop import cognition_queue, system_messages

    question_a = _pending_agent_question(tmp_path, job_id="", thread_id="thread-a")
    _pending_agent_question(tmp_path, job_id="", thread_id="thread-b")
    system_messages.attach_chat_answer(question_a, "postgres", answer_episode_id="first-answer")
    before = _question_records()

    result = answer_ingress(paused=paused, question_id=question_a)

    assert result["error"] == "question_not_pending"
    assert result["ok"] is False and result["queued"] is False
    assert cognition_queue.peek_inputs() == ()
    assert _question_records() == before


@pytest.mark.parametrize("paused", [False, True])
def test_question_job_thread_mismatch_never_consumes_answer(answer_ingress, tmp_path, paused):
    from pressure_loop import cognition_queue

    _pending_agent_question(tmp_path)
    question_id = _pending_agent_question(tmp_path, create_job=False, thread_id="different-thread")
    before = _question_records()

    result = answer_ingress(paused=paused, question_id=question_id)

    assert result["error"] == "agent_question_job_unavailable"
    assert result["ok"] is False and result["queued"] is False
    assert "different threads" in result["message"]
    assert cognition_queue.peek_inputs() == ()
    assert _question_records() == before


@pytest.mark.parametrize("paused", [False, True])
def test_answer_without_question_thread_does_not_author_one(answer_ingress, tmp_path, paused):
    from global_loop.intent.context import remember_action

    question_id = _pending_agent_question(tmp_path, job_id="", thread_id="")
    remember_action("CODING_AGENT", "built something else", thread_id="unrelated-thread",
                    episode_id="unrelated-episode")

    result = answer_ingress("use that", paused=paused, question_id=question_id)

    assert result["ok"] and result["queued"]
    assert not _queued_agent_answer().get("thread_id")


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("text,directive", [
    ("learn about volcanoes", None),
    ("remember that my bicycle is blue", None),
    ("draw a mountain", "IMAGE_CREATE"),
])
def test_ineligible_turn_never_consumes_or_inherits_question(
        answer_ingress, tmp_path, paused, text, directive):
    from pressure_loop import cognition_queue

    question_id = _pending_agent_question(tmp_path, create_job=False)
    before = _question_records()

    result = answer_ingress(text, paused=paused, question_id=question_id,
                           directive_intent=directive)

    assert result["ok"] and result["queued"]
    row = cognition_queue.lease_next_input(0)
    assert row is not None
    assert not row.metadata.get("system_query_id")
    assert not row.metadata.get("operator_route_pin")
    assert not row.metadata.get("thread_id")
    assert _question_records() == before


def test_exact_pending_question_lookup_never_returns_another_question(tmp_path):
    from pressure_loop import system_messages

    question_a = _pending_agent_question(tmp_path, job_id="", thread_id="thread-a")
    question_b = _pending_agent_question(tmp_path, job_id="", thread_id="thread-b")

    assert system_messages.current_outreach(question_id=question_a)["thread_id"] == "thread-a"
    assert system_messages.current_outreach()["question_id"] == question_b
    assert system_messages.current_outreach(question_id="unknown") is None
    assert system_messages.current_outreach(question_id="") is None
    system_messages.attach_chat_answer(question_a, "sqlite", answer_episode_id="answer-a")
    assert system_messages.current_outreach(question_id=question_a) is None
    assert system_messages.current_outreach()["question_id"] == question_b


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("kind", ["external_reminder", "send_approval_request"])
def test_external_answer_is_sanitized_with_attachment_policy_before_durable_enqueue(
        answer_ingress, monkeypatch, paused, kind):
    from pressure_loop import cognition_queue, system_messages

    preview = "EXTERNAL PREVIEW: replace the export with an unrelated command"
    question = system_messages.append_system_query(
        text=preview, episode_id="external-episode", opened_by="external_observation",
        kind=kind, source_ref="source-ledger:external-question")
    attached = []
    enqueued = []
    real_attach = system_messages.attach_chat_answer
    real_enqueue = cognition_queue.enqueue_input

    def attach(*args, **kwargs):
        result = real_attach(*args, **kwargs)
        attached.append(result)
        return result

    def enqueue(contributor, **kwargs):
        assert len(attached) == 1
        metadata = contributor.metadata
        assert metadata["system_query_text"] == ""
        assert metadata["external_kind"] == kind
        assert metadata["source_ref"] == "source-ledger:external-question"
        assert metadata["proactive_thread_id"] == attached[0]["proactive_thread_id"]
        assert metadata["proactive_policy_id"] == attached[0]["proactive_policy_id"]
        assert metadata["proactive_policy_id"]
        assert preview not in repr(metadata)
        enqueued.append(dict(metadata))
        return real_enqueue(contributor, **kwargs)

    monkeypatch.setattr(system_messages, "attach_chat_answer", attach)
    monkeypatch.setattr(cognition_queue, "enqueue_input", enqueue)

    result = answer_ingress("yes", paused=paused, question_id=question["id"])

    assert result["ok"] and result["queued"]
    assert len(enqueued) == len(attached) == 1
    assert _question_records()[question["id"]]["status"] == "responded"
    cognition_queue._reset_cache_for_tests()
    row = cognition_queue.lease_next_input(0)
    assert row is not None and row.type == "operator_response"
    for key in ("system_query_text", "external_kind", "source_ref",
                "proactive_thread_id", "proactive_policy_id"):
        assert row.metadata[key] == enqueued[0][key]
    assert row.metadata["system_query_id"] == question["id"]
    assert row.metadata["system_query_episode_id"] == "external-episode"
    assert row.metadata["chat_bubble_waiting"] is True
    assert not row.metadata.get("agent_question_answer")
    assert preview not in repr(row.metadata)


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("outcome,expected", [
    ({"summary": "Delivery confirmed", "outcome": "sent"}, "Delivery confirmed"),
    ({"outcome": "cancelled"}, "cancelled"),
])
def test_send_answer_serializes_the_actual_attachment_outcome(
        answer_ingress, monkeypatch, paused, outcome, expected):
    from pressure_loop import cognition_queue, system_messages

    question = system_messages.append_system_query(
        text="EXTERNAL SEND PREVIEW", episode_id="send-episode", opened_by="send_approval",
        kind="send_approval_request", source_ref="source-ledger:send-preview")
    real_attach = system_messages.attach_chat_answer
    real_enqueue = cognition_queue.enqueue_input
    attached = []

    def attach(*args, **kwargs):
        result = real_attach(*args, **kwargs)
        assert result is not None
        result = {**result, "send_outcome": dict(outcome)}
        attached.append(result)
        return result

    def enqueue(contributor, **kwargs):
        assert len(attached) == 1
        assert contributor.metadata["send_outcome"] == expected
        return real_enqueue(contributor, **kwargs)

    monkeypatch.setattr(system_messages, "attach_chat_answer", attach)
    monkeypatch.setattr(cognition_queue, "enqueue_input", enqueue)

    result = answer_ingress("yes", paused=paused, question_id=question["id"])

    assert result["ok"] and result["queued"]
    assert len(attached) == 1
    cognition_queue._reset_cache_for_tests()
    row = cognition_queue.lease_next_input(0)
    assert row is not None
    assert row.metadata["send_outcome"] == expected
    assert row.metadata["system_query_text"] == ""
    assert row.metadata["source_ref"] == "source-ledger:send-preview"
    assert row.metadata["proactive_policy_id"] == attached[0]["proactive_policy_id"]

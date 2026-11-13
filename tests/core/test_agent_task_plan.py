"""Planning is the agent's first step, and the plan is what every later episode reads.

Live 2026-09-20: with one tool batch per episode and nothing carried between them but a
raw tool log, a finished todo app was re-read episode after episode until the operator
typed "stop". The task now starts by writing its steps down, clears them as the work
lands, and finishes when the list is clear.
"""

from __future__ import annotations

import json

import pytest

from auto_loop.execute.run import _agent_continuation_prompt, _operator_tools_exchange


def _call(name, args, call_id):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _episode(generate, *, episode, job_id="", session=None, cycle="cycle-plan"):
    return _operator_tools_exchange(
        generate,
        system="sys",
        prompt="build a basic todo app using flask",
        objective="build a basic todo app using flask",
        episode_id=episode,
        max_tokens=100,
        enable_thinking=False,
        timeout=None,
        chat_cycle_id=cycle,
        warnings=[],
        operator_route="agent",
        permission_mode="allow_edits",
        runtime_client=object(),
        job_id=job_id,
        session=session if session is not None
        else {"records": [], "owned": False, "job_id": "", "cursor": 0},
    )


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "workspace"))
    return tmp_path / "workspace"


def _claim_and_plan(steps=("write app.py", "write the template")):
    def generate(**_kwargs):
        return {"text": "", "tool_calls": [
            _call("start_job", {"project_root": "todo_app"}, "c1"),
            _call("task_plan", {"steps": list(steps)}, "p1")]}
    return generate


def test_the_first_episode_writes_the_plan_onto_the_job(workspace) -> None:
    from auto_loop.execute import job_control as jobs

    session = {"records": [], "owned": False, "job_id": "", "cursor": 0}
    _, records, _ = _episode(_claim_and_plan(), episode="ep-plan-1", session=session)

    plan_record = next(row for row in records if row["tool_name"] == "task_plan")
    assert plan_record["status"] == "success", plan_record["error"]
    assert "1. [open] write app.py" in plan_record["output"]
    job = jobs.get_job(session["job_id"])
    assert [(s.step, s.text, s.done) for s in job.plan] == [
        (1, "write app.py", False), (2, "write the template", False)]


def test_mutable_work_without_a_plan_is_refused_and_names_the_tool(workspace) -> None:
    """Mirrors the existing rule that mutable work requires start_job: step one is planning."""
    def generate(**_kwargs):
        return {"text": "", "tool_calls": [
            _call("start_job", {"project_root": "todo_app"}, "c1"),
            _call("write_file", {"path": "todo_app/app.py", "content": "x = 1\n"}, "w1")]}

    _, records, _ = _episode(generate, episode="ep-plan-gate",
                             session={"records": [], "owned": False, "job_id": "", "cursor": 0})

    write = next(row for row in records if row["tool_name"] == "write_file")
    assert write["status"] == "failure", write["output"]
    assert "task_plan" in write["error"], write["error"]
    assert not (workspace / "todo_app" / "app.py").exists(), "nothing was written"


def test_work_proceeds_once_the_plan_exists(workspace) -> None:
    session = {"records": [], "owned": False, "job_id": "", "cursor": 0}
    _episode(_claim_and_plan(), episode="ep-plan-2", session=session)
    job_id = session["job_id"]

    from auto_loop.execute import job_control as jobs

    jobs.leave_writer(job_id, "cycle-plan")

    def work(**_kwargs):
        return {"text": "", "tool_calls": [
            _call("write_file", {"path": "todo_app/app.py", "content": "x = 1\n"}, "w1"),
            _call("task_plan", {"completed": [1]}, "p2")]}

    _, records, _ = _episode(work, episode="ep-plan-3", job_id=job_id,
                             session={"records": [], "owned": False, "job_id": "", "cursor": 0})

    assert next(row for row in records if row["tool_name"] == "write_file")["status"] == "success"
    job = jobs.get_job(job_id)
    assert {s.step: s.done for s in job.plan} == {1: True, 2: False}
    assert jobs.plan_is_complete(job) is False


def test_a_later_episode_reads_the_plan_not_just_the_tool_log(workspace) -> None:
    """The continuation brief led with a raw dump of earlier outputs, which is an invitation
    to re-read. It now leads with what is done and what is left."""
    from auto_loop.execute import job_control as jobs

    session = {"records": [], "owned": False, "job_id": "", "cursor": 0}
    _episode(_claim_and_plan(), episode="ep-plan-4", session=session)
    job_id = session["job_id"]
    jobs.complete_plan_steps(job_id, [1])

    brief = _agent_continuation_prompt({
        "job_id": job_id,
        "agent_next_step": "finish the template",
        "agent_observations": [{"tool": "read_files", "args": {}, "status": "success",
                                "output": "the whole file again"}],
    })

    assert "TASK PLAN" in brief
    assert "1. [done] write app.py" in brief
    assert "2. [open] write the template" in brief
    assert brief.index("TASK PLAN") < brief.index("Tool results from earlier steps"), (
        "the plan must come before the tool log, not after it"
    )


def test_the_plan_tool_refuses_before_a_project_is_claimed(workspace) -> None:
    def generate(**_kwargs):
        return {"text": "", "tool_calls": [_call("task_plan", {"steps": ["do it"]}, "p1")]}

    _, records, _ = _episode(generate, episode="ep-plan-5",
                             session={"records": [], "owned": False, "job_id": "", "cursor": 0})

    plan_record = next(row for row in records if row["tool_name"] == "task_plan")
    assert plan_record["status"] == "failure"
    assert "start_job" in plan_record["error"]


def test_the_brief_tells_the_model_to_plan_first() -> None:
    from organ.promptorgan.packs.terminal_agent.pack import TERMINAL_AGENT_SYSTEM

    text = TERMINAL_AGENT_SYSTEM.casefold()
    assert "task_plan" in text, "the brief never names the planning tool"
    assert "plan" in text


def test_the_plan_reaches_the_hud_as_structured_rows(workspace) -> None:
    """The HUD draws the plan as a checklist, so it must receive rows, not rendered text.

    The trace used to get the host's `1. [open] ...` string in a code block. Parsing a
    display format back into state is the contract-less seam section 2.1 forbids, so the
    rows ride the tool result: the record keeps them, the live frame carries them, and
    the finished reply's steps promote them (pressure_loop/operator_chat.py).
    """
    session = {"records": [], "owned": False, "job_id": "", "cursor": 0}
    _, records, _ = _episode(_claim_and_plan(), episode="ep-plan-hud", session=session)

    plan_record = next(row for row in records if row["tool_name"] == "task_plan")
    assert plan_record["result"]["plan"] == [
        {"step": 1, "text": "write app.py", "done": False},
        {"step": 2, "text": "write the template", "done": False},
    ]
    assert plan_record["result"]["plan_complete"] is False

    from pressure_loop.operator_chat import _thinking_from_auto

    thinking = _thinking_from_auto(
        auto={"outputs": {"operator_tool_calls": records}}, route={}, prompt_pack={})
    step = next(row for row in thinking["steps"] if row["tool"] == "task_plan")
    assert step["plan"] == plan_record["result"]["plan"], (
        "a reloaded HUD repaints the checklist from the finished reply, not from the frame"
    )


def test_rewriting_the_same_plan_reports_that_nothing_changed(workspace) -> None:
    """A plan call that changes nothing must not read like a plan call that did something.

    Live 2026-09-20: seven consecutive episodes called
    ``task_plan(completed=[1,2], steps=[the same five strings])`` and each got back a
    cheerful ``task plan · 2 / 5 done``. Success on an unchanged plan is the no-silent-empty
    shape from law 2.1 — "recorded your plan" and "changed nothing" shared a return value —
    and it is what let the build mistake standing still for working.
    """
    from auto_loop.execute import job_control as jobs

    session = {"records": [], "owned": False, "job_id": "", "cursor": 0}
    _episode(_claim_and_plan(), episode="ep-plan-noop-1", session=session)
    job_id = session["job_id"]
    jobs.leave_writer(job_id, "cycle-plan")

    steps = ["write app.py", "write the template"]

    def replay(**_kwargs):
        return {"text": "", "tool_calls": [
            _call("task_plan", {"steps": steps, "completed": []}, "p2")]}

    _, records, _ = _episode(replay, episode="ep-plan-noop-2", job_id=job_id,
                             session={"records": [], "owned": False, "job_id": "", "cursor": 0})

    row = next(r for r in records if r["tool_name"] == "task_plan")
    assert "unchanged" in row["output"].casefold(), row["output"]
    assert row["result"]["plan_changed"] is False
    # The plan itself is untouched: a no-op says so, it does not corrupt what is there.
    job = jobs.get_job(job_id)
    assert [(s.step, s.text, s.done) for s in job.plan] == [
        (1, "write app.py", False), (2, "write the template", False)]


def test_a_plan_call_that_does_change_something_still_reports_the_change(workspace) -> None:
    session = {"records": [], "owned": False, "job_id": "", "cursor": 0}
    _episode(_claim_and_plan(), episode="ep-plan-change-1", session=session)
    job_id = session["job_id"]

    from auto_loop.execute import job_control as jobs

    jobs.leave_writer(job_id, "cycle-plan")

    def mark(**_kwargs):
        return {"text": "", "tool_calls": [_call("task_plan", {"completed": [1]}, "p2")]}

    _, records, _ = _episode(mark, episode="ep-plan-change-2", job_id=job_id,
                             session={"records": [], "owned": False, "job_id": "", "cursor": 0})

    row = next(r for r in records if r["tool_name"] == "task_plan")
    assert row["result"]["plan_changed"] is True
    assert "unchanged" not in row["output"].casefold(), row["output"]

"""AUTO hands Omega one receipt per terminal-agent batch; the foldback decides the next batch.

Live 2026-09-15: "Build a basic todo app using flask" claimed a job and stopped. The batch ran,
the foldback saw "success" with no ledger, and Omega asked the operator what they wanted instead
of pressing for the next batch. The browser lane's shape is the fix: a bounded run receipt the
host writes and Omega judges.
"""
from __future__ import annotations

import pytest

from auto_loop.execute.run import _agent_run_receipt


def _record(tool, *, ok=True, job_id="job-1", touched=(), output="", error=""):
    return {
        "tool_call_id": f"call-{tool}",
        "tool_name": tool,
        "args": {"path": "app.py"} if tool == "write_file" else {},
        "status": "success" if ok else "failure",
        "output": output,
        "error": error,
        "touched_files": [{"path": path} for path in touched],
        "job_id": job_id,
    }


@pytest.fixture
def project(tmp_path):
    from auto_loop.execute import job_control as jobs

    workspace = tmp_path / "workspace"
    root = workspace / "todo_app"
    root.mkdir(parents=True)
    jobs.open_job("job-1", "cycle-1", "build a todo app", workspace, project_root=root)
    return root


def test_a_batch_that_only_claimed_the_project_reports_job_claimed(project):
    receipt = _agent_run_receipt([_record("start_job")], operator_route="agent", text="", stop_reason="")

    assert receipt["status"] == "job_claimed"
    assert receipt["steps"] == 1 and receipt["job_id"] == "job-1"
    assert receipt["project_root"] == str(project.resolve())
    assert receipt["actions"][0]["tool"] == "start_job" and receipt["actions"][0]["ok"] is True


def test_a_batch_that_wrote_files_reports_what_the_project_holds(project):
    (project / "app.py").write_text("from flask import Flask\n", encoding="utf-8")

    receipt = _agent_run_receipt(
        [_record("start_job"), _record("write_file", touched=("app.py",), output="wrote app.py")],
        operator_route="agent",
        text="Created the app.",
        stop_reason="",
    )

    assert receipt["status"] == "batch_ran"
    assert receipt["project_files"] == ["app.py"]
    assert receipt["touched_files"] == ["app.py"]
    assert receipt["summary"] == "Created the app."


def test_a_question_with_no_calls_is_the_agent_asking(project):
    receipt = _agent_run_receipt(
        [], operator_route="agent", text="Which port should it listen on?", stop_reason=""
    )

    assert receipt["status"] == "ask_operator"
    assert receipt["question"] == "Which port should it listen on?"
    assert receipt["summary"] == ""


def test_silence_with_no_calls_is_no_action(project):
    assert _agent_run_receipt([], operator_route="agent", text="", stop_reason="")["status"] == "no_action"


def test_a_batch_whose_every_call_failed_is_blocked(project):
    receipt = _agent_run_receipt(
        [_record("write_file", ok=False, error="permission required")],
        operator_route="agent",
        text="",
        stop_reason="",
    )

    assert receipt["status"] == "blocked"


def test_the_receipt_lists_the_project_not_a_drive_s_system_folders(project):
    """A project claimed at a drive root reported the recycle bin as its files, and that is what
    Omega reads as build evidence (live 2026-09-15: 64 recycled paths, no project)."""
    (project / "app.py").write_text("from flask import Flask", encoding="utf-8")
    for junk in ("$RECYCLE.BIN", "System Volume Information", ".git"):
        (project / junk).mkdir()
        (project / junk / "noise.txt").write_text("noise", encoding="utf-8")

    receipt = _agent_run_receipt([_record("start_job")], operator_route="agent", text="", stop_reason="")

    assert receipt["project_files"] == ["app.py"]


def test_the_chat_route_has_no_agent_run():
    assert _agent_run_receipt(
        [_record("web_search")], operator_route="chat", text="", stop_reason=""
    ) is None


def _packet(run, *, lane="agent"):
    from omega_loop.episode.types import OmegaEpisodeInput

    return OmegaEpisodeInput(
        omega_lane=lane,
        episode_id="e1",
        intent="USE_TOOL",
        action="RESPONSE",
        tool=None,
        domain="python",
        operator_text="build a basic todo app using flask",
        outcome="success",
        failure_reason=None,
        raw_tool_result=None,
        recall=(),
        recall_error=None,
        prior_mastery=None,
        agent_run=run,
    )


def test_the_packet_carries_the_receipt_and_only_on_the_agent_lane():
    run = {
        "status": "job_claimed",
        "steps": 1,
        "job_id": "job-1",
        "project_root": "E:\\todo_app",
        "actions": [{"step": 1, "tool": "start_job", "ok": True}],
        "project_files": [],
        "touched_files": [],
    }

    assert _packet(run).agent_run["status"] == "job_claimed"
    with pytest.raises(ValueError, match="agent lane"):
        _packet(run, lane="chat")


def _pressure_control(intent):
    assessment = {key: "text" for key in ("who", "what", "when", "where", "why", "how")}
    assessment.update(
        desired_state="a built app",
        observed_state="nothing ran",
        gap="the app does not exist",
        evidence=["every call failed"],
    )
    return {
        "control": {
            "assessment": assessment,
            "decision": "PRESSURE",
            "synthesis": "the work continues",
            "pressure": {
                "intent": intent,
                "confidence": 0.9,
                "reason": "write app.py with the Flask routes",
                "setpoints": {},
                "desired_state": "a built app",
                "observed_state": "nothing ran",
                "resolution_conditions": ["app.py exists and runs"],
            },
        }
    }


def test_a_blocked_batch_cannot_be_continued_with_use_tool():
    from organ.promptorgan.packs.meta_omega.core import parse_lane

    blocked = _packet({"status": "blocked", "steps": 1, "actions": [],
                       "project_files": [], "touched_files": []})
    with pytest.raises(ValueError, match="blocked agent batch"):
        parse_lane(_pressure_control("USE_TOOL"), blocked)

    claimed = _packet({"status": "job_claimed", "steps": 1, "actions": [],
                       "project_files": [], "touched_files": []})
    result = parse_lane(_pressure_control("USE_TOOL"), claimed)
    assert result.control_decision["pressure"]["intent"] == "USE_TOOL"


def test_the_agent_lane_prompt_decides_from_the_run_status():
    from organ.promptorgan.packs.meta_omega import agent as agent_pack

    assert "Decide from agent_run.status" in agent_pack.AGENT_POLICY
    for status in ("job_claimed", "batch_ran", "done_claimed", "ask_operator", "no_action", "blocked"):
        assert status in agent_pack.AGENT_POLICY


# ── what the TASK has done, not just this batch ────────────────────────────────
# Live 2026-09-20, the todo-app build's closing verdict: "The task plan is defined but no
# implementation has occurred yet" — while `write_file success: app.py` sat in the same
# run and the file held full CRUD. Omega was not hallucinating. The receipt is built from
# ONE batch's records, so every later episode handed it `touched_files: []`; `project_files`
# proved nothing because that project already existed when the task started; and the plan,
# which said steps 1-2 were done, was never in Omega's evidence at all. Omega described the
# batch correctly and generalised it to the task, because the task had no ledger.


def test_the_receipt_carries_what_the_whole_task_has_touched(project):
    """A batch that touches nothing must not look like a task that has built nothing."""
    from auto_loop.execute import job_control as jobs

    (project / "app.py").write_text("from flask import Flask\n", encoding="utf-8")
    writing = _agent_run_receipt(
        [_record("write_file", touched=("app.py",), output="wrote app.py")],
        operator_route="agent", text="", stop_reason="")
    assert writing["touched_files"] == ["app.py"]
    assert writing["task_touched_files"] == ["app.py"], (
        "the writing batch seeds the task ledger"
    )

    # A later episode that only reads. This is the shape that produced the wrong verdict.
    reading = _agent_run_receipt(
        [_record("read_files", output="app.py (1 lines)")],
        operator_route="agent", text="", stop_reason="")
    assert reading["touched_files"] == [], "this batch really did touch nothing"
    assert reading["task_touched_files"] == ["app.py"], (
        "but the TASK has written app.py, and Omega must be able to see that"
    )
    assert jobs.get_job("job-1").touched_paths == ("app.py",)


def test_the_receipt_carries_the_task_plan_omega_judges_against(project):
    """The plan's done flags are the task's own record of what it accomplished.

    It reached AUTO's continuation brief but never Omega's evidence, so the foldback judged
    progress without the one artifact that states it.
    """
    from auto_loop.execute import job_control as jobs

    jobs.set_job_plan("job-1", ["write app.py", "write the template", "run the tests"])
    jobs.complete_plan_steps("job-1", [1])

    receipt = _agent_run_receipt(
        [_record("read_files", output="app.py (1 lines)")],
        operator_route="agent", text="", stop_reason="")

    assert receipt["plan"] == [
        {"step": 1, "text": "write app.py", "done": True},
        {"step": 2, "text": "write the template", "done": False},
        {"step": 3, "text": "run the tests", "done": False},
    ]
    assert receipt["plan_complete"] is False


def test_omegas_bounded_receipt_keeps_the_task_evidence(project):
    """`bounded_agent_run` is a whitelist: a field it does not name never reaches Omega."""
    from omega_loop.episode.types import bounded_agent_run

    from auto_loop.execute import job_control as jobs

    jobs.set_job_plan("job-1", ["write app.py"])
    jobs.complete_plan_steps("job-1", [1])
    (project / "app.py").write_text("x = 1\n", encoding="utf-8")
    _agent_run_receipt([_record("write_file", touched=("app.py",))],
                       operator_route="agent", text="", stop_reason="")
    receipt = _agent_run_receipt([_record("read_files")], operator_route="agent",
                                 text="", stop_reason="")

    bounded = bounded_agent_run(receipt)
    assert bounded["task_touched_files"] == ["app.py"], (
        "the task ledger must survive the boundary into Omega's packet"
    )
    assert bounded["plan"] == [{"step": 1, "text": "write app.py", "done": True}]
    assert bounded["plan_complete"] is True

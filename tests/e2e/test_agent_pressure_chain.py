"""The terminal-agent chain (plan Task 14, step 4).

A build whose first verification is red: agent Omega returns PRESSURE, episode 1
closes, a successor receives the same objective and lineage plus the earlier tool
results, verification goes green, Omega returns DONE. AUTO never executes twice under
one episode id, and the operator's bubble resolves once, with Omega's answer.
"""

from __future__ import annotations

import pytest

from tests.helpers.blueprint_driver import BlueprintDriver, agent_decision, tool_call

pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")

OBJECTIVE = "build a tested statistics package"


def _exit(code: int) -> str:
    """A verification command with a known exit code (the shell runs inside the project)."""
    return f"exit {code}"


@pytest.fixture
def driver(monkeypatch, tmp_path):
    return BlueprintDriver(monkeypatch=monkeypatch, tmp_path=tmp_path)


def test_agent_pressure_chain_reruns_verification_in_a_successor_until_green(driver):
    driver.agent_batches.extend([
        {"text": "", "tool_calls": [
            tool_call("start_job", {"project_root": "."}, "claim"),
            tool_call("task_plan", {"steps": ["do the work the operator asked for"]}, "plan"),
            tool_call("write_file", {"path": "src/stats.py", "content": "def mean(values):\n    return sum(values) / len(values)\n"}, "write"),
            tool_call("run_command", {"command": _exit(1)}, "verify"),
        ]},
        {"text": "", "tool_calls": [
            tool_call("write_file", {"path": "tests/test_stats.py", "content": "from src.stats import mean\n"}, "write-2"),
            tool_call("run_command", {"command": _exit(0)}, "verify-2"),
        ]},
    ])
    driver.omega.extend([
        agent_decision("PRESSURE", reason="add the missing test and rerun verification"),
        agent_decision("DONE", synthesis="The statistics package is built and its verification passes."),
    ])
    executions: list[str] = []
    real_execute = __import__("auto_loop.execute.run", fromlist=["execute"]).execute

    def counting_execute(*args, **kwargs):
        executions.append(args[4])
        return real_execute(*args, **kwargs)

    driver.monkeypatch.setattr("auto_loop.loop.execute_phase.execute", counting_execute)

    first = driver.view(driver.operator_turn(OBJECTIVE, route="CODING_AGENT", cycle_id="bubble-agent"))
    assert (first.intent, first.lane) == ("RESPOND", "agent")
    ledger = first.outputs["operator_tool_calls"]
    assert [(row["tool_name"], row["status"]) for row in ledger] == [
        ("start_job", "success"), ("task_plan", "success"), ("write_file", "success"),
        ("run_command", "failure")]
    assert (driver.harness.workspace_root / "src/stats.py").is_file()
    receipt = first.outputs["agent_run"]
    assert receipt["status"] == "batch_ran" and receipt["steps"] == 4
    assert receipt["job_id"] == ledger[0]["job_id"]
    assert "src/stats.py" in receipt["project_files"]
    assert [row["tool"] for row in receipt["actions"]] == [
        "start_job", "task_plan", "write_file", "run_command"]
    fold = driver.folds[-1]  # the preceding fold belongs to the Chat handoff episode
    assert "Decide from agent_run.status" in fold["system"], "Omega judges the batch receipt"
    assert "batch_ran" in fold["prompt"] and "src/stats.py" in fold["prompt"]
    assert not first.outputs.get("response_text"), "a red batch is not an answer"
    assert first.control["decision"] == "PRESSURE" and first.control["pressure"]["intent"] == "USE_TOOL"
    first_reply = driver.reply("bubble-agent")
    assert "Coding Agent lane" in first_reply["text"]
    assert first_reply.get("lane_work", {}).get("lane") == "agent", first_reply
    assert first_reply.get("lane_work", {}).get("status") == "progress", first_reply

    (successor,) = driver.staged_successors()
    assert successor.metadata["original_cycle_id"] == "bubble-agent"
    assert successor.metadata["original_objective"] == OBJECTIVE
    assert [row["tool"] for row in successor.metadata["agent_observations"]] == [
        "start_job", "task_plan", "write_file", "run_command"]
    assert successor.metadata["agent_job_id"] == ledger[0]["job_id"], "the next batch re-owns this job"

    second = driver.view(driver.run_successor())
    assert second.episode_id != first.episode_id and (second.intent, second.lane) == ("USE_TOOL", "agent")
    prompt = [g for g in driver.generations if g.get("messages")][-1]["messages"][1]["content"]
    assert "CONTINUING THE OPERATOR'S TASK" in prompt and "add the missing test" in prompt
    ledger = second.outputs["operator_tool_calls"]
    assert [(row["tool_name"], row["status"]) for row in ledger] == [
        ("write_file", "success"), ("run_command", "success")]
    assert ledger[0]["job_id"] == first.outputs["operator_tool_calls"][0]["job_id"], "the same single writer"
    assert second.control["decision"] == "DONE"

    assert len(executions) == len(set(executions)), "AUTO ran at most once per episode id"
    assert {first.episode_id, second.episode_id} <= set(executions), "both Agent episodes ran AUTO"
    assert driver.staged_successors() == []
    reply = driver.reply("bubble-agent")
    assert "Coding Agent lane" in reply["text"], "the original acknowledgement stays in Chat"
    assert reply["lane_work"]["status"] == "completed"
    from pressure_loop.operator_chat_responses import get_lane_updates

    lane_updates = get_lane_updates("bubble-agent")["updates"]
    assert lane_updates[-1]["text"] == "The statistics package is built and its verification passes."
    assert lane_updates[-1]["episode_id"] == second.episode_id


def test_a_batch_that_only_claimed_the_project_still_continues(driver):
    """Live 2026-09-15: "Build a basic todo app using flask" claimed the job and stopped there.

    The batch receipt never reached the fold, so Omega saw a successful step with no artifacts and
    asked the operator whether it wanted code, a tutorial or a build. A claim is a step, not an
    answer: the receipt says so and the next batch does the work.
    """
    driver.agent_batches.extend([
        {"text": "", "tool_calls": [tool_call("start_job", {"project_root": "."}, "claim")]},
        # No second start_job: the continuation re-owns the job its first batch claimed, so
        # mutable work is allowed without claiming the project again. It does write the
        # task's steps down first, because mutable work waits for a plan.
        {"text": "", "tool_calls": [
            tool_call("task_plan", {"steps": ["write app.py with the Flask routes"]}, "plan"),
            tool_call("write_file", {"path": "app.py", "content": "from flask import Flask"}, "write"),
        ]},
    ])
    driver.omega.extend([
        agent_decision("PRESSURE", reason="write app.py with the Flask routes for the todo list"),
        agent_decision("DONE", synthesis="The todo app is built: app.py holds the Flask routes."),
    ])

    first = driver.view(driver.operator_turn(
        "build a basic todo app using flask", route="CODING_AGENT", cycle_id="bubble-todo"))

    receipt = first.outputs["agent_run"]
    assert receipt["status"] == "job_claimed" and receipt["steps"] == 1
    assert "job_claimed" in driver.folds[-1]["prompt"], "Omega sees that the batch only claimed the project"
    assert first.control["decision"] == "PRESSURE" and first.control["pressure"]["intent"] == "USE_TOOL"
    reply = driver.reply("bubble-todo")
    assert "Coding Agent lane" in reply["text"] and reply["lane_work"]["status"] == "progress"

    (successor,) = driver.staged_successors()
    assert successor.metadata["agent_job_id"] == receipt["job_id"]

    second = driver.view(driver.run_successor())

    assert "build a basic todo app using flask" in driver.folds[-1]["prompt"], (
        "the successor's fold carries the operator's objective; with it empty Omega asked the "
        "operator what the task was, mid-build"
    )
    receipt_2 = second.outputs["agent_run"]
    assert receipt_2["status"] == "batch_ran"
    assert receipt_2["job_id"] == receipt["job_id"], "the continuation ran inside the same job"
    assert receipt_2["project_root"] == receipt["project_root"]
    assert "app.py" in receipt_2["project_files"]
    assert (driver.harness.workspace_root / "app.py").is_file()
    assert second.control["decision"] == "DONE"
    reply = driver.reply("bubble-todo")
    assert "Coding Agent lane" in reply["text"] and reply["lane_work"]["status"] == "completed"
    from pressure_loop.operator_chat_responses import get_lane_updates

    assert get_lane_updates("bubble-todo")["updates"][-1]["text"] == (
        "The todo app is built: app.py holds the Flask routes."
    )

    from auto_loop.execute import job_control as jobs

    assert jobs.get_job(receipt["job_id"]).status == "completed", (
        "the job spans the task's batches and closes when Omega settles it"
    )

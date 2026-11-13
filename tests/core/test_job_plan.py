"""The plan a build carries from its first episode to its last.

The agent lane runs one tool batch per episode, so a task's steps have to survive between
episodes or every episode starts from the raw tool log — which is how one live build
re-read its own files until the operator stopped it (2026-09-20).

The plan is execution control state, not memory (law section 7.1), so it rides the job
record: one durable place that already spans every batch of a task and survives a restart.
The model supplies the step TEXT; the host numbers them, because the host owns every
identifier (law section 0).
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    from auto_loop.execute import job_control as module

    monkeypatch.setattr(module, "jobs_root", lambda: tmp_path / "jobs")
    module.reset_for_tests()
    yield module
    module.reset_for_tests()


def _open(jobs, tmp_path: Path):
    return jobs.open_job("build", "bubble", "build a todo app", tmp_path, project_root="todo_app")


def test_a_plan_is_numbered_by_the_host_and_starts_open(jobs, tmp_path) -> None:
    _open(jobs, tmp_path)

    job = jobs.set_job_plan("build", ["create app.py", "add the template", "run it"])

    assert [(step.step, step.text, step.done) for step in job.plan] == [
        (1, "create app.py", False),
        (2, "add the template", False),
        (3, "run it", False),
    ]


def test_completing_steps_clears_only_those_named(jobs, tmp_path) -> None:
    _open(jobs, tmp_path)
    jobs.set_job_plan("build", ["create app.py", "add the template", "run it"])

    job = jobs.complete_plan_steps("build", [1, 3])

    assert {step.step: step.done for step in job.plan} == {1: True, 2: False, 3: True}
    again = jobs.complete_plan_steps("build", [1])
    assert {step.step: step.done for step in again.plan} == {1: True, 2: False, 3: True}, "idempotent"


def test_completing_a_step_that_is_not_in_the_plan_fails_loudly(jobs, tmp_path) -> None:
    """A model clearing a step nobody planned is a mistake, not a silent no-op (law 2.1)."""
    _open(jobs, tmp_path)
    jobs.set_job_plan("build", ["create app.py"])

    with pytest.raises(ValueError, match="plan step"):
        jobs.complete_plan_steps("build", [7])


def test_the_plan_survives_a_restart(jobs, tmp_path) -> None:
    _open(jobs, tmp_path)
    jobs.set_job_plan("build", ["create app.py", "add the template"])
    jobs.complete_plan_steps("build", [1])

    jobs.reset_for_tests()  # a fresh process: nothing in memory, only the record on disk

    job = jobs.get_job("build")
    assert [(s.step, s.text, s.done) for s in job.plan] == [
        (1, "create app.py", True),
        (2, "add the template", False),
    ]


def test_a_record_written_before_plans_existed_still_loads(jobs, tmp_path) -> None:
    """Store upgrade: an older job record reads as a job with no plan yet."""
    import json

    _open(jobs, tmp_path)
    path = jobs.jobs_root() / "build.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("plan")
    path.write_text(json.dumps(payload), encoding="utf-8")
    jobs.reset_for_tests()

    assert jobs.get_job("build").plan == ()


def test_the_plan_is_bounded(jobs, tmp_path) -> None:
    _open(jobs, tmp_path)

    with pytest.raises(ValueError, match="plan"):
        jobs.set_job_plan("build", [f"step {n}" for n in range(jobs.MAX_JOB_PLAN_STEPS + 1)])


def test_the_rendered_plan_shows_what_is_left(jobs, tmp_path) -> None:
    """This text is what every later episode reads instead of the raw tool log."""
    _open(jobs, tmp_path)
    jobs.set_job_plan("build", ["create app.py", "add the template"])
    jobs.complete_plan_steps("build", [1])

    rendered = jobs.render_job_plan(jobs.get_job("build"))

    assert "1. [done] create app.py" in rendered
    assert "2. [open] add the template" in rendered
    assert jobs.plan_is_complete(jobs.get_job("build")) is False
    jobs.complete_plan_steps("build", [2])
    assert jobs.plan_is_complete(jobs.get_job("build")) is True


def test_an_empty_plan_is_not_a_complete_plan(jobs, tmp_path) -> None:
    """Otherwise a build with no plan at all would read as finished."""
    _open(jobs, tmp_path)

    assert jobs.plan_is_complete(jobs.get_job("build")) is False

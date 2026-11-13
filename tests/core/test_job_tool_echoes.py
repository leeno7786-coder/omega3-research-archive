"""The job's echo ledger: what this task's calls have already produced.

Contract pins for the API that `test_agent_loop_progress.py` drove into existence. The
agent lane runs one tool batch per episode, so the only repeat it can make is one that
crosses episodes — and a batch-local ledger is blind to exactly that.

Live 2026-09-20: seven consecutive episodes replayed one pair of calls and each returned
byte-identical output. The ledger lives on the job because the job is the only thing that
outlives an episode (law 7.1: execution control state, never canonical memory).
"""

from __future__ import annotations

import pytest

@pytest.fixture
def jobs(tmp_path, monkeypatch):
    from auto_loop.execute import job_control as module

    monkeypatch.setattr(module, "jobs_root", lambda: tmp_path / "jobs")
    module.reset_for_tests()
    module.open_job("build", "bubble", "build a todo app", tmp_path, project_root="todo_app")
    yield module
    module.reset_for_tests()


job = "build"


def test_the_same_call_with_the_same_result_is_a_repeat(jobs) -> None:
    assert jobs.record_tool_echo(job, "read_files:app.py", "digest-a") is False, (
        "the first time a call reports something, that is progress"
    )
    assert jobs.record_tool_echo(job, "read_files:app.py", "digest-a") is True, (
        "the same call reporting the same thing added nothing the task did not have"
    )


def test_the_same_call_with_a_new_result_is_progress_again(jobs) -> None:
    """A re-read after a write reports different bytes. That is work, not a loop."""
    jobs.record_tool_echo(job, "read_files:app.py", "digest-a")
    jobs.record_tool_echo(job, "read_files:app.py", "digest-a")
    assert jobs.get_job(job).stalled_calls == 1

    assert jobs.record_tool_echo(job, "read_files:app.py", "digest-b") is False
    assert jobs.get_job(job).stalled_calls == 0, "any real progress clears the stall count"
    # And the ledger now compares against the NEWEST result for that call.
    assert jobs.record_tool_echo(job, "read_files:app.py", "digest-b") is True
    assert jobs.record_tool_echo(job, "read_files:app.py", "digest-a") is False


def test_a_task_is_stalled_only_after_repeating_itself(jobs) -> None:
    assert jobs.job_is_stalled(jobs.get_job(job)) is False
    # The productive episodes: each call reports something the task did not have.
    jobs.record_tool_echo(job, "task_plan:same", "plan-digest")
    jobs.record_tool_echo(job, "read_files:app.py", "read-digest")
    assert jobs.get_job(job).stalled_calls == 0

    # The live fixed point: that same pair replayed back to back with nothing new between
    # them. The count is CONSECUTIVE repeats, so real progress in between clears it.
    assert jobs.record_tool_echo(job, "task_plan:same", "plan-digest") is True
    record = jobs.get_job(job)
    assert record.stalled_calls == 1
    assert jobs.job_is_stalled(record) is False, "one repeat is a stumble, not a fixed point"

    assert jobs.record_tool_echo(job, "read_files:app.py", "read-digest") is True
    record = jobs.get_job(job)
    assert record.stalled_calls == jobs.MAX_JOB_STALLED_CALLS
    assert jobs.job_is_stalled(record) is True


def test_the_ledger_keeps_one_entry_per_call_and_is_bounded(jobs) -> None:
    for index in range(jobs.MAX_JOB_ECHOES + 20):
        jobs.record_tool_echo(job, f"read_files:file-{index}", f"digest-{index}")
    echoes = jobs.get_job(job).echoes
    assert len(echoes) == jobs.MAX_JOB_ECHOES
    assert len({echo.signature for echo in echoes}) == len(echoes), "one entry per signature"
    # Newest first: the oldest calls are the ones dropped.
    assert echoes[0].signature.endswith(str(jobs.MAX_JOB_ECHOES + 19))


def test_the_ledger_survives_a_reload(jobs) -> None:
    """The ledger is only useful if it outlives the episode that wrote it."""
    jobs.record_tool_echo(job, "read_files:app.py", "digest-a")
    # get_job reads the record off disk every time, so this IS the next episode's view.
    assert [(e.signature, e.digest) for e in jobs.get_job(job).echoes] == [
        ("read_files:app.py", "digest-a")]
    assert jobs.record_tool_echo(job, "read_files:app.py", "digest-a") is True


def test_an_unknown_job_and_an_empty_echo_both_fail_loudly(jobs) -> None:
    with pytest.raises(KeyError):
        jobs.record_tool_echo("no-such-job", "read_files:app.py", "digest-a")
    with pytest.raises(ValueError):
        jobs.record_tool_echo(job, "", "digest-a")
    with pytest.raises(ValueError):
        jobs.record_tool_echo(job, "read_files:app.py", "")

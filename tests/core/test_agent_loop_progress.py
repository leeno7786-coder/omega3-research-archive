"""The agent lane must spend its episodes on work, and must be able to end one.

Live 2026-09-20 ("lets build a basic todo app using flask"): the build wrote a complete
Flask app in three episodes, then spent every later episode re-claiming the project it
already owned and re-reading the two files it had just written, byte for byte, until the
operator typed "stop". Seven of sixteen calls were ``start_job`` on a claimed project.

The agent lane runs ONE tool batch per episode (``run.py``: "One tool batch per agent
episode"), so each of those calls cost a whole episode — and nothing in the brief ever
named a move that ends a task, so the model had no way to stop.
"""

from __future__ import annotations

import json

from auto_loop.execute.run import _operator_tools_exchange


def _call(name, args, call_id):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _tool_names(pass_kwargs) -> set[str]:
    return {spec["function"]["name"] for spec in (pass_kwargs.get("tools") or ())}


def _episode(generate, *, episode, job_id="", session=None, cycle="cycle-todo-app"):
    """One agent episode: one tool batch, and the job stays owned by the operator's turn,
    so every episode of the task shares that turn's cycle id (see ``own()`` in run.py)."""
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


def test_a_continuation_is_never_offered_the_project_it_already_owns(tmp_path, monkeypatch) -> None:
    """Re-claiming a claimed project is a no-op, and on this lane it costs a whole episode."""
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "workspace"))
    first_passes: list[dict] = []
    second_passes: list[dict] = []

    def claim(**kwargs):
        first_passes.append(kwargs)
        return {"text": "", "tool_calls": [
            _call("start_job", {"project_root": "."}, "c1"),
            _call("write_file", {"path": "app.py", "content": "todos = []\n"}, "w1")]}

    from auto_loop.execute import job_control as jobs

    session = {"records": [], "owned": False, "job_id": "", "cursor": 0}
    _episode(claim, episode="ep-agent-claim", session=session)
    job_id = session["job_id"]
    assert job_id, "the first episode claimed a project"
    # What the lane's caller does between batches: the batch ends, the writer seat is
    # released, and the job stays open for the next episode of the same task.
    jobs.leave_writer(job_id, "cycle-todo-app")

    def continue_work(**kwargs):
        second_passes.append(kwargs)
        return {"text": "", "tool_calls": [
            _call("write_file", {"path": "templates/index.html", "content": "<h1>todo</h1>\n"}, "w2")]}

    _episode(continue_work, episode="ep-agent-continue", job_id=job_id,
             session={"records": [], "owned": False, "job_id": "", "cursor": 0})

    assert "start_job" in _tool_names(first_passes[0]), "the first episode must be able to claim"
    assert "start_job" not in _tool_names(second_passes[0]), (
        "start_job stayed on a continuation's table, which is how the live build spent 7 of "
        "its 16 calls re-claiming a project it already owned"
    )
    assert {"write_file", "finish_task"} <= _tool_names(second_passes[0]), (
        "the working tools and the finishing move stay"
    )


def test_the_agent_brief_names_the_move_that_ends_a_task() -> None:
    """``finish_task`` existed but appeared in no prompt pack, so nothing ever told the model
    a task could end: every episode worked, none finished, and Omega continued it for ever."""
    from organ.promptorgan.packs.terminal_agent.pack import TERMINAL_AGENT_SYSTEM

    text = TERMINAL_AGENT_SYSTEM.casefold()
    assert "finish_task" in text, "the brief never names the ending move"
    assert "start_job first" not in text, (
        "an unconditional 'start_job first' is what made every episode re-claim the project"
    )
    assert "no_progress" in text or "never re-read" in text, (
        "the brief should say that re-reading what it just wrote is not work"
    )


def test_a_repeat_across_episodes_is_reported_as_no_progress(tmp_path, monkeypatch) -> None:
    """The stall ledger lived in the batch, so on a one-batch-per-episode lane it was dead.

    Live 2026-09-20 ("lets build a basic todo app using python"): after three productive
    episodes the build reached a fixed point and replayed ONE pair of calls —
    ``task_plan(completed=[1,2], steps=[...])`` then ``read_files`` of the same three
    paths — for SEVEN consecutive episodes, each returning the byte-identical
    ``task plan · 2 / 5 done``, until the operator typed "stop". Every repeat was the
    first call of a fresh batch, so ``seen_results`` was empty and saw nothing.
    """
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "workspace"))
    from auto_loop.execute import job_control as jobs

    read = _call("read_files", {"paths": ["app.py"]}, "r1")

    def first(**_kwargs):
        return {"text": "", "tool_calls": [
            _call("start_job", {"project_root": "."}, "c1"),
            _call("task_plan", {"steps": ["write the app"]}, "p1"),
            _call("write_file", {"path": "app.py", "content": "todos = []\n"}, "w1")]}

    session = {"records": [], "owned": False, "job_id": "", "cursor": 0}
    _episode(first, episode="ep-echo-1", session=session)
    job_id = session["job_id"]
    jobs.leave_writer(job_id, "cycle-todo-app")

    def reader(**_kwargs):
        return {"text": "", "tool_calls": [read]}

    # Episode two reads the file. Nothing has seen that result before, so it is progress.
    _, records, _ = _episode(reader, episode="ep-echo-2", job_id=job_id,
                             session={"records": [], "owned": False, "job_id": "", "cursor": 0})
    row = next(r for r in records if r["tool_name"] == "read_files")
    assert "no_progress" not in row["output"], row["output"]
    jobs.leave_writer(job_id, "cycle-todo-app")

    # Episode three makes the SAME call and gets the SAME bytes back. That is the loop.
    _, records, _ = _episode(reader, episode="ep-echo-3", job_id=job_id,
                             session={"records": [], "owned": False, "job_id": "", "cursor": 0})
    row = next(r for r in records if r["tool_name"] == "read_files")
    assert "no_progress" in row["output"], row["output"]
    assert "earlier step of this task" in row["output"], (
        "the message must say the repeat crossed episodes, not 'earlier call in this step'"
    )


def test_a_stalled_task_is_offered_only_the_moves_that_end_it(tmp_path, monkeypatch) -> None:
    """Narrowing the table was also per-batch, so it never fired on this lane either.

    A task at a fixed point has exactly two honest moves left: finish it, or say what it
    is blocked on. Everything else is another wasted episode.
    """
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(tmp_path / "workspace"))
    from auto_loop.execute import job_control as jobs

    read = _call("read_files", {"paths": ["app.py"]}, "r1")

    def first(**_kwargs):
        return {"text": "", "tool_calls": [
            _call("start_job", {"project_root": "."}, "c1"),
            _call("task_plan", {"steps": ["write the app"]}, "p1"),
            _call("write_file", {"path": "app.py", "content": "todos = []\n"}, "w1")]}

    session = {"records": [], "owned": False, "job_id": "", "cursor": 0}
    _episode(first, episode="ep-stall-1", session=session)
    job_id = session["job_id"]

    def reader(**_kwargs):
        return {"text": "", "tool_calls": [read]}

    passes: list[dict] = []

    def watched_reader(**kwargs):
        passes.append(kwargs)
        return {"text": "", "tool_calls": [read]}

    # Two echoing episodes is a fixed point.
    for index in range(4):
        jobs.leave_writer(job_id, "cycle-todo-app")
        _episode(reader if index < 3 else watched_reader,
                 episode=f"ep-stall-{index + 2}", job_id=job_id,
                 session={"records": [], "owned": False, "job_id": "", "cursor": 0})

    offered = _tool_names(passes[0])
    assert offered, "the stalled episode still got a tool table"
    assert offered <= {"finish_task", "ask_operator"}, (
        f"a stalled task must be offered only the finishing moves, got {sorted(offered)}"
    )


def test_the_brief_says_replaying_the_plan_is_not_progress() -> None:
    """The observed loop was task_plan + read_files, not file work, so the brief has to
    name the plan replay specifically — 'never re-derive the list' was not enough."""
    from organ.promptorgan.packs.terminal_agent.pack import TERMINAL_AGENT_SYSTEM

    text = TERMINAL_AGENT_SYSTEM.casefold()
    assert "same plan" in text or "plan again" in text, (
        "the brief never says that re-sending the same plan is standing still"
    )
    assert "ask_operator" in text, "and the blocked move stays named"

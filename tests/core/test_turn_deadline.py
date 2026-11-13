"""One absolute deadline per operator turn — not a timeout that resets per request.

Demo-readiness inventory item #5: ``runtime/brain_cloud.py`` and ``brain_local.py`` cap a
single READ at 600 s, and a turn chains several inference stages (Global A escalation, AUTO,
the AUTO tool loop, Omega's fold). Nothing bounded the SUM, so a blackholed provider bought
ten minutes of silence *per stage* and the operator's bubble had no terminal state at all.

What is pinned here:

* the budget starts when the input is ACCEPTED at the one gate (§4.0), not when a pipeline
  finally leases it — queue wait is part of the operator's silence;
* the episode adopts the same deadline object, so cycle and episode name one budget;
* every model call is handed the REMAINING time, so two stages cannot each take the full
  read cap;
* a call that cannot start inside the budget RAISES ``TurnDeadlineExpired`` carrying its
  stage and overrun — "no time left" never looks like "the model returned nothing" (§2.1);
* expiry SETTLES the turn: the waiting bubble is answered with the failure, the episode
  records ``cascade_failure`` and folds to its terminus rather than crashing the loop
  (§2.2 — a failure is episode content);
* the registry is released at the terminus, so a finished turn tracks no budget.

Deterministic: the clock is injected, no sleeps, no runtime, no gateway.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import pressure_loop.turn_deadline as td


class _Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(td, "_now", c)
    td.reset_for_tests()
    yield c
    td.reset_for_tests()


# ─────────────────────────────── the primitive ───────────────────────────────


def test_deadline_is_absolute_not_per_call(clock):
    deadline = td.start("cyc-1", budget_s=300.0)

    assert deadline.remaining() == 300.0
    clock.advance(120.0)
    assert deadline.remaining() == 180.0
    # A second look does NOT restart the budget — that is the whole defect.
    clock.advance(120.0)
    assert deadline.remaining() == 60.0
    assert deadline.elapsed() == 240.0
    assert deadline.expired() is False


def test_expiry_raises_with_its_stage_and_overrun(clock):
    deadline = td.start("cyc-1", budget_s=300.0)
    clock.advance(301.5)

    assert deadline.expired() is True
    with pytest.raises(td.TurnDeadlineExpired) as caught:
        deadline.check("omega")

    expiry = caught.value
    assert expiry.key == "cyc-1"
    assert expiry.stage == "omega"
    assert expiry.budget_s == 300.0
    assert expiry.elapsed_s == 301.5
    assert round(expiry.overrun_s, 3) == 1.5
    assert "300.0" in str(expiry) and "omega" in str(expiry)


def test_check_returns_the_remaining_budget_while_it_lasts(clock):
    deadline = td.start("cyc-1", budget_s=300.0)
    clock.advance(100.0)

    assert deadline.check("auto") == 200.0


def test_no_deadline_is_unknown_never_zero(clock):
    """§2.1: "this turn has no budget" and "this turn has no time left" must not share a
    value. ``None`` means ungoverned; ``0.0`` would silently refuse every call."""
    assert td.get("never-seen") is None
    assert td.remaining_for("never-seen") is None
    assert td.check("never-seen", stage="auto") is None


def test_start_is_idempotent_so_a_retry_does_not_extend_the_turn(clock):
    first = td.start("cyc-1", budget_s=300.0)
    clock.advance(50.0)
    again = td.start("cyc-1", budget_s=300.0)

    assert again is first
    assert again.remaining() == 250.0


def test_a_live_queued_turn_is_not_evicted_when_registry_exceeds_256_entries(clock):
    # Each accepted turn has a cycle key and an episode alias. Capacity-based eviction by
    # alias count silently removed deadlines from still-queued work once 128 turns arrived.
    for index in range(140):
        td.start(f"cyc-{index}", budget_s=10.0)
        td.adopt(f"ep-{index}", f"cyc-{index}")

    assert len(td.tracked_keys()) == 280
    assert td.get("cyc-0") is td.get("ep-0")
    assert td.get("cyc-139") is td.get("ep-139")

    td.finish("cyc-0")
    assert td.get("cyc-0") is None
    assert td.get("ep-0") is None
    assert td.get("cyc-1") is not None


def test_default_budget_reads_the_environment(clock, monkeypatch):
    monkeypatch.delenv(td.BUDGET_ENV, raising=False)
    assert td.default_budget_s() == td.DEFAULT_TURN_BUDGET_S

    monkeypatch.setenv(td.BUDGET_ENV, "45.5")
    assert td.default_budget_s() == 45.5

    monkeypatch.setenv(td.BUDGET_ENV, "not-a-number")
    assert td.default_budget_s() == td.DEFAULT_TURN_BUDGET_S


# ───────────────────── admission → episode → model call ─────────────────────


def test_accepted_chat_input_starts_the_budget_at_the_gate(clock, monkeypatch, tmp_path):
    """The one gate (§4.0) stamps the budget, so queue wait counts against it."""
    from pressure_loop import cognition_queue as cq
    from pressure_loop.contributors import make_contributor

    monkeypatch.setattr(cq, "_queue_path", lambda: tmp_path / "cognition_queue.json")
    monkeypatch.setattr(cq, "_migration_sources", lambda: [])
    monkeypatch.setattr(cq, "notify_work", lambda *a, **k: None, raising=False)
    cq._reset_cache_for_tests()

    operator_turn = make_contributor(
        "operator", "operator_query", 1.0, {"cycle_id": "cyc-chat", "message": "hello"}
    )
    assert cq.enqueue_input(operator_turn, operator_activity=True) is True

    deadline = td.get("cyc-chat")
    assert deadline is not None
    assert deadline.budget_s == td.default_budget_s()
    assert deadline.started_at == clock.t


def test_machine_minted_work_carries_no_operator_turn_budget(clock, monkeypatch, tmp_path):
    """Omega's own dispatch is not an operator sitting in front of a bubble."""
    from pressure_loop import cognition_queue as cq
    from pressure_loop.contributors import make_contributor

    monkeypatch.setattr(cq, "_queue_path", lambda: tmp_path / "cognition_queue.json")
    monkeypatch.setattr(cq, "_migration_sources", lambda: [])
    monkeypatch.setattr(cq, "notify_work", lambda *a, **k: None, raising=False)
    cq._reset_cache_for_tests()

    dispatch = make_contributor(
        "omega", "agent_motor_dispatch", 1.0, {"cycle_id": "dispatch-1", "message": "build"}
    )
    assert cq.enqueue_input(dispatch, operator_activity=False) is True

    assert td.get("dispatch-1") is None


def test_the_episode_adopts_the_cycles_budget_including_its_queue_wait(clock):
    deadline = td.start("cyc-chat", budget_s=300.0)
    clock.advance(40.0)                      # the turn waited in line for 40 s

    adopted = td.adopt("ep-7", "cyc-chat")

    assert adopted is td.get("cyc-chat")
    assert td.remaining_for("ep-7") == 260.0  # the wait is spent, not forgiven


def test_a_bound_deadline_reaches_a_stage_that_renames_the_episode(clock):
    """Omega asks under ``f"{episode_id}-omega-ep"`` and fetched-knowledge under
    ``f"{ep_id}-fetched-knowledge"``. A key lookup alone would miss both, so the cascade
    BINDS the turn for its own thread and those stages still resolve it."""
    deadline = td.start("cyc-chat", budget_s=300.0)

    assert td.resolve("ep-7-omega-ep") is None
    with td.bind(deadline):
        assert td.current() is deadline
        assert td.resolve("ep-7-omega-ep") is deadline
    assert td.current() is None


def test_finish_releases_the_turn_and_every_alias(clock):
    td.start("cyc-chat", budget_s=300.0)
    td.adopt("ep-7", "cyc-chat")

    released = td.finish("ep-7")

    assert released is not None
    assert td.get("cyc-chat") is None
    assert td.get("ep-7") is None
    assert td.tracked_keys() == ()


# ───────────────────────── the model-call chokepoint ─────────────────────────


class _RecordingClient:
    def __init__(self, *, on_generate=None) -> None:
        self.calls: list[dict] = []
        self.on_generate = on_generate

    def generate_completion(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.on_generate is not None:
            self.on_generate()
        return {"ok": True, "text": "fine"}


@pytest.fixture
def socket_seam(monkeypatch):
    """The real ``invoke_runtime_generate`` over a stub client — the broker and the
    emit seams are not what this file is about."""
    import runtime.socket as rs

    monkeypatch.setattr(rs, "_preflight_socket", lambda *a, **k: (True, None))
    monkeypatch.setattr(rs, "_emit_runtime_enter", lambda *a, **k: None)
    monkeypatch.setattr(
        rs,
        "resolve_runtime_generate",
        lambda: rs.RuntimeSocket(
            name="generate",
            endpoint="/generate",
            capability="coding_model",
            method="generate_completion",
        ),
    )
    return rs


def test_each_stage_is_handed_only_what_is_left(clock, socket_seam):
    """Two stages of one turn cannot each claim the full read cap."""
    deadline = td.start("cyc-chat", budget_s=300.0)
    td.adopt("ep-7", "cyc-chat")
    client = _RecordingClient()

    socket_seam.invoke_runtime_generate(
        operation="intent_escalation", episode_id="ep-7", client=client, prompt="a"
    )
    clock.advance(100.0)
    socket_seam.invoke_runtime_generate(
        operation="response", episode_id="ep-7", client=client, prompt="b"
    )
    clock.advance(150.0)
    with td.bind(deadline):
        socket_seam.invoke_runtime_generate(
            operation="episode_cognition", episode_id="ep-7-omega-ep", client=client, prompt="c"
        )

    assert [call["timeout"] for call in client.calls] == [300.0, 200.0, 50.0]


def test_a_callers_own_timeout_is_clamped_never_widened(clock, socket_seam):
    td.start("cyc-chat", budget_s=300.0)
    clock.advance(270.0)
    client = _RecordingClient()

    socket_seam.invoke_runtime_generate(
        operation="episode_cognition",
        episode_id="ep-7",
        cycle_id="cyc-chat",
        client=client,
        prompt="a",
        timeout=600.0,
    )
    socket_seam.invoke_runtime_generate(
        operation="response",
        episode_id="ep-7",
        cycle_id="cyc-chat",
        client=client,
        prompt="b",
        timeout=5.0,
    )

    assert [call["timeout"] for call in client.calls] == [30.0, 5.0]


def test_an_ungoverned_call_keeps_its_own_timeout(clock, socket_seam):
    """Autonomous work has no operator waiting; nothing here changes its contract."""
    client = _RecordingClient()

    socket_seam.invoke_runtime_generate(
        operation="episode_cognition", episode_id="ep-auto", client=client, prompt="a"
    )
    socket_seam.invoke_runtime_generate(
        operation="episode_cognition", episode_id="ep-auto", client=client, prompt="b",
        timeout=42.0,
    )

    assert "timeout" not in client.calls[0]
    assert client.calls[1]["timeout"] == 42.0


def test_an_expired_turn_never_starts_another_call(clock, socket_seam):
    td.start("cyc-chat", budget_s=300.0)
    td.adopt("ep-7", "cyc-chat")
    clock.advance(301.0)
    client = _RecordingClient()

    with pytest.raises(td.TurnDeadlineExpired) as caught:
        socket_seam.invoke_runtime_generate(
            operation="episode_cognition", episode_id="ep-7", client=client, prompt="a"
        )

    assert caught.value.stage == "episode_cognition"
    assert client.calls == []   # never asked — and never a quiet None either


def test_a_provider_that_returns_after_the_deadline_cannot_succeed(clock, socket_seam):
    td.start("cyc-chat", budget_s=30.0)
    td.adopt("ep-7", "cyc-chat")
    client = _RecordingClient(on_generate=lambda: clock.advance(31.0))

    with pytest.raises(td.TurnDeadlineExpired) as caught:
        socket_seam.invoke_runtime_generate(
            operation="response", episode_id="ep-7", client=client, prompt="a"
        )

    assert caught.value.stage == "response"
    assert len(client.calls) == 1  # provider returned, but too late to count as a reply


def test_the_tool_loop_inside_auto_shrinks_with_the_turn(clock, socket_seam):
    """AUTO's bounded tool dispatch already shrinks its OWN budget each round; the turn's
    budget bounds it too, whichever is tighter."""
    from auto_loop.execute import brain_skill_executor as bse

    td.start("cyc-chat", budget_s=300.0)
    clock.advance(280.0)

    assert bse._turn_bounded_timeout(120.0, cycle_id="cyc-chat") == 20.0
    assert bse._turn_bounded_timeout(5.0, cycle_id="cyc-chat") == 5.0
    assert bse._turn_bounded_timeout(120.0, cycle_id="no-such-turn") == 120.0


def test_skill_dispatch_that_returns_after_turn_expiry_is_failed(clock):
    from auto_loop.execute import brain_skill_executor as bse

    td.start("cyc-chat", budget_s=10.0)
    clock.advance(11.0)

    with pytest.raises(td.TurnDeadlineExpired) as caught:
        bse._turn_bounded_timeout(120.0, cycle_id="cyc-chat")

    assert caught.value.stage == "skill_dispatch"


class _StoppableProcess:
    def __init__(self) -> None:
        self.killed = False
        self.terminated = False
        self.returncode = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def communicate(self, timeout=None):
        return "", ""


@pytest.mark.parametrize(
    ("tool", "command"),
    [("run_command", "echo ready"), ("run_tests", "pytest -q")],
)
def test_operator_subprocess_is_killed_at_the_turn_deadline(
    tool, command, clock, monkeypatch, tmp_path
):
    """The wall-clock cap is enforced while a process is running, for both command tools."""
    from auto_loop.execute import brain_skill_executor as bse

    process = _StoppableProcess()
    monkeypatch.setattr(bse.ExecutionContext, "write_target", lambda self, raw: self.workspace_root)
    monkeypatch.setattr(bse, "_shell_prefix", lambda: ["fake-shell"])
    monkeypatch.setattr(bse, "_command_monotonic", clock)
    monkeypatch.setattr(bse, "_command_sleep", clock.advance)
    monkeypatch.setattr(bse.subprocess, "Popen", lambda *args, **kwargs: process)
    td.start("cyc-tool", budget_s=0.25)
    ctx = bse.ExecutionContext(
        workspace_root=tmp_path,
        objective="run the bounded command",
        timeout_s=30.0,
        cycle_id="cyc-tool",
    )
    runner = bse._run_command if tool == "run_command" else bse._run_tests

    with pytest.raises(td.TurnDeadlineExpired) as caught:
        runner({"command": command}, ctx)

    assert caught.value.stage == "run_command"
    assert process.killed is True
    assert clock.t == 1_000.25


def test_command_keeps_its_explicit_timeout_when_no_turn_deadline(clock, monkeypatch, tmp_path):
    """Ungoverned commands retain their own timeout and still stop the child process."""
    from auto_loop.execute import brain_skill_executor as bse

    process = _StoppableProcess()
    monkeypatch.setattr(bse.ExecutionContext, "write_target", lambda self, raw: self.workspace_root)
    monkeypatch.setattr(bse, "_shell_prefix", lambda: ["fake-shell"])
    monkeypatch.setattr(bse, "_command_monotonic", clock)
    monkeypatch.setattr(bse, "_command_sleep", clock.advance)
    monkeypatch.setattr(bse.subprocess, "Popen", lambda *args, **kwargs: process)
    ctx = bse.ExecutionContext(
        workspace_root=tmp_path,
        objective="run the explicitly bounded command",
        timeout_s=30.0,
    )

    result = bse._run_command({"command": "echo ready", "timeout_s": 1.0}, ctx)

    assert result.ok is False
    assert result.error == "run_command: timed out after 1s"
    assert process.killed is True
    assert clock.t == 1_001.0


# ──────────────────────────── settling the turn ────────────────────────────


def _episode(cycle_id: str = "cyc-chat", *, meta=None):
    return SimpleNamespace(
        episode_id="ep-7",
        pipeline_id=0,
        work_kind="operator_turn",
        opened_by="operator_query",
        global_={"intent_decision": {"intent": "RESPOND"}},
        meta=dict(meta or {}),
        auto={"status": "failed", "outputs": {}},
        pressure={
            "contributors": [
                {
                    "id": "operator_query",
                    "detail": {"cycle_id": cycle_id, "chat_bubble_waiting": True},
                }
            ]
        },
    )


@pytest.fixture
def chat_rail(tmp_path, monkeypatch):
    import pressure_loop.operator_chat_responses as ocr

    monkeypatch.setattr(ocr, "_store_path", lambda: tmp_path / "ocr.json")
    ocr._reset_cache_for_tests()
    return ocr


def test_expiry_answers_the_waiting_bubble_with_the_truth(clock, chat_rail):
    import cognition_chain as cl

    deadline = td.start("cyc-chat", budget_s=300.0)
    clock.advance(302.0)
    episode = _episode()

    cl._settle_turn_deadline_expiry(
        episode, td.TurnDeadlineExpired(key="cyc-chat", stage="auto", budget_s=300.0,
                                       elapsed_s=302.0)
    )

    record = chat_rail.get_response("cyc-chat")
    assert record is not None
    assert record["delivery"]["failed"] is True
    assert "300" in record["text"]
    assert episode.meta["turn_deadline_expired"]["stage"] == "auto"
    assert episode.meta["cascade_failure"]["error_type"] == "TurnDeadlineExpired"
    assert deadline is td.get("cyc-chat")     # released by the terminus, not here


def test_expiry_after_the_answer_shipped_posts_no_second_bubble(clock, chat_rail):
    import cognition_chain as cl

    td.start("cyc-chat", budget_s=300.0)
    clock.advance(302.0)
    episode = _episode(meta={"chat_reply_delivered_cycle": "cyc-chat"})

    cl._settle_turn_deadline_expiry(
        episode, td.TurnDeadlineExpired(key="cyc-chat", stage="omega", budget_s=300.0,
                                        elapsed_s=302.0)
    )

    assert chat_rail.get_response("cyc-chat") is None      # the operator already has the reply
    assert episode.meta["turn_deadline_expired"]["stage"] == "omega"
    assert episode.meta["cascade_failure"]["error_type"] == "TurnDeadlineExpired"


def test_exception_after_store_close_keeps_deadline_without_terminal_receipt(
    clock, monkeypatch
):
    import cognition_chain as cl

    contributor = SimpleNamespace(metadata={"cycle_id": "cyc-chat"})
    ignition = SimpleNamespace(pipeline_id=0, dominant_contributor=contributor)
    deadline = td.start("cyc-chat", budget_s=300.0)

    def terminal_failure(_store, _ignition):
        td.adopt("ep-7", "cyc-chat")
        raise RuntimeError("producer failed after episode was archived")

    monkeypatch.setattr(cl, "_ignite_episode_cascade_bound", terminal_failure)

    with pytest.raises(RuntimeError, match="episode was archived"):
        cl._ignite_episode_cascade(SimpleNamespace(current=lambda *_a: None), ignition)

    assert td.get("cyc-chat") is deadline
    assert td.get("ep-7") is deadline


def test_retryable_episode_keeps_deadline_after_cascade_exception(clock, monkeypatch):
    import cognition_chain as cl

    episode = _episode()
    contributor = SimpleNamespace(metadata={"cycle_id": "cyc-chat"})
    ignition = SimpleNamespace(pipeline_id=0, dominant_contributor=contributor)
    deadline = td.start("cyc-chat", budget_s=300.0)

    def retryable_failure(_store, _ignition):
        td.adopt("ep-7", "cyc-chat")
        raise RuntimeError("DeepSleep failed and left episode open")

    monkeypatch.setattr(cl, "_ignite_episode_cascade_bound", retryable_failure)

    with pytest.raises(RuntimeError, match="left episode open"):
        cl._ignite_episode_cascade(SimpleNamespace(current=lambda *_a: episode), ignition)

    assert td.get("cyc-chat") is deadline
    assert td.get("ep-7") is deadline


def test_a_failure_before_admission_keeps_the_queued_turn_deadline(clock, monkeypatch):
    import cognition_chain as cl

    contributor = SimpleNamespace(metadata={"cycle_id": "cyc-chat"})
    ignition = SimpleNamespace(dominant_contributor=contributor)
    deadline = td.start("cyc-chat", budget_s=300.0)

    def broken_before_admission(_store, _ignition):
        raise RuntimeError("episode store unavailable")

    monkeypatch.setattr(cl, "_ignite_episode_cascade_bound", broken_before_admission)

    with pytest.raises(RuntimeError, match="episode store unavailable"):
        cl._ignite_episode_cascade(SimpleNamespace(), ignition)

    assert td.get("cyc-chat") is deadline


def test_the_cascade_settles_and_closes_instead_of_crashing_the_loop(clock, chat_rail, monkeypatch):
    """A blown budget is a bounded outcome, not a programming fault: the turn is answered,
    the episode folds to its terminus with ``cascade_status=failed`` (§2.2), the pressure
    loop is not condemned, and the budget is released."""
    import cognition_chain as cl

    episode = _episode()
    ignition = SimpleNamespace(
        pipeline_id=0,
        opened_by="operator_query",
        work_kind="operator_turn",
        dominant_contributor=SimpleNamespace(
            source="operator", type="operator_query", metadata={"cycle_id": "cyc-chat"}
        ),
    )
    td.start("cyc-chat", budget_s=300.0)
    clock.advance(302.0)

    terminus: dict = {}

    def _expire(*_a, **_k):
        raise td.TurnDeadlineExpired(key="cyc-chat", stage="auto", budget_s=300.0,
                                     elapsed_s=302.0)

    monkeypatch.setattr(cl, "_admit_episode", lambda store, ign: (episode, ign))
    monkeypatch.setattr(cl, "_start_global_a", _expire)
    monkeypatch.setattr(cl, "_settle_plugin_permission_decision", lambda ep: None)
    monkeypatch.setattr(cl, "_record_operator_outcome", lambda store, ep: None)
    monkeypatch.setattr(cl, "_record_system_message", lambda ep: None)
    monkeypatch.setattr(cl, "_record_proactive_chat", lambda ep: None)
    monkeypatch.setattr(cl, "_announce_agent_completion", lambda ep, cascade_status: None)
    monkeypatch.setattr(cl, "_close_episode_sram", lambda ep_id: None)
    monkeypatch.setattr(
        cl,
        "_run_per_episode_terminus",
        lambda store, ep, *, cascade_status: terminus.update(status=cascade_status),
    )
    monkeypatch.setattr(cl._pipeline_state, "mark_inference", lambda *a, **k: None)
    monkeypatch.setattr(cl._pipeline_state, "mark_committing", lambda *a, **k: None)
    monkeypatch.setattr(cl._pipeline_state, "mark_idle", lambda *a, **k: None)

    store = SimpleNamespace(current=lambda: episode)

    closed = cl._ignite_episode_cascade(store, ignition)

    assert closed is episode
    assert terminus == {"status": "failed"}
    assert chat_rail.get_response("cyc-chat")["delivery"]["failed"] is True
    assert episode.meta["cascade_failure"]["error_type"] == "TurnDeadlineExpired"
    # Terminal cleanup: a closed turn holds no budget.
    assert td.get("cyc-chat") is None
    assert td.get("ep-7") is None
    assert td.current() is None

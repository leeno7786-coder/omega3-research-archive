from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest

from pressure_loop import pressure
from pressure_loop.omega_successor import stage_omega_successor
from schema.omega_control_decision import OmegaControlDecision


def control(intent="USE_SKILL", decision="PRESSURE", **browser):
    assessment = dict.fromkeys(("who", "what", "when", "where", "why", "how",
                                "desired_state", "observed_state"), "fact")
    assessment.update(gap="" if decision == "DONE" else "unfinished", evidence=[])
    p = dict(intent=intent, confidence=0.0, reason="finish", setpoints={},
             desired_state="complete", observed_state="partial", resolution_conditions=["verified"])
    p.update(browser)
    return OmegaControlDecision.from_dict(dict(assessment=assessment, decision=decision,
        pressure=None if decision == "DONE" else p, synthesis="summary"))


def episode(identity="parent"):
    return SimpleNamespace(episode_id=identity, global_={"omega_packet": {
        "original_objective": "original task", "omega_lane": "agent"}},
        opened_by="operator_query", pressure={"contributors": [
            {"id": "operator_query", "detail": {
                "cycle_id": "bubble", "agent_job_id": f"job-{identity}",
                "job_cycle_id": f"cycle-{identity}"}}]})


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(pressure, "_successor_path", lambda: tmp_path / "successors.json")
    monkeypatch.setattr(pressure, "_RESEED_STAGING", {})


def test_stage_replay_release_and_restart():
    first = stage_omega_successor(episode(), control())
    assert first.metadata["cycle_id"] != "bubble"
    assert first.metadata["original_cycle_id"] == "bubble"
    assert first.metadata["parent_episode_id"] == "parent"
    stage_omega_successor(episode(), control())
    pressure._RESEED_STAGING.clear()
    assert pressure.staged_signals() == (first,)
    assert pressure.release_staged(first)
    stage_omega_successor(episode(), control())
    assert pressure.staged_signals() == ()


def test_concurrent_same_domain_episodes_are_distinct():
    with ThreadPoolExecutor(2) as pool:
        rows = list(pool.map(lambda name: stage_omega_successor(episode(name), control()), ["a", "b"]))
    assert len(pressure.staged_signals()) == 2
    assert pressure.release_staged(rows[0])
    assert pressure.staged_signals() == (rows[1],)


def test_autonomous_parent_successor_pursues_omegas_desired_state():
    """An idle-learning episode carries no operator request to hand forward."""
    autonomous = SimpleNamespace(episode_id="autonomous-parent", global_={}, opened_by="clarification_gap",
                                 pressure={"contributors": [{"id": "clarification_gap", "detail": {}}]})

    row = stage_omega_successor(autonomous, control("ASK_OPERATOR", "ASK_OPERATOR"))

    assert (row.metadata["original_objective"], row.metadata["message"]) == ("complete", "complete")
    assert row.metadata["original_cycle_id"] == ""
    assert pressure.staged_signals() == (row,)


def test_successor_of_an_operator_successor_keeps_the_lineage():
    first = stage_omega_successor(episode(), control())
    child = SimpleNamespace(episode_id="child", global_={}, opened_by="omega_successor",
                            pressure={"contributors": [{"id": "omega_successor", "detail": dict(first.metadata)}]})

    second = stage_omega_successor(child, control())

    assert second is not None
    assert (second.metadata["original_cycle_id"], second.metadata["original_objective"]) == ("bubble", "original task")


def test_a_question_already_with_the_operator_is_not_asked_again():
    """The operator's reply continues an ASK; Omega re-asking before it arrives is a loop."""
    asked = stage_omega_successor(episode(), control("ASK_OPERATOR", "ASK_OPERATOR"))
    pressure.release_staged(asked)
    child = SimpleNamespace(episode_id="asked", global_={}, opened_by="omega_successor",
                            pressure={"contributors": [{"id": "omega_successor", "detail": dict(asked.metadata)}]})

    assert stage_omega_successor(child, control("ASK_OPERATOR", "ASK_OPERATOR")) is None
    assert pressure.staged_signals() == ()
    assert stage_omega_successor(child, control("USE_TOOL")) is not None


@pytest.fixture
def quiet_field(monkeypatch):
    monkeypatch.setattr("pressure_loop.pressure_field.build_registry", lambda: {})
    monkeypatch.setattr("memory.taxonomy.config.taxonomy_pressure_enabled", lambda: False)
    monkeypatch.setattr(pressure, "_rollback_event_contributor", lambda: None)


@pytest.mark.parametrize("idle_learning", [False, True])
def test_only_operator_work_continues_while_idle_learning_is_paused(quiet_field, monkeypatch, idle_learning):
    """Operator 2026-09-14: with no operator turn behind it, a chain runs only while idle
    learning is engaged; the operator's own work keeps its successors either way."""
    monkeypatch.setattr("pressure_loop.idle_wake.governor_enabled", lambda: idle_learning)
    operator_row = stage_omega_successor(episode("operator-parent"), control())
    autonomous = SimpleNamespace(episode_id="autonomous-parent", global_={}, opened_by="clarification_gap",
                                 pressure={"contributors": [{"id": "clarification_gap", "detail": {}}]})
    autonomous_row = stage_omega_successor(autonomous, control("USE_TOOL"))

    gathered = pressure.gather_contributors(None)

    assert operator_row in gathered
    assert (autonomous_row in gathered) is idle_learning
    if not idle_learning:
        assert pressure.staged_signals() == (operator_row,)
        assert autonomous_row.metadata["successor_id"] in pressure._read_successors()["admitted"]


def test_done_and_unvalidated_control():
    assert stage_omega_successor(episode(), control(decision="DONE")) is None
    with pytest.raises(ValueError):
        stage_omega_successor(episode(), control().to_dict())
    assert not pressure.staged_signals()


def test_ask_operator_and_validated_global_intent():
    from global_loop.intent.run import run
    row = stage_omega_successor(episode(), control("ASK_OPERATOR", "ASK_OPERATOR"))
    ignition = SimpleNamespace(dominant_contributor=row, opened_by=row.type)
    assert run(ignition) == "ASK_OPERATOR"
    row.metadata["directive_intent"] = "BROWSE"
    with pytest.raises(ValueError):
        run(ignition)


def _ignition(row, episode_id):
    from pressure_loop.contributors import EpisodeIgnition

    return EpisodeIgnition(episode_id, 1.0, row.type, row, (row,), None)


def test_gather_holds_successor_until_its_parent_episode_closes(monkeypatch):
    monkeypatch.setattr("pressure_loop.pressure_field.build_registry", lambda: {})
    monkeypatch.setattr("memory.taxonomy.config.taxonomy_pressure_enabled", lambda: False)
    monkeypatch.setattr(pressure, "_rollback_event_contributor", lambda: None)
    row = stage_omega_successor(episode(), control())
    store = SimpleNamespace(active_episodes=lambda: [SimpleNamespace(episode_id="parent")])
    assert row not in pressure.gather_contributors(None, episode_store=store)
    store.active_episodes = lambda: []
    assert row in pressure.gather_contributors(None, episode_store=store)


@pytest.mark.parametrize("intent,decision,lane", [
    ("USE_SKILL", "PRESSURE", "agent"),
    ("LEARN", "PRESSURE", "agent"),
    ("ASK_OPERATOR", "ASK_OPERATOR", "chat"),
])
def test_successor_lane_is_the_lane_global_a_authors(intent, decision, lane):
    from global_loop.loop import run_global_a

    row = stage_omega_successor(episode(), control(intent, decision))
    child = SimpleNamespace(episode_id="child", global_={})
    authored = run_global_a(child, _ignition(row, child.episode_id))
    assert (authored.intent, authored.omega_lane, row.metadata["omega_lane"], row.metadata["lane"]) == (
        intent, lane, lane, lane
    )


OBJECTIVE = "open Hugging Face and browse models under 9B"


def _run(status="budget_exhausted", steps=2):
    return {"status": status, "steps": steps, "actions": [
        {"step": 1, "action": "navigate", "summary": "navigate https://huggingface.co/models", "ok": True,
         "error": "", "url": "https://huggingface.co/models"}][:steps],
        "final_page": {"url": "https://huggingface.co/models", "title": "Models", "text": "Models"},
        "summary": "", "question": "", "stopped_reason": "", "operator_notes_seen": 0}


@pytest.fixture
def browser_parent(monkeypatch, tmp_path):
    from global_loop.loop import run_global_a
    from pressure_loop.contributors import make_contributor

    monkeypatch.setenv("OMEGA_BROWSER_WORKFLOW_PATH", str(tmp_path / "workflow.json"))
    from tests.helpers.intent_router import stub_router

    stub_router(monkeypatch, "BROWSER_AGENT")
    operator = make_contributor("operator", "operator_query", 1.0, {
        "message": OBJECTIVE, "directive_intent": "RESPOND"})
    parent = SimpleNamespace(episode_id="browser-parent", global_={}, opened_by="operator_query", pressure={
        "contributors": [{"id": "operator_query", "detail": {"cycle_id": "browser-bubble"}}]})
    run_global_a(parent, _ignition(operator, parent.episode_id))
    parent.global_["omega_packet"] = {"original_objective": OBJECTIVE, "omega_lane": "browser"}
    parent.auto = {"outputs": {"browser_run": _run()}}
    return parent


def _browse(reason="filter the catalog to models under 9B"):
    return control("BROWSE", reason=reason)


def test_browser_successor_continues_the_workflow_with_omegas_reason_as_its_instruction(browser_parent):
    from global_loop.loop import run_global_a
    from meta_loop.browser.workflow import record_browser_run

    parent = browser_parent
    workflow_id = parent.global_["browser_workflow"]["workflow_id"]
    # AUTO's run moved the durable revision past Global A's receipt.
    receipt = record_browser_run(workflow_id, _run())

    row = stage_omega_successor(parent, _browse())

    meta = row.metadata
    assert (meta["directive_intent"], meta["omega_lane"]) == ("BROWSE", "browser")
    assert (meta["original_objective"], meta["browser_workflow_id"]) == (OBJECTIVE, workflow_id)
    assert meta["message"] == "filter the catalog to models under 9B"
    assert meta["browser_workflow_revision"] == receipt["revision"]
    assert "plan_id" not in meta and "browser_transition" not in meta
    child = SimpleNamespace(episode_id="browser-child", global_={}, pressure={})
    authored = run_global_a(child, _ignition(row, child.episode_id))
    assert (authored.intent, authored.omega_lane) == ("BROWSE", "browser")
    assert child.global_["browser_workflow"]["workflow_id"] == workflow_id
    assert child.global_["browser_workflow"]["objective"] == OBJECTIVE


@pytest.mark.parametrize("ending", ["closed", "replaced", "blocked"])
def test_browser_successor_is_declined_when_its_workflow_can_no_longer_run(browser_parent, ending):
    from meta_loop.browser.workflow import close_browser_workflow, prepare_browser_turn, record_browser_run

    parent = browser_parent
    workflow_id = parent.global_["browser_workflow"]["workflow_id"]
    if ending == "blocked":
        for _ in range(3):
            record_browser_run(workflow_id, _run("blocked"))
    else:
        close_browser_workflow(workflow_id)
        if ending == "replaced":
            prepare_browser_turn("open a different site")

    assert stage_omega_successor(parent, _browse()) is None
    assert pressure.staged_signals() == ()


@pytest.mark.parametrize("outcome", ["blocked", "inference_failed", "busy", "note_queued", "perception_invalid"])
def test_browser_successor_is_not_staged_after_a_run_that_could_not_act(browser_parent, outcome):
    """A run that could not act fails identically on every retry; the operator resumes it."""
    from meta_loop.browser.workflow import browser_workflow_receipt

    parent = browser_parent
    if outcome == "perception_invalid":
        parent.auto = {"failure_kind": "browser_perception_invalid", "outputs": {}}
    else:
        parent.auto = {"outputs": {"browser_run": _run(outcome, steps=0)}}

    assert stage_omega_successor(parent, _browse()) is None
    assert pressure.staged_signals() == ()
    assert browser_workflow_receipt()["status"] == "waiting_for_operator"


@pytest.mark.parametrize("ending", ["still_runnable", "closed", "operator_follow_up"])
def test_gather_retires_a_staged_browser_successor_that_can_no_longer_run(browser_parent, monkeypatch, ending):
    from meta_loop.browser.workflow import close_browser_workflow, prepare_browser_turn

    monkeypatch.setattr("pressure_loop.pressure_field.build_registry", lambda: {})
    monkeypatch.setattr("memory.taxonomy.config.taxonomy_pressure_enabled", lambda: False)
    monkeypatch.setattr(pressure, "_rollback_event_contributor", lambda: None)
    parent = browser_parent
    row = stage_omega_successor(parent, _browse())
    assert row is not None
    workflow_id = row.metadata["browser_workflow_id"]
    if ending == "closed":
        close_browser_workflow(workflow_id)
    elif ending == "operator_follow_up":
        prepare_browser_turn("click the second model", cycle_id="follow-up")

    gathered = pressure.gather_contributors(None)

    if ending == "still_runnable":
        assert row in gathered
        assert pressure.staged_signals() == (row,)
    else:
        assert row not in gathered
        assert pressure.staged_signals() == ()
        # Retirement takes the admitted slot: the successor's lifecycle is over, not pending.
        assert row.metadata["successor_id"] in pressure._read_successors()["admitted"]


@pytest.fixture
def fifo(monkeypatch, tmp_path):
    from pressure_loop import cognition_queue

    monkeypatch.setattr(cognition_queue, "_queue_path", lambda: tmp_path / "queue.json")
    cognition_queue._reset_cache_for_tests()
    yield cognition_queue
    cognition_queue.clear_inputs()


def test_staged_successors_travel_the_fifo_and_die_only_at_admission(monkeypatch, fifo):
    from pressure_loop.policy import pressure_open_threshold

    monkeypatch.setattr("pressure_loop.pressure_field.build_registry", lambda: {})
    monkeypatch.setattr("memory.taxonomy.config.taxonomy_pressure_enabled", lambda: False)
    monkeypatch.setattr(pressure, "_rollback_event_contributor", lambda: None)
    by_parent = lambda rows: sorted(rows, key=lambda row: row.metadata["parent_episode_id"])  # noqa: E731
    staged = by_parent(stage_omega_successor(episode(name), control()) for name in ("a", "b"))

    gathered = pressure.gather_contributors(None)
    assert by_parent(gathered) == staged
    for row in staged:
        assert pressure.evaluate([row], active_episode=False, neuromod={},
                                 open_threshold=pressure_open_threshold()) is not None
        assert fifo.enqueue_input(row, operator_activity=False)
    first = fifo.lease_next_input(2)
    assert first is not None
    assert first.metadata["parent_episode_id"] == "a"
    assert by_parent(pressure.staged_signals()) == staged  # polling and leasing consume nothing
    assert fifo.acknowledge_input(first, 2)
    assert pressure.release_staged(first)
    second = fifo.lease_next_input(2)
    assert second is not None
    assert second.metadata["parent_episode_id"] == "b"
    assert fifo.acknowledge_input(second, 2)
    assert pressure.release_staged(second)
    assert pressure.staged_signals() == ()


@pytest.mark.parametrize("decision,intent,setpoints", [
    ("PRESSURE", "LEARN", {"mastery_debt_delta": 1.0}),
    ("ASK_OPERATOR", "ASK_OPERATOR", {"clarification_debt_delta": 0.9}),
    ("PRESSURE", "USE_SKILL", {"execution_debt_delta": 0.8}),
])
def test_one_omega_decision_produces_exactly_one_live_pressure(quiet_field, monkeypatch, decision, intent, setpoints):
    """Plan Task 13: the strict control envelope is the only per-episode pressure authority.

    Its projected setpoints and intent ride omega_result for the journal and ignite nothing."""
    import cognition_chain as chain
    from omega_loop.episode.types import OmegaEpisodeResult
    from organ.promptorgan.packs.meta_omega.core import project_control

    monkeypatch.setattr("pressure_loop.idle_wake.governor_enabled", lambda: True)
    for name in ("_advance_proactive_lifecycle", "_ingest_fold_knowledge", "_apply_onboarding_subtopics",
                 "_persist_motor_attempt_and_pressure", "_apply_build_report_decision"):
        monkeypatch.setattr(chain, name, lambda *args: None)
    data = control(intent, decision).to_dict()
    data["pressure"]["setpoints"] = setpoints
    decided = OmegaControlDecision.from_dict(data)
    parent = episode("fold-parent")
    result = project_control(OmegaEpisodeResult("fold-parent", True, (), (), {}, None, "summary", None), decided)
    assert result.pressure_params == setpoints  # still projected for the journal

    chain._apply_omega_episode_result(parent, result)
    chain._settle_operator_turn(parent, decided)
    live = [c for c in pressure.gather_contributors(None) if c.source == "omega"]

    assert [(c.type, c.metadata["parent_episode_id"], c.metadata["directive_intent"]) for c in live] == [
        ("omega_successor", "fold-parent", intent)]


def test_an_agent_successor_carries_its_batch_results_forward():
    """The next terminal-agent batch sees what this one found (operator 2026-09-14)."""
    parent = episode("agent-parent")
    parent.auto = {"outputs": {"operator_tool_calls": [
        {"tool_name": "list_directory", "args": {"path": "."}, "status": "success", "output": "FILE\ta.py", "error": ""}]}}

    row = stage_omega_successor(parent, control("USE_TOOL"))

    assert row.metadata["agent_observations"] == [
        {"tool": "list_directory", "args": '{"path": "."}', "status": "success", "output": "FILE\ta.py", "error": ""}]
    child = SimpleNamespace(episode_id="agent-child", global_={}, opened_by="omega_successor",
                            pressure={"contributors": [{"id": "omega_successor", "detail": dict(row.metadata)}]},
                            auto={"outputs": {"operator_tool_calls": [
                                {"tool_name": "read_files", "args": {"path": "a.py"}, "status": "success",
                                 "output": "print(1)", "error": ""}]}})
    grandchild = stage_omega_successor(child, control("USE_TOOL"))
    assert [row["tool"] for row in grandchild.metadata["agent_observations"]] == ["list_directory", "read_files"]


def test_a_window_reflection_successor_has_no_operator_lineage(quiet_field, monkeypatch):
    """Live 2026-09-15: a window contributor's own cycle id passed as operator lineage, so the
    window's successor ran with idle learning paused and crashed Meta A."""
    from pressure_loop.window_lane import WINDOW_REFLECTION_READY

    monkeypatch.setattr("pressure_loop.idle_wake.governor_enabled", lambda: False)
    window = SimpleNamespace(episode_id="window-parent", global_={}, opened_by=WINDOW_REFLECTION_READY,
                             pressure={"contributors": [{"id": WINDOW_REFLECTION_READY, "detail": {
                                 "cycle_id": "window:win-1", "window_id": "win-1"}}]})

    row = stage_omega_successor(window, control("USE_TOOL"))

    assert row.metadata["original_cycle_id"] == ""
    assert row not in pressure.gather_contributors(None)


def test_omega_cannot_choose_pursue_outcome():
    from schema.omega_control_decision import PRESSURE_INTENTS

    assert "PURSUE_OUTCOME" not in PRESSURE_INTENTS
    with pytest.raises(ValueError, match="pressure intent"):
        control("PURSUE_OUTCOME")

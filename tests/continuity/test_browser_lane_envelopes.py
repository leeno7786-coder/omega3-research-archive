"""Browser outcomes retain instruction and failure evidence through publication."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from browser_agent.agent_loop import BrowserAgentRun, RunBudget
from browser_agent.controller import BrowserControllerError
from tests.browser_agent.test_agent_loop import FakeController, Seat, Sink, call
from tests.auto_loop.test_browser_run import pack

browser_pack = pack


def test_failed_operator_notes_read_stops_before_another_action():
    controller = FakeController(window_open=True)
    seat = Seat(call("done", summary="Incorrect success"))

    def unavailable():
        raise OSError("BROWSER-NOTES-SENTINEL unavailable")

    receipt = BrowserAgentRun(seat, controller, objective="Inspect the page", notes_reader=unavailable).run()
    assert receipt["status"] == "blocked"
    assert "OSError: BROWSER-NOTES-SENTINEL unavailable" in receipt["stopped_reason"]
    assert seat.requests == []
    assert receipt["actions"] == []


@pytest.mark.parametrize("name,args", [("done", {}), ("ask_operator", {"question": ""})])
def test_invalid_terminal_call_cannot_claim_a_successful_outcome(name, args):
    receipt = BrowserAgentRun(
        Seat(call(name, **args)), FakeController(window_open=True), objective="Inspect the page",
        budget=RunBudget(max_inference_failures=0),
    ).run()
    assert receipt["status"] == "inference_failed"
    assert "invalid" in receipt["stopped_reason"].lower()
    assert not any(row["ok"] for row in receipt["actions"])


def test_observation_failure_does_not_rewrite_the_completed_action():
    class Controller(FakeController):
        def observe(self):
            if self.calls:
                raise BrowserControllerError("BROWSER-OBSERVATION-SENTINEL unavailable")
            return super().observe()

    controller = Controller(window_open=True)
    sink = Sink()
    receipt = BrowserAgentRun(
        Seat(call("click", x=500, y=100)), controller, objective="Click the button", publish=sink,
    ).run()
    assert len(controller.calls) == 1 and controller.calls[0][0] == "click"
    assert receipt["status"] == "blocked"
    assert "BROWSER-OBSERVATION-SENTINEL" in receipt["stopped_reason"]
    assert receipt["actions"][0]["ok"] is True
    assert receipt["actions"][0]["error"] == ""
    assert sink.of("chat.tool_result")[0]["ok"] is True


def test_provider_failure_keeps_the_reported_cause():
    receipt = BrowserAgentRun(
        Seat({"ok": False, "error": "BROWSER-PROVIDER-SENTINEL disconnected"}),
        FakeController(window_open=True), objective="Inspect the page",
        budget=RunBudget(max_inference_failures=0),
    ).run()
    assert receipt["status"] == "inference_failed"
    assert "BROWSER-PROVIDER-SENTINEL disconnected" in receipt["stopped_reason"]


def test_operator_update_reaches_the_browser_model_in_full():
    instruction = "Inspect the report. " * 80 + "BROWSER-INSTRUCTION-SENTINEL: do not submit it."
    seat = Seat(call("done", summary="Inspected the report."))
    receipt = BrowserAgentRun(
        seat, FakeController(window_open=True), objective="Inspect the report",
        notes_reader=lambda: [instruction],
    ).run()
    assert receipt["operator_notes_seen"] == 1
    assert any(instruction in str(message["content"]) for message in seat.requests[0]["messages"])


def test_workflow_hands_off_the_complete_operator_update():
    from meta_loop.browser.workflow import prepare_browser_turn, take_operator_notes

    initial = prepare_browser_turn("Inspect the report", cycle_id="browser-initial")
    instruction = "Inspect the report. " * 80 + "BROWSER-INSTRUCTION-SENTINEL: do not submit it."
    prepare_browser_turn(instruction, cycle_id="browser-followup")
    assert take_operator_notes(initial["workflow"]["workflow_id"]) == [instruction]


def test_workflow_never_discards_undelivered_operator_updates():
    from meta_loop.browser.workflow import prepare_browser_turn, take_operator_notes

    initial = prepare_browser_turn("Inspect the report", cycle_id="browser-initial")
    instructions = [f"Inspect numbered page {number}." for number in range(40)]
    for number, instruction in enumerate(instructions):
        prepare_browser_turn(instruction, cycle_id=f"browser-followup-{number}")
    # Steps take bounded batches; across them every update arrives, in order.
    workflow_id = initial["workflow"]["workflow_id"]
    delivered: list[str] = []
    while batch := take_operator_notes(workflow_id):
        delivered.extend(batch)
    assert delivered == instructions


@pytest.mark.parametrize("queued", [False, True])
def test_busy_browser_receipt_distinguishes_queued_input_from_no_handoff(browser_pack, monkeypatch, queued):
    from auto_loop.execute.run import _execute_browser_run
    from meta_loop.browser.workflow import prepare_browser_turn

    monkeypatch.setattr("browser_agent.agent_loop.is_running", lambda: True)
    if queued:
        prepare_browser_turn("Read the second page too", cycle_id="followup")
    result = _execute_browser_run(lambda **_: None, browser_pack, None, episode_id="browser-busy")
    assert result["outputs"]["browser_run"]["status"] == ("note_queued" if queued else "busy")


def test_missing_workflow_receipt_is_an_explicit_failure_with_the_action_preserved(browser_pack, monkeypatch):
    from auto_loop.execute.run import _execute_browser_run

    monkeypatch.setattr("browser_agent.agent_loop.run_browser_agent", lambda *_args, **_kwargs: {
        "status": "done", "steps": 1, "actions": [], "summary": "BROWSER-ACTION-SENTINEL completed",
    })
    monkeypatch.setattr("meta_loop.browser.workflow.record_browser_run", lambda *_args: None)
    result = _execute_browser_run(lambda **_: None, browser_pack, None, episode_id="browser-orphan")
    assert result["failure_kind"] == "browser_run_failed"
    assert "workflow" in result["failure_reason"]
    assert result["outputs"]["browser_run"]["summary"] == "BROWSER-ACTION-SENTINEL completed"


def test_failed_browser_notes_are_learned_through_the_real_cascade(monkeypatch, tmp_path):
    from memory import episode_commit_view, episode_sram
    from memory.engine import walker
    from tests.helpers.blueprint_driver import BlueprintDriver, browser_action, browser_decision

    driver = BlueprintDriver(monkeypatch=monkeypatch, tmp_path=tmp_path)
    driver.browser_actions.append(browser_action("done", summary="Incorrect success"))
    driver.omega.append(browser_decision("DONE", synthesis="The browser could not read its update."))

    def unavailable(*_args):
        raise OSError("BROWSER-NOTES-CANONICAL-SENTINEL unavailable")

    monkeypatch.setattr("meta_loop.browser.workflow.take_operator_notes", unavailable)
    episode = driver.operator_turn("Inspect the report in the browser", route="BROWSER_AGENT",
                                   cycle_id="browser-failed-notes-cycle")
    ref = episode_commit_view.episode_record_ref(episode.episode_id)
    assert ref is not None
    walker.load_coord_index()
    document = json.loads(Path(walker.path_for_id(ref.record_id)).read_text(encoding="utf-8"))
    body = json.loads(document["text"])
    assert "BROWSER-NOTES-CANONICAL-SENTINEL" in json.dumps(body)
    assert body["outputs"]["browser_run"]["status"] == "blocked"
    assert not episode_sram.is_open(episode.episode_id)
    assert driver.store.current() is None

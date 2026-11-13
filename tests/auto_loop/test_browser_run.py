"""AUTO runs the browser agent from Meta A's perception and records the receipt; it never judges it."""
from __future__ import annotations

import base64
from io import BytesIO
from types import SimpleNamespace

import pytest

from meta_loop.browser.perception import capture_browser_perception


def png(width=8, height=6):
    from PIL import Image

    encoded = BytesIO()
    Image.new("RGB", (width, height), (40, 90, 160)).save(encoded, format="PNG")
    return encoded.getvalue()


class Controller:
    session_id = "s1"
    locked_window = "w1"

    def ensure_task_tab_alive(self):
        return "live"

    def read_snapshot(self):
        return {"session_id": self.session_id, "locked_window": self.locked_window,
                "url": "https://example.org", "title": "Catalog", "page_text": "Catalog of models",
                "screenshot": base64.b64encode(png()).decode()}


@pytest.fixture
def pack(monkeypatch, tmp_path):
    from auto_loop.execute import brain_skill_executor as executor
    from meta_loop.browser.workflow import prepare_browser_turn

    monkeypatch.setenv("OMEGA_BROWSER_WORKFLOW_PATH", str(tmp_path / "browser.json"))
    controller = Controller()
    monkeypatch.setattr(executor, "_browser_controller", lambda: controller)
    perception, _ = capture_browser_perception(controller, ocr=lambda _: "Catalog")
    workflow = prepare_browser_turn("inspect the catalog", cycle_id="bubble")["workflow"]
    return {"action": "BROWSER_ACTION", "tool": "browser_agent", "execution_inputs": {
        "browser_perception": perception.to_dict(), "objective": "inspect the catalog",
        "current_instruction": "inspect the catalog", "browser_workflow": workflow,
        "browser_workflow_id": workflow["workflow_id"]}}


def test_auto_runs_the_agent_records_the_run_and_publishes_frames(pack, monkeypatch):
    from auto_loop.execute.run import _execute_browser_run
    from browser_agent import agent_loop
    from meta_loop.browser.workflow import browser_workflow_receipt

    seen = {}

    def fake_run(generate, controller, **kwargs):
        seen.update(kwargs, controller=controller)
        kwargs["publish"]("chat.browser_frame", call_id="c1", step=1, tool="navigate", ok=True, error="",
                          url="https://example.org/models", title="Models", image="data:image/jpeg;base64,x",
                          width=10, height=6)
        return {"status": "done", "steps": 1, "actions": [
            {"step": 1, "action": "navigate", "summary": "navigate https://example.org/models", "ok": True,
             "error": "", "url": "https://example.org/models"}],
            "final_page": {"url": "https://example.org/models", "title": "Models", "text": "Models list"},
            "summary": "The catalog is open.", "question": "", "stopped_reason": "", "duration_s": 1.0,
            "operator_notes_seen": 0, "inference_failures": 0}

    monkeypatch.setattr(agent_loop, "run_browser_agent", fake_run)
    events = []
    sink = SimpleNamespace(event=lambda kind, **fields: events.append((kind, fields)))

    result = _execute_browser_run(lambda **_: None, pack, None, episode_id="ep-1", token_sink=sink)

    assert result["failure_kind"] is None
    run = result["outputs"]["browser_run"]
    assert run["status"] == "done" and run["cycle_id"] == "ep-1"
    assert seen["objective"] == "inspect the catalog" and seen["instruction"] == "inspect the catalog"
    assert callable(seen["notes_reader"]) and seen["progress"] == ""
    assert result["outputs"]["operator_tool_calls"] == [{
        "tool_call_id": "browser-1", "tool_name": "navigate", "args": {"summary": "navigate https://example.org/models"},
        "status": "success", "output": "navigate https://example.org/models", "error": ""}]
    assert events == [("chat.browser_frame", {"call_id": "c1", "step": 1, "tool": "navigate", "ok": True, "error": "",
                                              "url": "https://example.org/models", "title": "Models",
                                              "image": "data:image/jpeg;base64,x", "width": 10, "height": 6})]
    receipt = browser_workflow_receipt(pack["execution_inputs"]["browser_workflow_id"])
    assert receipt["run_count"] == 1 and receipt["last_page"]["url"] == "https://example.org/models"
    assert result["outputs"]["browser_workflow"]["last_run"]["status"] == "done"


def test_omegas_lane_entry_direction_reaches_the_browser_agent(pack, monkeypatch):
    """Integration spec section 4 (T9): the run is handed the host-rendered block, whole."""
    from auto_loop.execute.run import _execute_browser_run
    from browser_agent import agent_loop

    seen = {}
    monkeypatch.setattr(agent_loop, "run_browser_agent",
                        lambda generate, controller, **kwargs: seen.update(kwargs) or {
                            "status": "done", "steps": 0, "actions": [], "summary": "",
                            "final_page": {"url": "", "title": "", "text": ""}})
    direction = "OMEGA'S DIRECTION " + "z" * 5000

    gate = lambda: None  # noqa: E731
    should_stop = lambda: False  # noqa: E731
    _execute_browser_run(lambda **_: None, pack, None, episode_id="ep-d", direction=direction,
                         before_action=gate, should_stop=should_stop)
    assert seen["direction"] == direction and seen["before_action"] is gate
    assert seen["should_stop"] is should_stop

    _execute_browser_run(lambda **_: None, pack, None, episode_id="ep-e")
    assert seen["direction"] == "" and seen["before_action"] is None


def test_a_blocked_run_is_auto_s_failure_but_still_recorded(pack, monkeypatch):
    from auto_loop.execute.run import _execute_browser_run
    from browser_agent import agent_loop

    monkeypatch.setattr(agent_loop, "run_browser_agent", lambda *a, **k: {
        "status": "blocked", "steps": 2, "actions": [], "final_page": {"url": "", "title": "", "text": ""},
        "summary": "", "question": "", "stopped_reason": "3 actions failed in a row: click failed",
        "duration_s": 2.0, "operator_notes_seen": 0, "inference_failures": 0})

    result = _execute_browser_run(lambda **_: None, pack, None, episode_id="ep-2")

    assert result["failure_kind"] == "browser_run_failed"
    assert result["failure_reason"] == "3 actions failed in a row: click failed"
    assert result["outputs"]["browser_workflow"]["failures"] == 1


def test_a_stale_session_never_runs_the_agent(pack, monkeypatch):
    from auto_loop.execute import brain_skill_executor as executor
    from auto_loop.execute.run import _execute_browser_run
    from browser_agent import agent_loop

    monkeypatch.setattr(executor, "_browser_controller", lambda: SimpleNamespace(session_id="other", locked_window="w1"))
    monkeypatch.setattr(agent_loop, "run_browser_agent", lambda *a, **k: pytest.fail("must not run"))

    result = _execute_browser_run(lambda **_: None, pack, None, episode_id="ep-3")

    assert result["failure_kind"] == "browser_perception_invalid"
    assert "stale browser session" in result["failure_reason"]


def test_a_turn_while_another_run_owns_the_window_only_delivers_its_note(pack, monkeypatch):
    """Operator 2026-09-15: chat with Omega while it works; the note reaches the running loop."""
    from auto_loop.execute.run import _execute_browser_run
    from browser_agent import agent_loop
    from meta_loop.browser.workflow import prepare_browser_turn

    prepare_browser_turn("use the email me@example.test", cycle_id="bubble-2")
    monkeypatch.setattr(agent_loop, "is_running", lambda: True)
    monkeypatch.setattr(agent_loop, "run_browser_agent", lambda *a, **k: pytest.fail("must not start a second run"))

    result = _execute_browser_run(lambda **_: None, pack, None, episode_id="ep-4")

    assert result["failure_kind"] is None
    run = result["outputs"]["browser_run"]
    assert run["status"] == "note_queued" and "1 note(s) queued" in run["stopped_reason"]


def test_meta_a_perception_reports_busy_instead_of_looking_during_a_run(monkeypatch):
    from browser_agent import agent_loop

    monkeypatch.setattr(agent_loop, "is_running", lambda: True)

    class Untouchable:
        def __getattr__(self, name):
            raise AssertionError(f"the window must not be touched: {name}")

    pack, attachment = capture_browser_perception(Untouchable())
    assert pack.status == "busy" and attachment is None


def test_a_closed_workflow_runs_nothing_and_reports_it(pack, monkeypatch):
    from auto_loop.execute.run import _execute_browser_run
    from browser_agent import agent_loop
    from meta_loop.browser.workflow import close_browser_workflow

    closed = close_browser_workflow(pack["execution_inputs"]["browser_workflow_id"])
    pack["execution_inputs"]["browser_workflow"] = closed
    monkeypatch.setattr(agent_loop, "run_browser_agent", lambda *a, **k: pytest.fail("must not run"))

    result = _execute_browser_run(lambda **_: None, pack, None, episode_id="ep-5")

    assert result["failure_kind"] is None
    assert result["outputs"]["browser_run"]["status"] == "blocked"
    assert "closed" in result["outputs"]["browser_run"]["stopped_reason"]

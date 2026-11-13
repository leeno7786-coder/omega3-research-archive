"""Omega's browser agent loop: one tool per turn, a screenshot after every action, bounded."""
from __future__ import annotations

import base64
import copy
import json
from collections import deque
from io import BytesIO

import pytest

from browser_agent import agent_loop
from browser_agent.agent_loop import BrowserAgentRun, RunBudget, TOOL_NAMES, run_browser_agent
from browser_agent.controller import BrowserControllerError


def png(width=2000, height=1200) -> bytes:
    from PIL import Image

    encoded = BytesIO()
    Image.new("RGB", (width, height), (30, 80, 140)).save(encoded, format="PNG")
    return encoded.getvalue()


PIXELS = png()


class FakeController:
    """Omega's window as the loop sees it: a viewport 1000 CSS px wide behind a 2000 px screenshot."""

    def __init__(self, *, window_open=False):
        self.session_id = "edge-session"
        self.locked_window = "omega-window" if window_open else None
        self.url = "https://example.test/" if window_open else ""
        self.title = "Example" if window_open else ""
        self.text = "Welcome to Example. Search below."
        self.elements = [{"tag": "input", "text": "Search", "x": 500, "y": 191, "type": "search"},
                         {"tag": "button", "text": "Go", "x": 640, "y": 191}]
        self.active = None
        self.calls: list[tuple] = []
        self.fail_next: list[str] = []
        self.banner_shown = 0

    # observation
    def show_task_banner(self):
        self.banner_shown += 1

    def page_state(self):
        return {"url": self.url, "title": self.title, "ready": "complete",
                "viewport": {"width": 1000, "height": 600, "dpr": 2.0},
                "scroll": {"x": 0, "y": 0, "height": 2400}, "text": self.text,
                "elements": list(self.elements), "active": self.active}

    def observe(self):
        state = self.page_state()
        state.update(screenshot=base64.b64encode(PIXELS).decode("ascii"), session_id=self.session_id,
                     locked_window=self.locked_window or "")
        return state

    def wait_ready(self, timeout_s=3.0, *, settle_s=0.4):
        return "complete"

    # actions
    def _maybe_fail(self, name):
        if self.fail_next and self.fail_next[0] == name:
            self.fail_next.pop(0)
            raise BrowserControllerError(f"{name} failed")

    def navigate_in_new_tab(self, url):
        self.calls.append(("navigate", url))
        self.locked_window = self.locked_window or "omega-window"
        self.url, self.title = url, "Page " + url
        return {"url": url, "title": self.title}

    def click_xy(self, x, y):
        self._maybe_fail("click")
        self.calls.append(("click", x, y))
        if (x, y) == (500, 191):
            self.active = {"tag": "input", "type": "search", "editable": True}
        if (x, y) == (10, 10):
            self.active = {"tag": "input", "type": "password", "editable": True}
        return {"x": x, "y": y}

    def type_text(self, text):
        self.calls.append(("type", text))
        return {"typed_chars": len(text)}

    def press_key(self, key):
        self.calls.append(("key", key))
        return {"key": key}

    def scroll(self, dx, dy, *, x=0, y=0):
        self.calls.append(("scroll", dx, dy, x, y))
        return {"delta_x": dx, "delta_y": dy}

    def drag(self, sx, sy, ex, ey, *, duration_ms=300):
        self.calls.append(("drag", sx, sy, ex, ey))
        return {}

    def move_mouse(self, x, y):
        self.calls.append(("move_mouse", x, y))
        return {"x": x, "y": y}

    def capture_full_page(self):
        self.calls.append(("see_page", "full"))
        return {"screenshot": base64.b64encode(png(400, 1600)).decode("ascii"),
                "width": 400, "height": 1600, "full_page": True, "clamped": False}

    def scan_viewports(self, screens):
        self.calls.append(("scan", screens))
        return [{"index": index, "screenshot": base64.b64encode(png(200, 120)).decode("ascii"),
                 "scroll_y": index * 600, "viewport_height": 600} for index in range(screens)]

    def extract(self, mode, *, selector="", attribute="text"):
        self.calls.append(("extract", mode, selector, attribute))
        return {"success": True, "mode": mode,
                "data": [{"title": "Pricing", "url": "https://example.test/pricing", "snippet": "Plans from $42"}]}

    def run_script(self, script, args=None):
        self.calls.append(("script", script, tuple(args or ())))
        if "arguments[0]" in script:
            return [{"tag": "a", "text": "Pricing", "x": 250, "y": 100}]
        return {"answer": 42}

    def back(self):
        self.calls.append(("back",))
        return {}

    def tabs(self):
        return {"handles": ["omega-window", "omega-popup"], "current": self.locked_window, "locked": self.locked_window}

    def switch_tab(self, handle):
        self.calls.append(("switch_tab", handle))
        self.locked_window = handle
        return {"handle": handle, "url": self.url, "title": self.title}


def call(name, **args):
    return {"ok": True, "text": "", "tool_calls": [{"id": f"call-{name}", "function": {"name": name, "arguments": json.dumps(args)}}]}


class Seat:
    """The model seat: scripted tool calls, remembering every request."""

    def __init__(self, *responses):
        self.responses = deque(responses)
        self.requests: list[dict] = []

    def __call__(self, **kwargs):
        stored = dict(kwargs)
        if isinstance(stored.get("messages"), list):
            stored["messages"] = copy.deepcopy(stored["messages"])
        self.requests.append(stored)
        assert self.responses, "the loop asked for more decisions than the scenario scripted"
        return self.responses.popleft()


class Sink:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __call__(self, kind, **fields):
        self.events.append((kind, fields))

    def of(self, kind):
        return [fields for name, fields in self.events if name == kind]


def _as_text(content) -> str:
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return str(content or "")


def run(seat, controller, sink=None, **kwargs):
    return BrowserAgentRun(seat, controller, objective=kwargs.pop("objective", "find the pricing page"),
                           publish=sink, **kwargs).run()


def test_the_model_chooses_tools_and_every_action_is_followed_by_a_screenshot_frame():
    controller = FakeController()
    sink = Sink()
    seat = Seat(call("navigate", url="https://example.test/"),
                call("click", x=784, y=300, target="search box"),
                call("type_text", text="pricing", submit=True),
                call("done", summary="The pricing page is open.", evidence="title says Pricing"))

    receipt = run(seat, controller, sink)

    assert receipt["status"] == "done" and receipt["steps"] == 4
    assert receipt["summary"] == "The pricing page is open."
    assert [row["action"] for row in receipt["actions"]] == ["navigate", "click", "type_text", "done"]
    assert all(row["ok"] for row in receipt["actions"])
    # No window yet: the first brief carries no image and says so; navigate opened Omega's window.
    first = seat.requests[0]["messages"]
    assert "No Omega window is open yet" in first[1]["content"]
    assert all(not isinstance(m.get("content"), list) for m in first)
    assert controller.calls[0] == ("navigate", "https://example.test/")
    # Screenshot pixels (2000 wide, sent at 1568) become CSS viewport pixels (1000 wide).
    assert ("click", 500, 191) in controller.calls
    assert ("type", "pricing") in controller.calls and ("key", "Enter") in controller.calls
    # The tools are in the model's prompt; the model decides which to call.
    assert {spec["function"]["name"] for spec in seat.requests[0]["tools"]} == TOOL_NAMES
    assert seat.requests[0]["tool_choice"] == "auto"
    # After every action: a tool_call, a tool_result and a browser frame with the screenshot.
    frames = sink.of("chat.browser_frame")
    assert [f["tool"] for f in frames] == ["navigate", "click", "type_text"], "no window, no frame at the start"
    assert all(f["image"].startswith("data:image/jpeg;base64,") for f in frames)
    assert frames[0]["url"] == "https://example.test/" and frames[0]["step"] == 1
    foldbacks = sink.of("chat.browser_foldback")
    assert [row["step"] for row in foldbacks] == [1, 2, 3]
    assert all("checking the next browser step" in row["message"] for row in foldbacks)
    progress_kinds = [kind for kind, _fields in sink.events]
    for index, kind in enumerate(progress_kinds):
        if kind == "chat.browser_frame":
            assert progress_kinds[index + 1] == "chat.browser_foldback"
    assert [f["tool"] for f in sink.of("chat.tool_call")] == ["navigate", "click", "type_text", "done"]
    assert [f["ok"] for f in sink.of("chat.tool_result")] == [True, True, True, True]
    # Only the newest screenshot stays in the transcript as pixels, and it is the
    # tool result of the action that produced it.
    last = seat.requests[-1]["messages"]
    tool_shots = [message for message in last if message.get("role") == "tool" and isinstance(message.get("content"), list)
                  and any(isinstance(part, dict) and part.get("type") == "image_url" for part in message["content"])]
    assert len(tool_shots) == 1
    assert any(isinstance(part, dict) and "Screenshot after this action" in str(part.get("text") or "")
               for part in tool_shots[0]["content"])
    assert controller.banner_shown >= 3


def test_operator_stop_during_inference_prevents_the_returned_browser_action():
    controller = FakeController()
    stopped = False

    def generate(**_kwargs):
        nonlocal stopped
        stopped = True
        return call("navigate", url="https://example.test/should-not-open")

    receipt = run_browser_agent(
        generate, controller, objective="open the pricing page", should_stop=lambda: stopped,
    )

    assert receipt["status"] == "stopped"
    assert receipt["stopped_reason"] == "operator requested stop"
    assert receipt["actions"] == []
    assert controller.calls == [], "a tool returned after the stop must not reach the browser"


def test_visible_controls_are_listed_in_screenshot_pixels_and_typing_needs_focus():
    controller = FakeController(window_open=True)
    seat = Seat(call("type_text", text="hello"),
                call("click", x=784, y=300),
                call("type_text", text="hello"),
                call("done", summary="typed"))

    sink = Sink()
    receipt = run(seat, controller, sink)

    assert sink.of("chat.browser_frame")[0]["tool"] == "observe", "an open window is shown before the first action"
    brief = seat.requests[0]["messages"][1]["content"]
    assert 'input "Search" input:search @ (784,299)' in brief and 'button "Go" @ (1004,299)' in brief
    assert "SCREENSHOT: 1568x941 px" in brief
    rows = receipt["actions"]
    assert rows[0]["ok"] is False and "click the field first" in rows[0]["error"]
    assert rows[2]["ok"] is True and controller.calls[-1] == ("type", "hello")
    assert any("Action failed" in _as_text(message.get("content")) for message in seat.requests[1]["messages"])


def test_password_text_never_reaches_the_frames_or_the_ledger():
    controller = FakeController(window_open=True)
    sink = Sink()
    seat = Seat(call("click", x=16, y=16), call("type_text", text="hunter2"), call("done", summary="signed in"))

    receipt = run(seat, controller, sink)

    assert ("type", "hunter2") in controller.calls
    shown = [f["args"] for f in sink.of("chat.tool_call") if f["tool"] == "type_text"]
    assert shown == [{"text": "••••••"}]
    assert "hunter2" not in json.dumps(receipt) and "hunter2" not in json.dumps(sink.events)
    assert receipt["actions"][1]["summary"] == "type 7 chars"


def test_ask_operator_ends_the_run_with_the_question():
    controller = FakeController(window_open=True)
    seat = Seat(call("ask_operator", question="Which email address should the account use?"))

    receipt = run(seat, controller)

    assert receipt["status"] == "ask_operator"
    assert receipt["question"] == "Which email address should the account use?"
    assert receipt["final_page"]["url"] == "https://example.test/"


def test_budgets_bound_the_run():
    controller = FakeController(window_open=True)
    seat = Seat(*[call("scroll", direction="down") for _ in range(4)])

    receipt = run(seat, controller, budget=RunBudget(max_steps=3))

    assert receipt["status"] == "budget_exhausted" and receipt["steps"] == 3
    assert len(controller.calls) == 3 and receipt["stopped_reason"] == "3 actions taken"


def test_three_failed_actions_in_a_row_block_the_run():
    controller = FakeController(window_open=True)
    controller.fail_next = ["click", "click", "click"]
    seat = Seat(*[call("click", x=100, y=100) for _ in range(3)])

    receipt = run(seat, controller)

    assert receipt["status"] == "blocked" and "3 actions failed in a row" in receipt["stopped_reason"]
    assert [row["ok"] for row in receipt["actions"]] == [False, False, False]


def test_prose_without_a_tool_call_is_nudged_then_fails_the_run():
    controller = FakeController(window_open=True)
    seat = Seat({"ok": True, "text": "I think the page is open.", "tool_calls": []},
                {"ok": True, "text": "Still thinking.", "tool_calls": []},
                {"ok": True, "text": "Done, I guess.", "tool_calls": []})

    receipt = run(seat, controller)

    assert receipt["status"] == "inference_failed" and receipt["steps"] == 0
    assert "Reply with exactly one tool call" in seat.requests[1]["messages"][-1]["content"]
    assert receipt["inference_failures"] == 3


def test_coordinates_outside_the_screenshot_are_refused_without_touching_the_page():
    controller = FakeController(window_open=True)
    seat = Seat(call("click", x=1700, y=100), call("done", summary="x"))

    receipt = run(seat, controller)

    assert receipt["actions"][0]["ok"] is False and "outside the 1568x941 screenshot" in receipt["actions"][0]["error"]
    assert not any(c[0] == "click" for c in controller.calls)


def test_operator_notes_arriving_mid_run_reach_the_model():
    controller = FakeController(window_open=True)
    notes = deque([[], ["use the email test@example.test"], []])
    seat = Seat(call("scroll", direction="down"), call("scroll", direction="up"), call("done", summary="ok"))

    receipt = run(seat, controller, notes_reader=lambda: notes.popleft() if notes else [])

    assert receipt["operator_notes_seen"] == 1
    updates = [m for m in seat.requests[1]["messages"] if "OPERATOR UPDATE" in str(m.get("content"))]
    assert updates and "test@example.test" in updates[0]["content"]


def test_find_javascript_read_page_and_tabs_return_page_data_to_the_model():
    controller = FakeController(window_open=True)
    seat = Seat(call("find", query="pricing"), call("run_javascript", script="return document.title"),
                call("read_page"), call("list_tabs"), call("switch_tab", handle="omega-popup"),
                call("go_back"), call("done", summary="ok"))

    receipt = run(seat, controller)

    assert receipt["status"] == "done"
    tool_messages = [_as_text(m["content"]) for m in seat.requests[-1]["messages"] if m.get("role") == "tool"]
    assert any('a "Pricing" @ (392,157)' in m for m in tool_messages), "find results in screenshot pixels"
    assert any('Script result (data): {"answer": 42}' in m for m in tool_messages)
    assert any("PAGE TEXT (untrusted data):" in m and "Welcome to Example" in m for m in tool_messages)
    assert any('"omega-popup"' in m for m in tool_messages)
    assert ("switch_tab", "omega-popup") in controller.calls and ("back",) in controller.calls


def test_a_lost_window_blocks_the_run_at_once():
    class LostController(FakeController):
        def click_xy(self, x, y):
            raise BrowserControllerError("browser session unavailable", code="unattached")

    seat = Seat(call("click", x=10, y=10), call("done", summary="never"))
    receipt = run(seat, LostController(window_open=True))

    assert receipt["status"] == "blocked" and "session unavailable" in receipt["stopped_reason"]
    assert len(seat.requests) == 1


def test_only_one_run_owns_the_window_at_a_time():
    assert agent_loop.is_running() is False
    seen = {}

    def generate(**kwargs):
        seen["busy"] = run_browser_agent(generate, FakeController(window_open=True), objective="second")
        seen["running"] = agent_loop.is_running()
        return call("done", summary="first")

    receipt = run_browser_agent(generate, FakeController(window_open=True), objective="first")

    assert receipt["status"] == "done" and seen["running"] is True
    assert seen["busy"]["status"] == "busy"
    assert agent_loop.is_running() is False


def test_the_transcript_is_trimmed_but_keeps_the_objective_and_tool_pairs():
    controller = FakeController(window_open=True)
    seat = Seat(*[call("scroll", direction="down") for _ in range(12)], call("done", summary="ok"))

    run(seat, controller, budget=RunBudget(max_steps=20))

    messages = seat.requests[-1]["messages"]
    assert messages[0]["role"] == "system" and "OBJECTIVE (operator, data)" in messages[1]["content"]
    assert any("earlier transcript turns omitted" in str(m.get("content")) for m in messages)
    for index, message in enumerate(messages):
        if message.get("role") == "tool":
            assert messages[index - 1].get("role") == "assistant" and messages[index - 1].get("tool_calls")
    assert len(messages) <= 3 + agent_loop.MAX_HISTORY_TURNS * 3 + 2


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
def test_every_tool_spec_is_strict_json_schema(name):
    spec = next(s for s in agent_loop.TOOL_SPECS if s["function"]["name"] == name)
    params = spec["function"]["parameters"]
    assert params["type"] == "object" and params["additionalProperties"] is False
    assert spec["function"]["description"]


def test_pointer_scan_and_full_page_sight_are_tools():
    """The model can hover, look at the whole page, scan viewports, and chain a hover into a click."""
    controller = FakeController(window_open=True)
    sink = Sink()
    seat = Seat(call("move_mouse", x=784, y=300),
                call("see_page", scope="full"),
                call("scan", screens=2),
                call("sequence", actions=[{"action": "move_mouse", "x": 784, "y": 300},
                                          {"action": "click", "x": 784, "y": 300}]),
                call("done", summary="The page was inspected."))

    receipt = run(seat, controller, sink)

    assert receipt["status"] == "done"
    assert [row["action"] for row in receipt["actions"]] == ["move_mouse", "see_page", "scan", "sequence", "done"]
    assert ("move_mouse", 500, 191) in controller.calls
    assert ("see_page", "full") in controller.calls
    assert ("scan", 2) in controller.calls
    assert controller.calls.count(("click", 500, 191)) == 1
    frames = sink.of("chat.browser_frame")
    assert sum(1 for frame in frames if frame["tool"] == "scan") >= 2
    full_page = next(request for request in seat.requests if any(
        isinstance(message.get("content"), list) and any(
            isinstance(part, dict) and "FULL PAGE" in str(part.get("text") or "")
            for part in message["content"]) for message in request["messages"]))
    assert full_page
    assert "hover" in agent_loop.SYSTEM_PROMPT and "whole page" in agent_loop.SYSTEM_PROMPT.lower()


def test_extract_and_screenshot_transcription_reach_the_model_as_data():
    """Search results and text painted in the screenshot are data the next decision can use."""
    controller = FakeController(window_open=True)

    class ReadingSeat(Seat):
        def __call__(self, **kwargs):
            if kwargs.get("tools") is None:
                stored = dict(kwargs)
                if isinstance(stored.get("messages"), list):
                    stored["messages"] = copy.deepcopy(stored["messages"])
                self.requests.append(stored)
                return {"ok": True, "text": "Total due $42", "tool_calls": []}
            return super().__call__(**kwargs)

    seat = ReadingSeat(call("extract", mode="search_results"), call("ocr"),
                       call("done", summary="Pricing is $42."))
    receipt = run(seat, controller)

    assert receipt["status"] == "done"
    assert ("extract", "search_results", "", "text") in controller.calls
    tools = [_as_text(message["content"]) for request in seat.requests for message in request["messages"]
             if message.get("role") == "tool"]
    assert any("Pricing" in content and "https://example.test/pricing" in content for content in tools)
    assert any("Total due $42" in content for content in tools)
    transcription = next(request for request in seat.requests if request.get("tools") is None)
    parts = transcription["messages"][0]["content"]
    assert any(isinstance(part, dict) and str(part.get("text") or "").startswith("Transcribe") for part in parts)
    assert any(isinstance(part, dict) and str((part.get("image_url") or {}).get("url") or "").startswith("data:image")
               for part in parts)
    assert "prefer extract" in agent_loop.SYSTEM_PROMPT.lower()


def test_browser_decisions_stream_and_a_repeated_click_stops_when_the_page_does_not_change():
    controller = FakeController(window_open=True)
    restarts = []

    class Sink:
        def __call__(self, token):
            return None

        def restart(self):
            restarts.append(1)

    sink = Sink()
    seat = Seat(call("click", x=784, y=300, target="search"),
                call("click", x=784, y=300, target="search"),
                call("done", summary="The search did not change."))

    receipt = run(seat, controller, stream=sink)

    assert controller.calls.count(("click", 500, 191)) == 1
    assert any("did not change" in row["error"] for row in receipt["actions"])
    assert seat.requests[0]["token_sink"] is sink
    assert len(restarts) == 3
    assert any("SCREENSHOT UNCHANGED" in str(message.get("content")) for message in seat.requests[1]["messages"])


def test_older_tool_results_keep_only_their_narrative():
    """Live 2026-09-15: eight full page states in the transcript grew a step to 22k prompt tokens."""
    controller = FakeController(window_open=True)
    seat = Seat(*[call("scroll", direction="down") for _ in range(4)], call("done", summary="ok"))

    run(seat, controller)

    tools = [_as_text(m["content"]) for m in seat.requests[-1]["messages"] if m.get("role") == "tool"]
    assert len(tools) == 4
    assert all("PAGE TEXT" not in older and "omitted" in older for older in tools[:-2])
    assert all("PAGE TEXT (untrusted data, excerpt)" in newest for newest in tools[-2:])
    assert all(older.startswith("Scrolled down 600px.") for older in tools[:-2])

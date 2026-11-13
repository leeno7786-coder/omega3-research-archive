"""Omega's browser controller over DevTools: one background tab of the operator's window."""
from __future__ import annotations

import json
from typing import Any

import pytest

from browser_agent.bootstrap import BrowserBootstrapError
from browser_agent.cdp_client import CdpError
from browser_agent.controller import BrowserController, BrowserControllerError


class FakeCdp:
    """The DevTools connection as the controller uses it; every call is remembered."""

    def __init__(self, address: str = "127.0.0.1:9222") -> None:
        self.address = address
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.pages_: list[dict[str, str]] = [{"id": "hud-tab", "url": "http://127.0.0.1:13310/", "title": "OMEGA"}]
        self.connected = False
        self.next_target = 0
        self.values: dict[str, Any] = {}
        self.history_: tuple[int, list[dict]] = (1, [{"id": 10}, {"id": 11}])

    def connect(self) -> str:
        self.connected = True
        self.calls.append(("connect", (self.address,)))
        return "browser-1"

    def alive(self) -> bool:
        return self.connected

    def close(self) -> None:
        self.connected = False
        self.calls.append(("close", ()))

    def pages(self):
        return [dict(page) for page in self.pages_]

    def create_page(self, url="about:blank", *, background=True):
        self.next_target += 1
        target = f"omega-tab-{self.next_target}"
        self.pages_.append({"id": target, "url": url, "title": ""})
        self.calls.append(("create_page", (url, background)))
        return target

    def attach(self, target_id):
        self.calls.append(("attach", (target_id,)))
        return f"session-{target_id}"

    def close_target(self, target_id):
        self.calls.append(("close_target", (target_id,)))
        self.pages_ = [page for page in self.pages_ if page["id"] != target_id]

    def evaluate(self, session, expression, *, timeout_s=None):
        self.calls.append(("evaluate", (session, expression)))
        if "elements.push" in expression:
            return {"url": "https://example.test/next", "title": "Example", "ready": "complete",
                    "viewport": {"width": 1000, "height": 600, "dpr": 1}, "scroll": {"x": 0, "y": 0, "height": 600},
                    "text": "hello", "elements": [{"tag": "a", "text": "Docs", "x": 10, "y": 20,
                                                   "href": "https://x.test/?token=1"}], "active": None}
        if "omega-agent-cursor" in expression:
            return True
        if "omega-extract" in expression:
            return {"success": True, "mode": "links",
                    "data": [{"text": "Docs", "url": "https://example.test/docs?token=1"}]}
        if "scrollBy" in expression:
            self.calls.append(("scroll", tuple(json.loads(expression.rsplit(".apply(null, ", 1)[1][:-1]))))
            return "window"
        if "location.href" in expression:
            return {"url": "https://example.test/next?token=secret", "title": "Example"}
        if "readyState" in expression:
            return "complete"
        if "innerText" in expression and "elements" not in expression:
            return "hello page"
        if "querySelector" in expression:
            return self.values.get("querySelector", [40, 50] if "getBoundingClientRect" in expression else True)
        return self.values.get("evaluate")

    def screenshot(self, session):
        self.calls.append(("screenshot", (session,)))
        return "image-b64"

    def navigate(self, session, url):
        self.calls.append(("navigate", (session, url)))

    def mouse(self, session, **params):
        self.calls.append(("mouse", (params["type"], params.get("x"), params.get("y"))))

    def key(self, session, **params):
        self.calls.append(("key", (params["type"], params.get("key"), params.get("modifiers"), tuple(params.get("commands") or ()))))

    def insert_text(self, session, text):
        self.calls.append(("insert_text", (session, text)))

    def history(self, session):
        return self.history_

    def navigate_history(self, session, entry_id):
        self.calls.append(("navigate_history", (entry_id,)))

    def call(self, method, params=None, *, session=None, timeout_s=None):
        self.calls.append(("call", (method,)))
        return {}


def attached(cdp: FakeCdp | None = None) -> tuple[BrowserController, FakeCdp]:
    cdp = cdp or FakeCdp()
    controller = BrowserController(client_factory=lambda address: cdp, bootstrap_factory=object)
    controller.attach("127.0.0.1:9222")
    return controller, cdp


def test_attach_connects_over_devtools_and_owns_no_tab_yet() -> None:
    controller, cdp = attached()

    assert controller.session_id == "cdp-browser-1" and controller.locked_window is None
    assert cdp.calls[0] == ("connect", ("127.0.0.1:9222",))
    with pytest.raises(BrowserControllerError, match="no Omega tab"):
        controller.snapshot()


def test_the_task_tab_opens_in_the_background_and_the_operators_tabs_stay_foreign() -> None:
    """Operator 2026-09-15: a new tab in the same window, and it must not switch to it."""
    controller, cdp = attached()

    result = controller.navigate_in_new_tab("https://huggingface.co")

    assert ("create_page", ("about:blank", True)) in cdp.calls, "created in the background"
    assert ("attach", ("omega-tab-1",)) in cdp.calls
    assert ("navigate", ("session-omega-tab-1", "https://huggingface.co")) in cdp.calls
    assert not any(name == "call" and args[0] == "Target.activateTarget" for name, args in cdp.calls)
    assert result == {"url": "https://example.test/next?token=secret", "title": "Example"}
    assert controller.locked_window == "omega-tab-1"
    assert controller.owns_handle("omega-tab-1") and not controller.owns_handle("hud-tab")
    assert controller.tabs()["handles"] == ["omega-tab-1"], "the operator's tabs are never listed"
    # A second navigation reuses the live tab instead of opening another.
    controller.navigate_in_new_tab("https://huggingface.co/models")
    assert [name for name, _args in cdp.calls].count("create_page") == 1
    with pytest.raises(BrowserControllerError, match="locked to Omega's own tabs"):
        controller.switch_tab("hud-tab")


def test_actions_go_to_the_hidden_tab_through_input_events() -> None:
    controller, cdp = attached()
    controller.navigate_in_new_tab("https://example.test")
    cdp.calls.clear()

    controller.click_xy(40, 50)
    assert controller.type_text("typed") == {"typed_chars": 5}
    controller.press_key("Enter")
    controller.press_key("Control+a")
    controller.scroll(0, 600)
    controller.drag(40, 50, 140, 50, duration_ms=0)
    controller.click("#submit")
    controller.type_into("#query", "hello")

    mouse = [args for name, args in cdp.calls if name == "mouse"]
    assert mouse[:3] == [("mouseMoved", 40, 50), ("mousePressed", 40, 50), ("mouseReleased", 40, 50)]
    assert ("insert_text", ("session-omega-tab-1", "typed")) in cdp.calls
    keys = [args for name, args in cdp.calls if name == "key"]
    assert keys[0] == ("keyDown", "Enter", 0, ()) and keys[1] == ("keyUp", "Enter", 0, ())
    assert keys[2] == ("rawKeyDown", "a", 2, ("selectAll",)) and keys[3] == ("keyUp", "a", 2, ())
    assert ("scroll", (0, 0, 0, 600)) in cdp.calls, "scrolling is done by the page, never a wheel event"
    assert ("mousePressed", 40, 50) in mouse and ("mouseReleased", 140, 50) in mouse
    assert ("insert_text", ("session-omega-tab-1", "hello")) in cdp.calls
    assert "typed" not in str([c for c in cdp.calls if c[0] != "insert_text"])


def test_pointer_gestures_are_trusted_mouse_events_and_show_a_cursor() -> None:
    """A real browser agent moves a visible pointer with button state, including hover."""
    controller, cdp = attached()
    controller.navigate_in_new_tab("https://example.test")
    cdp.calls.clear()
    cdp.mouse_params = []
    original = cdp.mouse

    def remember(session, **params):
        cdp.mouse_params.append(dict(params))
        original(session, **params)

    cdp.mouse = remember

    controller.move_mouse(12, 34)
    controller.click_xy(40, 50)
    controller.click_xy(40, 50, button="right", click_count=2)
    controller.drag(10, 20, 80, 20, duration_ms=0)

    assert cdp.mouse_params[0] == {"type": "mouseMoved", "x": 12, "y": 34, "buttons": 0}
    moved, press, release = cdp.mouse_params[1:4]
    assert moved == {"type": "mouseMoved", "x": 40, "y": 50, "buttons": 0}
    assert press == {"type": "mousePressed", "x": 40, "y": 50, "button": "left", "buttons": 1, "clickCount": 1}
    assert release == {"type": "mouseReleased", "x": 40, "y": 50, "button": "left", "buttons": 0, "clickCount": 1}
    right = [row for row in cdp.mouse_params if row.get("button") == "right"]
    assert [(row["type"], row["clickCount"], row["buttons"]) for row in right] == [
        ("mousePressed", 1, 2), ("mouseReleased", 1, 0), ("mousePressed", 2, 2), ("mouseReleased", 2, 0)]
    drag_moves = [row for row in cdp.mouse_params if row["type"] == "mouseMoved" and row.get("buttons") == 1]
    assert drag_moves and drag_moves[-1]["x"] == 80 and drag_moves[-1]["y"] == 20
    cursor = [args[1] for name, args in cdp.calls if name == "evaluate" and "omega-agent-cursor" in args[1]]
    assert cursor, "the pointer is drawn into the page before the gesture"
    assert "12" in cursor[0] and "hover" in cursor[0]


def test_full_page_capture_clips_the_rendered_document() -> None:
    controller, cdp = attached()
    controller.navigate_in_new_tab("https://example.test")

    def call(method, params=None, *, session=None, timeout_s=None):
        cdp.calls.append(("call", (method, params)))
        if method == "Page.getLayoutMetrics":
            return {"cssContentSize": {"width": 1000, "height": 12000}}
        if method == "Page.captureScreenshot":
            return {"data": "full-page-b64"}
        return {}

    cdp.call = call
    captured = controller.capture_full_page()

    assert captured["full_page"] is True and captured["screenshot"] == "full-page-b64"
    assert captured["width"] == 1000 and captured["clamped"] is True
    shot = next(params for name, args in cdp.calls if name == "call" and args[0] == "Page.captureScreenshot" for params in [args[1]])
    assert shot["captureBeyondViewport"] is True and shot["clip"]["height"] == 8000 and shot["clip"]["scale"] == 1


def test_scan_scrolls_one_viewport_between_screenshots() -> None:
    controller, cdp = attached()
    controller.navigate_in_new_tab("https://example.test")
    cdp.calls.clear()
    shots = []
    original = cdp.screenshot

    def numbered(session):
        shots.append(original(session))
        return f"viewport-{len(shots)}"

    cdp.screenshot = numbered
    frames = controller.scan_viewports(2)

    assert [frame["screenshot"] for frame in frames] == ["viewport-1", "viewport-2"]
    assert ("scroll", (0, 0, 0, 600)) in cdp.calls
    assert frames[0]["scroll_y"] == 0


def test_extract_asks_the_page_and_returns_bounded_links() -> None:
    controller, cdp = attached()
    controller.navigate_in_new_tab("https://example.test")
    cdp.calls.clear()

    result = controller.extract("links")

    assert result["data"] == [{"text": "Docs", "url": "https://example.test/docs"}]
    assert any(name == "evaluate" and "omega-extract" in args[1] and "links" in args[1]
               for name, args in cdp.calls)


def test_history_navigation_uses_devtools_entries() -> None:
    controller, cdp = attached()
    controller.navigate_in_new_tab("https://example.test")

    controller.back()
    assert ("navigate_history", (10,)) in cdp.calls
    cdp.history_ = (0, [{"id": 10}, {"id": 11}])
    with pytest.raises(BrowserControllerError, match="no history entry"):
        controller.back()
    controller.forward()
    assert ("navigate_history", (11,)) in cdp.calls


def test_page_state_and_snapshot_are_bounded_page_data_with_public_links() -> None:
    controller, cdp = attached()
    controller.navigate_in_new_tab("https://example.test")

    state = controller.page_state()
    assert state["viewport"] == {"width": 1000, "height": 600, "dpr": 1.0}
    assert state["elements"] == [{"tag": "a", "text": "Docs", "x": 10, "y": 20, "href": "https://x.test/"}]
    observed = controller.observe()
    assert observed["screenshot"] == "image-b64" and observed["locked_window"] == "omega-tab-1"
    snapshot = controller.read_snapshot()
    assert snapshot["page_text"] == "hello page" and snapshot["session_id"] == "cdp-browser-1"
    assert snapshot["screenshot"] == "image-b64"


def test_a_closed_task_tab_releases_its_lock_and_a_dead_browser_releases_the_session() -> None:
    controller, cdp = attached()
    controller.navigate_in_new_tab("https://example.test")

    cdp.pages_ = [page for page in cdp.pages_ if page["id"] != "omega-tab-1"]
    assert controller.ensure_task_tab_alive() == "tab_closed" and controller.locked_window is None
    assert controller.ensure_task_tab_alive() == "live"
    cdp.connected = False
    assert controller.ensure_task_tab_alive() == "session_lost" and controller.session_id is None


def test_release_closes_omegas_tabs_and_leaves_the_operators() -> None:
    """Operator 2026-09-15: Omega's tab must not survive a shutdown to be restored on boot."""
    controller, cdp = attached()
    controller.navigate_in_new_tab("https://huggingface.co")
    cdp.pages_.append({"id": "omega-popup", "url": "https://x.test/", "title": "popup"})

    controller.release()

    closed = [args[0] for name, args in cdp.calls if name == "close_target"]
    assert closed == ["omega-tab-1", "omega-popup"] and [p["id"] for p in cdp.pages_] == ["hud-tab"]
    assert cdp.calls[-1] == ("close", ()) and controller.session_id is None
    assert controller.locked_window is None


def test_release_never_closes_the_last_page() -> None:
    controller, cdp = attached()
    controller.navigate_in_new_tab("https://huggingface.co")
    cdp.pages_ = [page for page in cdp.pages_ if page["id"] == "omega-tab-1"]

    controller.release()

    assert not any(name == "close_target" for name, _ in cdp.calls) and controller.session_id is None


def test_attach_with_auto_setup_starts_only_the_debugger_and_keeps_stable_codes() -> None:
    seen: dict[str, object] = {}

    class _Bootstrap:
        def ensure_debugger(self, **kwargs):
            seen.update(kwargs)

        def close(self):
            return None

    cdp = FakeCdp()
    controller = BrowserController(client_factory=lambda address: cdp, bootstrap_factory=lambda: _Bootstrap())
    controller.attach("127.0.0.1:9222", auto_setup=True)
    assert seen == {"debugger_address": "127.0.0.1:9222"}

    class _Broken:
        def ensure_debugger(self, **_kwargs):
            raise BrowserBootstrapError("edge_debug_profile_unavailable")

        def close(self):
            return None

    controller = BrowserController(client_factory=lambda address: cdp, bootstrap_factory=lambda: _Broken())
    with pytest.raises(BrowserControllerError) as raised:
        controller.attach("127.0.0.1:9222", auto_setup=True)
    assert raised.value.code == "edge_debug_profile_unavailable"


def test_a_devtools_failure_is_an_attributable_controller_error() -> None:
    class _Refusing(FakeCdp):
        def mouse(self, session, **params):
            raise CdpError("Input.dispatchMouseEvent: target closed")

    controller, _cdp = attached(_Refusing())
    controller.navigate_in_new_tab("https://example.test")
    with pytest.raises(BrowserControllerError, match="pointer click failed: Input.dispatchMouseEvent"):
        controller.click_xy(1, 1)

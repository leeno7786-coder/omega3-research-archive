"""The first browser look attaches Omega's Edge session instead of perceiving nothing."""

import base64

from browser_agent.controller import BrowserControllerError
from meta_loop.browser.perception import capture_browser_perception


class ColdController:
    """No session until attach, like the process-wide controller after a boot."""

    def __init__(self, attach_error=None):
        self.session_id = None
        self.locked_window = None
        self.attach_calls = []
        self.attach_error = attach_error
        self.snapshots = 0

    def attach(self, debugger_address, *, auto_setup=False):
        self.attach_calls.append((debugger_address, auto_setup))
        if self.attach_error is not None:
            raise self.attach_error
        self.session_id = "fresh-session"
        return {"session_id": self.session_id, "debugger_address": debugger_address}

    def ensure_task_tab_alive(self):
        return "live"

    def read_snapshot(self):
        if not self.session_id:
            raise BrowserControllerError("browser session unavailable", code="unattached")
        self.snapshots += 1
        return {"session_id": self.session_id, "locked_window": self.locked_window, "url": "https://example.org/",
                "title": "Example", "screenshot": base64.b64encode(b"pixels").decode()}


def test_first_look_attaches_the_session_and_captures_no_operator_tab():
    """With no task tab yet the current tab is the operator's (the HUD); it is never captured."""
    controller = ColdController()

    pack, attachment = capture_browser_perception(controller, ocr=lambda _: "Omega")

    assert controller.attach_calls == [("127.0.0.1:9222", True)]
    assert (pack.status, pack.session_id, pack.has_screenshot) == ("no_task_tab", "fresh-session", False)
    assert attachment is None and controller.snapshots == 0


def test_an_attached_session_with_a_task_tab_is_perceived_without_reattaching():
    controller = ColdController()
    controller.session_id = "live-session"
    controller.locked_window = "task-tab"

    pack, attachment = capture_browser_perception(controller, ocr=lambda _: "Example")

    assert controller.attach_calls == []
    assert pack.status == "success" and attachment is not None and attachment.screenshot == b"pixels"


def test_a_failed_attach_is_an_explicit_perception_error():
    controller = ColdController(attach_error=BrowserControllerError("browser setup failed", code="edge_missing"))

    pack, attachment = capture_browser_perception(controller, ocr=lambda _: "Omega")

    assert attachment is None
    assert pack.status != "success" and pack.error == "edge_missing"

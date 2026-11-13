import base64
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from browser_agent.controller import BrowserController, BrowserControllerError


class ReadOnlyClient:
    """A DevTools connection that only answers reads; any actuation is a test failure."""

    def __init__(self):
        self.url = "https://huggingface.co/models"
        self.title = "Models"
        self.text = "Models\n  Filter by size   8B   7B   Trending"
        self.page_ids = ["hud", "w1"]
        self.live = True

    def alive(self):
        return self.live

    def pages(self):
        return [{"id": page, "url": "", "title": ""} for page in self.page_ids]

    def evaluate(self, session, expression, *, timeout_s=None):
        assert session == "ps1"
        if "location.href" in expression:
            return {"url": self.url, "title": self.title}
        if "innerText" in expression:
            return self.text
        raise AssertionError(f"Forbidden script: {expression[:60]}")

    def screenshot(self, session):
        return base64.b64encode(b"test-image").decode()

    def close(self):
        self.live = False

    def __getattr__(self, name):
        raise AssertionError(f"Forbidden DevTools access: {name}")


def controller():
    result = BrowserController()
    result._client = ReadOnlyClient()
    result._session_id = "s1"
    result._locked_window = "w1"
    result._page_session = "ps1"
    return result


def test_meta_a_prepares_the_browser_screen_for_the_agent_run(monkeypatch):
    from meta_loop.browser import perception
    from meta_loop.loop import run_meta_a
    from pressure_loop.episode import CognitiveEpisode

    ep = CognitiveEpisode(episode_id="e1")
    ep.global_ = {"intent_decision": {"intent": "BROWSE", "episode_id": "e1"},
                  "browser_workflow": {"workflow_id": "wf1", "revision": 2, "objective": "Compare models"}}
    monkeypatch.setattr(perception, "_controller", controller)
    monkeypatch.setattr(perception, "read_ocr", lambda image: "Models 8B 7B")
    pack = run_meta_a(ep)
    assert pack["action"] == "BROWSER_ACTION"
    assert pack["tool"] == "browser_agent"
    inputs = pack["execution_inputs"]
    assert inputs["objective"] == "Compare models"
    observed = inputs["browser_perception"]
    assert observed["url"] == "https://huggingface.co/models"
    assert observed["ocr_text"] == "Models 8B 7B"
    assert observed["has_screenshot"] is True
    assert observed["ocr_status"] == "success"
    assert "selector" not in inputs and "action" not in inputs
    assert pack["execution_constraints"]["no_execution_in_meta"] is True
    assert pack["execution_constraints"]["no_inference_in_meta"] is True
    assert "test-image" not in str(asdict(ep))
    assert base64.b64encode(b"test-image").decode() not in str(asdict(ep))
    assert ep._browser_perception_attachment.screenshot == b"test-image"
    from meta_loop.pack.run import validate_execution_action_pack

    inputs["browser_workflow_id"] = "different-workflow"
    with pytest.raises(ValueError, match="workflow identity"):
        validate_execution_action_pack(pack)


def test_read_snapshot_never_actuates_and_needs_omegas_tab():
    c = controller()
    assert c.read_snapshot()["locked_window"] == "w1"
    c._locked_window = None
    with pytest.raises(BrowserControllerError, match="no Omega tab"):
        c.read_snapshot()


def test_capture_keeps_visual_evidence_when_ocr_fails():
    from meta_loop.browser.perception import capture_browser_perception

    def broken(image):
        raise RuntimeError("OCR not installed")

    pack, attachment = capture_browser_perception(controller(), ocr=broken)
    assert pack.ocr_status == "failed"
    assert pack.status == "success"
    assert pack.has_screenshot and attachment.screenshot


def test_unattached_capture_is_explicit_unavailable(monkeypatch):
    from meta_loop.browser.perception import capture_browser_perception

    def no_browser(self, *args, **kwargs):
        raise BrowserControllerError("browser session unavailable", code="unattached")

    # Perception attaches before it looks; with no browser to attach to, it says so.
    monkeypatch.setattr(BrowserController, "attach", no_browser)
    pack, attachment = capture_browser_perception(BrowserController())
    assert pack.status == "unavailable"
    assert pack.error
    assert not pack.has_screenshot and attachment is None


def test_a_closed_task_tab_releases_its_lock_and_perceives_a_live_tab():
    """The operator closed Omega's task tab: the next look sees the tab that remains."""
    from meta_loop.browser.perception import capture_browser_perception

    c = controller()
    c._locked_window = "closed-task-tab"
    pack, attachment = capture_browser_perception(c, ocr=lambda image: "HUD")
    assert c.locked_window is None
    # The remaining tab is the operator's, not Omega's: nothing is captured until AUTO opens a task tab.
    assert pack.status == "no_task_tab" and attachment is None


def test_a_lost_session_is_released_and_attached_fresh(monkeypatch):
    """Edge restarted: the dead connection is dropped and never resumed."""
    from meta_loop.browser.perception import capture_browser_perception

    c = controller()
    c._client.live = False
    attached = []

    def attach(debugger_address, *, auto_setup=False):
        attached.append(auto_setup)
        c._client = ReadOnlyClient()
        c._session_id = "s1"
        return {"session_id": "s1"}

    monkeypatch.setattr(c, "attach", attach)
    pack, attachment = capture_browser_perception(c, ocr=lambda image: "text")
    assert attached == [True]
    assert pack.status == "no_task_tab" and pack.session_id == "s1" and attachment is None


def test_the_operator_s_tab_is_never_captured_before_omega_owns_a_task_tab():
    from meta_loop.browser.perception import capture_browser_perception

    c = controller()
    c._locked_window = None

    def forbidden(session):
        raise AssertionError("the operator's tab must not be screenshotted")

    c._client.screenshot = forbidden
    pack, attachment = capture_browser_perception(c, ocr=lambda image: "HUD")
    assert (pack.status, pack.has_screenshot, pack.url) == ("no_task_tab", False, "")
    assert attachment is None


def test_capture_bounds_untrusted_text_and_removes_url_credentials():
    from meta_loop.browser.perception import capture_browser_perception

    c = controller()
    c._client.url = "https://user:password@example.org/models?token=secret#private"
    c._client.title = "T" * 1000
    pack, _ = capture_browser_perception(c, ocr=lambda image: "O" * 9000)
    assert pack.url == "https://example.org/models"
    assert len(pack.title) == 500
    assert len(pack.ocr_text) == 4000


def test_perception_rejects_invalid_hash_and_unavailable_pixels():
    from schema.browser_perception import BrowserPerceptionPack

    with pytest.raises(ValueError, match="hash"):
        BrowserPerceptionPack(status="success", has_screenshot=True, screenshot_hash="x")
    with pytest.raises(ValueError, match="unavailable"):
        BrowserPerceptionPack(has_screenshot=True, screenshot_hash="a" * 64)


def test_a_screenshot_the_browser_refuses_is_unavailable_without_actuation():
    from browser_agent.cdp_client import CdpError
    from meta_loop.browser.perception import capture_browser_perception

    c = controller()

    def refuse(session):
        raise CdpError("Page.captureScreenshot: target crashed")

    c._client.screenshot = refuse
    pack, attachment = capture_browser_perception(c)
    assert pack.error == "snapshot_failed"
    assert pack.status == "unavailable"
    assert attachment is None


def test_perception_carries_the_page_s_readable_text_from_the_dom():
    """Operator 2026-09-15 ("add ocr"): Omega could not see what a page said. The readable text
    comes from the DOM through DevTools, exact where OCR would guess; OCR stays the optional
    channel for text rendered only as pixels."""
    from meta_loop.browser.perception import capture_browser_perception

    pack, _ = capture_browser_perception(controller())

    assert pack.page_text == "Models Filter by size 8B 7B Trending" and pack.text_status == "success"
    assert pack.ocr_status == "unavailable", "no OCR engine is installed here"
    assert pack.to_dict()["page_text"] == pack.page_text


def test_a_page_that_refuses_the_text_script_still_perceives():
    from browser_agent.cdp_client import CdpError
    from meta_loop.browser.perception import capture_browser_perception

    c = controller()
    original = c._client.evaluate

    def refuse(session, expression, *, timeout_s=None):
        if "innerText" in expression:
            raise CdpError("javascript disabled")
        return original(session, expression, timeout_s=timeout_s)

    c._client.evaluate = refuse
    pack, attachment = capture_browser_perception(c)
    assert pack.status == "success" and attachment is not None
    assert (pack.page_text, pack.text_status) == ("", "unavailable")

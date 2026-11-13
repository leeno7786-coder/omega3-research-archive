"""Execution-only browser controller for Omega's own tab, driven over DevTools (CDP).

Omega works in a tab of the operator's Edge window that it opened in the background: the
HUD stays in front and the operator decides when to look (operator 2026-09-15). Tabs that
existed before Omega opened its own are the operator's and are never listed, switched to,
captured or closed. Ownership lives only in this process and is released at shutdown.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from typing import Any

from .bootstrap import BrowserBootstrap, BrowserBootstrapError
from .cdp_client import CdpClient, CdpError
from .page_state import (
    CURSOR_SCRIPT,
    PAGE_STATE_SCRIPT,
    READY_STATE_SCRIPT,
    TASK_BANNER_SCRIPT,
    bounded_page_state,
)
from .extract import EXTRACT_MODES, EXTRACT_SCRIPT, bound_extract

MAX_FULL_PAGE_HEIGHT = 8000
_MOUSE_BUTTONS = {"left": 1, "right": 2, "middle": 4}

_PAGE_TEXT_SCRIPT = "return (document.body && document.body.innerText) ? document.body.innerText : '';"
_PAGE_INFO_SCRIPT = "return {url: location.href, title: document.title};"
_SCROLL_SCRIPT = """
const [x, y, dx, dy] = arguments;
let el = (x || y) ? document.elementFromPoint(x, y) : null;
while (el && el !== document.body && el !== document.documentElement) {
  const s = getComputedStyle(el);
  const scrolls = /(auto|scroll)/.test(s.overflowY + s.overflowX) && (el.scrollHeight > el.clientHeight || el.scrollWidth > el.clientWidth);
  if (scrolls) { el.scrollBy(dx, dy); return 'element'; }
  el = el.parentElement;
}
window.scrollBy(dx, dy);
return 'window';
"""

# DevTools key descriptors for the named keys a browser agent presses.
_NAMED_KEYS: dict[str, dict[str, Any]] = {
    "enter": {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r"},
    "return": {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r"},
    "tab": {"key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9},
    "escape": {"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
    "esc": {"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
    "backspace": {"key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8},
    "delete": {"key": "Delete", "code": "Delete", "windowsVirtualKeyCode": 46},
    "space": {"key": " ", "code": "Space", "windowsVirtualKeyCode": 32, "text": " "},
    "home": {"key": "Home", "code": "Home", "windowsVirtualKeyCode": 36},
    "end": {"key": "End", "code": "End", "windowsVirtualKeyCode": 35},
    "pageup": {"key": "PageUp", "code": "PageUp", "windowsVirtualKeyCode": 33},
    "pagedown": {"key": "PageDown", "code": "PageDown", "windowsVirtualKeyCode": 34},
    "arrowup": {"key": "ArrowUp", "code": "ArrowUp", "windowsVirtualKeyCode": 38},
    "up": {"key": "ArrowUp", "code": "ArrowUp", "windowsVirtualKeyCode": 38},
    "arrowdown": {"key": "ArrowDown", "code": "ArrowDown", "windowsVirtualKeyCode": 40},
    "down": {"key": "ArrowDown", "code": "ArrowDown", "windowsVirtualKeyCode": 40},
    "arrowleft": {"key": "ArrowLeft", "code": "ArrowLeft", "windowsVirtualKeyCode": 37},
    "left": {"key": "ArrowLeft", "code": "ArrowLeft", "windowsVirtualKeyCode": 37},
    "arrowright": {"key": "ArrowRight", "code": "ArrowRight", "windowsVirtualKeyCode": 39},
    "right": {"key": "ArrowRight", "code": "ArrowRight", "windowsVirtualKeyCode": 39},
    "f5": {"key": "F5", "code": "F5", "windowsVirtualKeyCode": 116},
}
_MODIFIERS = {"alt": 1, "control": 2, "ctrl": 2, "meta": 4, "command": 4, "cmd": 4, "shift": 8}
# Editing shortcuts DevTools runs as editor commands, so a chord works without native key handling.
_CHORD_COMMANDS = {(2, "a"): "selectAll", (2, "c"): "copy", (2, "v"): "paste", (2, "x"): "cut",
                   (2, "z"): "undo", (2, "y"): "redo"}


def _wrap(script: str, args: list[Any] | None = None) -> str:
    """A WebDriver-style script body (uses ``return`` and ``arguments``) as one expression."""
    return f"(function(){{\n{script}\n}}).apply(null, {json.dumps(list(args or []), ensure_ascii=False)})"


class BrowserControllerError(RuntimeError):
    """Stable browser-worker failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "browser_controller_error",
        details: dict[str, object] | None = None,
    ) -> None:
        self.code = str(code)
        self.details = dict(details or {})
        super().__init__(message)


class BrowserController:
    """Own one DevTools connection to Omega's Edge and one background task tab in it.

    This class intentionally contains no task planning, model calls, prompt
    construction, credential lookup, or operator-facing response generation.
    """

    def __init__(
        self,
        *,
        client_factory: Callable[[str], CdpClient] = CdpClient,
        bootstrap_factory: Callable[[], BrowserBootstrap] = BrowserBootstrap,
    ) -> None:
        self._client_factory = client_factory
        self._bootstrap_factory = bootstrap_factory
        self._client: CdpClient | Any | None = None
        self._bootstrap: BrowserBootstrap | Any | None = None
        self._session_id: str | None = None
        self._locked_window: str | None = None
        self._page_session: str | None = None
        # The operator's own tabs (the HUD among them) as they were when Omega opened its
        # tab: never captured, switched to, or closed. Every later target is Omega's.
        self._foreign_handles: frozenset[str] = frozenset()

    @property
    def session_id(self) -> str | None:
        return self._session_id if self._client is not None else None

    @property
    def locked_window(self) -> str | None:
        """The DevTools target id of Omega's task tab, if one is currently owned."""
        return self._locked_window

    # ── session ──────────────────────────────────────────────────────────────
    def attach(self, debugger_address: str, *, auto_setup: bool = False) -> dict[str, str]:
        """Connect to Omega's debugger-enabled Edge, starting it when asked and missing."""
        self._locked_window = None
        self._page_session = None
        if auto_setup:
            try:
                self._bootstrap = self._bootstrap_factory()
                self._bootstrap.ensure_debugger(debugger_address=debugger_address)
            except BrowserBootstrapError as exc:
                self._bootstrap = None
                raise BrowserControllerError(f"browser setup failed: {exc}", code=exc.reason,
                                             details=exc.details) from exc
            except (OSError, ValueError) as exc:
                self._bootstrap = None
                raise BrowserControllerError(f"browser setup failed: {exc}") from exc
        client = self._client_factory(debugger_address)
        try:
            browser_id = client.connect()
        except (CdpError, OSError, ValueError) as exc:
            if self._bootstrap is not None:
                self._bootstrap.close()
                self._bootstrap = None
            raise BrowserControllerError(f"browser attach failed: {exc}") from exc
        self._client = client
        self._session_id = f"cdp-{browser_id}"[:200]
        return {"session_id": self._session_id, "debugger_address": debugger_address}

    def detach(self) -> None:
        client, bootstrap = self._client, self._bootstrap
        self._client = None
        self._bootstrap = None
        self._session_id = None
        self._locked_window = None
        self._page_session = None
        self._foreign_handles = frozenset()
        if client is not None:
            client.close()
        if bootstrap is not None:
            bootstrap.close()

    def release(self) -> None:
        """Release the browser at shutdown: close every tab Omega opened, then disconnect.

        The operator's own tabs are never touched; when Omega's tab is the only page left,
        Edge would close entirely, so it is left as it is.
        """
        client = self._client
        if client is not None and self._locked_window is not None:
            try:
                pages = client.pages()
                for page in list(pages):
                    if self.owns_handle(page["id"]) and len(pages) > 1:
                        client.close_target(page["id"])
                        pages = [row for row in pages if row["id"] != page["id"]]
            except (CdpError, OSError, ValueError):
                pass  # the browser may already be gone; detaching below still releases it
        self.detach()

    def ensure_task_tab_alive(self) -> str:
        """Check the connection and Omega's tab before anything reads it.

        Returns ``"live"``; ``"tab_closed"`` when the task tab is gone (the operator closed
        it), after releasing the lock; or ``"session_lost"`` when Edge went away, after
        releasing the dead connection so the caller can attach a fresh one.
        """
        client = self._client
        if client is None or not client.alive():
            self._drop_lost_session()
            return "session_lost"
        if self._locked_window is None:
            return "live"
        try:
            ids = {page["id"] for page in client.pages()}
        except (CdpError, OSError, ValueError):
            self._drop_lost_session()
            return "session_lost"
        if self._locked_window in ids:
            return "live"
        self._locked_window = None
        self._page_session = None
        return "tab_closed"

    def _drop_lost_session(self) -> None:
        client, bootstrap = self._client, self._bootstrap
        self._client = None
        self._bootstrap = None
        self._session_id = None
        self._locked_window = None
        self._page_session = None
        self._foreign_handles = frozenset()
        if client is not None:
            client.close()
        if bootstrap is not None:
            bootstrap.close()

    def _require_client(self) -> Any:
        if self._client is None:
            raise BrowserControllerError("browser session unavailable; attach existing Edge first", code="unattached")
        return self._client

    def _require_page(self) -> tuple[Any, str]:
        client = self._require_client()
        if self._locked_window is None or not self._page_session:
            raise BrowserControllerError("no Omega tab is open yet", code="no_task_tab")
        return client, self._page_session

    def owns_handle(self, handle: str) -> bool:
        """True for tabs Omega opened (its tab and anything spawned from it)."""
        return bool(handle) and handle not in self._foreign_handles

    # ── tab ownership ────────────────────────────────────────────────────────
    def open_task_window(self) -> dict[str, object]:
        """Ensure Omega owns a live task tab; open one in the background only when it does not."""
        client = self._require_client()
        if self._locked_window is not None and self._page_session:
            return {"handle": self._locked_window, "opened": False}
        try:
            existing = {page["id"] for page in client.pages()}
            target = client.create_page("about:blank", background=True)
            self._page_session = client.attach(target)
            self._locked_window = target
            self._foreign_handles = frozenset(existing - {target})
            return {"handle": target, "opened": True}
        except (CdpError, OSError, ValueError) as exc:
            raise BrowserControllerError(f"browser task tab failed: {exc}") from exc

    def navigate_in_new_tab(self, url: str) -> dict[str, str]:
        """Open Omega's own tab (when none is live) and navigate it; a live tab is reused."""
        self.open_task_window()
        return self.navigate(url)

    def tabs(self) -> dict[str, object]:
        """Omega's own tabs only; the operator's tabs are never listed."""
        client = self._require_client()
        try:
            handles = [page["id"] for page in client.pages() if self.owns_handle(page["id"])]
        except (CdpError, OSError, ValueError) as exc:
            raise BrowserControllerError(f"browser tabs failed: {exc}") from exc
        return {"handles": handles, "current": self._locked_window, "locked": self._locked_window}

    def switch_tab(self, handle: str) -> dict[str, str]:
        """Move Omega's lock to another of its own tabs (a link that opened one, say)."""
        client = self._require_client()
        if not self.owns_handle(handle):
            raise BrowserControllerError("browser tab is locked to Omega's own tabs")
        try:
            if handle not in {page["id"] for page in client.pages()}:
                raise BrowserControllerError("browser tab is gone")
            self._page_session = client.attach(handle)
            self._locked_window = handle
            return {"handle": handle, **self._page_info()}
        except (CdpError, OSError, ValueError) as exc:
            raise BrowserControllerError(f"browser tab switch failed: {exc}") from exc

    def close_tab(self) -> dict[str, object]:
        client, _session = self._require_page()
        try:
            closing = self._locked_window
            client.close_target(closing)
            self._locked_window = None
            self._page_session = None
            handles = [page["id"] for page in client.pages() if page["id"] != closing]
            return {"handles": handles, "current": None}
        except (CdpError, OSError, ValueError) as exc:
            raise BrowserControllerError(f"browser tab close failed: {exc}") from exc

    # ── observation ──────────────────────────────────────────────────────────
    def _page_info(self) -> dict[str, str]:
        client, session = self._require_page()
        value = client.evaluate(session, _wrap(_PAGE_INFO_SCRIPT))
        value = value if isinstance(value, dict) else {}
        return {"url": str(value.get("url") or ""), "title": str(value.get("title") or "")}

    def _page_text(self) -> str:
        """Readable page text for perception; a page that refuses the script reads as empty."""
        try:
            client, session = self._require_page()
            return str(client.evaluate(session, _wrap(_PAGE_TEXT_SCRIPT)) or "")[:20000]
        except (BrowserControllerError, CdpError, OSError, ValueError):
            return ""

    def read_snapshot(self) -> dict[str, str]:
        """Observe Omega's tab: address, title, screenshot, readable text; never actuates."""
        client = self._client
        if client is None:
            raise BrowserControllerError("browser session unavailable", code="unattached")
        if self._locked_window is None or not self._page_session:
            raise BrowserControllerError("no Omega tab is open yet", code="no_task_tab")
        try:
            info = self._page_info()
            return {**info, "screenshot": client.screenshot(self._page_session),
                    "session_id": str(self._session_id or ""), "locked_window": self._locked_window,
                    "page_text": self._page_text()}
        except BrowserControllerError:
            raise
        except (CdpError, OSError, ValueError) as exc:
            raise BrowserControllerError("browser read snapshot failed", code="snapshot_failed") from exc

    snapshot = read_snapshot

    def page_state(self) -> dict[str, Any]:
        """What the page shows now: address, title, viewport, readable text, visible controls."""
        client, session = self._require_page()
        try:
            raw = client.evaluate(session, _wrap(PAGE_STATE_SCRIPT))
        except (CdpError, OSError, ValueError) as exc:
            raise BrowserControllerError(f"browser page state failed: {exc}") from exc
        state = bounded_page_state(raw)
        if not state.get("url"):
            try:
                state.update(self._page_info())
            except (BrowserControllerError, CdpError, OSError, ValueError):
                pass
        return state

    def observe(self) -> dict[str, Any]:
        """Screenshot plus page state of Omega's tab, for the agent loop."""
        state = self.page_state()
        client, session = self._require_page()
        try:
            state["screenshot"] = client.screenshot(session)
        except (CdpError, OSError, ValueError) as exc:
            raise BrowserControllerError(f"browser screenshot failed: {exc}") from exc
        state["session_id"] = str(self._session_id or "")
        state["locked_window"] = self._locked_window or ""
        return state

    def wait_ready(self, timeout_s: float = 3.0, *, settle_s: float = 0.4) -> str:
        """Wait for the document to finish loading (bounded), then a short settle."""
        client, session = self._require_page()
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        state = "unknown"
        while True:
            try:
                state = str(client.evaluate(session, _wrap(READY_STATE_SCRIPT), timeout_s=5.0) or "unknown")
            except (CdpError, OSError, ValueError):
                state = "unknown"
            if state == "complete" or time.monotonic() >= deadline:
                break
            time.sleep(0.15)
        if settle_s > 0:
            time.sleep(min(float(settle_s), 3.0))
        return state

    def show_task_banner(self) -> None:
        """Mark Omega's tab as Omega's, visibly; failure-soft, never blocks input."""
        try:
            client, session = self._require_page()
            client.evaluate(session, _wrap(TASK_BANNER_SCRIPT))
        except (BrowserControllerError, CdpError, OSError, ValueError):
            pass

    # ── actions ──────────────────────────────────────────────────────────────
    def navigate(self, url: str) -> dict[str, str]:
        client, session = self._require_page()
        clean = str(url or "").strip()
        if not clean.startswith(("http://", "https://")):
            raise BrowserControllerError("browser URL must be HTTP(S)")
        try:
            client.navigate(session, clean)
            self.wait_ready(8.0, settle_s=0.3)
            return self._page_info()
        except (CdpError, OSError, ValueError) as exc:
            raise BrowserControllerError(f"browser navigation failed: {exc}") from exc

    def back(self) -> dict[str, str]:
        return self._history_step(-1, "back")

    def forward(self) -> dict[str, str]:
        return self._history_step(1, "forward")

    def refresh(self) -> dict[str, str]:
        client, session = self._require_page()
        try:
            client.call("Page.reload", {}, session=session)
            self.wait_ready(8.0, settle_s=0.3)
            return self._page_info()
        except (CdpError, OSError, ValueError) as exc:
            raise BrowserControllerError(f"browser refresh failed: {exc}") from exc

    def _history_step(self, offset: int, name: str) -> dict[str, str]:
        client, session = self._require_page()
        try:
            current, entries = client.history(session)
            index = current + offset
            if not 0 <= index < len(entries):
                raise BrowserControllerError(f"browser {name} failed: no history entry there")
            client.navigate_history(session, int(entries[index]["id"]))
            self.wait_ready(8.0, settle_s=0.3)
            return self._page_info()
        except (CdpError, OSError, ValueError, KeyError) as exc:
            raise BrowserControllerError(f"browser {name} failed: {exc}") from exc

    def show_cursor(self, x: int, y: int, state: str) -> None:
        """Draw the agent pointer at a CSS viewport point. A page that cannot show it fails the gesture."""
        client, session = self._require_page()
        try:
            shown = client.evaluate(session, _wrap(CURSOR_SCRIPT, [int(x), int(y), str(state)]))
        except (CdpError, OSError, ValueError) as exc:
            raise BrowserControllerError(f"browser cursor failed: {exc}") from exc
        if shown is not True:
            raise BrowserControllerError("browser cursor could not be shown")

    def _dispatch_mouse(self, client: Any, session: str, event_type: str, x: int, y: int, *,
                        button: str | None = None, buttons: int = 0, click_count: int = 0) -> None:
        params: dict[str, Any] = {"type": event_type, "x": int(x), "y": int(y), "buttons": int(buttons)}
        if button is not None:
            params["button"] = button
        if click_count:
            params["clickCount"] = int(click_count)
        client.mouse(session, **params)

    def move_mouse(self, x: int, y: int) -> dict[str, int]:
        """Move the real pointer so hover menus and tooltips can appear."""
        client, session = self._require_page()
        try:
            self.show_cursor(int(x), int(y), "hover")
            self._dispatch_mouse(client, session, "mouseMoved", int(x), int(y), buttons=0)
            return {"x": int(x), "y": int(y)}
        except BrowserControllerError:
            raise
        except (CdpError, OSError, ValueError) as exc:
            raise BrowserControllerError(f"browser pointer move failed: {exc}") from exc

    def click_xy(self, x: int, y: int, *, button: str = "left", click_count: int = 1) -> dict[str, Any]:
        client, session = self._require_page()
        if button not in _MOUSE_BUTTONS:
            raise BrowserControllerError("mouse button must be left, right, or middle")
        if type(click_count) is not int or click_count not in (1, 2):
            raise BrowserControllerError("click count must be 1 or 2")
        pressed = _MOUSE_BUTTONS[button]
        try:
            self.show_cursor(int(x), int(y), "click")
            self._dispatch_mouse(client, session, "mouseMoved", int(x), int(y), buttons=0)
            for count in range(1, click_count + 1):
                self._dispatch_mouse(client, session, "mousePressed", int(x), int(y),
                                     button=button, buttons=pressed, click_count=count)
                self._dispatch_mouse(client, session, "mouseReleased", int(x), int(y),
                                     button=button, buttons=0, click_count=count)
            return {"x": int(x), "y": int(y), "button": button, "clicks": click_count}
        except BrowserControllerError:
            raise
        except (CdpError, OSError, ValueError) as exc:
            raise BrowserControllerError(f"browser pointer click failed: {exc}") from exc

    def drag(self, start_x: int, start_y: int, end_x: int, end_y: int, *, duration_ms: int = 300) -> dict[str, int]:
        client, session = self._require_page()
        if type(duration_ms) is not int or duration_ms < 0 or duration_ms > 5_000:
            raise BrowserControllerError("drag duration must be between 0 and 5 seconds")
        try:
            steps = max(1, min(20, math.ceil(duration_ms / 30))) if duration_ms else 1
            self.show_cursor(int(start_x), int(start_y), "drag-start")
            self._dispatch_mouse(client, session, "mouseMoved", int(start_x), int(start_y), buttons=0)
            self._dispatch_mouse(client, session, "mousePressed", int(start_x), int(start_y),
                                 button="left", buttons=1, click_count=1)
            for step in range(1, steps + 1):
                point_x = int(start_x + (end_x - start_x) * step / steps)
                point_y = int(start_y + (end_y - start_y) * step / steps)
                self._dispatch_mouse(client, session, "mouseMoved", point_x, point_y, button="left", buttons=1)
                if duration_ms and step < steps:
                    time.sleep(duration_ms / 1000.0 / steps)
            self.show_cursor(int(end_x), int(end_y), "drag-end")
            self._dispatch_mouse(client, session, "mouseReleased", int(end_x), int(end_y),
                                 button="left", buttons=0, click_count=1)
            return {"start_x": int(start_x), "start_y": int(start_y), "end_x": int(end_x), "end_y": int(end_y)}
        except BrowserControllerError:
            raise
        except (CdpError, OSError, ValueError) as exc:
            raise BrowserControllerError(f"browser pointer drag failed: {exc}") from exc

    def capture_full_page(self) -> dict[str, Any]:
        """Screenshot the rendered document, clamped, so the agent can see past the current fold."""
        client, session = self._require_page()
        try:
            metrics = client.call("Page.getLayoutMetrics", {}, session=session)
            content = {}
            if isinstance(metrics, dict):
                content = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
            if not isinstance(content, dict):
                raise BrowserControllerError("browser layout metrics had no content size")
            width = max(1, math.ceil(float(content.get("width") or 0)))
            raw_height = float(content.get("height") or 0)
            height = min(MAX_FULL_PAGE_HEIGHT, max(1, math.ceil(raw_height)))
            if width <= 1 or raw_height <= 1:
                raise BrowserControllerError("browser page has no rendered size")
            shot = client.call("Page.captureScreenshot", {
                "format": "png", "captureBeyondViewport": True, "fromSurface": True,
                "clip": {"x": 0, "y": 0, "width": width, "height": height, "scale": 1},
            }, session=session)
        except BrowserControllerError:
            raise
        except (CdpError, OSError, ValueError, TypeError) as exc:
            raise BrowserControllerError(f"browser full-page screenshot failed: {exc}") from exc
        data = str(shot.get("data") or "") if isinstance(shot, dict) else ""
        if not data:
            raise BrowserControllerError("browser full-page screenshot returned no image")
        return {"screenshot": data, "width": width, "height": height, "full_page": True,
                "clamped": raw_height > MAX_FULL_PAGE_HEIGHT}

    def scan_viewports(self, screens: int) -> list[dict[str, Any]]:
        """Capture successive viewports, scrolling one viewport height between them."""
        if type(screens) is not int or screens < 1 or screens > 6:
            raise BrowserControllerError("scan screens must be between 1 and 6")
        frames: list[dict[str, Any]] = []
        for index in range(screens):
            observed = self.observe()
            viewport = observed.get("viewport") if isinstance(observed.get("viewport"), dict) else {}
            scroll = observed.get("scroll") if isinstance(observed.get("scroll"), dict) else {}
            frames.append({
                "index": index,
                "screenshot": str(observed.get("screenshot") or ""),
                "scroll_y": int(scroll.get("y") or 0),
                "viewport_height": int(viewport.get("height") or 0),
            })
            if index + 1 < screens:
                self.scroll(0, int(viewport.get("height") or 0))
                self.wait_ready(1.0, settle_s=0.2)
        return frames

    def scroll(self, delta_x: int, delta_y: int, *, x: int = 0, y: int = 0) -> dict[str, int]:
        """Scroll the nearest scrollable area under (x, y), or the page when no point is given.

        A synthetic wheel event never returns on a hidden tab (its compositor is idle; measured
        2026-09-15), so the scroll is done by the page itself.
        """
        client, session = self._require_page()
        try:
            client.evaluate(session, _wrap(_SCROLL_SCRIPT, [int(x), int(y), int(delta_x), int(delta_y)]))
            return {"delta_x": int(delta_x), "delta_y": int(delta_y)}
        except (CdpError, OSError, ValueError) as exc:
            raise BrowserControllerError(f"browser scroll failed: {exc}") from exc

    def type_text(self, text: str) -> dict[str, int]:
        """Type into the focused field; the result never echoes what was typed."""
        client, session = self._require_page()
        if type(text) is not str or not text:
            raise BrowserControllerError("text must be a non-empty string")
        try:
            client.insert_text(session, text)
            return {"typed_chars": len(text)}
        except (CdpError, OSError, ValueError) as exc:
            raise BrowserControllerError(f"browser type failed: {exc}") from exc

    def press_key(self, key: str) -> dict[str, str]:
        """Press one named key or chord ("Enter", "Tab", "Control+a", "Shift+Tab")."""
        client, session = self._require_page()
        parts = [part.strip() for part in str(key or "").split("+") if part.strip()]
        if not parts:
            raise BrowserControllerError("key is required")
        modifiers = 0
        for part in parts[:-1]:
            bit = _MODIFIERS.get(part.lower())
            if bit is None:
                raise BrowserControllerError("chord modifiers must be Control, Shift, Alt or Meta")
            modifiers |= bit
        final = parts[-1]
        descriptor = dict(_NAMED_KEYS.get(final.lower()) or {})
        if not descriptor:
            if len(final) != 1:
                raise BrowserControllerError(f"unknown key {final!r}")
            descriptor = {"key": final, "code": f"Key{final.upper()}" if final.isalpha() else "",
                          "windowsVirtualKeyCode": ord(final.upper()), "text": final}
        if modifiers:
            descriptor.pop("text", None)
            command = _CHORD_COMMANDS.get((modifiers, final.lower()))
            if command:
                descriptor["commands"] = [command]
        try:
            down = {"type": "keyDown" if descriptor.get("text") else "rawKeyDown", "modifiers": modifiers, **descriptor}
            if descriptor.get("text"):
                down["unmodifiedText"] = descriptor["text"]
            client.key(session, **down)
            up = {name: value for name, value in descriptor.items() if name not in {"text", "commands"}}
            client.key(session, type="keyUp", modifiers=modifiers, **up)
            return {"key": key}
        except (CdpError, OSError, ValueError) as exc:
            raise BrowserControllerError(f"browser key press failed: {exc}") from exc

    def extract(self, mode: str, *, selector: str = "", attribute: str = "text") -> dict[str, Any]:
        """Read one structured slice of the page. The page's answer is bounded before it returns."""
        if mode not in EXTRACT_MODES:
            raise BrowserControllerError(f"unknown extract mode {mode}")
        if attribute not in {"text", "href", "src"}:
            raise BrowserControllerError("extract attribute must be text, href, or src")
        raw = self.run_script(EXTRACT_SCRIPT, [str(mode), str(selector)[:200], attribute])
        try:
            return bound_extract(raw)
        except ValueError as exc:
            raise BrowserControllerError(str(exc)) from exc

    def run_script(self, script: str, args: list[Any] | None = None) -> Any:
        """Run page JavaScript (a function body; ``return`` gives the value; ``arguments`` are the args)."""
        client, session = self._require_page()
        if type(script) is not str or not script.strip():
            raise BrowserControllerError("script is required")
        try:
            return client.evaluate(session, _wrap(script, list(args or [])))
        except (CdpError, OSError, ValueError) as exc:
            raise BrowserControllerError(f"browser script failed: {exc}") from exc

    def click(self, selector: str) -> dict[str, str]:
        """Click the centre of the first element matching a CSS selector."""
        rect = self.run_script(
            "const el = document.querySelector(arguments[0]); if (!el) return null;"
            " el.scrollIntoView({block: 'center', inline: 'center'});"
            " const r = el.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2];",
            [str(selector)])
        if not isinstance(rect, list) or len(rect) != 2:
            raise BrowserControllerError(f"browser click failed: no element matches {selector!r}")
        self.click_xy(int(rect[0]), int(rect[1]))
        return {"selector": str(selector)}

    def type_into(self, selector: str, text: str) -> dict[str, str]:
        """Focus the first element matching a CSS selector and type into it."""
        focused = self.run_script(
            "const el = document.querySelector(arguments[0]); if (!el) return false; el.focus(); return true;",
            [str(selector)])
        if focused is not True:
            raise BrowserControllerError(f"browser type failed: no element matches {selector!r}")
        self.type_text(text)
        return {"selector": str(selector)}


__all__ = ["BrowserController", "BrowserControllerError"]

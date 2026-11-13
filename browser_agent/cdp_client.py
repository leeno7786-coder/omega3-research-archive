"""A small synchronous Chrome DevTools Protocol client for Omega's Edge.

Omega drives its own tab over CDP, the way the Omega browser extension does through
chrome.debugger: a tab created in the background stays hidden behind the HUD while
``Page.captureScreenshot``, ``Input.dispatch*`` and ``Runtime.evaluate`` all keep working
(measured 2026-09-15 on Edge 153). WebDriver could not do this: its screenshot activated the
tab and its input actions stalled on a hidden one.

Transport only: no page semantics, no task state. Every failure is a ``CdpError``.
"""
from __future__ import annotations

import json
import threading
from collections import deque
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

_MAX_MESSAGE = 64 * 1024 * 1024
_HTTP_TIMEOUT = 5.0


class CdpError(RuntimeError):
    """Stable DevTools transport or protocol failure."""


def _local_address(address: str) -> str:
    host, _, port = str(address or "").strip().partition(":")
    if host not in {"127.0.0.1", "localhost"} or not port.isdigit():
        raise ValueError("debugger address must be local host:port")
    return f"{host}:{port}"


def _http_json(url: str) -> Any:
    try:
        with urlopen(url, timeout=_HTTP_TIMEOUT) as response:  # noqa: S310 - local-only endpoint
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, ValueError) as exc:
        raise CdpError(f"devtools http failed: {exc}") from exc


class CdpClient:
    """One browser-level DevTools connection with flattened per-target sessions."""

    def __init__(self, debugger_address: str, *, timeout_s: float = 30.0) -> None:
        if type(timeout_s) not in {int, float} or timeout_s <= 0:
            raise ValueError("devtools timeout must be positive")
        self.debugger_address = _local_address(debugger_address)
        self.timeout_s = float(timeout_s)
        self._ws: Any = None
        self._lock = threading.RLock()
        self._seq = 0
        self._events: deque[dict[str, Any]] = deque(maxlen=256)
        self.browser_id = ""

    # ── connection ───────────────────────────────────────────────────────────
    def connect(self) -> str:
        """Open the browser websocket; returns the browser id (stable for this Edge run)."""
        version = _http_json(f"http://{self.debugger_address}/json/version")
        url = str((version or {}).get("webSocketDebuggerUrl") or "")
        if not url.startswith("ws://"):
            raise CdpError("devtools websocket url missing")
        try:
            from websockets.sync.client import connect

            self._ws = connect(url, max_size=_MAX_MESSAGE, open_timeout=self.timeout_s)
        except Exception as exc:  # noqa: BLE001 - one stable failure for every transport cause
            raise CdpError(f"devtools connect failed: {exc}") from exc
        self.browser_id = url.rsplit("/", 1)[-1][:200]
        return self.browser_id

    def alive(self) -> bool:
        if self._ws is None:
            return False
        try:
            _http_json(f"http://{self.debugger_address}/json/version")
        except CdpError:
            return False
        return True

    def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - closing a dead socket is not a failure
                pass

    # ── protocol ─────────────────────────────────────────────────────────────
    def call(self, method: str, params: dict[str, Any] | None = None, *, session: str | None = None,
             timeout_s: float | None = None) -> dict[str, Any]:
        if self._ws is None:
            raise CdpError("devtools not connected")
        with self._lock:
            self._seq += 1
            message: dict[str, Any] = {"id": self._seq, "method": method, "params": dict(params or {})}
            if session:
                message["sessionId"] = session
            try:
                self._ws.send(json.dumps(message))
                deadline = timeout_s or self.timeout_s
                while True:
                    raw = self._ws.recv(timeout=deadline)
                    reply = json.loads(raw)
                    if reply.get("id") == message["id"]:
                        if "error" in reply:
                            error = reply["error"] if isinstance(reply["error"], dict) else {"message": str(reply["error"])}
                            raise CdpError(f"{method}: {error.get('message') or 'devtools error'}")
                        result = reply.get("result")
                        return dict(result) if isinstance(result, dict) else {}
                    if reply.get("method"):
                        self._events.append(reply)
            except CdpError:
                raise
            except Exception as exc:  # noqa: BLE001 - websocket/timeout/json failures share one cause
                raise CdpError(f"{method}: {type(exc).__name__}: {exc}") from exc

    def drain_events(self) -> Iterator[dict[str, Any]]:
        while self._events:
            yield self._events.popleft()

    # ── targets ──────────────────────────────────────────────────────────────
    def pages(self) -> list[dict[str, str]]:
        rows = _http_json(f"http://{self.debugger_address}/json/list")
        pages = []
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("type") == "page" and row.get("id"):
                pages.append({"id": str(row["id"]), "url": str(row.get("url") or ""),
                              "title": str(row.get("title") or "")})
        return pages

    def create_page(self, url: str = "about:blank", *, background: bool = True) -> str:
        result = self.call("Target.createTarget", {"url": url, "background": bool(background), "newWindow": False})
        target = str(result.get("targetId") or "")
        if not target:
            raise CdpError("Target.createTarget returned no target")
        return target

    def attach(self, target_id: str) -> str:
        result = self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        session = str(result.get("sessionId") or "")
        if not session:
            raise CdpError("Target.attachToTarget returned no session")
        self.call("Page.enable", session=session)
        return session

    def close_target(self, target_id: str) -> None:
        """Close one of Omega's tabs.

        The HTTP endpoint is used on purpose: ``Target.closeTarget`` answers success for a
        target this connection holds a flattened session on and then leaves the tab open
        (measured 2026-09-15 on Edge 153); ``/json/close`` closes it.
        """
        try:
            with urlopen(f"http://{self.debugger_address}/json/close/{target_id}", timeout=_HTTP_TIMEOUT) as response:  # noqa: S310
                response.read()
        except (HTTPError, URLError, OSError) as exc:
            raise CdpError(f"close target failed: {exc}") from exc

    # ── page ─────────────────────────────────────────────────────────────────
    def evaluate(self, session: str, expression: str, *, timeout_s: float | None = None) -> Any:
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                                "awaitPromise": True}, session=session, timeout_s=timeout_s)
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            text = details.get("text") if isinstance(details, dict) else ""
            exception = (details.get("exception") or {}) if isinstance(details, dict) else {}
            raise CdpError(f"script threw: {exception.get('description') or text or 'error'}"[:300])
        value = result.get("result")
        return value.get("value") if isinstance(value, dict) else None

    def screenshot(self, session: str) -> str:
        result = self.call("Page.captureScreenshot", {"format": "png", "fromSurface": True}, session=session)
        data = str(result.get("data") or "")
        if not data:
            raise CdpError("Page.captureScreenshot returned no image")
        return data

    def navigate(self, session: str, url: str) -> None:
        result = self.call("Page.navigate", {"url": url}, session=session)
        if result.get("errorText"):
            raise CdpError(f"navigation failed: {result['errorText']}")

    def mouse(self, session: str, **params: Any) -> None:
        self.call("Input.dispatchMouseEvent", params, session=session)

    def key(self, session: str, **params: Any) -> None:
        self.call("Input.dispatchKeyEvent", params, session=session)

    def insert_text(self, session: str, text: str) -> None:
        self.call("Input.insertText", {"text": text}, session=session)

    def history(self, session: str) -> tuple[int, list[dict[str, Any]]]:
        result = self.call("Page.getNavigationHistory", session=session)
        entries = [entry for entry in result.get("entries") or [] if isinstance(entry, dict)]
        return int(result.get("currentIndex") or 0), entries

    def navigate_history(self, session: str, entry_id: int) -> None:
        self.call("Page.navigateToHistoryEntry", {"entryId": int(entry_id)}, session=session)


__all__ = ["CdpClient", "CdpError"]

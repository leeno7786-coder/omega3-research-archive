"""
HUD events bridge — guardian.terminal_feedback events -> SSE stream.

The terminal_feedback module fires `_publish_event(dict)` for every
phase / signal / memory emit. We subscribe a callback that appends to
a thread-safe ring buffer; the SSE endpoint streams new entries as
they arrive.

Architecture choice — ring buffer + sequence numbers:
    - Each event gets a monotonic sequence id at append time
    - SSE clients track their last-seen seq and request from there
    - Buffer is bounded (default 1000 events) to cap memory
    - Multiple SSE clients can subscribe simultaneously without
      blocking each other (each tracks its own position)
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import deque
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional

log = logging.getLogger("omega.hud.events")


# Bounded ring buffer (~1k events ≈ several minutes of activity).
_BUFFER_MAX = 1000
_BUFFER: deque = deque(maxlen=_BUFFER_MAX)
_BUFFER_LOCK = threading.Lock()
_SEQ = 0
_INSTALLED = False
_INSTALL_LOCK = threading.Lock()


def _publish(event: Dict[str, Any]) -> None:
    """Subscriber callback installed on guardian.terminal_feedback.
    Appends the event with a monotonic seq id for SSE consumers."""
    global _SEQ
    with _BUFFER_LOCK:
        _SEQ += 1
        seq = _SEQ
    enriched = dict(event)
    enriched["_seq"] = seq
    _BUFFER.append(enriched)


def install() -> None:
    """Install the events subscriber on terminal_feedback. Idempotent."""
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        _INSTALLED = True
    try:
        from guardian.terminal_feedback import subscribe

        subscribe(_publish)
        log.info("hud.events: installed subscriber on terminal_feedback")
    except Exception as exc:
        log.warning("hud.events: install failed (%s)", exc)


def events_after(seq: int, limit: int = 200) -> list:
    """Return events with seq > `seq`, oldest first, capped at limit.
    Used by the SSE endpoint to catch up a client."""
    out = []
    with _BUFFER_LOCK:
        for ev in _BUFFER:
            if ev.get("_seq", 0) > seq:
                out.append(ev)
                if len(out) >= limit:
                    break
    return out


async def sse_stream(
    start_seq: int = 0,
    is_disconnected: Optional[Callable[[], Awaitable[bool]]] = None,
) -> AsyncIterator[str]:
    """SSE generator — yields formatted SSE messages as new events
    arrive. Sends a heartbeat comment every 15s so proxies don't
    drop the connection.

    `is_disconnected` is an optional awaitable predicate (the HUD passes
    Starlette's Request.is_disconnected). When it reports the client has
    gone, the generator returns promptly. Without it, a closed EventSource
    leaves this coroutine waking every 0.4s for the life of the process —
    one leaked task per dropped connection.
    """
    # Replay any events the client missed before subscribing
    last_seq = start_seq
    for ev in events_after(last_seq):
        last_seq = ev.get("_seq", last_seq)
        yield _format_sse(ev)

    last_heartbeat = asyncio.get_event_loop().time()

    while True:
        if is_disconnected is not None and await is_disconnected():
            return
        await asyncio.sleep(0.4)
        # Drain any new events
        new = events_after(last_seq)
        for ev in new:
            last_seq = ev.get("_seq", last_seq)
            yield _format_sse(ev)

        # Heartbeat (SSE comment) keeps proxies + browsers happy
        now = asyncio.get_event_loop().time()
        if now - last_heartbeat >= 15.0:
            yield ": heartbeat\n\n"
            last_heartbeat = now


def _format_sse(event: Dict[str, Any]) -> str:
    """SSE message format: `id:`, `event:`, `data:` lines + blank line."""
    seq = event.get("_seq", 0)
    kind = event.get("kind", "event")
    payload = json.dumps(event, default=str, ensure_ascii=False)
    return f"id: {seq}\nevent: {kind}\ndata: {payload}\n\n"


# Diagnostic — used by tests to reset between runs.
def _reset_for_tests() -> None:
    global _SEQ, _INSTALLED
    with _BUFFER_LOCK:
        _BUFFER.clear()
        _SEQ = 0
    with _INSTALL_LOCK:
        _INSTALLED = False

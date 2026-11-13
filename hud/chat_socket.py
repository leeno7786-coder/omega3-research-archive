"""Push delivery for the operator chat WebSocket.

AGENTS.md §4.0: **transport is not ignition.** This module carries a finished
reply from the response rail out to whichever socket is waiting on that turn. It
never touches pressure, never opens an episode, and owns no state that cognition
reads back.

Why it exists: the chat WebSocket at ``ws://localhost:13317/omega/bridge``
(hud/ui-src/core/boot.js:452) was specified and never built, so chat rode
``POST /api/chat`` and the reply was POLLED from ``/api/chat/responses`` on
``cycle_id``. Speech could not begin until the whole reply was assembled AND a
poll happened to catch it — the polling seam, not the models, was the TTS
latency.

Threading: ``append_response`` runs on the cascade thread; the sockets live on
the HUD's event loop. Subscribers are therefore called SYNCHRONOUSLY on the
caller's thread and are expected to hand off (``loop.call_soon_threadsafe``)
rather than block. A subscriber that raises is dropped from the fan-out and
never propagates: a closed browser tab must not stop the mind from recording its
own answer (§2).
"""

from __future__ import annotations

import itertools
import logging
import threading
from typing import Any, Callable, Dict, List, Tuple

log = logging.getLogger("omega.hud.chat_socket")

Subscriber = Callable[[Dict[str, Any]], None]

_LOCK = threading.RLock()
_SUBSCRIBERS: List[Tuple[int, Subscriber]] = []
_NEXT_TOKEN = 1
_EVENT_SEQUENCE = itertools.count(1)


class JobFollowing:
    """Socket-local display authority learned only from owned server events.

    Reconnect starts empty; missed finals use the existing durable orphan rail.
    Bounded job history survives an origin final, not the socket's lifetime.
    """

    def __init__(self, max_jobs: int = 128):
        self._cycles: set[str] = set()
        self._jobs: dict[str, None] = {}
        self._replied_cycles: set[str] = set()
        self._terminal_lane_cycles: set[str] = set()
        self._max_jobs = max_jobs
        self._lock = threading.RLock()

    def add(self, cycle_id: str) -> None:
        with self._lock:
            self._cycles.add(cycle_id)

    def discard(self, cycle_id: str) -> None:
        with self._lock:
            self._cycles.discard(cycle_id)
            self._replied_cycles.discard(cycle_id)
            self._terminal_lane_cycles.discard(cycle_id)

    def route(self, payload: Dict[str, Any]) -> Dict[str, Any] | None:
        with self._lock:
            cycle = str(payload.get("cycle_id") or "")
            job = str(payload.get("job_id") or "")
            if cycle in self._cycles:
                if job:
                    self._jobs[job] = None
                    while len(self._jobs) > self._max_jobs:
                        self._jobs.pop(next(iter(self._jobs)))
                kind = str(payload.get("kind") or "")
                if kind == "chat.reply":
                    lane_work = payload.get("lane_work")
                    if not isinstance(lane_work, dict):
                        self.discard(cycle)
                    else:
                        self._replied_cycles.add(cycle)
                        if (str(lane_work.get("status") or "")
                                in {"needs_input", "completed", "failed"}
                                or cycle in self._terminal_lane_cycles):
                            self.discard(cycle)
                elif kind == "chat.update" and str(payload.get("status") or "") in {
                    "needs_input", "completed", "failed",
                }:
                    self._terminal_lane_cycles.add(cycle)
                    if cycle in self._replied_cycles:
                        self.discard(cycle)
                return payload
            if cycle and job and job in self._jobs:
                return {**payload, "job_continuation": True}
            return None


def subscribe(fn: Subscriber) -> int:
    """Register a reply sink. Returns the token used to unsubscribe."""
    global _NEXT_TOKEN
    with _LOCK:
        token = _NEXT_TOKEN
        _NEXT_TOKEN += 1
        _SUBSCRIBERS.append((token, fn))
        return token


def unsubscribe(token: int) -> bool:
    with _LOCK:
        for i, (tok, _fn) in enumerate(_SUBSCRIBERS):
            if tok == token:
                del _SUBSCRIBERS[i]
                return True
    return False


def subscriber_count() -> int:
    with _LOCK:
        return len(_SUBSCRIBERS)


_HUD_DISPLAY_KEYS = (
    "plugin_apps",
    "effect_summary",
    "ordered_operations",
    "audience",
    "destination",
    "crosses_providers",
    "field_names",
    "risks",
    "argument_previews",
)
_HUD_PLUGIN_APP_KEYS = ("plugin_id", "plugin_name", "app_id", "app_name")


def _copy_hud_plugin_apps(value: Any) -> list[dict[str, str]] | None:
    if type(value) is not list:
        return None
    copied: list[dict[str, str]] = []
    for item in value:
        if type(item) is not dict:
            return None
        app: dict[str, str] = {}
        for key in _HUD_PLUGIN_APP_KEYS:
            field = item.get(key)
            if type(field) is not str or not field:
                return None
            app[key] = field
        copied.append(app)
    return copied


def plugin_permission_hud_payload(record: Dict[str, Any]) -> Dict[str, Any] | None:
    """Copy the existing HUD dict onto a reply. Never invent a second payload.

    Extra keys are stripped, not rejected, so a stuffed secret cannot ride
    persist/publish/poll. Nested display fields are whitelist-copied.
    """
    value = record.get("plugin_permission_request")
    if type(value) is not dict:
        return None
    request_id = value.get("request_id")
    revision = value.get("revision")
    display_in = value.get("display")
    if type(request_id) is not str or not request_id:
        return None
    if type(revision) is not str or not revision:
        return None
    if type(display_in) is not dict:
        return None
    display: Dict[str, Any] = {}
    for key in _HUD_DISPLAY_KEYS:
        item = display_in.get(key)
        if key == "plugin_apps":
            copied = _copy_hud_plugin_apps(item)
            if copied is None:
                return None
            display[key] = copied
        elif key == "crosses_providers":
            if type(item) is not bool:
                return None
            display[key] = item
        elif key in ("ordered_operations", "field_names", "risks", "argument_previews"):
            if key == "argument_previews" and item is None:
                continue
            if type(item) is not list or any(type(entry) is not str for entry in item):
                return None
            display[key] = list(item)
        else:
            if type(item) is not str:
                return None
            display[key] = item
    return {
        "request_id": request_id,
        "revision": revision,
        "display": display,
    }


def publish_delta(
    *, cycle_id: str, episode_id: str, text: str, seq: int, attempt: int = 0,
    pipeline_id: int | None = None, job_id: str | None = None, work_kind: str = "",
) -> int:
    """Push ONE partial of a reply that is still being generated (RISK 5).

    Same fan-out, same sockets, same cycle_id the finished ``chat.reply`` will
    arrive on — so the client that is already filtering on its own turn needs no
    second subscription and no second transport. The operator hears the answer
    while the engine is still producing it instead of after it has finished.

    NOT storage, and deliberately so: nothing here is persisted, nothing is
    marked delivered, and a reply that streamed to nobody is still recorded in
    full by ``append_response``. The durable rail remains the source of truth —
    a client that reconnects mid-reply fetches the whole thing, which is exactly
    why a dropped delta is allowed to cost nothing.

    ``seq`` counts from 0 within one ATTEMPT. The response stage may generate a
    reply twice (a think-on pass that produced only thinking is retried
    think-off), so a renderer resets its buffer whenever it sees ``seq == 0``:
    the second attempt REPLACES the first, it does not continue it.
    """
    cid = str(cycle_id or "").strip()
    body = str(text or "")
    if not cid or not body:
        return 0
    payload: Dict[str, Any] = {
        "kind": "chat.reply_delta",
        "cycle_id": cid,
        "episode_id": str(episode_id or ""),
        "text": body,
        "seq": int(seq),
        "attempt": int(attempt),
    }
    _attach_correlation(payload, pipeline_id=pipeline_id, job_id=job_id, work_kind=work_kind)
    return _fan_out(payload)


def _attach_correlation(
    payload: Dict[str, Any], *, pipeline_id: int | None, job_id: str | None, work_kind: str
) -> None:
    if pipeline_id is not None:
        from core.pipelines import validate_pipeline_id

        payload["pipeline_id"] = validate_pipeline_id(pipeline_id)
    if job_id:
        payload["job_id"] = str(job_id)
    if work_kind:
        payload["work_kind"] = str(work_kind)


def publish_chat_progress(
    cycle_id: str,
    kind: str,
    *,
    pipeline_id: int,
    job_id: str | None = None,
    **fields: object,
) -> dict[str, object]:
    """Publish one physically-owned progress frame and return its envelope."""
    cid = str(cycle_id or "").strip()
    name = str(kind or "").strip()
    if not cid or not name:
        return {}
    payload: Dict[str, Any] = {"kind": name, "cycle_id": cid}
    payload.update(fields)
    _attach_correlation(
        payload,
        pipeline_id=pipeline_id,
        job_id=job_id,
        work_kind=str(fields.get("work_kind") or ""),
    )
    _fan_out(payload)
    return dict(payload)


def publish_event(*, kind: str, cycle_id: str, episode_id: str = "", **fields: Any) -> int:
    """Push ONE live frame of the work behind a reply — a tool call the brain
    is making (``chat.tool_call``), what it got back (``chat.tool_result``), or a
    a redacted reasoning-progress marker (``chat.reasoning_delta``) — to every socket
    waiting on that turn (operator 2026-09-05: "the tool calls and reasoning
    prose etc, diff files, markdown, all that appends to the chat pane").

    Display only, exactly like ``publish_delta``: nothing here is persisted or
    marked delivered. The finished ``chat.reply`` carries the tool ledger in its
    ``thinking`` payload, so a client that missed these frames still sees what
    happened once the answer lands.
    """
    cid = str(cycle_id or "").strip()
    name = str(kind or "").strip()
    if not cid or not name:
        return 0
    pipeline_id = fields.pop("pipeline_id", None)
    if pipeline_id is None:
        return 0
    if name == "chat.reasoning_delta":
        fields.pop("text", None)
        fields["reasoning_state"] = "in_progress"
    payload = publish_chat_progress(
        cid,
        name,
        pipeline_id=pipeline_id,
        job_id=fields.pop("job_id", None),
        episode_id=str(episode_id or ""),
        **fields,
    )
    return int(bool(payload))


def publish_reply(record: Dict[str, Any]) -> int:
    """Fan a finished reply out to every live socket. Never raises.

    Returns the number of subscribers that accepted it. Called from
    ``pressure_loop.operator_chat_responses.append_response`` on the cascade
    thread, so it must be cheap and must not block.
    """
    if not isinstance(record, dict):
        return 0
    cycle_id = str(record.get("cycle_id") or "").strip()
    if not cycle_id:
        return 0
    payload: Dict[str, Any] = {
        "kind": "chat.reply",
        "cycle_id": cycle_id,
        "episode_id": str(record.get("episode_id") or ""),
        "text": str(record.get("text") or ""),
        "ts": str(record.get("ts") or ""),
    }
    _attach_correlation(
        payload,
        pipeline_id=record.get("pipeline_id"),
        job_id=record.get("job_id"),
        work_kind=str(record.get("work_kind") or ""),
    )
    for key in (
        "delivery", "route", "prompt_pack", "thinking", "image", "document",
        "workspace_assignment", "operator_question", "lane_work",
    ):
        value = record.get(key)
        if isinstance(value, dict) and value:
            payload[key] = dict(value)
    permission = plugin_permission_hud_payload(record)
    if permission is not None:
        payload["plugin_permission_request"] = permission

    return _fan_out(payload)


def publish_lane_update(record: Dict[str, Any]) -> int:
    """Push a durable Browser or Coding Agent update into its original chat turn."""
    if not isinstance(record, dict):
        return 0
    cycle_id = str(record.get("cycle_id") or "").strip()
    update_id = str(record.get("update_id") or "").strip()
    lane = str(record.get("lane") or "")
    status = str(record.get("status") or "")
    text = str(record.get("text") or "")
    if not cycle_id or not update_id or lane not in {"browser", "agent"} or not text:
        return 0
    payload: Dict[str, Any] = {
        "kind": "chat.update",
        "cycle_id": cycle_id,
        "update_id": update_id,
        "seq": int(record.get("seq") or 0),
        "episode_id": str(record.get("episode_id") or ""),
        "lane": lane,
        "status": status,
        "text": text,
    }
    question = record.get("question")
    if isinstance(question, dict) and question:
        payload["question"] = dict(question)
    return _fan_out(payload)


def _fan_out(payload: Dict[str, Any]) -> int:
    """One frame to every live socket. Never raises; returns how many took it."""
    with _LOCK:
        payload["event_seq"] = next(_EVENT_SEQUENCE)
        delivered = 0
        dead: List[int] = []
        for token, fn in list(_SUBSCRIBERS):
            try:
                fn(dict(payload))
                delivered += 1
            except Exception:
                # A dead socket is not cognition's problem. Drop it and carry on.
                dead.append(token)
                log.debug("chat socket subscriber failed; dropping it", exc_info=True)
        for token in dead:
            unsubscribe(token)
    return delivered


def publish_control(payload: Dict[str, Any], subscriber: Subscriber) -> Dict[str, Any]:
    """Sequence and hand off one socket-local control frame atomically."""
    with _LOCK:
        stamped = dict(payload)
        stamped["event_seq"] = next(_EVENT_SEQUENCE)
        subscriber(dict(stamped))
        return stamped


def _reset_for_tests() -> None:
    global _EVENT_SEQUENCE
    with _LOCK:
        _SUBSCRIBERS.clear()
        _EVENT_SEQUENCE = itertools.count(1)

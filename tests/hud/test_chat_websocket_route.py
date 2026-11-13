from __future__ import annotations

import asyncio
from types import SimpleNamespace

from starlette.websockets import WebSocketDisconnect

import hud.server as hud_server
from hud import chat_socket


class _Lifecycle:
    plugin_registry = None

    def close(self) -> None:
        pass


class _Socket:
    def __init__(self, message: dict[str, object], *, headers: dict[str, str] | None = None) -> None:
        self._message = message
        self._reads = 0
        self._finished = asyncio.Event()
        self.frames: list[dict[str, object]] = []
        self.headers = headers or {}
        self.url = SimpleNamespace(scheme="ws")
        self.closed_code: int | None = None

    async def accept(self) -> None:
        pass

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code
        self._finished.set()

    async def receive_json(self) -> dict[str, object]:
        self._reads += 1
        if self._reads == 1:
            return self._message
        await self._finished.wait()
        raise WebSocketDisconnect()

    async def send_json(self, payload: dict[str, object]) -> None:
        self.frames.append(dict(payload))
        if payload.get("kind") in {"chat.reply", "chat.rejected"}:
            self._finished.set()


def _chat_endpoint(monkeypatch):
    monkeypatch.setattr(
        hud_server,
        "get_default_plugin_lifecycle_service",
        lambda: _Lifecycle(),
    )
    app = hud_server.create_app()
    return next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/omega/chat"
    )


def test_websocket_chat_reaches_the_existing_queue_and_emits_only_the_real_reply(
    monkeypatch,
) -> None:
    """The HUD socket is transport for one queued turn, not an ACK lane."""
    endpoint = _chat_endpoint(monkeypatch)
    chat_socket._reset_for_tests()
    handled: list[dict[str, object]] = []

    def handle_operator_chat(**kwargs: object) -> dict[str, object]:
        handled.append(dict(kwargs))
        cycle_id = str(kwargs["cycle_id"])
        chat_socket.publish_reply(
            {
                "cycle_id": cycle_id,
                "episode_id": "episode-chat",
                "text": "The real AUTO result.",
                "pipeline_id": 2,
                "job_id": "job-chat",
                "work_kind": "operator_turn",
                "delivery": {"text": "The real AUTO result.", "rendered_by": "omega_async"},
            }
        )
        return {
            "ok": True,
            "queued": True,
            "cycle_id": cycle_id,
            "delivery": {"text": "", "rendered_by": "queued"},
        }

    monkeypatch.setattr(
        "pressure_loop.operator_chat.handle_operator_chat",
        handle_operator_chat,
    )
    websocket = _Socket(
        {
            "kind": "chat.user_message",
            "text": "Run the tool",
            "uploads": [
                {
                    "kind": "image",
                    "upload_ref": "image/scene.jpg",
                }
            ],
        }
    )

    asyncio.run(asyncio.wait_for(endpoint(websocket), timeout=5.0))

    assert len(handled) == 1
    assert handled[0]["text"] == "Run the tool"
    assert handled[0]["cycle_id"]
    assert handled[0]["upload_refs"] == ["image/scene.jpg"]
    assert websocket.frames == [
        {
            "kind": "chat.reply",
            "event_seq": websocket.frames[0]["event_seq"],
            "cycle_id": handled[0]["cycle_id"],
            "episode_id": "episode-chat",
            "text": "The real AUTO result.",
            "ts": "",
            "pipeline_id": 2,
            "job_id": "job-chat",
            "work_kind": "operator_turn",
            "delivery": {"text": "The real AUTO result.", "rendered_by": "omega_async"},
        }
    ]


def test_websocket_chat_failure_rejects_the_waiting_turn_instead_of_closing_silently(
    monkeypatch,
) -> None:
    endpoint = _chat_endpoint(monkeypatch)
    chat_socket._reset_for_tests()

    def fail_operator_chat(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("admission failed")

    monkeypatch.setattr(
        "pressure_loop.operator_chat.handle_operator_chat",
        fail_operator_chat,
    )
    websocket = _Socket({"kind": "chat.user_message", "text": "hello"})

    asyncio.run(asyncio.wait_for(endpoint(websocket), timeout=5.0))

    assert websocket.frames == [
        {
            "kind": "chat.rejected",
            "event_seq": websocket.frames[0]["event_seq"],
            "cycle_id": websocket.frames[0]["cycle_id"],
            "error": "admission failed",
        }
    ]
    assert websocket.frames[0]["cycle_id"]


def test_socket_continuation_stays_in_origin_conversation_without_cross_job_adoption(monkeypatch):
    endpoint = _chat_endpoint(monkeypatch)
    chat_socket._reset_for_tests()
    class FollowingSocket(_Socket):
        async def send_json(self, payload):
            self.frames.append(dict(payload))
            if payload.get("cycle_id") == "continuation" and payload.get("kind") == "chat.reply":
                self._finished.set()

    def handle(**kwargs):
        chat_socket.publish_reply({"cycle_id": kwargs["cycle_id"], "job_id": "owned-job", "text": "first", "pipeline_id": 0})
        chat_socket.publish_chat_progress("unrelated", "chat.tool_call", job_id="other-job", pipeline_id=1)
        chat_socket.publish_chat_progress("continuation", "chat.tool_call", job_id="owned-job", pipeline_id=2)
        chat_socket.publish_reply({"cycle_id": "continuation", "job_id": "owned-job", "text": "continued", "pipeline_id": 2})
        return {"ok": True, "cycle_id": kwargs["cycle_id"]}
    monkeypatch.setattr("pressure_loop.operator_chat.handle_operator_chat", handle)
    socket = FollowingSocket({"text": "build", "job_id": "other-job"})
    asyncio.run(asyncio.wait_for(endpoint(socket), timeout=5))
    assert [row["kind"] for row in socket.frames] == ["chat.reply", "chat.tool_call", "chat.reply"]
    assert all(row["cycle_id"] == "continuation" and row["job_continuation"] for row in socket.frames[1:])
    assert all(row["job_id"] == "owned-job" for row in socket.frames)


def test_cross_origin_websocket_is_rejected_before_accept(monkeypatch):
    endpoint = _chat_endpoint(monkeypatch)
    chat_socket._reset_for_tests()
    socket = _Socket(
        {"kind": "chat.user_message", "text": "do something"},
        headers={"origin": "https://evil.example"},
    )

    asyncio.run(asyncio.wait_for(endpoint(socket), timeout=5.0))

    assert socket.closed_code == 1008
    assert socket.frames == []


class _AckingSocket(_Socket):
    """Sends one turn, then (optionally) acknowledges the reply the page rendered."""

    def __init__(self, message: dict[str, object], *, ack: str | None) -> None:
        super().__init__(message)
        self._ack = ack
        self._replied = asyncio.Event()

    async def receive_json(self) -> dict[str, object]:
        self._reads += 1
        if self._reads == 1:
            return self._message
        if self._reads == 2 and self._ack is not None:
            await self._replied.wait()
            cycle_id = next(
                frame["cycle_id"] for frame in self.frames if frame.get("kind") == "chat.reply"
            )
            return {"kind": "chat.ack", "cycle_id": self._ack or cycle_id}
        self._finished.set()
        raise WebSocketDisconnect()

    async def send_json(self, payload: dict[str, object]) -> None:
        self.frames.append(dict(payload))
        if payload.get("kind") == "chat.reply":
            self._replied.set()
            if self._ack is None:
                self._finished.set()


def _run_acking_turn(monkeypatch, *, ack: str | None) -> tuple[_AckingSocket, list[str]]:
    endpoint = _chat_endpoint(monkeypatch)
    chat_socket._reset_for_tests()
    delivered: list[str] = []
    monkeypatch.setattr(
        "pressure_loop.operator_chat_responses.mark_delivered",
        lambda cycle_id: delivered.append(cycle_id) or True,
    )

    def handle_operator_chat(**kwargs: object) -> dict[str, object]:
        chat_socket.publish_reply({"cycle_id": str(kwargs["cycle_id"]), "text": "Rendered answer."})
        return {"ok": True, "queued": True, "cycle_id": kwargs["cycle_id"]}

    monkeypatch.setattr("pressure_loop.operator_chat.handle_operator_chat", handle_operator_chat)
    websocket = _AckingSocket({"kind": "chat.user_message", "text": "hello"}, ack=ack)
    asyncio.run(asyncio.wait_for(endpoint(websocket), timeout=5.0))
    return websocket, delivered


def test_a_sent_reply_stays_pending_until_the_page_acknowledges_it(monkeypatch) -> None:
    """A frame written to the socket may never render (the page closed or failed). Only the
    page's acknowledgement of that exact cycle marks it delivered; until then the reply stays
    on the orphan sweep so a reload recovers it."""
    websocket, delivered = _run_acking_turn(monkeypatch, ack=None)
    assert any(frame.get("kind") == "chat.reply" for frame in websocket.frames)
    assert delivered == []


def test_the_page_acknowledgement_marks_exactly_its_reply_delivered(monkeypatch) -> None:
    websocket, delivered = _run_acking_turn(monkeypatch, ack="")
    cycle_id = next(frame["cycle_id"] for frame in websocket.frames if frame.get("kind") == "chat.reply")
    assert delivered == [cycle_id]


def test_an_acknowledgement_for_a_reply_this_socket_never_sent_is_ignored(monkeypatch) -> None:
    _websocket, delivered = _run_acking_turn(monkeypatch, ack="cycle-from-another-page")
    assert delivered == []

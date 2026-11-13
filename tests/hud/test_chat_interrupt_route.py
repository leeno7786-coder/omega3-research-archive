"""POST /api/chat/interrupt and the chat.interrupt socket frame (barge-in)."""

from __future__ import annotations

import asyncio
import inspect

import pytest
from fastapi import HTTPException

from hud.server import create_app
from pressure_loop import operator_chat_responses


def _route(app, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


@pytest.fixture(autouse=True)
def _clean():
    operator_chat_responses._reset_cache_for_tests()
    yield
    operator_chat_responses._reset_cache_for_tests()


def test_interrupt_route_requests_the_stop_for_that_cycle():
    endpoint = _route(create_app(), "/api/chat/interrupt", "POST")
    response = asyncio.run(endpoint(payload={"cycle_id": " cyc-42 "}))
    assert response.status_code == 200
    assert b'"cycle_id":"cyc-42"' in response.body.replace(b" ", b"")
    assert operator_chat_responses.interrupt_requested("cyc-42") is True
    assert operator_chat_responses.interrupt_requested("other") is False


def test_barge_in_route_stops_the_speech_and_leaves_the_work_running():
    """Voice barge-in is delivery-only, and it needs a route of its own.

    `sendInterrupt` in the HUD falls back to HTTP when the chat socket is down, and
    `/api/chat/interrupt` means "stop the work" by operator ruling (2026-09-15: Esc and the stop
    control land there). So the same gesture — speaking over Omega — either muted delivery or
    killed the agent jobs behind the turn, depending on whether a websocket happened to be open.
    One gesture, one consequence: barge-in raises the speech verb only.
    """
    endpoint = _route(create_app(), "/api/chat/barge-in", "POST")
    response = asyncio.run(endpoint(payload={"cycle_id": " cyc-7 "}))
    assert response.status_code == 200
    assert b'"cycle_id":"cyc-7"' in response.body.replace(b" ", b"")
    assert operator_chat_responses.interrupt_requested("cyc-7") is True
    assert operator_chat_responses.work_halt_requested("cyc-7") is False, (
        "a barge-in must never halt the work behind the turn"
    )


def test_barge_in_route_requires_a_cycle_id():
    endpoint = _route(create_app(), "/api/chat/barge-in", "POST")
    with pytest.raises(HTTPException) as info:
        asyncio.run(endpoint(payload={}))
    assert info.value.status_code == 400


def test_the_stop_route_still_halts_the_work():
    """The ruling this order does NOT change: Esc and the stop control stop the work."""
    endpoint = _route(create_app(), "/api/chat/interrupt", "POST")
    asyncio.run(endpoint(payload={"cycle_id": "cyc-stop-me"}))
    assert operator_chat_responses.work_halt_requested("cyc-stop-me") is True
    assert operator_chat_responses.interrupt_requested("cyc-stop-me") is True


def test_interrupt_route_requires_a_cycle_id():
    endpoint = _route(create_app(), "/api/chat/interrupt", "POST")
    with pytest.raises(HTTPException) as info:
        asyncio.run(endpoint(payload={}))
    assert info.value.status_code == 400


def test_socket_accepts_the_interrupt_frame_without_treating_it_as_a_turn():
    import hud.server as server

    source = inspect.getsource(server.create_app)
    idx = source.index('if kind == "chat.interrupt":')
    turn_idx = source.index('if kind != "chat.user_message":')
    assert idx < turn_idx, "the interrupt frame must be handled before the turn gate"
    assert "request_interrupt(interrupt_cycle)" in source
    assert '"kind": "chat.interrupted"' in source


def test_playback_interrupt_stops_deltas_but_retains_genuine_cognition_final(monkeypatch):
    deltas: list[str] = []
    monkeypatch.setattr("hud.chat_socket.publish_delta", lambda **kw: deltas.append(kw["text"]))
    monkeypatch.setattr("hud.chat_socket.publish_reply", lambda _record: 0)
    monkeypatch.setattr(operator_chat_responses, "_persist", lambda: None)
    monkeypatch.setattr(operator_chat_responses, "remember_assistant_turn", lambda *_args: None)

    sink = operator_chat_responses.reply_token_sink("cyc-42", "ep-1")
    sink("before")
    assert operator_chat_responses.request_interrupt("cyc-42") is True
    sink("after")

    record = operator_chat_responses.append_response(
        cycle_id="cyc-42",
        episode_id="ep-1",
        text="the generation eventually completed",
        delivery={"rendered_by": "omega_async"},
    )
    assert deltas == ["before"]
    assert record is not None
    assert record["text"] == "the generation eventually completed"
    assert record["delivery"] == {"rendered_by": "omega_async"}
    assert operator_chat_responses.interrupt_requested("cyc-42") is False

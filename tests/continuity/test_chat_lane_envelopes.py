"""Chat's durable answer and each transport must carry the same owned result."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

from hud import chat_socket
from pressure_loop import operator_chat_responses as responses


@pytest.fixture(autouse=True)
def _isolated_response_rail(tmp_path, monkeypatch):
    path = tmp_path / "responses.json"
    original_write = responses.atomic_write_bytes
    monkeypatch.setattr(responses, "_store_path", lambda: path)
    responses._reset_cache_for_tests()
    chat_socket._reset_for_tests()
    yield
    # The session's outer rail fixture also clears after us. Restore deliberately
    # injected I/O failures/corruption before it performs that legitimate write.
    monkeypatch.setattr(responses, "atomic_write_bytes", original_write)
    monkeypatch.setattr(responses, "_store_path", lambda: path)
    path.write_text("[]", encoding="utf-8")
    responses._reset_cache_for_tests()
    chat_socket._reset_for_tests()


def test_failed_response_write_is_not_a_durable_answer_and_retry_can_store_it(monkeypatch):
    published = []
    captured = []
    chat_socket.subscribe(published.append)
    monkeypatch.setattr(responses, "remember_assistant_turn", lambda *args: captured.append(args))
    original_write = responses.atomic_write_bytes
    failure = OSError("answer disk unavailable")

    def fail_write(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(responses, "atomic_write_bytes", fail_write)
    with pytest.raises(OSError) as raised:
        responses.append_response(cycle_id="waiting-cycle", episode_id="owner", text="The result")
    assert raised.value is failure
    assert responses.get_response("waiting-cycle") is None
    assert published == []
    assert captured == []

    monkeypatch.setattr(responses, "atomic_write_bytes", original_write)
    responses.append_response(cycle_id="waiting-cycle", episode_id="owner", text="The result")
    responses._reset_cache_for_tests()
    assert responses.get_response("waiting-cycle")["text"] == "The result"


@pytest.mark.parametrize("contents", ["{broken", "{}", '["not a response"]'])
def test_unreadable_response_store_does_not_become_an_empty_success(contents):
    path = responses._store_path()
    path.write_text(contents, encoding="utf-8")

    # The second call must also fail: failed loads must not poison the loaded cache.
    for _ in range(2):
        with pytest.raises((ValueError, TypeError)):
            responses.get_response("waiting-cycle")
    assert path.read_text(encoding="utf-8") == contents


@pytest.mark.parametrize("invalid", ["missing_cycle", "nonstring_cycle", "invalid_status", "duplicate_cycle"])
def test_stored_reply_identity_and_state_are_unambiguous(invalid):
    responses.append_response(cycle_id="known-cycle", episode_id="owner", text="Known result")
    path = responses._store_path()
    rows = json.loads(path.read_text(encoding="utf-8"))
    if invalid == "missing_cycle":
        rows[0].pop("cycle_id")
    elif invalid == "nonstring_cycle":
        rows[0]["cycle_id"] = 42
    elif invalid == "invalid_status":
        rows[0]["status"] = "unknown"
    else:
        rows.append({**rows[0], "episode_id": "different-owner", "text": "Conflicting answer"})
    path.write_text(json.dumps(rows), encoding="utf-8")
    responses._reset_cache_for_tests()

    for _ in range(2):
        with pytest.raises(ValueError, match="response"):
            responses.get_response("known-cycle")


def test_response_capacity_failure_is_distinct_from_a_valid_noop(monkeypatch):
    monkeypatch.setattr(responses, "_MAX_RESPONSES", 1)
    responses.append_response(cycle_id="first", episode_id="one", text="first result")
    with pytest.raises(RuntimeError, match="capacity"):
        responses.append_response(cycle_id="second", episode_id="two", text="second result")
    assert responses.get_response("first")["text"] == "first result"
    assert responses.get_response("second") is None


@pytest.mark.parametrize("operation", ["mark_delivered", "clear_responses"])
def test_failed_response_state_write_preserves_the_pending_answer(monkeypatch, operation):
    responses.append_response(cycle_id="pending-cycle", episode_id="owner", text="Pending result")
    failure = OSError("response status disk unavailable")

    def fail_write(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(responses, "atomic_write_bytes", fail_write)
    with pytest.raises(OSError) as raised:
        if operation == "mark_delivered":
            responses.mark_delivered("pending-cycle")
        else:
            responses.clear_responses()
    assert raised.value is failure
    assert responses.get_response("pending-cycle")["status"] == responses.STATUS_PENDING
    responses._reset_cache_for_tests()
    assert responses.get_response("pending-cycle")["status"] == responses.STATUS_PENDING


def test_unrelated_open_socket_does_not_claim_an_orphan_answer(monkeypatch):
    # Use the real server's subscription callback: it filters by owned cycle/job.
    from tests.hud.test_chat_websocket_route import _AckingSocket, _chat_endpoint

    endpoint = _chat_endpoint(monkeypatch)

    def handle_operator_chat(**kwargs):
        responses.append_response(cycle_id="disconnected-turn", episode_id="other-owner", text="Recover me")
        responses.append_response(cycle_id=kwargs["cycle_id"], episode_id="live-owner", text="Live result")
        return {"ok": True, "queued": True, "cycle_id": kwargs["cycle_id"]}

    monkeypatch.setattr("pressure_loop.operator_chat.handle_operator_chat", handle_operator_chat)
    live_cycle = "4323318b-28a3-48a3-a610-2bd1c96d8293"
    # The page acknowledges the reply it rendered; only that marks it delivered.
    socket = _AckingSocket({"kind": "chat.user_message", "text": "A live turn", "cycle_id": live_cycle}, ack="")
    asyncio.run(asyncio.wait_for(endpoint(socket), timeout=5))

    assert [row["cycle_id"] for row in socket.frames] == [live_cycle]
    assert responses.get_response(live_cycle)["status"] == responses.STATUS_DELIVERED
    assert responses.get_response("disconnected-turn")["status"] == responses.STATUS_PENDING
    assert [row["cycle_id"] for row in responses.list_undelivered(min_age_s=0)] == ["disconnected-turn"]


def test_socket_send_failure_leaves_the_durable_answer_pending(monkeypatch):
    from tests.hud.test_chat_websocket_route import _Socket, _chat_endpoint

    endpoint = _chat_endpoint(monkeypatch)

    class BrokenSocket(_Socket):
        async def send_json(self, payload):
            self._finished.set()
            raise OSError("socket disconnected before send")

    def handle_operator_chat(**kwargs):
        responses.append_response(cycle_id=kwargs["cycle_id"], episode_id="owner", text="Recover me")
        return {"ok": True, "queued": True, "cycle_id": kwargs["cycle_id"]}

    monkeypatch.setattr("pressure_loop.operator_chat.handle_operator_chat", handle_operator_chat)
    broken_cycle = "b8ebf2a0-739e-43c7-9d90-e5eab6051a0c"
    socket = BrokenSocket({"kind": "chat.user_message", "text": "A turn", "cycle_id": broken_cycle})
    asyncio.run(asyncio.wait_for(endpoint(socket), timeout=5))
    assert responses.get_response(broken_cycle)["status"] == responses.STATUS_PENDING
    assert socket.closed_code == 1011
    assert chat_socket.subscriber_count() == 0


def test_socket_ack_write_failure_closes_the_receiver_and_retains_the_answer(monkeypatch, caplog):
    import logging

    from tests.hud.test_chat_websocket_route import _Socket, _chat_endpoint

    endpoint = _chat_endpoint(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="omega.hud.server")
    cycle_id = "792e0cef-0b7c-4290-bd40-42a8366f8283"
    failure = OSError("ACK persistence sentinel")
    original_write = responses.atomic_write_bytes

    class OpenSocket(_Socket):
        """A real page stays open after a reply and acknowledges the one it rendered."""

        def __init__(self, message):
            super().__init__(message)
            self._replied = asyncio.Event()

        async def receive_json(self):
            self._reads += 1
            if self._reads == 1:
                return self._message
            if self._reads == 2:
                await self._replied.wait()
                return {"kind": "chat.ack", "cycle_id": cycle_id}
            await self._finished.wait()
            raise WebSocketDisconnect()

        async def send_json(self, payload):
            self.frames.append(dict(payload))
            if payload.get("kind") == "chat.reply":
                self._replied.set()

    def handle_operator_chat(**kwargs):
        responses.append_response(cycle_id=kwargs["cycle_id"], episode_id="owner", text="Recover me")
        return {"ok": True, "queued": True, "cycle_id": kwargs["cycle_id"]}

    monkeypatch.setattr("pressure_loop.operator_chat.handle_operator_chat", handle_operator_chat)
    socket = OpenSocket({"kind": "chat.user_message", "text": "A turn", "cycle_id": cycle_id})

    async def exercise():
        attempted = asyncio.Event()

        def fail_ack(path, payload):
            if any(row["status"] == responses.STATUS_DELIVERED for row in json.loads(payload)):
                attempted.set()
                raise failure
            return original_write(path, payload)

        monkeypatch.setattr(responses, "atomic_write_bytes", fail_ack)
        task = asyncio.create_task(endpoint(socket))
        try:
            await asyncio.wait_for(attempted.wait(), timeout=5)
            done, _ = await asyncio.wait({task}, timeout=0.5)
            assert done, "failed ACK left the socket receiver alive without an outgoing pump"
            await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())
    assert [frame["cycle_id"] for frame in socket.frames] == [cycle_id]
    assert socket.closed_code == 1011
    assert chat_socket.subscriber_count() == 0
    assert responses.get_response(cycle_id)["status"] == responses.STATUS_PENDING
    responses._reset_cache_for_tests()
    assert responses.get_response(cycle_id)["status"] == responses.STATUS_PENDING
    assert any(record.exc_info and record.exc_info[1] is failure for record in caplog.records)


def test_socket_cancellation_reaps_its_sender_and_receiver(monkeypatch):
    from tests.hud.test_chat_websocket_route import _Socket, _chat_endpoint

    endpoint = _chat_endpoint(monkeypatch)

    async def exercise():
        entered = asyncio.Event()
        before = asyncio.all_tasks()

        class WaitingSocket(_Socket):
            async def receive_json(self):
                entered.set()
                await asyncio.Future()

        task = asyncio.create_task(endpoint(WaitingSocket({})))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert asyncio.all_tasks() == before

    asyncio.run(exercise())
    assert chat_socket.subscriber_count() == 0


@pytest.mark.parametrize("boundary", ["auto", "settled", "failed", "close", "query_close", "proactive_close"])
def test_reply_storage_failure_is_episode_content(monkeypatch, boundary):
    import cognition_chain
    from memory import episode_sram
    from memory.episode_events import read_episode_events
    from tests.core.test_chat_deliver_at_auto import _episode

    episode = _episode(
        contributors=[{"id": "operator_query", "detail": {
            "cycle_id": "waiting-cycle", "chat_bubble_waiting": True,
        }}],
        response_text="The action result",
    )
    episode_sram.open_bucket(episode.episode_id, pipeline_id=0)
    failure = OSError("response rail storage failed sentinel")

    def fail_response(**_kwargs):
        raise failure

    monkeypatch.setattr(responses, "append_response", fail_response)
    if boundary == "auto":
        cognition_chain._deliver_chat_reply_at_auto(episode)
    elif boundary == "settled":
        monkeypatch.setattr(cognition_chain, "_omega_settles_turn", lambda _episode: True)
        monkeypatch.setattr(cognition_chain, "_close_settled_agent_job", lambda *_args: None)
        monkeypatch.setattr("pressure_loop.pressure.release_continuation", lambda *_args: None)
        cognition_chain._settle_operator_turn(episode, None)
    elif boundary == "failed":
        episode.meta["cascade_failure"] = {"error_type": "RuntimeError", "message": "AUTO failed"}
        assert not cognition_chain._deliver_failed_chat_bubble(episode, episode.meta)
    else:
        from unittest.mock import Mock

        if boundary in {"query_close", "proactive_close"}:
            episode.global_["intent_decision"]["intent"] = "RESOLVE_QUERY"
        if boundary == "proactive_close":
            episode.pressure["contributors"][0]["detail"]["chat_bubble_waiting"] = False
            monkeypatch.setattr("pressure_loop.proactive_chat_messages.append_update", fail_response)
        cognition_chain._record_operator_outcome(Mock(), episode)
    events = read_episode_events(episode.episode_id)
    failures = [event for event in events if event.event_kind == "chat.delivery_failed"]
    assert len(failures) == 1
    assert "OSError: response rail storage failed sentinel" in failures[0].payload["reason"]
    assert "chat_reply_delivered_cycle" not in episode.meta


def test_hot_turn_capture_failure_preserves_its_cause_for_the_owning_episode(monkeypatch):
    from memory import episode_sram

    episode_sram.open_bucket("hot-turn-owner", pipeline_id=0)
    failure = OSError("conversation projection unavailable")

    def fail_append(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr("memory.hot_buffer.append_turn", fail_append)
    with pytest.raises(OSError) as raised:
        responses.remember_assistant_turn("hot-turn-owner", "The captured reply")
    assert raised.value is failure
    assert [item.text for item in episode_sram.pending("hot-turn-owner") if item.kind == "turn"] == [
        "The captured reply",
    ]


@pytest.mark.parametrize("transport", ["websocket", "poll", "orphan"])
def test_all_reply_transports_preserve_operator_question_and_workspace_assignment(monkeypatch, transport):
    question = {"question": "Which output format?", "options": ["Markdown", "HTML"], "job_id": "job-question"}
    assignment = {"project_root": "workspace/report", "job_id": "job-question"}
    record = responses.append_response(
        cycle_id="question-cycle",
        episode_id="question-owner",
        text=question["question"],
        operator_question=question,
        workspace_assignment=assignment,
    )
    assert record is not None
    if transport == "websocket":
        sent = []
        chat_socket.subscribe(sent.append)
        chat_socket.publish_reply(record)
        payload = sent[0]
    else:
        import hud.server as server
        from tests.hud.test_chat_websocket_route import _Lifecycle

        monkeypatch.setattr(server, "get_default_plugin_lifecycle_service", lambda: _Lifecycle())
        app = server.create_app()
        route_path = "/api/chat/responses" if transport == "poll" else "/api/chat/responses/orphans"
        endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", None) == route_path)
        if transport == "poll":
            payload = json.loads(endpoint(cycle_id="question-cycle").body)
        else:
            monkeypatch.setattr(responses, "list_undelivered", lambda **_kwargs: [record])
            payload = json.loads(endpoint().body)["responses"][0]
    assert payload.get("operator_question") == question
    assert payload.get("workspace_assignment") == assignment


@pytest.mark.parametrize("delivery_failure", [False, True])
def test_chat_reply_and_delivery_failure_reach_canonical_memory(monkeypatch, tmp_path, delivery_failure):
    import cognition_chain
    from memory import episode_commit_view, episode_sram
    from memory.engine import walker
    from tests.helpers.blueprint_driver import BlueprintDriver, agent_decision

    driver = BlueprintDriver(monkeypatch=monkeypatch, tmp_path=tmp_path)
    answer = "CHAT-ENVELOPE-SENTINEL: the requested explanation."
    request = "Explain the chat envelope sentinel."
    driver.agent_batches.append({"text": answer})
    driver.omega.append(agent_decision("DONE", synthesis="The answer was reviewed."))
    original_omega = cognition_chain._run_omega_episode_cognition
    observed = []
    if delivery_failure:
        original_persist = responses._persist
        attempts = []

        def fail_first_delivery():
            attempts.append(True)
            if len(attempts) == 1:
                raise OSError("canonical delivery failure sentinel")
            original_persist()

        monkeypatch.setattr(responses, "_persist", fail_first_delivery)

    def check_reply_before_reflection(episode):
        if delivery_failure:
            assert driver.reply("chat-roundtrip-cycle") is None
            assert "chat_reply_delivered_cycle" not in episode.meta
        else:
            assert driver.reply("chat-roundtrip-cycle")["text"] == answer
        assert episode_commit_view.episode_record_ref(episode.episode_id) is None
        assert episode_sram.is_open(episode.episode_id)
        observed.append(episode.episode_id)
        return original_omega(episode)

    monkeypatch.setattr(cognition_chain, "_run_omega_episode_cognition", check_reply_before_reflection)
    episode = driver.operator_turn(request, route="FREEFORM_CHAT", cycle_id="chat-roundtrip-cycle")

    assert observed == [episode.episode_id]
    assert driver.store.current() is None
    assert not episode_sram.is_open(episode.episode_id)
    ref = episode_commit_view.episode_record_ref(episode.episode_id)
    assert ref is not None
    walker.load_coord_index()
    assert ref.record_id in walker.COORD_INDEX
    document = json.loads(Path(walker.path_for_id(ref.record_id)).read_text(encoding="utf-8"))
    body = json.loads(document["text"])
    assert body["outputs"]["response_text"] == answer
    assert body["outcome"] == episode.global_["outcome"]
    assert body["episode_record"]["objective"]["original_request"] == request
    if delivery_failure:
        events = body["episode_record"]["events"]["by_value"]
        failures = [event for event in events if event["event_kind"] == "chat.delivery_failed"]
        assert len(failures) == 1
        assert failures[0]["payload"]["reason"] == "OSError: canonical delivery failure sentinel"

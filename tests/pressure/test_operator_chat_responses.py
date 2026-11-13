"""The operator-chat-response store: durable, keyed by the operator cycle_id,
terminal-display only (NO pressure re-injection, NO operator-owed badge)."""

from __future__ import annotations

import importlib
import json

import pytest

import pressure_loop.operator_chat_responses as ocr


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "_store_path", lambda: tmp_path / "ocr.json")
    monkeypatch.setattr(ocr, "_lane_updates_path", lambda: tmp_path / "ocr-lane-updates.json")
    ocr._reset_cache_for_tests()


def test_append_get_mark_delivered(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ocr.append_response(cycle_id="abc", episode_id="ep1", text="Hello Noah")
    rec = ocr.get_response("abc")
    assert rec is not None
    assert rec["text"] == "Hello Noah"
    assert rec["status"] == "pending"
    assert ocr.get_response("nope") is None

    ocr.mark_delivered("abc")
    assert ocr.get_response("abc")["status"] == "delivered"


def test_keys_on_cycle_id_not_episode_id(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ocr.append_response(cycle_id="cyc-1", episode_id="ep-99", text="answer")
    # retrievable by the OPERATOR cycle_id, not the answering episode_id
    assert ocr.get_response("cyc-1") is not None
    assert ocr.get_response("ep-99") is None


def test_durable_across_reload(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ocr.append_response(cycle_id="dur", episode_id="ep", text="persisted")
    ocr._reset_cache_for_tests()  # drop in-memory; force reload from disk
    assert ocr.get_response("dur")["text"] == "persisted"


def test_persists_async_thinking_payload(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ocr.append_response(
        cycle_id="with-thinking",
        episode_id="ep",
        text="persisted",
        delivery={"text": "persisted", "rendered_by": "omega_async"},
        route={"opened_by": "operator_query", "intent": "RESPOND", "action": "RESPONSE"},
        prompt_pack={"prompt_type": "RESPONSE_QUESTION"},
        thinking={"memory": {"entries": [{"label": "Creator", "preview": "Built by Noah"}]}},
    )
    rec = ocr.get_response("with-thinking")
    assert rec["delivery"]["rendered_by"] == "omega_async"
    assert rec["prompt_pack"]["prompt_type"] == "RESPONSE_QUESTION"
    assert rec["thinking"]["memory"]["entries"][0]["label"] == "Creator"


def test_specialist_lane_updates_are_durable_and_idempotent(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ocr.append_response(
        cycle_id="operator-cycle",
        episode_id="chat-episode",
        text="Queued for Browser.",
        lane_work={
            "handoff_id": "h" * 64,
            "lane": "browser",
            "original_cycle_id": "operator-cycle",
            "thread_id": "conversation-1",
        },
    )

    started = ocr.append_lane_update(
        cycle_id="operator-cycle",
        episode_id="browser-episode",
        lane="browser",
        handoff_id="h" * 64,
        status="started",
        text="Browser lane started.",
        update_id="browser-episode:started",
    )
    retried = ocr.append_lane_update(
        cycle_id="operator-cycle",
        episode_id="browser-episode",
        lane="browser",
        handoff_id="h" * 64,
        status="started",
        text="Browser lane started.",
        update_id="browser-episode:started",
    )
    completed = ocr.append_lane_update(
        cycle_id="operator-cycle",
        episode_id="browser-episode",
        lane="browser",
        handoff_id="h" * 64,
        status="completed",
        text="Browser found two matching pages.",
        update_id="browser-episode:completed",
    )

    assert started == retried
    assert started["seq"] == 1
    assert completed["seq"] == 2
    ocr._reset_cache_for_tests()
    record = ocr.get_response("operator-cycle")
    updates = ocr.get_lane_updates("operator-cycle")
    assert record["lane_work"]["status"] == "completed"
    assert [row["text"] for row in updates["updates"]] == [
        "Browser lane started.",
        "Browser found two matching pages.",
    ]


def test_specialist_update_acknowledgement_survives_reload_and_closes_recovery(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ocr.append_response(
        cycle_id="operator-cycle",
        episode_id="chat-episode",
        text="Queued for Browser.",
        lane_work={
            "handoff_id": "h" * 64,
            "lane": "browser",
            "original_cycle_id": "operator-cycle",
            "thread_id": "conversation-1",
        },
    )
    for seq, status, text in (
        (1, "started", "Browser started."),
        (2, "completed", "Browser completed."),
    ):
        ocr.append_lane_update(
            cycle_id="operator-cycle", episode_id="browser-episode", lane="browser",
            handoff_id="h" * 64, status=status, text=text,
            update_id=f"browser-episode:{seq}",
        )

    assert ocr.list_active_lane_work()[0]["last_seq"] == 2
    ocr.mark_delivered("operator-cycle")
    assert ocr.acknowledge_lane_updates("operator-cycle", through_seq=2) == 2
    ocr._reset_cache_for_tests()

    assert ocr.get_lane_updates("operator-cycle")["acknowledged_seq"] == 2
    assert ocr.list_active_lane_work() == []


def test_specialist_update_acknowledgement_rejects_an_unseen_sequence(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ocr.append_lane_update(
        cycle_id="operator-cycle", episode_id="agent-episode", lane="agent",
        handoff_id="h" * 64, status="started", text="Coding Agent started.",
        update_id="agent-episode:started",
    )

    with pytest.raises(ValueError, match="beyond the last update"):
        ocr.acknowledge_lane_updates("operator-cycle", through_seq=2)


def test_persists_plugin_permission_hud_dict_and_strips_secrets(tmp_path, monkeypatch):
    from hud import chat_socket

    _isolate(tmp_path, monkeypatch)
    chat_socket._reset_for_tests()
    published: list[dict] = []
    chat_socket.subscribe(published.append)
    hud = {
        "request_id": "a" * 48,
        "revision": "b" * 64,
        "display": {
            "plugin_apps": [
                {
                    "plugin_id": "google.workspace.drive",
                    "plugin_name": "Workspace Drive",
                    "app_id": "google.workspace.drive.mcp",
                    "app_name": "Workspace Drive Mcp",
                }
            ],
            "effect_summary": "Create data",
            "ordered_operations": ["Create file"],
            "audience": "{}",
            "destination": "{}",
            "crosses_providers": False,
            "field_names": ["name"],
            "risks": ["write"],
        },
    }
    stuffed = {
        **hud,
        "access_token": "ya29.secret",
        "plan": {"arguments": {"body": "secret-argument-value"}},
        "callback_url": "https://oauth.example/callback",
    }

    stored = ocr.append_response(
        cycle_id="perm-1",
        episode_id="ep-perm",
        text="Permission required",
        plugin_permission_request=stuffed,
    )

    assert stored is not None
    assert stored["plugin_permission_request"] == hud
    assert published[0]["plugin_permission_request"] == hud
    ocr._reset_cache_for_tests()
    reloaded = ocr.get_response("perm-1")
    assert reloaded is not None
    assert reloaded["plugin_permission_request"] == hud
    serialized = json.dumps(reloaded).lower() + json.dumps(published[0]).lower()
    for token in ("ya29.secret", "secret-argument-value", "https://oauth.example/callback"):
        assert token not in serialized
    chat_socket._reset_for_tests()


def test_permission_card_persists_without_chat_text(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    hud = {
        "request_id": "c" * 48,
        "revision": "d" * 64,
        "display": {
            "plugin_apps": [],
            "effect_summary": "Create data",
            "ordered_operations": ["Create file"],
            "audience": "{}",
            "destination": "{}",
            "crosses_providers": False,
            "field_names": ["name"],
            "risks": ["write"],
        },
    }
    stored = ocr.append_response(
        cycle_id="perm-empty",
        episode_id="ep-perm",
        text="",
        plugin_permission_request=hud,
    )
    assert stored is not None
    assert stored["text"] == ""
    assert stored["plugin_permission_request"] == hud


def test_blank_cycle_or_text_is_noop(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    assert ocr.append_response(cycle_id="", episode_id="ep", text="x") is None
    assert ocr.append_response(cycle_id="c", episode_id="ep", text="   ") is None
    assert ocr.list_pending() == []


def test_idempotent_same_turn_answered_once(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ocr.append_response(cycle_id="once", episode_id="ep1", text="first")
    ocr.append_response(cycle_id="once", episode_id="ep2", text="second")
    assert ocr.get_response("once")["text"] == "first"
    assert len(ocr.list_pending()) == 1


def test_new_response_holds_one_assistant_turn_for_episode(tmp_path, monkeypatch):
    import memory.episode_sram as sram

    _isolate(tmp_path, monkeypatch)
    sram.reset()
    sram.open_bucket("ep-assistant", pipeline_id=0)
    sram.hold_turn("user", "What is the retry budget?")

    ocr.append_response(
        cycle_id="assistant-once",
        episode_id="ep-assistant",
        text="The retry budget is three attempts.",
    )
    ocr.append_response(
        cycle_id="assistant-once",
        episode_id="ep-assistant",
        text="A duplicate answer that must be ignored.",
    )

    turns = [item for item in sram.pending() if item.kind == "turn"]
    assert [(item.role, item.text) for item in turns] == [
        ("user", "What is the retry budget?"),
        ("assistant", "The retry budget is three attempts."),
    ]
    sram.reset()


def test_new_response_appends_assistant_to_hot_buffer_without_episode(tmp_path, monkeypatch):
    import memory.episode_sram as sram

    _isolate(tmp_path, monkeypatch)
    sram.reset()
    appended: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "memory.hot_buffer.append_turn",
        lambda role, text, **_kwargs: appended.append((role, text)),
    )

    ocr.append_response(
        cycle_id="assistant-hot",
        episode_id="closed-episode",
        text="This answer remains in conversation memory.",
    )
    ocr.append_response(
        cycle_id="assistant-hot",
        episode_id="closed-episode",
        text="duplicate",
    )

    assert appended == [("assistant", "This answer remains in conversation memory.")]


def test_no_pressure_reinjection_or_badge_surface():
    """Terminal-display contract: this module must NOT carry the system_messages
    re-injection rail (enqueue_operator_input) or an operator-owed badge."""
    src = importlib.util.find_spec("pressure_loop.operator_chat_responses").origin
    text = open(src, encoding="utf-8").read()
    assert "enqueue_operator_input" not in text
    assert "unread_count" not in text

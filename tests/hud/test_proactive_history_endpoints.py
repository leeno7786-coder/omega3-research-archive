"""Durable proactive pinboard and read-only history endpoint contracts."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hud.server import create_app  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_stores(tmp_path, monkeypatch):
    import pressure_loop.cognition_queue as operator_queue
    import pressure_loop.proactive_chat_messages as notices
    import pressure_loop.proactive_lifecycle as lifecycle
    import pressure_loop.system_messages as dialogue

    monkeypatch.setattr(operator_queue, "_queue_path", lambda: tmp_path / "operator_queue.json")
    monkeypatch.setattr(notices, "_store_path", lambda: tmp_path / "proactive_notices.json")
    monkeypatch.setattr(lifecycle, "_ledger_path", lambda: tmp_path / "proactive_lifecycle.jsonl")
    monkeypatch.setattr(dialogue, "_store_path", lambda: tmp_path / "system_messages.json")
    operator_queue._reset_cache_for_tests()
    notices._reset_cache_for_tests()
    lifecycle._reset_cache_for_tests()
    dialogue._reset_cache_for_tests()
    yield


def _client() -> TestClient:
    return TestClient(create_app())


def test_notice_delivery_and_clear_are_separate_transitions():
    import pressure_loop.proactive_chat_messages as notices

    rec = notices.append_update(
        text="I finished comparing the three distributions.",
        episode_id="ep-notice",
        opened_by="proactive_update",
    )
    assert rec is not None
    client = _client()

    pending = client.get("/api/proactive/notices")
    assert pending.status_code == 200
    assert pending.json()["messages"][0]["status"] == "pending"

    delivered = client.post(f"/api/chat/proactive/{rec['id']}/delivered")
    assert delivered.status_code == 200
    pinned = client.get("/api/proactive/notices")
    assert pinned.json()["messages"][0]["status"] == "delivered"

    cleared = client.post("/api/proactive/notices/clear")
    assert cleared.status_code == 200
    assert cleared.json() == {"ok": True, "cleared": 1}
    assert client.get("/api/proactive/notices").json() == {
        "messages": [],
        "count": 0,
    }


def test_history_correlates_dialogue_responses_and_cleared_notices():
    import pressure_loop.proactive_chat_messages as notices
    from pressure_loop.system_messages import (
        append_result_presentation,
        append_system_query,
    )

    question = append_system_query(
        text="Should I continue with boundary cases?",
        episode_id="ep-question",
        opened_by="operator_priority",
    )
    notice = notices.append_update(
        text="Scenario pass complete.",
        episode_id="ep-notice",
        opened_by="proactive_update",
    )
    result = append_result_presentation(
        text="I classified all ten scenarios. How would you grade the pass?",
        episode_id="ep-result",
        opened_by="proactive_result",
        result_id="result-1",
        thread_id="thread-1",
        mastery_delta=0.8,
    )
    assert notice is not None and result is not None
    client = _client()
    answered = client.post(
        f"/api/system-messages/{question['id']}/respond",
        json={"text": "Yes, focus on Binomial versus Poisson."},
    )
    assert answered.status_code == 200
    graded = client.post(
        f"/api/system-messages/{result['id']}/respond",
        json={"text": "Good application; continue to the next lesson."},
    )
    assert graded.status_code == 200
    assert client.post("/api/proactive/notices/clear").status_code == 200

    response = client.get("/api/proactive/history")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 3
    by_id = {entry["id"]: entry for entry in body["entries"]}
    assert by_id[question["id"]]["type"] == "question"
    assert by_id[question["id"]]["status"] == "responded"
    assert by_id[question["id"]]["response"]["text"] == ("Yes, focus on Binomial versus Poisson.")
    assert by_id[notice["id"]]["type"] == "notice"
    assert by_id[notice["id"]]["status"] == "cleared"
    assert by_id[notice["id"]]["response"] is None
    assert by_id[result["id"]]["type"] == "result"
    assert by_id[result["id"]]["status"] == "responded"
    assert by_id[result["id"]]["response"]["text"] == (
        "Good application; continue to the next lesson."
    )


def test_system_message_delete_route_is_not_available_and_record_survives():
    from pressure_loop.system_messages import append_system_query, list_messages

    rec = append_system_query(
        text="Durable question?",
        episode_id="ep-durable",
        opened_by="operator_priority",
    )
    response = _client().delete(f"/api/system-messages/{rec['id']}")

    assert response.status_code in (404, 405)
    assert [row["id"] for row in list_messages()] == [rec["id"]]


def test_identical_notice_text_never_lands_twice():
    """Text is the identity for autonomous notices across fresh episode IDs."""
    import pressure_loop.proactive_chat_messages as notices

    first = notices.append_update(
        text="Plugin connection ready.",
        episode_id="plugin-ready-uuid-1",
        opened_by="plugin_update",
    )
    again = notices.append_update(
        text="Plugin connection ready.",
        episode_id="plugin-ready-uuid-2",
        opened_by="plugin_update",
    )
    assert again["id"] == first["id"]
    assert len(notices.list_messages()) == 1

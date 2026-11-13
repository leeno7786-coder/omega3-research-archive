"""HUD system-messages endpoints — RED-first against live FastAPI routes."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hud.server import create_app  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_stores(tmp_path, monkeypatch):
    import guardian.external_ingress as external_ingress
    import memory.source_ledger.store as source_ledger
    import pressure_loop.cognition_queue as operator_queue
    import pressure_loop.system_messages as system_messages

    monkeypatch.setattr(
        operator_queue,
        "_queue_path",
        lambda: tmp_path / "operator_input_queue.json",
    )
    operator_queue._reset_cache_for_tests()
    monkeypatch.setattr(
        system_messages,
        "_store_path",
        lambda: tmp_path / "system_messages.json",
    )
    system_messages._reset_cache_for_tests()
    monkeypatch.setattr(
        source_ledger,
        "record_operator_chat_source",
        lambda text, *, cycle_id="", **_: {
            "source_id": f"operator-chat:{cycle_id}",
            "content_hash": "test-hash",
        },
    )
    monkeypatch.setattr(
        external_ingress,
        "screen_text",
        lambda text, **_: (str(text), "allow"),
    )
    yield


def _client() -> TestClient:
    return TestClient(create_app())


def test_get_lists_messages_and_unread():
    from pressure_loop.system_messages import append_system_query

    append_system_query(
        text="First query?",
        episode_id="ep-hud-1",
        opened_by="operator_priority",
    )
    append_system_query(
        text="Second query?",
        episode_id="ep-hud-2",
        opened_by="operator_priority",
    )

    response = _client().get("/api/system-messages")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["messages"]) == 2
    assert payload["unread"] == 2


def test_respond_attaches_and_enqueues():
    from pressure_loop.cognition_queue import peek_inputs
    from pressure_loop.system_messages import append_system_query

    rec = append_system_query(
        text="What should I learn?",
        episode_id="ep-hud-3",
        opened_by="operator_priority",
        seed_question="Learning focus?",
    )
    client = _client()

    empty = client.post(
        f"/api/system-messages/{rec['id']}/respond",
        json={"text": "   "},
    )
    assert empty.status_code == 400

    missing = client.post(
        "/api/system-messages/no-such-id/respond",
        json={"text": "hello"},
    )
    assert missing.status_code == 404

    ok = client.post(
        f"/api/system-messages/{rec['id']}/respond",
        json={"text": "Focus on quantum basics."},
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["ok"] is True
    assert body["message"]["status"] == "responded"
    assert body["message"]["response"]["text"] == "Focus on quantum basics."

    queued = peek_inputs()
    assert len(queued) == 1
    assert queued[0].metadata.get("system_query_id") == rec["id"]
    citation = queued[0].metadata.get("source_citation") or {}
    assert citation["source_id"] == f"operator-chat:{queued[0].metadata['cycle_id']}"


def test_proactive_response_guardian_rejection_leaves_question_pending(
    monkeypatch,
):
    import guardian.external_ingress as external_ingress
    from pressure_loop.cognition_queue import peek_inputs
    from pressure_loop.system_messages import append_system_query, list_messages

    rec = append_system_query(
        text="What should I learn?",
        episode_id="ep-hud-gated",
        opened_by="operator_priority",
    )
    monkeypatch.setattr(
        external_ingress,
        "screen_text",
        lambda text, **_: ("", "quarantine"),
    )

    response = _client().post(
        f"/api/system-messages/{rec['id']}/respond",
        json={"text": "untrusted operator text"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "guardian_quarantined"
    assert peek_inputs() == ()
    stored = next(row for row in list_messages() if row["id"] == rec["id"])
    assert stored["status"] == "pending"


def test_history_surface_exposes_no_destructive_message_route():
    """Dialogue remains durable; neither ignore nor delete is an action."""
    from pressure_loop.cognition_queue import peek_inputs
    from pressure_loop.system_messages import append_system_query, list_messages

    rec = append_system_query(
        text="Want a summary?",
        episode_id="ep-hud-4",
        opened_by="operator_priority",
    )
    client = _client()

    # The ignore route is removed — not 200, not 404-on-id: no route at all.
    gone = client.post(f"/api/system-messages/{rec['id']}/ignore")
    assert gone.status_code in (404, 405)

    delete_attempt = client.delete(f"/api/system-messages/{rec['id']}")
    assert delete_attempt.status_code in (404, 405)
    assert [m["id"] for m in list_messages()] == [rec["id"]]
    assert peek_inputs() == ()


def test_badge_count_excludes_handled():
    from pressure_loop.system_messages import append_system_query

    first = append_system_query(
        text="Pending one",
        episode_id="ep-hud-6",
        opened_by="operator_priority",
    )
    second = append_system_query(
        text="Pending two",
        episode_id="ep-hud-7",
        opened_by="operator_priority",
    )
    third = append_system_query(
        text="Pending three",
        episode_id="ep-hud-8",
        opened_by="operator_priority",
    )
    client = _client()

    client.post(
        f"/api/system-messages/{first['id']}/respond",
        json={"text": "Done."},
    )
    client.post(
        f"/api/system-messages/{second['id']}/respond",
        json={"text": "Done too."},
    )

    response = client.get("/api/system-messages")
    assert response.status_code == 200
    payload = response.json()
    assert payload["unread"] == 1
    pending = [m for m in payload["messages"] if m["status"] == "pending"]
    assert len(pending) == 1
    assert pending[0]["id"] == third["id"]


def test_respond_carries_the_operators_explicit_verdict_on_a_presented_result():
    """The operator's verdict is a control they chose, not a reading of their words."""
    from pressure_loop.cognition_queue import peek_inputs
    from pressure_loop.system_messages import append_result_presentation

    rec = append_result_presentation(
        text="I finished the refactor you asked about.",
        episode_id="ep-hud-graded",
        opened_by="omega_successor",
        result_id="result-hud-1",
        thread_id="thread-hud-1",
    )
    client = _client()

    refused = client.post(
        f"/api/system-messages/{rec['id']}/respond",
        json={"text": "it worked", "verdict": "lgtm"},
    )
    assert refused.status_code == 400
    assert not peek_inputs(), "a refused verdict still queued the reply"

    ok = client.post(
        f"/api/system-messages/{rec['id']}/respond",
        json={"text": "it didn't work", "verdict": "contradicts"},
    )
    assert ok.status_code == 200
    assert ok.json()["message"]["response"]["verdict"] == "contradicts"

    queued = peek_inputs()
    assert len(queued) == 1
    assert queued[0].metadata.get("system_query_episode_id") == "ep-hud-graded"
    assert queued[0].metadata.get("system_query_kind") == "result_presentation"
    assert queued[0].metadata.get("system_query_verdict") == "contradicts"


def test_a_reply_without_a_verdict_carries_none_and_still_names_its_subject():
    from pressure_loop.cognition_queue import peek_inputs
    from pressure_loop.system_messages import append_result_presentation

    rec = append_result_presentation(
        text="I finished the other refactor.",
        episode_id="ep-hud-ungraded",
        opened_by="omega_successor",
        result_id="result-hud-2",
        thread_id="thread-hud-2",
    )
    ok = _client().post(
        f"/api/system-messages/{rec['id']}/respond",
        json={"text": "hmm, not sure yet"},
    )
    assert ok.status_code == 200
    assert not ok.json()["message"]["response"].get("verdict")
    queued = peek_inputs()
    assert queued[0].metadata.get("system_query_episode_id") == "ep-hud-ungraded"
    assert not queued[0].metadata.get("system_query_verdict")

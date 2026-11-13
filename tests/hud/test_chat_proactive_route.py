"""GET/POST proactive chat — poll pending, ack after HUD render."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hud.server import create_app  # noqa: E402
import pressure_loop.proactive_chat_messages as pcm  # noqa: E402


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(pcm, "_store_path", lambda: tmp_path / "pcm.json")
    pcm._reset_cache_for_tests()


def test_poll_returns_pending_without_marking_delivered(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    rec = pcm.append_update(
        text="Still working through the quantum module.",
        episode_id="ep-1",
        opened_by="proactive_update",
    )
    assert rec is not None

    client = TestClient(create_app())
    r = client.get("/api/chat/proactive")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["messages"][0]["message"] == "Still working through the quantum module."
    assert body["messages"][0]["delivery"]["rendered_by"] == "proactive_autonomous"
    assert pcm.list_pending()[0]["id"] == rec["id"]


def test_deliver_ack_marks_delivered(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    rec = pcm.append_update(
        text="Progress update.",
        episode_id="ep-2",
        opened_by="proactive_update",
    )
    client = TestClient(create_app())
    ack = client.post(f"/api/chat/proactive/{rec['id']}/delivered")
    assert ack.status_code == 200
    assert ack.json()["ok"] is True
    assert pcm.list_pending() == []
    # idempotent ack
    again = client.post(f"/api/chat/proactive/{rec['id']}/delivered")
    assert again.status_code == 200


def test_deliver_does_not_persist_to_main_chat_history(tmp_path, monkeypatch):
    """Proactive notices have their own durable store, outside main chat history.

    Acking a proactive line delivered must mark it delivered (so the feed stops
    re-announcing it) but must NOT append it to the permanent HUD
    chat_history ring. Persisting it pinned the one-shot autonomous line to the top
    of the chat feed on every reload (operator finding 2026-06-30).
    """
    _isolate(tmp_path, monkeypatch)
    import hud.chat_history as hud_chat_history

    appended: list = []
    monkeypatch.setattr(hud_chat_history, "append_message", lambda raw: appended.append(raw) or raw)

    rec = pcm.append_update(
        text="Autonomous note.", episode_id="ep-hist", opened_by="proactive_update"
    )
    client = TestClient(create_app())
    ack = client.post(f"/api/chat/proactive/{rec['id']}/delivered")

    assert ack.status_code == 200
    assert pcm.list_pending() == []  # still flips to delivered
    assert appended == []  # but is NOT written to the permanent transcript ring


def test_deliver_unknown_returns_404(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    client = TestClient(create_app())
    r = client.post("/api/chat/proactive/no-such-id/delivered")
    assert r.status_code == 404


def test_poll_empty_returns_zero_count(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    client = TestClient(create_app())
    r = client.get("/api/chat/proactive")
    assert r.status_code == 200
    assert r.json() == {"messages": [], "count": 0}

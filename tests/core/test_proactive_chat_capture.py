"""Capture-on-close: a PROACTIVE_RESPONSE episode lands in the proactive chat store."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import cognition_chain as cl
import pressure_loop.proactive_chat_messages as pcm


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(pcm, "_store_path", lambda: tmp_path / "pcm.json")
    pcm._reset_cache_for_tests()


def _episode(*, response_text="", intent="PROACTIVE_RESPONSE", opened_by="proactive_update"):
    return SimpleNamespace(
        episode_id="ep-proactive-1",
        opened_by=opened_by,
        global_={
            "intent_decision": {"intent": intent},
            "outcome": {"artifacts": {"response_text": response_text}},
        },
        meta={"execution_action_pack": {"action": "PROACTIVE_RESPONSE"}},
        auto={"status": "completed", "outputs": {}},
        pressure={"contributors": [{"id": opened_by, "detail": {"message": "status"}}]},
    )


def test_proactive_response_records_chat_update(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ep = _episode(response_text="Finished the first learning pass on Python basics.")
    cl._record_proactive_chat(ep)

    pending = pcm.list_pending()
    assert len(pending) == 1
    assert "Python basics" in pending[0]["text"]
    assert pending[0]["episode_id"] == "ep-proactive-1"


def test_ask_operator_does_not_record_proactive_chat(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ep = _episode(
        response_text="What should I learn?", intent="ASK_OPERATOR", opened_by="operator_priority"
    )
    cl._record_proactive_chat(ep)
    assert pcm.list_pending() == []


def test_proactive_capture_is_failure_soft(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    def _boom(**_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(pcm, "append_update", _boom)
    ep = _episode(response_text="update")
    cl._record_proactive_chat(ep)  # must not raise


def test_append_update_is_idempotent_by_episode_id(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    first = pcm.append_update(
        text="First update",
        episode_id="ep-same",
        opened_by="proactive_update",
    )
    second = pcm.append_update(
        text="Duplicate should not append",
        episode_id="ep-same",
        opened_by="proactive_update",
    )
    assert first is not None and second is not None
    assert first["id"] == second["id"]
    assert len(pcm.list_pending()) == 1


def test_present_result_is_idempotent_by_result_id(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    first = pcm.append_update(
        text="First rendering",
        episode_id="ep-one",
        opened_by="proactive_update",
        kind="PRESENT_RESULT",
        payload={"result_id": "result-1", "application": [{"prompt": "x"}]},
    )
    second = pcm.append_update(
        text="Retry rendering",
        episode_id="ep-two",
        opened_by="proactive_update",
        kind="PRESENT_RESULT",
        payload={"result_id": "result-1", "application": [{"prompt": "x"}]},
    )
    assert first is not None and second is not None
    assert first["id"] == second["id"]
    assert first["kind"] == "PRESENT_RESULT"
    assert len(pcm.list_pending()) == 1


def test_delivered_records_remain_durable_until_day_zero(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ids = []
    for i in range(5):
        rec = pcm.append_update(
            text=f"msg-{i}",
            episode_id=f"ep-{i}",
            opened_by="proactive_update",
        )
        assert rec is not None
        ids.append(rec["id"])
        pcm.mark_delivered(rec["id"])

    assert [row["id"] for row in pcm.list_messages()] == ids
    assert [row["id"] for row in pcm.list_pinned()] == ids


def test_clear_unpins_notices_without_deleting_history(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    first = pcm.append_update(text="one", episode_id="ep-1", opened_by="proactive_update")
    second = pcm.append_update(text="two", episode_id="ep-2", opened_by="proactive_update")
    assert first is not None and second is not None
    pcm.mark_delivered(first["id"])

    assert pcm.clear_pinned_notices() == 2
    assert pcm.list_pinned() == []
    rows = pcm.list_messages()
    assert [row["status"] for row in rows] == [
        pcm.STATUS_CLEARED,
        pcm.STATUS_CLEARED,
    ]
    assert all(row.get("cleared_at") for row in rows)


def test_late_delivery_ack_cannot_resurrect_cleared_notice(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    rec = pcm.append_update(text="race", episode_id="ep-race", opened_by="proactive_update")
    assert rec is not None
    pcm.clear_pinned_notices()

    updated = pcm.mark_delivered(rec["id"])
    assert updated is not None
    assert updated["status"] == pcm.STATUS_CLEARED
    assert pcm.list_pinned() == []

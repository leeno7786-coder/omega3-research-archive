"""Operator delivery at the terminal boundary of an execution report."""

from types import SimpleNamespace

import pytest

import cognition_chain as cc
import pressure_loop.proactive_chat_messages as pcm


def test_operator_build_complete_notifies_operator(monkeypatch):
    calls = []
    monkeypatch.setattr(pcm, "append_update", lambda **kw: calls.append(kw))
    ep = SimpleNamespace(episode_id="ep1", opened_by="operator_command", pressure={})

    cc._emit_build_complete_to_operator(
        ep,
        "build a todo app",
        {"summary": "todo app with add/list/complete", "files_changed": ["todo/main.py"]},
    )

    assert len(calls) == 1
    assert calls[0]["text"].startswith("✅ Finished:")
    assert "todo app" in calls[0]["text"]
    assert "main.py" in calls[0]["text"]
    assert calls[0]["intent"] == "PROACTIVE_RESPONSE"


def test_autonomous_build_does_not_notify(monkeypatch):
    calls = []
    monkeypatch.setattr(pcm, "append_update", lambda **kw: calls.append(kw))
    ep = SimpleNamespace(episode_id="ep2", opened_by="curiosity_gap", pressure={})

    cc._emit_build_complete_to_operator(ep, "practice build", {"summary": "x"})

    assert calls == []


def test_operator_notice_failure_propagates(monkeypatch):
    def _boom(**_kw):
        raise RuntimeError("channel down")

    monkeypatch.setattr(pcm, "append_update", _boom)
    ep = SimpleNamespace(episode_id="ep3", opened_by="operator_command", pressure={})

    with pytest.raises(RuntimeError, match="channel down"):
        cc._emit_build_complete_to_operator(ep, "obj", {"summary": "s"})


def test_operator_build_stop_notifies_with_unresolved_work(monkeypatch):
    calls = []
    monkeypatch.setattr(pcm, "append_update", lambda **kw: calls.append(kw))
    ep = SimpleNamespace(episode_id="ep4", opened_by="operator_command", pressure={})

    cc._emit_build_stopped_to_operator(
        ep,
        "build csv_stats",
        "README missing",
        origin_opened_by="operator_command",
        ported_to="incomplete/csv_stats",
    )

    assert len(calls) == 1
    assert calls[0]["text"].startswith("⛔ Stopped:")
    assert "README missing" in calls[0]["text"]

"""Capture-on-close: an operator-opened RESPOND episode's answer is recorded to
the operator-chat-response store, keyed by the ORIGINATING operator cycle_id, so
the HUD can resolve the waiting 'thinking' bubble. Non-operator episodes and
action-only operator commands (no response_text) record nothing here.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import cognition_chain as cl
import pressure_loop.operator_chat_responses as ocr


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "_store_path", lambda: tmp_path / "ocr.json")
    ocr._reset_cache_for_tests()


def _episode(*, contributors, response_text="", intent="RESPOND", action="RESPONSE"):
    return SimpleNamespace(
        episode_id="ep-answer",
        opened_by=(contributors[0].get("id") if contributors else ""),
        global_={
            "intent_decision": {"intent": intent},
            "outcome": {"artifacts": {"response_text": response_text}},
        },
        meta={"execution_action_pack": {"action": action}},
        auto={
            "status": "completed",
            "outputs": {},
            "prompt_pack": {"prompt_type": "RESPONSE_QUESTION", "response_mode": "question"},
            "memory": {
                "memory_status": "success",
                "memory_scope": ["identity", "knowledge"],
                "items": [
                    {
                        "memory_id": "0.00.omega_creator",
                        "title": "Creator — Omega AI",
                        "content": "Built Omega 3.0 by hand.",
                    },
                ],
            },
        },
        pressure={"contributors": contributors},
    )


def test_operator_respond_records_chat_response_keyed_by_cycle_id(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ep = _episode(
        contributors=[{"id": "operator_query", "detail": {"cycle_id": "cyc-noah"}}],
        response_text="Hello Noah, good to meet you.",
    )
    cl._record_operator_outcome(MagicMock(), ep)

    rec = ocr.get_response("cyc-noah")
    assert rec is not None
    assert "Hello Noah" in rec["text"]
    assert rec["status"] == "pending"
    assert rec["prompt_pack"]["prompt_type"] == "RESPONSE_QUESTION"
    assert rec["thinking"]["memory"]["entries"][0]["label"] == "Creator — Omega AI"
    # keyed by the operator cycle_id, NOT the answering episode_id
    assert ocr.get_response("ep-answer") is None


def test_deliver_at_auto_records_image_artifact_without_response_text(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ep = _episode(
        contributors=[
            {
                "id": "operator_command",
                "detail": {"cycle_id": "cyc-image", "chat_bubble_waiting": True},
            }
        ],
        response_text="",
        intent="IMAGE_CREATE",
        action="IMAGE_CREATION",
    )
    ep.auto["outputs"] = {"image_artifact": {"artifact_id": "img-1", "path": "images/img-1.png"}}

    cl._deliver_chat_reply_at_auto(ep)

    rec = ocr.get_response("cyc-image")
    assert rec is not None
    assert rec["text"] == ""
    assert rec["image"]["artifact_id"] == "img-1"


def test_deliver_at_auto_persists_plugin_permission_hud_dict(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
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
    ep = _episode(
        contributors=[
            {
                "id": "operator_query",
                "detail": {"cycle_id": "cyc-perm", "chat_bubble_waiting": True},
            }
        ],
        response_text="",
    )
    ep.auto["outputs"] = {
        "plugin_permission_request": {
            **hud,
            "access_token": "ya29.secret",
            "plan": {"arguments": {"body": "secret-argument-value"}},
        }
    }
    ep.auto["failure_kind"] = "pending_input"

    cl._deliver_chat_reply_at_auto(ep)

    rec = ocr.get_response("cyc-perm")
    assert rec is not None
    assert rec["plugin_permission_request"] == hud
    assert "access_token" not in rec
    assert "ya29.secret" not in str(rec).lower()


def test_non_operator_episode_records_nothing(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ep = _episode(
        contributors=[{"id": "learning_debt", "detail": {}}],
        response_text="autonomous chatter",
    )
    cl._record_operator_outcome(MagicMock(), ep)
    assert ocr.list_pending() == []


def test_operator_command_without_response_text_records_nothing(tmp_path, monkeypatch):
    """An action-only operator command (e.g. LEARN) has no chat reply — nothing
    to deliver to the chat bubble."""
    _isolate(tmp_path, monkeypatch)
    ep = _episode(
        contributors=[{"id": "operator_command", "detail": {"cycle_id": "cyc-learn"}}],
        response_text="",
        intent="LEARN",
        action="USE_TOOL",
    )
    cl._record_operator_outcome(MagicMock(), ep)
    assert ocr.get_response("cyc-learn") is None


def test_resolve_query_answer_routes_to_proactive_feed(tmp_path, monkeypatch):
    """The operator ANSWERED a system query -> Omega's reply must reach the HUD via the PROACTIVE
    feed (the HUD never polls /api/chat/responses for a query answer, and the operator_response
    contributor carries no chat cycle_id). Previously it died silently in operator_chat_responses
    (operator finding 2026-06-27)."""
    import pressure_loop.proactive_chat_messages as pcm

    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(pcm, "_store_path", lambda: tmp_path / "pcm.json")
    monkeypatch.setattr(cl, "_mint_query_exchange_memory", lambda *a, **k: None)
    pcm._reset_cache_for_tests()

    ep = _episode(
        contributors=[
            {"id": "operator_response", "detail": {"message": "Flask", "system_query_id": "q1"}}
        ],
        response_text="Got it — I'll use Flask for the todo app.",
        intent="RESOLVE_QUERY",
        action="RESPONSE",
    )
    cl._record_operator_outcome(MagicMock(), ep)

    pending = pcm.list_pending()
    assert len(pending) == 1
    assert "Flask" in pending[0]["text"]  # reached the feed the HUD actually reads
    assert ocr.list_pending() == []  # NOT the cycle-keyed store the HUD won't poll for it


def test_capture_failure_is_recorded_in_the_episode(tmp_path, monkeypatch):
    """A store failure does not break episode close; it becomes episode content."""
    _isolate(tmp_path, monkeypatch)

    def _boom(**_k):
        raise RuntimeError("disk full")

    captured = []
    monkeypatch.setattr(ocr, "append_response", _boom)
    monkeypatch.setattr(
        "memory.cognition_capture.capture_phase_failure",
        lambda episode, **kw: captured.append((episode.episode_id, kw)),
    )
    ep = _episode(
        contributors=[{"id": "operator_query", "detail": {"cycle_id": "c"}}],
        response_text="answer",
    )
    cl._record_operator_outcome(MagicMock(), ep)  # must not raise
    assert captured == [(
        "ep-answer",
        {"phase": "auto", "event_kind": "chat.delivery_failed", "reason": "RuntimeError: disk full"},
    )]


def test_orphan_sweep_lists_only_aged_pending_rows(tmp_path, monkeypatch):
    """list_undelivered is the sweep rail for replies whose bubble poll never
    claimed them (live miss 2026-07-19: a proactive-query answer generated ok,
    sat pending forever, and never painted). Age floor keeps live turns on the
    bubble rail; delivered rows never re-sweep."""
    _isolate(tmp_path, monkeypatch)
    ocr.append_response(
        cycle_id="cyc-orphan", episode_id="ep-1", text="the reply that never painted"
    )
    # Fresh rows stay off the sweep (bubble rail's race to win) ...
    assert ocr.list_undelivered(min_age_s=60.0) == []
    # ... aged rows are swept ...
    rows = ocr.list_undelivered(min_age_s=0.0)
    assert [r["cycle_id"] for r in rows] == ["cyc-orphan"]
    # ... and a delivered row never re-sweeps (shows once).
    ocr.mark_delivered("cyc-orphan")
    assert ocr.list_undelivered(min_age_s=0.0) == []

"""Fix B (operator 2026-07-03): the chat reply delivers AT AUTO — the moment the
response text exists — and the rest of the cascade (Meta B, Global B, Omega
identity fold, DeepSleep) folds back BEHIND the already-delivered reply.
Previously the delivery write lived in the episode-close finally block, so the
operator waited on the reply inference PLUS the identity-reflection inference
("2 inferences before responding").

`_deliver_chat_reply_at_auto` runs inside `_start_auto` right after
`run_auto_front` finalizes ``auto.outputs.response_text``; it stamps
``episode.meta['chat_reply_delivered_cycle']`` so `_record_operator_outcome`
never posts a second bubble at close.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import cognition_chain as cl
import memory.episode_sram as sram
import pressure_loop.operator_chat_responses as ocr


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "_store_path", lambda: tmp_path / "ocr.json")
    ocr._reset_cache_for_tests()


def _episode(*, contributors, response_text="", intent="RESPOND", action="RESPONSE"):
    # Shape at AUTO time: outputs.response_text exists; global outcome does NOT yet.
    return SimpleNamespace(
        episode_id="ep-auto",
        pipeline_id=0,
        work_kind="operator_turn",
        opened_by=(contributors[0].get("id") if contributors else ""),
        global_={"intent_decision": {"intent": intent}},
        meta={"execution_action_pack": {"action": action}},
        auto={"status": "completed", "outputs": {"response_text": response_text}},
        pressure={"contributors": contributors},
    )


def test_deliver_at_auto_writes_response_and_marks_episode(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ep = _episode(
        contributors=[{"id": "operator_query", "detail": {"cycle_id": "cyc-noah"}}],
        response_text="Hello Noah, good to meet you.",
    )

    cl._deliver_chat_reply_at_auto(ep)

    rec = ocr.get_response("cyc-noah")
    assert rec is not None
    assert "Hello Noah" in rec["text"]
    # The marker _record_operator_outcome keys its skip on.
    assert ep.meta.get("chat_reply_delivered_cycle") == "cyc-noah"


def test_permission_continuation_delivers_to_the_original_chat_cycle(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ep = _episode(
        contributors=[
            {
                "id": "plugin_permission_decision",
                "detail": {
                    "request_id": "a" * 48,
                    "cycle_id": "plugin-permission:" + "a" * 48,
                    "task_id": "cyc-plugin-original",
                },
            }
        ],
        response_text='Plugin operation completed:\n{"id":"event-1"}',
        intent="PLUGIN_OPERATION",
        action="PLUGIN_OPERATION",
    )

    cl._deliver_chat_reply_at_auto(ep)

    record = ocr.get_response("cyc-plugin-original")
    assert record is not None
    assert "event-1" in record["text"]


def test_record_operator_outcome_skips_already_delivered_reply(tmp_path, monkeypatch):
    """Close-time capture must NOT post a second bubble for a reply already
    delivered at AUTO — but the brain-turn receipt still lands (the window
    reflection folds on it)."""
    _isolate(tmp_path, monkeypatch)
    ep = _episode(
        contributors=[{"id": "operator_query", "detail": {"cycle_id": "cyc-noah"}}],
        response_text="Hello Noah, good to meet you.",
    )
    ep.meta["chat_reply_delivered_cycle"] = "cyc-noah"
    store = MagicMock()

    cl._record_operator_outcome(store, ep)

    assert ocr.get_response("cyc-noah") is None  # no duplicate delivery
    assert store.append_brain_turn.called  # the fold-back receipt still writes


def test_record_operator_outcome_still_delivers_without_marker(tmp_path, monkeypatch):
    """Fail-mode direction: an episode that never passed the AUTO delivery seam
    (marker absent) keeps the close-time delivery — the safety net stays."""
    _isolate(tmp_path, monkeypatch)
    ep = _episode(
        contributors=[{"id": "operator_query", "detail": {"cycle_id": "cyc-net"}}],
        response_text="Answer via the close-time net.",
    )

    cl._record_operator_outcome(MagicMock(), ep)

    assert ocr.get_response("cyc-net") is not None


def test_deliver_at_auto_skips_non_operator_episode(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ep = _episode(
        contributors=[{"id": "learning_debt", "detail": {}}],
        response_text="autonomous chatter",
    )

    cl._deliver_chat_reply_at_auto(ep)

    assert ocr.list_pending() == []
    assert "chat_reply_delivered_cycle" not in ep.meta


def test_deliver_at_auto_skips_when_no_response_text(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ep = _episode(
        contributors=[{"id": "operator_command", "detail": {"cycle_id": "cyc-learn"}}],
        response_text="",
        intent="LEARN",
        action="USE_TOOL",
    )

    cl._deliver_chat_reply_at_auto(ep)

    assert ocr.get_response("cyc-learn") is None
    assert "chat_reply_delivered_cycle" not in ep.meta


def test_deliver_at_auto_skips_fast_reply_delivered(tmp_path, monkeypatch):
    """The fast-reply behind-episode already answered inline — AUTO delivery
    must honor operator_reply_delivered exactly like the close-time path."""
    _isolate(tmp_path, monkeypatch)
    ep = _episode(
        contributors=[
            {
                "id": "operator_query",
                "detail": {"cycle_id": "cyc-fast", "operator_reply_delivered": True},
            }
        ],
        response_text="the fuller behind-cascade answer",
    )

    cl._deliver_chat_reply_at_auto(ep)

    assert ocr.get_response("cyc-fast") is None


def test_deliver_at_auto_records_delivery_failure_and_continues(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)

    def _boom(**_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ocr, "append_response", _boom)
    ep = _episode(
        contributors=[{"id": "operator_query", "detail": {"cycle_id": "c"}}],
        response_text="answer",
    )
    sram.open_bucket(ep.episode_id, pipeline_id=ep.pipeline_id)

    cl._deliver_chat_reply_at_auto(ep)

    from memory.episode_events import read_episode_events

    failures = [event for event in read_episode_events(ep.episode_id)
                if event.event_kind == "chat.delivery_failed"]
    assert len(failures) == 1
    assert failures[0].payload["reason"] == "RuntimeError: disk full"
    assert "chat_reply_delivered_cycle" not in ep.meta


def test_start_auto_delivers_before_meta_b(tmp_path, monkeypatch):
    """Wiring: `_start_auto` itself delivers the reply — proof the operator's
    bubble resolves before Meta B / Global B / Omega ever run."""
    _isolate(tmp_path, monkeypatch)

    ep = _episode(
        contributors=[{"id": "operator_query", "detail": {"cycle_id": "cyc-wire"}}],
        response_text="",
    )

    def _fake_auto_front(episode):
        episode.auto["outputs"]["response_text"] = "delivered at AUTO"
        return object()

    monkeypatch.setattr("auto_loop.auto.run_auto_front", _fake_auto_front)
    monkeypatch.setattr("autonomic.symphony_neuromod.compute_episode_neuromod", lambda *a, **k: {})
    monkeypatch.setattr("autonomic.symphony_neuromod.set_current_memory_salience", lambda *_a: None)
    foldback_seen: list[bool] = []

    def _foldback(*_args, **_kwargs):
        assert ocr.get_response("cyc-wire") is not None
        foldback_seen.append(True)

    monkeypatch.setattr(cl, "_start_meta_b_global_b", _foldback)
    store = MagicMock()
    # WO-06: the typed population; an empty one yields the labelled 0.5 prior.
    from pressure_loop.episode import OutcomePopulation

    store.recent_outcome_population.return_value = OutcomePopulation()

    cl._start_auto(store, ep)

    rec = ocr.get_response("cyc-wire")
    assert rec is not None
    assert rec["text"] == "delivered at AUTO"
    assert ep.meta.get("chat_reply_delivered_cycle") == "cyc-wire"
    assert foldback_seen == [True]


def test_resolve_query_panel_answer_delivers_at_auto_via_proactive_feed(tmp_path, monkeypatch):
    """Operator directive 2026-07-11: RESOLVE_QUERY no longer waits for the fold.
    A panel answer (no chat bubble waiting) posts to the proactive feed the
    moment AUTO finalizes the reply, and stamps the episode so close-time never
    double-posts."""
    _isolate(tmp_path, monkeypatch)
    posted = []
    monkeypatch.setattr(
        "pressure_loop.proactive_chat_messages.append_update",
        lambda **kw: posted.append(kw),
    )
    ep = _episode(
        contributors=[{"id": "operator_response", "detail": {"cycle_id": "cyc-ans"}}],
        response_text="Great — undergraduate level it is.",
        intent="RESOLVE_QUERY",
    )

    cl._deliver_chat_reply_at_auto(ep)

    assert len(posted) == 1
    assert posted[0]["intent"] == "RESOLVE_QUERY"
    assert "undergraduate" in posted[0]["text"]
    assert ep.meta.get("resolve_reply_delivered") is True


def test_resolve_query_panel_reply_joins_episode_transcript_once(tmp_path, monkeypatch):
    """Pinned-chat egress is the same conversational turn as main-chat egress."""
    _isolate(tmp_path, monkeypatch)
    sram.reset()
    sram.open_bucket("ep-auto", pipeline_id=0)
    sram.hold_turn("user", "Undergraduate level, please.")
    posted = []
    monkeypatch.setattr(
        "pressure_loop.proactive_chat_messages.append_update",
        lambda **kw: posted.append(kw),
    )
    ep = _episode(
        contributors=[{"id": "operator_response", "detail": {"cycle_id": "cyc-panel"}}],
        response_text="Great — undergraduate level it is.",
        intent="RESOLVE_QUERY",
    )

    cl._deliver_chat_reply_at_auto(ep)
    cl._deliver_chat_reply_at_auto(ep)

    assert len(posted) == 1
    assert [(item.role, item.text) for item in sram.pending() if item.kind == "turn"] == [
        ("user", "Undergraduate level, please."),
        ("assistant", "Great — undergraduate level it is."),
    ]


def test_resolve_query_chat_box_answer_resolves_waiting_bubble_at_auto(tmp_path, monkeypatch):
    """A chat-box answer HAS a waiting bubble — resolve it at AUTO like any chat."""
    _isolate(tmp_path, monkeypatch)
    ep = _episode(
        contributors=[
            {
                "id": "operator_response",
                "detail": {"cycle_id": "cyc-chatans", "chat_bubble_waiting": True},
            }
        ],
        response_text="Noted — probability first.",
        intent="RESOLVE_QUERY",
    )

    cl._deliver_chat_reply_at_auto(ep)

    rec = ocr.get_response("cyc-chatans")
    assert rec is not None and "probability" in rec["text"]
    assert ep.meta.get("chat_reply_delivered_cycle") == "cyc-chatans"


def test_deliver_at_auto_resolves_the_bubble_with_the_failure(tmp_path, monkeypatch):
    """Live 2026-09-05: two "build" turns died in motor planning with no
    response text, so nothing resolved the bubble and the HUD spun for seven
    minutes before printing a timeout note. A failed AUTO now answers the
    waiting turn with its failure — resolved and honest, never eaten."""
    _isolate(tmp_path, monkeypatch)
    ep = _episode(
        contributors=[
            {
                "id": "operator_command",
                "detail": {"cycle_id": "cyc-fail", "chat_bubble_waiting": True},
            }
        ],
    )
    ep.auto = {
        "status": "failed",
        "failure_kind": "motor_planning",
        "failure_reason": "motor planning failed after 2 attempts: the seat returned NO OUTPUT",
        "outputs": {},
    }

    cl._deliver_chat_reply_at_auto(ep)

    rec = ocr.get_response("cyc-fail")
    assert rec is not None
    assert rec["text"] == (
        "I could not complete that: motor planning failed after 2 attempts: "
        "the seat returned NO OUTPUT"
    )
    assert rec["delivery"]["failed"] is True
    assert ep.meta.get("chat_reply_delivered_cycle") == "cyc-fail"


def test_deliver_at_auto_failure_without_an_operator_turn_delivers_nothing(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ep = _episode(contributors=[{"id": "learning_debt", "detail": {}}])
    ep.auto = {
        "status": "failed",
        "failure_kind": "motor_planning",
        "failure_reason": "x",
        "outputs": {},
    }

    cl._deliver_chat_reply_at_auto(ep)

    assert ocr.list_pending() == []

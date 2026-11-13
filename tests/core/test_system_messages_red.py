"""The text app — system queries to the operator + attached responses. RED-first.

Operator spec 2026-06-12 (infancy-stage proactive speech for initial
release): a minor system that is BOTH the queue for the mind's outbound
queries AND the mechanism for attaching operator responses to them. Lives
behind a badge icon in the HUD; the operator can respond / ignore / delete.

- A winning proactive episode's generated text lands here at episode close.
- RESPOND feeds back: the reply rides the operator input queue box as an
  operator_response contributor (-> RESOLVE_QUERY) carrying the thread.
- IGNORE feeds back NOTHING (the mind is never told — consistent with the
  one-shot ask philosophy). DELETE removes the record.

Each test asserts the configured VALUE and the FAIL-MODE, both directions.
"""

from __future__ import annotations

from pressure_loop import cognition_queue as cq

import json
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    import pressure_loop.proactive_lifecycle as lifecycle
    import pressure_loop.system_messages as sm

    monkeypatch.setattr(sm, "_store_path", lambda: tmp_path / "system_messages.json")
    monkeypatch.setattr(lifecycle, "_ledger_path", lambda: tmp_path / "proactive_lifecycle.jsonl")
    sm._reset_cache_for_tests()
    lifecycle._reset_cache_for_tests()
    yield
    lifecycle._reset_cache_for_tests()


def _proactive_episode(
    *,
    intent: str = "ASK_OPERATOR",
    opened_by: str = "operator_priority",
    response_text: str | None = "Operator — what should I prioritize learning?",
    seed_question: str = "What should I focus on?",
):
    outputs = {}
    if response_text is not None:
        outputs["response_text"] = response_text
    # The LIVE episode serialization shape (cognition_chain.py:230):
    # {"id": c.type, "weight": c.weight, "detail": dict(c.metadata)} — there
    # is NO "metadata" key on the live path. This fixture pins that contract:
    # if the serialization ever renames "detail", seed-question extraction
    # breaks THIS TEST loudly instead of silently blanking seed_question.
    contributors = [
        {
            "id": opened_by,
            "weight": 0.9,
            "detail": {"message": seed_question, "question": seed_question},
        }
    ]
    return SimpleNamespace(
        episode_id="ep-textapp-1",
        opened_by=opened_by,
        global_={
            "intent_decision": {
                "episode_id": "ep-textapp-1",
                "intent": intent,
                "confidence": 0.9,
                "reason": "test",
            },
            "outcome": {"artifacts": {}},
        },
        auto={"outputs": outputs},
        meta={},
        pressure={"contributors": contributors},
    )


# --- the store: append, list, badge count, persistence --------------------------


def test_append_list_and_badge_count():
    from pressure_loop.system_messages import (
        append_system_query,
        list_messages,
        unread_count,
    )

    rec = append_system_query(
        text="Operator — what should I prioritize learning?",
        episode_id="ep-1",
        opened_by="operator_priority",
        seed_question="What should I focus on?",
    )
    assert rec["id"]
    assert rec["status"] == "pending"
    assert rec["text"] == "Operator — what should I prioritize learning?"
    assert rec["seed_question"] == "What should I focus on?"
    assert rec["proactive_thread_id"]

    msgs = list_messages()
    assert len(msgs) == 1
    assert unread_count() == 1


def test_store_persists_across_reload():
    import pressure_loop.system_messages as sm
    from pressure_loop.system_messages import (
        append_system_query,
        list_messages,
        unread_count,
    )

    append_system_query(
        text="Do you want me to keep exploring biology?",
        episode_id="ep-2",
        opened_by="operator_priority",
    )
    path = sm._store_path()
    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert len(on_disk) == 1

    # Simulate restart: cache dropped, store reloads from disk.
    sm._reset_cache_for_tests()
    assert len(list_messages()) == 1
    assert unread_count() == 1


def test_pre_lifecycle_pending_question_can_be_answered_and_upgraded(tmp_path):
    """Questions persisted before the lifecycle ledger existed remain answerable."""
    import pressure_loop.proactive_lifecycle as lifecycle
    import pressure_loop.system_messages as sm

    legacy = {
        "id": "legacy-question",
        "ts": "2026-07-10T00:00:00+00:00",
        "episode_id": "ep-legacy-ask",
        "opened_by": "ask_operator",
        "text": "Continue foundational work or pivot?",
        "seed_question": "Choose the next curriculum step.",
        "status": sm.STATUS_PENDING,
        "response": None,
    }
    sm._store_path().write_text(json.dumps([legacy]), encoding="utf-8")
    sm._reset_cache_for_tests()

    updated = sm.respond_to_message("legacy-question", "Continue scenario practice.")

    assert updated is not None
    assert updated["status"] == sm.STATUS_RESPONDED
    assert updated["proactive_thread_id"]
    assert updated["proactive_policy_id"]
    policy = lifecycle.active_policy(thread_id=updated["proactive_thread_id"])
    assert policy is not None
    assert policy.operator_text == "Continue scenario practice."


def test_question_is_still_persisted_when_lifecycle_ask_recording_fails(monkeypatch):
    """Operational-ledger failure must not drop the operator-facing question."""
    import pressure_loop.proactive_lifecycle as lifecycle
    import pressure_loop.system_messages as sm

    monkeypatch.setattr(
        lifecycle,
        "record_ask",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("ledger unavailable")),
    )

    rec = sm.append_system_query(
        text="Which topic should I practice next?",
        episode_id="ep-ledger-down",
        opened_by="ask_operator",
    )

    assert rec["status"] == sm.STATUS_PENDING
    assert not rec.get("proactive_thread_id")
    assert sm.list_messages()[0]["id"] == rec["id"]


# --- close hook: winning proactive speech lands in the app ----------------------


def test_proactive_episode_lands_in_text_app():
    from cognition_chain import _record_system_message
    from pressure_loop.system_messages import list_messages

    _record_system_message(_proactive_episode())

    msgs = list_messages()
    assert len(msgs) == 1
    rec = msgs[0]
    assert rec["text"] == "Operator — what should I prioritize learning?"
    assert rec["episode_id"] == "ep-textapp-1"
    assert rec["opened_by"] == "operator_priority"
    assert rec["seed_question"] == "What should I focus on?"
    assert rec["status"] == "pending"


def test_non_ask_or_textless_never_lands():
    from cognition_chain import _record_system_message
    from pressure_loop.system_messages import list_messages

    # An operator-initiated RESPOND episode delivered synchronously already.
    _record_system_message(_proactive_episode(intent="RESPOND", opened_by="operator_query"))
    # CLEAN ROUTE SEPARATION (operator directive 2026-06-12): the text app
    # is the ASK_OPERATOR artifact's channel — a PROACTIVE_RESPONSE statement
    # episode must NOT land here even with text.
    _record_system_message(
        _proactive_episode(intent="PROACTIVE_RESPONSE", opened_by="proactive_update")
    )
    # An ask episode that produced no text has nothing to say (the question is authored by a
    # real ASK_OPERATOR episode — ignited via the clarification_gap pressure lever — not by the
    # episode that merely DECIDED to ask).
    _record_system_message(_proactive_episode(response_text=None))

    assert list_messages() == []


# --- respond: attach + feed back through the system ------------------------------


def test_respond_attaches_and_feeds_back_through_input_queue():
    from pressure_loop.cognition_queue import peek_inputs
    from pressure_loop.pressure import evaluate
    from pressure_loop.system_messages import (
        append_system_query,
        respond_to_message,
        unread_count,
    )

    rec = append_system_query(
        text="Operator — what should I prioritize learning?",
        episode_id="ep-3",
        opened_by="operator_priority",
        seed_question="What should I focus on?",
    )

    updated = respond_to_message(rec["id"], "Focus on quantum computing basics.")
    assert updated is not None
    assert updated["status"] == "responded"
    assert updated["response"]["text"] == "Focus on quantum computing basics."
    assert updated["response"]["ts"]
    assert updated["proactive_policy_id"]
    assert updated["proactive_thread_id"] == rec["proactive_thread_id"]
    assert unread_count() == 0

    # The reply rides the input queue box as operator_response with the
    # thread attached — Q and A travel together into the episode.
    queued = peek_inputs()
    assert len(queued) == 1
    reply = queued[0]
    assert reply.source == "operator"
    assert reply.type == "operator_response"
    assert reply.metadata.get("message") == "Focus on quantum computing basics."
    assert reply.metadata.get("system_query_id") == rec["id"]
    assert reply.metadata.get("proactive_thread_id") == rec["proactive_thread_id"]
    assert reply.metadata.get("proactive_policy_id") == updated["proactive_policy_id"]
    assert (
        reply.metadata.get("system_query_text") == "Operator — what should I prioritize learning?"
    )
    from pressure_loop.proactive_lifecycle import active_policy

    policy = active_policy(thread_id=rec["proactive_thread_id"])
    assert policy is not None
    assert policy.operator_text == "Focus on quantum computing basics."
    assert policy.origin_prompt_id == rec["id"]

    # And the reply takes its own pressure cycle (queue box one-per-cycle).
    ignition = evaluate([cq.lease_next_input(0)], active_episode=False, neuromod={})
    assert ignition is not None
    assert ignition.opened_by == "operator_response"

    # Fail-mode: unknown id attaches nothing, feeds back nothing.
    assert respond_to_message("no-such-id", "hello?") is None


def test_responded_message_routes_to_resolve_query():
    from global_loop.intent.run import run as global_a
    from pressure_loop.pressure import evaluate
    from pressure_loop.system_messages import (
        append_system_query,
        respond_to_message,
    )

    rec = append_system_query(
        text="Should I keep going?",
        episode_id="ep-4",
        opened_by="operator_priority",
    )
    respond_to_message(rec["id"], "Yes — keep going.")

    ignition = evaluate([cq.lease_next_input(0)], active_episode=False, neuromod={})
    assert ignition is not None
    # The existing rail: operator_response -> RESOLVE_QUERY.
    assert global_a(ignition) == "RESOLVE_QUERY"


# --- ignore / delete -------------------------------------------------------------


def test_ignore_is_removed_two_action_baseline():
    """Operator decision 2026-06-12: the clean baseline is RESPOND (signal
    back, closes the question) and DELETE (pressure-loss logic — nonexistent
    without affecting the system). There is NO ignore action."""
    import pressure_loop.system_messages as sm

    assert not hasattr(sm, "ignore_message")
    assert not hasattr(sm, "STATUS_IGNORED")


def test_append_syncs_open_questions_journal(tmp_path, monkeypatch):
    import pressure_loop.open_questions as oq
    from pressure_loop.system_messages import append_system_query, current_outreach

    path = tmp_path / "open_questions.jsonl"
    monkeypatch.setattr(oq, "OPEN_QUESTIONS_PATH", path)

    rec = append_system_query(
        text="Operator — what should I prioritize?",
        episode_id="ep-journal-1",
        opened_by="operator_priority",
        seed_question="What should I focus on?",
    )
    assert rec.get("open_question_id")

    outreach = current_outreach()
    assert outreach is not None
    assert outreach["question_text"] == rec["text"]
    assert outreach["outreach_id"] == rec["id"]

    rows = oq.list_open()
    assert len(rows) == 1
    assert rows[0].question_text == rec["text"]
    assert rows[0].curiosity_id == "system_query:ep-journal-1"


def test_respond_marks_journal_answered(tmp_path, monkeypatch):
    import pressure_loop.open_questions as oq
    from pressure_loop.system_messages import (
        append_system_query,
        respond_to_message,
    )

    path = tmp_path / "open_questions.jsonl"
    monkeypatch.setattr(oq, "OPEN_QUESTIONS_PATH", path)

    rec = append_system_query(
        text="What domain next?",
        episode_id="ep-journal-2",
        opened_by="operator_priority",
    )
    qid = rec["open_question_id"]
    assert qid

    updated = respond_to_message(rec["id"], "Quantum computing basics.")
    assert updated is not None
    assert updated["status"] == "responded"

    row = oq.get(qid)
    assert row is not None
    assert row.status == oq.STATUS_RESOLVED


def test_only_explicit_reset_clears_durable_system_message_history():
    import pressure_loop.system_messages as sm
    from pressure_loop.system_messages import (
        append_system_query,
        clear_messages,
        list_messages,
    )

    rec = append_system_query(
        text="May I rearrange my open questions?",
        episode_id="ep-6",
        opened_by="operator_priority",
    )
    assert not hasattr(sm, "delete_message")
    assert [row["id"] for row in list_messages()] == [rec["id"]]

    clear_messages()
    assert list_messages() == []
    assert json.loads(sm._store_path().read_text(encoding="utf-8")) == []


def test_compaction_carries_the_operator_thread():
    """The respond signal must REACH Omega's prompt: compaction keeps the
    question + answer thread for operator_response episodes (the window body
    carries full metadata, but the prompt sees only the compact form)."""
    from pressure_loop.episode_journal import compact_episode_for_reflection

    body = {
        "episode_id": "ep-thread-1",
        "intent": "RESOLVE_QUERY",
        "action": "RESPONSE",
        "outcome": {"status": "completed"},
        "cascade_status": "completed",
        "pressure": {
            "opened_by": "operator_response",
            "aggregate_pressure": 1.0,
            "contributors": [
                {
                    "id": "operator_response",
                    "weight": 1.0,
                    "detail": {
                        "message": "Focus on quantum computing basics.",
                        "system_query_id": "q-123",
                        "system_query_text": "Operator — what should I prioritize?",
                    },
                }
            ],
        },
        "execution": {},
        "brain_turns": [],
    }
    compact = compact_episode_for_reflection(body)
    thread = compact.get("operator_thread")
    assert thread is not None, "the Q&A thread must survive compaction"
    assert thread["question"] == "Operator — what should I prioritize?"
    assert thread["answer"] == "Focus on quantum computing basics."
    assert thread["system_query_id"] == "q-123"

    # Other direction: a non-thread episode emits NO thread block.
    plain = compact_episode_for_reflection(
        {
            "episode_id": "ep-plain",
            "intent": "LEARN",
            "action": "USE_TOOL",
            "outcome": {},
            "pressure": {"opened_by": "learning_debt", "contributors": []},
            "execution": {},
        }
    )
    assert "operator_thread" not in plain

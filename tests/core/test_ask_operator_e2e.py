"""The operator's answer to a pending system question, end to end.

RETURN — the operator responds in the questions panel → the reply rides the input
queue as operator_response with the thread → wins its own cycle → a RESOLVE_QUERY
episode answers over it → the record closes out → and the Q&A thread survives window
compaction so Omega READS the operator's answer in its next reflection.

How a question is raised is Omega's ASK_OPERATOR control decision (realignment Tasks 3,
8 and 12). Everything below the reply's ignition runs the REAL cascade
(cognition_chain.ignite_episode); only the runtime generate call is stubbed.
"""

from __future__ import annotations

import json

import pytest

import cognition_chain as cl
from pressure_loop.episode import EpisodeStore
from pressure_loop.pressure import evaluate
from tests.helpers.pipeline_ownership import bind_ignition

SEED_QUESTION = "What should I prioritize learning for you?"
ANSWER_TEXT = "Focus on quantum computing basics first."
ACK_TEXT = "Understood — I will prioritize quantum computing basics."


@pytest.fixture()
def _cascade(monkeypatch):
    """Real cascade, stubbed model: records every (operation, system, prompt) it saw."""
    seen: list[dict] = []

    def _fake_generate(**kwargs):
        operation = str(kwargs.get("operation") or "")
        seen.append(
            {
                "operation": operation,
                "system": str(kwargs.get("system") or ""),
                "prompt": str(kwargs.get("prompt") or ""),
            }
        )
        if operation != "episode_cognition":
            return {"text": ACK_TEXT}
        assessment = dict.fromkeys(
            ("who", "what", "when", "where", "why", "how", "desired_state", "observed_state"),
            "the operator's explicit priority",
        )
        return {
            "text": json.dumps(
                {
                    "control": {
                        "assessment": {**assessment, "gap": "", "evidence": ["operator answer received"]},
                        "decision": "DONE",
                        "pressure": None,
                        "synthesis": "I received the operator's explicit priority.",
                    },
                    "mastery": {
                        "knowledge_mastery": 0.0,
                        "application_mastery": 0.0,
                        "rationale": "The exchange did not establish domain mastery.",
                    },
                    "identity_patterns": [],
                }
            )
        }

    monkeypatch.setattr("runtime.socket.invoke_runtime_generate", _fake_generate)
    monkeypatch.setattr("pressure_loop.pressure_field.build_registry", lambda: {})
    return seen


def test_operator_answer_to_a_pending_question_resolves_and_survives_compaction(_cascade):
    from pressure_loop import pressure as pressure_mod
    from pressure_loop.cognition_queue import lease_next_input
    from pressure_loop.episode_journal import (
        compact_episode_for_reflection,
        enriched_episode_from_episode,
    )
    from pressure_loop.system_messages import (
        append_system_query,
        list_messages,
        respond_to_message,
        unread_count,
    )

    pressure_mod.clear_pending_contributors()
    pressure_mod._RESEED_STAGING.clear()
    store = EpisodeStore()
    seen = _cascade

    # A question Omega asked sits pending in the questions panel.
    append_system_query(
        text=SEED_QUESTION,
        episode_id="ep-ask",
        opened_by="omega_successor",
        seed_question=SEED_QUESTION,
    )
    msgs = list_messages()
    assert len(msgs) == 1
    assert msgs[0]["status"] == "pending"
    assert unread_count() == 1

    # The operator responds in the panel.
    updated = respond_to_message(msgs[0]["id"], ANSWER_TEXT)
    assert updated is not None
    assert updated["status"] == "responded"
    assert unread_count() == 0

    # The reply takes its own cycle from the durable FIFO.
    reply = lease_next_input(0)
    assert reply is not None
    ignition = evaluate([reply], active_episode=False, neuromod={})
    assert ignition is not None
    assert ignition.opened_by == "operator_response"

    # REAL cascade: RESOLVE_QUERY answers over the thread.
    episode = cl.ignite_episode(store, bind_ignition(ignition, arrival_seq=2))
    assert episode is not None
    assert (episode.global_ or {}).get("intent_decision", {}).get("intent") == "RESOLVE_QUERY"
    pack = (episode.meta or {}).get("execution_action_pack", {})
    assert pack.get("action") == "RESPONSE"
    ctx = (pack.get("execution_inputs") or {}).get("resolution_context") or {}
    assert ctx.get("system_query_id") == msgs[0]["id"]
    assert ctx.get("system_query_text") == SEED_QUESTION
    assert ctx.get("message") == ANSWER_TEXT

    # Omega folds the resolution exactly once and sees the operator's answer.
    resolve_calls = [s for s in seen if s["operation"] == "episode_cognition"]
    assert len(resolve_calls) == 1
    assert ANSWER_TEXT in resolve_calls[0]["prompt"]

    # The Q&A thread survives window compaction.
    compact = compact_episode_for_reflection(enriched_episode_from_episode(episode))
    thread = compact.get("operator_thread")
    assert thread is not None
    assert thread["system_query_id"] == msgs[0]["id"]
    assert thread["question"] == SEED_QUESTION
    assert thread["answer"] == ANSWER_TEXT

    # The RESOLVE episode never re-lands in the text app.
    final = list_messages()
    assert len(final) == 1
    assert final[0]["status"] == "responded"
    assert final[0]["response"]["text"] == ANSWER_TEXT

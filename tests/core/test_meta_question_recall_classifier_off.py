"""Recall-skip root-cause regression: a question-shaped RESPOND turn grounds recall
even with NO chat_intent (the classifier returned no label — runtime unreachable or the
flag explicitly disabled; the deterministic floor must still fire)."""

from __future__ import annotations

from meta_loop._meta_a_state import MetaAState
from meta_loop.memory.run import run


def _state(text: str, meta: dict | None = None) -> MetaAState:
    return MetaAState(
        mi=None,
        episode_id="ep-q-off",
        intent="RESPOND",
        action="RESPONSE",
        source={},
        text=text,
        target_str=text,
        constraints={},
        meta=dict(meta or {}),
    )


def test_question_without_chat_intent_grounds_recall():
    out = run(_state("who is your creator?"))  # no chat_intent in meta
    assert out.memory_required is True
    tools = [t.get("tool") for t in out.memory_tools if isinstance(t, dict)]
    assert tools == ["remember_identity"]


def test_question_without_chat_intent_routes_one_owner_lane():
    # The deterministic floor uses the same single-owner MRIL route.
    out = run(_state("how does the recall pipeline work?"))  # no chat_intent
    tools = [t.get("tool") for t in out.memory_tools if isinstance(t, dict)]
    assert tools == ["remember_workflow"]


def test_recall_imperative_hits_the_floor_without_classifier():
    """Live 2026-07-19: the classify call flaked (classification=None ok=False)
    and 'Tell me everything you know about Noah' — imperative, no '?' — missed
    the question-shape floor entirely: memory_required stayed False and Omega
    answered 'I have no information regarding an individual named Noah' with
    the persona lobe sitting right there."""
    out = run(_state("Tell me everything you know about Noah"))
    assert out.memory_required is True
    tools = [t.get("tool") for t in out.memory_tools if isinstance(t, dict)]
    assert tools == ["remember_persona"]


def test_plain_statement_without_chat_intent_stays_unshaped():
    # A non-question chit-chat statement must NOT be forced into question recall.
    out = run(_state("on it, thanks"))
    assert out.memory_required is False


def test_classifier_question_label_still_wins():
    # Explicit chat_intent="question" path is unchanged.
    out = run(_state("anything", {"chat_intent": "question"}))
    assert out.memory_required is True
    tools = [t.get("tool") for t in out.memory_tools if isinstance(t, dict)]
    assert "remember_knowledge" in tools

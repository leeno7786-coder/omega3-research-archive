"""Deterministic chat-intent classifier -> RESPONSE reply-pack selection."""

from __future__ import annotations

import auto_loop.promptpack.intent_classifier as ic
from auto_loop.promptpack.run import build_prompt_pack
from auto_loop.recall.run import memory_envelope_before_execute


def _pack_for(message: str, monkeypatch, intent, *, memory_required: bool = False):
    # The intent is classified ONCE at the chat ingress and threaded onto the operator
    # contributor's metadata (chat_intent); AUTO reads it there — it does NOT re-classify.
    execution_pack = {
        "episode_id": "ep-ci",
        "action": "RESPONSE",
        "memory_required": memory_required,
        "execution_intent": "plan for global intent RESPOND",
        "execution_inputs": {},
        "source_action_goal": {"intent": "RESPOND"},
    }
    env = memory_envelope_before_execute(execution_pack)
    meta = {"message": message}
    if intent:
        meta["chat_intent"] = intent
    return build_prompt_pack(
        execution_pack,
        env,
        pressure={
            "contributors": [
                {
                    "source": "operator",
                    "type": "operator_query",
                    "weight": 1.0,
                    "metadata": meta,
                }
            ]
        },
    )


def test_classified_intent_picks_pack(monkeypatch):
    cases = {
        # a bare go-ahead is conversation, not a canned receipt (2026-09-05)
        "ack": ("RESPONSE_FREEFORM", "freeform"),
        "question": ("RESPONSE_QUESTION", "question"),
        "freeform": ("RESPONSE_FREEFORM", "freeform"),
        "reasoning": ("RESPONSE_REASONING", "reasoning"),
    }
    for intent, (pt, rm) in cases.items():
        pack = _pack_for("build an ai system", monkeypatch, intent)
        assert pack.prompt_type == pt, intent
        assert pack.response_mode == rm, intent


def test_classifier_off_falls_back_to_deterministic(monkeypatch):
    # None (flag off / no model) -> the existing heuristics decide.
    assert (
        _pack_for("what is the capital of france?", monkeypatch, None).prompt_type
        == "RESPONSE_QUESTION"
    )
    assert (
        _pack_for("tell me about quantum tunneling", monkeypatch, None).prompt_type
        == "RESPONSE_REASONING"
    )


def test_temporal_query_overrides_classifier_and_question_shape(monkeypatch):
    # A date-anchored recall routes to RESPONSE_TEMPORAL DETERMINISTICALLY — even when the classifier
    # is stale/fuzzy and returns "question", and even though the query is question-shaped. (This is
    # what makes temporal work before the retrained classifier model is reloaded.)
    assert (
        _pack_for("what did we build yesterday", monkeypatch, "question").prompt_type
        == "RESPONSE_TEMPORAL"
    )
    assert (
        _pack_for("recap what we shipped last week", monkeypatch, None).prompt_type
        == "RESPONSE_TEMPORAL"
    )
    # the classifier `temporal` label still routes there for temporal intent the parser misses
    # (no explicit date phrase, so the deterministic detector defers to the classifier)
    assert (
        _pack_for("what have we been up to", monkeypatch, "temporal").prompt_type
        == "RESPONSE_TEMPORAL"
    )
    # a bare date with NO recall intent must not misroute — the classifier/heuristics still decide
    assert (
        _pack_for("schedule a meeting monday", monkeypatch, "ack").prompt_type
        == "RESPONSE_FREEFORM"
    )


def test_classifier_intent_wins_over_memory_required(monkeypatch):
    # Intent is the PRIMARY signal now: a classified intent keeps its shape even when
    # memory was recalled, so an ack-with-memory keeps the conversational shape the
    # ack label maps to (RESPONSE_FREEFORM since 2026-09-05: the chat brain reads the
    # go-ahead with the recent turns and acts with tools on demand) instead of being
    # reshaped to reasoning. The Memory Response Law is honored inside each shape's
    # prompt; memory_required only attaches the remembered-knowledge block, it no
    # longer overrides the intent.
    ack = _pack_for(
        "based on the spec I gave you, build it", monkeypatch, "ack", memory_required=True
    )
    assert ack.prompt_type == "RESPONSE_FREEFORM"
    q = _pack_for("what did I say the timeout was?", monkeypatch, "question", memory_required=True)
    assert q.prompt_type == "RESPONSE_QUESTION"


def test_deterministic_fallback_covers_chat_speech_acts_without_runtime():
    cases = {
        "yes": "ack",
        "What is Python": "question",
        "Work through why the retry loop fails under load": "reasoning",
        "recap what we shipped last week": "temporal",
        "I prefer concise answers.": "freeform",
        "what's on my calendar tomorrow?": "command",
    }
    for text, expected in cases.items():
        assert ic.deterministic_chat_intent(text) == expected, text

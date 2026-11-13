"""Per-intent recall grounding — STEP 3 (reasoning).

REASONING (RESPONSE_REASONING) is a chain-of-thought reply that helps the operator reason OVER
information. Unlike freeform/question it is NOT a "be Omega" turn — it pulls NO identity or persona,
only KNOWLEDGE + CODING keyed to the prompt so the chain of thought works over real remembered facts
and code.
"""

from __future__ import annotations

import pytest

from meta_loop._meta_a_state import MetaAState
from meta_loop.memory.run import run


def _state(text: str, meta: dict) -> MetaAState:
    return MetaAState(
        mi=None,
        episode_id="ep-reasoning",
        intent="RESPOND",
        action="RESPONSE",
        source={},
        text=text,
        target_str=text,
        constraints={},
        meta=dict(meta),
    )


def test_reasoning_recalls_knowledge_and_coding():
    out = run(
        _state(
            "work through why the asyncio retry loop deadlocks under load",
            {"chat_intent": "reasoning"},
        )
    )
    assert out.memory_required is True
    tools = [t.get("tool") for t in (out.memory_tools or ()) if isinstance(t, dict)]
    assert "remember_knowledge" in tools
    assert "remember_code" in tools


def test_reasoning_pulls_no_identity_or_persona():
    # Reasoning is a CoT helper, NOT a "be Omega" turn — so unlike freeform/question it must NOT
    # recall identity or persona (no dynamic personality directive is composed for reasoning).
    out = run(
        _state(
            "reason over the tradeoffs between a queue and a semaphore here",
            {"chat_intent": "reasoning"},
        )
    )
    tools = [t.get("tool") for t in (out.memory_tools or ()) if isinstance(t, dict)]
    assert "remember_identity" not in tools
    assert "remember_persona" not in tools
    # and it stays the two lanes — no episodes / reasoning-lobe folded in
    assert "remember_situation" not in tools
    assert "remember_reasoning" not in tools


def test_reasoning_lanes_keyed_to_the_prompt_text():
    q = "compare the memory cost of memoizing vs recomputing the embedding"
    out = run(_state(q, {"chat_intent": "reasoning"}))
    by_tool = {t["tool"]: t for t in (out.memory_tools or ()) if isinstance(t, dict)}
    assert by_tool["remember_knowledge"]["query"] == q
    assert by_tool["remember_code"]["query"] == q


def test_reasoning_is_exactly_two_lanes():
    out = run(
        _state(
            "think step by step about the cache invalidation order", {"chat_intent": "reasoning"}
        )
    )
    tools = sorted(t.get("tool") for t in (out.memory_tools or ()) if isinstance(t, dict))
    assert tools == ["remember_code", "remember_knowledge"]


@pytest.mark.parametrize("chat_intent", ["reasoning", "freeform"])
def test_classified_prior_reference_recalls_conversation_before_shape(chat_intent):
    out = run(
        _state(
            "Reason over what I told you previously about the retry budget",
            {"chat_intent": chat_intent},
        )
    )

    assert "operator_history" in out.memory_scope
    assert "conversation" in out.memory_scope
    assert out.memory_reason == "Question references prior operator conversation"

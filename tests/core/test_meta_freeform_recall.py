"""Per-intent recall grounding — STEP 1 (freeform).

Freeform Omega chat must be grounded by Omega's own identity/persona, plus user-facing knowledge,
coding, and saved temporal episodes so casual operator conversation still has the full Omega
context. It must not recall situational or journal/deepsleep mind-process lobes for user-facing
chat. The recall decision keys on the chat-intent, which already rides on
``state.meta["chat_intent"]`` (action/run.py threads the contributor metadata).
"""

from __future__ import annotations

from meta_loop._meta_a_state import MetaAState
from meta_loop.memory.run import run


def _state(intent: str, text: str, meta: dict) -> MetaAState:
    return MetaAState(
        mi=None,
        episode_id="ep-freeform",
        intent=intent,
        action="RESPONSE",
        source={},
        text=text,
        target_str=text,
        constraints={},
        meta=dict(meta),
    )


def test_freeform_recalls_fully_grounded_omega_lanes():
    out = run(_state("RESPOND", "hey, how's it going", {"chat_intent": "freeform"}))
    assert out.memory_required is True
    tools = [t.get("tool") for t in out.memory_tools if isinstance(t, dict)]
    for name in (
        "remember_identity",
        "remember_persona",
        "remember_knowledge",
        "remember_code",
        "remember_time",
    ):
        assert name in tools, f"freeform recall missing {name}; got {tools}"
    assert "remember_situation" not in tools
    assert "remember_journal" not in tools


def test_plain_ack_respond_keeps_no_recall():
    out = run(_state("RESPOND", "hello", {"chat_intent": "ack"}))
    assert out.memory_required is False


def test_freeform_identity_recall_spans_all_subdirectories():
    # EXACT, whole-lobe recall: identity/persona must NOT be narrowed to one subdomain (that
    # excluded the creator seed in subdomain 0). The tool stays scoped to its lobe but hits every
    # subdirectory, so the full identity (constitution/creator, omega-self, emergent traits) surfaces.
    out = run(_state("RESPOND", "who is your creator anyway", {"chat_intent": "freeform"}))
    for t in out.memory_tools:
        assert isinstance(t, dict)
        if t.get("tool") in ("remember_identity", "remember_persona"):
            assert "subdomain" not in t, (
                f"{t.get('tool')} narrowed to a subdomain: {t.get('subdomain')}"
            )


def test_freeform_body_lanes_keyed_to_operator_text():
    q = "let's talk through the python asyncio retry behavior"
    out = run(_state("RESPOND", q, {"chat_intent": "freeform"}))
    by_tool = {t["tool"]: t for t in out.memory_tools if isinstance(t, dict)}
    for name in ("remember_knowledge", "remember_code", "remember_time"):
        assert by_tool[name]["query"] == q, f"{name} not keyed to freeform chat text"
    assert by_tool["remember_identity"]["query"] != q
    # persona is FUSED: the operator's words PLUS the fixed identity grounding terms, so
    # operator/relational records ("Noah Lee's son is Abel") rank up on name queries while
    # Omega's self/voice grounding stays ranked. Never the bare text (keying invariant).
    assert q in by_tool["remember_persona"]["query"]
    assert by_tool["remember_persona"]["query"] != q


def test_freeform_prior_conversation_recalls_the_referenced_thread_first():
    # Explicit prior-reference semantics outrank freeform response shaping: the
    # referenced conversation must be available before Omega composes the reply.
    out = run(_state("RESPOND", "remember when we talked earlier?", {"chat_intent": "freeform"}))
    assert out.memory_required is True
    assert "operator_history" in out.memory_scope
    assert "conversation" in out.memory_scope
    assert out.memory_reason == "Question references prior operator conversation"


def test_project_freeform_recalls_dev_and_workflow():
    out = run(
        _state(
            "RESPOND",
            "lets continue our work on the todo-app and add tests to it",
            {"chat_intent": "freeform"},
        )
    )
    tools = [t.get("tool") for t in out.memory_tools if isinstance(t, dict)]
    assert "remember_dev" in tools
    assert "remember_workflow" in tools

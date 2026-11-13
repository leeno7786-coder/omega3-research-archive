"""Slice 2 B2 — Meta A intent → ExecutionActionPack conversion."""

from __future__ import annotations

import pytest

from meta_loop.meta_a import (
    MetaAInput,
    build_execution_action_pack,
    validate_execution_action_pack,
    execution_action_pack_to_dict,
)


def _mi(
    intent: str,
    *,
    target=None,
    message: str = "",
    opened_by: str = "operator_command",
    **extra_meta,
):
    # Global A no longer carries a target — the topic/message reaches Meta A via the
    # dominant contributor message. Map a string target onto the message for tests.
    eff_message = message or (target if isinstance(target, str) else "")
    contributors = []
    if eff_message or extra_meta:
        meta = {"message": eff_message, **extra_meta}
        contributors.append(
            {
                "source": "operator",
                "type": opened_by,
                "weight": 1.0,
                "metadata": meta,
            }
        )
    return MetaAInput(
        episode_id="ep_1",
        pressure={"reason": opened_by, "contributors": contributors},
        trajectory_snapshot={},
        action_goal={
            "episode_id": "ep_1",
            "intent": intent,
            "confidence": 0.9,
            "reason": "test",
        },
    )


@pytest.mark.parametrize(
    "intent,expected_action",
    [
        ("RESPOND", "RESPONSE"),
        ("RESOLVE_QUERY", "RESPONSE"),
        ("PROACTIVE_RESPONSE", "PROACTIVE_RESPONSE"),
        ("LEARN", "USE_TOOL"),
        ("USE_TOOL", "USE_TOOL"),
        ("IMAGE_CREATE", "IMAGE_CREATION"),
        ("DOCUMENT_CREATE", "DOCUMENT_CREATION"),
        # SELF_INQUIRY was removed from the system (operator directive): it collapsed
        # to IDLE. As a now-unknown intent it falls to the IDLE action via _intent_to_action_map's default.
        ("SELF_INQUIRY", "IDLE"),
        ("IDLE", "IDLE"),
    ],
)
def test_intent_to_action_map(intent, expected_action):
    pack = build_execution_action_pack(_mi(intent, message="x"))
    assert pack.action == expected_action
    assert pack.source_action_goal["intent"] == intent


def test_learn_python_web_fetch_recalls_resolved_topic():
    pack = build_execution_action_pack(_mi("LEARN", message="Python"))
    assert pack.action == "USE_TOOL"
    assert pack.tool == "web_fetch"
    assert pack.tool_reason
    assert pack.memory_required is True
    assert pack.memory_query == "Python"
    # Python is a CODING domain (lobe 8) -> its grounding recall searches coding via remember_code
    # (domain-aware LEARN recall, 2026-06-30). A knowledge domain would use remember_knowledge.
    assert [t["tool"] for t in pack.memory_tools] == ["remember_code"]
    assert pack.execution_inputs.get("query")
    validate_execution_action_pack(pack)


def test_respond_prior_conversation_memory():
    pack = build_execution_action_pack(
        _mi(
            "RESPOND",
            target={"question": "What did I tell Omega about Python?"},
            message="What did I tell Omega about Python?",
        )
    )
    assert pack.action == "RESPONSE"
    assert pack.memory_required is True
    assert pack.memory_reason
    assert pack.memory_query
    assert len(pack.memory_tools) > 0
    assert len(pack.memory_scope) > 0
    validate_execution_action_pack(pack)


def test_respond_recall_covers_epistemic_and_prior_conversation():
    """Epistemic imperatives recall knowledge; prior references recall the thread."""
    plain = build_execution_action_pack(_mi("RESPOND", message="tell me about it"))
    assert plain.memory_required is True
    tools = [t.get("tool") for t in (plain.memory_tools or [])]
    assert "remember_knowledge" in tools

    prior = build_execution_action_pack(
        _mi("RESPOND", message="what did i tell you about the plan?")
    )
    assert prior.memory_required is True
    assert {"operator_history", "conversation"}.issubset(prior.memory_scope)


def test_question_recall_attempt_is_distinct_from_memory_bound_answer():
    general = build_execution_action_pack(
        _mi("RESPOND", message="what is the capital of France?")
    )
    assert general.memory_required is True
    assert getattr(general, "memory_answer_required", False) is False

    autobiographical = build_execution_action_pack(
        _mi("RESPOND", message="what do you remember about Mitchell Ray?")
    )
    assert autobiographical.memory_required is True
    assert getattr(autobiographical, "memory_answer_required", False) is True


def test_memory_bound_answer_cannot_bypass_the_recall_contract():
    pack = build_execution_action_pack(
        _mi("RESPOND", message="what do you remember about Mitchell Ray?")
    )
    malformed = execution_action_pack_to_dict(pack)
    malformed["memory_required"] = False

    with pytest.raises(ValueError, match="memory_answer_required requires memory_required"):
        validate_execution_action_pack(malformed)


def test_resolve_query_grounds_recall_on_the_answer_thread():
    # 2026-07-10: RESOLVE_QUERY recalls — the operator's answer (plus the
    # system's question when threaded) grounds the reply instead of replying
    # blind. resolution_context still rides execution_inputs unchanged.
    pack = build_execution_action_pack(
        _mi(
            "RESOLVE_QUERY",
            message="fixed the bug",
            opened_by="operator_response",
        )
    )
    assert pack.memory_required is True
    assert "fixed the bug" in (pack.memory_query or "")
    assert "resolution_context" in pack.execution_inputs


def test_image_create_requires_image_spec():
    pack = build_execution_action_pack(
        _mi(
            "IMAGE_CREATE",
            message="Generate an image of an omega cognition graph",
        )
    )
    assert pack.action == "IMAGE_CREATION"
    assert pack.tool == "image_tool"
    assert pack.tool_reason
    assert pack.execution_inputs.get("image_spec")
    validate_execution_action_pack(pack)


def test_document_create_carries_format_and_template():
    pack = build_execution_action_pack(
        _mi(
            "DOCUMENT_CREATE",
            message="Quarterly report",
            format="pdf",
            template="generic",
        )
    )
    assert pack.action == "DOCUMENT_CREATION"
    assert pack.tool == "document_tool"
    spec = pack.execution_inputs.get("document_spec") or {}
    assert spec.get("format") == "pdf"
    assert spec.get("template") == "generic"
    validate_execution_action_pack(pack)


def test_self_inquiry_collapses_to_idle():
    # SELF_INQUIRY was removed (operator directive): the intent, its action-phase
    # mapping, and the memory-phase self-inquiry MRIL identity-recall branch are all
    # gone. A SELF_INQUIRY input is now an unknown intent that collapses to IDLE —
    # no action promotion, no tool, no identity recall.
    pack = build_execution_action_pack(_mi("SELF_INQUIRY", message="who am I"))
    assert pack.action == "IDLE"
    assert pack.tool is None
    assert pack.memory_required is False
    validate_execution_action_pack(pack)


def test_use_tool_prior_history_promotes_mril_response():
    pack = build_execution_action_pack(
        _mi("USE_TOOL", message="remember what I told you about Python earlier")
    )
    assert pack.action == "RESPONSE"
    assert pack.tool is None
    assert pack.memory_required is True
    assert pack.memory_query
    assert len(pack.memory_tools) > 0
    validate_execution_action_pack(pack)


def test_idle_minimal_pack():
    pack = build_execution_action_pack(_mi("IDLE"))
    assert pack.action == "IDLE"
    assert pack.tool is None
    assert pack.memory_required is False
    validate_execution_action_pack(pack)


def test_use_tool_file_reader_capability():
    pack = build_execution_action_pack(_mi("USE_TOOL", message="read file report.pdf"))
    assert pack.tool == "file_reader"


def test_never_emits_learn_as_action():
    pack = build_execution_action_pack(_mi("LEARN", target="Python"))
    assert pack.action != "LEARN"


def test_every_produced_pack_passes_validation():
    for intent in (
        "RESPOND",
        "RESOLVE_QUERY",
        "PROACTIVE_RESPONSE",
        "LEARN",
        "USE_TOOL",
        "IMAGE_CREATE",
        "IDLE",
    ):
        pack = build_execution_action_pack(_mi(intent, message="sample"))
        validate_execution_action_pack(execution_action_pack_to_dict(pack))


@pytest.mark.parametrize(
    "message",
    ["Which text editor do I like best?", "What is my favorite color?", "What is my dog's name?"],
)
def test_operator_personal_questions_are_memory_bound(message):
    """Live 2026-09-06: "which text editor do I like best?" routed to persona,
    recall came back empty, and the answer model invented Notepad++ because the
    turn was not memory-bound. A question routed to the operator's own facts is
    answerable from memory or not at all."""
    pack = build_execution_action_pack(_mi("RESPOND", message=message))
    assert pack.memory_required is True
    assert [t.get("tool") for t in (pack.memory_tools or [])] == ["remember_persona"]
    assert pack.memory_answer_required is True


def test_general_knowledge_questions_stay_answerable_from_the_model():
    pack = build_execution_action_pack(_mi("RESPOND", message="what is a mutex?"))
    assert [t.get("tool") for t in (pack.memory_tools or [])] == ["remember_knowledge"]
    assert pack.memory_answer_required is False

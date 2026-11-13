"""Slice 3 B3 — PromptPack construction."""

from __future__ import annotations

from datetime import datetime, timezone

from auto_loop.auto import MemoryEnvelope, build_prompt_pack


def test_temporal_envelope_is_carried_as_dynamic_response_context():
    # Response templates are retired; the fixed episode time rides as context
    # for the engineer/answer path rather than reviving a static system prompt.
    epoch = datetime(2026, 7, 16, 18, 0, tzinfo=timezone.utc).timestamp()
    pp = build_prompt_pack(_pack(), _mem(success=False), temporal={"wall_clock_seconds": epoch})
    assert pp.system_prompt == ""
    assert pp.context_blocks["temporal_prompt_line"].startswith("Current date & time: ")
    assert "2026" in pp.context_blocks["temporal_prompt_line"]


def test_no_temporal_leaves_system_prompt_unchanged():
    # Dynamic replies carry no classifier-selected answer template.
    pp = build_prompt_pack(_pack(), _mem(success=False))
    assert pp.system_prompt == ""
    assert "temporal_prompt_line" not in pp.context_blocks


def _mem(success: bool = True, items=()):
    return MemoryEnvelope(
        memory_required=True,
        memory_status="success" if success else "failed",
        memory_reason="r",
        memory_scope=("knowledge",),
        items=tuple(items),
        error=None,
    )


def _pack(**kw):
    base = {
        "action": "RESPONSE",
        "memory_required": False,
        "execution_intent": "Plan operator response",
        "execution_inputs": {},
        "tool": None,
        "tool_reason": None,
        "source_action_goal": {"target": {"question": "Hi"}},
    }
    base.update(kw)
    return base


def test_response_memory_enriched_reasoning():
    pp = build_prompt_pack(
        _pack(memory_required=True),
        _mem(items=[{"content": "prior note"}]),
    )
    assert pp.prompt_type == "RESPONSE_REASONING"
    assert pp.response_mode == "reasoning"
    assert pp.context_blocks.get("remembered_knowledge_tool_results") == ()
    assert "memory_block" not in pp.context_blocks
    assert pp.context_blocks["memory_envelope"] == {
        "memory_required": True,
        "memory_status": "success",
        "memory_reason": "r",
        "memory_scope": ["knowledge"],
        "item_count": 1,
        "error": None,
    }


def test_promptpack_carries_execution_constraints_block():
    pp = build_prompt_pack(
        _pack(
            execution_constraints={
                "planning_only": True,
                "no_inference_in_meta": True,
                "no_execution_in_meta": True,
                "no_model_fields": True,
            }
        ),
        _mem(success=False),
    )
    assert pp.context_blocks["constraints_block"]["planning_only"] is True


def test_response_question_mark():
    pp = build_prompt_pack(
        _pack(source_action_goal={"target": {"question": "What is X?"}}),
        _mem(success=False),
    )
    assert pp.prompt_type == "RESPONSE_QUESTION"
    assert pp.response_mode == "question"


def test_response_question_mark_from_pressure_message():
    pp = build_prompt_pack(
        _pack(source_action_goal={"target": "What is X"}),
        _mem(success=False),
        pressure={
            "contributors": [
                {"detail": {"message": "What is X?"}},
            ],
        },
    )
    assert pp.prompt_type == "RESPONSE_QUESTION"
    assert pp.response_mode == "question"
    assert pp.system_prompt == ""


def test_response_ack_from_execution_intent():
    pp = build_prompt_pack(
        _pack(execution_intent="acknowledge receipt of message"),
        _mem(success=False),
    )
    assert pp.prompt_type == "RESPONSE_ACK"
    assert pp.response_mode == "ack"


def test_use_tool_block_and_spec():
    pack = _pack(
        action="USE_TOOL",
        tool="web_fetch",
        tool_reason="fetch docs",
        execution_inputs={"query": "python asyncio"},
    )
    pp = build_prompt_pack(pack, _mem(success=False))
    assert pp.prompt_type == "TOOL_CALL_SYSTEM"
    assert "web_fetch" in (pp.context_blocks.get("tool_block") or "")
    assert pp.tool_call_spec == {
        "tool": "web_fetch",
        "params": {"query": "python asyncio"},
    }
    assert pp.system_prompt


def test_image_and_idle_prompt_types():
    img = build_prompt_pack(
        _pack(
            action="IMAGE_CREATION",
            tool="image_tool",
            execution_inputs={"image_spec": "a red cube"},
        ),
        _mem(success=False),
    )
    assert img.prompt_type == "IMAGE_SYSTEM"
    assert img.system_prompt
    doc = build_prompt_pack(
        _pack(
            action="DOCUMENT_CREATION",
            tool="document_tool",
            execution_inputs={
                "document_spec": {"format": "md", "template": "generic", "text": "notes"},
            },
        ),
        _mem(success=False),
    )
    assert doc.prompt_type == "DOCUMENT_SYSTEM"
    assert doc.system_prompt
    idle = build_prompt_pack(_pack(action="IDLE"), _mem(success=False))
    assert idle.prompt_type == "IDLE_SYSTEM"


def test_use_skill_build_uses_execution_prompt_not_idle():
    pp = build_prompt_pack(
        _pack(
            action="USE_SKILL",
            execution_intent="BUILD",
            execution_inputs={
                "skill_execution_pack": {
                    "selected_skill": {
                        "skill_id": "codex-grade-coding",
                        "skill_name": "codex-grade-coding",
                    },
                    "objective": "build a basic todo app green",
                }
            },
        ),
        _mem(success=False),
    )

    assert pp.prompt_type == "SKILL_BUILD_SYSTEM"
    assert pp.response_mode == "summary"
    assert pp.prompt_type != "IDLE_SYSTEM"


def test_no_prompt_type_outside_allowed():
    from auto_loop.auto import PROMPT_TYPES

    for action, kwargs, expected in [
        ("PROACTIVE_RESPONSE", {"memory_required": True}, "PROACTIVE_REASONING"),
        (
            "PROACTIVE_RESPONSE",
            {"execution_intent": "status notification"},
            "PROACTIVE_ACK",
        ),
    ]:
        pp = build_prompt_pack(_pack(action=action, **kwargs), _mem(success=False))
        assert pp.prompt_type in PROMPT_TYPES
        assert pp.prompt_type == expected

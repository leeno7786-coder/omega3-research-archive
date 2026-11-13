"""AUTO route phase — model profile selection."""

from __future__ import annotations

from typing import Mapping, Optional

from auto_loop.motor_adapter import effective_motor_action, motor_program_from_pack


def tool_call_is_direct(execution_pack: Mapping[str, object]) -> bool:
    """True when Meta-bound params fully specify the tool call (no model query)."""
    from pressure_loop.policy import (
        auto_direct_tools_always,
        auto_web_fetch_requires_query,
    )

    tool = str(execution_pack.get("tool") or "")
    inputs = dict(execution_pack.get("execution_inputs") or {})
    program = motor_program_from_pack(execution_pack)
    action = str(execution_pack.get("action") or "").strip()
    # Legacy/direct phase callers may supply the already-bound tool without an
    # action field.  Motor packs are stricter and must explicitly resolve to the
    # USE_TOOL transport; ordinary packs retain the historical direct-tool
    # behavior unless they explicitly name a different action.
    if program is not None:
        if effective_motor_action(execution_pack) != "USE_TOOL":
            return False
    elif action and effective_motor_action(execution_pack) != "USE_TOOL":
        return False
    if program is not None and not str(inputs.get("query") or "").strip():
        inputs["query"] = program.execution_plan.operations[0]
    if tool == "web_fetch":
        if bool(inputs.get("web_research")):
            return False
        if auto_web_fetch_requires_query():
            return bool(str(inputs.get("query") or "").strip())
        return True
    if tool in auto_direct_tools_always():
        return True
    return not tool


def select_model_profile(
    prompt_type: str,
    *,
    memory_enriched: bool,
    tool_call_is_direct: bool = False,
) -> Optional[str]:
    """PHASE 3 — deterministic model profile routing."""
    from pressure_loop.policy import auto_memory_enriched_bumps_fast_to_reasoning

    bump = auto_memory_enriched_bumps_fast_to_reasoning()
    profile: Optional[str]
    if prompt_type in ("RESPONSE_REASONING", "PROACTIVE_REASONING"):
        profile = "TEXT_REASONING"
    elif prompt_type in ("RESPONSE_QUESTION", "PROACTIVE_ACK", "RESPONSE_ACK"):
        if memory_enriched and bump:
            profile = "TEXT_REASONING"
        else:
            profile = "TEXT_FAST"
    elif prompt_type == "TOOL_CALL_SYSTEM":
        profile = None if tool_call_is_direct else "TOOL_EXECUTOR"
    elif prompt_type == "TOOL_RESULT_SYNTHESIS":
        profile = "TEXT_REASONING"
    elif prompt_type == "IMAGE_SYSTEM":
        profile = "IMAGE_GENERATION"
    elif prompt_type == "DOCUMENT_SYSTEM":
        profile = "TEXT_REASONING"
    elif prompt_type == "ASK_OPERATOR_QUERY":
        profile = "TEXT_FAST"
    elif prompt_type == "IDLE_SYSTEM":
        profile = None
    else:
        profile = None

    try:
        from guardian.terminal_feedback import emit_data, emit_guardian_event

        emit_data(
            "auto",
            "model_profile",
            summary=f"prompt_type={prompt_type} profile={profile}",
            prompt_type=prompt_type,
            profile=profile,
            memory_enriched=memory_enriched,
            tool_call_is_direct=tool_call_is_direct,
        )
        emit_guardian_event(
            "omega.auto",
            "model_profile",
            prompt_type=prompt_type,
            profile=profile,
            memory_enriched=memory_enriched,
            tool_call_is_direct=tool_call_is_direct,
        )
    except Exception:
        pass

    return profile


def run(
    prompt_type: str,
    *,
    memory_enriched: bool,
    tool_call_is_direct_flag: bool = False,
) -> Optional[str]:
    return select_model_profile(
        prompt_type,
        memory_enriched=memory_enriched,
        tool_call_is_direct=tool_call_is_direct_flag,
    )

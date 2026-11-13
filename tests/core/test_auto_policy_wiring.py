"""Auto loop reads execute/route/recall config via pressure_loop.policy."""

from __future__ import annotations

from pressure_loop.policy import (
    auto_direct_tools_always,
    auto_memory_recall_limit,
    auto_profile_max_tokens,
    auto_system_prompt,
    auto_web_fetch_default_max_results,
    clear_policy_cache,
)
from auto_loop.promptpack.run import build_prompt_pack
from auto_loop._auto_state import MemoryEnvelope
from auto_loop.route.run import tool_call_is_direct


def test_auto_yaml_system_prompt_wired():
    clear_policy_cache()
    assert "Omega AUTO" in auto_system_prompt()
    env = MemoryEnvelope(
        memory_required=False,
        memory_status="skipped",
        memory_reason=None,
        memory_scope=(),
        items=(),
        error=None,
    )
    pack = build_prompt_pack(
        {
            "action": "IDLE",
            "execution_intent": "",
            "execution_inputs": {},
            "source_action_goal": {},
        },
        env,
    )
    assert pack.system_prompt == auto_system_prompt("IDLE_SYSTEM")


def test_auto_yaml_system_prompt_variants_wired():
    clear_policy_cache()
    assert auto_system_prompt("RESPONSE_QUESTION") != auto_system_prompt()
    assert "answering a direct" in auto_system_prompt("RESPONSE_QUESTION")


def test_auto_prompts_teach_chat_output_formats():
    clear_policy_cache()
    prompt = auto_system_prompt("RESPONSE_QUESTION")
    assert "Markdown" in prompt
    assert "fenced code blocks" in prompt
    assert "Mermaid" in prompt
    assert "Never emit raw HTML" in prompt


def test_auto_format_guidance_does_not_contaminate_machine_prompts():
    clear_policy_cache()
    assert "Response formatting" not in auto_system_prompt("TOOL_CALL_SYSTEM")
    assert "Response formatting" not in auto_system_prompt("IMAGE_SYSTEM")
    assert "Response formatting" not in auto_system_prompt("IDLE_SYSTEM")


def test_auto_web_fetch_max_results_from_config():
    clear_policy_cache()
    assert auto_web_fetch_default_max_results() == 3


def test_auto_profile_max_tokens_from_config():
    clear_policy_cache()
    # Ceilings raised 2026-07-12 (1a99b547): TEXT_FAST 256 truncated real
    # teaching replies mid-sentence — these pin the operator-directed config.
    assert auto_profile_max_tokens("TEXT_FAST") == 1024
    assert auto_profile_max_tokens("TOOL_EXECUTOR") == 512


def test_auto_direct_tools_from_config():
    clear_policy_cache()
    always = auto_direct_tools_always()
    assert "memory_recall" not in always
    assert "file_reader" in always
    assert tool_call_is_direct({"tool": "file_reader", "execution_inputs": {"query": "report.pdf"}})
    assert tool_call_is_direct({"tool": "web_fetch", "execution_inputs": {"query": "Python"}})
    assert not tool_call_is_direct(
        {"tool": "web_fetch", "execution_inputs": {"query": "Python", "web_research": True}}
    )


def test_auto_memory_recall_limit_from_config():
    clear_policy_cache()
    assert auto_memory_recall_limit() == 10

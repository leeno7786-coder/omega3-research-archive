"""Slice 3 B4 — model profile routing."""

from __future__ import annotations

from auto_loop.auto import select_model_profile


def test_reasoning_profiles():
    assert select_model_profile("RESPONSE_REASONING", memory_enriched=False) == "TEXT_REASONING"
    assert select_model_profile("PROACTIVE_REASONING", memory_enriched=False) == "TEXT_REASONING"


def test_fast_profiles_and_memory_bump():
    assert select_model_profile("RESPONSE_QUESTION", memory_enriched=False) == "TEXT_FAST"
    assert select_model_profile("RESPONSE_QUESTION", memory_enriched=True) == "TEXT_REASONING"
    assert select_model_profile("RESPONSE_ACK", memory_enriched=False) == "TEXT_FAST"


def test_tool_direct_vs_mediated():
    assert (
        select_model_profile("TOOL_CALL_SYSTEM", memory_enriched=False, tool_call_is_direct=True)
        is None
    )
    assert (
        select_model_profile("TOOL_CALL_SYSTEM", memory_enriched=False, tool_call_is_direct=False)
        == "TOOL_EXECUTOR"
    )


def test_synth_image_idle():
    assert select_model_profile("TOOL_RESULT_SYNTHESIS", memory_enriched=False) == "TEXT_REASONING"
    assert select_model_profile("IMAGE_SYSTEM", memory_enriched=False) == "IMAGE_GENERATION"
    assert select_model_profile("IDLE_SYSTEM", memory_enriched=False) is None

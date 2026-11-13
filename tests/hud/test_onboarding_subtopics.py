"""Onboarding subtopic decomposition: the prompt that folds the operator's onboarding
topics through the model so Omega breaks each into concrete learnable subtopics, framed
as gaps it has NO knowledge of yet and must learn."""

from __future__ import annotations

from hud.onboarding_subtopics import build_subtopic_prompt, decompose_subtopics


def test_prompt_tells_model_to_decompose_and_that_it_has_no_knowledge():
    system, user = build_subtopic_prompt("software_engineer", ["Algorithms", "Python"])
    blob = (system + "\n" + user).lower()
    # Frames the topics as gaps with no stored knowledge -> must learn.
    assert "no" in blob and "knowledge" in blob
    assert "learn" in blob
    # Tells the model to break topics into smaller subtopics.
    assert "subtopic" in blob
    # The actual onboarding topics are in the prompt.
    assert "Algorithms" in user
    assert "Python" in user
    # Asks for strict JSON so the result is parseable.
    assert "json" in blob


def test_decompose_parses_model_json_map():
    def fake(system, user):
        return (
            '{"Algorithms": ["Big-O analysis", "Sorting", "Graph traversal"],'
            ' "Python": ["asyncio", "decorators"]}'
        )

    out = decompose_subtopics("software_engineer", ["Algorithms", "Python"], reason_fn=fake)
    assert out["Algorithms"] == ["Big-O analysis", "Sorting", "Graph traversal"]
    assert out["Python"] == ["asyncio", "decorators"]


def test_decompose_handles_reasoning_preamble_and_markdown_fence():
    def fake(system, user):
        return (
            "<think>Let me break these down...</think>\n"
            "```json\n"
            '{"Algorithms": ["Recursion", "Dynamic programming"]}\n'
            "```"
        )

    out = decompose_subtopics("student", ["Algorithms"], reason_fn=fake)
    assert out["Algorithms"] == ["Recursion", "Dynamic programming"]


def test_decompose_restricts_to_requested_categories_dedups_and_skips_blank():
    def fake(system, user):
        return '{"Algorithms": ["Sorting", "sorting", "  ", "Graphs"], "Unasked Topic": ["nope"]}'

    out = decompose_subtopics("software_engineer", ["Algorithms"], reason_fn=fake)
    assert out["Algorithms"] == ["Sorting", "Graphs"]  # deduped (case-insens), blanks gone
    assert "Unasked Topic" not in out  # only categories we asked about


def test_decompose_is_failure_soft_on_garbage_or_raise():
    assert (
        decompose_subtopics("writer", ["Editing"], reason_fn=lambda s, u: "not json at all") == {}
    )
    assert decompose_subtopics("writer", ["Editing"], reason_fn=lambda s, u: None) == {}

    def boom(system, user):
        raise RuntimeError("model down")

    assert decompose_subtopics("writer", ["Editing"], reason_fn=boom) == {}

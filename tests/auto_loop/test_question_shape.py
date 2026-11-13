"""Deterministic question-shape detector (Stage 0 recall-skip floor)."""

from __future__ import annotations

import pytest

from auto_loop.promptpack.question_shape import is_question_shaped


@pytest.mark.parametrize(
    "text",
    [
        "who is your creator?",
        "what is the capital of France?",
        "How do I reset the index",  # leading interrogative, no '?'
        "Can you tell me the time?",
        "  why does this happen?  ",  # surrounding whitespace
    ],
)
def test_question_shapes_detected(text):
    assert is_question_shaped(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "build me a fibonacci script",
        "remember that my favorite color is green",
        "on it, thanks",
        "",
        "   ",
    ],
)
def test_non_questions_rejected(text):
    assert is_question_shaped(text) is False

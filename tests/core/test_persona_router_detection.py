"""High-confidence person-name detection for persona profile creation."""

from __future__ import annotations

import pytest

from core.persona_router import detect_persons


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Remember Mitchell Ray.", ["Mitchell Ray"]),
        ("Remember Abel.", ["Abel"]),
        ("I met Priya Shah at a conference.", ["Priya Shah"]),
        ("I spoke with Abel yesterday.", ["Abel"]),
        ("My business partner Morgan Lee called.", ["Morgan Lee"]),
    ],
)
def test_detect_persons_returns_whole_person_names(text, expected):
    """Splitting a full name or missing an introduced single name would create
    the wrong persona tree (or none at all).
    """
    assert detect_persons(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Please learn Python on Windows.",
        "Remember that TypeScript is useful.",
        "Monday is the next testing day.",
    ],
)
def test_detect_persons_does_not_promote_capitalized_non_people(text):
    """A technology or weekday must never become a persona stub."""
    assert detect_persons(text) == []

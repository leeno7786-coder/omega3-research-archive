from __future__ import annotations

import pytest

from memory.speech_act import classify_speech_act


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("How many different doctors did I visit?", "question"),
        ("Do you have any pets", "question"),
        ("I just signed a contract with my first client today.", "statement"),
        ("Please correct my grammar below.", "command"),
        ("Remember that my sister's birthday is in May.", "command"),
        ("Tell me, did I ever visit a dermatologist?", "question"),
        ("Do the dishes.", "command"),
        ("Will called earlier.", "statement"),
        ("", "unknown"),
    ],
)
def test_classify_speech_act(text: str, expected: str) -> None:
    assert classify_speech_act(text) == expected


def test_build_memory_dict_persists_inferred_speech_act() -> None:
    from memory.core import build_memory_dict

    record = build_memory_dict(
        "What do you remember about Noah?",
        [1, 0, 1],
        "operator_chat",
        metadata={"domain": "operator"},
    )

    assert record["speech_act"] == "question"


def test_explicit_speech_act_from_ingress_contract_wins() -> None:
    from memory.core import build_memory_dict

    record = build_memory_dict(
        "Please retain this as an operator preference.",
        [1, 0, 2],
        "operator_chat",
        metadata={"speech_act": "statement"},
    )

    assert record["speech_act"] == "statement"

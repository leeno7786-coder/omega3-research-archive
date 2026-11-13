from __future__ import annotations

import requests

from auto_loop.promptpack.intent_classifier import (
    classify_chat_intent,
    deterministic_chat_intent,
)


def test_chat_classifier_is_deterministic_and_never_calls_runtime(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise AssertionError("the retired classifier model must not be called")

    monkeypatch.setattr(requests, "post", fail)
    for text in (
        "hello there",
        "What is the latest Python release?",
        "reason through the failure",
        "what did we build yesterday",
    ):
        assert classify_chat_intent(text) == deterministic_chat_intent(text)

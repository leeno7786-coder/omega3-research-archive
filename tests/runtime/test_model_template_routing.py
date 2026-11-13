from __future__ import annotations

from runtime.brain_cloud import _chat_body


def _payload(*, enable_thinking: bool, tools=None) -> dict:
    return {
        "prompt": "continue",
        "stop": ["<end_of_turn>", "<|im_end|>", "CUSTOM_STOP"],
        "chat_template_kwargs": {"enable_thinking": enable_thinking},
        "tools": tools,
    }


def test_lmstudio_gemma4_uses_its_turn_end_and_thinking_template() -> None:
    body = _chat_body(
        _payload(enable_thinking=False),
        "google/gemma-4-26b-a4b-qat",
        "lmstudio",
    )

    assert body["stop"] == ["CUSTOM_STOP", "<turn|>"]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_lmstudio_gemma4_tool_calls_also_stop_at_tool_response_boundary() -> None:
    body = _chat_body(
        _payload(
            enable_thinking=True,
            tools=[{"type": "function", "function": {"name": "navigate"}}],
        ),
        "google/gemma-4-26b-a4b-qat",
        "lmstudio",
    )

    assert body["stop"] == ["CUSTOM_STOP", "<turn|>", "<|tool_response>"]
    assert body["chat_template_kwargs"] == {"enable_thinking": True}


def test_lmstudio_qwen_uses_its_turn_end_and_thinking_template() -> None:
    body = _chat_body(
        _payload(enable_thinking=False),
        "Qwen/Qwen3.5-9B",
        "lmstudio",
    )

    assert body["stop"] == ["CUSTOM_STOP", "<|im_end|>"]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_legacy_gemma_and_qwen_keep_family_endings_without_thinking_override() -> None:
    gemma = _chat_body(
        _payload(enable_thinking=True),
        "google/gemma-3-27b-it",
        "lmstudio",
    )
    qwen = _chat_body(
        _payload(enable_thinking=True),
        "Qwen/Qwen2.5-14B-Instruct",
        "lmstudio",
    )

    assert gemma["stop"] == ["CUSTOM_STOP", "<end_of_turn>"]
    assert qwen["stop"] == ["CUSTOM_STOP", "<|im_end|>"]
    assert "chat_template_kwargs" not in gemma
    assert "chat_template_kwargs" not in qwen


def test_unknown_lmstudio_model_keeps_server_managed_template_behavior() -> None:
    body = _chat_body(
        _payload(enable_thinking=False),
        "publisher/custom-model",
        "lmstudio",
    )

    assert body["stop"] == ["CUSTOM_STOP"]
    assert "chat_template_kwargs" not in body


def test_unregistered_future_family_versions_do_not_guess_template_tokens() -> None:
    for model in ("google/gemma-5-26b", "Qwen/Qwen4-32B"):
        body = _chat_body(_payload(enable_thinking=False), model, "lmstudio")

        assert body["stop"] == ["CUSTOM_STOP"]
        assert "chat_template_kwargs" not in body


def test_cloud_openai_dialect_does_not_apply_lmstudio_model_profile() -> None:
    body = _chat_body(
        _payload(enable_thinking=False),
        "google/gemma-4-26b-a4b-qat",
        "openai",
    )

    assert body["stop"] == ["<end_of_turn>", "<|im_end|>", "CUSTOM_STOP"]
    assert "chat_template_kwargs" not in body

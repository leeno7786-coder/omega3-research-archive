from __future__ import annotations

import json

import runtime.brain_local as local
import runtime.brain_cloud_config as config
from runtime import local_services


class _Response:
    status_code = 200
    text = "{}"

    def json(self):
        return {
            "choices": [
                {
                    "message": {"content": "local answer"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        }


def test_local_lmstudio_descriptor_is_openai_compatible(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "_config_path", lambda: tmp_path / "mind_backend.json")
    (tmp_path / "mind_backend.json").write_text(
        json.dumps(
            {
                "backend": "local",
                "enabled": False,
                "provider": "lmstudio",
                "model": "qwen-local",
                "local_model": "qwen-local",
                "base_url": "http://127.0.0.1:1234/v1",
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    class _Session:
        def post(self, url, json=None, headers=None, timeout=None, stream=False):
            captured.update(url=url, body=json, headers=headers, timeout=timeout)
            return _Response()

    monkeypatch.setattr(local, "_ensure_session", lambda: _Session())

    result = local.generate_completion(
        {"prompt": "hello", "system": "you are Omega", "temperature": 0.2}
    )

    assert captured["url"] == "http://127.0.0.1:1234/v1/chat/completions"
    assert captured["headers"] == {}
    assert captured["body"]["model"] == "qwen-local"
    assert result["ok"] is True
    assert result["text"] == "local answer"
    assert result["backend"] == "local"


def test_local_lmstudio_completion_uses_model_template_profile(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "_config_path", lambda: tmp_path / "mind_backend.json")
    (tmp_path / "mind_backend.json").write_text(
        json.dumps(
            {
                "backend": "local",
                "provider": "lmstudio",
                "model": "google/gemma-4-26b-a4b-qat",
                "base_url": "http://127.0.0.1:1234/v1",
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    class _Session:
        def post(self, url, json=None, headers=None, timeout=None, stream=False):
            captured.update(body=json)
            return _Response()

    monkeypatch.setattr(local, "_ensure_session", lambda: _Session())

    result = local.generate_completion(
        {
            "prompt": "reflect",
            "stop": ["<end_of_turn>", "<|im_end|>"],
            "chat_template_kwargs": {"enable_thinking": True},
        }
    )

    assert captured["body"]["stop"] == ["<turn|>"]
    assert captured["body"]["chat_template_kwargs"] == {"enable_thinking": True}
    assert result["ok"] is True


def test_local_lmstudio_stream_uses_model_template_profile(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "_config_path", lambda: tmp_path / "mind_backend.json")
    (tmp_path / "mind_backend.json").write_text(
        json.dumps(
            {
                "backend": "local",
                "provider": "lmstudio",
                "model": "Qwen/Qwen3.5-9B",
                "base_url": "http://127.0.0.1:1234/v1",
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    class _StreamResponse:
        status_code = 200
        text = ""
        encoding = "utf-8"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_lines(self, *, decode_unicode=False):
            yield 'data: {"choices":[{"delta":{"content":"answer"}}]}'
            yield "data: [DONE]"

    class _Session:
        def post(self, url, json=None, **kwargs):
            captured.update(body=json)
            return _StreamResponse()

    monkeypatch.setattr(local, "_ensure_session", lambda: _Session())

    events = list(
        local.generate_stream_events(
            {
                "prompt": "answer",
                "stop": ["<end_of_turn>", "CUSTOM_STOP"],
                "chat_template_kwargs": {"enable_thinking": False},
            }
        )
    )

    assert captured["body"]["stop"] == ["CUSTOM_STOP", "<|im_end|>"]
    assert captured["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert events[0] == ("token", "answer")


def test_local_services_owns_the_lmstudio_proxy(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    class _Dme:
        provider = "CPUExecutionProvider"
        device = "cpu"
        provider_probe_error = ""

        def warmup(self):
            return None

    class _Tts:
        execution = {
            "device": "cpu",
            "provider": "CPUExecutionProvider",
        }

    class _Stt:
        device = "cpu"
        backend = "cpu"

        def close(self):
            return None

    monkeypatch.setattr(
        local_services,
        "selected_mind_seat",
        lambda: "local",
    )
    monkeypatch.setattr(
        local,
        "generate_completion",
        lambda payload, timeout=None: {
            "ok": True,
            "text": "through lm studio",
            "backend": "local",
        },
    )
    app = local_services.create_app(dme=_Dme(), tts=_Tts(), stt=_Stt(), warmup=False)

    response = TestClient(app).post("/generate", json={"prompt": "hello"})

    assert response.status_code == 200
    assert response.json()["text"] == "through lm studio"


def test_local_sse_decodes_utf8_punctuation_even_when_endpoint_defaults_to_latin1(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(config, "_config_path", lambda: tmp_path / "mind_backend.json")
    (tmp_path / "mind_backend.json").write_text(
        json.dumps(
            {
                "backend": "local",
                "provider": "lmstudio",
                "model": "qwen-local",
                "base_url": "http://127.0.0.1:1234/v1",
            }
        ),
        encoding="utf-8",
    )

    class _StreamResponse:
        status_code = 200
        text = ""
        encoding = "ISO-8859-1"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_lines(self, *, decode_unicode=False):
            assert decode_unicode is True
            row = {
                "choices": [{"delta": {"content": "I’m here now—what can I help you with?"}}]
            }
            yield ("data: " + json.dumps(row, ensure_ascii=False)).encode(
                "utf-8"
            ).decode(self.encoding)
            yield "data: [DONE]"

    class _Session:
        def post(self, *args, **kwargs):
            return _StreamResponse()

    monkeypatch.setattr(local, "_ensure_session", lambda: _Session())

    assert list(local.generate_stream({"prompt": "hello"})) == [
        "I’m here now—what can I help you with?"
    ]


def test_generate_stream_events_forward_tool_call_fragments_and_finish_reason(monkeypatch, tmp_path):
    """Live 2026-09-15: a chat reply that called web_search streamed as nothing, because the
    proxy yielded only content deltas; the tool fragments and finish reason now travel too."""
    monkeypatch.setattr(config, "_config_path", lambda: tmp_path / "mind_backend.json")
    (tmp_path / "mind_backend.json").write_text(
        json.dumps({"backend": "local", "provider": "lmstudio", "model": "mistral-medium",
                    "base_url": "http://127.0.0.1:1234/v1"}),
        encoding="utf-8",
    )

    class _StreamResponse:
        status_code = 200
        text = ""
        encoding = "utf-8"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_lines(self, *, decode_unicode=False):
            rows = [
                {"choices": [{"delta": {"reasoning_content": "look it up"}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1", "function": {
                    "name": "web_search", "arguments": "{\"query\": \"Qwen"}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "3.5 2B\"}"}}]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ]
            for row in rows:
                yield "data: " + json.dumps(row)
            yield "data: [DONE]"

    class _Session:
        def post(self, *args, **kwargs):
            return _StreamResponse()

    monkeypatch.setattr(local, "_ensure_session", lambda: _Session())

    events = list(local.generate_stream_events({"prompt": "Tell me about Qwen3.5 2B"}))

    assert events == [
        ("reasoning", "look it up"),
        ("tool_call", {"index": 0, "id": "call-1", "name": "web_search", "arguments": "{\"query\": \"Qwen"}),
        ("tool_call", {"index": 0, "id": "", "name": "", "arguments": "3.5 2B\"}"}),
        ("done", {"finish_reason": "tool_calls", "model_id": "mistral-medium"}),
    ]
    assert list(local.generate_stream({"prompt": "x"})) == []


# ---------------------------------------------------------------------------
# EARLY-V01, 2026-09-21, recommendation 4. A reasoning model is the right class for the
# window lane -- the reflection IS metacognition -- and the trial measured this shape from
# a live OpenAI-dialect endpoint:
#
#   finish_reason: "length", message.reasoning: "<505 chars of thinking>", content: null
#
# The call succeeded at the transport layer and produced nothing the parser could read.
# brain_cloud already refuses that (cloud_empty_completion), which is correct. Three gaps
# remain, and each is a law section 2.1 "could not run" / "ran, found nothing" collapse:
#
#   1. brain_local has NO such guard: it returns ok=True with text="".
#   2. Neither path reads `message.reasoning`. Both read `reasoning_content` only, so a
#      trace under the other spelling is dropped and the failure looks causeless.
#   3. The refusal does not distinguish "spent its budget thinking" (finish_reason=length
#      with a trace present -> raise the token ceiling) from a genuinely empty answer.


def _payload(*, content, reasoning=None, finish_reason="stop"):
    message = {"content": content}
    if reasoning is not None:
        message["reasoning"] = reasoning
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 20, "total_tokens": 29},
    }


class _Reply:
    status_code = 200
    text = "{}"

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _local_config(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_config_path", lambda: tmp_path / "mind_backend.json")
    (tmp_path / "mind_backend.json").write_text(
        json.dumps(
            {
                "backend": "local",
                "enabled": False,
                "provider": "lmstudio",
                "model": "qwen-local",
                "local_model": "qwen-local",
                "base_url": "http://127.0.0.1:1234/v1",
            }
        ),
        encoding="utf-8",
    )


def _run_local(monkeypatch, tmp_path, body):
    _local_config(monkeypatch, tmp_path)

    class _Session:
        def post(self, url, json=None, headers=None, timeout=None, stream=False):
            return _Reply(body)

    monkeypatch.setattr(local, "_ensure_session", lambda: _Session())
    return local.generate_completion({"prompt": "reflect", "system": "you are Omega"})


def test_a_local_reasoning_model_that_answers_nothing_is_a_failure_not_an_empty_answer(
    monkeypatch, tmp_path
) -> None:
    """Law section 2.1 on the local path.

    The model thought for its whole budget and emitted no answer. That is a failure with a
    knowable cause, not a reflection that concluded nothing, and the caller must not be able
    to mistake one for the other.
    """
    result = _run_local(
        monkeypatch,
        tmp_path,
        _payload(content=None, reasoning="I should answer with JSON. The window shows",
                 finish_reason="length"),
    )

    assert result["ok"] is False, (
        "a local completion with no text and no tool call was reported as success with an "
        "empty answer; 'could not answer' and 'answered nothing' must not share a return"
    )
    assert result.get("finish_reason") == "length", "the cause must survive to the caller"


def test_a_local_reasoning_trace_is_not_discarded(monkeypatch, tmp_path) -> None:
    """``message.reasoning`` is the OpenAI-dialect spelling many reasoning models use.

    Reading only ``reasoning_content`` throws the trace away, and then the failure above
    has no evidence attached to explain itself.
    """
    result = _run_local(
        monkeypatch,
        tmp_path,
        _payload(content="the answer", reasoning="first I considered the receipts"),
    )

    assert result["ok"] is True
    assert result.get("reasoning") == "first I considered the receipts", (
        "the trace arrived under 'reasoning' and was dropped because only "
        "'reasoning_content' is read"
    )


def test_an_exhausted_reasoning_budget_is_named_as_such(monkeypatch, tmp_path) -> None:
    """A thought CUT OFF mid-sentence is not the same as a thought that said nothing.

    Scope matters here, and my first draft of this test got it wrong. Operator precedent
    2026-07-11, pinned by
    tests/runtime/test_response_pipeline_streaming.py::test_an_empty_completion_is_an_answer_not_a_failure,
    is that an empty model response with finish_reason 'stop' is a BOUNDED, RECOVERABLE
    condition -- "a thought that failed, not substrate poison" -- which the response stage
    retries think-off. So an empty answer that terminated normally stays ok=True and is left
    exactly as it was.

    What EARLY-V01 measured is a different shape: finish_reason 'length' WITH a reasoning
    trace and no answer. There the model never finished the sentence; the ceiling cut it. It
    cannot be retried think-off, because thinking is what consumed the budget, and the
    remedy is to raise the ceiling. That one must fail attributably.
    """
    truncated = _run_local(
        monkeypatch, tmp_path,
        _payload(content=None, reasoning="thinking and thinking", finish_reason="length"),
    )
    terminated_empty = _run_local(
        monkeypatch, tmp_path, _payload(content=None, finish_reason="stop"),
    )

    assert truncated["ok"] is False, "a thought cut off by the token ceiling is a failure"
    assert "reasoning" in truncated["reason"] or "truncat" in truncated["reason"], (
        f"the cause {truncated['reason']!r} does not say the reasoning budget ran out"
    )
    assert terminated_empty["ok"] is True, (
        "an empty completion that terminated normally is the operator's recoverable case "
        "(2026-07-11); narrowing to the truncation shape must not reclassify it"
    )

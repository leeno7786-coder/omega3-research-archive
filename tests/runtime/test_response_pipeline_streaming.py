"""RISK 5 — the operator's reply streams with its physical pipeline context.

Operator 2026-08-11: the pipelines called the runtime non-streaming and waited for
whole completions, so the operator felt a latency the engine never charged —
the serving engine can interleave physical requests inside one forward pass.

What must hold, and what these tests pin:

  * the physical pipeline context survives the transport on every frame; a
    streamed pass cannot lose correlation with the episode that owns it.
  * streaming changes WHEN the operator hears the answer and nothing else. The
    cascade still receives one whole ``response_text``; the durable record is
    still written once by ``append_response``; every stage after AUTO is
    byte-identical.
  * the stream and the finished reply address the SAME chat bubble. Both derive
    it from ``operator_chat_responses.waiting_chat_turn``, because two copies of
    that rule could paint an answer into one bubble and finish it in another —
    and both halves would look correct on their own.
  * delivery is never a dependency. A sink that raises or a socket with no
    listeners cannot corrupt the model result. A stream failure is contextual
    and terminal; callers do not silently retry it through a blocking path.

Hermetic — no gateway, no GPU, no engine. The runtime seam is stubbed at
``requests`` for the client tests and at ``invoke_runtime_generate`` for the
cascade wiring.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import patch

import pytest

from runtime.client import RuntimeClient, RuntimeStreamError


# ── a fake /generate/stream ──────────────────────────────────────────────────


class _FakeSSEResponse:
    status_code = 200

    def __init__(self, lines: List[str]) -> None:
        self._lines = lines
        self.text = ""

    def iter_lines(self, decode_unicode: bool = False) -> Iterator[str]:
        yield from self._lines

    def __enter__(self) -> "_FakeSSEResponse":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def _sse(*events: tuple[str, dict]) -> List[str]:
    lines: List[str] = []
    for name, data in events:
        lines.append(f"event: {name}")
        lines.append(f"data: {json.dumps(data)}")
        lines.append("")
    return lines


def _tokens(*words: str) -> List[tuple[str, dict]]:
    return [("token", {"token": w}) for w in words]


class _RecordingSession:
    def __init__(self, lines: List[str], status: int = 200) -> None:
        self.lines = lines
        self.status = status
        self.posts: List[Dict[str, Any]] = []

    def post(self, url: str, json: Optional[dict] = None, **kwargs: Any):  # noqa: A002
        self.posts.append({"url": url, "json": json})
        response = _FakeSSEResponse(self.lines)
        response.status_code = self.status  # type: ignore[misc]
        return response


@pytest.fixture
def client(monkeypatch) -> RuntimeClient:
    rt = RuntimeClient(base_url="http://127.0.0.1:11434")
    # No GPU lease or cloud brain: this is the streaming path under test.
    monkeypatch.setattr(rt, "_gpu_clear_to_generate", lambda op: True)
    monkeypatch.setattr("runtime.brain_cloud.enabled", lambda: False)
    return rt


def _wire(client: RuntimeClient, monkeypatch, lines: List[str], status: int = 200):
    session = _RecordingSession(lines, status=status)
    monkeypatch.setattr(client, "_ensure_session", lambda: session)
    return session


# ── physical pipeline context survives the transport ─────────────────────────


def test_streamed_completion_carries_its_pipeline_context_and_model(client, monkeypatch):
    session = _wire(
        client,
        monkeypatch,
        _sse(*_tokens("Hel", "lo"), ("done", {"finish_reason": "stop", "tokens_out": 2})),
    )
    body = client.generate_completion_stream(
        "hi", pipeline_id=0, work_kind="operator_turn", cycle_id="c0",
        model="qwen35_4b_unsloth", max_tokens=64
    )
    sent = session.posts[-1]["json"]
    assert sent["pipeline_id"] == 0
    assert sent["work_kind"] == "operator_turn"
    assert sent["cycle_id"] == "c0"
    assert sent["model"] == "qwen35_4b_unsloth"
    assert sent["max_tokens"] == 64
    assert body["ok"] is True
    assert body["text"] == "Hello"


def test_token_iterator_also_carries_pipeline_context(client, monkeypatch):
    session = _wire(client, monkeypatch, _sse(*_tokens("a"), ("done", {})))
    assert list(client.generate_stream("hi", pipeline_id=3, work_kind="reflection")) == ["a"]
    assert session.posts[-1]["json"]["pipeline_id"] == 3
    assert session.posts[-1]["json"]["work_kind"] == "reflection"


def test_an_untagged_stream_sends_no_pipeline_at_all(client, monkeypatch):
    # Absent, not guessed: pre-episode callers cannot invent physical ownership.
    session = _wire(client, monkeypatch, _sse(*_tokens("a"), ("done", {})))
    client.generate_completion_stream("hi")
    assert "pipeline_id" not in session.posts[-1]["json"]


# ── the answer is whole, and it arrives early ────────────────────────────────


def test_tokens_reach_the_sink_before_the_answer_is_finished(client, monkeypatch):
    _wire(
        client,
        monkeypatch,
        _sse(*_tokens("one ", "two ", "three"), ("done", {"finish_reason": "stop"})),
    )
    seen: List[str] = []
    body = client.generate_completion_stream(
        "hi", pipeline_id=0, work_kind="operator_turn", on_token=seen.append
    )
    # Every token was handed over individually...
    assert seen == ["one ", "two ", "three"]
    # ...and the caller still gets ONE whole reply, which is what the cascade
    # and the durable response rail both read.
    assert body["text"] == "one two three"


def test_done_frame_details_ride_the_result(client, monkeypatch):
    _wire(
        client,
        monkeypatch,
        _sse(
            *_tokens("x"),
            (
                "done",
                {
                    "model_id": "qwen35_4b_unsloth",
                    "finish_reason": "length",
                    "tokens_out": 1,
                    "latency_ms": 12,
                },
            ),
        ),
    )
    body = client.generate_completion_stream("hi")
    assert body["model_id"] == "qwen35_4b_unsloth"  # which endpoint model answered
    assert body["finish_reason"] == "length"
    assert body["tokens_out"] == 1
    assert body["streamed"] is True


def test_reasoning_stays_out_of_the_answer_text(client, monkeypatch):
    _wire(
        client,
        monkeypatch,
        _sse(
            ("reasoning", {"text": "weighing options"}),
            *_tokens("42"),
            ("done", {}),
        ),
    )
    body = client.generate_completion_stream("hi", enable_thinking=True)
    assert body["text"] == "42"
    assert body["reasoning"] == "weighing options"


def test_an_empty_completion_is_an_answer_not_a_failure(client, monkeypatch):
    """Operator precedent 2026-07-11: an empty model response is a bounded,
    recoverable condition — a thought that failed, not substrate poison. The
    response stage retries it think-off, which it can only do if it can tell
    "the model said nothing" from "the runtime said nothing"."""
    from schema.continuity_json import content_hash

    session = _wire(client, monkeypatch, _sse(("done", {"finish_reason": "stop"})))
    body = client.generate_completion_stream("hi")
    assert body == {
        "ok": True,
        "text": "",
        "finish_reason": "stop",
        "tokens_out": None,
        "latency_ms": body["latency_ms"],
        "streamed": True,
        # WO-05 step 1: the digest of the exact body posted to the gateway.
        "request_digest": content_hash(session.posts[-1]["json"]),
    }


# ── stream failure is contextual and terminal ────────────────────────────────


def test_a_stream_that_never_finished_is_not_a_completion(client, monkeypatch):
    """Tokens arrived and then the connection died. Returning the partial would
    hand the cascade a truncated reply it cannot tell from a finished one, so
    this raises the correlated stream failure without a blocking retry."""
    _wire(client, monkeypatch, _sse(*_tokens("half an ans")))  # no done frame
    with pytest.raises(RuntimeStreamError, match="stream_missing_done"):
        client.generate_completion_stream("hi")


def test_a_declined_gpu_preflight_reports_failure(client, monkeypatch):
    monkeypatch.setattr(client, "_gpu_clear_to_generate", lambda op: False)
    _wire(client, monkeypatch, _sse(*_tokens("x"), ("done", {})))
    with pytest.raises(RuntimeStreamError, match="generation_preflight_declined"):
        client.generate_completion_stream("hi")


def test_a_server_error_frame_reports_failure(client, monkeypatch):
    _wire(
        client,
        monkeypatch,
        _sse(("error", {"reason": "exceed_context_size", "detail": "engine context full"})),
    )
    with pytest.raises(RuntimeStreamError, match="exceed_context_size.*engine context full"):
        client.generate_completion_stream("hi")
    # The token iterator keeps its older, louder contract for callers that want
    # to tell a runtime that said "no" from one that said nothing.
    _wire(client, monkeypatch, _sse(("error", {"reason": "worker_error"})))
    with pytest.raises(RuntimeStreamError):
        list(client.generate_stream("hi"))


def test_a_sink_that_raises_costs_its_own_tokens_and_nothing_else(client, monkeypatch):
    _wire(client, monkeypatch, _sse(*_tokens("a", "b", "c"), ("done", {})))

    def _boom(token: str) -> None:
        raise RuntimeError("browser tab closed")

    body = client.generate_completion_stream("hi", on_token=_boom)
    # A dead socket is not cognition's problem: the reply is complete.
    assert body["text"] == "abc"


# ── a tool-carrying pass streams (operator 2026-09-05) ───────────────────────


def test_tool_call_fragments_reassemble_into_whole_calls(client, monkeypatch):
    """The gateway forwards ``delta.tool_calls`` as fragments keyed by index;
    the body carries the same OpenAI-shaped calls the blocking path returns."""
    _wire(
        client,
        monkeypatch,
        _sse(
            ("token", {"token": "Let me look."}),
            ("tool_call", {"index": 0, "id": "call_a", "name": "read_files", "arguments": '{"path": "a'}),
            ("tool_call", {"index": 1, "id": "call_b", "name": "web_search", "arguments": '{"query": "x"}'}),
            ("tool_call", {"index": 0, "arguments": '.py"}'}),
            ("done", {"finish_reason": "tool_calls"}),
        ),
    )
    seen: List[str] = []
    body = client.generate_completion_stream(
        "", messages=[{"role": "user", "content": "read a.py"}], tools=[{"type": "function"}],
        tool_choice="auto", on_token=seen.append,
    )
    assert seen == ["Let me look."]
    assert body["text"] == "Let me look."
    assert body["finish_reason"] == "tool_calls"
    assert body["tool_calls"] == [
        {"id": "call_a", "type": "function", "function": {"name": "read_files", "arguments": '{"path": "a.py"}'}},
        {"id": "call_b", "type": "function", "function": {"name": "web_search", "arguments": '{"query": "x"}'}},
    ]


def test_stream_payload_carries_the_transcript_and_the_tool_table(client, monkeypatch):
    session = _wire(client, monkeypatch, _sse(("done", {})))
    tools = [{"type": "function", "function": {"name": "read_files", "parameters": {}}}]
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    client.generate_completion_stream(
        "", pipeline_id=0, work_kind="operator_turn", tools=tools,
        tool_choice="auto", messages=messages
    )
    payload = session.posts[0]["json"]
    assert payload["tools"] == tools
    assert payload["tool_choice"] == "auto"
    assert payload["messages"] == messages
    assert payload["pipeline_id"] == 0
    assert payload["work_kind"] == "operator_turn"


def test_reasoning_reaches_its_sink_as_it_streams(client, monkeypatch):
    _wire(
        client,
        monkeypatch,
        _sse(
            ("reasoning", {"text": "first, "}),
            ("reasoning", {"text": "check the file"}),
            *_tokens("ok"),
            ("done", {}),
        ),
    )
    thought: List[str] = []
    body = client.generate_completion_stream("hi", on_reasoning=thought.append)
    assert thought == ["first, ", "check the file"]
    assert body["reasoning"] == "first, check the file"
    assert body["text"] == "ok"


def test_a_reasoning_sink_that_raises_costs_its_own_pieces_and_nothing_else(client, monkeypatch):
    _wire(client, monkeypatch, _sse(("reasoning", {"text": "a"}), ("reasoning", {"text": "b"}), *_tokens("x"), ("done", {})))

    def _boom(_piece: str) -> None:
        raise RuntimeError("dead tab")

    body = client.generate_completion_stream("hi", on_reasoning=_boom)
    assert body["text"] == "x"
    assert body["reasoning"] == "ab"


def test_a_stream_without_tool_calls_carries_none(client, monkeypatch):
    _wire(client, monkeypatch, _sse(*_tokens("plain"), ("done", {"finish_reason": "stop"})))
    body = client.generate_completion_stream("hi", tools=[{"type": "function"}])
    assert "tool_calls" not in body
    assert body["finish_reason"] == "stop"

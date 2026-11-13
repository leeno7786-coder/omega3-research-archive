"""The existing runtime client can carry a native tool-call transcript."""

from __future__ import annotations

from runtime.client import RuntimeClient


def test_generate_completion_forwards_messages_and_tool_controls(monkeypatch):
    seen: dict = {}

    def _fake(self, payload, *, op, timeout=None, local_only=False):
        seen.update(payload=payload, op=op)
        return {"ok": True, "text": "done", "finish_reason": "stop"}

    monkeypatch.setattr(RuntimeClient, "_cloud_or_local_generate", _fake)
    client = RuntimeClient(base_url="http://127.0.0.1:11434")
    turns = [
        {"role": "user", "content": "inspect the workspace"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_files", "arguments": '{"path":"README.md"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "read_files",
            "content": "Omega",
        },
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_files",
                "parameters": {"type": "object"},
            },
        }
    ]

    body = client.generate_completion(
        "",
        system="You are Omega.",
        messages=turns,
        tools=tools,
        tool_choice="auto",
        seed=7,
        pipeline_id=0,
        work_kind="operator_turn",
        cycle_id="cycle-1",
    )

    assert body == {"ok": True, "text": "done", "finish_reason": "stop"}
    assert seen["op"] == "generate_completion"
    assert seen["payload"]["messages"] == turns
    assert seen["payload"]["messages"] is not turns
    assert seen["payload"]["tool_choice"] == "auto"
    assert seen["payload"]["seed"] == 7
    assert seen["payload"]["pipeline_id"] == 0
    assert seen["payload"]["work_kind"] == "operator_turn"
    assert seen["payload"]["cycle_id"] == "cycle-1"


def test_generate_completion_legacy_prompt_shape_is_unchanged(monkeypatch):
    seen: dict = {}

    def _fake(self, payload, *, op, timeout=None, local_only=False):
        seen["payload"] = payload
        return {"ok": True, "text": "hello"}

    monkeypatch.setattr(RuntimeClient, "_cloud_or_local_generate", _fake)
    RuntimeClient(base_url="http://127.0.0.1:11434").generate_completion("hi", system="S")

    assert seen["payload"]["prompt"] == "hi"
    assert seen["payload"]["system"] == "S"
    assert not ({"messages", "tool_choice", "seed"} & seen["payload"].keys())
def test_missing_stream_completion_raises_but_empty_done_is_success(monkeypatch):
    import pytest
    from runtime.client import RuntimeClient, RuntimeStreamError
    client = RuntimeClient(base_url="http://127.0.0.1:11434")
    monkeypatch.setattr(client, "_stream_events", lambda *args, **kwargs: iter(()))
    with pytest.raises(RuntimeStreamError) as failure:
        client.generate_completion_stream("hello", cycle_id="c", job_id="j", pipeline_id=1)
    assert failure.value.context["job_id"] == "j"
    monkeypatch.setattr(client, "_stream_events", lambda *args, **kwargs: iter((("done", {}),)))
    assert client.generate_completion_stream("hello")["text"] == ""

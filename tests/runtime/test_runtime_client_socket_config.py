"""Runtime client routes configured service/tool operations through the socket catalog."""

from __future__ import annotations

from runtime.client import RuntimeClient


def test_runtime_client_uses_governed_service_and_tool_paths(monkeypatch):
    client = RuntimeClient("http://runtime.test")
    calls: list[tuple[str, str]] = []

    def post(path, payload, *, op):
        calls.append((path, op))
        if op == "transcribe":
            return {"ok": True, "text": "hello"}
        if op == "synthesise":
            return {"ok": True, "audio_b64": "YQ=="}
        return {"ok": True, "image_b64": "YQ=="}

    monkeypatch.setattr(client, "_post", post)
    assert client.transcribe(b"audio") == "hello"
    assert client.synthesise("hello") == b"a"
    assert client.image_generate("a cat") == b"a"

    assert calls == [
        ("/transcribe", "transcribe"),
        ("/synthesise", "synthesise"),
        ("/image/generate", "image_gen"),
    ]

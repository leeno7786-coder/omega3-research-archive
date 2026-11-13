"""Pin RuntimeClient.list_models to the OpenAI-style /v1/models endpoint.

GET /models returns a filtered big-model DICT (no top-level data[]); the full
model list lives at GET /v1/models as {"object":"list","data":[...]}. A consumer
expecting data[] off list_models parses empty unless it hits /v1/models.
"""

from __future__ import annotations

from runtime.client import RuntimeClient


def test_list_models_uses_v1_models_endpoint(monkeypatch):
    client = RuntimeClient(base_url="http://127.0.0.1:11434")
    calls: list[str] = []

    def _fake_get(path, op=None):
        calls.append(path)
        return {"object": "list", "data": [{"id": "gemma_e4b"}, {"id": "jackrong_qwen"}]}

    monkeypatch.setattr(client, "_get", _fake_get)

    result = client.list_models()
    assert calls == ["/v1/models"]  # not the filtered /models dict
    assert [m["id"] for m in result["data"]] == ["gemma_e4b", "jackrong_qwen"]

"""An explicit runtime model override rides the request body end to end."""

from __future__ import annotations

class _CaptureClient:
    def __init__(self):
        self.payloads = []

    def _capture_post(self, path, payload, **kwargs):
        self.payloads.append((path, dict(payload)))
        return {"ok": True, "text": "{}", "model_id": payload.get("model") or "bound"}


def test_generate_completion_carries_the_seat_override(monkeypatch):
    from runtime.client import RuntimeClient

    client = RuntimeClient(base_url="http://127.0.0.1:9")
    cap = _CaptureClient()
    monkeypatch.setattr(client, "_post", cap._capture_post)

    client.generate_completion("reflect", model="agents_a1_4b")
    path, payload = cap.payloads[-1]
    assert path == "/generate"
    assert payload["model"] == "agents_a1_4b"


def test_generate_completion_without_override_names_no_model(monkeypatch):
    # Per-episode cognition and chat send NO model — the binding decides.
    from runtime.client import RuntimeClient

    client = RuntimeClient(base_url="http://127.0.0.1:9")
    cap = _CaptureClient()
    monkeypatch.setattr(client, "_post", cap._capture_post)

    client.generate_completion("respond to the operator")
    _path, payload = cap.payloads[-1]
    assert "model" not in payload

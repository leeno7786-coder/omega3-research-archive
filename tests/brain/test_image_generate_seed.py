"""Regression: runtime_client.image_generate must omit `seed` when not
supplied.

The runtime's /image/generate schema requires `seed` to be a valid
integer and rejects an explicit null with 422 Unprocessable. The client
used to always send `"seed": seed` (None by default), so every default
image request 422'd. It must omit the key when None and pass an int when
given.
"""

from runtime.client import RuntimeClient


def _capture_payload(monkeypatch):
    captured = {}

    def fake_post(path, payload, **kwargs):
        captured["path"] = path
        captured["payload"] = payload
        # minimal valid-looking body so image_generate returns cleanly
        return {"ok": True, "image_b64": ""}

    client = RuntimeClient()
    monkeypatch.setattr(client, "_post", fake_post)
    return client, captured


def test_seed_omitted_when_none(monkeypatch):
    client, captured = _capture_payload(monkeypatch)
    client.image_generate("a red circle", width=256, height=256, steps=4)
    assert captured["path"] == "/image/generate"
    assert "seed" not in captured["payload"], (
        "seed=null must NOT be sent — the runtime rejects it with 422"
    )


def test_seed_passed_when_provided(monkeypatch):
    client, captured = _capture_payload(monkeypatch)
    client.image_generate("a blue square", seed=42)
    assert captured["payload"].get("seed") == 42


def test_core_image_params_always_present(monkeypatch):
    client, captured = _capture_payload(monkeypatch)
    client.image_generate("x", width=128, height=128, steps=2)
    p = captured["payload"]
    assert p["prompt"] == "x" and p["width"] == 128 and p["height"] == 128 and p["steps"] == 2

from __future__ import annotations

from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from hud.server import create_app


def _disable_unrelated_warmups(monkeypatch):
    monkeypatch.setattr("runtime.tts.warmup", lambda: None)
    monkeypatch.setattr("core.face_recall_warmup.warm_anchor_store", lambda: None)


def test_startup_does_not_call_retired_embedding_endpoint(monkeypatch):
    _disable_unrelated_warmups(monkeypatch)
    monkeypatch.setenv("OMEGA_RUNTIME_URL", "http://runtime.example:7777")
    calls: list[str] = []

    def _post(url: str, **_kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(httpx, "post", _post)
    app = create_app()

    with TestClient(app):
        pass

    assert calls == []

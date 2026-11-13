"""Pin runtime.client._post asymmetric connect/read timeout tuple."""

from __future__ import annotations

from unittest.mock import MagicMock

from runtime.client import RuntimeClient


def test_post_connect_timeout_tuple_both_branches(monkeypatch):
    captured: list[tuple[float, float]] = []

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"ok": True}

    mock_session = MagicMock()

    def _capture_post(url, json, timeout):
        captured.append(timeout)
        return mock_response

    mock_session.post = _capture_post

    client = RuntimeClient(base_url="http://127.0.0.1:11434")
    monkeypatch.setattr(client, "_ensure_session", lambda: mock_session)

    client._post("/generate", {"prompt": "x"}, op="generate", timeout=2.0)
    client._post("/generate", {"prompt": "x"}, op="generate", timeout=10.0)

    assert captured == [(2.0, 2.0), (5.0, 10.0)]

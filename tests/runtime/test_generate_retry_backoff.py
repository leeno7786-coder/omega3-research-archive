"""C — generate rides out the gemma-reload window with bounded retry-with-backoff.

After the fold-back handoff unloads jackrong, gemma lazy-reloads on the next /generate. That
first call can transiently time out / connection-fail / 503 while the worker spins up. The brain
must NOT treat that blip as a hard failure (which strands the apply decision) — it retries a
bounded number of times with backoff, then degrades. It must NEVER retry an HONEST failure
(ok=false / 4xx) and must NEVER raise.
"""

from __future__ import annotations

import pytest
import requests

import runtime.client as rc
from runtime import gpu_lease
from runtime.client import RuntimeClient


class _Resp:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


class _Session:
    """Replays a scripted sequence of responses/exceptions for .post()."""

    def __init__(self, sequence: list) -> None:
        self._seq = list(sequence)
        self.calls = 0

    def post(self, url, json=None, timeout=None):  # noqa: A002 - mirror requests API
        item = self._seq[min(self.calls, len(self._seq) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    gpu_lease._reset_for_tests()  # never deferred in these tests
    monkeypatch.setenv("OMEGA_GPU_BUILD_LEASE", "1")
    monkeypatch.setenv("OMEGA_GENERATE_RETRIES", "2")
    monkeypatch.setattr(rc.time, "sleep", lambda *_a, **_k: None)  # no real backoff delay
    yield
    gpu_lease._reset_for_tests()


def _client(monkeypatch, sequence):
    client = RuntimeClient(base_url="http://127.0.0.1:11434")
    sess = _Session(sequence)
    monkeypatch.setattr(client, "_ensure_session", lambda: sess)
    return client, sess


def test_retries_transport_error_then_succeeds(monkeypatch):
    client, sess = _client(
        monkeypatch,
        [
            requests.ConnectionError("refused"),
            requests.ConnectionError("refused"),
            _Resp(200, {"ok": True, "text": "done"}),
        ],
    )
    assert client.generate("x") == "done"
    assert sess.calls == 3  # 1 + 2 retries


def test_retries_503_runtime_not_ready_then_succeeds(monkeypatch):
    client, sess = _client(
        monkeypatch,
        [
            _Resp(503, {"ok": False, "reason": "runtime_not_ready"}),
            _Resp(200, {"ok": True, "text": "ok"}),
        ],
    )
    assert client.generate("x") == "ok"
    assert sess.calls == 2
def test_gives_up_after_retries_and_returns_none(monkeypatch):
    client, sess = _client(monkeypatch, [requests.Timeout("slow")])
    assert client.generate("x") is None
    assert sess.calls == 3  # 1 + 2 retries, then degrade


def test_does_not_retry_honest_ok_false(monkeypatch):
    client, sess = _client(monkeypatch, [_Resp(200, {"ok": False, "reason": "refused_by_model"})])
    assert client.generate("x") is None
    assert sess.calls == 1  # honest failure -> no retry


def test_does_not_retry_4xx(monkeypatch):
    client, sess = _client(monkeypatch, [_Resp(400, {"ok": False, "reason": "bad_request"})])
    assert client.generate("x") is None
    assert sess.calls == 1


def test_generate_completion_also_retries(monkeypatch):
    client, sess = _client(
        monkeypatch,
        [
            requests.ConnectionError("refused"),
            _Resp(200, {"ok": True, "text": "done", "tool_calls": None}),
        ],
    )
    body = client.generate_completion("x")
    assert body and body.get("text") == "done"
    assert sess.calls == 2

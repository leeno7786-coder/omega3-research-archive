from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

from hud.server import create_app


class _FakeUpload:
    filename = "sample.webm"

    async def read(self, _size: int) -> bytes:
        return b"fake-audio"


def _route(app, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def test_stt_transcribe_runs_blocking_runtime_work_off_event_loop(monkeypatch):
    import runtime.stt as stt

    app = create_app()
    endpoint = _route(app, "/api/stt/transcribe", "POST")
    event_loop_thread = threading.get_ident()
    worker_threads: list[int] = []

    monkeypatch.setattr(stt, "is_available", lambda: True)
    monkeypatch.setattr(stt, "infer_audio_format", lambda _path: "webm")

    def fake_transcribe_with_segments(*_args, **_kwargs):
        worker_threads.append(threading.get_ident())
        return SimpleNamespace(
            text="hello",
            segments=[],
            detected_language="en",
            language_probability=1.0,
            duration=0.1,
        )

    monkeypatch.setattr(stt, "transcribe_with_segments", fake_transcribe_with_segments)

    response = asyncio.run(endpoint(audio=_FakeUpload(), language="en", task=None))
    body = json.loads(response.body.decode("utf-8"))

    assert body["ok"] is True
    assert body["text"] == "hello"
    assert worker_threads
    assert all(thread_id != event_loop_thread for thread_id in worker_threads)

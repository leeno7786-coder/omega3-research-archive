from __future__ import annotations

import asyncio
import threading

from fastapi.testclient import TestClient

from hud.server import create_app


def _route(app, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def test_tts_speak_runs_blocking_synthesis_off_event_loop(monkeypatch):
    import runtime.tts as tts

    app = create_app()
    endpoint = _route(app, "/api/tts/speak", "POST")
    event_loop_thread = threading.get_ident()
    availability_threads: list[int] = []
    worker_threads: list[int] = []

    def fake_is_available():
        availability_threads.append(threading.get_ident())
        return True

    monkeypatch.setattr(tts, "is_available", fake_is_available)

    def fake_synthesize(*_args, **_kwargs):
        worker_threads.append(threading.get_ident())
        return b"RIFFfakeWAVE"

    monkeypatch.setattr(tts, "synthesize", fake_synthesize)
    monkeypatch.setattr(
        "hud.server.hud_settings.load_voice_settings",
        lambda: {
            "settings": {
                "tts_voice": "af_bella",
                "tts_speed": 1.0,
                "tts_pitch": 1.0,
            }
        },
    )

    response = asyncio.run(endpoint(payload={"text": "Omega is online."}))

    assert response.body == b"RIFFfakeWAVE"
    assert availability_threads
    assert all(thread_id != event_loop_thread for thread_id in availability_threads)
    assert worker_threads
    assert all(thread_id != event_loop_thread for thread_id in worker_threads)


def test_tts_stream_returns_incremental_pcm_contract(monkeypatch):
    import runtime.tts as tts

    app = create_app()
    seen: dict[str, object] = {}

    monkeypatch.setattr(tts, "is_available", lambda: True)

    async def fake_synthesize_stream(text, *, voice=None, length_scale=None):
        seen.update(text=text, voice=voice, length_scale=length_scale)
        yield b"pcm-a"
        yield b"pcm-b"

    monkeypatch.setattr(tts, "synthesize_stream", fake_synthesize_stream)
    monkeypatch.setattr(
        "hud.server.hud_settings.load_voice_settings",
        lambda: {
            "settings": {
                "tts_voice": "af_bella",
                "tts_speed": 1.0,
                "tts_pitch": 1.0,
            }
        },
    )

    with TestClient(app) as client:
        response = client.post("/api/tts/stream", json={"text": "Omega is online."})

    assert response.status_code == 200
    assert response.content == b"pcm-apcm-b"
    assert response.headers["content-type"] == "audio/pcm"
    assert response.headers["x-omega-tts-sample-rate"] == "24000"
    assert response.headers["x-omega-tts-channels"] == "1"
    assert response.headers["x-omega-tts-sample-width"] == "2"
    assert response.headers["x-omega-tts-pitch-applied"] == "false"
    assert seen == {
        "text": "Omega is online.",
        "voice": "af_bella",
        "length_scale": 1.0,
    }


def test_tts_status_does_not_block_voice_transport(monkeypatch):
    import runtime.tts as tts

    endpoint = _route(create_app(), "/api/tts/status", "GET")
    loop_thread = threading.get_ident()
    calls = []

    def status():
        calls.append(threading.get_ident())
        return {"available": True}

    monkeypatch.setattr(tts, "status", status)
    asyncio.run(endpoint())
    assert calls and all(t != loop_thread for t in calls)

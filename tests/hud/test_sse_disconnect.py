"""Phase C #52 — the SSE event generator must STOP when the client
disconnects instead of looping forever.

hud.events.sse_stream's `while True` loop only sleeps + scans the ring buffer;
it never notices the browser closing the EventSource. Without a disconnect
check the coroutine keeps running (waking every 0.4s) for the life of the
process — one leaked task per dropped connection.

Fix: sse_stream gains an optional `is_disconnected` awaitable predicate (the
HUD passes Starlette's Request.is_disconnected). When it reports the client is
gone, the generator returns promptly. The HUD endpoint must forward it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hud import events as hud_events  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    hud_events._reset_for_tests()
    yield
    hud_events._reset_for_tests()


def test_sse_stream_stops_when_client_disconnected():
    async def run():
        async def disconnected():
            return True  # client already gone

        gen = hud_events.sse_stream(start_seq=0, is_disconnected=disconnected)
        chunks = []
        async for chunk in gen:
            chunks.append(chunk)
        return chunks

    # If the generator ignores disconnect it loops forever -> wait_for raises.
    asyncio.run(asyncio.wait_for(run(), timeout=3.0))


def test_sse_stream_keeps_running_while_connected_then_closes():
    async def run():
        calls = {"n": 0}

        async def connected():
            calls["n"] += 1
            return False  # still connected

        gen = hud_events.sse_stream(start_seq=0, is_disconnected=connected)
        # Pump a couple of loop iterations, then close it ourselves — it must
        # NOT terminate on its own while the client is connected.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(gen.__anext__(), timeout=1.2)
        await gen.aclose()
        assert calls["n"] >= 1  # the predicate was actually consulted

    asyncio.run(run())


def test_api_events_endpoint_forwards_disconnect_predicate(monkeypatch):
    # Wiring regression: the /api/events endpoint must hand its Request's
    # is_disconnected to sse_stream, or the leak fix never reaches production.
    captured = {}

    def fake_stream(start_seq=0, is_disconnected=None):
        captured["start_seq"] = start_seq
        captured["is_disconnected"] = is_disconnected

        async def _gen():
            if False:  # pragma: no cover - empty async generator
                yield ""

        return _gen()

    monkeypatch.setattr(hud_events, "sse_stream", fake_stream)

    from fastapi.testclient import TestClient
    from hud.server import create_app

    client = TestClient(create_app())
    with client.stream("GET", "/api/events") as resp:
        assert resp.status_code == 200

    assert captured.get("is_disconnected") is not None
    assert callable(captured["is_disconnected"])

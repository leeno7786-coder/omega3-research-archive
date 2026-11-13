"""ASGI disconnects must close audio during reads and backpressured sends."""
import asyncio
import ssl

import pytest

from runtime.tts_stream import PCMResponse
from runtime.tts_stream import pcm_client


def test_local_pcm_client_does_not_load_an_unused_https_trust_store(monkeypatch):
    seen = []
    monkeypatch.setattr("runtime.tts_stream.httpx.AsyncClient", lambda **kwargs: seen.append(kwargs))
    pcm_client(url="http://127.0.0.1:12345")
    assert isinstance(seen[0]["verify"], ssl.SSLContext)
    assert seen[0]["verify"].verify_mode == ssl.CERT_REQUIRED
    assert seen[0]["verify"].check_hostname is True
    assert seen[0]["verify"].cert_store_stats()["x509_ca"] == 0
    pcm_client(url="https://runtime.example")
    assert seen[1]["verify"] is True


@pytest.mark.parametrize("phase", ["before_audio", "during_read", "during_send"])
def test_disconnect_closes_upstream(phase):
    async def run():
        checkpoint = asyncio.Event()
        closed = asyncio.Event()

        async def pcm():
            try:
                if phase == "before_audio":
                    checkpoint.set()
                    await asyncio.Event().wait()
                yield b"\0\0"
                checkpoint.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

        async def receive():
            await checkpoint.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if phase == "during_send" and message["type"] == "http.response.body":
                checkpoint.set()
                await asyncio.Event().wait()

        response = PCMResponse(pcm())
        await asyncio.wait_for(response({}, receive, send), 1)
        assert closed.is_set()

    asyncio.run(run())

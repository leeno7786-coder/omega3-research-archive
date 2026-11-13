"""Audio transport failures must not be successful empty/partial responses."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from runtime import qwen_tts
from tests.runtime.test_local_services import _FakeDme, _FakePackedDme, _FakeSpeech
from runtime.local_services import create_app


async def collect(stream):
    if hasattr(stream, "__aiter__"):
        return [chunk async for chunk in stream]
    return list(stream)


def test_qwen_preserves_error_body():
    def failure(request):
        return httpx.Response(400, json={"error": "unknown speaker"})

    engine = qwen_tts.QwenTTSProcess.__new__(qwen_tts.QwenTTSProcess)
    engine.base_url = "http://qwen.test"
    engine.config = {}
    engine._client = httpx.Client(transport=httpx.MockTransport(failure))
    engine._async_client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(failure))
    with pytest.raises(RuntimeError, match="unknown speaker"):
        asyncio.run(collect(engine.synthesise_stream("Hello", "invalid", 1)))


def test_qwen_stream_cancels_a_pending_read():
    async def run():
        waiting = asyncio.Event()
        closed = asyncio.Event()

        class PCM(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"\0\0"
                waiting.set()
                await asyncio.Event().wait()

            async def aclose(self):
                closed.set()

        engine = qwen_tts.QwenTTSProcess.__new__(qwen_tts.QwenTTSProcess)
        engine.base_url = "http://qwen.test"
        engine.config = {}
        engine._async_client_factory = lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=PCM())))
        stream = engine.synthesise_stream("Hello", "Aiden", 1)
        assert hasattr(stream, "__aiter__"), "blocking TTS reads cannot propagate disconnect cancellation"
        assert await anext(stream) == b"\0\0"
        read = asyncio.create_task(anext(stream))
        await asyncio.wait_for(waiting.wait(), 1)
        read.cancel()
        with pytest.raises(asyncio.CancelledError):
            await read
        assert closed.is_set(), "cancelling a read must close the Qwen connection"

    asyncio.run(run())


@pytest.mark.parametrize("partial", [False, True])
def test_runtime_stream_failure_is_not_clean_eof(partial):
    class BrokenSpeech(_FakeSpeech):
        async def synthesise_stream(self, *args):
            if partial:
                yield b"\0\0"
            raise RuntimeError("codec crashed")

    app = create_app(dme=_FakeDme(), packed_dme=_FakePackedDme(),
                     stt=_FakeSpeech(), tts=BrokenSpeech(), warmup=False)
    with TestClient(app) as client:
        if partial:
            with pytest.raises(ExceptionGroup) as failure:
                client.post("/synthesise/stream", json={"text": "Hello"})
            assert str(failure.value.exceptions[0]) == "codec crashed"
        else:
            response = client.post("/synthesise/stream", json={"text": "Hello"})
            assert response.status_code == 502
            assert "codec crashed" in response.text

"""Cancellation-aware PCM transport shared by the runtime and HUD."""
from __future__ import annotations

import logging
import ssl
from urllib.parse import urlsplit

import anyio
import httpx
from starlette.responses import JSONResponse, StreamingResponse

log = logging.getLogger("omega.runtime.tts_stream")


async def http_pcm(url, payload, *, client_factory):
    """Own the upstream connection until EOF, failure, or downstream cancellation."""
    async with client_factory() as client:
        async with client.stream("POST", url, json=payload) as response:
            if response.status_code >= 400:
                detail = (await response.aread()).decode("utf-8", errors="replace")[:240]
                raise RuntimeError(f"TTS stream failed ({response.status_code}): {detail}")
            seen = False
            async for chunk in response.aiter_bytes():
                if chunk:
                    seen = True
                    yield chunk
            if not seen:
                raise RuntimeError("TTS stream produced empty audio")


def pcm_client(timeout=180, *, url: str):
    # httpx builds a TLS context even for HTTP. Loading Windows' trust roots
    # per utterance costs ~0.5 seconds per hop. HTTP needs no trust-store scan;
    # retain CERT_REQUIRED/hostname checks and normal trust roots for HTTPS.
    verify = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT) if urlsplit(url).scheme == "http" else True
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=5), trust_env=False, verify=verify,
    )


class PCMResponse(StreamingResponse):
    """Delay success until audio arrives; close upstream even during a blocked send.

    Listen for disconnects while waiting for the first byte as well as during
    playback. A sync iterator in a worker thread cannot cancel an upstream read.
    After headers, exceptions deliberately abort HTTP rather than forge clean EOF.
    """

    def __init__(self, stream, *, headers=None):
        super().__init__(stream, media_type="audio/pcm", headers={
            "Cache-Control": "no-store",
            "X-Omega-TTS-Sample-Rate": "24000",
            "X-Omega-TTS-Channels": "1",
            "X-Omega-TTS-Sample-Width": "2",
            **(headers or {}),
        })

    async def stream_response(self, send):
        try:
            first = await anext(self.body_iterator)
            if not first:
                raise RuntimeError("TTS stream produced empty audio")
        except Exception as exc:
            log.error("TTS stream failed before audio: %s", exc)
            error = JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=502)
            await send({"type": "http.response.start", "status": 502, "headers": error.raw_headers})
            await send({"type": "http.response.body", "body": error.body})
            return
        await send({"type": "http.response.start", "status": 200, "headers": self.raw_headers})
        await send({"type": "http.response.body", "body": first, "more_body": True})
        async for chunk in self.body_iterator:
            if chunk:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def __call__(self, scope, receive, send):
        try:
            async with anyio.create_task_group() as group:
                async def produce():
                    await self.stream_response(send)
                    group.cancel_scope.cancel()

                group.start_soon(produce)
                await self.listen_for_disconnect(receive)
                group.cancel_scope.cancel()
        finally:
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()

"""Response stream cleanup owns iterators as well as the HTTP connection."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import httpcore2
import httpx2
import pytest
from httpcore2._async.connection_pool import PoolByteStream
from httpcore2._async.http11 import HTTP11ConnectionByteStream
from httpx2._transports.default import AsyncResponseStream

from mcp_platform.client_contracts import McpClientError, McpClientErrorReason
from mcp_platform.transports.streamable_http import _BoundedResponseStream


class _Body(httpx2.AsyncByteStream):
    def __init__(self) -> None:
        self.iterator_closed = False
        self.close_calls = 0

    async def __aiter__(self):
        try:
            yield b"first"
            yield b"second"
        finally:
            self.iterator_closed = True

    async def aclose(self) -> None:
        self.close_calls += 1


def _bounded(stream: httpx2.AsyncByteStream) -> _BoundedResponseStream:
    return _BoundedResponseStream(
        stream,
        request=httpx2.Request("GET", "https://fixture.invalid"),
        limit=100,
        duration_seconds=1,
        shutdown_seconds=0.05,
    )


def test_early_consumer_exit_finalizes_owned_iterator_before_returning() -> None:
    async def exercise() -> None:
        body = _Body()
        bounded = _bounded(body)
        iterator = bounded.__aiter__()
        assert await anext(iterator) == b"first"

        await iterator.aclose()

        assert body.iterator_closed
        assert body.close_calls == 1
        await bounded.aclose()
        assert body.close_calls == 1

    asyncio.run(exercise())


def test_external_close_finalizes_iterator_paused_at_a_yield() -> None:
    async def exercise() -> None:
        body = _Body()
        bounded = _bounded(body)
        iterator = bounded.__aiter__()
        assert await anext(iterator) == b"first"

        await bounded.aclose()

        assert body.iterator_closed
        assert body.close_calls == 1
        await iterator.aclose()
        assert body.close_calls == 1

    asyncio.run(exercise())


def test_partial_real_httpcore_stream_does_not_fail_at_async_generator_shutdown() -> None:
    """Use installed HTTP byte-stream implementations, without any network."""

    errors: list[dict[str, object]] = []
    events: list[str] = []

    class Connection:
        async def _receive_response_body(self, **_kwargs):
            try:
                yield b"first"
                yield b"second"
            finally:
                events.append("body_finalized")

        async def _response_closed(self) -> None:
            events.append("connection_released")

    async def close_connections(_connections) -> None:
        events.append("pool_released")

    async def exercise() -> None:
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: errors.append(context)
        )
        pool_request = object()
        pool = SimpleNamespace(
            _optional_thread_lock=nullcontext(),
            _requests=[pool_request],
            _assign_requests_to_connections=lambda: [],
            _close_connections=close_connections,
        )
        core_stream = HTTP11ConnectionByteStream(
            Connection(), httpcore2.Request("GET", "https://fixture.invalid")
        )
        body = AsyncResponseStream(PoolByteStream(core_stream, pool_request, pool))
        iterator = _bounded(body).__aiter__()
        assert await anext(iterator) == b"first"
        await iterator.aclose()
        assert pool._requests == []

    asyncio.run(exercise())

    assert events.count("connection_released") == 1
    assert events.count("pool_released") == 1
    assert events.count("body_finalized") == 1
    assert errors == []


def test_external_close_aborts_active_read_before_finalizing_iterator() -> None:
    async def exercise() -> None:
        reading = asyncio.Event()
        abort = asyncio.Event()

        class ReadingBody(_Body):
            async def __aiter__(self):
                try:
                    yield b"first"
                    reading.set()
                    await abort.wait()
                    raise httpx2.ReadError("response_closed")
                finally:
                    self.iterator_closed = True

            async def aclose(self) -> None:
                self.close_calls += 1
                abort.set()

        body = ReadingBody()
        bounded = _bounded(body)
        iterator = bounded.__aiter__()
        assert await anext(iterator) == b"first"
        reader = asyncio.create_task(anext(iterator))
        await reading.wait()

        await bounded.aclose()
        with pytest.raises(httpx2.ReadError, match="^response_closed$"):
            await reader

        assert body.iterator_closed
        assert body.close_calls == 1
        await bounded.aclose()
        assert body.close_calls == 1

    asyncio.run(exercise())


def test_iterator_finalization_keeps_deadline_and_one_recoverable_cleanup_owner() -> None:
    async def exercise() -> None:
        finalizing = asyncio.Event()
        release = asyncio.Event()

        class HeldBody(_Body):
            finalization_count = 0

            async def __aiter__(self):
                try:
                    yield b"first"
                finally:
                    self.finalization_count += 1
                    finalizing.set()
                    await release.wait()
                    self.iterator_closed = True

        body = HeldBody()
        bounded = _bounded(body)
        iterator = bounded.__aiter__()
        assert await anext(iterator) == b"first"
        closing_iterator = asyncio.create_task(iterator.aclose())
        try:
            await asyncio.wait_for(finalizing.wait(), timeout=0.5)
            with pytest.raises(McpClientError) as raised:
                await bounded.aclose()
            assert raised.value.reason is McpClientErrorReason.CLEANUP_REQUIRED
            assert not body.iterator_closed
            assert body.finalization_count == 1
            assert body.close_calls == 1
        finally:
            release.set()
            await closing_iterator
            await bounded.aclose()
        assert body.iterator_closed
        assert body.finalization_count == 1
        assert body.close_calls == 1

    asyncio.run(exercise())


def test_resumed_consumer_cannot_enter_an_iterator_being_finalized() -> None:
    async def exercise() -> None:
        finalizing = asyncio.Event()
        release = asyncio.Event()
        reads: list[str] = []

        class ObservedIterator:
            def __init__(self, iterator) -> None:
                self.iterator = iterator

            def __aiter__(self):
                return self

            async def __anext__(self):
                reads.append("read")
                return await anext(self.iterator)

            async def aclose(self) -> None:
                await self.iterator.aclose()

        class HeldBody(_Body):
            def __aiter__(self):
                return ObservedIterator(self.body())

            async def body(self):
                try:
                    yield b"first"
                    yield b"second"
                finally:
                    finalizing.set()
                    await release.wait()
                    self.iterator_closed = True

        body = HeldBody()
        bounded = _bounded(body)
        iterator = bounded.__aiter__()
        assert await anext(iterator) == b"first"
        closing = asyncio.create_task(bounded.aclose())
        try:
            await asyncio.wait_for(finalizing.wait(), timeout=0.5)
            with pytest.raises(McpClientError) as raised:
                await anext(iterator)
            assert raised.value.reason is McpClientErrorReason.CLEANUP_REQUIRED
            assert reads == ["read"]
            with pytest.raises(McpClientError) as closed:
                await closing
            assert closed.value.reason is McpClientErrorReason.CLEANUP_REQUIRED
        finally:
            release.set()
            await asyncio.gather(closing, return_exceptions=True)
            await bounded.aclose()
        assert body.iterator_closed
        assert body.close_calls == 1

    asyncio.run(exercise())

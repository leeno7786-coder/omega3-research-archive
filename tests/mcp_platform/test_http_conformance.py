from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx2
from mcp import Client, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION

ROOT = Path(__file__).resolve().parents[2]
FAKE_SERVER = Path(__file__).with_name("fake_stdio_server.py")
SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"
EXPECTED_SERVER_INFO = {"name": "omega.fake", "version": "1.0.0"}


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def _start_http_peer(port: int) -> asyncio.subprocess.Process:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(FAKE_SERVER),
        "--http",
        "--port",
        str(port),
        cwd=ROOT,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        if process.returncode is not None:
            assert process.stderr is not None
            raise AssertionError((await process.stderr.read()).decode("utf-8"))
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("loopback current MCP server did not start")
            await asyncio.sleep(0.025)
            continue
        writer.close()
        await writer.wait_closed()
        del reader
        return process


async def _stop_http_peer(
    process: asyncio.subprocess.Process,
    port: int,
) -> tuple[bytes, bytes]:
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        process.kill()
        await asyncio.wait_for(process.wait(), timeout=2)
        raise AssertionError("loopback current MCP server exceeded shutdown bound")
    assert process.stdout is not None and process.stderr is not None
    stdout, stderr = await process.stdout.read(), await process.stderr.read()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port),
            timeout=0.25,
        )
    except (OSError, TimeoutError):
        pass
    else:
        writer.close()
        await writer.wait_closed()
        raise AssertionError("loopback listener survived server shutdown")
    return stdout, stderr


def _client_info() -> types.Implementation:
    return types.Implementation(name="omega-task12-conformance", version="1.0.0")


async def _stdio_echo(text: str) -> dict[str, Any]:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(FAKE_SERVER), "--stdio"],
        env={"PYTHONUTF8": "1"},
        cwd=ROOT,
    )
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        async with Client(
            stdio_client(params, errlog=errlog),
            mode=MCP_PROTOCOL_VERSION,
            client_info=_client_info(),
            cache=None,
        ) as client:
            result = await client.call_tool("omega.fake.echo", {"text": text})
            assert result.meta == {SERVER_INFO_KEY: EXPECTED_SERVER_INFO}
            assert result.is_error is False
            assert result.structured_content is not None
            return result.structured_content


async def _http_echo(port: int, text: str) -> dict[str, Any]:
    async with Client(
        f"http://127.0.0.1:{port}/mcp",
        mode=MCP_PROTOCOL_VERSION,
        client_info=_client_info(),
        cache=None,
    ) as client:
        result = await client.call_tool("omega.fake.echo", {"text": text})
        assert result.meta == {SERVER_INFO_KEY: EXPECTED_SERVER_INFO}
        assert result.is_error is False
        assert result.structured_content is not None
        return result.structured_content


def test_same_current_operation_result_over_real_stdio_and_loopback_http() -> None:
    # Catches transport-specific server behavior hidden by ASGI-only mocks.
    async def exercise() -> None:
        port = _free_loopback_port()
        process = await _start_http_peer(port)
        baseline_tasks = set(asyncio.all_tasks())
        try:
            stdio_result = await _stdio_echo("parity")
            http_result = await _http_echo(port, "parity")
            assert stdio_result == http_result == {"text": "parity"}
        finally:
            stdout, stderr = await _stop_http_peer(process, port)
        assert stdout == b""
        visible_stderr = stderr.decode("utf-8", errors="strict")
        assert "Bearer " not in visible_stderr
        assert "provider-token-sentinel" not in visible_stderr
        await asyncio.sleep(0)
        survivors = {
            task
            for task in asyncio.all_tasks() - baseline_tasks
            if task is not asyncio.current_task() and not task.done()
        }
        assert survivors == set()

    asyncio.run(exercise())


def test_wrong_http_protocol_is_stably_rejected_without_session_or_listener_leak() -> None:
    # Catches the public HTTP app falling through to a handshake-era session manager.
    async def exercise() -> None:
        port = _free_loopback_port()
        process = await _start_http_peer(port)
        try:
            async with httpx2.AsyncClient() as raw:
                response = await raw.post(
                    f"http://127.0.0.1:{port}/mcp",
                    headers={"Mcp-Protocol-Version": "2026-07-27"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2026-07-27",
                            "clientInfo": {"name": "retired", "version": "1"},
                            "capabilities": {},
                        },
                    },
                )
                assert response.status_code == 400
                assert response.headers.get("mcp-session-id") is None
                assert response.json() == {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32022,
                        "message": "Unsupported protocol version",
                        "data": {
                            "supported": [MCP_PROTOCOL_VERSION],
                            "requested": "2026-07-27",
                        },
                    },
                }
            assert await _http_echo(port, "still-current") == {"text": "still-current"}
        finally:
            stdout, stderr = await _stop_http_peer(process, port)
        assert stdout == b""
        visible_stderr = stderr.decode("utf-8", errors="strict")
        assert "Bearer " not in visible_stderr
        assert "provider-token-sentinel" not in visible_stderr

    asyncio.run(exercise())


def test_real_http_overlapping_requests_preserve_ids_payloads_and_shutdown() -> None:
    # Catches per-request HTTP dispatch serializing or crossing concurrent payloads.
    async def exercise() -> None:
        port = _free_loopback_port()
        process = await _start_http_peer(port)
        baseline_tasks = set(asyncio.all_tasks())
        try:
            async with httpx2.AsyncClient() as raw:

                async def invoke(request_id: int, text: str) -> dict[str, Any]:
                    response = await raw.post(
                        f"http://127.0.0.1:{port}/mcp",
                        headers={
                            "Mcp-Protocol-Version": MCP_PROTOCOL_VERSION,
                            "Mcp-Method": "tools/call",
                            "Mcp-Name": "omega.fake.concurrent",
                        },
                        json={
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "tools/call",
                            "params": {
                                "name": "omega.fake.concurrent",
                                "arguments": {"text": text},
                                "_meta": {
                                    "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
                                    "io.modelcontextprotocol/clientInfo": {
                                        "name": "omega-task12-concurrency",
                                        "version": "1.0.0",
                                    },
                                    "io.modelcontextprotocol/clientCapabilities": {},
                                },
                            },
                        },
                    )
                    assert response.status_code == 200, response.text
                    return response.json()

                documents = await asyncio.wait_for(
                    asyncio.gather(invoke(301, "first"), invoke(404, "second")),
                    timeout=5,
                )
            assert all("result" in document for document in documents)
            by_id = {document["id"]: document["result"] for document in documents}
            assert by_id[301]["structuredContent"] == {"text": "first", "peak": 2}
            assert by_id[404]["structuredContent"] == {"text": "second", "peak": 2}
        finally:
            stdout, stderr = await _stop_http_peer(process, port)
        assert stdout == b""
        visible_stderr = stderr.decode("utf-8", errors="strict")
        assert "provider-token-sentinel" not in visible_stderr
        await asyncio.sleep(0)
        survivors = {
            task
            for task in asyncio.all_tasks() - baseline_tasks
            if task is not asyncio.current_task() and not task.done()
        }
        assert survivors == set()

    asyncio.run(exercise())

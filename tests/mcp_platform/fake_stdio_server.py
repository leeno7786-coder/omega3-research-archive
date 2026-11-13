"""Real current-protocol first-party peer for stdio and HTTP conformance tests."""

from __future__ import annotations

import argparse
import sys
import warnings
from collections.abc import Mapping
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import anyio  # noqa: E402
from mcp import types  # noqa: E402
from mcp.server import CacheHint  # noqa: E402
from mcp.server.subscriptions import ToolsListChanged  # noqa: E402
from pydantic.warnings import TypedDictExtraConfigWarning  # noqa: E402

from mcp_platform.canonical import canonical_bytes, sha256_hex  # noqa: E402
from mcp_platform.package_contracts import ResourceCatalog, ToolCatalog  # noqa: E402
from mcp_platform.sdk import FirstPartyMcpServer, PromptRequest  # noqa: E402
from mcp_platform.sdk import server as server_module  # noqa: E402

with warnings.catch_warnings():
    # mcp-types 2.0.0's public RequestParams metadata is an open TypedDict;
    # Pydantic warns when Omega's closed Tasks model inherits its public base.
    warnings.filterwarnings(
        "ignore",
        message=r"TypedDict class 'RequestParamsMeta' allows extra items, but 'extra' configuration is set to `'forbid'`\.",
        category=TypedDictExtraConfigWarning,
    )
    from mcp_platform.tasks import (  # noqa: E402
        TASKS_EXTENSION_IDENTIFIER,
        CancelTaskParams,
        CancelTaskResult,
        CreateTaskResult,
        GetTaskParams,
        GetTaskResult,
        UpdateTaskParams,
        UpdateTaskResult,
    )

_CREATED_AT = "2026-08-17T00:00:00Z"
_UPDATED_AT = "2026-08-17T00:00:01Z"


def _schema() -> dict[str, object]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    }


def _concurrent_output_schema() -> dict[str, object]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "peak": {"type": "integer", "minimum": 2, "maximum": 2},
        },
        "required": ["text", "peak"],
        "additionalProperties": False,
    }


def _catalogs() -> tuple[ToolCatalog, ResourceCatalog]:
    schema = _schema()
    tools: list[dict[str, object]] = []
    for name, title in (
        ("omega.fake.echo", "Echo"),
        ("omega.fake.concurrent", "Concurrent"),
        ("omega.fake.input", "Input"),
        ("omega.fake.task", "Task"),
        ("omega.fake.notify", "Notify"),
        ("omega.fake.failure", "Failure"),
    ):
        output_schema = _concurrent_output_schema() if name.endswith(".concurrent") else schema
        tools.append(
            {
                "name": name,
                "title": title,
                "input_schema": schema,
                "output_schema": output_schema,
                "input_schema_sha256": sha256_hex(canonical_bytes(schema)),
                "output_schema_sha256": sha256_hex(canonical_bytes(output_schema)),
                "capability_tags": ["fake.read"],
                "provider_extension_tags": (
                    [TASKS_EXTENSION_IDENTIFIER] if name == "omega.fake.task" else []
                ),
                "risk_class": "read_only",
                "effect": name.rsplit(".", 1)[-1],
                "requires_revision": False,
                "max_result_bytes": 512,
                "allowed_mime_types": ["application/json", "text/plain"],
            }
        )
    return (
        ToolCatalog.from_dict(
            {
                "catalog_version": 1,
                "server_id": "omega.fake",
                "server_version": "1.0.0",
                "tools": tools,
            }
        ),
        ResourceCatalog.from_dict(
            {
                "catalog_version": 1,
                "server_id": "omega.fake",
                "server_version": "1.0.0",
                "resources": [
                    {
                        "uri": "omega://fake/help",
                        "name": "help",
                        "mime_type": "text/plain",
                        "max_bytes": 64,
                        "capability_tags": ["fake.read"],
                    },
                    {
                        "uri": "omega://fake/failure",
                        "name": "failure",
                        "mime_type": "text/plain",
                        "max_bytes": 64,
                        "capability_tags": ["fake.read"],
                    },
                ],
                "resource_templates": [
                    {
                        "uri_template": "omega://fake/items/{id}",
                        "name": "item",
                        "mime_type": "application/json",
                        "max_bytes": 64,
                        "capability_tags": ["fake.read"],
                    }
                ],
            }
        ),
    )


def _server() -> FirstPartyMcpServer:
    prompt = types.Prompt(
        name="omega.fake.summarize",
        title="Summarize",
        arguments=[types.PromptArgument(name="text", required=True)],
    )
    server = FirstPartyMcpServer(
        *_catalogs(),
        prompts=(prompt,),
        extensions={TASKS_EXTENSION_IDENTIFIER: {}},
        cache_hints={"tools/list": CacheHint(ttl_ms=1_250, scope="public")},
        enable_subscriptions=True,
    )

    async def echo(arguments: Mapping[str, object]) -> Mapping[str, object]:
        return {"text": arguments["text"]}

    concurrent_active = 0
    concurrent_peak = 0
    concurrent_release = anyio.Event()

    async def concurrent(arguments: Mapping[str, object]) -> Mapping[str, object]:
        nonlocal concurrent_active, concurrent_peak
        concurrent_active += 1
        concurrent_peak = max(concurrent_peak, concurrent_active)
        if concurrent_active == 2:
            concurrent_release.set()
        try:
            with anyio.fail_after(2):
                await concurrent_release.wait()
            return {"text": arguments["text"], "peak": concurrent_peak}
        finally:
            concurrent_active -= 1

    async def input_tool(arguments: Mapping[str, object]) -> object:
        request_state = getattr(arguments, "request_state")
        input_responses = getattr(arguments, "input_responses")
        if request_state is None:
            return types.InputRequiredResult(
                requestState="opaque\x00state-雪",
                inputRequests={
                    "confirm": types.ElicitRequest(
                        params=types.ElicitRequestFormParams(
                            message="Confirm echo",
                            requestedSchema={
                                "type": "object",
                                "properties": {"approved": {"type": "boolean"}},
                                "required": ["approved"],
                                "additionalProperties": False,
                            },
                        )
                    )
                },
            )
        accepted = input_responses.get("confirm")
        if (
            request_state != "opaque\x00state-雪"
            or type(accepted) is not types.ElicitResult
            or accepted.action != "accept"
            or accepted.content != {"approved": True}
        ):
            raise ValueError("invalid current input response")
        return {"text": arguments["text"]}

    async def create_task(arguments: Mapping[str, object]) -> CreateTaskResult:
        del arguments
        return CreateTaskResult(
            task_id="task-1",
            status="working",
            created_at=_CREATED_AT,
            last_updated_at=_CREATED_AT,
            ttl_ms=5_000,
            poll_interval_ms=0,
            result_type="task",
        )

    async def notify(arguments: Mapping[str, object]) -> Mapping[str, object]:
        await server.publish_event(ToolsListChanged())
        return {"text": arguments["text"]}

    async def fail_tool(arguments: Mapping[str, object]) -> Mapping[str, object]:
        del arguments
        raise RuntimeError("Bear" + "er task12-provider-token-sentinel")

    async def help_resource(uri: str) -> bytes:
        del uri
        return b"help"

    async def fail_resource(uri: str) -> bytes:
        del uri
        raise RuntimeError("Bear" + "er task12-provider-token-sentinel")

    async def item_resource(uri: str) -> bytes:
        identifier = uri.rsplit("/", 1)[-1]
        return ('{"id":"' + identifier + '"}').encode("utf-8")

    async def summarize(request: PromptRequest) -> types.GetPromptResult:
        return types.GetPromptResult(
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(text=request.arguments.get("text", "")),
                )
            ]
        )

    async def get_task(params: GetTaskParams) -> GetTaskResult:
        return GetTaskResult(
            task_id=params.task_id,
            status="completed",
            created_at=_CREATED_AT,
            last_updated_at=_UPDATED_AT,
            ttl_ms=5_000,
            result={"text": "task"},
            result_type="complete",
        )

    async def update_task(params: UpdateTaskParams) -> UpdateTaskResult:
        del params
        return UpdateTaskResult(result_type="complete")

    async def cancel_task(params: CancelTaskParams) -> CancelTaskResult:
        del params
        return CancelTaskResult(result_type="complete")

    server.register_tool("omega.fake.echo", echo)
    server.register_tool("omega.fake.concurrent", concurrent)
    server.register_tool("omega.fake.input", input_tool)  # type: ignore[arg-type]
    server.register_tool("omega.fake.task", create_task)
    server.register_tool("omega.fake.notify", notify)
    server.register_tool("omega.fake.failure", fail_tool)
    server.register_resource("omega://fake/help", help_resource)
    server.register_resource("omega://fake/failure", fail_resource)
    server.register_resource("omega://fake/items/{id}", item_resource)
    server.register_prompt("omega.fake.summarize", summarize)
    server.register_extension_method(
        TASKS_EXTENSION_IDENTIFIER,
        "tasks/get",
        GetTaskParams,
        get_task,
    )
    server.register_extension_method(
        TASKS_EXTENSION_IDENTIFIER,
        "tasks/update",
        UpdateTaskParams,
        update_task,
    )
    server.register_extension_method(
        TASKS_EXTENSION_IDENTIFIER,
        "tasks/cancel",
        CancelTaskParams,
        cancel_task,
    )
    server.register_extension_result(
        TASKS_EXTENSION_IDENTIFIER,
        "task",
        CreateTaskResult,
    )
    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    transport = parser.add_mutually_exclusive_group(required=True)
    transport.add_argument("--stdio", action="store_true")
    transport.add_argument("--http", action="store_true")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--fail-cleanup", action="store_true")
    args = parser.parse_args()
    server = _server()
    if args.stdio:
        if args.fail_cleanup:
            original_stdio = server_module.stdio_server

            @asynccontextmanager
            async def failing_stdio() -> object:
                async with original_stdio() as streams:
                    yield streams
                raise RuntimeError("Bear" + "er standalone-cleanup-provider-token-sentinel")

            server_module.stdio_server = failing_stdio  # type: ignore[assignment]
        anyio.run(server.run_stdio)
        return
    anyio.run(partial(server.run_streamable_http, host="127.0.0.1", port=args.port))


if __name__ == "__main__":
    main()

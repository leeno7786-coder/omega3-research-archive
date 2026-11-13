from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import pytest
import httpx2
from mcp import Client, types
from mcp.client.extension import ClaimContext, ClientExtension, ResultClaim
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import ConfigDict

from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION
from mcp_platform.capabilities import project_declared_capabilities
from mcp_platform.client import CurrentMcpClient, SdkClientFactory
from mcp_platform.client_contracts import (
    McpClientDeployment,
    McpClientErrorReason,
    McpClientLimits,
    McpConnectionDescriptor,
    McpOperationKind,
    McpOperationRequest,
    McpOperationResult,
    McpOperationStatus,
    McpTransport,
)
from mcp_platform.tasks import (
    TASKS_EXTENSION_IDENTIFIER,
    TASKS_SCHEMA_SHA256,
    CancelTaskParams,
    CancelTaskRequest,
    CancelTaskResult,
    CreateTaskResult,
    GetTaskParams,
    GetTaskRequest,
    GetTaskResult,
    TasksClientExtension,
    TaskInputRequest,
    TaskInputResponse,
    UpdateTaskParams,
    UpdateTaskRequest,
    UpdateTaskResult,
)
from plugin_platform.contracts import AppRequirement


class _ProofGetTaskParams(types.RequestParams):
    model_config = ConfigDict(extra="forbid")

    task_id: str


class _ProofGetTaskRequest(types.Request[_ProofGetTaskParams, Literal["tasks/get"]]):
    model_config = ConfigDict(extra="forbid")
    name_param = "taskId"

    method: Literal["tasks/get"] = "tasks/get"
    params: _ProofGetTaskParams


class _ProofGetTaskResult(types.Result):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: Literal["working"]
    created_at: str
    last_updated_at: str
    ttl_ms: float | None
    result_type: Literal["complete"]


class _ProofCreateTaskResult(_ProofGetTaskResult):
    result_type: Literal["task"]


async def _proof_resolver(
    _result: _ProofCreateTaskResult,
    _context: ClaimContext,
) -> types.CallToolResult:
    return types.CallToolResult(content=[])


class _ProofTasksExtension(ClientExtension):
    identifier = "io.modelcontextprotocol/tasks"

    def settings(self) -> dict[str, Any]:
        return {}

    def claims(self) -> tuple[ResultClaim[_ProofCreateTaskResult], ...]:
        return (
            ResultClaim(
                result_type="task",
                model=_ProofCreateTaskResult,
                resolve=_proof_resolver,
                protocol_versions=frozenset({MCP_PROTOCOL_VERSION}),
            ),
        )


def test_installed_sdk_public_extension_and_explicit_schema_seams_send_tasks_get() -> None:
    async def exercise() -> None:
        seen: list[str] = []

        async def get_task(
            _context: Any,
            params: _ProofGetTaskParams,
        ) -> _ProofGetTaskResult:
            seen.append(params.task_id)
            return _ProofGetTaskResult(
                task_id=params.task_id,
                status="working",
                created_at="2026-08-16T00:00:00Z",
                last_updated_at="2026-08-16T00:00:01Z",
                ttl_ms=1_000,
                result_type="complete",
            )

        server = Server("tasks-proof", version="1.0.0")
        server.add_request_handler("tasks/get", _ProofGetTaskParams, get_task)
        extension = _ProofTasksExtension()
        assert extension.identifier == "io.modelcontextprotocol/tasks"
        assert extension.settings() == {}
        assert extension.claims()[0].result_type == "task"

        async with Client(
            server,
            mode=MCP_PROTOCOL_VERSION,
            client_info=types.Implementation(name="tasks-proof", version="1.0.0"),
            extensions=(extension,),
            cache=None,
        ) as connected:
            result = await connected.session.send_request(
                _ProofGetTaskRequest(params=_ProofGetTaskParams(task_id="task-1")),
                result_type=_ProofGetTaskResult,
            )

        assert seen == ["task-1"]
        assert type(result) is _ProofGetTaskResult
        assert result.task_id == "task-1"

    asyncio.run(exercise())


def test_public_streamable_http_wire_sets_required_task_name_header() -> None:
    observed_names: list[str | None] = []

    async def get_task(_context: Any, params: GetTaskParams) -> GetTaskResult:
        return GetTaskResult(
            task_id=params.task_id,
            status="working",
            status_message=None,
            created_at="2026-08-16T00:00:00Z",
            last_updated_at="2026-08-16T00:00:01Z",
            ttl_ms=1_000,
            result_type="complete",
        )

    server = Server("tasks-http-proof", version="1.0.0")
    server.add_request_handler("tasks/get", GetTaskParams, get_task)
    manager = StreamableHTTPSessionManager(server, json_response=True, stateless=True)

    async def observed_app(scope: Any, receive: Any, send: Any) -> None:
        headers = {key.decode(): value.decode() for key, value in scope["headers"]}
        observed_names.append(headers.get("mcp-name"))
        await manager.handle_request(scope, receive, send)

    @asynccontextmanager
    async def transport() -> AsyncIterator[Any]:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=observed_app),
            base_url="http://127.0.0.1",
        ) as http_client:
            async with streamable_http_client(
                "http://127.0.0.1/mcp",
                http_client=http_client,
                terminate_on_close=False,
            ) as streams:
                yield streams

    async def exercise() -> None:
        async with manager.run():
            async with Client(
                transport(),
                mode=MCP_PROTOCOL_VERSION,
                client_info=types.Implementation(name="tasks-proof", version="1.0.0"),
                extensions=(TasksClientExtension(),),
                cache=None,
            ) as connected:
                result = await connected.session.send_request(
                    GetTaskRequest(params=GetTaskParams(task_id="task-1")),
                    result_type=GetTaskResult,
                )
                assert result.task_id == "task-1"

    asyncio.run(exercise())
    assert observed_names == ["task-1"]


_SCHEMA_PATH = Path("schema/mcp/extensions/io.modelcontextprotocol.tasks.schema.json")
_PROVENANCE_PATH = Path("schema/mcp/extensions/io.modelcontextprotocol.tasks.provenance.json")
_TOOL_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}
_TOOL_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "integer"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def _descriptor(
    *,
    limits: McpClientLimits | None = None,
    tool_max_result_bytes: int = 4_096,
    tool_allowed_mime_types: tuple[str, ...] = ("application/json",),
) -> McpConnectionDescriptor:
    requirement = AppRequirement.from_dict(
        {
            "app_id": "com.example.tasks",
            "publisher_id": "example.publisher",
            "deployment": "local",
            "provider_id": None,
            "protocol_version": MCP_PROTOCOL_VERSION,
            "transports": ["stdio"],
            "remote_origin": None,
            "authorization_issuer": None,
            "authentication": {"kind": "none", "scopes": [], "secret_fields": []},
            "capabilities": [
                {
                    "kind": "tool",
                    "name": "jobs.start",
                    "schema_sha256": hashlib.sha256(
                        json.dumps(
                            _TOOL_SCHEMA,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    "version": None,
                },
                {
                    "kind": "tool",
                    "name": "jobs.other",
                    "schema_sha256": hashlib.sha256(
                        json.dumps(
                            _TOOL_SCHEMA,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    "version": None,
                },
                {
                    "kind": "extension",
                    "name": TASKS_EXTENSION_IDENTIFIER,
                    "schema_sha256": None,
                    "version": None,
                },
            ],
        }
    )
    projection = project_declared_capabilities(requirement)
    return McpConnectionDescriptor(
        app_id=requirement.app_id,
        publisher_id=requirement.publisher_id,
        deployment=McpClientDeployment.LOCAL,
        provider_id=None,
        protocol_version=MCP_PROTOCOL_VERSION,
        transport=McpTransport.STDIO,
        verified_launch_sha256="d" * 64,
        remote_origin=None,
        authorization_issuer=None,
        authentication=requirement.authentication,
        declared_capabilities={
            "tool:jobs.start": requirement.capabilities[0].schema_sha256,
            "tool:jobs.other": requirement.capabilities[1].schema_sha256,
        },
        extension_versions={TASKS_EXTENSION_IDENTIFIER: None},
        capability_revision=projection.snapshot.capability_revision,
        limits=limits or McpClientLimits(),
        signed_capability_authority={
            "tools": {
                name: {
                    "title": name,
                    "input_schema": _TOOL_SCHEMA,
                    "input_schema_sha256": hashlib.sha256(
                        json.dumps(
                            _TOOL_SCHEMA,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    "output_schema": _TOOL_OUTPUT_SCHEMA,
                    "output_schema_sha256": hashlib.sha256(
                        json.dumps(
                            _TOOL_OUTPUT_SCHEMA,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    "risk_class": "read_only",
                    "effect": "read",
                    "requires_revision": False,
                    "max_result_bytes": tool_max_result_bytes,
                    "allowed_mime_types": list(tool_allowed_mime_types),
                }
                for name in ("jobs.start", "jobs.other")
            },
            "resources": {},
            "resource_templates": {},
        },
    )


def _metadata(descriptor: McpConnectionDescriptor) -> dict[str, object]:
    return {
        "appId": descriptor.app_id,
        "publisherId": descriptor.publisher_id,
        "deployment": descriptor.deployment.value,
        "providerId": descriptor.provider_id,
        "protocolVersion": descriptor.protocol_version,
        "transport": descriptor.transport.value,
        "remoteOrigin": descriptor.remote_origin,
        "authorizationIssuer": descriptor.authorization_issuer,
    }


def _request(
    descriptor: McpConnectionDescriptor,
    operation: McpOperationKind,
    capability_id: str,
    arguments: Mapping[str, object] | None = None,
    **changes: object,
) -> McpOperationRequest:
    values: dict[str, object] = {
        "request_id": "task-operation-1",
        "app_id": descriptor.app_id,
        "operation": operation,
        "capability_id": capability_id,
        "arguments": {} if arguments is None else arguments,
        "capability_revision": descriptor.capability_revision,
        "deadline_monotonic": asyncio.get_running_loop().time() + 30,
        "cancellation_id": "task-cancel-1",
        "task_id": None,
        "input_request_id": None,
        "request_state": None,
    }
    values.update(changes)
    return McpOperationRequest(**values)  # type: ignore[arg-type]


class _TaskBehavior:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.status = "working"
        self.next_task = 1
        self.poll_interval_ms = 0
        self.input_requests: dict[str, object] = {
            "approval": _elicitation_request(
                {
                    "type": "object",
                    "properties": {"approved": {"type": "boolean"}},
                    "required": ["approved"],
                    "additionalProperties": False,
                }
            )
        }
        self.completed_result: dict[str, object] = _tool_result_envelope(
            structured_content={"answer": 42}
        )
        self.error: dict[str, object] = {"code": "secret failure"}
        self.create_metadata: Mapping[str, object] | None = None
        self.cancel_error: BaseException | None = None
        self.cancel_entered: asyncio.Event | None = None
        self.release_cancel: asyncio.Event | None = None
        self.resist_cancel_cancellation = False
        self.update_error: BaseException | None = None
        self.update_metadata: Mapping[str, object] | None = None
        self.update_responses: list[dict[str, object]] = []
        self.update_entered: asyncio.Event | None = None
        self.release_update: asyncio.Event | None = None
        self.update_wait_limit: int | None = None
        self.update_wait_count = 0
        self.get_entered: asyncio.Event | None = None
        self.release_get: asyncio.Event | None = None
        self.call_entered: asyncio.Queue[str] | None = None
        self.release_call: asyncio.Event | None = None
        self.resist_call_cancellation = False


def _elicitation_request(
    schema: Mapping[str, object],
    *,
    message: str = "Approve this task?",
) -> dict[str, object]:
    return {
        "method": "elicitation/create",
        "params": {
            "mode": "form",
            "message": message,
            "requestedSchema": dict(schema),
        },
    }


def _elicitation_response(content: Mapping[str, object]) -> dict[str, object]:
    return {"action": "accept", "content": dict(content)}


def _tool_result_envelope(
    *,
    structured_content: Mapping[str, object] | None = None,
    content: list[dict[str, object]] | None = None,
    is_error: bool = False,
) -> dict[str, object]:
    result: dict[str, object] = {
        "content": [] if content is None else content,
        "isError": is_error,
        "resultType": "complete",
    }
    if structured_content is not None:
        result["structuredContent"] = dict(structured_content)
    return result


def _task_result(
    result_type: type[CreateTaskResult] | type[GetTaskResult],
    *,
    task_id: str,
    status: str,
    metadata: Mapping[str, object],
    poll_interval_ms: int = 0,
    input_requests: Mapping[str, object] | None = None,
    completed_result: Mapping[str, object] | None = None,
    error: Mapping[str, object] | None = None,
) -> CreateTaskResult | GetTaskResult:
    values: dict[str, object] = {
        "task_id": task_id,
        "status": status,
        "status_message": f"task is {status}",
        "created_at": "2026-08-16T00:00:00Z",
        "last_updated_at": "2026-08-16T00:00:01Z",
        "ttl_ms": 10_000,
        "meta": dict(metadata),
        "result_type": "task" if result_type is CreateTaskResult else "complete",
        "poll_interval_ms": poll_interval_ms,
    }
    if status == "completed":
        values["result"] = dict(
            completed_result or _tool_result_envelope(structured_content={"answer": 42})
        )
    elif status == "failed":
        values["error"] = dict(error or {"code": "failed"})
    elif status == "input_required":
        values["input_requests"] = dict(input_requests or behavior_default_input_requests())
    return result_type(**values)  # type: ignore[arg-type,return-value]


def behavior_default_input_requests() -> dict[str, object]:
    return {
        "approval": _elicitation_request(
            {
                "type": "object",
                "properties": {"approved": {"type": "boolean"}},
                "required": ["approved"],
                "additionalProperties": False,
            }
        )
    }


def _server(descriptor: McpConnectionDescriptor, behavior: _TaskBehavior) -> Server:
    metadata = _metadata(descriptor)

    async def discover(_ctx: Any, _params: types.RequestParams) -> types.DiscoverResult:
        return types.DiscoverResult(
            supportedVersions=[MCP_PROTOCOL_VERSION],
            capabilities=types.ServerCapabilities(
                tools=types.ToolsCapability(),
                extensions={TASKS_EXTENSION_IDENTIFIER: {}},
            ),
            _meta=metadata,
        )

    async def list_tools(
        _ctx: Any,
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="jobs.start",
                    inputSchema=_TOOL_SCHEMA,
                    outputSchema=_TOOL_OUTPUT_SCHEMA,
                ),
                types.Tool(
                    name="jobs.other",
                    inputSchema=_TOOL_SCHEMA,
                    outputSchema=_TOOL_OUTPUT_SCHEMA,
                ),
            ],
            _meta=metadata,
        )

    async def call_tool(
        _ctx: Any,
        _params: types.CallToolRequestParams,
    ) -> Any:
        task_id = f"task-{behavior.next_task}"
        behavior.next_task += 1
        behavior.calls.append(("tools/call", task_id))
        if behavior.call_entered is not None:
            behavior.call_entered.put_nowait(task_id)
        if behavior.release_call is not None:
            while not behavior.release_call.is_set():
                try:
                    await behavior.release_call.wait()
                except asyncio.CancelledError:
                    if not behavior.resist_call_cancellation:
                        raise
        return _task_result(
            CreateTaskResult,
            task_id=task_id,
            status="working",
            metadata=behavior.create_metadata or metadata,
        )

    async def get_task(_ctx: Any, params: GetTaskParams) -> GetTaskResult:
        behavior.calls.append(("tasks/get", params.task_id))
        if behavior.get_entered is not None:
            behavior.get_entered.set()
        if behavior.release_get is not None:
            await behavior.release_get.wait()
        return _task_result(
            GetTaskResult,
            task_id=params.task_id,
            status=behavior.status,
            metadata=metadata,
            poll_interval_ms=behavior.poll_interval_ms,
            input_requests=behavior.input_requests,
            completed_result=behavior.completed_result,
            error=behavior.error,
        )  # type: ignore[return-value]

    async def update_task(_ctx: Any, params: UpdateTaskParams) -> UpdateTaskResult:
        behavior.calls.append(("tasks/update", params.task_id))
        behavior.update_responses.append(dict(params.input_responses))
        if behavior.update_entered is not None:
            behavior.update_entered.set()
        should_wait = behavior.release_update is not None and (
            behavior.update_wait_limit is None
            or behavior.update_wait_count < behavior.update_wait_limit
        )
        if should_wait:
            behavior.update_wait_count += 1
            await behavior.release_update.wait()
        if behavior.update_error is not None:
            error = behavior.update_error
            behavior.update_error = None
            raise error
        return UpdateTaskResult(
            result_type="complete",
            meta=behavior.update_metadata or metadata,
        )

    async def cancel_task(_ctx: Any, params: CancelTaskParams) -> CancelTaskResult:
        behavior.calls.append(("tasks/cancel", params.task_id))
        if behavior.cancel_entered is not None:
            behavior.cancel_entered.set()
        if behavior.release_cancel is not None:
            while not behavior.release_cancel.is_set():
                try:
                    await behavior.release_cancel.wait()
                except asyncio.CancelledError:
                    if not behavior.resist_cancel_cancellation:
                        raise
        if behavior.cancel_error is not None:
            error = behavior.cancel_error
            behavior.cancel_error = None
            raise error
        return CancelTaskResult(result_type="complete", meta=metadata)

    server = Server(
        "tasks-test",
        version="1.0.0",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    server.add_request_handler("server/discover", types.RequestParams, discover)
    server.add_request_handler("tasks/get", GetTaskParams, get_task)
    server.add_request_handler("tasks/update", UpdateTaskParams, update_task)
    server.add_request_handler("tasks/cancel", CancelTaskParams, cancel_task)
    return server


async def _started(
    *,
    limits: McpClientLimits | None = None,
) -> tuple[CurrentMcpClient, McpConnectionDescriptor, _TaskBehavior]:
    descriptor = _descriptor(limits=limits)
    behavior = _TaskBehavior()
    client = CurrentMcpClient(
        descriptor,
        SdkClientFactory.for_in_process_server(_server(descriptor, behavior)),
    )
    await client.start()
    return client, descriptor, behavior


def _assert_failed(result: McpOperationResult, reason: McpClientErrorReason) -> None:
    assert result.status is McpOperationStatus.FAILED
    assert result.error is not None
    assert result.error.reason is reason


def test_vendored_task_schema_hash_is_checked_before_parse_and_provenance_is_exact(
    tmp_path: Path,
) -> None:
    # .gitattributes is a UNION of rule sets (byte-exact benchmark/taxonomy fixtures plus
    # this schema); assert the schema's -text rule is present, not that it is the only rule.
    assert (
        "schema/mcp/extensions/io.modelcontextprotocol.tasks.schema.json -text"
        in Path(".gitattributes").read_text(encoding="utf-8").splitlines()
    )
    schema_bytes = _SCHEMA_PATH.read_bytes()
    digest = hashlib.sha256(schema_bytes).hexdigest()
    assert (
        digest
        == TASKS_SCHEMA_SHA256
        == ("b17cb4a2534379c214b17770bd5d3d54f69fde16a953bfb542c58235a61274bb")
    )
    repository_path = _SCHEMA_PATH.as_posix()
    repository_root = Path(__file__).resolve().parents[2]
    git = ["git", "-c", f"safe.directory={repository_root}", "-C", str(repository_root)]
    blob_oid = subprocess.run(
        [*git, "rev-parse", f":{repository_path}"],
        check=True,
        capture_output=True,
    ).stdout.strip()
    repository_blob = subprocess.run(
        [*git, "cat-file", "blob", blob_oid],
        check=True,
        capture_output=True,
    ).stdout
    assert hashlib.sha256(repository_blob).hexdigest() == TASKS_SCHEMA_SHA256

    checkout_root = tmp_path / "autocrlf-checkout"
    checkout_root.mkdir()
    subprocess.run(
        [
            *git,
            "-c",
            "core.autocrlf=true",
            "checkout-index",
            f"--prefix={checkout_root}{os.sep}",
            "--",
            repository_path,
        ],
        check=True,
    )
    checkout_bytes = (checkout_root / _SCHEMA_PATH).read_bytes()
    assert hashlib.sha256(checkout_bytes).hexdigest() == TASKS_SCHEMA_SHA256

    schema = json.loads(schema_bytes)
    provenance = json.loads(_PROVENANCE_PATH.read_bytes())
    assert schema["description"].endswith("Extension Identifier: io.modelcontextprotocol/tasks")
    assert provenance == {
        "repository": "https://github.com/modelcontextprotocol/ext-tasks",
        "commit": "2c1425d9a288b9b1f489430fe1e00bb392b47e48",
        "path": "schema/draft/schema.json",
        "sha256": TASKS_SCHEMA_SHA256,
        "license": {
            "name": "Apache License 2.0",
            "spdx_id": "Apache-2.0",
            "path": "LICENSE",
            "url": "https://github.com/modelcontextprotocol/ext-tasks/blob/2c1425d9a288b9b1f489430fe1e00bb392b47e48/LICENSE",
        },
        "retrieval_date": "2026-08-16",
    }


def test_tasks_extension_claims_only_exact_identifier_settings_and_task_result() -> None:
    extension = TasksClientExtension()
    assert extension.identifier == TASKS_EXTENSION_IDENTIFIER == "io.modelcontextprotocol/tasks"
    assert extension.settings() == {}
    assert len(extension.claims()) == 1
    claim = extension.claims()[0]
    assert claim.result_type == "task"
    assert claim.model is CreateTaskResult
    assert claim.method == "tools/call"
    assert claim.protocol_versions == frozenset({MCP_PROTOCOL_VERSION})


def test_task_request_and_result_models_are_explicit_closed_wire_schemas() -> None:
    request_models = (
        GetTaskRequest(params=GetTaskParams(task_id="task-1")),
        UpdateTaskRequest(
            params=UpdateTaskParams(
                task_id="task-1",
                input_responses={"approval": True},
            )
        ),
        CancelTaskRequest(params=CancelTaskParams(task_id="task-1")),
    )
    assert [model.method for model in request_models] == [
        "tasks/get",
        "tasks/update",
        "tasks/cancel",
    ]
    assert [type(model).name_param for model in request_models] == [
        "taskId",
        "taskId",
        "taskId",
    ]
    assert [
        model.model_dump(by_alias=True, exclude_none=True)["params"]["taskId"]
        for model in request_models
    ] == [
        "task-1",
        "task-1",
        "task-1",
    ]
    assert GetTaskResult.model_config["extra"] == "forbid"
    assert UpdateTaskResult.model_config["extra"] == "forbid"
    assert CancelTaskResult.model_config["extra"] == "forbid"
    with pytest.raises(ValueError):
        GetTaskParams(task_id="")
    with pytest.raises(ValueError):
        GetTaskResult(
            task_id="task-1",
            status="completed",
            created_at="2026-08-16T00:00:00Z",
            last_updated_at="2026-08-16T00:00:01Z",
            ttl_ms=10,
        )
    for model, values in (
        (
            CreateTaskResult,
            {
                "task_id": "task-1",
                "status": "working",
                "created_at": "2026-08-16T00:00:00Z",
                "last_updated_at": "2026-08-16T00:00:01Z",
                "ttl_ms": 10,
            },
        ),
        (UpdateTaskResult, {}),
        (CancelTaskResult, {}),
    ):
        with pytest.raises(ValueError):
            model(**values)

    embedded = TaskInputRequest.model_validate(behavior_default_input_requests()["approval"])
    assert embedded.model_dump(by_alias=True) == behavior_default_input_requests()["approval"]
    response = TaskInputResponse.model_validate(_elicitation_response({"approved": True}))
    assert response.model_dump(by_alias=True, exclude_none=True) == (
        _elicitation_response({"approved": True})
    )
    invalid_requests = (
        {"params": behavior_default_input_requests()["approval"]["params"]},
        {
            "method": "sampling/createMessage",
            "params": behavior_default_input_requests()["approval"]["params"],
        },
        {
            **behavior_default_input_requests()["approval"],
            "execute": "server text",
        },
        {
            "method": "elicitation/create",
            "params": {
                "mode": "url",
                "message": "open",
                "requestedSchema": {"type": "object"},
            },
        },
    )
    for invalid in invalid_requests:
        with pytest.raises(ValueError):
            TaskInputRequest.model_validate(invalid)
    for invalid in (
        {"content": {"approved": True}},
        {"action": "accept"},
        {"action": "decline", "content": {}},
        {"action": "accept", "content": {}, "serverText": "secret"},
    ):
        with pytest.raises(ValueError):
            TaskInputResponse.model_validate(invalid)


def test_server_created_task_is_bound_and_only_get_update_cancel_are_sent() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.status is McpOperationStatus.TASK_STARTED
        handle = started.task_handle
        assert handle is not None
        assert handle.to_dict() == {
            "task_id": "task-1",
            "app_id": descriptor.app_id,
            "capability_id": "tool:jobs.start",
            "capability_revision": descriptor.capability_revision,
            "connection_revision": client.connection_revision,
            "authorization_context_revision": client.authorization_context_revision,
        }

        behavior.status = "input_required"
        requested = await client.get_task(
            _request(
                descriptor,
                McpOperationKind.TASK_GET,
                "tool:jobs.start",
                task_id=handle.task_id,
            )
        )
        assert requested.status is McpOperationStatus.COMPLETED
        assert requested.to_dict()["data"]["task"]["input_requests"] == (
            behavior_default_input_requests()
        )
        updated = await client.update_task(
            _request(
                descriptor,
                McpOperationKind.TASK_UPDATE,
                "tool:jobs.start",
                {"inputResponses": {"approval": _elicitation_response({"approved": True})}},
                task_id=handle.task_id,
            )
        )
        assert updated.status is McpOperationStatus.TASK_UPDATED
        assert updated.task_handle == handle

        behavior.status = "working"
        polled = await client.get_task(
            _request(
                descriptor,
                McpOperationKind.TASK_GET,
                "tool:jobs.start",
                task_id=handle.task_id,
            )
        )
        assert polled.status is McpOperationStatus.COMPLETED
        assert polled.data["task"]["status"] == "working"

        cancelled = await client.cancel_task(
            _request(
                descriptor,
                McpOperationKind.TASK_CANCEL,
                "tool:jobs.start",
                task_id=handle.task_id,
            )
        )
        assert cancelled.status is McpOperationStatus.TASK_UPDATED
        assert behavior.calls == [
            ("tools/call", "task-1"),
            ("tasks/get", "task-1"),
            ("tasks/update", "task-1"),
            ("tasks/get", "task-1"),
            ("tasks/cancel", "task-1"),
        ]
        await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled"])
def test_task_terminal_states_complete_once_and_reject_further_polling(
    terminal_status: str,
) -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.task_handle is not None
        behavior.status = terminal_status
        request = _request(
            descriptor,
            McpOperationKind.TASK_GET,
            "tool:jobs.start",
            task_id=started.task_handle.task_id,
        )
        terminal = await client.get_task(request)
        assert terminal.status is McpOperationStatus.COMPLETED
        assert terminal.data == {
            "content": (),
            "structured_content": ({"answer": 42} if terminal_status == "completed" else None),
            "is_error": terminal_status != "completed",
        }
        assert "task is" not in repr(terminal)
        assert "secret failure" not in repr(terminal)
        again = await client.get_task(request)
        _assert_failed(again, McpClientErrorReason.TASK_INVALID)
        await client.close()

    asyncio.run(exercise())


def test_task_authority_revisions_context_bounds_and_schema_fail_closed() -> None:
    async def exercise() -> None:
        limits = McpClientLimits(
            max_task_polls=1,
            max_task_updates=1,
            max_task_duration_ms=5_000,
        )
        client, descriptor, behavior = await _started(limits=limits)
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.task_handle is not None
        task_id = started.task_handle.task_id

        mismatches = [
            await client.get_task(
                _request(
                    descriptor,
                    McpOperationKind.TASK_GET,
                    "tool:jobs.start",
                    task_id=task_id,
                    app_id="com.example.other",
                )
            ),
            await client.get_task(
                _request(
                    descriptor,
                    McpOperationKind.TASK_GET,
                    "tool:jobs.start",
                    task_id=task_id,
                    capability_revision="f" * 64,
                )
            ),
            await client.get_task(
                _request(
                    descriptor,
                    McpOperationKind.TASK_GET,
                    "tool:jobs.other",
                    task_id=task_id,
                )
            ),
        ]
        assert [item.error.reason for item in mismatches if item.error is not None] == [
            McpClientErrorReason.IDENTITY_MISMATCH,
            McpClientErrorReason.CAPABILITY_DRIFT,
            McpClientErrorReason.TASK_INVALID,
        ]

        first_poll = await client.get_task(
            _request(
                descriptor,
                McpOperationKind.TASK_GET,
                "tool:jobs.start",
                task_id=task_id,
            )
        )
        assert first_poll.status is McpOperationStatus.COMPLETED
        poll_limit = await client.get_task(
            _request(
                descriptor,
                McpOperationKind.TASK_GET,
                "tool:jobs.start",
                task_id=task_id,
            )
        )
        _assert_failed(poll_limit, McpClientErrorReason.TASK_INVALID)

        behavior.status = "input_required"
        other = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "other"},
            )
        )
        assert other.task_handle is not None
        input_needed = await client.get_task(
            _request(
                descriptor,
                McpOperationKind.TASK_GET,
                "tool:jobs.start",
                task_id=other.task_handle.task_id,
            )
        )
        assert input_needed.data["task"]["status"] == "input_required"
        update_request = _request(
            descriptor,
            McpOperationKind.TASK_UPDATE,
            "tool:jobs.start",
            {"inputResponses": {"approval": _elicitation_response({"approved": True})}},
            task_id=other.task_handle.task_id,
        )
        assert (await client.update_task(update_request)).status is McpOperationStatus.TASK_UPDATED
        _assert_failed(
            await client.update_task(update_request),
            McpClientErrorReason.TASK_INVALID,
        )

        expiring_client, expiring_descriptor, _ = await _started(
            limits=McpClientLimits(max_task_duration_ms=20)
        )
        expiring = await expiring_client.call_tool(
            _request(
                expiring_descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "expire"},
            )
        )
        assert expiring.task_handle is not None
        await asyncio.sleep(0.03)
        _assert_failed(
            await expiring_client.get_task(
                _request(
                    expiring_descriptor,
                    McpOperationKind.TASK_GET,
                    "tool:jobs.start",
                    task_id=expiring.task_handle.task_id,
                )
            ),
            McpClientErrorReason.TASK_INVALID,
        )
        await expiring_client.close()

        second, second_descriptor, _ = await _started(limits=limits)
        cross_context = await second.get_task(
            _request(
                second_descriptor,
                McpOperationKind.TASK_GET,
                "tool:jobs.start",
                task_id=task_id,
            )
        )
        _assert_failed(cross_context, McpClientErrorReason.TASK_INVALID)
        await second.close()
        await client.close()

    asyncio.run(exercise())


def test_task_duration_expiring_during_poll_latches_before_state_publication() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started(
            limits=McpClientLimits(max_task_duration_ms=10)
        )
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.task_handle is not None
        behavior.get_entered = asyncio.Event()
        behavior.release_get = asyncio.Event()
        request = _request(
            descriptor,
            McpOperationKind.TASK_GET,
            "tool:jobs.start",
            task_id=started.task_handle.task_id,
        )
        pending = asyncio.create_task(client.get_task(request))
        await behavior.get_entered.wait()
        await asyncio.sleep(0.02)
        behavior.release_get.set()
        expired = await pending
        _assert_failed(expired, McpClientErrorReason.TASK_INVALID)
        _assert_failed(await client.get_task(request), McpClientErrorReason.TASK_INVALID)
        await client.close()

    asyncio.run(exercise())


def test_task_state_poll_interval_and_input_request_authority_are_preserved() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.task_handle is not None
        behavior.status = "input_required"
        behavior.poll_interval_ms = 50
        behavior.input_requests = {
            "approval": _elicitation_request(
                {
                    "type": "object",
                    "properties": {"approved": {"type": "boolean"}},
                    "required": ["approved"],
                    "additionalProperties": False,
                }
            ),
            "note": _elicitation_request(
                {
                    "type": "object",
                    "properties": {"note": {"type": "string", "maxLength": 4}},
                    "required": ["note"],
                    "additionalProperties": False,
                },
                message="Add a note",
            ),
        }
        get_request = _request(
            descriptor,
            McpOperationKind.TASK_GET,
            "tool:jobs.start",
            task_id=started.task_handle.task_id,
        )
        state = await client.get_task(get_request)
        assert state.status is McpOperationStatus.COMPLETED
        assert state.to_dict()["data"]["task"] == {
            "task_id": "task-1",
            "status": "input_required",
            "status_message": "task is input_required",
            "created_at": "2026-08-16T00:00:00Z",
            "last_updated_at": "2026-08-16T00:00:01Z",
            "ttl_ms": 10_000.0,
            "poll_interval_ms": 50.0,
            "input_requests": {
                "approval": behavior.input_requests["approval"],
                "note": behavior.input_requests["note"],
            },
        }
        await asyncio.sleep(0.06)
        repeated_state = await client.get_task(get_request)
        assert repeated_state.status is McpOperationStatus.COMPLETED
        assert repeated_state.data == state.data

        for responses in (
            {"unknown": _elicitation_response({"approved": True})},
            {"approval": _elicitation_response({"approved": "yes"})},
            {"note": _elicitation_response({"note": "too long"})},
        ):
            invalid = await client.update_task(
                _request(
                    descriptor,
                    McpOperationKind.TASK_UPDATE,
                    "tool:jobs.start",
                    {"inputResponses": responses},
                    task_id=started.task_handle.task_id,
                )
            )
            _assert_failed(invalid, McpClientErrorReason.TASK_INVALID)
        updated = await client.update_task(
            _request(
                descriptor,
                McpOperationKind.TASK_UPDATE,
                "tool:jobs.start",
                {"inputResponses": {"approval": _elicitation_response({"approved": True})}},
                task_id=started.task_handle.task_id,
            )
        )
        assert updated.status is McpOperationStatus.TASK_UPDATED
        behavior.update_error = RuntimeError("one-shot transport failure")
        failed_note = await client.update_task(
            _request(
                descriptor,
                McpOperationKind.TASK_UPDATE,
                "tool:jobs.start",
                {"inputResponses": {"note": _elicitation_response({"note": "okay"})}},
                task_id=started.task_handle.task_id,
            )
        )
        _assert_failed(failed_note, McpClientErrorReason.TRANSPORT_UNAVAILABLE)
        retried = await client.update_task(
            _request(
                descriptor,
                McpOperationKind.TASK_UPDATE,
                "tool:jobs.start",
                {"inputResponses": {"note": _elicitation_response({"note": "okay"})}},
                task_id=started.task_handle.task_id,
            )
        )
        assert retried.status is McpOperationStatus.TASK_UPDATED
        stale = await client.update_task(
            _request(
                descriptor,
                McpOperationKind.TASK_UPDATE,
                "tool:jobs.start",
                {"inputResponses": {"approval": _elicitation_response({"approved": True})}},
                task_id=started.task_handle.task_id,
            )
        )
        _assert_failed(stale, McpClientErrorReason.TASK_INVALID)
        assert [name for name, _value in behavior.calls].count("tasks/update") == 3
        await client.close()

    asyncio.run(exercise())


def test_latest_input_required_snapshot_retires_omitted_request_ids() -> None:
    """Catches union accumulation retaining IDs omitted by the latest server snapshot."""

    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.task_handle is not None
        approval = behavior.input_requests["approval"]
        note = _elicitation_request(
            {
                "type": "object",
                "properties": {"note": {"type": "string"}},
                "required": ["note"],
                "additionalProperties": False,
            }
        )
        behavior.status = "input_required"
        behavior.input_requests = {"approval": approval, "note": note}
        get_request = _request(
            descriptor,
            McpOperationKind.TASK_GET,
            "tool:jobs.start",
            task_id=started.task_handle.task_id,
        )
        assert (await client.get_task(get_request)).status is McpOperationStatus.COMPLETED

        behavior.input_requests = {"note": note}
        latest = await client.get_task(get_request)
        assert latest.status is McpOperationStatus.COMPLETED
        assert set(latest.data["task"]["input_requests"]) == {"note"}
        stale = await client.update_task(
            _request(
                descriptor,
                McpOperationKind.TASK_UPDATE,
                "tool:jobs.start",
                {"inputResponses": {"approval": _elicitation_response({"approved": True})}},
                task_id=started.task_handle.task_id,
            )
        )
        _assert_failed(stale, McpClientErrorReason.TASK_INVALID)
        current = await client.update_task(
            _request(
                descriptor,
                McpOperationKind.TASK_UPDATE,
                "tool:jobs.start",
                {"inputResponses": {"note": _elicitation_response({"note": "current"})}},
                task_id=started.task_handle.task_id,
            )
        )
        assert current.status is McpOperationStatus.TASK_UPDATED
        await client.close()

    asyncio.run(exercise())


def test_transition_away_from_input_required_retires_all_request_ids() -> None:
    """Catches stale input authority surviving a later non-input task state."""

    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.task_handle is not None
        behavior.status = "input_required"
        get_request = _request(
            descriptor,
            McpOperationKind.TASK_GET,
            "tool:jobs.start",
            task_id=started.task_handle.task_id,
        )
        assert (await client.get_task(get_request)).status is McpOperationStatus.COMPLETED

        behavior.status = "working"
        working = await client.get_task(get_request)
        assert working.status is McpOperationStatus.COMPLETED
        stale = await client.update_task(
            _request(
                descriptor,
                McpOperationKind.TASK_UPDATE,
                "tool:jobs.start",
                {"inputResponses": {"approval": _elicitation_response({"approved": True})}},
                task_id=started.task_handle.task_id,
            )
        )
        _assert_failed(stale, McpClientErrorReason.TASK_INVALID)
        await client.close()

    asyncio.run(exercise())


def test_latest_input_snapshot_rebinds_changed_same_id_schema() -> None:
    """Catches treating a repeated ID as immutable instead of latest-snapshot authority."""

    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.task_handle is not None
        behavior.status = "input_required"
        get_request = _request(
            descriptor,
            McpOperationKind.TASK_GET,
            "tool:jobs.start",
            task_id=started.task_handle.task_id,
        )
        assert (await client.get_task(get_request)).status is McpOperationStatus.COMPLETED

        changed = _elicitation_request(
            {
                "type": "object",
                "properties": {"decision": {"type": "string", "enum": ["yes", "no"]}},
                "required": ["decision"],
                "additionalProperties": False,
            },
            message="Choose a decision",
        )
        behavior.input_requests = {"approval": changed}
        rebound = await client.get_task(get_request)
        assert rebound.status is McpOperationStatus.COMPLETED
        assert rebound.to_dict()["data"]["task"]["input_requests"] == {"approval": changed}
        old_schema = await client.update_task(
            _request(
                descriptor,
                McpOperationKind.TASK_UPDATE,
                "tool:jobs.start",
                {"inputResponses": {"approval": _elicitation_response({"approved": True})}},
                task_id=started.task_handle.task_id,
            )
        )
        _assert_failed(old_schema, McpClientErrorReason.TASK_INVALID)
        updated = await client.update_task(
            _request(
                descriptor,
                McpOperationKind.TASK_UPDATE,
                "tool:jobs.start",
                {"inputResponses": {"approval": _elicitation_response({"decision": "yes"})}},
                task_id=started.task_handle.task_id,
            )
        )
        assert updated.status is McpOperationStatus.TASK_UPDATED
        await client.close()

    asyncio.run(exercise())


def test_old_update_ack_cannot_consume_newer_same_id_input_authority() -> None:
    """Catches an old wire ack retiring a changed same-ID request from a later poll."""

    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.task_handle is not None
        behavior.status = "input_required"
        get_request = _request(
            descriptor,
            McpOperationKind.TASK_GET,
            "tool:jobs.start",
            task_id=started.task_handle.task_id,
        )
        assert (await client.get_task(get_request)).status is McpOperationStatus.COMPLETED

        update_entered = asyncio.Event()
        release_update = asyncio.Event()
        behavior.update_entered = update_entered
        behavior.release_update = release_update
        old_update = asyncio.create_task(
            client.update_task(
                _request(
                    descriptor,
                    McpOperationKind.TASK_UPDATE,
                    "tool:jobs.start",
                    {"inputResponses": {"approval": _elicitation_response({"approved": True})}},
                    task_id=started.task_handle.task_id,
                )
            )
        )
        await asyncio.wait_for(update_entered.wait(), timeout=1)

        changed = _elicitation_request(
            {
                "type": "object",
                "properties": {"decision": {"type": "string", "enum": ["yes", "no"]}},
                "required": ["decision"],
                "additionalProperties": False,
            },
            message="Choose a decision",
        )
        behavior.input_requests = {"approval": changed}
        rebound = await client.get_task(get_request)
        assert rebound.status is McpOperationStatus.COMPLETED
        assert rebound.to_dict()["data"]["task"]["input_requests"] == {"approval": changed}

        release_update.set()
        acknowledged = await old_update
        assert acknowledged.status is McpOperationStatus.TASK_UPDATED
        behavior.update_entered = None
        behavior.release_update = None

        current = await client.update_task(
            _request(
                descriptor,
                McpOperationKind.TASK_UPDATE,
                "tool:jobs.start",
                {"inputResponses": {"approval": _elicitation_response({"decision": "yes"})}},
                task_id=started.task_handle.task_id,
            )
        )
        assert current.status is McpOperationStatus.TASK_UPDATED
        await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("winning_approved", [True, False])
def test_overlapping_same_generation_update_is_rejected_before_second_wire_call(
    winning_approved: bool,
) -> None:
    """Catches two concurrent updates sending side effects for one input authority."""

    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.task_handle is not None
        behavior.status = "input_required"
        assert (
            await client.get_task(
                _request(
                    descriptor,
                    McpOperationKind.TASK_GET,
                    "tool:jobs.start",
                    task_id=started.task_handle.task_id,
                )
            )
        ).status is McpOperationStatus.COMPLETED

        behavior.update_entered = asyncio.Event()
        behavior.release_update = asyncio.Event()
        behavior.update_wait_limit = 1
        winning_request = _request(
            descriptor,
            McpOperationKind.TASK_UPDATE,
            "tool:jobs.start",
            {"inputResponses": {"approval": _elicitation_response({"approved": winning_approved})}},
            task_id=started.task_handle.task_id,
        )
        losing_request = _request(
            descriptor,
            McpOperationKind.TASK_UPDATE,
            "tool:jobs.start",
            {
                "inputResponses": {
                    "approval": _elicitation_response({"approved": not winning_approved})
                }
            },
            task_id=started.task_handle.task_id,
        )
        winning = asyncio.create_task(client.update_task(winning_request))
        await asyncio.wait_for(behavior.update_entered.wait(), timeout=1)
        losing = await client.update_task(losing_request)
        behavior.release_update.set()
        won = await winning

        assert won.status is McpOperationStatus.TASK_UPDATED
        _assert_failed(losing, McpClientErrorReason.TASK_INVALID)
        assert losing.data == {}
        assert "approved" not in repr(losing)
        assert behavior.update_responses == [
            {"approval": _elicitation_response({"approved": winning_approved})}
        ]
        await client.close()

    asyncio.run(exercise())


def test_newer_generation_can_update_while_old_same_id_generation_is_reserved() -> None:
    """Catches a raw-ID reservation blocking a changed same-ID authority generation."""

    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.task_handle is not None
        behavior.status = "input_required"
        get_request = _request(
            descriptor,
            McpOperationKind.TASK_GET,
            "tool:jobs.start",
            task_id=started.task_handle.task_id,
        )
        assert (await client.get_task(get_request)).status is McpOperationStatus.COMPLETED

        behavior.update_entered = asyncio.Event()
        behavior.release_update = asyncio.Event()
        behavior.update_wait_limit = 1
        old_request = _request(
            descriptor,
            McpOperationKind.TASK_UPDATE,
            "tool:jobs.start",
            {"inputResponses": {"approval": _elicitation_response({"approved": True})}},
            task_id=started.task_handle.task_id,
        )
        old_update = asyncio.create_task(client.update_task(old_request))
        await asyncio.wait_for(behavior.update_entered.wait(), timeout=1)

        changed = _elicitation_request(
            {
                "type": "object",
                "properties": {"decision": {"type": "string"}},
                "required": ["decision"],
                "additionalProperties": False,
            }
        )
        behavior.input_requests = {"approval": changed}
        assert (await client.get_task(get_request)).status is McpOperationStatus.COMPLETED
        current_request = _request(
            descriptor,
            McpOperationKind.TASK_UPDATE,
            "tool:jobs.start",
            {"inputResponses": {"approval": _elicitation_response({"decision": "current"})}},
            task_id=started.task_handle.task_id,
        )
        current = await client.update_task(current_request)
        assert current.status is McpOperationStatus.TASK_UPDATED

        behavior.release_update.set()
        assert (await old_update).status is McpOperationStatus.TASK_UPDATED
        _assert_failed(
            await client.update_task(current_request),
            McpClientErrorReason.TASK_INVALID,
        )
        assert behavior.update_responses == [
            {"approval": _elicitation_response({"approved": True})},
            {"approval": _elicitation_response({"decision": "current"})},
        ]
        await client.close()

    asyncio.run(exercise())


def test_mixed_input_reservation_is_atomic_when_one_authority_is_already_reserved() -> None:
    """Catches a rejected multi-ID update leaving its unconflicted ID reserved."""

    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.task_handle is not None
        note_request = _elicitation_request(
            {
                "type": "object",
                "properties": {"note": {"type": "string"}},
                "required": ["note"],
                "additionalProperties": False,
            }
        )
        behavior.status = "input_required"
        behavior.input_requests = {
            "approval": behavior.input_requests["approval"],
            "note": note_request,
        }
        assert (
            await client.get_task(
                _request(
                    descriptor,
                    McpOperationKind.TASK_GET,
                    "tool:jobs.start",
                    task_id=started.task_handle.task_id,
                )
            )
        ).status is McpOperationStatus.COMPLETED

        behavior.update_entered = asyncio.Event()
        behavior.release_update = asyncio.Event()
        behavior.update_wait_limit = 1
        approval_request = _request(
            descriptor,
            McpOperationKind.TASK_UPDATE,
            "tool:jobs.start",
            {"inputResponses": {"approval": _elicitation_response({"approved": True})}},
            task_id=started.task_handle.task_id,
        )
        approval_update = asyncio.create_task(client.update_task(approval_request))
        await asyncio.wait_for(behavior.update_entered.wait(), timeout=1)

        mixed = await client.update_task(
            _request(
                descriptor,
                McpOperationKind.TASK_UPDATE,
                "tool:jobs.start",
                {
                    "inputResponses": {
                        "approval": _elicitation_response({"approved": False}),
                        "note": _elicitation_response({"note": "mixed"}),
                    }
                },
                task_id=started.task_handle.task_id,
            )
        )
        _assert_failed(mixed, McpClientErrorReason.TASK_INVALID)
        note_update = await client.update_task(
            _request(
                descriptor,
                McpOperationKind.TASK_UPDATE,
                "tool:jobs.start",
                {"inputResponses": {"note": _elicitation_response({"note": "free"})}},
                task_id=started.task_handle.task_id,
            )
        )
        assert note_update.status is McpOperationStatus.TASK_UPDATED

        behavior.release_update.set()
        assert (await approval_update).status is McpOperationStatus.TASK_UPDATED
        assert behavior.update_responses == [
            {"approval": _elicitation_response({"approved": True})},
            {"note": _elicitation_response({"note": "free"})},
        ]
        await client.close()

    asyncio.run(exercise())


def test_cancelling_old_generation_releases_only_its_exact_reservation() -> None:
    """Catches old cleanup erasing a concurrent newer-generation reservation."""

    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.task_handle is not None
        behavior.status = "input_required"
        get_request = _request(
            descriptor,
            McpOperationKind.TASK_GET,
            "tool:jobs.start",
            task_id=started.task_handle.task_id,
        )
        assert (await client.get_task(get_request)).status is McpOperationStatus.COMPLETED

        behavior.update_entered = asyncio.Event()
        behavior.release_update = asyncio.Event()
        behavior.update_wait_limit = 2
        old_update = asyncio.create_task(
            client.update_task(
                _request(
                    descriptor,
                    McpOperationKind.TASK_UPDATE,
                    "tool:jobs.start",
                    {"inputResponses": {"approval": _elicitation_response({"approved": True})}},
                    task_id=started.task_handle.task_id,
                )
            )
        )
        await asyncio.wait_for(behavior.update_entered.wait(), timeout=1)
        behavior.update_entered.clear()

        changed = _elicitation_request(
            {
                "type": "object",
                "properties": {"decision": {"type": "string"}},
                "required": ["decision"],
                "additionalProperties": False,
            }
        )
        behavior.input_requests = {"approval": changed}
        assert (await client.get_task(get_request)).status is McpOperationStatus.COMPLETED
        current_request = _request(
            descriptor,
            McpOperationKind.TASK_UPDATE,
            "tool:jobs.start",
            {"inputResponses": {"approval": _elicitation_response({"decision": "current"})}},
            task_id=started.task_handle.task_id,
        )
        current_update = asyncio.create_task(client.update_task(current_request))
        await asyncio.wait_for(behavior.update_entered.wait(), timeout=1)

        old_update.cancel()
        with pytest.raises(asyncio.CancelledError):
            await old_update
        duplicate = await client.update_task(current_request)
        _assert_failed(duplicate, McpClientErrorReason.TASK_INVALID)
        assert len(behavior.update_responses) == 2

        behavior.release_update.set()
        assert (await current_update).status is McpOperationStatus.TASK_UPDATED
        await client.close()

    asyncio.run(exercise())


def test_invalid_update_ack_releases_reservation_for_retry() -> None:
    """Catches result-validation failure leaking an input-authority reservation."""

    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.task_handle is not None
        behavior.status = "input_required"
        assert (
            await client.get_task(
                _request(
                    descriptor,
                    McpOperationKind.TASK_GET,
                    "tool:jobs.start",
                    task_id=started.task_handle.task_id,
                )
            )
        ).status is McpOperationStatus.COMPLETED
        update_request = _request(
            descriptor,
            McpOperationKind.TASK_UPDATE,
            "tool:jobs.start",
            {"inputResponses": {"approval": _elicitation_response({"approved": True})}},
            task_id=started.task_handle.task_id,
        )
        behavior.update_metadata = {
            **_metadata(descriptor),
            "appId": "com.example.attacker",
        }
        invalid = await client.update_task(update_request)
        _assert_failed(invalid, McpClientErrorReason.IDENTITY_MISMATCH)

        behavior.update_metadata = None
        retried = await client.update_task(update_request)
        assert retried.status is McpOperationStatus.TASK_UPDATED
        assert len(behavior.update_responses) == 2
        await client.close()

    asyncio.run(exercise())


def test_old_update_ack_requires_generation_when_newer_request_returns_to_same_shape() -> None:
    """Catches fingerprint-only matching across changed-away-and-back generations."""

    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.task_handle is not None
        behavior.status = "input_required"
        original = behavior.input_requests["approval"]
        get_request = _request(
            descriptor,
            McpOperationKind.TASK_GET,
            "tool:jobs.start",
            task_id=started.task_handle.task_id,
        )
        assert (await client.get_task(get_request)).status is McpOperationStatus.COMPLETED

        update_entered = asyncio.Event()
        release_update = asyncio.Event()
        behavior.update_entered = update_entered
        behavior.release_update = release_update
        old_update_request = _request(
            descriptor,
            McpOperationKind.TASK_UPDATE,
            "tool:jobs.start",
            {"inputResponses": {"approval": _elicitation_response({"approved": True})}},
            task_id=started.task_handle.task_id,
        )
        old_update = asyncio.create_task(client.update_task(old_update_request))
        await asyncio.wait_for(update_entered.wait(), timeout=1)

        behavior.input_requests = {
            "approval": _elicitation_request(
                {
                    "type": "object",
                    "properties": {"decision": {"type": "string"}},
                    "required": ["decision"],
                    "additionalProperties": False,
                }
            )
        }
        assert (await client.get_task(get_request)).status is McpOperationStatus.COMPLETED
        behavior.input_requests = {"approval": original}
        assert (await client.get_task(get_request)).status is McpOperationStatus.COMPLETED

        release_update.set()
        assert (await old_update).status is McpOperationStatus.TASK_UPDATED
        behavior.update_entered = None
        behavior.release_update = None

        current = await client.update_task(old_update_request)
        assert current.status is McpOperationStatus.TASK_UPDATED
        replay = await client.update_task(old_update_request)
        _assert_failed(replay, McpClientErrorReason.TASK_INVALID)
        await client.close()

    asyncio.run(exercise())


def test_old_update_ack_consumes_only_unchanged_authorities_from_mixed_generations() -> None:
    """Catches all-or-none generation handling for a partial multi-ID acknowledgment."""

    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.task_handle is not None
        note = _elicitation_request(
            {
                "type": "object",
                "properties": {"note": {"type": "string"}},
                "required": ["note"],
                "additionalProperties": False,
            },
            message="Add a note",
        )
        behavior.status = "input_required"
        behavior.input_requests = {
            "approval": behavior.input_requests["approval"],
            "note": note,
        }
        get_request = _request(
            descriptor,
            McpOperationKind.TASK_GET,
            "tool:jobs.start",
            task_id=started.task_handle.task_id,
        )
        assert (await client.get_task(get_request)).status is McpOperationStatus.COMPLETED

        update_entered = asyncio.Event()
        release_update = asyncio.Event()
        behavior.update_entered = update_entered
        behavior.release_update = release_update
        old_update = asyncio.create_task(
            client.update_task(
                _request(
                    descriptor,
                    McpOperationKind.TASK_UPDATE,
                    "tool:jobs.start",
                    {
                        "inputResponses": {
                            "approval": _elicitation_response({"approved": True}),
                            "note": _elicitation_response({"note": "old"}),
                        }
                    },
                    task_id=started.task_handle.task_id,
                )
            )
        )
        await asyncio.wait_for(update_entered.wait(), timeout=1)

        changed = _elicitation_request(
            {
                "type": "object",
                "properties": {"decision": {"type": "string"}},
                "required": ["decision"],
                "additionalProperties": False,
            }
        )
        behavior.input_requests = {"approval": changed, "note": note}
        assert (await client.get_task(get_request)).status is McpOperationStatus.COMPLETED
        release_update.set()
        assert (await old_update).status is McpOperationStatus.TASK_UPDATED
        behavior.update_entered = None
        behavior.release_update = None

        consumed_note = await client.update_task(
            _request(
                descriptor,
                McpOperationKind.TASK_UPDATE,
                "tool:jobs.start",
                {"inputResponses": {"note": _elicitation_response({"note": "replay"})}},
                task_id=started.task_handle.task_id,
            )
        )
        _assert_failed(consumed_note, McpClientErrorReason.TASK_INVALID)
        current_approval = await client.update_task(
            _request(
                descriptor,
                McpOperationKind.TASK_UPDATE,
                "tool:jobs.start",
                {"inputResponses": {"approval": _elicitation_response({"decision": "current"})}},
                task_id=started.task_handle.task_id,
            )
        )
        assert current_approval.status is McpOperationStatus.TASK_UPDATED
        await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("outcome", ["failure", "cancellation"])
def test_unsuccessful_old_update_preserves_newer_same_id_authority(outcome: str) -> None:
    """Catches failed or cancelled old wire work disturbing a later generation."""

    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.task_handle is not None
        behavior.status = "input_required"
        get_request = _request(
            descriptor,
            McpOperationKind.TASK_GET,
            "tool:jobs.start",
            task_id=started.task_handle.task_id,
        )
        assert (await client.get_task(get_request)).status is McpOperationStatus.COMPLETED

        update_entered = asyncio.Event()
        release_update = asyncio.Event()
        behavior.update_entered = update_entered
        behavior.release_update = release_update
        if outcome == "failure":
            behavior.update_error = RuntimeError("old generation failed")
        old_update = asyncio.create_task(
            client.update_task(
                _request(
                    descriptor,
                    McpOperationKind.TASK_UPDATE,
                    "tool:jobs.start",
                    {"inputResponses": {"approval": _elicitation_response({"approved": True})}},
                    task_id=started.task_handle.task_id,
                )
            )
        )
        await asyncio.wait_for(update_entered.wait(), timeout=1)

        changed = _elicitation_request(
            {
                "type": "object",
                "properties": {"decision": {"type": "string"}},
                "required": ["decision"],
                "additionalProperties": False,
            }
        )
        behavior.input_requests = {"approval": changed}
        assert (await client.get_task(get_request)).status is McpOperationStatus.COMPLETED
        if outcome == "cancellation":
            old_update.cancel()
        release_update.set()
        if outcome == "cancellation":
            with pytest.raises(asyncio.CancelledError):
                await old_update
        else:
            _assert_failed(await old_update, McpClientErrorReason.TRANSPORT_UNAVAILABLE)
        behavior.update_entered = None
        behavior.release_update = None

        current = await client.update_task(
            _request(
                descriptor,
                McpOperationKind.TASK_UPDATE,
                "tool:jobs.start",
                {"inputResponses": {"approval": _elicitation_response({"decision": "current"})}},
                task_id=started.task_handle.task_id,
            )
        )
        assert current.status is McpOperationStatus.TASK_UPDATED
        await client.close()

    asyncio.run(exercise())


def test_new_input_poll_after_ack_uses_distinct_authority_and_replay_stays_retired() -> None:
    """Catches an acknowledged ID leaking into a later distinct request snapshot."""

    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.task_handle is not None
        behavior.status = "input_required"
        get_request = _request(
            descriptor,
            McpOperationKind.TASK_GET,
            "tool:jobs.start",
            task_id=started.task_handle.task_id,
        )
        assert (await client.get_task(get_request)).status is McpOperationStatus.COMPLETED
        approval_update = _request(
            descriptor,
            McpOperationKind.TASK_UPDATE,
            "tool:jobs.start",
            {"inputResponses": {"approval": _elicitation_response({"approved": True})}},
            task_id=started.task_handle.task_id,
        )
        assert (await client.update_task(approval_update)).status is McpOperationStatus.TASK_UPDATED
        _assert_failed(
            await client.update_task(approval_update),
            McpClientErrorReason.TASK_INVALID,
        )

        decision = _elicitation_request(
            {
                "type": "object",
                "properties": {"decision": {"type": "string"}},
                "required": ["decision"],
                "additionalProperties": False,
            }
        )
        behavior.input_requests = {"decision": decision}
        later = await client.get_task(get_request)
        assert later.status is McpOperationStatus.COMPLETED
        current = await client.update_task(
            _request(
                descriptor,
                McpOperationKind.TASK_UPDATE,
                "tool:jobs.start",
                {"inputResponses": {"decision": _elicitation_response({"decision": "current"})}},
                task_id=started.task_handle.task_id,
            )
        )
        assert current.status is McpOperationStatus.TASK_UPDATED
        await client.close()

    asyncio.run(exercise())


def test_terminal_poll_during_old_update_does_not_restore_input_authority() -> None:
    """Catches a late update acknowledgment mutating a terminal task transition."""

    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.task_handle is not None
        behavior.status = "input_required"
        get_request = _request(
            descriptor,
            McpOperationKind.TASK_GET,
            "tool:jobs.start",
            task_id=started.task_handle.task_id,
        )
        assert (await client.get_task(get_request)).status is McpOperationStatus.COMPLETED

        update_entered = asyncio.Event()
        release_update = asyncio.Event()
        behavior.update_entered = update_entered
        behavior.release_update = release_update
        update_request = _request(
            descriptor,
            McpOperationKind.TASK_UPDATE,
            "tool:jobs.start",
            {"inputResponses": {"approval": _elicitation_response({"approved": True})}},
            task_id=started.task_handle.task_id,
        )
        old_update = asyncio.create_task(client.update_task(update_request))
        await asyncio.wait_for(update_entered.wait(), timeout=1)
        behavior.status = "completed"
        assert (await client.get_task(get_request)).status is McpOperationStatus.COMPLETED
        release_update.set()
        assert (await old_update).status is McpOperationStatus.TASK_UPDATED
        _assert_failed(await client.update_task(update_request), McpClientErrorReason.TASK_INVALID)
        _assert_failed(await client.get_task(get_request), McpClientErrorReason.TASK_INVALID)
        await client.close()

    asyncio.run(exercise())


def test_close_cancels_blocked_task_update_without_deadlock() -> None:
    """Catches task-transition coordination preventing close-time cancellation."""

    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.task_handle is not None
        behavior.status = "input_required"
        assert (
            await client.get_task(
                _request(
                    descriptor,
                    McpOperationKind.TASK_GET,
                    "tool:jobs.start",
                    task_id=started.task_handle.task_id,
                )
            )
        ).status is McpOperationStatus.COMPLETED
        behavior.update_entered = asyncio.Event()
        behavior.release_update = asyncio.Event()
        blocked = asyncio.create_task(
            client.update_task(
                _request(
                    descriptor,
                    McpOperationKind.TASK_UPDATE,
                    "tool:jobs.start",
                    {"inputResponses": {"approval": _elicitation_response({"approved": True})}},
                    task_id=started.task_handle.task_id,
                )
            )
        )
        await asyncio.wait_for(behavior.update_entered.wait(), timeout=1)
        await asyncio.wait_for(client.close(), timeout=2)
        with pytest.raises(asyncio.CancelledError):
            await blocked

    asyncio.run(exercise())


def test_cancelled_task_update_retains_outstanding_response_for_retry() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.task_handle is not None
        behavior.status = "input_required"
        polled = await client.get_task(
            _request(
                descriptor,
                McpOperationKind.TASK_GET,
                "tool:jobs.start",
                task_id=started.task_handle.task_id,
            )
        )
        assert polled.status is McpOperationStatus.COMPLETED
        update_request = _request(
            descriptor,
            McpOperationKind.TASK_UPDATE,
            "tool:jobs.start",
            {"inputResponses": {"approval": _elicitation_response({"approved": True})}},
            task_id=started.task_handle.task_id,
        )
        for _ in range(2):
            entered = asyncio.Event()
            release = asyncio.Event()
            behavior.update_entered = entered
            behavior.release_update = release
            pending = asyncio.create_task(client.update_task(update_request))
            await entered.wait()
            pending.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
        behavior.update_entered = None
        behavior.release_update = None
        retried = await client.update_task(update_request)
        assert retried.status is McpOperationStatus.TASK_UPDATED
        await client.close()

    asyncio.run(exercise())


def test_task_registry_owns_authority_independently_of_returned_handle() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        handle = started.task_handle
        assert handle is not None
        object.__setattr__(handle, "capability_id", "tool:jobs.other")
        behavior.status = "working"
        polled = await client.get_task(
            _request(
                descriptor,
                McpOperationKind.TASK_GET,
                "tool:jobs.start",
                task_id="task-1",
            )
        )
        assert polled.status is McpOperationStatus.COMPLETED
        assert polled.data["task"]["task_id"] == "task-1"
        await client.close()

    asyncio.run(exercise())


def test_task_terminal_result_uses_originating_signed_output_policy() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.task_handle is not None
        behavior.status = "completed"
        behavior.completed_result = _tool_result_envelope(structured_content={"answer": "wrong"})
        invalid = await client.get_task(
            _request(
                descriptor,
                McpOperationKind.TASK_GET,
                "tool:jobs.start",
                task_id=started.task_handle.task_id,
            )
        )
        _assert_failed(invalid, McpClientErrorReason.RESULT_INVALID)
        await client.close()

    asyncio.run(exercise())


def test_completed_task_parses_full_call_tool_result_envelope_without_semantic_loss() -> None:
    async def exercise() -> None:
        descriptor = _descriptor(
            tool_allowed_mime_types=("application/json", "text/plain"),
        )
        behavior = _TaskBehavior()
        client = CurrentMcpClient(
            descriptor,
            SdkClientFactory.for_in_process_server(_server(descriptor, behavior)),
        )
        await client.start()
        started = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert started.task_handle is not None
        behavior.status = "completed"
        behavior.completed_result = _tool_result_envelope(
            structured_content={"answer": 42},
            content=[{"type": "text", "text": "done"}],
        )
        completed = await client.get_task(
            _request(
                descriptor,
                McpOperationKind.TASK_GET,
                "tool:jobs.start",
                task_id=started.task_handle.task_id,
            )
        )
        assert completed.status is McpOperationStatus.COMPLETED
        assert completed.data == {
            "content": ({"type": "text", "text": "done"},),
            "structured_content": {"answer": 42},
            "is_error": False,
        }

        errored = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run again"},
            )
        )
        assert errored.task_handle is not None
        behavior.completed_result = _tool_result_envelope(
            content=[{"type": "text", "text": "bounded error"}],
            is_error=True,
        )
        error_envelope = await client.get_task(
            _request(
                descriptor,
                McpOperationKind.TASK_GET,
                "tool:jobs.start",
                task_id=errored.task_handle.task_id,
            )
        )
        assert error_envelope.data == {
            "content": ({"type": "text", "text": "bounded error"},),
            "structured_content": None,
            "is_error": True,
        }
        await client.close()

    asyncio.run(exercise())


def test_completed_task_rejects_nonclosed_or_missing_call_tool_result_envelope() -> None:
    async def exercise() -> None:
        for envelope in (
            {"structuredContent": {"answer": 42}, "resultType": "complete"},
            {
                **_tool_result_envelope(structured_content={"answer": 42}),
                "serverSecret": "must not cross",
            },
            _tool_result_envelope(structured_content={"answer": 42})
            | {"resultType": "input_required"},
        ):
            client, descriptor, behavior = await _started()
            started = await client.call_tool(
                _request(
                    descriptor,
                    McpOperationKind.TOOL_CALL,
                    "tool:jobs.start",
                    {"query": "run"},
                )
            )
            assert started.task_handle is not None
            behavior.status = "completed"
            behavior.completed_result = envelope
            invalid = await client.get_task(
                _request(
                    descriptor,
                    McpOperationKind.TASK_GET,
                    "tool:jobs.start",
                    task_id=started.task_handle.task_id,
                )
            )
            _assert_failed(invalid, McpClientErrorReason.RESULT_INVALID)
            assert "must not cross" not in repr(invalid)
            await client.close()

    asyncio.run(exercise())


def test_post_create_validation_failure_cancels_exact_server_task() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        behavior.create_metadata = {
            **_metadata(descriptor),
            "appId": "com.example.attacker",
        }
        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        _assert_failed(result, McpClientErrorReason.IDENTITY_MISMATCH)
        assert behavior.calls == [
            ("tools/call", "task-1"),
            ("tasks/cancel", "task-1"),
        ]
        await client.close()

    asyncio.run(exercise())


def test_post_create_cancel_ambiguity_returns_bound_recovery_handle() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        behavior.create_metadata = {
            **_metadata(descriptor),
            "appId": "com.example.attacker",
        }
        behavior.cancel_error = RuntimeError("ambiguous cleanup")
        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert result.status is McpOperationStatus.TASK_RECOVERY_REQUIRED
        assert result.task_handle is not None
        assert "ambiguous cleanup" not in repr(result)
        behavior.create_metadata = None
        recovered = await client.cancel_task(
            _request(
                descriptor,
                McpOperationKind.TASK_CANCEL,
                "tool:jobs.start",
                task_id=result.task_handle.task_id,
            )
        )
        assert recovered.status is McpOperationStatus.TASK_UPDATED
        assert behavior.calls == [
            ("tools/call", "task-1"),
            ("tasks/cancel", "task-1"),
            ("tasks/cancel", "task-1"),
        ]
        await client.close()

    asyncio.run(exercise())


def test_duplicate_task_id_keeps_independent_origin_authority_for_recovery() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        original = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "first"},
            )
        )
        assert original.task_handle is not None
        behavior.next_task = 1
        behavior.create_metadata = {
            **_metadata(descriptor),
            "appId": "com.example.attacker",
        }
        behavior.cancel_error = RuntimeError("ambiguous duplicate cleanup")
        duplicate = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.other",
                {"query": "second"},
            )
        )
        assert duplicate.status is McpOperationStatus.TASK_RECOVERY_REQUIRED
        assert duplicate.task_handle is not None
        assert duplicate.task_handle.task_id == original.task_handle.task_id == "task-1"
        assert duplicate.task_handle.capability_id == "tool:jobs.other"
        assert "ambiguous duplicate cleanup" not in repr(duplicate)

        behavior.create_metadata = None
        recovered = await client.cancel_task(
            _request(
                descriptor,
                McpOperationKind.TASK_CANCEL,
                "tool:jobs.other",
                task_id=duplicate.task_handle.task_id,
            )
        )
        assert recovered.status is McpOperationStatus.TASK_UPDATED
        behavior.status = "working"
        original_still_bound = await client.get_task(
            _request(
                descriptor,
                McpOperationKind.TASK_GET,
                "tool:jobs.start",
                task_id=original.task_handle.task_id,
            )
        )
        assert original_still_bound.status is McpOperationStatus.COMPLETED
        await client.close()

    asyncio.run(exercise())


def test_post_create_cleanup_timeout_retains_a_usable_recovery_handle() -> None:
    async def exercise() -> None:
        limits = McpClientLimits(max_shutdown_duration_ms=10)
        client, descriptor, behavior = await _started(limits=limits)
        behavior.create_metadata = {
            **_metadata(descriptor),
            "appId": "com.example.attacker",
        }
        behavior.cancel_entered = asyncio.Event()
        behavior.release_cancel = asyncio.Event()
        result = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "run"},
            )
        )
        assert result.status is McpOperationStatus.TASK_RECOVERY_REQUIRED
        assert result.task_handle is not None
        behavior.create_metadata = None
        behavior.release_cancel.set()
        cancelled = await client.cancel_task(
            _request(
                descriptor,
                McpOperationKind.TASK_CANCEL,
                "tool:jobs.start",
                task_id=result.task_handle.task_id,
            )
        )
        assert cancelled.status is McpOperationStatus.TASK_UPDATED
        await client.close()

    asyncio.run(exercise())


def test_post_create_cleanup_completes_before_preserving_caller_cancellation() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started()
        behavior.create_metadata = {
            **_metadata(descriptor),
            "appId": "com.example.attacker",
        }
        behavior.cancel_entered = asyncio.Event()
        behavior.release_cancel = asyncio.Event()
        pending = asyncio.create_task(
            client.call_tool(
                _request(
                    descriptor,
                    McpOperationKind.TOOL_CALL,
                    "tool:jobs.start",
                    {"query": "run"},
                )
            )
        )
        await behavior.cancel_entered.wait()
        pending.cancel()
        behavior.release_cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert behavior.calls == [
            ("tools/call", "task-1"),
            ("tasks/cancel", "task-1"),
        ]
        await client.close()

    asyncio.run(exercise())


def test_active_task_limit_reserves_before_any_second_tool_side_effect() -> None:
    async def exercise() -> None:
        limits = McpClientLimits(max_active_tasks=1, max_task_tombstones=2)
        client, descriptor, behavior = await _started(limits=limits)
        first = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "first"},
            )
        )
        assert first.status is McpOperationStatus.TASK_STARTED
        calls_before = list(behavior.calls)
        second = await client.call_tool(
            _request(
                descriptor,
                McpOperationKind.TOOL_CALL,
                "tool:jobs.start",
                {"query": "must-not-run"},
            )
        )
        _assert_failed(second, McpClientErrorReason.TASK_INVALID)
        assert behavior.calls == calls_before
        assert len(client._tasks) == 1
        await client.close()

    asyncio.run(exercise())


def test_detached_task_creation_keeps_single_slot_reserved_until_result_is_owned() -> None:
    async def exercise() -> None:
        limits = replace(
            McpClientLimits(),
            max_active_tasks=1,
            max_task_tombstones=2,
            max_operation_duration_ms=20,
            max_shutdown_duration_ms=20,
        )
        client, descriptor, behavior = await _started(limits=limits)
        behavior.call_entered = asyncio.Queue()
        behavior.release_call = asyncio.Event()
        behavior.resist_call_cancellation = True

        class _CapacityCheckedTasks(dict[Any, Any]):
            def __setitem__(self, key: Any, value: Any) -> None:
                assert (
                    key in self
                    or len(self) + client._pending_task_creations < limits.max_active_tasks
                )
                super().__setitem__(key, value)

        client._tasks = _CapacityCheckedTasks()
        try:
            first = await client.call_tool(
                _request(
                    descriptor,
                    McpOperationKind.TOOL_CALL,
                    "tool:jobs.start",
                    {"query": "first-resistant"},
                    request_id="resistant-task-1",
                    cancellation_id="resistant-cancel-1",
                )
            )
            _assert_failed(first, McpClientErrorReason.CLEANUP_REQUIRED)
            assert await behavior.call_entered.get() == "task-1"

            second = await client.call_tool(
                _request(
                    descriptor,
                    McpOperationKind.TOOL_CALL,
                    "tool:jobs.start",
                    {"query": "must-not-run"},
                    request_id="resistant-task-2",
                    cancellation_id="resistant-cancel-2",
                )
            )
            _assert_failed(second, McpClientErrorReason.TASK_INVALID)
            assert behavior.calls == [("tools/call", "task-1")]
            assert client._pending_task_creations == 1
            assert len(client._tasks) + len(client._task_tombstones) <= 1

            behavior.release_call.set()
            survivors = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and task.get_name().startswith("omega-mcp-operation:")
                and not task.done()
            ]
            await asyncio.gather(*survivors, return_exceptions=True)
            await asyncio.sleep(0)
            assert client._pending_task_creations == 0
            assert len(client._tasks) == 1
            assert len(client._tasks) + len(client._task_tombstones) <= 1
        finally:
            behavior.release_call.set()
            survivors = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and task.get_name().startswith("omega-mcp-operation:")
                and not task.done()
            ]
            await asyncio.gather(*survivors, return_exceptions=True)
            await asyncio.sleep(0)
            await client.close()

    asyncio.run(exercise())


def test_tombstone_replay_retains_task_slot_until_resistant_cleanup_is_joined() -> None:
    async def exercise() -> None:
        limits = replace(
            McpClientLimits(),
            max_active_tasks=1,
            max_task_tombstones=2,
            max_operation_duration_ms=40,
            max_shutdown_duration_ms=20,
        )
        client, descriptor, behavior = await _started(limits=limits)
        behavior.release_cancel = asyncio.Event()
        try:
            original = await client.call_tool(
                _request(
                    descriptor,
                    McpOperationKind.TOOL_CALL,
                    "tool:jobs.start",
                    {"query": "original"},
                    request_id="replay-original",
                    cancellation_id="replay-original-cancel",
                )
            )
            assert original.task_handle is not None
            behavior.release_cancel.set()
            retired = await client.cancel_task(
                _request(
                    descriptor,
                    McpOperationKind.TASK_CANCEL,
                    "tool:jobs.start",
                    task_id=original.task_handle.task_id,
                    request_id="retire-original",
                    cancellation_id="retire-original-cancel",
                )
            )
            assert retired.status is McpOperationStatus.TASK_UPDATED
            assert client._tasks == {}
            assert len(client._task_tombstones) == 1

            behavior.next_task = 1
            behavior.cancel_entered = asyncio.Event()
            behavior.release_cancel = asyncio.Event()
            behavior.resist_cancel_cancellation = True
            replayed = asyncio.create_task(
                client.call_tool(
                    _request(
                        descriptor,
                        McpOperationKind.TOOL_CALL,
                        "tool:jobs.start",
                        {"query": "replayed"},
                        request_id="replay-live-cleanup",
                        cancellation_id="replay-live-cleanup-cancel",
                    )
                )
            )
            await behavior.cancel_entered.wait()
            replay_result = await asyncio.wait_for(replayed, timeout=0.5)
            _assert_failed(replay_result, McpClientErrorReason.CLEANUP_REQUIRED)

            owners = [task for task in client._active_operations if not task.done()]
            assert len(owners) == 1
            assert client._pending_task_creations == 1
            calls_before_second = list(behavior.calls)
            second = await client.call_tool(
                _request(
                    descriptor,
                    McpOperationKind.TOOL_CALL,
                    "tool:jobs.start",
                    {"query": "must-not-run"},
                    request_id="replay-capacity-reject",
                    cancellation_id="replay-capacity-reject-cancel",
                )
            )
            _assert_failed(second, McpClientErrorReason.TASK_INVALID)
            assert behavior.calls == calls_before_second
            assert client._pending_task_creations == 1

            behavior.release_cancel.set()
            await asyncio.gather(*owners, return_exceptions=True)
            await asyncio.sleep(0)
            assert client._pending_task_creations == 0
            assert not client._active_operations

            retry = await client.call_tool(
                _request(
                    descriptor,
                    McpOperationKind.TOOL_CALL,
                    "tool:jobs.start",
                    {"query": "after-recovery"},
                    request_id="replay-after-recovery",
                    cancellation_id="replay-after-recovery-cancel",
                )
            )
            assert retry.status is McpOperationStatus.TASK_STARTED
            assert retry.task_handle is not None
            assert retry.task_handle.task_id == "task-2"
            assert client._pending_task_creations == 0
            assert len(client._tasks) == 1
        finally:
            behavior.resist_cancel_cancellation = False
            if behavior.release_cancel is not None:
                behavior.release_cancel.set()
            owners = [task for task in client._active_operations if not task.done()]
            if owners:
                await asyncio.gather(*owners, return_exceptions=True)
            await asyncio.sleep(0)
            await client.close()

    asyncio.run(exercise())


def test_terminal_and_cancelled_tasks_evict_with_separately_bounded_tombstones() -> None:
    async def exercise() -> None:
        limits = McpClientLimits(max_active_tasks=2, max_task_tombstones=2)
        client, descriptor, behavior = await _started(limits=limits)

        class _CapacityCheckedTombstones(dict[tuple[str, str, str, str, str, str], None]):
            def __setitem__(
                self,
                key: tuple[str, str, str, str, str, str],
                value: None,
            ) -> None:
                assert key in self or len(self) < limits.max_task_tombstones
                super().__setitem__(key, value)

        client._task_tombstones = _CapacityCheckedTombstones()
        completed_ids: list[str] = []
        for index in range(4):
            behavior.status = "working"
            started = await client.call_tool(
                _request(
                    descriptor,
                    McpOperationKind.TOOL_CALL,
                    "tool:jobs.start",
                    {"query": f"run-{index}"},
                )
            )
            assert started.task_handle is not None
            completed_ids.append(started.task_handle.task_id)
            if index % 2:
                terminal = await client.cancel_task(
                    _request(
                        descriptor,
                        McpOperationKind.TASK_CANCEL,
                        "tool:jobs.start",
                        task_id=started.task_handle.task_id,
                    )
                )
                assert terminal.status is McpOperationStatus.TASK_UPDATED
            else:
                behavior.status = "completed"
                terminal = await client.get_task(
                    _request(
                        descriptor,
                        McpOperationKind.TASK_GET,
                        "tool:jobs.start",
                        task_id=started.task_handle.task_id,
                    )
                )
                assert terminal.status is McpOperationStatus.COMPLETED
            assert client._tasks == {}
            assert len(client._task_tombstones) <= 2
        assert tuple(key[0] for key in client._task_tombstones)[-2:] == tuple(completed_ids[-2:])
        await client.close()

    asyncio.run(exercise())


def test_inconclusive_created_task_cleanup_preserves_exact_caller_cancellation() -> None:
    async def exercise() -> None:
        client, descriptor, behavior = await _started(
            limits=McpClientLimits(max_shutdown_duration_ms=10)
        )
        behavior.create_metadata = {
            **_metadata(descriptor),
            "appId": "com.example.attacker",
        }
        behavior.cancel_entered = asyncio.Event()
        behavior.release_cancel = asyncio.Event()
        propagated: list[asyncio.CancelledError] = []

        async def call_and_capture() -> McpOperationResult:
            try:
                return await client.call_tool(
                    _request(
                        descriptor,
                        McpOperationKind.TOOL_CALL,
                        "tool:jobs.start",
                        {"query": "run"},
                    )
                )
            except asyncio.CancelledError as error:
                propagated.append(error)
                raise

        pending = asyncio.create_task(call_and_capture())
        await behavior.cancel_entered.wait()
        pending.cancel("created-task-active-cancel")
        try:
            with pytest.raises(asyncio.CancelledError) as raised:
                await asyncio.wait_for(pending, timeout=0.5)
            assert propagated == [raised.value]
            assert raised.value.args == ("created-task-active-cancel",)
            assert len(client._tasks) == 1
        finally:
            behavior.release_cancel.set()
            await asyncio.sleep(0.02)
            await client.close()

    asyncio.run(exercise())


def test_tasks_surface_excludes_legacy_or_unscoped_operations_and_experimental_core() -> None:
    source = Path("mcp_platform/tasks.py").read_text(encoding="utf-8")
    client_source = Path("mcp_platform/client.py").read_text(encoding="utf-8")
    assert '"tasks/list"' not in source
    assert '"tasks/result"' not in source
    assert "experimental" not in source
    assert "ServerTasksCapability" not in source
    assert "capabilities.tasks," in client_source
    assert "from mcp._" not in source
    assert "import mcp._" not in source

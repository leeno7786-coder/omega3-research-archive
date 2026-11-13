from __future__ import annotations

import asyncio
import json
import os
import sys
import re
import tempfile
import warnings
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server.subscriptions import ToolsListChanged
from pydantic.warnings import TypedDictExtraConfigWarning

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.package_contracts import (
    MCP_PROTOCOL_VERSION,
    ResourceCatalog,
    ToolCatalog,
)
from mcp_platform.sdk import ConformanceIdentity, ConformanceReport, run_conformance

with warnings.catch_warnings():
    # mcp-types 2.0.0's public RequestParams metadata is an open TypedDict;
    # Pydantic warns when Omega's closed Tasks model inherits its public base.
    warnings.filterwarnings(
        "ignore",
        message=r"TypedDict class 'RequestParamsMeta' allows extra items, but 'extra' configuration is set to `'forbid'`\.",
        category=TypedDictExtraConfigWarning,
    )
    from mcp_platform.tasks import (
        CreateTaskResult,
        GetTaskParams,
        GetTaskRequest,
        GetTaskResult,
        TasksClientExtension,
        validate_create_task_result,
        validate_task_result,
    )

ROOT = Path(__file__).resolve().parents[2]
FAKE_SERVER = Path(__file__).with_name("fake_stdio_server.py")
SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"
PROTOCOL_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_INFO_KEY = "io.modelcontextprotocol/clientInfo"
CLIENT_CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
EXPECTED_SERVER_INFO = {"name": "omega.fake", "version": "1.0.0"}
EXPECTED_TOOLS = [
    "omega.fake.echo",
    "omega.fake.concurrent",
    "omega.fake.input",
    "omega.fake.task",
    "omega.fake.notify",
    "omega.fake.failure",
]


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
    tools = []
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
                    ["io.modelcontextprotocol/tasks"] if name == "omega.fake.task" else []
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


def _params() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[str(FAKE_SERVER), "--stdio"],
        env={"PYTHONUTF8": "1"},
        cwd=ROOT,
    )


def _current_meta() -> dict[str, object]:
    return {
        PROTOCOL_KEY: MCP_PROTOCOL_VERSION,
        CLIENT_INFO_KEY: {"name": "omega-task12-conformance", "version": "1.0.0"},
        CLIENT_CAPABILITIES_KEY: {"extensions": {"io.modelcontextprotocol/tasks": {}}},
    }


async def _raw_process(*extra_args: str) -> asyncio.subprocess.Process:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    return await asyncio.create_subprocess_exec(
        sys.executable,
        str(FAKE_SERVER),
        "--stdio",
        *extra_args,
        cwd=ROOT,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _raw_request(
    process: asyncio.subprocess.Process,
    request_id: int,
    method: str,
    params: Mapping[str, object],
) -> dict[str, Any]:
    await _raw_send(process, request_id, method, params)
    return await _raw_response(process)


async def _raw_send(
    process: asyncio.subprocess.Process,
    request_id: int,
    method: str,
    params: Mapping[str, object],
) -> None:
    assert process.stdin is not None
    process.stdin.write(
        json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    await process.stdin.drain()


async def _raw_response(process: asyncio.subprocess.Process) -> dict[str, Any]:
    assert process.stdout is not None
    line = await asyncio.wait_for(process.stdout.readline(), timeout=5)
    assert line
    response = json.loads(line)
    assert response["jsonrpc"] == "2.0"
    return response


async def _close_raw(process: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    assert process.stdin is not None
    process.stdin.close()
    await process.stdin.wait_closed()
    await asyncio.wait_for(process.wait(), timeout=5)
    assert process.returncode == 0
    assert process.stdout is not None and process.stderr is not None
    return await process.stdout.read(), await process.stderr.read()


def _assert_identity(response: Mapping[str, Any]) -> None:
    result = response["result"]
    assert result["resultType"] == "complete"
    assert result["_meta"][SERVER_INFO_KEY] == EXPECTED_SERVER_INFO


def test_first_stdio_request_is_current_without_initialize_and_every_result_is_stamped() -> None:
    # Catches a hidden initialize dependency or an unstamped response after discovery.
    async def exercise() -> None:
        process = await _raw_process()
        try:
            responses = [
                await _raw_request(process, 1, "server/discover", {"_meta": _current_meta()}),
                await _raw_request(process, 2, "tools/list", {"_meta": _current_meta()}),
                await _raw_request(process, 3, "prompts/list", {"_meta": _current_meta()}),
                await _raw_request(process, 4, "resources/list", {"_meta": _current_meta()}),
                await _raw_request(
                    process,
                    5,
                    "resources/templates/list",
                    {"_meta": _current_meta()},
                ),
            ]
            for response in responses:
                _assert_identity(response)
            assert responses[0]["result"]["supportedVersions"] == [MCP_PROTOCOL_VERSION]
            assert responses[0]["result"]["capabilities"]["extensions"] == {
                "io.modelcontextprotocol/tasks": {}
            }
            assert [item["name"] for item in responses[1]["result"]["tools"]] == EXPECTED_TOOLS
            assert [item["name"] for item in responses[2]["result"]["prompts"]] == [
                "omega.fake.summarize"
            ]
            assert [item["uri"] for item in responses[3]["result"]["resources"]] == [
                "omega://fake/help",
                "omega://fake/failure",
            ]
            assert [
                item["uriTemplate"] for item in responses[4]["result"]["resourceTemplates"]
            ] == ["omega://fake/items/{id}"]
        finally:
            remaining_stdout, stderr = await _close_raw(process)
        assert remaining_stdout == b""
        visible_stderr = stderr.decode("utf-8", errors="strict")
        assert "Bearer " not in visible_stderr
        assert "provider-token-sentinel" not in visible_stderr

    asyncio.run(exercise())


def test_wrong_stdio_protocol_is_stably_rejected_without_establishing_legacy_state() -> None:
    # Catches dual-era fallback after a retired initialize request.
    async def exercise() -> None:
        process = await _raw_process()
        try:
            rejected = await _raw_request(
                process,
                1,
                "initialize",
                {
                    "protocolVersion": "2026-07-27",
                    "clientInfo": {"name": "retired", "version": "1"},
                    "capabilities": {},
                },
            )
            assert rejected["error"] == {
                "code": -32022,
                "message": "Unsupported protocol version",
                "data": {
                    "supported": [MCP_PROTOCOL_VERSION],
                    "requested": "2026-07-27",
                },
            }
            current = await _raw_request(
                process,
                2,
                "tools/list",
                {"_meta": _current_meta()},
            )
            _assert_identity(current)
        finally:
            remaining_stdout, _stderr = await _close_raw(process)
        assert remaining_stdout == b""

    asyncio.run(exercise())


def test_real_stdio_overlapping_requests_preserve_ids_payloads_and_shutdown() -> None:
    # Catches sequential dispatch or cross-talk hidden by send-then-wait tests.
    async def exercise() -> None:
        process = await _raw_process()
        try:
            await _raw_send(
                process,
                101,
                "tools/call",
                {
                    "name": "omega.fake.concurrent",
                    "arguments": {"text": "first"},
                    "_meta": _current_meta(),
                },
            )
            await _raw_send(
                process,
                202,
                "tools/call",
                {
                    "name": "omega.fake.concurrent",
                    "arguments": {"text": "second"},
                    "_meta": _current_meta(),
                },
            )
            responses = [
                await _raw_response(process),
                await _raw_response(process),
            ]
            assert all("result" in response for response in responses)
            by_id = {response["id"]: response["result"] for response in responses}
            assert by_id[101]["structuredContent"] == {"text": "first", "peak": 2}
            assert by_id[202]["structuredContent"] == {"text": "second", "peak": 2}
            for result in by_id.values():
                assert result["_meta"][SERVER_INFO_KEY] == EXPECTED_SERVER_INFO
        finally:
            remaining_stdout, stderr = await _close_raw(process)
        assert remaining_stdout == b""
        visible_stderr = stderr.decode("utf-8", errors="strict")
        assert "provider-token-sentinel" not in visible_stderr

    asyncio.run(exercise())


#: An SGR colour sequence: ESC, "[", parameters, "m". Anchored on the escape
#: character so this strips terminal colour and nothing that merely looks
#: like it -- a bare "[35m" in provider text stays visible to the secret
#: assertions below.
_ANSI = re.compile("" + r"\[[0-9;]*m")


def _plain(text: str) -> str:
    """The same text without terminal colour, for content assertions."""
    return _ANSI.sub("", text)


def test_real_stdio_cleanup_failure_stderr_is_content_free() -> None:
    # Catches a standalone context-exit secret reaching the process traceback.
    async def exercise() -> None:
        process = await _raw_process("--fail-cleanup")
        assert process.stdin is not None
        process.stdin.close()
        await process.stdin.wait_closed()
        await asyncio.wait_for(process.wait(), timeout=5)
        assert process.stdout is not None and process.stderr is not None
        stdout = await process.stdout.read()
        visible_stderr = (await process.stderr.read()).decode("utf-8", errors="strict")
        assert process.returncode == 1
        assert stdout == b""
        # Python 3.13 colours tracebacks, and the escapes land between the
        # exception's class and its message -- so a plain substring match for
        # the cause fails on formatting rather than on content. Strip them for
        # the assertion about what the traceback SAYS.
        assert "ServerCleanupError: server cleanup failed" in _plain(visible_stderr)
        # ...and check the raw bytes for what it must NOT say, so an escape
        # sequence can never split a secret out of view.
        for text in (visible_stderr, _plain(visible_stderr)):
            assert "standalone-cleanup-provider-token-sentinel" not in text
            assert "Bearer " not in text

    asyncio.run(exercise())


def test_stdio_current_input_cache_subscriptions_tasks_and_redaction() -> None:
    # Catches current extension behavior working only through fakes or leaking provider text.
    async def exercise() -> None:
        sentinel = "Bear" + "er task12-provider-token-sentinel"
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
            async with Client(
                stdio_client(_params(), errlog=errlog),
                mode=MCP_PROTOCOL_VERSION,
                client_info=types.Implementation(
                    name="omega-task12-conformance",
                    version="1.0.0",
                ),
                extensions=(TasksClientExtension(),),
                cache=None,
            ) as client:
                discovered = await client.session.send_request(
                    types.DiscoverRequest(),
                    types.DiscoverResult,
                )
                assert discovered.meta == {SERVER_INFO_KEY: EXPECTED_SERVER_INFO}
                assert discovered.capabilities.extensions == {"io.modelcontextprotocol/tasks": {}}
                tools = await client.session.list_tools()
                assert [tool.name for tool in tools.tools] == EXPECTED_TOOLS
                assert tools.ttl_ms == 1_250
                assert tools.cache_scope == "public"

                first = await client.session.call_tool(
                    "omega.fake.input",
                    {"text": "hello"},
                    allow_input_required=True,
                )
                assert type(first) is types.InputRequiredResult
                assert first.request_state == "opaque\x00state-雪"
                terminal = await client.session.call_tool(
                    "omega.fake.input",
                    {"text": "hello"},
                    request_state=first.request_state,
                    input_responses={
                        "confirm": types.ElicitResult(
                            action="accept",
                            content={"approved": True},
                        )
                    },
                    allow_input_required=True,
                )
                assert type(terminal) is types.CallToolResult
                assert terminal.structured_content == {"text": "hello"}

                created = await client.session.call_tool(
                    "omega.fake.task",
                    {"text": "task"},
                    allow_claimed=True,
                )
                assert type(created) is CreateTaskResult
                validate_create_task_result(created)
                task = await client.session.send_request(
                    GetTaskRequest(params=GetTaskParams(task_id=created.task_id)),
                    GetTaskResult,
                )
                validate_task_result("tasks/get", task)
                assert task.status == "completed"
                assert task.result == {"text": "task"}

                async with client.listen(tools_list_changed=True) as subscription:
                    notified = await client.call_tool(
                        "omega.fake.notify",
                        {"text": "changed"},
                    )
                    assert notified.structured_content == {"text": "changed"}
                    event = await asyncio.wait_for(anext(subscription), timeout=5)
                    assert type(event) is ToolsListChanged

                failed = await client.call_tool(
                    "omega.fake.failure",
                    {"text": "hello"},
                )
                assert failed.is_error is True
                visible = failed.model_dump_json(by_alias=True, exclude_none=True)
                assert "tool handler failed" in visible
                assert sentinel not in visible
            errlog.seek(0)
            assert sentinel not in errlog.read()

    asyncio.run(exercise())


class _ReportAdapter:
    def __init__(self) -> None:
        tools, _resources = _catalogs()
        self._tools = tuple(
            types.Tool(
                name=definition.name,
                title=definition.title,
                inputSchema=definition.to_dict()["input_schema"],
                outputSchema=definition.to_dict()["output_schema"],
            )
            for definition in tools.tools
        )

    def identity(self) -> ConformanceIdentity:
        return ConformanceIdentity(
            server_id="omega.fake",
            server_version="1.0.0",
            protocol_version=MCP_PROTOCOL_VERSION,
            capabilities=types.ServerCapabilities(
                tools=types.ToolsCapability(listChanged=True),
                prompts=types.PromptsCapability(listChanged=True),
                resources=types.ResourcesCapability(subscribe=True, listChanged=True),
                extensions={"io.modelcontextprotocol/tasks": {}},
            ),
        )

    def list_tools(self) -> Sequence[types.Tool]:
        return self._tools

    def list_prompts(self) -> Sequence[types.Prompt]:
        return (types.Prompt(name="omega.fake.summarize", title="Summarize"),)

    def list_resources(self) -> Sequence[types.Resource]:
        return (
            types.Resource(uri="omega://fake/help", name="help", mimeType="text/plain"),
            types.Resource(uri="omega://fake/failure", name="failure", mimeType="text/plain"),
        )

    def list_resource_templates(self) -> Sequence[types.ResourceTemplate]:
        return (
            types.ResourceTemplate(
                uriTemplate="omega://fake/items/{id}",
                name="item",
                mimeType="application/json",
            ),
        )

    def rejects_invalid_input(self) -> bool:
        return True

    def rejects_invalid_output(self) -> bool:
        return True

    def refuses_sampling(self) -> bool:
        return True

    def refuses_roots(self) -> bool:
        return True

    def stdout_is_protocol_only(self) -> bool:
        return True

    def enforces_maximum_result(self) -> bool:
        return True

    def operational_status(self) -> Mapping[str, object]:
        return {"checks_run": 17, "failure_count": 0, "reason_codes": []}


def test_conformance_report_accounts_for_prompts_and_current_extensions() -> None:
    # Catches the durable report silently omitting newly required current surfaces.
    report = run_conformance(
        _ReportAdapter(),
        *_catalogs(),
        prompts=(types.Prompt(name="omega.fake.summarize", title="Summarize"),),
        extensions={"io.modelcontextprotocol/tasks": {}},
    )

    assert report == ConformanceReport(
        passed=True,
        checks_run=17,
        passed_checks=17,
        failed_checks=0,
        reason_codes=(),
    )
    assert set(report.__dict__) == {
        "passed",
        "checks_run",
        "passed_checks",
        "failed_checks",
        "reason_codes",
    }


_REPORT_PROMPTS = (types.Prompt(name="omega.fake.summarize", title="Summarize"),)
_REPORT_EXTENSIONS = {"io.modelcontextprotocol/tasks": {}}


class _MutableReportAdapter:
    def __init__(self) -> None:
        source = _ReportAdapter()
        self.identity_value: object = source.identity()
        self.tools_value: object = [tool.model_copy(deep=True) for tool in source.list_tools()]
        self.prompts_value: object = [
            prompt.model_copy(deep=True) for prompt in source.list_prompts()
        ]
        self.resources_value: object = [
            resource.model_copy(deep=True) for resource in source.list_resources()
        ]
        self.templates_value: object = [
            template.model_copy(deep=True) for template in source.list_resource_templates()
        ]
        self.input_rejected: object = True
        self.output_rejected: object = True
        self.sampling_refused: object = True
        self.roots_refused: object = True
        self.stdout_clean: object = True
        self.maximum_enforced: object = True
        self.status_value: object = {
            "checks_run": 17,
            "failure_count": 0,
            "reason_codes": [],
        }

    def identity(self) -> object:
        return self.identity_value

    def list_tools(self) -> object:
        return self.tools_value

    def list_prompts(self) -> object:
        return self.prompts_value

    def list_resources(self) -> object:
        return self.resources_value

    def list_resource_templates(self) -> object:
        return self.templates_value

    def rejects_invalid_input(self) -> object:
        return self.input_rejected

    def rejects_invalid_output(self) -> object:
        return self.output_rejected

    def refuses_sampling(self) -> object:
        return self.sampling_refused

    def refuses_roots(self) -> object:
        return self.roots_refused

    def stdout_is_protocol_only(self) -> object:
        return self.stdout_clean

    def enforces_maximum_result(self) -> object:
        return self.maximum_enforced

    def operational_status(self) -> object:
        return self.status_value


def _run_current_report(adapter: object) -> ConformanceReport:
    return run_conformance(
        adapter,  # type: ignore[arg-type]
        *_catalogs(),
        prompts=_REPORT_PROMPTS,
        extensions=_REPORT_EXTENSIONS,
    )


def test_conformance_reports_every_failure_with_content_free_codes() -> None:
    # Catches a newly added current surface silently escaping durable accounting.
    adapter = _MutableReportAdapter()
    adapter.identity_value = ConformanceIdentity(
        server_id="wrong",
        server_version="wrong",
        protocol_version="wrong",
        capabilities=types.ServerCapabilities(tasks=types.ServerTasksCapability(list={}, call={})),
    )
    adapter.tools_value = ()
    adapter.prompts_value = ()
    adapter.resources_value = (types.Resource(name="extra", uri="omega://fake/extra"),)
    adapter.templates_value = (
        types.ResourceTemplate(name="extra", uriTemplate="omega://fake/{id}"),
    )
    adapter.input_rejected = False
    adapter.output_rejected = False
    adapter.sampling_refused = False
    adapter.roots_refused = False
    adapter.stdout_clean = False
    adapter.maximum_enforced = False
    adapter.status_value = {"access_" + "token": "never include me", "content": "payload"}

    report = _run_current_report(adapter)

    assert report.reason_codes == (
        "server_identity_mismatch",
        "server_version_mismatch",
        "protocol_version_mismatch",
        "tool_catalog_mismatch",
        "resource_catalog_mismatch",
        "resource_template_catalog_mismatch",
        "invalid_input_accepted",
        "invalid_output_accepted",
        "prompt_capability_mismatch",
        "sampling_request_not_refused",
        "roots_request_not_refused",
        "tasks_capability_advertised",
        "stdout_pollution_detected",
        "maximum_result_not_enforced",
        "operational_status_sensitive_key",
        "prompt_catalog_mismatch",
        "extension_advertisement_mismatch",
    )
    assert report.failed_checks == report.checks_run == 17
    assert "never include me" not in repr(report)
    assert "payload" not in repr(report)


def test_each_current_capability_and_catalog_check_is_independent() -> None:
    # Catches prompt/Tasks/extension checks being collapsed into one coarse probe.
    cases: tuple[tuple[str, str], ...] = (
        ("prompt_capability", "prompt_capability_mismatch"),
        ("tasks", "tasks_capability_advertised"),
        ("sampling", "sampling_request_not_refused"),
        ("roots", "roots_request_not_refused"),
        ("prompts", "prompt_catalog_mismatch"),
        ("extensions", "extension_advertisement_mismatch"),
    )
    for change, reason in cases:
        adapter = _MutableReportAdapter()
        identity = adapter.identity_value
        assert type(identity) is ConformanceIdentity
        if change == "prompt_capability":
            identity.capabilities.prompts = None
        elif change == "tasks":
            identity.capabilities.tasks = types.ServerTasksCapability(list={}, call={})
        elif change == "sampling":
            adapter.sampling_refused = False
        elif change == "roots":
            adapter.roots_refused = False
        elif change == "prompts":
            adapter.prompts_value = ()
        else:
            identity.capabilities.extensions = {}
        assert _run_current_report(adapter).reason_codes == (reason,)


def test_adapter_exceptions_become_all_relevant_content_free_reasons() -> None:
    # Catches an adapter failure aborting conformance or leaking exception text.
    sentinel = "Bear" + "er task12-conformance-provider-token-sentinel"

    class ExplodingAdapter:
        def __getattribute__(self, name: str) -> object:
            if name.startswith("__"):
                return object.__getattribute__(self, name)

            def explode() -> object:
                raise RuntimeError(sentinel)

            return explode

    report = _run_current_report(ExplodingAdapter())

    assert report.failed_checks == report.checks_run == 17
    assert sentinel not in repr(report)


def test_malformed_truthy_adapter_values_fail_closed() -> None:
    # Catches truthiness accidentally passing strict current conformance probes.
    class Truthy:
        def __bool__(self) -> bool:
            return True

    adapter = _MutableReportAdapter()
    adapter.identity_value = Truthy()
    adapter.tools_value = Truthy()
    adapter.prompts_value = Truthy()
    adapter.resources_value = Truthy()
    adapter.templates_value = Truthy()
    adapter.input_rejected = Truthy()
    adapter.output_rejected = Truthy()
    adapter.sampling_refused = Truthy()
    adapter.roots_refused = Truthy()
    adapter.stdout_clean = Truthy()
    adapter.maximum_enforced = Truthy()
    adapter.status_value = None

    report = _run_current_report(adapter)

    assert report.failed_checks == report.checks_run == 17


def test_cyclic_status_and_hostile_runtime_fields_fail_closed_without_leak() -> None:
    # Catches recursive status crashes and provider-controlled field execution.
    adapter = _MutableReportAdapter()
    cycle: dict[str, object] = {}
    cycle["nested"] = cycle
    adapter.status_value = cycle
    assert _run_current_report(adapter).reason_codes == ()

    sentinel = "Bear" + "er task12-hostile-provider-token-sentinel"

    class HostileValue:
        def __eq__(self, other: object) -> bool:
            del other
            raise RuntimeError(sentinel)

        def __str__(self) -> str:
            raise RuntimeError(sentinel)

    tools = adapter.tools_value
    resources = adapter.resources_value
    assert type(tools) is list and type(resources) is list
    tools[0].name = HostileValue()  # type: ignore[assignment,union-attr]
    resources[0].uri = HostileValue()  # type: ignore[assignment,union-attr]

    report = _run_current_report(adapter)

    assert report.reason_codes == (
        "tool_catalog_mismatch",
        "resource_catalog_mismatch",
    )
    assert sentinel not in repr(report)

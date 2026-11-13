"""Catalog-bound first-party server for the current public MCP SDK boundary."""

from __future__ import annotations

import inspect
import json
import math
import re
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, TypeAlias, TypeVar, cast

import anyio
import jsonschema
import uvicorn
from mcp import types
from mcp.server import CacheHint, Server, ServerRequestContext
from mcp.server.extension import validate_extension_identifier
from mcp.server.runner import modern_on_request
from mcp.server.stdio import stdio_server
from mcp.server.subscriptions import (
    InMemorySubscriptionBus,
    ListenHandler,
    ServerEvent,
)
from mcp.shared.dispatcher import DispatchContext
from mcp.shared.exceptions import MCPError
from mcp.shared.inbound import InboundLadderRejection, classify_inbound_request
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp.shared.transport_context import TransportContext
from mcp_types import (
    INTERNAL_ERROR,
    UNSUPPORTED_PROTOCOL_VERSION,
    ErrorData,
    JSONRPCError,
)
from pydantic import BaseModel
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_platform.canonical import canonical_bytes
from mcp_platform.package_contracts import (
    MCP_PROTOCOL_VERSION,
    ResourceCatalog,
    ResourceDefinition,
    ResourceTemplateDefinition,
    ToolCatalog,
    ToolDefinition,
)

ToolResult: TypeAlias = Mapping[str, object] | types.InputRequiredResult | types.Result
ToolHandler: TypeAlias = Callable[[Mapping[str, object]], Awaitable[ToolResult]]
ResourceHandler: TypeAlias = Callable[[str], Awaitable[bytes]]
ExtensionHandler: TypeAlias = Callable[[BaseModel], Awaitable[types.Result]]
_ResultT = TypeVar("_ResultT", bound=types.Result)

_DEFAULT_SHUTDOWN_SECONDS = 5.0


class ServerCatalogError(Exception):
    """A stable rejection caused by a signed-catalog boundary violation."""


class ServerSchemaError(Exception):
    """A stable rejection caused by signed schema or byte-limit validation."""


class ServerCleanupError(Exception):
    """A stable failure when server-owned cleanup exceeds its bound."""


_SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"


@dataclass(frozen=True, slots=True)
class ServerHostIdentity:
    """Connection identity CurrentMcpClient requires on discover and list pages."""

    publisher_id: str
    deployment: str
    transport: str
    provider_id: str | None = None
    remote_origin: str | None = None
    authorization_issuer: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.publisher_id) is not str
            or not self.publisher_id
            or self.publisher_id.strip() != self.publisher_id
            or self.deployment not in {"local", "remote"}
            or (
                self.deployment == "local"
                and (
                    self.transport != "stdio"
                    or self.remote_origin is not None
                    or self.authorization_issuer is not None
                )
            )
            or (
                self.deployment == "remote"
                and (
                    self.transport != "streamable_http"
                    or type(self.remote_origin) is not str
                    or not self.remote_origin
                    or type(self.authorization_issuer) is not str
                    or not self.authorization_issuer
                )
            )
            or self.provider_id is not None
            and (type(self.provider_id) is not str or not self.provider_id)
        ):
            raise ServerCatalogError("host identity is invalid")

    def to_meta(self, *, app_id: str, server_version: str) -> dict[str, object]:
        return {
            "appId": app_id,
            "publisherId": self.publisher_id,
            "deployment": self.deployment,
            "providerId": self.provider_id,
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "transport": self.transport,
            "remoteOrigin": self.remote_origin,
            "authorizationIssuer": self.authorization_issuer,
            _SERVER_INFO_META_KEY: {
                "name": app_id,
                "version": server_version,
            },
        }


@dataclass(frozen=True, slots=True)
class PromptRequest:
    """Closed current prompt invocation delivered to a registered handler."""

    name: str
    arguments: Mapping[str, str]
    request_state: str | None
    input_responses: Mapping[str, object]


PromptHandler: TypeAlias = Callable[
    [PromptRequest], Awaitable[types.GetPromptResult | types.InputRequiredResult]
]


class ToolRequest(Mapping[str, object]):
    """Mapping-compatible tool arguments plus current input-required state."""

    __slots__ = ("_arguments", "input_responses", "request_state")

    def __init__(
        self,
        arguments: Mapping[str, object],
        *,
        request_state: str | None,
        input_responses: Mapping[str, object] | None,
    ) -> None:
        self._arguments = MappingProxyType(dict(arguments))
        self.request_state = request_state
        self.input_responses = MappingProxyType(dict(input_responses or {}))

    def __getitem__(self, key: str) -> object:
        return self._arguments[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._arguments)

    def __len__(self) -> int:
        return len(self._arguments)


class FirstPartyMcpServer:
    """Serve one immutable current-protocol first-party capability surface."""

    def __init__(
        self,
        tool_catalog: ToolCatalog,
        resource_catalog: ResourceCatalog,
        *,
        prompts: Sequence[types.Prompt] = (),
        extensions: Mapping[str, Mapping[str, object]] | None = None,
        cache_hints: Mapping[str, CacheHint] | None = None,
        enable_subscriptions: bool = False,
        shutdown_timeout_seconds: float = _DEFAULT_SHUTDOWN_SECONDS,
        host_identity: ServerHostIdentity | None = None,
    ) -> None:
        if (
            tool_catalog.server_id != resource_catalog.server_id
            or tool_catalog.server_version != resource_catalog.server_version
        ):
            raise ServerCatalogError("catalog identity mismatch")
        if any("text/plain" not in tool.allowed_mime_types for tool in tool_catalog.tools):
            raise ServerCatalogError("signed tool missing text/plain result MIME")
        if (
            type(shutdown_timeout_seconds) not in (int, float)
            or not math.isfinite(shutdown_timeout_seconds)
            or shutdown_timeout_seconds <= 0
        ):
            raise ServerCatalogError("shutdown timeout must be finite and positive")

        prompt_snapshot = tuple(prompt.model_copy(deep=True) for prompt in prompts)
        prompt_names = [prompt.name for prompt in prompt_snapshot]
        if len(prompt_names) != len(set(prompt_names)):
            raise ServerCatalogError("duplicate declared prompt")
        extension_snapshot = _extension_snapshot(extensions)

        self.tool_catalog = tool_catalog
        self.resource_catalog = resource_catalog
        self.handlers: dict[str, ToolHandler] = {}
        self.resource_handlers: dict[str, ResourceHandler] = {}
        self.prompt_handlers: dict[str, PromptHandler] = {}
        self._extension_handlers: dict[str, ExtensionHandler] = {}
        self._extension_result_claims: dict[str, tuple[str, type[types.Result]]] = {}
        self._tools = {tool.name: tool for tool in tool_catalog.tools}
        self._resources = {resource.uri: resource for resource in resource_catalog.resources}
        self._templates = {
            template.uri_template: template for template in resource_catalog.resource_templates
        }
        self._prompts = {prompt.name: prompt for prompt in prompt_snapshot}
        self._extensions = extension_snapshot
        self._shutdown_timeout_seconds = float(shutdown_timeout_seconds)
        self._subscription_bus = InMemorySubscriptionBus()
        self._listen_handler = ListenHandler(self._subscription_bus)
        self._subscriptions_enabled = enable_subscriptions
        self._http_app: ASGIApp | None = None
        self._host_identity = host_identity

        self._server: Server[dict[str, Any]] = Server(
            tool_catalog.server_id,
            version=tool_catalog.server_version,
            cache_hints=cache_hints,
            on_list_tools=self._handle_list_tools,
            on_call_tool=self._handle_call_tool,
            on_list_resources=self._handle_list_resources,
            on_list_resource_templates=self._handle_list_resource_templates,
            on_read_resource=self._handle_read_resource,
            on_list_prompts=self._handle_list_prompts if prompt_snapshot else None,
            on_get_prompt=self._handle_get_prompt if prompt_snapshot else None,
            on_subscriptions_listen=(self._listen_handler if enable_subscriptions else None),
        )
        self._server.extensions.update(deepcopy(extension_snapshot))
        if host_identity is not None:
            self._server.add_request_handler(
                "server/discover",
                types.RequestParams,
                self._handle_discover,
            )

    def _result_meta(self) -> dict[str, object] | None:
        if self._host_identity is None:
            return None
        return self._host_identity.to_meta(
            app_id=self.tool_catalog.server_id,
            server_version=self.tool_catalog.server_version,
        )

    def _stamp(self, result: _ResultT) -> _ResultT:
        meta = self._result_meta()
        if meta is None:
            return result
        return result.model_copy(update={"meta": meta})

    async def _handle_discover(
        self,
        _ctx: ServerRequestContext[dict[str, Any]] | None,
        _params: types.RequestParams | None,
    ) -> types.DiscoverResult:
        return types.DiscoverResult(
            supported_versions=[MCP_PROTOCOL_VERSION],
            capabilities=self._server.get_capabilities(protocol_version=MCP_PROTOCOL_VERSION),
            meta=self._result_meta(),
        )

    @property
    def capabilities(self) -> types.ServerCapabilities:
        """Return the exact current discovery capability projection."""
        return self._server.get_capabilities(protocol_version=MCP_PROTOCOL_VERSION)

    @property
    def cache_hints(self) -> dict[str, CacheHint]:
        """Return the immutable-at-construction cache hint projection."""
        return dict(self._server.cache_hints)

    def register_tool(self, name: str, handler: ToolHandler) -> None:
        """Bind one exact async provider handler to a signed tool name."""
        if name not in self._tools:
            raise ServerCatalogError("tool handler is not signed")
        if name in self.handlers:
            raise ServerCatalogError("duplicate tool handler")
        _require_async_unary(handler, "tool")
        self.handlers[name] = handler

    def register_resource(self, uri: str, handler: ResourceHandler) -> None:
        """Bind one exact async provider handler to a signed URI or template."""
        if uri not in self._resources and uri not in self._templates:
            raise ServerCatalogError("resource handler is not signed")
        if uri in self.resource_handlers:
            raise ServerCatalogError("duplicate resource handler")
        _require_async_unary(handler, "resource")
        self.resource_handlers[uri] = handler

    def register_prompt(self, name: str, handler: PromptHandler) -> None:
        """Bind one exact async provider handler to a declared prompt."""
        if name not in self._prompts:
            raise ServerCatalogError("prompt handler is not declared")
        if name in self.prompt_handlers:
            raise ServerCatalogError("duplicate prompt handler")
        _require_async_unary(handler, "prompt")
        self.prompt_handlers[name] = handler

    def register_extension_method(
        self,
        identifier: str,
        method: str,
        params_type: type[BaseModel],
        handler: ExtensionHandler,
    ) -> None:
        """Bind one current custom method beneath an advertised extension."""
        if identifier not in self._extensions:
            raise ServerCatalogError("extension is not advertised")
        if method in self._extension_handlers or self._server.get_request_handler(method):
            raise ServerCatalogError("duplicate extension method")
        _require_async_unary(handler, "extension")

        async def dispatch(
            _ctx: ServerRequestContext[dict[str, Any]],
            params: BaseModel,
        ) -> types.Result:
            active = self._extension_handlers[method]
            if not inspect.iscoroutinefunction(active):
                raise MCPError(INTERNAL_ERROR, "extension handler failed")
            try:
                return await active(params)
            except MCPError:
                raise
            except Exception:
                raise MCPError(INTERNAL_ERROR, "extension handler failed") from None

        self._extension_handlers[method] = handler
        self._server.add_request_handler(method, params_type, dispatch)

    def register_extension_result(
        self,
        identifier: str,
        result_type: str,
        result_model: type[types.Result],
    ) -> None:
        """Bind one non-core claimed result type to one advertised extension."""
        if identifier not in self._extensions:
            raise ServerCatalogError("extension is not advertised")
        if type(result_type) is not str or not result_type:
            raise ServerCatalogError("extension result type is invalid")
        if (
            not isinstance(result_model, type)
            or not issubclass(result_model, types.Result)
            or issubclass(
                result_model,
                (types.CallToolResult, types.InputRequiredResult),
            )
        ):
            raise ServerCatalogError("extension result model is invalid")
        if result_type in self._extension_result_claims:
            raise ServerCatalogError("duplicate extension result type")
        self._extension_result_claims[result_type] = (identifier, result_model)

    def assert_complete(self) -> None:
        """Reject startup unless every declared entry has exactly one handler."""
        missing_tools = set(self._tools) - set(self.handlers)
        missing_resources = (set(self._resources) | set(self._templates)) - set(
            self.resource_handlers
        )
        missing_prompts = set(self._prompts) - set(self.prompt_handlers)
        if missing_tools or missing_resources or missing_prompts:
            raise ServerCatalogError("missing handlers")

    async def list_tools(self) -> list[types.Tool]:
        """Emit signed tools in catalog order without runtime expansion."""
        return [_sdk_tool(tool) for tool in self.tool_catalog.tools]

    async def list_prompts(self) -> list[types.Prompt]:
        """Emit declared prompts in construction order."""
        return [prompt.model_copy(deep=True) for prompt in self._prompts.values()]

    async def list_resources(self) -> list[types.Resource]:
        """Emit signed fixed resources in catalog order."""
        return [
            types.Resource(
                uri=resource.uri,
                name=resource.name,
                mimeType=resource.mime_type,
            )
            for resource in self.resource_catalog.resources
        ]

    async def list_resource_templates(self) -> list[types.ResourceTemplate]:
        """Emit signed resource templates in catalog order."""
        return [
            types.ResourceTemplate(
                uriTemplate=template.uri_template,
                name=template.name,
                mimeType=template.mime_type,
            )
            for template in self.resource_catalog.resource_templates
        ]

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        request_state: str | None = None,
        input_responses: Mapping[str, object] | None = None,
    ) -> ToolResult:
        """Validate, execute, size, and validate one signed tool invocation."""
        definition = self._tools.get(name)
        if definition is None:
            raise ServerCatalogError("tool handler is not signed")
        _validate(arguments, definition.input_schema, "input schema rejection")
        handler = self.handlers.get(name)
        if handler is None:
            raise ServerCatalogError("missing handlers")
        if not inspect.iscoroutinefunction(handler):
            raise ServerCatalogError("tool handler must be async")
        request = ToolRequest(
            arguments,
            request_state=request_state,
            input_responses=input_responses,
        )
        try:
            result = await handler(request)
        except Exception:
            raise ServerSchemaError("tool handler failed") from None
        if isinstance(result, types.InputRequiredResult):
            _validate_result_size(result, definition)
            return result
        if isinstance(result, types.CallToolResult):
            if not isinstance(result.structured_content, Mapping):
                raise ServerSchemaError("output schema rejection")
            structured = dict(result.structured_content)
            _validate(structured, definition.output_schema, "output schema rejection")
            _validate_call_tool_result_size(result, definition)
            return result
        if isinstance(result, types.Result):
            result_type = result.result_type
            claim = self._extension_result_claims.get(result_type)
            if claim is None or type(result) is not claim[1]:
                raise ServerSchemaError("extension result is not registered")
            if claim[0] not in definition.provider_extension_tags:
                raise ServerSchemaError("extension result is not signed")
            _validate_result_size(result, definition)
            return result
        if not isinstance(result, Mapping):
            raise ServerSchemaError("output schema rejection")
        try:
            normalized = dict(result)
        except Exception:
            raise ServerSchemaError("tool handler failed") from None
        _validate(normalized, definition.output_schema, "output schema rejection")
        _validate_tool_result_size(normalized, definition)
        return normalized

    async def get_prompt(
        self,
        name: str,
        arguments: Mapping[str, str],
        *,
        request_state: str | None = None,
        input_responses: Mapping[str, object] | None = None,
    ) -> types.GetPromptResult | types.InputRequiredResult:
        """Invoke one declared prompt through a closed current request."""
        if name not in self._prompts:
            raise ServerCatalogError("prompt handler is not declared")
        handler = self.prompt_handlers.get(name)
        if handler is None:
            raise ServerCatalogError("missing handlers")
        if not inspect.iscoroutinefunction(handler):
            raise ServerCatalogError("prompt handler must be async")
        request = PromptRequest(
            name=name,
            arguments=MappingProxyType(dict(arguments)),
            request_state=request_state,
            input_responses=MappingProxyType(dict(input_responses or {})),
        )
        try:
            result = await handler(request)
        except Exception:
            raise ServerSchemaError("prompt handler failed") from None
        if not isinstance(result, types.GetPromptResult | types.InputRequiredResult):
            raise ServerSchemaError("prompt output rejection")
        return result

    async def read_resource(self, uri: str) -> list[types.TextResourceContents]:
        """Read a signed URI and enforce its declared media type and byte limit."""
        registration, definition = self._resource_registration(uri)
        handler = self.resource_handlers.get(registration)
        if handler is None:
            raise ServerCatalogError("missing handlers")
        if not inspect.iscoroutinefunction(handler):
            raise ServerCatalogError("resource handler must be async")
        try:
            content = await handler(uri)
        except Exception:
            raise ServerSchemaError("resource handler failed") from None
        if type(content) is not bytes:
            raise ServerSchemaError("resource output must be bytes")
        if len(content) > definition.max_bytes:
            raise ServerSchemaError("resource maximum exceeded")
        try:
            text = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise ServerSchemaError("resource output must be UTF-8") from None
        return [
            types.TextResourceContents(
                uri=uri,
                mimeType=definition.mime_type,
                text=text,
            )
        ]

    async def publish_event(self, event: ServerEvent) -> None:
        """Publish one typed event to bounded current listen streams."""
        if not self._subscriptions_enabled:
            raise ServerCatalogError("subscriptions are not enabled")
        await self._subscription_bus.publish(event)

    async def run_stdio(self) -> None:
        """Run current-only stdio with bounded cleanup and protocol-only stdout."""
        self.assert_complete()
        active_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        with anyio.CancelScope(shield=True) as cleanup_shield:
            with anyio.CancelScope(deadline=math.inf) as cleanup_deadline:
                stack = AsyncExitStack()
                try:
                    read_stream, write_stream = await stack.enter_async_context(stdio_server())
                    lifespan_state = await stack.enter_async_context(
                        self._server.lifespan(self._server)
                    )
                    dispatcher: JSONRPCDispatcher[TransportContext] = JSONRPCDispatcher(
                        read_stream,
                        write_stream,
                    )
                    cleanup_shield.shield = False
                    await dispatcher.run(
                        _current_request_handler(self._server, lifespan_state),
                        _ignore_current_notification,
                    )
                except BaseException as error:
                    active_error = error
                finally:
                    cleanup_shield.shield = True
                    cleanup_error = await _bounded_stack_close(
                        stack,
                        cleanup_deadline,
                        self._shutdown_timeout_seconds,
                    )
        if active_error is not None:
            raise active_error
        if cleanup_error is not None:
            raise cleanup_error

    def streamable_http_app(self, *, host: str = "127.0.0.1") -> ASGIApp:
        """Return one current-only stateless public SDK Streamable HTTP app."""
        self.assert_complete()
        if self._http_app is None:
            sdk_app = self._server.streamable_http_app(
                json_response=True,
                stateless_http=True,
                host=host,
            )
            self._http_app = _CurrentProtocolHttpGate(sdk_app)
        return self._http_app

    async def run_streamable_http(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
    ) -> None:
        """Serve the current-only app on one loopback-capable HTTP listener."""
        config = uvicorn.Config(
            self.streamable_http_app(host=host),
            host=host,
            port=port,
            access_log=False,
            log_level="critical",
            timeout_graceful_shutdown=self._shutdown_timeout_seconds,
        )
        if not config.loaded:
            config.load()
        owner = uvicorn.Server(config)
        owner.lifespan = config.lifespan_class(config)
        active_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        with anyio.CancelScope(shield=True) as cleanup_shield:
            with anyio.CancelScope(deadline=math.inf) as cleanup_deadline:
                try:
                    with owner.capture_signals():
                        try:
                            cleanup_shield.shield = False
                            await owner.startup()
                            if not getattr(owner, "should_exit", False):
                                await owner.main_loop()
                        except BaseException as error:
                            active_error = error
                        finally:
                            cleanup_shield.shield = True
                            cleanup_error = await _bounded_http_shutdown(
                                owner,
                                cleanup_deadline,
                                self._shutdown_timeout_seconds,
                            )
                except BaseException as error:
                    if active_error is None:
                        active_error = error
        if active_error is not None:
            raise active_error
        if cleanup_error is not None:
            raise cleanup_error

    def _resource_registration(
        self,
        uri: str,
    ) -> tuple[str, ResourceDefinition | ResourceTemplateDefinition]:
        resource = self._resources.get(uri)
        if resource is not None:
            return uri, resource
        for template in self.resource_catalog.resource_templates:
            if _matches_template(template.uri_template, uri):
                return template.uri_template, template
        raise ServerCatalogError("resource handler is not signed")

    async def _handle_list_tools(
        self,
        _ctx: ServerRequestContext[dict[str, Any]],
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return self._stamp(types.ListToolsResult(tools=await self.list_tools()))

    async def _handle_call_tool(
        self,
        _ctx: ServerRequestContext[dict[str, Any]],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult | types.InputRequiredResult | types.Result:
        try:
            result = await self.call_tool(
                params.name,
                params.arguments or {},
                request_state=params.request_state,
                input_responses=cast(Mapping[str, object] | None, params.input_responses),
            )
        except ServerCatalogError as error:
            return self._stamp(_tool_error(str(error)))
        except ServerSchemaError as error:
            return self._stamp(_tool_error(str(error)))
        if isinstance(result, types.Result):
            return self._stamp(result)
        return self._stamp(
            types.CallToolResult(
                content=[types.TextContent(text=json.dumps(result, indent=2))],
                structuredContent=dict(result),
                isError=False,
            )
        )

    async def _handle_list_prompts(
        self,
        _ctx: ServerRequestContext[dict[str, Any]],
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListPromptsResult:
        return self._stamp(types.ListPromptsResult(prompts=await self.list_prompts()))

    async def _handle_get_prompt(
        self,
        _ctx: ServerRequestContext[dict[str, Any]],
        params: types.GetPromptRequestParams,
    ) -> types.GetPromptResult | types.InputRequiredResult:
        try:
            return self._stamp(
                await self.get_prompt(
                    params.name,
                    params.arguments or {},
                    request_state=params.request_state,
                    input_responses=cast(Mapping[str, object] | None, params.input_responses),
                )
            )
        except ServerCatalogError as error:
            raise MCPError(INTERNAL_ERROR, str(error)) from None
        except ServerSchemaError as error:
            raise MCPError(INTERNAL_ERROR, str(error)) from None

    async def _handle_list_resources(
        self,
        _ctx: ServerRequestContext[dict[str, Any]],
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListResourcesResult:
        return self._stamp(types.ListResourcesResult(resources=await self.list_resources()))

    async def _handle_list_resource_templates(
        self,
        _ctx: ServerRequestContext[dict[str, Any]],
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListResourceTemplatesResult:
        return self._stamp(
            types.ListResourceTemplatesResult(
                resourceTemplates=await self.list_resource_templates()
            )
        )

    async def _handle_read_resource(
        self,
        _ctx: ServerRequestContext[dict[str, Any]],
        params: types.ReadResourceRequestParams,
    ) -> types.ReadResourceResult:
        try:
            return self._stamp(
                types.ReadResourceResult(contents=await self.read_resource(params.uri))
            )
        except ServerCatalogError as error:
            raise MCPError(INTERNAL_ERROR, str(error)) from None
        except ServerSchemaError as error:
            raise MCPError(INTERNAL_ERROR, str(error)) from None


def _extension_snapshot(
    extensions: Mapping[str, Mapping[str, object]] | None,
) -> dict[str, dict[str, object]]:
    snapshot: dict[str, dict[str, object]] = {}
    for identifier, settings in (extensions or {}).items():
        validate_extension_identifier(identifier, owner="FirstPartyMcpServer")
        if not isinstance(settings, Mapping):
            raise ServerCatalogError("extension settings must be an object")
        try:
            copied = deepcopy(dict(settings))
            canonical_bytes(copied)
        except Exception:
            raise ServerCatalogError("extension settings are invalid") from None
        snapshot[identifier] = copied
    return snapshot


def _sdk_tool(definition: ToolDefinition) -> types.Tool:
    document = definition.to_dict()
    return types.Tool(
        name=definition.name,
        title=definition.title,
        inputSchema=cast(dict[str, Any], document["input_schema"]),
        outputSchema=cast(dict[str, Any], document["output_schema"]),
    )


def _require_async_unary(handler: object, label: str) -> None:
    if not inspect.iscoroutinefunction(handler):
        raise ServerCatalogError(f"{label} handler must be async")
    try:
        parameters = tuple(inspect.signature(handler).parameters.values())
    except (TypeError, ValueError):
        raise ServerCatalogError(f"{label} handler has invalid signature") from None
    if (
        len(parameters) != 1
        or parameters[0].kind
        not in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        or parameters[0].default is not inspect.Parameter.empty
    ):
        raise ServerCatalogError(f"{label} handler has invalid signature")


def _validate(value: object, schema: Mapping[str, Any], reason: str) -> None:
    try:
        jsonschema.Draft202012Validator(schema).validate(value)
    except Exception:
        raise ServerSchemaError(reason) from None


def _validate_tool_result_size(
    result: Mapping[str, object],
    definition: ToolDefinition,
) -> None:
    try:
        size = len(canonical_bytes(result))
        text = types.TextContent(text=json.dumps(result, indent=2)).model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
        size += len(canonical_bytes(text))
    except (TypeError, ValueError):
        raise ServerSchemaError("output schema rejection") from None
    if size > definition.max_result_bytes:
        raise ServerSchemaError("output maximum exceeded")


def _validate_call_tool_result_size(
    result: types.CallToolResult,
    definition: ToolDefinition,
) -> None:
    try:
        size = len(canonical_bytes(result.structured_content))
        size += sum(
            len(canonical_bytes(item.model_dump(mode="json", by_alias=True, exclude_none=True)))
            for item in result.content
        )
    except (TypeError, ValueError):
        raise ServerSchemaError("output schema rejection") from None
    if size > definition.max_result_bytes:
        raise ServerSchemaError("output maximum exceeded")


def _validate_result_size(result: types.Result, definition: ToolDefinition) -> None:
    try:
        size = len(
            canonical_bytes(result.model_dump(mode="json", by_alias=True, exclude_none=True))
        )
    except (TypeError, ValueError):
        raise ServerSchemaError("extension output rejection") from None
    if size > definition.max_result_bytes:
        raise ServerSchemaError("output maximum exceeded")


def _matches_template(template: str, uri: str) -> bool:
    pieces: list[str] = []
    position = 0
    for match in re.finditer(r"\{[A-Za-z0-9_]+\}", template):
        pieces.append(re.escape(template[position : match.start()]))
        pieces.append(r"[^/?#]+")
        position = match.end()
    pieces.append(re.escape(template[position:]))
    return re.fullmatch("".join(pieces), uri) is not None


def _tool_error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(text=message)],
        isError=True,
    )


def _requested_protocol(params: Mapping[str, Any] | None) -> str:
    requested = (params or {}).get("protocolVersion")
    return requested if type(requested) is str else "missing"


def _unsupported_protocol(requested: str) -> MCPError:
    return MCPError(
        UNSUPPORTED_PROTOCOL_VERSION,
        "Unsupported protocol version",
        {"supported": [MCP_PROTOCOL_VERSION], "requested": requested},
    )


def _current_request_handler(
    server: Server[dict[str, Any]],
    lifespan_state: dict[str, Any],
) -> Callable[
    [DispatchContext[TransportContext], str, Mapping[str, Any] | None],
    Awaitable[dict[str, Any]],
]:
    current = modern_on_request(server, lifespan_state)

    async def handle(
        context: DispatchContext[TransportContext],
        method: str,
        params: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if method == "initialize":
            raise _unsupported_protocol(_requested_protocol(params))
        verdict = classify_inbound_request({"method": method, "params": params})
        if isinstance(verdict, InboundLadderRejection):
            raise MCPError(verdict.code, verdict.message, verdict.data)
        return await current(context, method, params)

    return handle


async def _ignore_current_notification(
    _context: DispatchContext[TransportContext],
    _method: str,
    _params: Mapping[str, Any] | None,
) -> None:
    return


async def _bounded_stack_close(
    stack: AsyncExitStack,
    deadline_scope: anyio.CancelScope,
    timeout_seconds: float,
) -> ServerCleanupError | BaseException | None:
    cleanup_error: BaseException | None = None
    deadline_scope.deadline = anyio.current_time() + timeout_seconds
    try:
        await stack.aclose()
    except BaseException as error:
        cleanup_error = error
    if deadline_scope.cancel_called:
        return ServerCleanupError("server cleanup timed out")
    return _stable_cleanup_error(cleanup_error)


async def _bounded_http_shutdown(
    owner: uvicorn.Server,
    deadline_scope: anyio.CancelScope,
    timeout_seconds: float,
) -> ServerCleanupError | BaseException | None:
    cleanup_error: BaseException | None = None
    owner.should_exit = True
    deadline_scope.deadline = anyio.current_time() + timeout_seconds
    try:
        await owner.shutdown()
    except BaseException as error:
        cleanup_error = error
    if deadline_scope.cancel_called:
        owner.force_exit = True
        return ServerCleanupError("server cleanup timed out")
    return _stable_cleanup_error(cleanup_error)


def _stable_cleanup_error(error: BaseException | None) -> BaseException | None:
    if isinstance(error, Exception):
        return ServerCleanupError("server cleanup failed")
    return error


class _CurrentProtocolHttpGate:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        protocol_headers = [
            value.decode("latin-1")
            for key, value in scope.get("headers", ())
            if key.decode("latin-1").casefold() == "mcp-protocol-version"
        ]
        if protocol_headers == [MCP_PROTOCOL_VERSION]:
            await self._app(scope, receive, send)
            return
        requested = protocol_headers[0] if len(protocol_headers) == 1 else "ambiguous"
        error = JSONRPCError(
            jsonrpc="2.0",
            id=None,
            error=ErrorData(
                code=UNSUPPORTED_PROTOCOL_VERSION,
                message="Unsupported protocol version",
                data={"supported": [MCP_PROTOCOL_VERSION], "requested": requested},
            ),
        )
        body = error.model_dump_json(by_alias=True, exclude_none=False).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


__all__ = [
    "ExtensionHandler",
    "FirstPartyMcpServer",
    "PromptHandler",
    "PromptRequest",
    "ResourceHandler",
    "ServerCatalogError",
    "ServerCleanupError",
    "ServerSchemaError",
    "ToolHandler",
    "ToolRequest",
]

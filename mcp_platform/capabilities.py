"""Pure current-protocol capability projection and live reconciliation."""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

from mcp import types
from pydantic import BaseModel

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.client_contracts import (
    DiscoverySource,
    McpClientDeployment,
    McpClientError,
    McpClientErrorReason,
    McpClientLimits,
    McpConnectionDescriptor,
    McpDiscoveryReceipt,
    McpTransport,
)
from mcp_platform.extensions import MCP_APPS_EXTENSION_IDENTIFIER, validate_mcp_apps_metadata
from plugin_platform.contracts import (
    AppDeployment,
    AppRequirement,
    CapabilityKind,
    McpTransport as PluginMcpTransport,
)
from plugin_platform.dependencies import AppCapabilitySnapshot, NamedCapability

_LOGGER = logging.getLogger("omega.mcp.capabilities")

_EXTERNAL_DESCRIPTION_PREFIX = "[MCP server description] "
_CATALOG_DESCRIPTION = "[Signed catalog declaration] No external description was observed."
_NO_EXTERNAL_DESCRIPTION = "No external description was observed."
_MISSING_EXTERNAL_DESCRIPTION = "No description was provided."
_IDENTITY_FIELDS = (
    "appId",
    "publisherId",
    "deployment",
    "providerId",
    "protocolVersion",
    "transport",
    "remoteOrigin",
    "authorizationIssuer",
)
_HEX = frozenset("0123456789abcdefABCDEF")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_TASKS_EXTENSION_IDENTIFIER = "io.modelcontextprotocol/tasks"
_MAX_MCP_APPS_METADATA_BYTES = 65_536
@dataclass(frozen=True, slots=True)
class CapabilityProjection:
    """One immutable plugin snapshot paired with its discovery evidence."""

    snapshot: AppCapabilitySnapshot
    receipt: McpDiscoveryReceipt

    def __post_init__(self) -> None:
        try:
            snapshot = _rebuild_snapshot(self.snapshot)
            receipt = _rebuild_receipt(self.receipt)
            groups = {
                "tools": snapshot.tools,
                "prompts": snapshot.prompts,
                "resources": snapshot.resources,
                "extensions": snapshot.extensions,
            }
            revision = _capability_revision(groups)
            if (
                snapshot.app_id != receipt.app_id
                or snapshot.publisher_id != receipt.publisher_id
                or snapshot.protocol_version != receipt.protocol_version
                or snapshot.deployment.value != receipt.deployment.value
                or receipt.transport.value
                not in {transport.value for transport in snapshot.transports}
                or snapshot.capability_revision != revision
                or receipt.capability_revision != revision
                or _snapshot_authority(groups) != _receipt_authority(receipt)
                or snapshot.installed is not True
                or snapshot.trusted is not True
                or snapshot.enabled is not False
            ):
                raise ValueError("capability projection is invalid")
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            if type(exc) is ValueError and str(exc) == "capability projection is invalid":
                raise
            raise ValueError("capability projection is invalid") from exc
        object.__setattr__(self, "snapshot", snapshot)
        object.__setattr__(self, "receipt", receipt)


def _rebuild_named_capabilities(value: Any) -> tuple[NamedCapability, ...]:
    if type(value) is not tuple or any(type(item) is not NamedCapability for item in value):
        raise ValueError("capability projection is invalid")
    return tuple(
        NamedCapability(
            name=item.name,
            schema_sha256=item.schema_sha256,
            version=item.version,
            description=item.description,
            kind=item.kind,
        )
        for item in value
    )


def _rebuild_snapshot(value: Any) -> AppCapabilitySnapshot:
    if type(value) is not AppCapabilitySnapshot:
        raise ValueError("capability projection is invalid")
    return AppCapabilitySnapshot(
        app_id=value.app_id,
        publisher_id=value.publisher_id,
        deployment=value.deployment,
        provider_id=value.provider_id,
        protocol_version=value.protocol_version,
        transports=value.transports,
        remote_origin=value.remote_origin,
        authorization_issuer=value.authorization_issuer,
        authentication=value.authentication,
        tools=_rebuild_named_capabilities(value.tools),
        prompts=_rebuild_named_capabilities(value.prompts),
        resources=_rebuild_named_capabilities(value.resources),
        extensions=_rebuild_named_capabilities(value.extensions),
        capability_revision=value.capability_revision,
        installed=value.installed,
        trusted=value.trusted,
        enabled=value.enabled,
    )


def _rebuild_receipt(value: Any) -> McpDiscoveryReceipt:
    if type(value) is not McpDiscoveryReceipt:
        raise ValueError("capability projection is invalid")
    return McpDiscoveryReceipt(
        app_id=value.app_id,
        publisher_id=value.publisher_id,
        deployment=value.deployment,
        transport=value.transport,
        protocol_version=value.protocol_version,
        source=value.source,
        server_id=value.server_id,
        server_version=value.server_version,
        tools=value.tools,
        prompts=value.prompts,
        resources=value.resources,
        resource_templates=value.resource_templates,
        extensions=value.extensions,
        cache_metadata=value.cache_metadata,
        capability_revision=value.capability_revision,
    )


def _snapshot_authority(
    groups: Mapping[str, tuple[NamedCapability, ...]],
) -> dict[str, dict[str, str | None]]:
    return {
        field: {
            item.name: item.version if field == "extensions" else item.schema_sha256
            for item in groups[field]
        }
        for field in ("tools", "prompts", "resources", "extensions")
    }


def _receipt_schema(entry: Any, *, tool: bool) -> str | None:
    if not isinstance(entry, Mapping):
        raise ValueError("capability projection is invalid")
    if tool:
        input_digest = entry.get("input_schema_sha256")
        catalog_digest = entry.get("schema_sha256")
        if input_digest is not None and catalog_digest is not None:
            raise ValueError("capability projection is invalid")
        return input_digest if input_digest is not None else catalog_digest
    return entry.get("schema_sha256")


def _receipt_authority(
    receipt: McpDiscoveryReceipt,
) -> dict[str, dict[str, str | None]]:
    resource_names = set(receipt.resources) | set(receipt.resource_templates)
    if set(receipt.resources) & set(receipt.resource_templates):
        raise ValueError("capability projection is invalid")
    return {
        "tools": {name: _receipt_schema(entry, tool=True) for name, entry in receipt.tools.items()},
        "prompts": {
            name: _receipt_schema(entry, tool=False) for name, entry in receipt.prompts.items()
        },
        "resources": {name: None for name in resource_names},
        "extensions": {
            name: (
                None
                if name == _TASKS_EXTENSION_IDENTIFIER and isinstance(entry, Mapping) and not entry
                else _receipt_extension_version(entry)
            )
            for name, entry in receipt.extensions.items()
        },
    }


def _receipt_extension_version(entry: Any) -> str | None:
    if not isinstance(entry, Mapping):
        raise ValueError("capability projection is invalid")
    return entry.get("version")


@dataclass(slots=True)
class _CanonicalBudget:
    limit: int
    size: int = 0

    def _add(self, size: int) -> None:
        self.size += size
        if self.size > self.limit:
            _fail(McpClientErrorReason.DOCUMENT_LIMIT_EXCEEDED)

    def _ensure(self, minimum_size: int) -> None:
        if self.size + minimum_size > self.limit:
            _fail(McpClientErrorReason.DOCUMENT_LIMIT_EXCEEDED)

    def _string(self, value: str) -> None:
        self._add(1)
        for character in value:
            codepoint = ord(character)
            if character in {'"', "\\"} or character in "\b\t\n\f\r":
                self._add(2)
            elif codepoint < 0x20:
                self._add(6)
            else:
                try:
                    self._add(len(character.encode("utf-8")))
                except UnicodeEncodeError:
                    _fail(McpClientErrorReason.RESULT_INVALID)
        self._add(1)

    def _mapping(self, value: Mapping[Any, Any]) -> None:
        entry_count = len(value)
        self._ensure(2 + max(0, entry_count - 1) + 4 * entry_count)
        keys = list(value)
        if len(keys) != entry_count or any(type(key) is not str for key in keys):
            _fail(McpClientErrorReason.RESULT_INVALID)
        self._add(1)
        for index, key in enumerate(sorted(keys)):
            if index:
                self._add(1)
            self._string(key)
            self._add(1)
            self.walk(value[key])
        self._add(1)

    def _sequence(self, value: list[Any] | tuple[Any, ...]) -> None:
        item_count = len(value)
        self._ensure(2 + max(0, item_count - 1) + item_count)
        self._add(1)
        for index, item in enumerate(value):
            if index:
                self._add(1)
            self.walk(item)
        self._add(1)

    def _model(self, value: BaseModel) -> None:
        value_type = type(value)
        entries: list[tuple[str, str, bool]] = []
        for field_name, model_field in value_type.model_fields.items():
            alias = model_field.serialization_alias or model_field.alias or field_name
            if type(alias) is not str:
                _fail(McpClientErrorReason.RESULT_INVALID)
            entries.append((alias, field_name, False))
        raw_extras = object.__getattribute__(value, "__pydantic_extra__")
        if raw_extras is not None and (type(raw_extras) is not dict or raw_extras):
            _fail(McpClientErrorReason.RESULT_INVALID)
        entries.sort(key=lambda entry: entry[0])
        included = 0
        self._add(1)
        for alias, field_name, _ in entries:
            item = getattr(value, field_name)
            if item is None:
                continue
            if included:
                self._add(1)
            included += 1
            self._string(alias)
            self._add(1)
            self.walk(item)
        self._add(1)

    def walk(self, value: Any) -> None:
        value_type = type(value)
        if value is None:
            self._add(4)
        elif value_type is bool:
            self._add(4 if value else 5)
        elif value_type is str:
            self._string(value)
        elif value_type in {int, float}:
            try:
                self._add(len(canonical_bytes(value)))
            except (TypeError, ValueError):
                _fail(McpClientErrorReason.RESULT_INVALID)
        elif value_type is list or value_type is tuple:
            self._sequence(value)
        elif issubclass(value_type, Mapping):
            self._mapping(value)
        elif issubclass(value_type, BaseModel):
            self._model(value)
        elif issubclass(value_type, Enum):
            self.walk(value.value)
        else:
            _fail(McpClientErrorReason.RESULT_INVALID)


def _preflight_document(budget: _CanonicalBudget, value: Any) -> None:
    try:
        budget.walk(value)
    except McpClientError:
        raise
    except Exception:
        _fail(McpClientErrorReason.RESULT_INVALID)


def _freeze_json(value: Any) -> Any:
    if value is None or type(value) in {str, bool, int, float}:
        return value
    if type(value) is list:
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            _fail(McpClientErrorReason.RESULT_INVALID)
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    _fail(McpClientErrorReason.RESULT_INVALID)


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


def _exact_optional_string(value: Any) -> str | None:
    if value is not None and type(value) is not str:
        _fail(McpClientErrorReason.RESULT_INVALID)
    return value


@dataclass(frozen=True, slots=True)
class _ObservedTool:
    name: str
    description: str | None
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any] | None
    apps_ui: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class _ObservedPromptArgument:
    name: str
    description: str | None
    required: bool | None


@dataclass(frozen=True, slots=True)
class _ObservedPrompt:
    name: str
    description: str | None
    arguments: tuple[_ObservedPromptArgument, ...]


@dataclass(frozen=True, slots=True)
class _ObservedResource:
    name: str
    uri: str
    description: str | None
    mime_type: str | None
    apps_ui: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class _ObservedResourceTemplate:
    name: str
    uri_template: str
    description: str | None
    mime_type: str | None
    apps_ui: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class _ObservedPage:
    surface: str
    meta: Mapping[str, Any]
    ttl_ms: int
    cache_scope: str
    next_cursor: str | None
    result_type: str
    items: tuple[Any, ...]
    document: Mapping[str, Any]

    @property
    def tools(self) -> tuple[Any, ...]:
        return self.items

    @property
    def prompts(self) -> tuple[Any, ...]:
        return self.items

    @property
    def resources(self) -> tuple[Any, ...]:
        return self.items

    @property
    def resource_templates(self) -> tuple[Any, ...]:
        return self.items


def _normalize_percent_encoding(value: str) -> str:
    normalized: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character != "%":
            normalized.append(character)
            index += 1
            continue
        if index + 2 >= len(value) or value[index + 1] not in _HEX or value[index + 2] not in _HEX:
            _fail(McpClientErrorReason.RESULT_INVALID)
        encoded = value[index + 1 : index + 3]
        octet = int(encoded, 16)
        if octet < 0x20 or octet == 0x7F:
            _fail(McpClientErrorReason.RESULT_INVALID)
        decoded = chr(octet)
        normalized.append(decoded if decoded in _UNRESERVED else f"%{encoded.upper()}")
        index += 3
    return "".join(normalized)


def _valid_uri_scheme(value: str) -> bool:
    return (
        bool(value)
        and value[0].isalpha()
        and value[0].isascii()
        and all(
            character.isascii() and (character.isalnum() or character in "+.-")
            for character in value[1:]
        )
    )


def _remove_dot_segments(path: str) -> str:
    absolute = path.startswith("/")
    trailing = path.endswith(("/", "/.", "/.."))
    output: list[str] = []
    for segment in path.split("/"):
        if segment == ".":
            continue
        if segment == "..":
            if output and output[-1] not in {"", ".."}:
                output.pop()
            elif not absolute:
                output.append(segment)
            continue
        output.append(segment)
    normalized = "/".join(output)
    if absolute and not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if trailing and normalized and not normalized.endswith("/"):
        normalized = f"{normalized}/"
    return normalized or ("/" if absolute else "")


def _canonical_authority(authority: str, *, scheme: str) -> str:
    if not authority or "@" in authority:
        _fail(McpClientErrorReason.RESULT_INVALID)
    if authority.startswith("["):
        bracket = authority.find("]")
        if bracket < 0:
            _fail(McpClientErrorReason.RESULT_INVALID)
        host = _normalize_percent_encoding(authority[: bracket + 1]).lower()
        remainder = authority[bracket + 1 :]
        if remainder and not remainder.startswith(":"):
            _fail(McpClientErrorReason.RESULT_INVALID)
        port = remainder[1:] if remainder else None
    else:
        if authority.count(":") > 1:
            _fail(McpClientErrorReason.RESULT_INVALID)
        host, separator, port_value = authority.partition(":")
        host = _normalize_percent_encoding(host).lower()
        port = port_value if separator else None
    if not host or any(character.isspace() for character in host):
        _fail(McpClientErrorReason.RESULT_INVALID)
    if port is not None:
        if not port.isascii() or not port.isdigit() or not 0 < int(port) <= 65_535:
            _fail(McpClientErrorReason.RESULT_INVALID)
        if (scheme, int(port)) in {("http", 80), ("https", 443)}:
            port = None
    return host if port is None else f"{host}:{port}"


def _canonical_endpoint(value: Any) -> str:
    if type(value) is not str or not value:
        _fail(McpClientErrorReason.RESULT_INVALID)
    if (
        "#" in value
        or "\\" in value
        or any(
            character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
            for character in value
        )
    ):
        _fail(McpClientErrorReason.RESULT_INVALID)
    scheme_value, separator, remainder = value.partition(":")
    if not separator or not _valid_uri_scheme(scheme_value):
        _fail(McpClientErrorReason.RESULT_INVALID)
    scheme = scheme_value.lower()
    authority = None
    if remainder.startswith("//"):
        hierarchical = remainder[2:]
        boundary = len(hierarchical)
        for delimiter in ("/", "?"):
            position = hierarchical.find(delimiter)
            if position >= 0:
                boundary = min(boundary, position)
        authority = _canonical_authority(hierarchical[:boundary], scheme=scheme)
        remainder = hierarchical[boundary:]
    path, query_separator, query = remainder.partition("?")
    path = _remove_dot_segments(_normalize_percent_encoding(path))
    query = _normalize_percent_encoding(query) if query_separator else ""
    endpoint = f"{scheme}:"
    if authority is not None:
        endpoint = f"{endpoint}//{authority}"
    endpoint = f"{endpoint}{path}"
    if query_separator:
        endpoint = f"{endpoint}?{query}"
    return endpoint


def canonical_resource_uri(value: Any) -> str:
    """Return the same canonical resource URI form used by live reconciliation."""

    return _canonical_endpoint(value)


def _snapshot_apps_ui(item: Any) -> Mapping[str, Any] | None:
    meta = getattr(item, "meta", None)
    if meta is None:
        return None
    if not isinstance(meta, Mapping):
        _fail(McpClientErrorReason.RESULT_INVALID)
    ui = meta.get("ui")
    if ui is None:
        return None
    if not isinstance(ui, Mapping):
        _fail(McpClientErrorReason.RESULT_INVALID)
    frozen = _freeze_json(ui)
    if not isinstance(frozen, Mapping):
        _fail(McpClientErrorReason.RESULT_INVALID)
    return frozen


def _snapshot_item(item: Any, *, item_type: type[Any]) -> Any:
    if type(item) is not item_type:
        _fail(McpClientErrorReason.RESULT_INVALID)
    if item_type is types.Tool:
        if type(item.name) is not str or type(item.input_schema) is not dict:
            _fail(McpClientErrorReason.RESULT_INVALID)
        if item.output_schema is not None and type(item.output_schema) is not dict:
            _fail(McpClientErrorReason.RESULT_INVALID)
        return _ObservedTool(
            name=item.name,
            description=_exact_optional_string(item.description),
            input_schema=_freeze_json(item.input_schema),
            output_schema=(
                None if item.output_schema is None else _freeze_json(item.output_schema)
            ),
            apps_ui=_snapshot_apps_ui(item),
        )
    if item_type is types.Prompt:
        if (
            type(item.name) is not str
            or item.arguments is not None
            and type(item.arguments) is not list
        ):
            _fail(McpClientErrorReason.RESULT_INVALID)
        arguments: list[_ObservedPromptArgument] = []
        for argument in item.arguments or []:
            if type(argument) is not types.PromptArgument or type(argument.name) is not str:
                _fail(McpClientErrorReason.RESULT_INVALID)
            if argument.required is not None and type(argument.required) is not bool:
                _fail(McpClientErrorReason.RESULT_INVALID)
            arguments.append(
                _ObservedPromptArgument(
                    name=argument.name,
                    description=_exact_optional_string(argument.description),
                    required=argument.required,
                )
            )
        return _ObservedPrompt(
            name=item.name,
            description=_exact_optional_string(item.description),
            arguments=tuple(arguments),
        )
    if item_type is types.Resource:
        if type(item.name) is not str or type(item.uri) is not str:
            _fail(McpClientErrorReason.RESULT_INVALID)
        return _ObservedResource(
            name=item.name,
            uri=_canonical_endpoint(item.uri),
            description=_exact_optional_string(item.description),
            mime_type=_exact_optional_string(item.mime_type),
            apps_ui=_snapshot_apps_ui(item),
        )
    if item_type is types.ResourceTemplate:
        if type(item.name) is not str or type(item.uri_template) is not str:
            _fail(McpClientErrorReason.RESULT_INVALID)
        return _ObservedResourceTemplate(
            name=item.name,
            uri_template=_canonical_endpoint(item.uri_template),
            description=_exact_optional_string(item.description),
            mime_type=_exact_optional_string(item.mime_type),
            apps_ui=_snapshot_apps_ui(item),
        )
    _fail(McpClientErrorReason.RESULT_INVALID)


def _snapshot_pages(
    pages: Any,
    *,
    limits: McpClientLimits,
    page_type: type[Any],
    item_type: type[Any],
    item_field: str,
    surface: str,
    item_count: int,
    budget: _CanonicalBudget,
) -> tuple[tuple[_ObservedPage, ...], int]:
    if type(pages) is not tuple or not pages or len(pages) > limits.max_list_pages:
        _fail(McpClientErrorReason.PAGINATION_LIMIT_EXCEEDED)
    snapshot: list[_ObservedPage] = []
    if type(pages[0]) is _ObservedPage:
        for page in pages:
            if type(page) is not _ObservedPage or page.surface != surface:
                _fail(McpClientErrorReason.RESULT_INVALID)
            item_count += len(page.items)
            if item_count > limits.max_list_items:
                _fail(McpClientErrorReason.RESULT_LIMIT_EXCEEDED)
            _preflight_document(budget, page.document)
            snapshot.append(page)
        return tuple(snapshot), item_count
    for page in pages:
        if type(page) is not page_type:
            _fail(McpClientErrorReason.RESULT_INVALID)
        raw_items = getattr(page, item_field, None)
        if type(raw_items) is not list:
            _fail(McpClientErrorReason.RESULT_INVALID)
        item_count += len(raw_items)
        if item_count > limits.max_list_items:
            _fail(McpClientErrorReason.RESULT_LIMIT_EXCEEDED)
        _preflight_document(budget, page)
        items = tuple(_snapshot_item(item, item_type=item_type) for item in raw_items)
        if type(page.ttl_ms) is not int or type(page.cache_scope) is not str:
            _fail(McpClientErrorReason.RESULT_INVALID)
        if page.next_cursor is not None and type(page.next_cursor) is not str:
            _fail(McpClientErrorReason.RESULT_INVALID)
        if type(page.result_type) is not str or not isinstance(page.meta, Mapping):
            _fail(McpClientErrorReason.RESULT_INVALID)
        try:
            document = page.model_dump(by_alias=True, exclude_none=True, mode="json")
        except (AttributeError, TypeError, ValueError):
            _fail(McpClientErrorReason.RESULT_INVALID)
        snapshot.append(
            _ObservedPage(
                surface=surface,
                meta=_freeze_json(page.meta),
                ttl_ms=page.ttl_ms,
                cache_scope=page.cache_scope,
                next_cursor=page.next_cursor,
                result_type=page.result_type,
                items=items,
                document=_freeze_json(document),
            )
        )
    return tuple(snapshot), item_count


@dataclass(frozen=True, slots=True)
class ObservedCapabilities:
    """Transport-free SDK result pages collected by the client lifecycle."""

    limits: McpClientLimits
    source: DiscoverySource
    metadata: Mapping[str, Any]
    tool_pages: tuple[_ObservedPage, ...]
    prompt_pages: tuple[_ObservedPage, ...]
    resource_pages: tuple[_ObservedPage, ...]
    resource_template_pages: tuple[_ObservedPage, ...]
    extensions: Mapping[str, Any]
    _document_bytes: int = field(init=False, repr=False, compare=False, default=0)

    def __post_init__(self) -> None:
        if type(self.limits) is not McpClientLimits:
            raise ValueError("observed discovery limits are invalid")
        if type(self.source) is not DiscoverySource:
            raise ValueError("observed discovery source is invalid")
        if not isinstance(self.metadata, Mapping) or not isinstance(self.extensions, Mapping):
            raise ValueError("observed discovery metadata is invalid")
        item_count = len(self.extensions)
        if item_count > self.limits.max_list_items:
            _fail(McpClientErrorReason.RESULT_LIMIT_EXCEEDED)
        budget = _CanonicalBudget(self.limits.max_discovery_document_bytes)
        _preflight_document(budget, self.metadata)
        _preflight_document(budget, self.extensions)
        metadata = _thaw_json(_freeze_json(self.metadata))
        extensions = _thaw_json(_freeze_json(self.extensions))
        definitions = (
            ("tool_pages", types.ListToolsResult, types.Tool, "tools", "tools"),
            ("prompt_pages", types.ListPromptsResult, types.Prompt, "prompts", "prompts"),
            (
                "resource_pages",
                types.ListResourcesResult,
                types.Resource,
                "resources",
                "resources",
            ),
            (
                "resource_template_pages",
                types.ListResourceTemplatesResult,
                types.ResourceTemplate,
                "resource_templates",
                "resource_templates",
            ),
        )
        for field_name, page_type, item_type, item_field, surface in definitions:
            pages, item_count = _snapshot_pages(
                getattr(self, field_name),
                limits=self.limits,
                page_type=page_type,
                item_type=item_type,
                item_field=item_field,
                surface=surface,
                item_count=item_count,
                budget=budget,
            )
            object.__setattr__(self, field_name, pages)
        object.__setattr__(self, "metadata", _freeze_json(metadata))
        object.__setattr__(self, "extensions", _freeze_json(extensions))
        object.__setattr__(self, "_document_bytes", budget.size)


def _canonical_requirement(requirement: Any) -> AppRequirement:
    if type(requirement) is not AppRequirement:
        raise ValueError("declared capabilities requirement is invalid")
    try:
        return AppRequirement.from_dict(requirement.to_dict())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("declared capabilities requirement is invalid") from exc


def _revision_document(
    groups: Mapping[str, tuple[NamedCapability, ...]],
) -> dict[str, list[dict[str, str | None]]]:
    return {
        field: [
            {
                "name": item.name,
                "schema_sha256": item.schema_sha256,
                "version": item.version,
            }
            for item in sorted(
                groups[field],
                key=lambda capability: (
                    capability.name,
                    capability.schema_sha256 or "",
                    capability.version or "",
                ),
            )
        ]
        for field in ("tools", "prompts", "resources", "extensions")
    }


def _capability_revision(
    groups: Mapping[str, tuple[NamedCapability, ...]],
) -> str:
    return sha256_hex(canonical_bytes(_revision_document(groups)))


def _deployment(value: AppDeployment) -> McpClientDeployment:
    return McpClientDeployment(value.value)


def _receipt_transport(requirement: AppRequirement) -> McpTransport:
    selected = (
        PluginMcpTransport.STDIO
        if requirement.deployment is AppDeployment.LOCAL
        else PluginMcpTransport.STREAMABLE_HTTP
    )
    if selected not in requirement.transports:
        raise ValueError("declared capabilities transport is invalid")
    return McpTransport(selected.value)


def _catalog_entry(capability: NamedCapability) -> dict[str, object]:
    if capability.name == _TASKS_EXTENSION_IDENTIFIER:
        return {}
    entry: dict[str, object] = {
        "description": {
            "source": "signed_catalog",
            "text": _NO_EXTERNAL_DESCRIPTION,
        }
    }
    if capability.version is not None:
        entry["version"] = capability.version
    else:
        entry["schema_sha256"] = capability.schema_sha256
    return entry


def project_declared_capabilities(requirement: AppRequirement) -> CapabilityProjection:
    """Project one signed declaration without opening a process or network path."""

    requirement = _canonical_requirement(requirement)
    grouped: dict[str, list[NamedCapability]] = {
        "tools": [],
        "prompts": [],
        "resources": [],
        "extensions": [],
    }
    fields = {
        CapabilityKind.TOOL: "tools",
        CapabilityKind.PROMPT: "prompts",
        CapabilityKind.RESOURCE: "resources",
        CapabilityKind.EXTENSION: "extensions",
    }
    for capability in requirement.capabilities:
        grouped[fields[capability.kind]].append(
            NamedCapability(
                name=capability.name,
                schema_sha256=capability.schema_sha256,
                version=capability.version,
                description=(
                    None if capability.name == _TASKS_EXTENSION_IDENTIFIER else _CATALOG_DESCRIPTION
                ),
                # The declaration knows its own kind; carrying it keeps a
                # declared capability comparable to the live one beside it.
                kind=capability.kind,
            )
        )
    groups = {
        field: tuple(sorted(items, key=lambda item: item.name)) for field, items in grouped.items()
    }
    revision = _capability_revision(groups)
    snapshot = AppCapabilitySnapshot(
        app_id=requirement.app_id,
        publisher_id=requirement.publisher_id,
        deployment=requirement.deployment,
        provider_id=requirement.provider_id,
        protocol_version=requirement.protocol_version,
        transports=tuple(requirement.transports),
        remote_origin=requirement.remote_origin,
        authorization_issuer=requirement.authorization_issuer,
        authentication=requirement.authentication,
        tools=groups["tools"],
        prompts=groups["prompts"],
        resources=groups["resources"],
        extensions=groups["extensions"],
        capability_revision=revision,
        installed=True,
        trusted=True,
        enabled=False,
    )
    receipt = McpDiscoveryReceipt(
        app_id=requirement.app_id,
        publisher_id=requirement.publisher_id,
        deployment=_deployment(requirement.deployment),
        transport=_receipt_transport(requirement),
        protocol_version=requirement.protocol_version,
        source=DiscoverySource.CATALOG_DECLARED,
        server_id=None,
        server_version=None,
        tools={item.name: _catalog_entry(item) for item in groups["tools"]},
        prompts={item.name: _catalog_entry(item) for item in groups["prompts"]},
        resources={item.name: _catalog_entry(item) for item in groups["resources"]},
        resource_templates={},
        extensions={item.name: _catalog_entry(item) for item in groups["extensions"]},
        cache_metadata={},
        capability_revision=revision,
    )
    return CapabilityProjection(snapshot=snapshot, receipt=receipt)


def _fail(reason: McpClientErrorReason) -> None:
    raise McpClientError(reason)


def _report_capability_drift(
    declared: set[str],
    observed: set[str],
) -> None:
    """Name what the package expected and count what it did not.

    Only the declared side is named. Those strings come from the signed package
    -- Omega's own reviewed vocabulary -- while a name the provider offered and
    nobody signed is provider-authored text, and the runtime log is closed to
    that. A count still separates "this provider dropped tools" from "this
    provider has more than the package knows", which is the distinction an
    operator needs.
    """
    missing = sorted(declared - observed)
    extra = len(observed - declared)
    _LOGGER.warning(
        "capability_drift: declared=%d live=%d missing=%d unexpected=%d%s",
        len(declared),
        len(observed),
        len(missing),
        extra,
        f"; package declares but server does not offer: {', '.join(missing)}" if missing else "",
    )
    if extra:
        # Named as a count on purpose. A per-account provider (Grafana Cloud
        # proxies whatever datasources a stack has) shows up exactly here, and
        # it is not a package that can be fixed by re-capturing -- a fixed
        # signed authority cannot describe a surface that differs per tenant.
        _LOGGER.warning(
            "capability_drift: the server offers %d capability(ies) this package "
            "does not declare; a surface that varies per account cannot be packaged",
            extra,
        )


def _validate_source(
    descriptor: McpConnectionDescriptor,
    source: DiscoverySource,
) -> None:
    if source is DiscoverySource.CATALOG_DECLARED:
        _fail(McpClientErrorReason.RESULT_INVALID)
    if descriptor.deployment is McpClientDeployment.REMOTE:
        if source is not DiscoverySource.LIVE_AUTHENTICATED:
            _fail(McpClientErrorReason.AUTHORIZATION_REQUIRED)
    elif source is not DiscoverySource.LIVE_OBSERVED:
        _fail(McpClientErrorReason.RESULT_INVALID)


def _identity_value_equal(actual: Any, expected: Any) -> bool:
    if expected is None:
        return actual is None
    return type(actual) is str and actual == expected


def _validate_identity(
    descriptor: McpConnectionDescriptor,
    metadata: Any,
    *,
    expected_server: tuple[str, str] | None = None,
) -> tuple[str, str]:
    if not isinstance(metadata, Mapping) or any(type(key) is not str for key in metadata):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    expected = {
        "appId": descriptor.app_id,
        "publisherId": descriptor.publisher_id,
        "deployment": descriptor.deployment.value,
        "providerId": descriptor.provider_id,
        "protocolVersion": descriptor.protocol_version,
        "transport": descriptor.transport.value,
        "remoteOrigin": descriptor.remote_origin,
        "authorizationIssuer": descriptor.authorization_issuer,
    }
    if not _identity_value_equal(metadata.get("protocolVersion"), expected["protocolVersion"]):
        _fail(McpClientErrorReason.PROTOCOL_MISMATCH)
    if any(
        not _identity_value_equal(metadata.get(field), expected[field])
        for field in _IDENTITY_FIELDS
        if field != "protocolVersion"
    ):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    server_id = metadata.get("serverId")
    server_version = metadata.get("serverVersion")
    if (
        type(server_id) is not str
        or not server_id
        or type(server_version) is not str
        or not server_version
    ):
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    identity = (server_id, server_version)
    if expected_server is not None and identity != expected_server:
        _fail(McpClientErrorReason.IDENTITY_MISMATCH)
    return identity


def _collect_pages(
    descriptor: McpConnectionDescriptor,
    pages: tuple[_ObservedPage, ...],
    *,
    surface: str,
    server_identity: tuple[str, str],
) -> tuple[list[Any], dict[str, object]]:
    if not pages or len(pages) > descriptor.limits.max_list_pages:
        _fail(McpClientErrorReason.PAGINATION_LIMIT_EXCEEDED)
    items: list[Any] = []
    cursors: set[str] = set()
    cache_identity: tuple[int, str] | None = None
    for index, page in enumerate(pages):
        if (
            type(page) is not _ObservedPage
            or page.surface != surface
            or page.result_type != "complete"
        ):
            _fail(McpClientErrorReason.RESULT_INVALID)
        _validate_identity(
            descriptor,
            page.meta,
            expected_server=server_identity,
        )
        ttl_ms = page.ttl_ms
        scope = page.cache_scope
        if (
            type(ttl_ms) is not int
            or ttl_ms < 0
            or ttl_ms > descriptor.limits.max_cache_ttl_seconds * 1_000
            or type(scope) is not str
            or scope not in {"public", "private"}
        ):
            _fail(McpClientErrorReason.RESULT_INVALID)
        page_cache_identity = (ttl_ms, scope)
        if cache_identity is None:
            cache_identity = page_cache_identity
        elif cache_identity != page_cache_identity:
            _fail(McpClientErrorReason.RESULT_INVALID)
        cursor = page.next_cursor
        if cursor is not None:
            if type(cursor) is not str or not cursor or cursor in cursors:
                _fail(McpClientErrorReason.PAGINATION_LIMIT_EXCEEDED)
            cursors.add(cursor)
            if len(cursors) > descriptor.limits.max_cursor_history:
                _fail(McpClientErrorReason.PAGINATION_LIMIT_EXCEEDED)
        if index < len(pages) - 1 and cursor is None:
            _fail(McpClientErrorReason.RESULT_INVALID)
        if index == len(pages) - 1 and cursor is not None:
            _fail(McpClientErrorReason.PAGINATION_LIMIT_EXCEEDED)
        items.extend(page.items)
    if cache_identity is None:
        _fail(McpClientErrorReason.RESULT_INVALID)
    return (
        items,
        {"scope": cache_identity[1], "ttl_ms": cache_identity[0]},
    )


def _description(value: Any) -> tuple[str, dict[str, str]]:
    text = _MISSING_EXTERNAL_DESCRIPTION if value is None else value
    if (
        type(text) is not str
        or not text
        or text.strip() != text
        or unicodedata.normalize("NFC", text) != text
        or len(_EXTERNAL_DESCRIPTION_PREFIX) + len(text) > 1_024
        or any(
            unicodedata.category(character).startswith("C")
            or unicodedata.category(character) in {"Zl", "Zp"}
            for character in text
        )
    ):
        _fail(McpClientErrorReason.RESULT_INVALID)
    return (
        f"{_EXTERNAL_DESCRIPTION_PREFIX}{text}",
        {"source": "mcp_server", "text": text},
    )


def _provider_description(value: str | None, descriptor: McpConnectionDescriptor) -> str | None:
    if descriptor.deployment is not McpClientDeployment.REMOTE or type(value) is not str:
        return value
    # Remote servers may use multiline markdown or long display descriptions.
    # Normalize only their display formatting; attribution and validation of
    # the bounded receipt text still happen at the common description boundary.
    #
    # Stripped AFTER the cut, not before: collapsing whitespace leaves no edge
    # space, but the cut lands wherever the bound falls, and when that is just
    # after a space the result ends in one -- which `_description` then refuses
    # as unstripped. Five of Wix's tools are long enough to be cut, and whether
    # each one connected came down to whether character 1010 was a space.
    return " ".join(value.split())[: 1_024 - len(_EXTERNAL_DESCRIPTION_PREFIX)].strip()


def _plain_mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        _fail(McpClientErrorReason.RESULT_INVALID)
    return value


def _schema_digest(value: Any) -> str:
    try:
        return sha256_hex(canonical_bytes(_thaw_json(value)))
    except (TypeError, ValueError):
        _fail(McpClientErrorReason.RESULT_INVALID)


def _named(
    *,
    name: Any,
    schema_sha256: str | None,
    version: str | None,
    description: Any,
    kind: CapabilityKind | None = None,
) -> tuple[NamedCapability, dict[str, str]]:
    named_description, attributed = _description(description)
    try:
        capability = NamedCapability(
            name=name,
            schema_sha256=schema_sha256,
            version=version,
            description=named_description,
            kind=kind,
        )
    except (TypeError, ValueError):
        _fail(McpClientErrorReason.RESULT_INVALID)
    return capability, attributed


def _prompt_schema(prompt: _ObservedPrompt) -> dict[str, object]:
    arguments = prompt.arguments
    parsed: list[dict[str, object]] = []
    names: set[str] = set()
    for argument in arguments:
        if type(argument) is not _ObservedPromptArgument or argument.name in names:
            _fail(McpClientErrorReason.RESULT_INVALID)
        names.add(argument.name)
        parsed.append(
            {
                "name": argument.name,
                "required": argument.required is True,
            }
        )
    return {"arguments": sorted(parsed, key=lambda item: str(item["name"]))}


def _extension_capabilities(
    extensions: Any,
    *,
    declared_versions: Mapping[str, str | None],
) -> tuple[tuple[NamedCapability, ...], dict[str, dict[str, object]]]:
    extension_mapping = _plain_mapping(extensions)
    named: list[NamedCapability] = []
    receipt: dict[str, dict[str, object]] = {}
    for name in sorted(extension_mapping):
        document = _plain_mapping(extension_mapping[name])
        if name == _TASKS_EXTENSION_IDENTIFIER:
            if document:
                _fail(McpClientErrorReason.RESULT_INVALID)
            try:
                capability = NamedCapability(
                    name=name,
                    schema_sha256=None,
                    version=None,
                    description=None,
                    kind=CapabilityKind.EXTENSION,
                )
            except (TypeError, ValueError):
                _fail(McpClientErrorReason.RESULT_INVALID)
            named.append(capability)
            receipt[capability.name] = {}
            continue
        if set(document) != {"version", "description"}:
            _fail(McpClientErrorReason.RESULT_INVALID)
        capability, attributed = _named(
            name=name,
            schema_sha256=None,
            version=document["version"],
            description=document["description"],
            kind=CapabilityKind.EXTENSION,
        )
        named.append(capability)
        receipt[capability.name] = {
            "description": attributed,
            "version": capability.version,
        }
    return tuple(named), receipt


def _ensure_unique(items: list[NamedCapability]) -> None:
    names = [item.name for item in items]
    if len(names) != len(set(names)):
        _fail(McpClientErrorReason.CAPABILITY_DRIFT)


def _declared_identity(groups: Mapping[str, tuple[NamedCapability, ...]]) -> dict[str, str | None]:
    singular = {
        "tools": "tool",
        "prompts": "prompt",
        "resources": "resource",
    }
    return {
        f"{singular[field]}:{item.name}": item.schema_sha256
        for field in ("tools", "prompts", "resources")
        for item in groups[field]
    }


def _signed_capability_ceiling(
    descriptor: McpConnectionDescriptor,
) -> dict[str, str | None] | None:
    """Every capability the package's signed authority vouches for.

    This, not the installed record, is what a live surface may not exceed. The
    record holds whatever was granted last time and is narrowed as that changes;
    the signed authority is the fixed set someone reviewed, so it is the only
    honest ceiling -- and using it is what lets an operator who ticks MORE boxes
    on a later sign-in still connect.

    None when the package ships no signed authority, which is a local plugin
    with no reviewed superset to be a subset of.
    """
    authority = descriptor.signed_capability_authority
    if not isinstance(authority, Mapping):
        return None
    ceiling: dict[str, str | None] = {}
    # A signed tool carries `input_schema_sha256`; a signed prompt carries
    # `schema_sha256` over its argument list; a resource carries no digest at
    # all. Resource TEMPLATES are resources here for the same reason
    # `_live_projection` files them there -- one namespace, checked for
    # collisions, so a template must occupy its name in the ceiling too.
    for group, kind, digest_field in (
        ("tools", "tool", "input_schema_sha256"),
        ("prompts", "prompt", "schema_sha256"),
        ("resources", "resource", None),
        ("resource_templates", "resource", None),
    ):
        entries = authority.get(group)
        if not isinstance(entries, Mapping):
            continue
        for name, policy in entries.items():
            if type(name) is not str:
                return None
            digest = (
                policy.get(digest_field)
                if digest_field is not None and isinstance(policy, Mapping)
                else None
            )
            ceiling[f"{kind}:{name}"] = digest if type(digest) is str else None
    return ceiling


def _validate_declared_authority(
    descriptor: McpConnectionDescriptor,
    groups: Mapping[str, tuple[NamedCapability, ...]],
) -> bool:
    """Admit a live surface against the signed ceiling; report whether it narrowed.

    Returns True when the operator granted strictly fewer capabilities than the
    record declares, which is the case whose capability revision legitimately
    differs from the descriptor's.
    """
    observed = _declared_identity(groups)
    declared = dict(descriptor.declared_capabilities)
    ceiling = _signed_capability_ceiling(descriptor)
    # Permitted is the union, not the intersection. The record's declarations
    # came from the signed package at install, so they are reviewed by
    # construction; the signed authority additionally covers what an earlier
    # sign-in narrowed away. Intersecting them would erase a whole group the
    # authority happens not to enumerate -- a package that signs tool policies
    # but no resource policies declares its resources only in the record.
    permitted: dict[str, str | None] = dict(declared) if ceiling is None else {
        **ceiling,
        **declared,
    }
    unexpected = set(observed) - set(permitted)
    if unexpected:
        _report_capability_drift(set(permitted), set(observed))
        _fail(McpClientErrorReason.CAPABILITY_DRIFT)
    if any(
        permitted[name] is not None and observed[name] != permitted[name] for name in observed
    ):
        _fail(McpClientErrorReason.CAPABILITY_DRIFT)
    # A subset is a TOOL concept, because a consent checklist is. Webflow lets
    # the operator choose tools; no provider offers a prompt or resource
    # checklist, so a declared prompt or resource that has stopped being served
    # is the server changing under a signed package, which is what
    # `capability_drift` has always been for. Narrowing the rule to tools keeps
    # that guard -- including the case where a server drops a whole group it
    # never advertised.
    missing = set(declared) - set(observed)
    if any(not name.startswith("tool:") for name in missing):
        _report_capability_drift(set(declared), set(observed))
        _fail(McpClientErrorReason.CAPABILITY_DRIFT)
    # Granting nothing is not a grant. An empty tool surface against a package
    # that declares tools is a server that has not finished warming (Grafana
    # answers zero for about fifteen seconds) or an operator who ticked no
    # boxes; neither is a connection worth making, and admitting it would
    # produce a plugin that is connected and can do nothing.
    if any(name.startswith("tool:") for name in declared) and not any(
        name.startswith("tool:") for name in observed
    ):
        _report_capability_drift(set(declared), set(observed))
        _fail(McpClientErrorReason.CAPABILITY_DRIFT)
    # Extensions are negotiated between Omega and the server, never chosen by
    # the operator, so they stay exact.
    observed_extensions = {item.name: item.version for item in groups["extensions"]}
    if observed_extensions != dict(descriptor.extension_versions):
        _fail(McpClientErrorReason.CAPABILITY_DRIFT)
    return set(observed) != set(declared)


def _validated_apps_ui(
    descriptor: McpConnectionDescriptor,
    *,
    group: str,
    name: str,
    observed: Mapping[str, Any] | None,
) -> dict[str, object] | None:
    authority = descriptor.signed_capability_authority
    signed_apps = None if authority is None else authority.get("mcp_apps_metadata")
    signed_group = None if not isinstance(signed_apps, Mapping) else signed_apps.get(group)
    signed = None if not isinstance(signed_group, Mapping) else signed_group.get(name)
    if signed is None and MCP_APPS_EXTENSION_IDENTIFIER not in descriptor.extension_versions:
        # An extension this connection never negotiated is not part of its
        # contract, and MCP puts extension data in ``_meta`` precisely so a
        # client that does not know it can ignore it. Reading it as drift means
        # any provider that ships an extension Omega has not declared becomes
        # unconnectable: Slack attaches an MCP Apps UI hint to one tool without
        # advertising the extension, and a signed, installed, authorized plugin
        # failed at connect with capability_drift (live 2026-09-07). Ignoring
        # the hint is also the safer half -- the tool is still called as an
        # ordinary tool, just without the provider's UI.
        #
        # Only the undeclared case is waived. A package that DOES declare the
        # extension still reconciles the observed metadata against its signed
        # copy exactly, and signed metadata present here without the extension
        # declared stays an error rather than being quietly skipped.
        return None
    if observed is None and signed is None:
        return None
    if not isinstance(observed, Mapping) or not isinstance(signed, Mapping):
        _fail(McpClientErrorReason.CAPABILITY_DRIFT)
    try:
        plain_observed = _thaw_json(observed)
        plain_signed = _thaw_json(signed)
        return validate_mcp_apps_metadata(
            plain_observed,
            signed_metadata=plain_signed,
            declared_extensions=descriptor.extension_versions,
            max_bytes=min(
                descriptor.limits.max_discovery_document_bytes,
                _MAX_MCP_APPS_METADATA_BYTES,
            ),
        )
    except ValueError:
        _fail(McpClientErrorReason.CAPABILITY_DRIFT)


def _live_projection(
    descriptor: McpConnectionDescriptor,
    observed: ObservedCapabilities,
) -> CapabilityProjection:
    if observed.limits != descriptor.limits:
        _fail(McpClientErrorReason.RESULT_INVALID)
    _validate_source(descriptor, observed.source)
    server_identity = _validate_identity(descriptor, observed.metadata)
    collected: dict[str, list[Any]] = {}
    cache_metadata: dict[str, dict[str, object]] = {}
    definitions = (
        ("tools", observed.tool_pages),
        ("prompts", observed.prompt_pages),
        ("resources", observed.resource_pages),
        ("resource_templates", observed.resource_template_pages),
    )
    for surface, pages in definitions:
        items, cache = _collect_pages(
            descriptor,
            pages,
            surface=surface,
            server_identity=server_identity,
        )
        collected[surface] = items
        cache_metadata[surface] = cache

    tools: list[NamedCapability] = []
    tool_receipt: dict[str, dict[str, object]] = {}
    for tool in collected["tools"]:
        input_digest = _schema_digest(tool.input_schema)
        output_digest = None if tool.output_schema is None else _schema_digest(tool.output_schema)
        capability, attributed = _named(
            name=tool.name,
            schema_sha256=input_digest,
            version=None,
            description=_provider_description(tool.description, descriptor),
            kind=CapabilityKind.TOOL,
        )
        tools.append(capability)
        entry: dict[str, object] = {
            "description": attributed,
            "input_schema_sha256": input_digest,
        }
        if output_digest is not None:
            entry["output_schema_sha256"] = output_digest
        apps_ui = _validated_apps_ui(
            descriptor,
            group="tools",
            name=capability.name,
            observed=tool.apps_ui,
        )
        if apps_ui is not None:
            entry["ui"] = apps_ui
        tool_receipt[capability.name] = entry

    prompts: list[NamedCapability] = []
    prompt_receipt: dict[str, dict[str, object]] = {}
    for prompt in collected["prompts"]:
        digest = _schema_digest(_prompt_schema(prompt))
        capability, attributed = _named(
            name=prompt.name,
            schema_sha256=digest,
            version=None,
            description=_provider_description(prompt.description, descriptor),
            kind=CapabilityKind.PROMPT,
        )
        prompts.append(capability)
        prompt_receipt[capability.name] = {
            "description": attributed,
            "schema_sha256": digest,
        }

    resources: list[NamedCapability] = []
    resource_receipt: dict[str, dict[str, object]] = {}
    for resource in collected["resources"]:
        capability, attributed = _named(
            name=resource.name,
            schema_sha256=None,
            version=None,
            description=_provider_description(resource.description, descriptor),
            # A provider names its resources for people to read. Stating the
            # kind is what lets that name be a label instead of an identifier.
            kind=CapabilityKind.RESOURCE,
        )
        resources.append(capability)
        entry = {
            "description": attributed,
            "mime_type": resource.mime_type,
            "uri": resource.uri,
        }
        apps_ui = _validated_apps_ui(
            descriptor,
            group="resources",
            name=capability.name,
            observed=resource.apps_ui,
        )
        if apps_ui is not None:
            entry["ui"] = apps_ui
        resource_receipt[capability.name] = entry

    template_receipt: dict[str, dict[str, object]] = {}
    for template in collected["resource_templates"]:
        capability, attributed = _named(
            name=template.name,
            schema_sha256=None,
            version=None,
            description=_provider_description(template.description, descriptor),
            kind=CapabilityKind.RESOURCE,
        )
        resources.append(capability)
        entry = {
            "description": attributed,
            "mime_type": template.mime_type,
            "uri_template": template.uri_template,
        }
        apps_ui = _validated_apps_ui(
            descriptor,
            group="resource_templates",
            name=capability.name,
            observed=template.apps_ui,
        )
        if apps_ui is not None:
            entry["ui"] = apps_ui
        template_receipt[capability.name] = entry

    resource_endpoints = [item.uri for item in collected["resources"]]
    template_endpoints = [item.uri_template for item in collected["resource_templates"]]
    if (
        len(resource_endpoints) != len(set(resource_endpoints))
        or len(template_endpoints) != len(set(template_endpoints))
        or set(resource_endpoints) & set(template_endpoints)
    ):
        _fail(McpClientErrorReason.CAPABILITY_DRIFT)

    extensions, extension_receipt = _extension_capabilities(
        observed.extensions,
        declared_versions=descriptor.extension_versions,
    )
    for items in (tools, prompts, resources):
        _ensure_unique(items)
    groups = {
        "tools": tuple(tools),
        "prompts": tuple(prompts),
        "resources": tuple(resources),
        "extensions": extensions,
    }
    narrowed = _validate_declared_authority(descriptor, groups)
    revision = _capability_revision(groups)
    # A granted subset has its own revision by construction, and that revision
    # is what the connection carries from here on. Only a surface that matches
    # the record capability-for-capability must also match its digest.
    if not narrowed and revision != descriptor.capability_revision:
        _fail(McpClientErrorReason.CAPABILITY_DRIFT)
    snapshot = AppCapabilitySnapshot(
        app_id=descriptor.app_id,
        publisher_id=descriptor.publisher_id,
        deployment=AppDeployment(descriptor.deployment.value),
        provider_id=descriptor.provider_id,
        protocol_version=descriptor.protocol_version,
        transports=(PluginMcpTransport(descriptor.transport.value),),
        remote_origin=descriptor.remote_origin,
        authorization_issuer=descriptor.authorization_issuer,
        authentication=descriptor.authentication,
        tools=groups["tools"],
        prompts=groups["prompts"],
        resources=groups["resources"],
        extensions=groups["extensions"],
        capability_revision=revision,
        installed=True,
        trusted=True,
        enabled=False,
    )
    receipt = McpDiscoveryReceipt(
        app_id=descriptor.app_id,
        publisher_id=descriptor.publisher_id,
        deployment=descriptor.deployment,
        transport=descriptor.transport,
        protocol_version=descriptor.protocol_version,
        source=observed.source,
        server_id=server_identity[0],
        server_version=server_identity[1],
        tools=tool_receipt,
        prompts=prompt_receipt,
        resources=resource_receipt,
        resource_templates=template_receipt,
        extensions=extension_receipt,
        cache_metadata=cache_metadata,
        capability_revision=revision,
    )
    return CapabilityProjection(snapshot=snapshot, receipt=receipt)


def reconcile_live_capabilities(
    descriptor: McpConnectionDescriptor,
    observed: ObservedCapabilities,
) -> CapabilityProjection:
    """Validate live SDK-shaped results against one exact trusted descriptor."""

    if (
        type(descriptor) is not McpConnectionDescriptor
        or type(observed) is not ObservedCapabilities
    ):
        _fail(McpClientErrorReason.RESULT_INVALID)
    try:
        return _live_projection(descriptor, observed)
    except McpClientError:
        raise
    except (AttributeError, TypeError, ValueError):
        _fail(McpClientErrorReason.RESULT_INVALID)


__all__ = [
    "CapabilityProjection",
    "ObservedCapabilities",
    "canonical_resource_uri",
    "project_declared_capabilities",
    "reconcile_live_capabilities",
]

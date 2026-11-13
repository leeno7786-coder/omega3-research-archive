"""Real stdio and loopback HTTP MCP peers for generic plugin acceptance."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import anyio  # noqa: E402

from mcp_platform.canonical import canonical_bytes, sha256_hex  # noqa: E402
from mcp_platform.package_contracts import ResourceCatalog, ToolCatalog  # noqa: E402
from mcp_platform.sdk import FirstPartyMcpServer  # noqa: E402
from mcp_platform.sdk.server import ServerHostIdentity  # noqa: E402

CALENDAR_PLUGIN = "northwind.calendar"
CALENDAR_APP = "northwind.calendar.mcp"
TRACKER_PLUGIN = "northwind.tracker"
TRACKER_APP = "northwind.tracker.mcp"
TRACKER_ORIGIN = "https://tracker.northwind.test"
TRACKER_ISSUER = "https://auth.northwind.test/oauth2"
LEDGER_PLUGIN = "northwind.ledger"
LEDGER_APP = "northwind.ledger.mcp"
CALENDAR_MIRROR_PLUGIN = "northwind.calendar.mirror"
RESOURCE_PLUGIN = "northwind.resources"
RESOURCE_APP = "northwind.resources.mcp"
RESOURCE_TEMPLATE_PLUGIN = "northwind.resource-templates"
RESOURCE_TEMPLATE_APP = "northwind.resource-templates.mcp"
PROMPT_PLUGIN = "northwind.prompts"
PROMPT_APP = "northwind.prompts.mcp"
ZERO_TOOL_PEERS = {
    "resource": (RESOURCE_PLUGIN, RESOURCE_APP, "resource", "records.current"),
    "resource_template": (
        RESOURCE_TEMPLATE_PLUGIN,
        RESOURCE_TEMPLATE_APP,
        "resource_template",
        "records.by_id",
    ),
    "prompt": (PROMPT_PLUGIN, PROMPT_APP, "prompt", "records.summarize"),
}

_EVENT_ITEM = "standup"
_ISSUE_ITEM = "NW-41"
_LEDGER_ITEM = "412.00"


def closed_object_schema(properties: dict[str, object], required: list[str]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def item_output_schema() -> dict[str, object]:
    return closed_object_schema({"item": {"type": "string", "minLength": 1}}, ["item"])


def tool_document(
    *,
    name: str,
    title: str,
    input_schema: dict[str, object],
    risk_class: str,
    effect: str,
) -> dict[str, object]:
    output_schema = item_output_schema()
    return {
        "name": name,
        "title": title,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "input_schema_sha256": sha256_hex(canonical_bytes(input_schema)),
        "output_schema_sha256": sha256_hex(canonical_bytes(output_schema)),
        "capability_tags": [name.replace("_", ".")],
        "provider_extension_tags": [],
        "risk_class": risk_class,
        "effect": effect,
        "requires_revision": False,
        "max_result_bytes": 4096,
        "allowed_mime_types": ["application/json", "text/plain"],
    }


def catalogs_for(plugin: str) -> tuple[ToolCatalog, ResourceCatalog]:
    if plugin == "calendar":
        server_id = CALENDAR_APP
        tools = [
            tool_document(
                name="list_events",
                title="List Northwind events",
                input_schema=closed_object_schema({"start": {"type": "string"}}, ["start"]),
                risk_class="read_only",
                effect="read",
            )
        ]
    elif plugin == "tracker":
        server_id = TRACKER_APP
        tools = [
            tool_document(
                name="create_issue",
                title="Create Northwind issue",
                input_schema=closed_object_schema({"summary": {"type": "string"}}, ["summary"]),
                risk_class="additive_write",
                effect="create",
            )
        ]
    elif plugin == "ledger":
        server_id = LEDGER_APP
        tools = [
            tool_document(
                name="show_balance",
                title="Show Northwind ledger balance",
                input_schema=closed_object_schema({"query": {"type": "string"}}, ["query"]),
                risk_class="read_only",
                effect="read",
            )
        ]
    elif plugin in ZERO_TOOL_PEERS:
        _plugin_id, server_id, _kind, _capability_id = ZERO_TOOL_PEERS[plugin]
        tools = []
    else:
        raise ValueError(f"unknown generic peer {plugin}")
    resources = []
    resource_templates = []
    if plugin == "resource":
        resources = [
            {
                "uri": "omega://records/current",
                "name": "records.current",
                "mime_type": "application/json",
                "max_bytes": 4096,
                "capability_tags": ["records.read"],
            }
        ]
    elif plugin == "resource_template":
        resource_templates = [
            {
                "uri_template": "omega://records/{record_id}",
                "name": "records.by_id",
                "mime_type": "application/json",
                "max_bytes": 4096,
                "capability_tags": ["records.read"],
            }
        ]
    return (
        ToolCatalog.from_dict(
            {
                "catalog_version": 1,
                "server_id": server_id,
                "server_version": "1.0.0",
                "tools": tools,
            }
        ),
        ResourceCatalog.from_dict(
            {
                "catalog_version": 1,
                "server_id": server_id,
                "server_version": "1.0.0",
                "resources": resources,
                "resource_templates": resource_templates,
            }
        ),
    )


def host_identity_for(plugin: str) -> ServerHostIdentity:
    if plugin == "tracker" or plugin in ZERO_TOOL_PEERS:
        return ServerHostIdentity(
            publisher_id="omega.first_party",
            deployment="remote",
            transport="streamable_http",
            provider_id="northwind",
            remote_origin=TRACKER_ORIGIN,
            authorization_issuer=TRACKER_ISSUER,
        )
    return ServerHostIdentity(
        publisher_id="omega.first_party",
        deployment="local",
        transport="stdio",
        provider_id="northwind",
    )


def build_server(plugin: str) -> FirstPartyMcpServer:
    tool_catalog, resource_catalog = catalogs_for(plugin)
    server = FirstPartyMcpServer(
        tool_catalog,
        resource_catalog,
        host_identity=host_identity_for(plugin),
    )

    async def list_events(arguments: Mapping[str, object]) -> Mapping[str, object]:
        del arguments
        return {"item": _EVENT_ITEM}

    async def create_issue(arguments: Mapping[str, object]) -> Mapping[str, object]:
        del arguments
        return {"item": _ISSUE_ITEM}

    async def show_balance(arguments: Mapping[str, object]) -> Mapping[str, object]:
        del arguments
        return {"item": _LEDGER_ITEM}

    handlers = {
        "list_events": list_events,
        "create_issue": create_issue,
        "show_balance": show_balance,
    }
    for tool in tool_catalog.tools:
        server.register_tool(tool.name, handlers[tool.name])
    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    transport = parser.add_mutually_exclusive_group(required=True)
    transport.add_argument("--stdio", action="store_true")
    transport.add_argument("--http", action="store_true")
    parser.add_argument(
        "--plugin",
        required=True,
        choices=("calendar", "tracker", "ledger", *ZERO_TOOL_PEERS),
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = build_server(args.plugin)
    if args.stdio:
        anyio.run(server.run_stdio)
        return
    anyio.run(partial(server.run_streamable_http, host="127.0.0.1", port=args.port))


if __name__ == "__main__":
    main()

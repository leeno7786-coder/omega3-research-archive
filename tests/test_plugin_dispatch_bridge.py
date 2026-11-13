from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.package_contracts import RiskClass
from mcp_platform.plugin_catalog import PluginToolBinding
from mcp_platform.plugin_dispatch import (
    canonical_plugin_tool_name,
    invoke_plugin_tool,
    plugin_tool_registry_entries,
    resolve_plugin_tool_request,
)
from mcp_platform.plugin_runtime import PluginRuntimeError
from mcp_platform.plugin_planning import retrieve_plugin_candidates


def _binding(
    *,
    plugin_id: str = "northwind.calendar",
    app_id: str = "northwind.calendar.mcp",
    capability: str = "list_events",
    title: str = "List Northwind events",
    risk: RiskClass = RiskClass.READ_ONLY,
    properties: dict[str, object] | None = None,
    required: list[str] | None = None,
    effect: str = "read",
) -> PluginToolBinding:
    input_schema = {
        "type": "object",
        "properties": properties or {"start": {"type": "string"}},
        "required": required or ["start"],
        "additionalProperties": False,
    }
    output_schema = {
        "type": "object",
        "properties": {"item": {"type": "string"}},
        "required": ["item"],
        "additionalProperties": False,
    }
    return PluginToolBinding(
        candidate_id="a" * 64,
        tool_name=canonical_plugin_tool_name(plugin_id, capability),
        plugin_id=plugin_id,
        plugin_revision="b" * 64,
        app_id=app_id,
        account_key_hash="c" * 64,
        connection_revision="d" * 64,
        capability=capability,
        title=title,
        description=title,
        input_schema=input_schema,
        input_schema_sha256=sha256_hex(canonical_bytes(input_schema)),
        output_schema=output_schema,
        output_schema_sha256=sha256_hex(canonical_bytes(output_schema)),
        risk_class=risk,
        effect=effect,
        requires_revision=False,
        max_result_bytes=4096,
        allowed_mime_types=("application/json",),
    )


@dataclass
class _Runtime:
    seen: list[Any]

    def invoke_tool(self, request):
        self.seen.append(request)
        return SimpleNamespace(
            data={"item": "standup"},
            to_dict=lambda: {
                "plugin_id": request.plugin_id,
                "app_id": request.app_id,
                "capability": request.capability,
                "status": "completed",
                "data": {"item": "standup"},
            },
        )


class _Service:
    def __init__(self, binding: PluginToolBinding) -> None:
        self.binding = binding
        self.plugin_runtime = _Runtime([])

    def plugin_tool_bindings(self) -> tuple[PluginToolBinding, ...]:
        return (self.binding,)


def test_plugin_registry_entries_are_namespaced_and_signed(monkeypatch) -> None:
    binding = _binding()
    monkeypatch.setattr(
        "mcp_platform.plugin_dispatch.plugin_tool_bindings",
        lambda *, service=None: (binding,),
    )
    entries = plugin_tool_registry_entries(service=object())
    name = canonical_plugin_tool_name("northwind.calendar", "list_events")
    assert list(entries) == [name]
    assert entries[name]["plugin_id"] == "northwind.calendar"
    assert entries[name]["app_id"] == "northwind.calendar.mcp"
    assert entries[name]["input_schema_sha256"] == binding.input_schema_sha256


def test_invoke_plugin_tool_is_retired_and_cannot_pin_a_second_invoker() -> None:
    binding = _binding()
    service = _Service(binding)
    with pytest.raises(PluginRuntimeError, match="plugin_tool_requires_plugin_operation"):
        invoke_plugin_tool(
            binding.tool_name,
            {"start": "tomorrow"},
            service=service,
        )
    assert service.plugin_runtime.seen == []


def test_unfamiliar_plugin_language_selects_generic_read_candidates(monkeypatch) -> None:
    binding = _binding()
    monkeypatch.setattr(
        "mcp_platform.plugin_dispatch.plugin_tool_bindings",
        lambda *, service=None: (binding,),
    )
    selected = resolve_plugin_tool_request("show tomorrow's northwind calendar events")
    assert selected is not None
    name, arguments = selected
    assert name == canonical_plugin_tool_name("northwind.calendar", "list_events")
    assert arguments == {"start": "tomorrow"}
    assert "fullText" not in str(arguments)
    retrieved = retrieve_plugin_candidates(
        "show tomorrow's northwind calendar events",
        (binding,),
    )
    assert retrieved == (binding,)


def test_google_drive_query_grammar_is_not_inferred_from_prose(monkeypatch) -> None:
    binding = _binding()
    monkeypatch.setattr(
        "mcp_platform.plugin_dispatch.plugin_tool_bindings",
        lambda *, service=None: (binding,),
    )
    selected = resolve_plugin_tool_request("Find the quarterly plan in Google Drive")
    assert selected is None or "fullText contains" not in str(selected[1])

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from mcp_platform.plugin_catalog import compile_plugin_tool_bindings
from mcp_platform.capabilities import project_declared_capabilities
from mcp_platform.plugin_connections import PluginConnectionService, _record_requirement
from mcp_platform.plugin_lifecycle import PluginLifecycleService
from plugin_platform.contracts import CapabilityKind, CapabilityRequirement
from tests.mcp_platform.test_plugin_catalog import (
    ACCOUNT_KEY_HASH,
    INPUT_DIGEST,
    _app,
    _app_snapshot,
    _descriptor,
    _plugin,
    _plugin_snapshot,
)


def test_shared_app_dependencies_keep_each_plugins_signed_capability_subset() -> None:
    app = replace(
        _app(),
        capabilities=(
            CapabilityRequirement(CapabilityKind.TOOL, "search", INPUT_DIGEST, None),
            CapabilityRequirement(CapabilityKind.TOOL, "summarize", INPUT_DIGEST, None),
        ),
    )
    left = _plugin()
    right = replace(
        _plugin(),
        plugin_id="example.second",
        versions=(replace(_plugin().versions[0], package_sha256="8" * 64),),
    )
    base = _descriptor(
        declared_capabilities={
            "tool:search": INPUT_DIGEST,
            "tool:summarize": INPUT_DIGEST,
        },
        capability_revision=project_declared_capabilities(
            _record_requirement(app)
        ).snapshot.capability_revision,
    )
    search = base.signed_capability_authority["tools"]["search"]
    descriptors = {
        left.plugin_id: replace(
            base,
            signed_capability_authority={
                "tools": {"search": search},
                "resources": {},
                "resource_templates": {},
                "prompts": {},
            },
        ),
        right.plugin_id: replace(
            base,
            signed_capability_authority={
                "tools": {"summarize": search},
                "resources": {},
                "resource_templates": {},
                "prompts": {},
            },
        ),
    }

    bindings = compile_plugin_tool_bindings(
        plugin_snapshot=_plugin_snapshot(left, right),
        app_snapshot=_app_snapshot(app),
        descriptor_for_app=lambda plugin_id, app_id: descriptors[plugin_id]
        if app_id == app.app_id
        else None,
        account_key_hash=ACCOUNT_KEY_HASH,
    )

    assert {(item.plugin_id, item.capability) for item in bindings} == {
        ("example.plugin", "search"),
        ("example.second", "summarize"),
    }


def test_shared_app_requirement_lookup_stays_partitioned_by_plugin_id() -> None:
    app = _app()
    requirement = _record_requirement(app)
    verified = {
        plugin_id: SimpleNamespace(
            manifest=SimpleNamespace(
                required_apps=(requirement,),
                optional_apps=(),
            )
        )
        for plugin_id in ("example.plugin", "example.second")
    }
    registry = SimpleNamespace(
        reverify_installed=lambda plugin_id: verified[plugin_id],
    )
    lifecycle = object.__new__(PluginLifecycleService)
    lifecycle.plugin_registry = registry
    lifecycle.app_registry = SimpleNamespace(get=lambda app_id: app)
    lifecycle._installed_verified = lambda plugin_id: verified[plugin_id]
    lifecycle.plugin_id_for_app = lambda _app_id: (_ for _ in ()).throw(
        AssertionError("shared app ownership must not be inverted")
    )
    connections = object.__new__(PluginConnectionService)
    connections._plugin_registry = registry
    connections._requirement_provider = lifecycle._connection_requirement

    assert connections._verified_requirement("example.plugin", app) == requirement
    assert connections._verified_requirement("example.second", app) == requirement

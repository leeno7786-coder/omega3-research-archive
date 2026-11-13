from __future__ import annotations

import ast
import itertools
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path

import pytest

from mcp_platform.canonical import canonical_bytes, sha256_hex
from plugin_platform.app_projection import (
    MAX_APP_ONLY_DESCRIPTION_CHARS,
    MAX_APP_ONLY_DISPLAY_NAME_CHARS,
    AppOnlyPluginProjection,
    derive_app_only_plugin,
)
from plugin_platform.contracts import (
    PLUGIN_PROTOCOL_VERSION,
    AppDeployment,
    CapabilityKind,
    McpTransport,
    PluginAuthentication,
    PluginKind,
)
from plugin_platform.dependencies import AppCapabilitySnapshot, NamedCapability


def named_capability(
    *,
    name: str = "calendar.list",
    schema_sha256: str | None = None,
    version: str | None = None,
    description: str | None = "List calendar events.",
) -> NamedCapability:
    return NamedCapability(
        name=name,
        schema_sha256=schema_sha256,
        version=version,
        description=description,
    )


def snapshot(
    *,
    app_id: str = "com.example.workspace",
    publisher_id: str = "example",
    deployment: AppDeployment = AppDeployment.LOCAL,
    provider_id: str | None = "workspace",
    protocol_version: str = PLUGIN_PROTOCOL_VERSION,
    transports: tuple[McpTransport, ...] = (McpTransport.STDIO,),
    remote_origin: str | None = None,
    authorization_issuer: str | None = None,
    tools: tuple[NamedCapability, ...] = (),
    prompts: tuple[NamedCapability, ...] = (),
    resources: tuple[NamedCapability, ...] = (),
    extensions: tuple[NamedCapability, ...] = (),
    capability_revision: str = "c" * 64,
    installed: bool = True,
    trusted: bool = True,
    enabled: bool = False,
) -> AppCapabilitySnapshot:
    return AppCapabilitySnapshot(
        app_id=app_id,
        publisher_id=publisher_id,
        deployment=deployment,
        provider_id=provider_id,
        protocol_version=protocol_version,
        transports=transports,
        remote_origin=remote_origin,
        authorization_issuer=authorization_issuer,
        authentication=PluginAuthentication.from_dict(
            {"kind": "none", "scopes": [], "secret_fields": []}
        ),
        tools=tools,
        prompts=prompts,
        resources=resources,
        extensions=extensions,
        capability_revision=capability_revision,
        installed=installed,
        trusted=trusted,
        enabled=enabled,
    )


def guided_snapshot(**overrides: object) -> AppCapabilitySnapshot:
    values: dict[str, object] = {
        "tools": (
            named_capability(
                name="calendar.list",
                schema_sha256="a" * 64,
                description="List calendar events.",
            ),
            named_capability(
                name="calendar.create",
                schema_sha256="b" * 64,
                description="Create calendar events.",
            ),
        ),
        "prompts": (
            named_capability(
                name="get_started",
                description="Guide workspace setup.",
            ),
        ),
        "resources": (
            named_capability(
                name="workspace.help",
                description="Describe available workspace resources.",
            ),
        ),
        "extensions": (
            named_capability(
                name="io.modelcontextprotocol/tasks",
                description=None,
            ),
        ),
    }
    values.update(overrides)
    return snapshot(**values)  # type: ignore[arg-type]


def remote_guided_snapshot(**overrides: object) -> AppCapabilitySnapshot:
    values: dict[str, object] = {
        "deployment": AppDeployment.REMOTE,
        "provider_id": None,
        "transports": (McpTransport.STREAMABLE_HTTP,),
        "remote_origin": "https://workspace.example",
        "authorization_issuer": "https://auth.workspace.example/oidc",
    }
    values.update(overrides)
    return guided_snapshot(**values)


def projection(app: AppCapabilitySnapshot | None = None) -> AppOnlyPluginProjection:
    return derive_app_only_plugin(
        app or guided_snapshot(),
        display_name="Example Workspace",
        description="Workspace tools and resources.",
    )


def forged_snapshot(
    source: AppCapabilitySnapshot,
    **changes: object,
) -> AppCapabilitySnapshot:
    forged = object.__new__(AppCapabilitySnapshot)
    for field in fields(AppCapabilitySnapshot):
        object.__setattr__(
            forged,
            field.name,
            changes.get(field.name, getattr(source, field.name)),
        )
    return forged


def forged_capability(
    source: NamedCapability,
    **changes: object,
) -> NamedCapability:
    forged = object.__new__(NamedCapability)
    for field in fields(NamedCapability):
        object.__setattr__(
            forged,
            field.name,
            changes.get(field.name, getattr(source, field.name)),
        )
    return forged


def test_derivation_pins_canonical_id_revision_and_visible_shell() -> None:
    app = guided_snapshot()
    expected_identity_digest = sha256_hex(
        canonical_bytes(
            {
                "app_id": app.app_id,
                "publisher_id": app.publisher_id,
                "deployment": app.deployment.value,
                "provider_id": app.provider_id,
                "remote_origin": app.remote_origin,
                "authorization_issuer": app.authorization_issuer,
            }
        )
    )
    expected_plugin_id = f"omega.generated.mcp.{expected_identity_digest[:24]}"
    expected_revision = sha256_hex(
        canonical_bytes(
            {
                "plugin_id": expected_plugin_id,
                "capability_revision": app.capability_revision,
                "display_name": "Example Workspace",
                "description": "Workspace tools and resources.",
            }
        )
    )

    result = projection(app)

    assert result.plugin_id == expected_plugin_id
    assert result.revision == expected_revision
    assert result.kind is PluginKind.APP_ONLY
    assert result.starter_request == "Help me get started with Example Workspace."
    assert result.generated is True
    assert result.enabled is False
    assert result.skill_assets == ()


def test_capability_input_order_cannot_change_projection_identity_revision_or_shape() -> None:
    original = guided_snapshot()
    reversed_input = guided_snapshot(
        tools=tuple(reversed(original.tools)),
        prompts=tuple(reversed(original.prompts)),
        resources=tuple(reversed(original.resources)),
        extensions=tuple(reversed(original.extensions)),
    )

    first = projection(original)
    second = projection(reversed_input)

    assert first == second
    assert tuple(item.name for item in first.tools) == (
        "calendar.create",
        "calendar.list",
    )
    assert first.required_apps[0].transports == (McpTransport.STDIO,)


def test_each_trusted_identity_change_changes_plugin_id_and_revision() -> None:
    apps = (
        guided_snapshot(),
        guided_snapshot(app_id="com.example.workspace-two"),
        guided_snapshot(publisher_id="example-two"),
        guided_snapshot(provider_id="workspace-two"),
        remote_guided_snapshot(),
        remote_guided_snapshot(remote_origin="https://workspace-two.example"),
        remote_guided_snapshot(authorization_issuer="https://auth.workspace.example/other"),
    )
    results = tuple(projection(app) for app in apps)

    assert len({item.plugin_id for item in results}) == len(results)
    assert len({item.revision for item in results}) == len(results)


@pytest.mark.parametrize(
    "change",
    [
        {"capability_revision": "d" * 64},
        {"display_name": "Example Calendar"},
        {"description": "Calendar tools and resources."},
    ],
)
def test_revision_changes_for_capability_revision_or_visible_summary(
    change: dict[str, str],
) -> None:
    app = guided_snapshot(capability_revision=change.get("capability_revision", "c" * 64))

    changed = derive_app_only_plugin(
        app,
        display_name=change.get("display_name", "Example Workspace"),
        description=change.get("description", "Workspace tools and resources."),
    )

    assert changed.plugin_id == projection().plugin_id
    assert changed.revision != projection().revision


def test_projection_has_exactly_one_required_app_with_only_canonical_capabilities() -> None:
    app = guided_snapshot()

    result = projection(app)

    assert len(result.required_apps) == 1
    dependency = result.required_apps[0]
    assert dependency.app_id == app.app_id
    assert dependency.publisher_id == app.publisher_id
    assert dependency.deployment is app.deployment
    assert dependency.provider_id == app.provider_id
    assert dependency.protocol_version == PLUGIN_PROTOCOL_VERSION
    assert dependency.transports == app.transports
    assert dependency.remote_origin == app.remote_origin
    assert dependency.authorization_issuer == app.authorization_issuer
    assert tuple(
        (item.kind, item.name, item.schema_sha256, item.version) for item in dependency.capabilities
    ) == (
        (CapabilityKind.TOOL, "calendar.create", "b" * 64, None),
        (CapabilityKind.TOOL, "calendar.list", "a" * 64, None),
        (CapabilityKind.PROMPT, "get_started", None, None),
        (CapabilityKind.RESOURCE, "workspace.help", None, None),
        (
            CapabilityKind.EXTENSION,
            "io.modelcontextprotocol/tasks",
            None,
            None,
        ),
    )


def test_projection_preserves_only_screened_capability_summaries_not_authority() -> None:
    result = projection()
    field_names = {field.name for field in fields(result)}
    forbidden_field_fragments = {
        "account",
        "authority",
        "credential",
        "executable",
        "oauth",
        "resource_content",
        "token",
    }

    assert not any(
        fragment in field_name
        for field_name in field_names
        for fragment in forbidden_field_fragments
    )
    assert result.skill_assets == ()
    assert tuple(item.description for item in result.prompts) == ("Guide workspace setup.",)
    assert tuple(item.description for item in result.resources) == (
        "Describe available workspace resources.",
    )
    assert all(type(item) is NamedCapability for item in result.capabilities)


def test_installed_trusted_dormant_app_is_admitted_and_projection_stays_dormant() -> None:
    dormant = projection(guided_snapshot(enabled=False))
    previously_enabled = projection(guided_snapshot(enabled=True))

    assert dormant.enabled is False
    assert previously_enabled.enabled is False
    assert dormant == previously_enabled


@pytest.mark.parametrize(
    "changes",
    [
        {"installed": False},
        {"trusted": False},
    ],
)
def test_uninstalled_or_untrusted_app_is_rejected_content_free(
    changes: dict[str, bool],
) -> None:
    with pytest.raises(ValueError, match="app-only projection app is ineligible") as raised:
        projection(guided_snapshot(**changes))

    assert "com.example.workspace" not in str(raised.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"protocol_version": "2025-11-25"},
        {"transports": ()},
        {"transports": ("bogus",)},
        {"deployment": "remote"},
        {"remote_origin": "https://Workspace.example"},
        {"authorization_issuer": "http://auth.workspace.example"},
    ],
)
def test_forged_protocol_transport_or_deployment_identity_is_rejected(
    changes: dict[str, object],
) -> None:
    app = forged_snapshot(guided_snapshot(), **changes)

    with pytest.raises(ValueError, match="app-only projection app is invalid") as raised:
        projection(app)

    assert "https://" not in str(raised.value)


@pytest.mark.parametrize(
    "field",
    ["tools", "prompts", "resources", "extensions"],
)
def test_duplicate_capability_identity_is_rejected_at_projection_boundary(
    field: str,
) -> None:
    app = guided_snapshot()
    duplicated = getattr(app, field)
    if not duplicated:
        pytest.fail("fixture must carry every capability kind")
    forged = forged_snapshot(app, **{field: (duplicated[0], duplicated[0])})

    with pytest.raises(ValueError, match="app-only projection app is invalid"):
        projection(forged)


def test_projection_rejects_missing_or_unscreened_guidance() -> None:
    with pytest.raises(ValueError, match="app-only projection guidance is insufficient"):
        projection(snapshot())

    valid = named_capability()
    unscreened = forged_capability(valid, description="")
    forged = forged_snapshot(snapshot(tools=(valid,)), tools=(unscreened,))
    with pytest.raises(ValueError, match="app-only projection app is invalid"):
        projection(forged)


@pytest.mark.parametrize(
    "display_name",
    [
        "",
        " leading",
        "trailing ",
        "line\nbreak",
        "tab\ttext",
        "e\u0301",
        "hidden\u0085control",
        "hidden\u202edirection",
        "hidden\ue000private",
        "x" * (MAX_APP_ONLY_DISPLAY_NAME_CHARS + 1),
    ],
)
def test_display_name_is_bounded_screened_plain_text(display_name: str) -> None:
    with pytest.raises(ValueError, match="app-only projection display name is invalid"):
        derive_app_only_plugin(
            guided_snapshot(),
            display_name=display_name,
            description="Workspace tools and resources.",
        )


@pytest.mark.parametrize(
    "description",
    [
        "",
        " leading",
        "trailing ",
        "line\nbreak",
        "tab\ttext",
        "e\u0301",
        "hidden\u0085control",
        "hidden\u202edirection",
        "hidden\ue000private",
        "x" * (MAX_APP_ONLY_DESCRIPTION_CHARS + 1),
    ],
)
def test_visible_description_is_bounded_screened_plain_text(description: str) -> None:
    with pytest.raises(ValueError, match="app-only projection description is invalid"):
        derive_app_only_plugin(
            guided_snapshot(),
            display_name="Example Workspace",
            description=description,
        )


def test_visible_text_exact_limits_are_admitted_and_starter_is_derived() -> None:
    display_name = "N" * MAX_APP_ONLY_DISPLAY_NAME_CHARS
    description = "D" * MAX_APP_ONLY_DESCRIPTION_CHARS

    result = derive_app_only_plugin(
        guided_snapshot(),
        display_name=display_name,
        description=description,
    )

    assert result.display_name == display_name
    assert result.description == description
    assert result.starter_request == f"Help me get started with {display_name}."


def test_projection_and_nested_collections_are_deeply_immutable() -> None:
    result = projection()

    with pytest.raises(FrozenInstanceError):
        result.generated = False  # type: ignore[misc]
    with pytest.raises(TypeError):
        result.tools[0] = named_capability()  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        result.tools[0].description = "Changed."  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    [
        {"plugin_id": "omega.generated.mcp." + "0" * 24},
        {"revision": "0" * 64},
        {"kind": PluginKind.MCP_BACKED},
        {"required_apps": ()},
        {"required_apps": []},
        {"capability_revision": "NOT-A-DIGEST"},
        {"starter_request": "Set up this app silently."},
        {"generated": False},
        {"enabled": True},
        {"skill_assets": ("skill.md",)},
        {"tools": []},
        {"tools": (object(),)},
    ],
)
def test_direct_constructor_rejects_forged_or_mutable_projection_shapes(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="app-only projection is invalid"):
        replace(projection(), **changes)


def test_direct_constructor_rejects_capability_dependency_drift() -> None:
    result = projection()
    changed_tool = named_capability(
        name="calendar.delete",
        description="Delete calendar events.",
    )

    with pytest.raises(ValueError, match="app-only projection is invalid"):
        replace(result, tools=(changed_tool,))


def test_direct_constructor_canonicalizes_valid_capability_order() -> None:
    result = projection()

    reconstructed = replace(result, tools=tuple(reversed(result.tools)))

    assert reconstructed == result
    assert tuple(item.name for item in reconstructed.tools) == (
        "calendar.create",
        "calendar.list",
    )


def test_projection_is_isolated_from_caller_collections() -> None:
    source = [named_capability()]
    app = snapshot(tools=tuple(source))
    result = projection(app)

    source.append(named_capability(name="calendar.create"))

    assert tuple(item.name for item in result.tools) == ("calendar.list",)
    assert tuple(item.name for item in result.required_apps[0].capabilities) == ("calendar.list",)


def test_projection_module_has_no_io_process_environment_or_nondeterminism_imports() -> None:
    source_path = Path("plugin_platform/app_projection.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    forbidden = {
        "asyncio",
        "datetime",
        "http",
        "logging",
        "multiprocessing",
        "os",
        "pathlib",
        "random",
        "requests",
        "secrets",
        "socket",
        "subprocess",
        "time",
        "urllib",
    }
    imported: set[str] = set()
    calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", maxsplit=1)[0])
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.add(node.func.attr)

    assert imported.isdisjoint(forbidden)
    assert calls.isdisjoint({"open", "print", "exec", "eval", "compile", "__import__"})


def test_projection_is_deterministic_across_all_capability_tuple_permutations() -> None:
    app = guided_snapshot()
    outcomes: list[AppOnlyPluginProjection] = []
    for tools in itertools.permutations(app.tools):
        outcomes.append(projection(guided_snapshot(tools=tools)))

    assert outcomes
    assert all(item == outcomes[0] for item in outcomes)

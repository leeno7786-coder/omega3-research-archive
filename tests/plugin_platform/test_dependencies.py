from __future__ import annotations

import ast
import itertools
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from plugin_platform.contracts import (
    PLUGIN_PROTOCOL_VERSION,
    AppDeployment,
    AppRequirement,
    CapabilityKind,
    McpTransport,
    PluginAuthentication,
    PluginManifest,
)
from plugin_platform.dependencies import (
    AppCapabilitySnapshot,
    DependencyBlocker,
    DependencyResolution,
    NamedCapability,
    ResolvedAppDependency,
    resolve_dependencies,
)


def capability_requirement(
    *,
    kind: str = "tool",
    name: str = "calendar.list",
    schema_sha256: str | None = None,
    version: str | None = None,
) -> dict[str, object]:
    return {
        "kind": kind,
        "name": name,
        "schema_sha256": schema_sha256,
        "version": version,
    }


def app_requirement(
    *,
    app_id: str = "com.example.workspace",
    publisher_id: str = "example",
    deployment: str = "local",
    provider_id: str | None = "workspace",
    protocol_version: str = PLUGIN_PROTOCOL_VERSION,
    transports: list[str] | None = None,
    remote_origin: str | None = None,
    authorization_issuer: str | None = None,
    authentication: dict[str, object] | None = None,
    capabilities: list[dict[str, object]] | None = None,
) -> AppRequirement:
    return AppRequirement.from_dict(
        {
            "app_id": app_id,
            "publisher_id": publisher_id,
            "deployment": deployment,
            "provider_id": provider_id,
            "protocol_version": protocol_version,
            "transports": transports or ["stdio"],
            "remote_origin": remote_origin,
            "authorization_issuer": authorization_issuer,
            "authentication": authentication
            or {"kind": "none", "scopes": [], "secret_fields": []},
            "capabilities": capabilities
            if capabilities is not None
            else [capability_requirement()],
        }
    )


def manifest(
    *,
    required_apps: tuple[AppRequirement, ...],
    optional_apps: tuple[AppRequirement, ...] = (),
) -> PluginManifest:
    kind = "mcp_backed" if len(required_apps) == 1 else "composite"
    return PluginManifest.from_dict(
        {
            "manifest_version": 2,
            "plugin_id": "omega.productivity.workspace",
            "publisher_id": "omega",
            "plugin_version": "1.0.0",
            "minimum_omega_version": "3.0.0",
            "kind": kind,
            "title": "Workspace",
            "description": "Workspace workflows.",
            "assets": [],
            "entry_points": [],
            "starter_requests": [],
            "required_apps": [item.to_dict() for item in required_apps],
            "optional_apps": [item.to_dict() for item in optional_apps],
            "schedule_template_paths": [],
            "proactive_event_rules": [],
            "enterprise_roles": [],
            "signing_key_id": "omega-plugin-release-1",
            "signature": "signed",
        }
    )


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
    authentication: PluginAuthentication | None = None,
    tools: tuple[NamedCapability, ...] | None = None,
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
        authentication=authentication
        or PluginAuthentication.from_dict(
            {"kind": "none", "scopes": [], "secret_fields": []}
        ),
        tools=tools if tools is not None else (named_capability(),),
        prompts=prompts,
        resources=resources,
        extensions=extensions,
        capability_revision=capability_revision,
        installed=installed,
        trusted=trusted,
        enabled=enabled,
    )


def test_app_capability_snapshot_retains_signed_authentication() -> None:
    authentication = app_requirement(
        authentication={
            "kind": "oauth_authorization_code_pkce",
            "scopes": ["workspace.read"],
            "secret_fields": [],
        }
    ).authentication

    observed = snapshot(authentication=authentication)

    assert observed.authentication == authentication
    assert observed.authentication.to_dict() == {
        "kind": "oauth_authorization_code_pkce",
        "scopes": ["workspace.read"],
        "secret_fields": [],
    }


def remote_requirement(**overrides: object) -> AppRequirement:
    values: dict[str, object] = {
        "deployment": "remote",
        "provider_id": None,
        "transports": ["streamable_http"],
        "remote_origin": "https://workspace.example",
        "authorization_issuer": "https://auth.workspace.example/oidc",
    }
    values.update(overrides)
    return app_requirement(**values)  # type: ignore[arg-type]


def remote_snapshot(**overrides: object) -> AppCapabilitySnapshot:
    values: dict[str, object] = {
        "deployment": AppDeployment.REMOTE,
        "provider_id": None,
        "transports": (McpTransport.STREAMABLE_HTTP,),
        "remote_origin": "https://workspace.example",
        "authorization_issuer": "https://auth.workspace.example/oidc",
    }
    values.update(overrides)
    return snapshot(**values)  # type: ignore[arg-type]


def blocker_reasons(resolution: DependencyResolution) -> tuple[str, ...]:
    return tuple(item.reason for item in resolution.blockers)


def test_exact_dependency_resolution_is_deeply_immutable_and_admits_dormant_app() -> None:
    required = app_requirement(
        transports=["stdio", "streamable_http"],
        capabilities=[
            capability_requirement(),
            capability_requirement(kind="prompt", name="get_started"),
            capability_requirement(
                kind="extension",
                name="io.modelcontextprotocol/tasks",
                version=None,
            ),
        ],
    )
    app = snapshot(
        transports=(McpTransport.STDIO, McpTransport.STREAMABLE_HTTP),
        prompts=(named_capability(name="get_started", description="Guide setup."),),
        extensions=(
            named_capability(
                name="io.modelcontextprotocol/tasks",
                version=None,
                description=None,
            ),
        ),
        enabled=False,
    )
    expected_requirement = app_requirement(
        transports=["stdio", "streamable_http"],
        capabilities=[
            capability_requirement(
                kind="extension",
                name="io.modelcontextprotocol/tasks",
                version=None,
            ),
            capability_requirement(kind="prompt", name="get_started"),
            capability_requirement(),
        ],
    )

    result = resolve_dependencies(manifest(required_apps=(required,)), (app,))

    assert result == DependencyResolution(
        required=(ResolvedAppDependency(requirement=expected_requirement, app=app),),
        optional=(),
        blockers=(),
        optional_blockers=(),
        ready=True,
    )
    with pytest.raises(FrozenInstanceError):
        result.ready = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.required[0].app = snapshot()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        app.tools[0].description = "changed"  # type: ignore[misc]


def test_versionless_tasks_snapshot_is_closed_and_other_extensions_stay_versioned() -> None:
    tasks = named_capability(
        name="io.modelcontextprotocol/tasks",
        version=None,
        description=None,
    )
    app = snapshot(tools=(), extensions=(tasks,))

    assert app.extensions == (tasks,)
    assert app.extensions[0].description is None
    with pytest.raises(ValueError):
        named_capability(
            name="io.modelcontextprotocol/tasks",
            version="1.0.0",
            description=None,
        )
    with pytest.raises(ValueError):
        named_capability(name="vendor.extension/task-v1", version=None, description=None)
    object.__setattr__(tasks, "version", "1.0.0")
    with pytest.raises(ValueError):
        snapshot(tools=(), extensions=(tasks,))


def test_optional_absence_is_reported_without_blocking_readiness() -> None:
    required = app_requirement()
    optional = app_requirement(app_id="com.example.optional", provider_id="optional")

    result = resolve_dependencies(
        manifest(required_apps=(required,), optional_apps=(optional,)),
        (snapshot(),),
    )

    assert result.ready is True
    assert result.blockers == ()
    assert result.optional == ()
    assert result.optional_blockers == (
        DependencyBlocker(
            app_id="com.example.optional",
            capability_kind=None,
            capability_name=None,
            reason="app_absent",
        ),
    )


def test_required_absence_blocks_readiness() -> None:
    result = resolve_dependencies(manifest(required_apps=(app_requirement(),)), ())

    assert result.ready is False
    assert blocker_reasons(result) == ("app_absent",)


@pytest.mark.parametrize(
    ("changed", "reason"),
    [
        ({"publisher_id": "different"}, "publisher_mismatch"),
        ({"provider_id": "different"}, "provider_mismatch"),
        ({"transports": (McpTransport.STREAMABLE_HTTP,)}, "transport_mismatch"),
        ({"installed": False}, "app_uninstalled"),
        ({"trusted": False}, "app_untrusted"),
        (
            {
                "authentication": PluginAuthentication.from_dict(
                    {
                        "kind": "oauth_device_code",
                        "scopes": ["workspace.read"],
                        "secret_fields": [],
                    }
                )
            },
            "authentication_mismatch",
        ),
    ],
)
def test_each_incompatible_app_predicate_returns_a_content_free_blocker(
    changed: dict[str, object], reason: str
) -> None:
    app = snapshot(**changed)

    result = resolve_dependencies(manifest(required_apps=(app_requirement(),)), (app,))

    assert reason in blocker_reasons(result)
    assert result.ready is False


def test_valid_local_snapshot_does_not_satisfy_remote_deployment_requirement() -> None:
    result = resolve_dependencies(
        manifest(required_apps=(remote_requirement(),)),
        (snapshot(),),
    )

    assert "deployment_mismatch" in blocker_reasons(result)


def test_snapshot_rejects_noncurrent_protocol_before_matching() -> None:
    with pytest.raises(ValueError, match="plugin app protocol is invalid"):
        snapshot(protocol_version="2025-11-25")


@pytest.mark.parametrize(
    ("requirement_change", "snapshot_change", "reason"),
    [
        ({"remote_origin": "https://other.example"}, {}, "origin_mismatch"),
        (
            {"authorization_issuer": "https://auth.other.example/oidc"},
            {},
            "issuer_mismatch",
        ),
        ({}, {"remote_origin": "https://other.example"}, "origin_mismatch"),
        (
            {},
            {"authorization_issuer": "https://auth.other.example/oidc"},
            "issuer_mismatch",
        ),
    ],
)
def test_remote_origin_and_issuer_are_exact_identity_predicates(
    requirement_change: dict[str, object],
    snapshot_change: dict[str, object],
    reason: str,
) -> None:
    required = remote_requirement(**requirement_change)
    app = remote_snapshot(**snapshot_change)

    result = resolve_dependencies(manifest(required_apps=(required,)), (app,))

    assert reason in blocker_reasons(result)


@pytest.mark.parametrize(
    "changes",
    [
        {"remote_origin": "https://workspace.example"},
        {"authorization_issuer": "https://auth.workspace.example/oidc"},
    ],
)
def test_local_snapshots_reject_remote_identity_fields(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="local plugin app remote identity is invalid"):
        snapshot(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"remote_origin": None},
        {"authorization_issuer": None},
        {"remote_origin": "http://workspace.example"},
        {"remote_origin": "https://Workspace.example"},
        {"remote_origin": "https://workspace.example:443"},
        {"authorization_issuer": "https://auth.workspace.example/%7Eoidc"},
        {"transports": (McpTransport.STDIO,)},
    ],
)
def test_remote_snapshot_validation_has_task1_canonical_https_parity(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        remote_snapshot(**changes)


@pytest.mark.parametrize(
    ("requirement_capability", "app_changes", "reasons"),
    [
        (
            capability_requirement(name="calendar.create"),
            {},
            ("capability_missing",),
        ),
        (
            capability_requirement(schema_sha256="a" * 64),
            {"tools": (named_capability(schema_sha256="b" * 64),)},
            ("schema_mismatch",),
        ),
        (
            capability_requirement(
                kind="extension",
                name="vendor.extension/task-v1",
                version="1.0.0",
            ),
            {
                "tools": (),
                "extensions": (
                    named_capability(
                        name="vendor.extension/task-v1",
                        version="2.0.0",
                    ),
                ),
            },
            ("version_mismatch",),
        ),
    ],
)
def test_capability_name_schema_and_extension_version_match_exactly(
    requirement_capability: dict[str, object],
    app_changes: dict[str, object],
    reasons: tuple[str, ...],
) -> None:
    required = app_requirement(capabilities=[requirement_capability])

    result = resolve_dependencies(
        manifest(required_apps=(required,)),
        (snapshot(**app_changes),),
    )

    assert blocker_reasons(result) == reasons


def test_all_identity_and_capability_blockers_are_returned() -> None:
    required = app_requirement(
        publisher_id="required-publisher",
        provider_id="required-provider",
        transports=["streamable_http"],
        capabilities=[
            capability_requirement(name="calendar.missing"),
            capability_requirement(name="calendar.list", schema_sha256="a" * 64),
        ],
    )
    app = snapshot(
        publisher_id="actual-publisher",
        provider_id="actual-provider",
        transports=(McpTransport.STDIO,),
        tools=(named_capability(schema_sha256="b" * 64),),
        installed=False,
        trusted=False,
    )

    result = resolve_dependencies(manifest(required_apps=(required,)), (app,))

    assert set(blocker_reasons(result)) == {
        "app_uninstalled",
        "app_untrusted",
        "capability_missing",
        "provider_mismatch",
        "publisher_mismatch",
        "schema_mismatch",
        "transport_mismatch",
    }


def test_duplicate_or_ambiguous_app_snapshots_fail_closed_without_first_match() -> None:
    required = app_requirement()
    matching = snapshot(capability_revision="a" * 64)
    incompatible = snapshot(capability_revision="b" * 64, trusted=False)

    first = resolve_dependencies(
        manifest(required_apps=(required,)),
        (matching, incompatible),
    )
    second = resolve_dependencies(
        manifest(required_apps=(required,)),
        (incompatible, matching),
    )

    assert first == second
    assert first.required == ()
    assert blocker_reasons(first) == ("app_ambiguous",)
    assert first.ready is False


@pytest.mark.parametrize("kind", ["tools", "prompts", "resources", "extensions"])
def test_snapshot_rejects_duplicate_same_kind_capability_names(kind: str) -> None:
    item = named_capability(
        name="vendor.extension/task-v1" if kind == "extensions" else "calendar.list",
        version="1.0.0" if kind == "extensions" else None,
    )
    values = {"tools": ()} if kind != "tools" else {}
    values[kind] = (item, item)

    with pytest.raises(ValueError, match="plugin capability identity is duplicated"):
        snapshot(**values)


@pytest.mark.parametrize(
    "field",
    ["transports", "tools", "prompts", "resources", "extensions"],
)
def test_snapshot_requires_exact_tuple_sequences(field: str) -> None:
    values: dict[str, object] = {"tools": ()} if field != "tools" else {}
    if field == "transports":
        values[field] = [McpTransport.STDIO]
    else:
        values[field] = []

    with pytest.raises(ValueError, match="must be a tuple"):
        snapshot(**values)


@pytest.mark.parametrize(
    "description",
    [
        "",
        " leading",
        "trailing ",
        "line\nbreak",
        "tab\ttext",
        "e\u0301",
        pytest.param("hidden\u0085control", id="c1-control"),
        pytest.param("hidden\u202edirection", id="bidi-format"),
        pytest.param("hidden\ue000private", id="private-use"),
        "x" * 1025,
    ],
)
def test_named_capability_screens_plain_text_descriptions(description: str) -> None:
    marker = "https://secret.example/?credential=DO-NOT-LEAK"

    with pytest.raises(ValueError) as raised:
        named_capability(description=description)

    assert marker not in str(raised.value)


def test_snapshot_and_resolution_are_isolated_from_caller_sequence_mutation() -> None:
    source = [named_capability(description="Original description.")]
    app = snapshot(tools=tuple(source))
    available = [app]
    result = resolve_dependencies(manifest(required_apps=(app_requirement(),)), available)

    source.append(named_capability(name="calendar.create"))
    available.clear()

    assert tuple(item.name for item in app.tools) == ("calendar.list",)
    assert result.required[0].app.tools[0].description == "Original description."


def test_ordering_is_deterministic_across_manifest_app_and_mapping_permutations() -> None:
    requirements = (
        app_requirement(app_id="com.example.zeta", provider_id="zeta"),
        app_requirement(app_id="com.example.alpha", provider_id="alpha"),
    )
    apps = (
        snapshot(app_id="com.example.zeta", provider_id="zeta", capability_revision="f" * 64),
        snapshot(app_id="com.example.alpha", provider_id="alpha", capability_revision="a" * 64),
    )
    outcomes: list[DependencyResolution] = []

    for requirement_order in itertools.permutations(requirements):
        plugin = manifest(required_apps=requirement_order)
        for app_order in itertools.permutations(apps):
            outcomes.append(resolve_dependencies(plugin, app_order))
            outcomes.append(
                resolve_dependencies(
                    plugin,
                    {item.capability_revision: item for item in app_order},
                )
            )

    assert outcomes
    assert all(result == outcomes[0] for result in outcomes)
    assert tuple(item.app.app_id for item in outcomes[0].required) == (
        "com.example.alpha",
        "com.example.zeta",
    )


def test_nested_transport_and_capability_order_is_canonical() -> None:
    capability_documents = [
        capability_requirement(name="calendar.zeta"),
        capability_requirement(name="calendar.alpha"),
    ]
    required_forward = app_requirement(
        transports=["stdio", "streamable_http"],
        capabilities=capability_documents,
    )
    required_reverse = app_requirement(
        transports=["streamable_http", "stdio"],
        capabilities=list(reversed(capability_documents)),
    )
    capabilities = (
        named_capability(name="calendar.zeta"),
        named_capability(name="calendar.alpha"),
    )
    app_forward = snapshot(
        transports=(McpTransport.STDIO, McpTransport.STREAMABLE_HTTP),
        tools=capabilities,
    )
    app_reverse = snapshot(
        transports=(McpTransport.STREAMABLE_HTTP, McpTransport.STDIO),
        tools=tuple(reversed(capabilities)),
    )

    forward = resolve_dependencies(
        manifest(required_apps=(required_forward,)),
        (app_forward,),
    )
    reverse = resolve_dependencies(
        manifest(required_apps=(required_reverse,)),
        (app_reverse,),
    )

    assert forward == reverse
    assert forward.required[0].requirement.transports == (
        McpTransport.STDIO,
        McpTransport.STREAMABLE_HTTP,
    )
    assert tuple(item.name for item in forward.required[0].app.tools) == (
        "calendar.alpha",
        "calendar.zeta",
    )


@pytest.mark.parametrize(
    "blocker",
    [
        {
            "app_id": "com.example.workspace",
            "capability_kind": CapabilityKind.TOOL,
            "capability_name": "calendar.list",
            "reason": "publisher_mismatch",
        },
        {
            "app_id": "com.example.workspace",
            "capability_kind": None,
            "capability_name": None,
            "reason": "capability_missing",
        },
    ],
)
def test_blocker_shape_is_exact_for_identity_and_capability_reasons(
    blocker: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="dependency blocker is invalid"):
        DependencyBlocker(**blocker)  # type: ignore[arg-type]


def test_blockers_and_validation_failures_never_echo_sensitive_or_provider_content() -> None:
    secrets = (
        "SECRET-DESCRIPTION",
        "https://workspace.example",
        "https://auth.workspace.example/oidc",
        "https://other.example",
        "https://auth.other.example/oidc",
        "credential-DO-NOT-LEAK",
        "schema-DO-NOT-LEAK",
    )
    required = remote_requirement(
        remote_origin="https://other.example",
        authorization_issuer="https://auth.other.example/oidc",
        capabilities=[capability_requirement(name="calendar.missing", schema_sha256="a" * 64)],
    )
    app = remote_snapshot(
        tools=(
            named_capability(
                description="SECRET-DESCRIPTION credential-DO-NOT-LEAK schema-DO-NOT-LEAK"
            ),
        ),
    )

    result = resolve_dependencies(manifest(required_apps=(required,)), (app,))
    rendered = repr(result.blockers)

    assert result.blockers
    assert "https://" not in rendered
    assert all(secret not in rendered for secret in secrets)

    with pytest.raises(ValueError) as raised:
        remote_snapshot(remote_origin="https://Workspace.example/credential-DO-NOT-LEAK")
    assert all(secret not in str(raised.value) for secret in secrets)


def test_resolver_has_no_forbidden_io_process_environment_or_nondeterminism_imports() -> None:
    source_path = Path("plugin_platform/dependencies.py")
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


def test_enabled_optional_and_ambiguity_mutation_guards() -> None:
    required = app_requirement()
    optional = app_requirement(app_id="com.example.optional", provider_id="optional")
    dormant = snapshot(enabled=False)
    ambiguous_optional = (
        snapshot(
            app_id="com.example.optional", provider_id="optional", capability_revision="a" * 64
        ),
        snapshot(
            app_id="com.example.optional", provider_id="optional", capability_revision="b" * 64
        ),
    )

    result = resolve_dependencies(
        manifest(required_apps=(required,), optional_apps=(optional,)),
        (dormant, *ambiguous_optional),
    )

    assert result.ready is True
    assert tuple(item.reason for item in result.optional_blockers) == ("app_ambiguous",)
    assert result.required == (ResolvedAppDependency(requirement=required, app=dormant),)


def test_frozen_snapshot_cannot_be_rebound_after_validation() -> None:
    app = snapshot()

    with pytest.raises(FrozenInstanceError):
        app.trusted = False  # type: ignore[misc]
    with pytest.raises(TypeError):
        app.tools[0] = named_capability(name="calendar.create")  # type: ignore[index]


def test_replace_revalidates_snapshot_identity_and_nested_capabilities() -> None:
    app = snapshot()

    with pytest.raises(ValueError):
        replace(app, capability_revision="NOT-A-DIGEST")
    with pytest.raises(ValueError):
        replace(app, tools=(object(),))  # type: ignore[arg-type]


class _StringSubclass(str):
    pass


@pytest.mark.parametrize(
    "changes",
    [
        {"name": ["calendar.list"]},
        {"name": _StringSubclass("calendar.list")},
        {"name": "https://provider.example/tool"},
        {"schema_sha256": bytearray(b"a" * 64)},
        {"schema_sha256": "A" * 64},
        {"version": ["1.0.0"]},
        {"version": "1.0.0"},
        {"name": "io.modelcontextprotocol/tasks", "version": None},
        {"name": "io.modelcontextprotocol/tasks", "version": "01.0.0"},
    ],
)
def test_named_capability_direct_constructor_rejects_unvalidated_or_mutable_scalars(
    changes: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "name": "calendar.list",
        "schema_sha256": None,
        "version": None,
        "description": "List calendar events.",
    }
    values.update(changes)

    with pytest.raises(ValueError):
        NamedCapability(**values)  # type: ignore[arg-type]


def test_named_capability_direct_constructor_accepts_reviewed_task1_name_forms() -> None:
    tool = named_capability(schema_sha256="a" * 64)
    extension = named_capability(
        name="io.modelcontextprotocol/tasks",
        version=None,
        description=None,
    )

    assert tool.name == "calendar.list"
    assert tool.schema_sha256 == "a" * 64
    assert tool.version is None
    assert extension.name == "io.modelcontextprotocol/tasks"
    assert extension.version is None


@pytest.mark.parametrize(
    "app",
    [
        snapshot(app_id="com.example.other"),
        snapshot(publisher_id="wrong-publisher"),
        snapshot(trusted=False),
        snapshot(installed=False),
        snapshot(tools=()),
        snapshot(transports=(McpTransport.STREAMABLE_HTTP,)),
    ],
)
def test_resolved_dependency_direct_constructor_rejects_unsatisfied_pair(
    app: AppCapabilitySnapshot,
) -> None:
    with pytest.raises(ValueError, match="resolved app dependency is invalid"):
        ResolvedAppDependency(requirement=app_requirement(), app=app)


def _forged_resolved_dependency(
    requirement: AppRequirement,
    app: AppCapabilitySnapshot,
) -> ResolvedAppDependency:
    forged = object.__new__(ResolvedAppDependency)
    object.__setattr__(forged, "requirement", requirement)
    object.__setattr__(forged, "app", app)
    return forged


@pytest.mark.parametrize(
    "app",
    [snapshot(publisher_id="wrong-publisher"), snapshot(trusted=False)],
)
def test_ready_resolution_revalidates_forged_nested_dependency(
    app: AppCapabilitySnapshot,
) -> None:
    forged = _forged_resolved_dependency(app_requirement(), app)

    with pytest.raises(ValueError, match="dependency resolution is invalid"):
        DependencyResolution(
            required=(forged,),
            optional=(),
            blockers=(),
            optional_blockers=(),
            ready=True,
        )


@pytest.mark.parametrize(
    ("app_id", "kind", "name", "reason"),
    [
        ("https://apps.example", None, None, "app_absent"),
        ("com.example.workspace:credential", None, None, "app_untrusted"),
        ("credential=DO-NOT-LEAK", None, None, "app_ambiguous"),
        (
            "com.example.workspace",
            CapabilityKind.TOOL,
            "https://provider.example/tool?credential=DO-NOT-LEAK",
            "capability_missing",
        ),
        (
            "com.example.workspace",
            CapabilityKind.EXTENSION,
            "io.modelcontextprotocol/tasks/credential",
            "version_mismatch",
        ),
    ],
)
def test_blocker_direct_constructor_rejects_noncanonical_or_sensitive_identities(
    app_id: str,
    kind: CapabilityKind | None,
    name: str | None,
    reason: str,
) -> None:
    with pytest.raises(ValueError) as raised:
        DependencyBlocker(
            app_id=app_id,
            capability_kind=kind,
            capability_name=name,
            reason=reason,
        )

    rendered = str(raised.value)
    assert "https://" not in rendered
    assert "credential" not in rendered


def test_blocker_accepts_reviewed_namespaced_extension_identity() -> None:
    blocker = DependencyBlocker(
        app_id="com.example.workspace",
        capability_kind=CapabilityKind.EXTENSION,
        capability_name="io.modelcontextprotocol/tasks",
        reason="capability_missing",
    )

    assert blocker.capability_name == "io.modelcontextprotocol/tasks"


@pytest.mark.parametrize(
    ("kind", "name", "reason"),
    [
        (None, None, "app_absent"),
        (CapabilityKind.TOOL, "calendar.list", "capability_missing"),
    ],
    ids=("neither-for-identity", "both-for-capability"),
)
def test_blocker_presence_matrix_accepts_only_complete_reason_shapes(
    kind: CapabilityKind | None,
    name: str | None,
    reason: str,
) -> None:
    blocker = DependencyBlocker(
        app_id="com.example.workspace",
        capability_kind=kind,
        capability_name=name,
        reason=reason,
    )

    assert blocker.capability_kind is kind
    assert blocker.capability_name == name


@pytest.mark.parametrize(
    ("kind", "name", "reason"),
    [
        (None, None, "capability_missing"),
        (CapabilityKind.TOOL, "calendar.list", "app_absent"),
        (None, "https://provider.example/tool?credential=DO-NOT-LEAK", "app_absent"),
        (CapabilityKind.TOOL, None, "app_absent"),
        (None, "calendar.list", "capability_missing"),
        (CapabilityKind.TOOL, None, "capability_missing"),
    ],
    ids=(
        "neither-for-capability",
        "both-for-identity",
        "name-only-for-identity",
        "kind-only-for-identity",
        "name-only-for-capability",
        "kind-only-for-capability",
    ),
)
def test_blocker_presence_matrix_rejects_partial_or_reason_inconsistent_shapes(
    kind: CapabilityKind | None,
    name: str | None,
    reason: str,
) -> None:
    with pytest.raises(ValueError) as raised:
        DependencyBlocker(
            app_id="com.example.workspace",
            capability_kind=kind,
            capability_name=name,
            reason=reason,
        )

    rendered = str(raised.value)
    assert "https://" not in rendered
    assert "credential" not in rendered


def _app_absent_blocker(app_id: str = "com.example.workspace") -> DependencyBlocker:
    return DependencyBlocker(
        app_id=app_id,
        capability_kind=None,
        capability_name=None,
        reason="app_absent",
    )


@pytest.mark.parametrize(
    ("required", "optional", "blockers", "optional_blockers", "ready"),
    [
        (
            (
                ResolvedAppDependency(
                    requirement=app_requirement(),
                    app=snapshot(),
                ),
            )
            * 2,
            (),
            (),
            (),
            True,
        ),
        ((), (), (_app_absent_blocker(),) * 2, (), False),
        ((), (), (_app_absent_blocker(),), (_app_absent_blocker(),), False),
        (
            (
                ResolvedAppDependency(
                    requirement=app_requirement(),
                    app=snapshot(),
                ),
            ),
            (),
            (_app_absent_blocker(),),
            (),
            False,
        ),
        (
            (),
            (
                ResolvedAppDependency(
                    requirement=app_requirement(),
                    app=snapshot(),
                ),
            ),
            (),
            (_app_absent_blocker(),),
            True,
        ),
    ],
)
def test_resolution_direct_constructor_rejects_duplicate_or_overlapping_lanes(
    required: tuple[ResolvedAppDependency, ...],
    optional: tuple[ResolvedAppDependency, ...],
    blockers: tuple[DependencyBlocker, ...],
    optional_blockers: tuple[DependencyBlocker, ...],
    ready: bool,
) -> None:
    with pytest.raises(ValueError, match="dependency resolution is invalid"):
        DependencyResolution(
            required=required,
            optional=optional,
            blockers=blockers,
            optional_blockers=optional_blockers,
            ready=ready,
        )


def test_resolution_direct_constructor_canonicalizes_each_lane() -> None:
    alpha_requirement = app_requirement(app_id="com.example.alpha", provider_id="alpha")
    zeta_requirement = app_requirement(app_id="com.example.zeta", provider_id="zeta")
    alpha = ResolvedAppDependency(
        requirement=alpha_requirement,
        app=snapshot(app_id="com.example.alpha", provider_id="alpha"),
    )
    zeta = ResolvedAppDependency(
        requirement=zeta_requirement,
        app=snapshot(app_id="com.example.zeta", provider_id="zeta"),
    )
    alpha_blocker = _app_absent_blocker("com.example.blocked-alpha")
    zeta_blocker = _app_absent_blocker("com.example.blocked-zeta")

    result = DependencyResolution(
        required=(zeta, alpha),
        optional=(),
        blockers=(zeta_blocker, alpha_blocker),
        optional_blockers=(),
        ready=False,
    )

    assert tuple(item.requirement.app_id for item in result.required) == (
        "com.example.alpha",
        "com.example.zeta",
    )
    assert tuple(item.app_id for item in result.blockers) == (
        "com.example.blocked-alpha",
        "com.example.blocked-zeta",
    )

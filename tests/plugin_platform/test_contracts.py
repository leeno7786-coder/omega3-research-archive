"""Behavioral contracts for signed declarative plugin manifests."""

from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Callable

import jsonschema
import pytest

from plugin_platform import contracts
from plugin_platform.contracts import (
    PLUGIN_PROTOCOL_VERSION,
    CapabilityKind,
    CapabilityRequirement,
    PluginManifest,
    parse_app_id,
)
from plugin_platform.schema_validation import validate_plugin_manifest_schema_document


def valid_plugin_manifest() -> dict[str, object]:
    return {
        "manifest_version": 2,
        "plugin_id": "omega.productivity.workspace",
        "publisher_id": "omega",
        "plugin_version": "1.0.0",
        "minimum_omega_version": "3.0.0",
        "kind": "mcp_backed",
        "title": "Workspace",
        "description": "Conversational workspace workflows.",
        "assets": [
            {
                "path": "skills/workspace/SKILL.md",
                "kind": "skill",
                "media_type": "text/markdown",
                "sha256": "a" * 64,
            }
        ],
        "entry_points": [
            {
                "entry_point_id": "get_started",
                "title": "Get started",
                "skill_path": "skills/workspace/SKILL.md",
                "required_app_ids": ["com.example.workspace"],
            }
        ],
        "starter_requests": ["Help me get started with Workspace."],
        "required_apps": [
            {
                "app_id": "com.example.workspace",
                "publisher_id": "example",
                "deployment": "local",
                "provider_id": "workspace",
                "protocol_version": "2026-07-28",
                "transports": ["stdio", "streamable_http"],
                "remote_origin": None,
                "authorization_issuer": None,
                "authentication": {"kind": "none", "scopes": [], "secret_fields": []},
                "capabilities": [
                    {
                        "kind": "tool",
                        "name": "calendar.list",
                        "schema_sha256": None,
                        "version": None,
                    }
                ],
            }
        ],
        "optional_apps": [],
        "schedule_template_paths": [],
        "proactive_event_rules": [
            {
                "event_class": "task_progress",
                "presentation": "proactive_panel",
                "acknowledgement_required": False,
            },
            {
                "event_class": "task_terminal",
                "presentation": "proactive_panel",
                "acknowledgement_required": True,
            },
        ],
        "enterprise_roles": [{"role_id": "workspace_user", "entry_point_ids": ["get_started"]}],
        "signing_key_id": "omega-plugin-release-1",
        "signature": "A" * 88,
    }


def parsed_manifest() -> PluginManifest:
    return PluginManifest.from_dict(valid_plugin_manifest())


def test_public_app_id_parser_has_manifest_identity_parity() -> None:
    assert parse_app_id("com.example.workspace") == "com.example.workspace"
    with pytest.raises(ValueError, match="plugin app_id"):
        parse_app_id("com.example.workspace/credential")


AUTH_CASES = {
    "none": ((), ()),
    "oauth_authorization_code_pkce": (("scope.read",), ()),
    "oauth_device_code": (("scope.read",), ()),
    "oauth_client_metadata_document": (("scope.read",), ()),
    "api_key": ((), ("api_key",)),
    "bearer_token": ((), ("token",)),
    "basic": ((), ("username", "password")),
    "declared_secret_fields": ((), ("tenant", "secret")),
}


def _authentication_document(kind: str) -> dict[str, object]:
    scopes, field_ids = AUTH_CASES[kind]
    fields = {
        "api_key": {"label": "API key", "secret": True, "required": True},
        "token": {"label": "Bearer token", "secret": True, "required": True},
        "username": {"label": "Username", "secret": False, "required": True},
        "password": {"label": "Password", "secret": True, "required": True},
        "tenant": {"label": "Tenant", "secret": False, "required": True},
        "secret": {"label": "Secret", "secret": True, "required": False},
    }
    return {
        "kind": kind,
        "scopes": list(scopes),
        "secret_fields": [
            {"field_id": field_id, **fields[field_id]} for field_id in field_ids
        ],
    }


def test_manifest_v2_requires_exact_authentication_per_app() -> None:
    raw = valid_plugin_manifest()
    raw["manifest_version"] = 2
    raw["required_apps"][0]["authentication"] = {  # type: ignore[index]
        "kind": "oauth_device_code",
        "scopes": ["repo", "read:org"],
        "secret_fields": [],
    }

    manifest = PluginManifest.from_dict(raw)

    auth = manifest.required_apps[0].authentication
    assert auth.kind is contracts.PluginAuthenticationKind.OAUTH_DEVICE_CODE
    assert auth.scopes == ("repo", "read:org")


def test_manifest_v1_and_missing_authentication_are_rejected() -> None:
    raw = valid_plugin_manifest()
    raw["manifest_version"] = 1
    with pytest.raises(ValueError, match="manifest_version"):
        PluginManifest.from_dict(raw)
    raw["manifest_version"] = 2
    del raw["required_apps"][0]["authentication"]  # type: ignore[index]
    with pytest.raises(ValueError, match="authentication"):
        PluginManifest.from_dict(raw)


@pytest.mark.parametrize(("kind", "expected"), AUTH_CASES.items())
def test_authentication_kinds_have_exact_scopes_and_secret_fields(
    kind: str,
    expected: tuple[tuple[str, ...], tuple[str, ...]],
) -> None:
    raw = valid_plugin_manifest()
    raw["manifest_version"] = 2
    raw["required_apps"][0]["authentication"] = _authentication_document(kind)  # type: ignore[index]

    authentication = PluginManifest.from_dict(raw).required_apps[0].authentication

    assert authentication.kind.value == kind
    assert authentication.scopes == expected[0]
    assert tuple(field.field_id for field in authentication.secret_fields) == expected[1]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda auth: auth.__setitem__("unknown", True), "unknown fields"),
        (lambda auth: auth.__setitem__("scopes", ["scope.read", "scope.read"]), "duplicates"),
        (
            lambda auth: auth.__setitem__(
                "secret_fields",
                [
                    {"field_id": "tenant", "label": "Tenant", "secret": False, "required": True},
                    {"field_id": "tenant", "label": "Tenant 2", "secret": False, "required": False},
                ],
            ),
            "duplicated",
        ),
        (
            lambda auth: auth.__setitem__(
                "secret_fields",
                [{"field_id": "tenant", "label": " ", "secret": False, "required": True}],
            ),
            "label",
        ),
        (
            lambda auth: auth.__setitem__(
                "secret_fields",
                [
                    {
                        "field_id": "tenant",
                        "label": "x" * 161,
                        "secret": False,
                        "required": True,
                    }
                ],
            ),
            "label",
        ),
    ],
)
def test_declared_authentication_rejects_noncanonical_fields(
    mutate: Callable[[dict[str, object]], None], message: str
) -> None:
    raw = valid_plugin_manifest()
    raw["manifest_version"] = 2
    authentication = _authentication_document("declared_secret_fields")
    mutate(authentication)
    raw["required_apps"][0]["authentication"] = authentication  # type: ignore[index]

    with pytest.raises(ValueError, match=message):
        PluginManifest.from_dict(raw)
    assert not _schema_accepts(raw)


@pytest.mark.parametrize(
    ("kind", "authentication", "message"),
    [
        ("oauth_device_code", _authentication_document("oauth_device_code"), "scopes"),
        (
            "none",
            {"kind": "none", "scopes": ["scope.read"], "secret_fields": []},
            "scopes",
        ),
        (
            "none",
            {
                "kind": "none",
                "scopes": [],
                "secret_fields": [
                    {"field_id": "token", "label": "Token", "secret": True, "required": True}
                ],
            },
            "none",
        ),
        (
            "api_key",
            {
                "kind": "api_key",
                "scopes": [],
                "secret_fields": [
                    {
                        "field_id": "api_key",
                        "label": "API key",
                        "secret": False,
                        "required": True,
                    }
                ],
            },
            "secret",
        ),
        (
            "basic",
            {
                "kind": "basic",
                "scopes": [],
                "secret_fields": [
                    {
                        "field_id": "username",
                        "label": "Username",
                        "secret": True,
                        "required": True,
                    },
                    {
                        "field_id": "password",
                        "label": "Password",
                        "secret": True,
                        "required": True,
                    },
                ],
            },
            "secret",
        ),
    ],
)
def test_authentication_rejects_kind_specific_invalid_shapes(
    kind: str,
    authentication: dict[str, object],
    message: str,
) -> None:
    raw = valid_plugin_manifest()
    raw["manifest_version"] = 2
    document = copy.deepcopy(authentication)
    if kind == "oauth_device_code":
        document["scopes"] = []
    raw["required_apps"][0]["authentication"] = document  # type: ignore[index]

    with pytest.raises(ValueError, match=message):
        PluginManifest.from_dict(raw)


def test_manifest_round_trip_is_closed_and_mutation_isolated() -> None:
    raw = valid_plugin_manifest()
    parsed = PluginManifest.from_dict(raw)

    assert parsed.to_dict() == raw
    raw["required_apps"][0]["capabilities"][0]["name"] = "changed"  # type: ignore[index]
    assert parsed.required_apps[0].capabilities[0].name == "calendar.list"


def test_app_requirement_and_capability_have_no_writable_nested_instance_state() -> None:
    """Catches frozen app authority remaining mutable through nested ``__dict__``."""
    requirement = parsed_manifest().required_apps[0]
    capability = requirement.capabilities[0]

    for value in (requirement, capability):
        with pytest.raises((AttributeError, TypeError)):
            value.__dict__["authority"] = "changed"

    assert requirement.app_id == "com.example.workspace"
    assert capability.name == "calendar.list"


def test_manifest_signing_bytes_cover_the_unsigned_canonical_document() -> None:
    parsed = parsed_manifest()

    assert parsed.unsigned_dict() == {
        key: value for key, value in valid_plugin_manifest().items() if key != "signature"
    }
    assert parsed.signing_bytes() == (
        b'{"assets":[{"kind":"skill","media_type":"text/markdown","path":"skills/workspace/SKILL.md",'
        b'"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}],'
        b'"description":"Conversational workspace workflows.","enterprise_roles":[{"entry_point_ids"'
        b':["get_started"],"role_id":"workspace_user"}],"entry_points":[{"entry_point_id":"get_started",'
        b'"required_app_ids":["com.example.workspace"],"skill_path":"skills/workspace/SKILL.md","title":"Get started"}],'
        b'"kind":"mcp_backed","manifest_version":2,"minimum_omega_version":"3.0.0","optional_apps":[],'
        b'"plugin_id":"omega.productivity.workspace","plugin_version":"1.0.0","proactive_event_rules":['
        b'{"acknowledgement_required":false,"event_class":"task_progress","presentation":"proactive_panel"},'
        b'{"acknowledgement_required":true,"event_class":"task_terminal","presentation":"proactive_panel"}],'
        b'"publisher_id":"omega","required_apps":[{"app_id":"com.example.workspace","authentication":{"kind":"none",'
        b'"scopes":[],"secret_fields":[]},"authorization_issuer":null,'
        b'"capabilities":[{"kind":"tool","name":"calendar.list","schema_sha256":null,"version":null}],'
        b'"deployment":"local","protocol_version":"2026-07-28","provider_id":"workspace","publisher_id":"example",'
        b'"remote_origin":null,"transports":["stdio","streamable_http"]}],"schedule_template_paths":[],'
        b'"signing_key_id":"omega-plugin-release-1","starter_requests":["Help me get started with Workspace."],'
        b'"title":"Workspace"}'
    )


@pytest.mark.parametrize("protocol", ["2025-11-25", "2025-06-18", "2026-07-28\n"])
def test_manifest_rejects_noncurrent_protocol(protocol: str) -> None:
    raw = valid_plugin_manifest()
    raw["required_apps"][0]["protocol_version"] = protocol  # type: ignore[index]

    with pytest.raises(ValueError, match="plugin app protocol is invalid"):
        PluginManifest.from_dict(raw)


def _manifest_with_capability_name(*, kind: str, name: str) -> dict[str, object]:
    raw = valid_plugin_manifest()
    capability = raw["required_apps"][0]["capabilities"][0]  # type: ignore[index]
    capability.update(  # type: ignore[union-attr]
        {
            "kind": kind,
            "name": name,
            "version": "1.0.0" if kind == "extension" else None,
        }
    )
    return raw


def _schema_accepts(raw: dict[str, object]) -> bool:
    try:
        validate_plugin_manifest_schema_document(raw)
    except jsonschema.ValidationError:
        return False
    return True


def test_official_namespaced_mcp_extension_has_exact_runtime_schema_parity() -> None:
    raw = _manifest_with_capability_name(
        kind="extension",
        name="io.modelcontextprotocol/tasks",
    )
    raw["required_apps"][0]["capabilities"][0]["version"] = None  # type: ignore[index]

    parsed = PluginManifest.from_dict(raw)

    assert parsed.to_dict() == raw
    assert parsed.required_apps[0].capabilities[0].name == "io.modelcontextprotocol/tasks"
    assert parsed.required_apps[0].capabilities[0].version is None
    assert _schema_accepts(raw)


@pytest.mark.parametrize(
    ("kind", "name", "version", "accepted"),
    [
        ("extension", "io.modelcontextprotocol/tasks", None, True),
        ("extension", "io.modelcontextprotocol/tasks", "1.0.0", False),
        ("extension", "vendor.extension/task-v1", None, False),
        ("extension", "vendor.extension/task-v1", "1.0.0", True),
        ("tool", "io.modelcontextprotocol/tasks", None, False),
    ],
)
def test_only_exact_tasks_extension_has_versionless_runtime_schema_parity(
    kind: str,
    name: str,
    version: str | None,
    accepted: bool,
) -> None:
    raw = _manifest_with_capability_name(kind=kind, name=name)
    raw["required_apps"][0]["capabilities"][0]["version"] = version  # type: ignore[index]

    try:
        parsed = PluginManifest.from_dict(raw)
    except ValueError:
        runtime_accepts = False
    else:
        runtime_accepts = True
        assert parsed.to_dict() == raw

    assert runtime_accepts is accepted
    assert _schema_accepts(raw) is accepted

    capability = raw["required_apps"][0]["capabilities"][0]  # type: ignore[index]
    capability.pop("version")  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        PluginManifest.from_dict(raw)
    assert not _schema_accepts(raw)


def test_versionless_tasks_capability_direct_constructor_is_closed_and_frozen() -> None:
    capability = CapabilityRequirement(
        kind=CapabilityKind.EXTENSION,
        name="io.modelcontextprotocol/tasks",
        schema_sha256=None,
        version=None,
    )
    assert capability.version is None
    with pytest.raises(FrozenInstanceError):
        capability.version = "1.0.0"  # type: ignore[misc]
    with pytest.raises(ValueError):
        CapabilityRequirement(
            kind=CapabilityKind.EXTENSION,
            name="io.modelcontextprotocol/tasks",
            schema_sha256=None,
            version="1.0.0",
        )
    with pytest.raises(ValueError):
        CapabilityRequirement(
            kind=CapabilityKind.TOOL,
            name="io.modelcontextprotocol/tasks",
            schema_sha256=None,
            version=None,
        )


@pytest.mark.parametrize(
    ("kind", "name", "accepted"),
    [
        ("extension", "io.modelcontextprotocol/tasks", True),
        ("extension", "vendor.extension/task-v1", True),
        ("tool", "io.modelcontextprotocol/tasks", False),
        ("prompt", "io.modelcontextprotocol/tasks", False),
        ("resource", "io.modelcontextprotocol/tasks", False),
        ("extension", "tasks", False),
        ("extension", "/tasks", False),
        ("extension", "io.modelcontextprotocol/", False),
        ("extension", "io/modelcontextprotocol/tasks", False),
        ("extension", "./tasks", False),
        ("extension", "../tasks", False),
        ("extension", "io.modelcontextprotocol/.", False),
        ("extension", "io.modelcontextprotocol/..", False),
        ("extension", "io.modelcontextprotocol\\tasks", False),
        ("extension", "io.modelcontextprotocol%2Ftasks", False),
        ("extension", "io.modelcontextprotocol:tasks", False),
        ("extension", "io.modelcontextprotocol/ tasks", False),
        ("extension", "io.modelcontextprotocol/tasks ", False),
        ("extension", "io.modelcontextprotocol/tasks\t", False),
        ("extension", "io.modelcontextprotocol/tasks\n", False),
        ("extension", "io.modelcontextprotocol/t\u00e1sks", False),
        ("extension", "io.modelcontextprotocol/ta\u0301sks", False),
    ],
)
def test_namespaced_extension_mutations_have_exact_runtime_schema_parity(
    kind: str,
    name: str,
    accepted: bool,
) -> None:
    raw = _manifest_with_capability_name(kind=kind, name=name)
    if kind == "extension" and name == "io.modelcontextprotocol/tasks":
        raw["required_apps"][0]["capabilities"][0]["version"] = None  # type: ignore[index]

    try:
        parsed = PluginManifest.from_dict(raw)
    except ValueError:
        runtime_accepts = False
    else:
        runtime_accepts = True
        assert parsed.to_dict() == raw

    assert runtime_accepts is accepted
    assert _schema_accepts(raw) is accepted


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda raw: raw.__setitem__("unknown", True), "unknown fields"),
        (
            lambda raw: raw["assets"][0].__setitem__("unknown", True),  # type: ignore[index]
            "unknown fields",
        ),
        (
            lambda raw: raw["assets"].append(copy.deepcopy(raw["assets"][0])),  # type: ignore[index]
            "plugin asset identity is duplicated",
        ),
        (
            lambda raw: raw.__setitem__("plugin_version", "1.0"),
            "plugin_version is not exact SemVer",
        ),
        (
            lambda raw: raw.__setitem__("minimum_omega_version", "v3.0.0"),
            "minimum_omega_version is not exact SemVer",
        ),
        (
            lambda raw: raw["assets"][0].__setitem__("path", "/plugin.json"),  # type: ignore[index]
            "plugin asset path is invalid",
        ),
        (
            lambda raw: raw["assets"][0].__setitem__("path", "../plugin.json"),  # type: ignore[index]
            "plugin asset path is invalid",
        ),
        (
            lambda raw: raw["assets"][0].__setitem__("path", "C:/plugin.json"),  # type: ignore[index]
            "plugin asset path is invalid",
        ),
        (
            lambda raw: raw["assets"][0].__setitem__("path", "NUL"),  # type: ignore[index]
            "plugin asset path is invalid",
        ),
        (
            lambda raw: raw["assets"][0].__setitem__("sha256", "A" * 64),  # type: ignore[index]
            "lowercase SHA-256",
        ),
        (
            lambda raw: raw["required_apps"].append(copy.deepcopy(raw["required_apps"][0])),  # type: ignore[index]
            "plugin app identity is duplicated",
        ),
        (
            lambda raw: raw["entry_points"][0].__setitem__("skill_path", "skills/missing/SKILL.md"),  # type: ignore[index]
            "plugin entry point skill is invalid",
        ),
        (
            lambda raw: raw["entry_points"][0].__setitem__("required_app_ids", ["missing"]),  # type: ignore[index]
            "plugin entry point app is invalid",
        ),
        (
            lambda raw: raw.__setitem__("schedule_template_paths", ["skills/workspace/SKILL.md"]),
            "plugin schedule template is invalid",
        ),
        (
            lambda raw: raw["proactive_event_rules"][0].__setitem__("event_class", "unknown"),  # type: ignore[index]
            "plugin proactive event class is invalid",
        ),
        (
            lambda raw: raw.__setitem__("title", " Workspace"),
            "stable string",
        ),
        (
            lambda raw: raw["assets"][0].__setitem__("kind", "script"),  # type: ignore[index]
            "plugin asset kind is invalid",
        ),
        (
            lambda raw: raw["assets"][0].__setitem__("media_type", "application/x-sh"),  # type: ignore[index]
            "plugin asset media type is invalid",
        ),
    ],
)
def test_manifest_rejects_closed_or_invalid_declarative_values(
    mutate: Callable[[dict[str, object]], None], message: str
) -> None:
    raw = valid_plugin_manifest()
    mutate(raw)

    with pytest.raises(ValueError, match=message):
        PluginManifest.from_dict(raw)


@pytest.mark.parametrize(
    ("kind", "required_apps", "message"),
    [
        ("skill_only", [valid_plugin_manifest()["required_apps"][0]], "skill_only"),
        ("mcp_backed", [], "mcp_backed"),
        (
            "composite",
            [
                valid_plugin_manifest()["required_apps"][0],
                {
                    **valid_plugin_manifest()["required_apps"][0],
                    "app_id": "com.example.workspace-two",
                },
            ],
            None,
        ),
        ("app_only", [valid_plugin_manifest()["required_apps"][0]], "app_only"),
    ],
)
def test_manifest_enforces_plugin_kind_app_structure(
    kind: str, required_apps: list[object], message: str | None
) -> None:
    raw = valid_plugin_manifest()
    raw["kind"] = kind
    raw["required_apps"] = required_apps
    if kind == "mcp_backed":
        raw["entry_points"][0]["required_app_ids"] = []  # type: ignore[index]
    if kind == "composite":
        raw["entry_points"][0]["required_app_ids"] = [  # type: ignore[index]
            "com.example.workspace",
            "com.example.workspace-two",
        ]
        assert PluginManifest.from_dict(raw).kind.value == "composite"
    else:
        with pytest.raises(ValueError, match=message):
            PluginManifest.from_dict(raw)


def test_remote_app_requires_canonical_https_identity_and_http_transport() -> None:
    raw = valid_plugin_manifest()
    app = raw["required_apps"][0]  # type: ignore[index]
    app.update(
        {
            "deployment": "remote",
            "provider_id": None,
            "remote_origin": "https://workspace.example",
            "authorization_issuer": "https://auth.workspace.example",
            "transports": ["streamable_http"],
        }
    )

    assert PluginManifest.from_dict(raw).required_apps[0].deployment.value == "remote"

    app["remote_origin"] = "https://workspace.example/"
    with pytest.raises(ValueError, match="canonical HTTPS"):
        PluginManifest.from_dict(raw)

    schema = json.loads(
        Path("schema/plugins/plugin_manifest.schema.json").read_text(encoding="utf-8")
    )
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(raw))

    # An issuer ending in "/" is what Miro and Netlify publish, and RFC 8414
    # s3.1 says to strip that slash before inserting the well-known path rather
    # than to reject the identifier. What must still be refused is a segment
    # that traverses, because the well-known prefix sits in front of this path.
    app["remote_origin"] = "https://workspace.example"
    app["authorization_issuer"] = "https://auth.workspace.example/"
    assert (
        PluginManifest.from_dict(raw).required_apps[0].authorization_issuer
        == "https://auth.workspace.example/"
    )
    assert not list(jsonschema.Draft202012Validator(schema).iter_errors(raw))

    for traversal in (
        "https://auth.workspace.example/oauth2/../v1",
        "https://auth.workspace.example/./v1",
        "https://auth.workspace.example/oauth2//v1",
    ):
        app["authorization_issuer"] = traversal
        with pytest.raises(ValueError, match="canonical HTTPS"):
            PluginManifest.from_dict(raw)
        assert list(jsonschema.Draft202012Validator(schema).iter_errors(raw))


def remote_plugin_manifest() -> dict[str, object]:
    raw = valid_plugin_manifest()
    raw["required_apps"][0].update(  # type: ignore[index]
        {
            "deployment": "remote",
            "provider_id": None,
            "remote_origin": "https://workspace.example",
            "authorization_issuer": "https://auth.workspace.example/oidc",
            "transports": ["streamable_http"],
        }
    )
    return raw


@pytest.mark.parametrize(
    ("field", "identity"),
    [
        ("remote_origin", "https://:443"),
        ("remote_origin", "https://workspace.example:invalid"),
        ("remote_origin", "https://Workspace.example"),
        ("remote_origin", "https://workspace.example:443"),
        ("authorization_issuer", "https://:443"),
        ("authorization_issuer", "https://auth.workspace.example:invalid"),
        ("authorization_issuer", "https://Auth.workspace.example/oidc"),
        ("authorization_issuer", "https://auth.workspace.example:443/oidc"),
        ("authorization_issuer", "https://auth.workspace.example/%7Eoidc"),
    ],
)
def test_manifest_rejects_noncanonical_remote_https_identities(field: str, identity: str) -> None:
    raw = remote_plugin_manifest()
    raw["required_apps"][0][field] = identity  # type: ignore[index]

    with pytest.raises(ValueError, match="canonical HTTPS"):
        PluginManifest.from_dict(raw)

    schema = json.loads(
        Path("schema/plugins/plugin_manifest.schema.json").read_text(encoding="utf-8")
    )
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(raw))


@pytest.mark.parametrize("path", ["NUL", " skills/workspace/SKILL.md", "skill:backup"])
def test_runtime_and_schema_reject_the_same_invalid_package_paths(path: str) -> None:
    raw = valid_plugin_manifest()
    raw["assets"][0]["path"] = path  # type: ignore[index]
    raw["entry_points"][0]["skill_path"] = path  # type: ignore[index]

    with pytest.raises(ValueError):
        PluginManifest.from_dict(raw)

    schema = json.loads(
        Path("schema/plugins/plugin_manifest.schema.json").read_text(encoding="utf-8")
    )
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(raw))


@pytest.mark.parametrize(
    "path",
    ["nul", "CoN", "prn.txt", "aUx", "com1", "cOm9.log", "lpt1", "LpT9.txt"],
)
def test_schema_matches_runtime_for_case_insensitive_windows_device_paths(path: str) -> None:
    raw = valid_plugin_manifest()
    raw["assets"][0]["path"] = path  # type: ignore[index]
    raw["entry_points"][0]["skill_path"] = path  # type: ignore[index]

    with pytest.raises(ValueError):
        PluginManifest.from_dict(raw)

    schema = json.loads(
        Path("schema/plugins/plugin_manifest.schema.json").read_text(encoding="utf-8")
    )
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(raw))


def test_schema_accepts_the_same_valid_document_and_rejects_trailing_newline() -> None:
    schema = json.loads(
        Path("schema/plugins/plugin_manifest.schema.json").read_text(encoding="utf-8")
    )
    validator = jsonschema.Draft202012Validator(schema)
    raw = valid_plugin_manifest()

    assert list(validator.iter_errors(raw)) == []
    raw["required_apps"][0]["protocol_version"] = PLUGIN_PROTOCOL_VERSION + "\n"  # type: ignore[index]
    assert list(validator.iter_errors(raw))


def test_plugin_icons_are_png_only_at_runtime_and_schema() -> None:
    """Catches advertising a WebP asset that package verification can never admit."""
    raw = valid_plugin_manifest()
    raw["assets"].append(  # type: ignore[union-attr]
        {
            "path": "assets/plugin.webp",
            "kind": "icon",
            "media_type": "image/webp",
            "sha256": "b" * 64,
        }
    )
    schema = json.loads(
        Path("schema/plugins/plugin_manifest.schema.json").read_text(encoding="utf-8")
    )

    with pytest.raises(ValueError, match="plugin asset media type is invalid"):
        PluginManifest.from_dict(raw)
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(raw))


def test_schema_defers_selected_key_capability_uniqueness_to_runtime() -> None:
    """Pin the Draft 2020-12 boundary for duplicate ``(kind, name)`` identities.

    JSON Schema ``uniqueItems`` compares complete objects.  Two namespaced extension
    requirements can therefore differ in signed metadata while still colliding on the
    semantic identity that runtime must reject.
    """
    raw = valid_plugin_manifest()
    app = raw["required_apps"][0]  # type: ignore[index]
    app["capabilities"] = [
        {
            "kind": "extension",
            "name": "io.modelcontextprotocol/tasks",
            "schema_sha256": None,
            "version": None,
        },
        {
            "kind": "extension",
            "name": "io.modelcontextprotocol/tasks",
            "schema_sha256": "c" * 64,
            "version": None,
        },
    ]
    schema = json.loads(
        Path("schema/plugins/plugin_manifest.schema.json").read_text(encoding="utf-8")
    )

    assert list(jsonschema.Draft202012Validator(schema).iter_errors(raw)) == []
    with pytest.raises(ValueError, match="plugin capability identity is duplicated"):
        PluginManifest.from_dict(raw)

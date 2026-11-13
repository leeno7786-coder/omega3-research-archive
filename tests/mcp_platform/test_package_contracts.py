"""Behavioral contracts for signed MCP package metadata."""

from __future__ import annotations

import json
from copy import deepcopy

import jsonschema
import pytest

from core.paths import OMEGA_ROOT
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.package_contracts import (
    MCP_PROTOCOL_VERSION,
    McpPackageManifest,
    NetworkClass,
    ResourceCatalog,
    ToolCatalog,
)


def valid_manifest() -> dict[str, object]:
    return {
        "manifest_version": 1,
        "server_id": "omega.first_party.fake",
        "publisher_id": "omega.first_party",
        "server_version": "1.0.0",
        "minimum_omega_version": "3.0.0",
        "supported_mcp_protocol_versions": [MCP_PROTOCOL_VERSION],
        "network_class": "local_only",
        "platform": "windows-x86_64",
        "entrypoint": {"artifact": "omega-mcp-fake.exe", "arguments": []},
        "artifact_sha256": "a" * 64,
        "tool_catalog_sha256": "b" * 64,
        "resource_catalog_sha256": "c" * 64,
        "capability_tags": ["mail.search"],
        "risk_declarations": ["read_only"],
        "oauth_providers": [],
        "allowed_network_destinations": [],
        "signing_key_id": "omega-release-2026-1",
        "signature": "A" * 88,
    }


def valid_tool_catalog() -> dict[str, object]:
    input_schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    output_schema = {"type": "object", "properties": {"results": {"type": "array"}}}
    return {
        "catalog_version": 1,
        "server_id": "omega.first_party.fake",
        "server_version": "1.0.0",
        "tools": [
            {
                "name": "mail_search",
                "title": "Search mail",
                "input_schema": input_schema,
                "output_schema": output_schema,
                "input_schema_sha256": "094ec29d007cce150c65abf0756d79ad5b62a1acfdb6e0841f69f1377ef41761",
                "output_schema_sha256": "d519f72d3610fbd9db07bee9fdcaa6a4cc579b74529cac2b8e35216294376c7c",
                "capability_tags": ["mail.search"],
                "provider_extension_tags": [],
                "risk_class": "read_only",
                "effect": "searches the connected mail corpus",
                "requires_revision": True,
                "max_result_bytes": 4096,
                "allowed_mime_types": ["application/json"],
            }
        ],
    }


def valid_resource_catalog() -> dict[str, object]:
    return {
        "catalog_version": 1,
        "server_id": "omega.first_party.fake",
        "server_version": "1.0.0",
        "resources": [
            {
                "uri": "omega://mail/inbox",
                "name": "Inbox",
                "mime_type": "application/json",
                "max_bytes": 4096,
                "capability_tags": ["mail.search"],
            }
        ],
        "resource_templates": [
            {
                "uri_template": "omega://mail/message/{message_id}",
                "name": "Message",
                "mime_type": "application/json",
                "max_bytes": 4096,
                "capability_tags": ["mail.search"],
            }
        ],
    }


def test_manifest_round_trip_is_exact() -> None:
    manifest = McpPackageManifest.from_dict(valid_manifest())

    assert manifest.network_class is NetworkClass.LOCAL_ONLY
    assert manifest.to_dict() == valid_manifest()


def test_manifest_apps_metadata_is_signed_closed_bounded_and_inert() -> None:
    manifest = valid_manifest()
    manifest["mcp_apps_metadata"] = {
        "tools": {
            "mail_search": {
                "resourceUri": "ui://mail/search",
                "mimeType": "text/html",
                "title": "Mail search",
            }
        },
        "resources": {"Inbox": {"resourceUri": "ui://mail/inbox", "title": "Inbox"}},
        "resource_templates": {},
    }
    parsed = McpPackageManifest.from_dict(manifest)
    assert parsed.to_dict() == manifest
    with pytest.raises(TypeError):
        parsed.mcp_apps_metadata["tools"]["mail_search"]["title"] = "changed"
    assert b'"mcp_apps_metadata"' in parsed.signing_bytes()

    invalid_documents = (
        {
            "tools": {"mail_search": {"resourceUri": "ui://mail/search", "html": "x"}},
            "resources": {},
            "resource_templates": {},
        },
        {
            "tools": {"mail_search": {"resourceUri": "ui://mail/search", "onMessage": "x"}},
            "resources": {},
            "resource_templates": {},
        },
        {
            "tools": {"mail_search": {"resourceUri": "ui://mail/search", "title": "x" * 70_000}},
            "resources": {},
            "resource_templates": {},
        },
    )
    for invalid in invalid_documents:
        candidate = valid_manifest()
        candidate["mcp_apps_metadata"] = invalid
        with pytest.raises(ValueError, match="MCP Apps metadata"):
            McpPackageManifest.from_dict(candidate)

    schema = json.loads(
        (OMEGA_ROOT / "schema" / "mcp" / "package_manifest.schema.json").read_text(encoding="utf-8")
    )
    validator = jsonschema.Draft202012Validator(schema)
    validator.validate(manifest)
    for invalid in invalid_documents:
        candidate = valid_manifest()
        candidate["mcp_apps_metadata"] = invalid
        with pytest.raises(jsonschema.ValidationError):
            validator.validate(candidate)


def test_manifest_rejects_unknown_fields_and_path_escape() -> None:
    unknown = valid_manifest()
    unknown["server_command"] = "powershell.exe"
    with pytest.raises(ValueError, match="unknown fields"):
        McpPackageManifest.from_dict(unknown)

    escaped = deepcopy(valid_manifest())
    escaped["entrypoint"] = {"artifact": "../server.exe", "arguments": []}
    with pytest.raises(ValueError, match="relative package path"):
        McpPackageManifest.from_dict(escaped)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--client-secret=opaque-value"],
        ["--CLIENT_SECRETS", "opaque-value"],
        ["--accessToken:opaque-value"],
        ["--refresh.tokens", "opaque-value"],
        ["--Api-Key", "opaque-value"],
        ["--oauth_passphrase", "opaque-value"],
        ["--authorization", "opaque-value"],
        ["--credentials-file", "credentials.json"],
        ["--auth", "opaque-value"],
        ["--clientsecret=opaque-value"],
        ["--clientsecrets", "opaque-value"],
        ["--apikey:opaque-value"],
        ["--apikeys", "opaque-value"],
        ["--accesstoken=opaque-value"],
        ["--accesstokens", "opaque-value"],
        ["--refreshtoken:opaque-value"],
        ["--refreshtokens", "opaque-value"],
    ],
)
def test_manifest_rejects_credential_bearing_process_argument_labels(
    arguments: list[str],
) -> None:
    manifest = valid_manifest()
    manifest["entrypoint"] = {
        "artifact": "omega-mcp-fake.exe",
        "arguments": arguments,
    }

    with pytest.raises(
        ValueError,
        match="^entrypoint_arguments_secret_bearing$",
    ) as caught:
        McpPackageManifest.from_dict(manifest)

    assert "opaque-value" not in str(caught.value)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--oauthclientsecret=opaque-value"],
        ["--oauthclientsecret", "opaque-value"],
        ["--OAUTHCLIENTSECRETS:opaque-value"],
        ["--oauthClientSecret=opaque-value"],
        ["--oauth-client-secrets", "opaque-value"],
        ["--oauthrefreshtoken=opaque-value"],
        ["--oauthrefreshtokens", "opaque-value"],
        ["--oauthRefreshToken:opaque-value"],
        ["--oauth-refresh-tokens", "opaque-value"],
        ["--provideraccesstoken=opaque-value"],
        ["--provideraccesstokens", "opaque-value"],
        ["--providerAccessToken:opaque-value"],
        ["--provider-access-tokens", "opaque-value"],
    ],
)
def test_manifest_rejects_multiscope_compact_credential_labels(
    arguments: list[str],
) -> None:
    manifest = valid_manifest()
    manifest["entrypoint"] = {
        "artifact": "omega-mcp-fake.exe",
        "arguments": arguments,
    }

    with pytest.raises(
        ValueError,
        match="^entrypoint_arguments_secret_bearing$",
    ) as caught:
        McpPackageManifest.from_dict(manifest)

    assert "opaque-value" not in str(caught.value)


@pytest.mark.parametrize(
    "secret_value",
    [
        "Bear" + "er opaque.direct.value",
        "ya" + "29." + "a" * 24,
        "1" + "//" + "b" * 24,
        "GOC" + "SPX-" + "c" * 24,
        "-----BEGIN " + "PRIVATE KEY-----opaque",
    ],
)
def test_manifest_rejects_recognizable_direct_secret_argument_values(
    secret_value: str,
) -> None:
    manifest = valid_manifest()
    manifest["entrypoint"] = {
        "artifact": "omega-mcp-fake.exe",
        "arguments": ["--session-material", secret_value],
    }

    with pytest.raises(
        ValueError,
        match="^entrypoint_arguments_secret_bearing$",
    ) as caught:
        McpPackageManifest.from_dict(manifest)

    assert secret_value not in str(caught.value)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--stdio"],
        ["--config", "ordinary-settings.json"],
        ["--token-count", "2048"],
        ["--tokenization-mode", "bounded"],
        ["--tokens-per-batch", "64"],
        ["--token_count", "2048"],
        ["--tokenization", "bounded"],
        ["--keyframe", "24"],
        ["--keyboard-layout", "ordinary"],
        ["--monkey-patch", "disabled"],
        ["--api-version", "v1"],
        ["--provider-id=ordinary"],
        ["--client-id", "ordinary"],
        ["--provider-metadata", "ordinary"],
        ["--clientVersion", "v1"],
        ["--oauth-mode", "local"],
        ["--provider-tokenization", "bounded"],
        ["ordinary-file-name.txt"],
    ],
)
def test_manifest_allows_benign_process_arguments(arguments: list[str]) -> None:
    manifest = valid_manifest()
    manifest["entrypoint"] = {
        "artifact": "omega-mcp-fake.exe",
        "arguments": arguments,
    }

    assert McpPackageManifest.from_dict(manifest).arguments == tuple(arguments)


@pytest.mark.parametrize("artifact", ["/server.exe", r"\server.exe"])
def test_manifest_and_schema_reject_root_relative_windows_artifacts(artifact: str) -> None:
    manifest = valid_manifest()
    manifest["entrypoint"] = {"artifact": artifact, "arguments": []}
    with pytest.raises(ValueError, match="relative package path"):
        McpPackageManifest.from_dict(manifest)

    schema_path = OMEGA_ROOT / "schema" / "mcp" / "package_manifest.schema.json"
    validator = jsonschema.Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(manifest)


def test_manifest_requires_the_pinned_protocol_and_lowercase_digests() -> None:
    unsupported = valid_manifest()
    unsupported["supported_mcp_protocol_versions"] = ["2024-11-05"]
    with pytest.raises(ValueError, match="MCP protocol"):
        McpPackageManifest.from_dict(unsupported)

    uppercase_digest = valid_manifest()
    uppercase_digest["artifact_sha256"] = "A" * 64
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        McpPackageManifest.from_dict(uppercase_digest)


def test_manifest_schema_rejects_local_network_declarations() -> None:
    schema_path = OMEGA_ROOT / "schema" / "mcp" / "package_manifest.schema.json"
    validator = jsonschema.Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))
    invalid = valid_manifest()
    invalid["allowed_network_destinations"] = ["api.example.invalid"]

    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)


def test_tool_catalog_round_trips_and_binds_schema_digests() -> None:
    catalog = valid_tool_catalog()

    assert ToolCatalog.from_dict(catalog).to_dict() == catalog

    mismatched = deepcopy(catalog)
    tool = mismatched["tools"][0]
    assert isinstance(tool, dict)
    tool["input_schema_sha256"] = "d" * 64
    with pytest.raises(ValueError, match="input_schema_sha256"):
        ToolCatalog.from_dict(mismatched)


def test_catalog_schema_state_cannot_mutate_after_digest_validation() -> None:
    source = valid_tool_catalog()
    catalog = ToolCatalog.from_dict(source)
    source_tool = source["tools"][0]
    assert isinstance(source_tool, dict)
    source_input_schema = source_tool["input_schema"]
    assert isinstance(source_input_schema, dict)
    source_input_schema["properties"]["query"]["type"] = "number"

    tool = catalog.tools[0]
    with pytest.raises(TypeError):
        tool.input_schema["properties"]["query"]["type"] = "number"

    exported = catalog.to_dict()
    exported_tool = exported["tools"][0]
    assert isinstance(exported_tool, dict)
    exported_input_schema = exported_tool["input_schema"]
    assert isinstance(exported_input_schema, dict)
    exported_input_schema["properties"]["query"]["type"] = "number"

    assert catalog.to_dict() == valid_tool_catalog()
    assert ToolCatalog.from_dict(catalog.to_dict()).to_dict() == valid_tool_catalog()


def test_catalogs_reject_unknown_signed_fields() -> None:
    tools = valid_tool_catalog()
    tool = tools["tools"][0]
    assert isinstance(tool, dict)
    tool["command"] = "cmd.exe"
    with pytest.raises(ValueError, match="unknown fields"):
        ToolCatalog.from_dict(tools)

    resources = valid_resource_catalog()
    resource = resources["resources"][0]
    assert isinstance(resource, dict)
    resource["remote_url"] = "https://example.invalid"
    with pytest.raises(ValueError, match="unknown fields"):
        ResourceCatalog.from_dict(resources)


def test_resource_catalog_round_trip_is_exact() -> None:
    catalog = valid_resource_catalog()

    assert ResourceCatalog.from_dict(catalog).to_dict() == catalog


def test_rfc8785_bytes_and_sha256_are_stable() -> None:
    left = {"z": 1, "a": [True, None, "Ω"]}
    right = {"a": [True, None, "Ω"], "z": 1}
    expected = b'{"a":[true,null,"\xce\xa9"],"z":1}'

    assert canonical_bytes(left) == expected
    assert canonical_bytes(right) == expected
    assert (
        sha256_hex(expected) == "50a3b2032b832eb35e6589fb4f9ebcffd542fa31dbd1488265ccc913f2c68f0c"
    )


def test_manifest_signing_bytes_omit_signature_and_are_canonical() -> None:
    manifest = McpPackageManifest.from_dict(valid_manifest())
    expected = (
        b'{"allowed_network_destinations":[],"artifact_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"capability_tags":["mail.search"],"entrypoint":{"arguments":[],"artifact":"omega-mcp-fake.exe"},'
        b'"manifest_version":1,"minimum_omega_version":"3.0.0","network_class":"local_only",'
        b'"oauth_providers":[],"platform":"windows-x86_64","publisher_id":"omega.first_party",'
        b'"resource_catalog_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",'
        b'"risk_declarations":["read_only"],"server_id":"omega.first_party.fake","server_version":"1.0.0",'
        b'"signing_key_id":"omega-release-2026-1","supported_mcp_protocol_versions":["2026-07-28"],'
        b'"tool_catalog_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}'
    )

    assert manifest.unsigned_dict() == {
        key: value for key, value in valid_manifest().items() if key != "signature"
    }
    assert manifest.signing_bytes() == expected


def test_canonical_bytes_reject_non_json_values() -> None:
    with pytest.raises(ValueError, match="finite JSON"):
        canonical_bytes({"value": float("nan")})

    with pytest.raises(ValueError, match="not a JSON value"):
        canonical_bytes({"value": {"not", "json"}})


@pytest.mark.parametrize(
    ("filename", "document", "unknown_field"),
    [
        ("package_manifest.schema.json", valid_manifest(), "server_command"),
        ("tool_catalog.schema.json", valid_tool_catalog(), "command"),
        ("resource_catalog.schema.json", valid_resource_catalog(), "remote_url"),
    ],
)
def test_signed_json_schemas_are_draft_2020_12_and_closed(
    filename: str,
    document: dict[str, object],
    unknown_field: str,
) -> None:
    schema_path = OMEGA_ROOT / "schema" / "mcp" / filename
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    validator.validate(document)

    invalid = deepcopy(document)
    if filename == "package_manifest.schema.json":
        invalid[unknown_field] = "powershell.exe"
    else:
        items_key = "tools" if filename == "tool_catalog.schema.json" else "resources"
        item = invalid[items_key][0]
        assert isinstance(item, dict)
        item[unknown_field] = "unexpected"
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)

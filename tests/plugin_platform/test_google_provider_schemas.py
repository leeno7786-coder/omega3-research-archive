"""Published Google packages must match captured official tool contracts."""

import json
import asyncio
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_platform.canonical import canonical_bytes
from plugin_platform.package_verifier import verify_plugin_package
from plugin_platform.trust import load_embedded_key_ring


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("provider", ["drive", "calendar", "gmail"])
def test_signed_google_tools_match_official_provider_input_and_output_schemas(provider):
    package = ROOT / "plugin_platform/marketplace/packages" / f"google.workspace.{provider}"
    verified = verify_plugin_package(package, load_embedded_key_ring(), "3.0.0")
    fixture = json.loads(
        (ROOT / f"tests/fixtures/google_mcp/{provider}_discovery.json").read_bytes()
    )
    app = verified.manifest.required_apps[0]
    assert fixture["endpoint"] == app.remote_origin
    authority = json.loads(verified.asset_bytes["tools/authority.json"])["apps"][app.app_id]
    assert (
        fixture["protocol_version"] == authority["runtime_compatibility"]["wire_protocol_version"]
    )
    assert fixture["server_info"] == {
        "name": authority["runtime_compatibility"]["missing_metadata"]["server_id"],
        "version": authority["runtime_compatibility"]["missing_metadata"]["server_version"],
    }
    observed = {tool["name"]: tool for tool in fixture["tools"]}
    assert set(authority["tools"]) == {cap.name for cap in app.capabilities}
    assert set(authority["tools"]) == set(observed)
    for capability in app.capabilities:
        tool = observed[capability.name]
        policy = authority["tools"][capability.name]
        assert policy["input_schema"] == tool["inputSchema"], capability.name
        assert policy["output_schema"] == tool["outputSchema"], capability.name
        assert capability.schema_sha256 == sha256(canonical_bytes(tool["inputSchema"])).hexdigest()
        assert (
            policy["output_schema_sha256"]
            == sha256(canonical_bytes(tool["outputSchema"])).hexdigest()
        )


@pytest.mark.parametrize("provider", ["drive", "calendar", "gmail"])
@pytest.mark.parametrize("mutation", [None, "missing", "schema", "duplicate"])
def test_google_provider_capture_completes_real_client_validation(provider, mutation):
    from mcp import MCPError, types
    from mcp_platform.app_registry import InstalledMcpAppRecord
    from mcp_platform.app_sources import TrustedRemoteAppSource
    from mcp_platform.client import (
        CurrentMcpClient,
        _issue_sdk_client_factory,
        _SDK_CLIENT_FACTORY_ISSUER,
    )
    from mcp_platform.client_contracts import (
        McpClientDeployment,
        McpTransport,
        DiscoverySource,
        McpClientError,
        McpClientErrorReason,
        McpOperationStatus,
    )
    from mcp_platform.plugin_connections import descriptor_for_record, _verified_plugin_authority
    from tests.mcp_platform.test_client import (
        _FakeSdkClient,
        _FakeSdkSession,
        _FakeSdkClientFactory,
        _list_tools_request,
    )

    package = ROOT / "plugin_platform/marketplace/packages" / f"google.workspace.{provider}"
    verified = verify_plugin_package(package, load_embedded_key_ring(), "3.0.0")
    app = verified.manifest.required_apps[0]
    descriptor = descriptor_for_record(
        InstalledMcpAppRecord.from_source(TrustedRemoteAppSource(app)),
        signed_capability_authority=_verified_plugin_authority(verified, app.app_id),
    )
    capture = json.loads(
        (ROOT / f"tests/fixtures/google_mcp/{provider}_discovery.json").read_bytes()
    )
    selected_name = app.capabilities[0].name
    selected = next(tool for tool in capture["tools"] if tool["name"] == selected_name)
    if mutation == "missing":
        capture["tools"].remove(selected)
    elif mutation == "schema":
        selected["inputSchema"] = {"type": "object", "properties": {}}
    elif mutation == "duplicate":
        capture["tools"].append(selected)
    session = _FakeSdkSession(descriptor)
    session.errors["discover"] = MCPError(types.METHOD_NOT_FOUND, "Not supported")
    session.pages["tools"] = [
        types.ListToolsResult(
            tools=[types.Tool.model_validate(t) for t in capture["tools"]],
            ttlMs=0,
            cacheScope="private",
        )
    ]

    class CapturedSdkClient(_FakeSdkClient):
        async def list_tools(self, *, cursor=None, cache_mode):
            assert cache_mode in {"use", "bypass"}
            params = None if cursor is None else types.PaginatedRequestParams(cursor=cursor)
            return await self.session.list_tools(params=params)

    sdk_client = CapturedSdkClient(session)
    sdk_client.protocol_version = capture["protocol_version"]
    sdk_client.server_info = SimpleNamespace(**capture["server_info"])
    sdk_client.server_capabilities = types.ServerCapabilities.model_validate(
        capture["server_capabilities"]
    )
    builder = _FakeSdkClientFactory(sdk_client)
    factory = _issue_sdk_client_factory(
        _SDK_CLIENT_FACTORY_ISSUER,
        deployment=McpClientDeployment.REMOTE,
        transport=McpTransport.STREAMABLE_HTTP,
        builder=builder,
        builder_identity=builder,
        discovery_source=DiscoverySource.LIVE_AUTHENTICATED,
        descriptor=descriptor,
    )
    client = CurrentMcpClient(descriptor, factory)

    async def exercise():
        try:
            if mutation is not None:
                with pytest.raises(McpClientError) as raised:
                    await client.start()
                assert raised.value.reason is McpClientErrorReason.CAPABILITY_DRIFT
                return
            receipt = await client.start()
            assert set(receipt.tools) == {cap.name for cap in app.capabilities}
            assert receipt.signed_capability_authority == descriptor.signed_capability_authority
            listed = await client.list_tools(_list_tools_request(descriptor, "google-tools-list"))
            assert listed.status is McpOperationStatus.COMPLETED
            assert {item["name"] for item in listed.data["tools"]} == set(receipt.tools)
        finally:
            await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("provider", ["drive", "calendar", "gmail"])
def test_google_update_requires_full_capability_and_scope_review(provider, tmp_path):
    from mcp_platform.plugin_lifecycle import _update_permission_changes
    from plugin_platform.catalog import verify_plugin_catalog

    prior_files = json.loads((ROOT / "tests/fixtures/google_mcp/release_1_0_2.json").read_bytes())
    for relative, content in prior_files.items():
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content.encode("utf-8"))
    ring = load_embedded_key_ring()
    prior_catalog = verify_plugin_catalog(tmp_path / "catalog.json", ring)
    current_catalog = verify_plugin_catalog(ROOT / "plugin_platform/marketplace/catalog.json", ring)
    assert prior_catalog.sequence == 8
    assert current_catalog.sequence > prior_catalog.sequence
    prior_entries = {entry.plugin_id: entry for entry in prior_catalog.entries}
    current_entries = {entry.plugin_id: entry for entry in current_catalog.entries}
    # This used to assert GitHub's entry was untouched, as a control that a
    # Google update moved nothing else. GitHub has since had releases of its
    # own -- 1.0.5 corrected an authorization_issuer it could not resolve a
    # profile with -- so the control now measures the distance between two
    # releases rather than the blast radius of this one. The test is about the
    # Google package it parametrises; that is what it checks below.
    plugin_id = f"google.workspace.{provider}"
    old = verify_plugin_package(tmp_path / "packages" / plugin_id, ring, "3.0.0")
    new = verify_plugin_package(
        ROOT / "plugin_platform/marketplace/packages" / plugin_id, ring, "3.0.0"
    )
    # Every Google package moved again on 2026-09-08, when `allowed_mime_types`
    # was widened to the pair MCP servers actually return -- a text block
    # alongside `structuredContent`. Before that the result gate refused every
    # tool call as `result_invalid`. Gmail had already reached 1.0.4 with its
    # encoding fix and 1.0.5 for a sentence Google added to `get_thread`;
    # Calendar reached 1.0.4 when Google changed seven output schemas.
    current_version = {"gmail": "1.0.6", "calendar": "1.0.5"}.get(provider, "1.0.4")
    assert (old.manifest.plugin_version, new.manifest.plugin_version) == (
        "1.0.2",
        current_version,
    )
    old_app, new_app = old.manifest.required_apps[0], new.manifest.required_apps[0]
    if provider == "gmail":
        assert set(new_app.authentication.scopes) == {
            *old_app.authentication.scopes,
            "https://www.googleapis.com/auth/gmail.modify",
        }
    else:
        assert old_app.authentication == new_app.authentication
    assert {cap.name for cap in old_app.capabilities} <= {cap.name for cap in new_app.capabilities}
    assert old_app.remote_origin == new_app.remote_origin
    assert old_app.authorization_issuer == new_app.authorization_issuer
    old_tools = json.loads(old.asset_bytes["tools/authority.json"])["apps"][old_app.app_id]["tools"]
    new_tools = json.loads(new.asset_bytes["tools/authority.json"])["apps"][new_app.app_id]["tools"]
    # `allowed_mime_types` moved for every tool in the 2026-09-08 release, so it
    # is compared separately below rather than making every tool look rewritten.
    schema_fields = {
        "input_schema",
        "input_schema_sha256",
        "output_schema",
        "output_schema_sha256",
        "allowed_mime_types",
    }
    for name in old_tools:
        assert {k: v for k, v in old_tools[name].items() if k not in schema_fields} == {
            k: v for k, v in new_tools[name].items() if k not in schema_fields
        }
    changes = _update_permission_changes(
        old,
        new,
        installed_provenance=None,
        candidate_provenance=None,
    )
    assert f"capabilities_changed:{new_app.app_id}" in changes
    assert (f"scopes_changed:{new_app.app_id}" in changes) is (provider == "gmail")
    assert ("1.0.2", old.package_sha256) in {
        (entry.plugin_version, entry.package_sha256)
        for entry in current_entries[plugin_id].release_history
    }


def test_gmail_encoding_update_preserves_permissions_and_requires_schema_review(tmp_path):
    from mcp_platform.plugin_lifecycle import _update_permission_changes
    from plugin_platform.catalog import verify_plugin_catalog

    files = json.loads((ROOT / "tests/fixtures/google_mcp/gmail_release_1_0_3.json").read_bytes())
    for relative, content in files.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
    ring = load_embedded_key_ring()
    old = verify_plugin_package(tmp_path / "packages/google.workspace.gmail", ring, "3.0.0")
    new = verify_plugin_package(
        ROOT / "plugin_platform/marketplace/packages/google.workspace.gmail", ring, "3.0.0"
    )
    assert (old.manifest.plugin_version, new.manifest.plugin_version) == ("1.0.3", "1.0.6")
    old_app, new_app = old.manifest.required_apps[0], new.manifest.required_apps[0]
    assert old_app.authentication == new_app.authentication
    old_authority = json.loads(old.asset_bytes["tools/authority.json"])
    new_authority = json.loads(new.asset_bytes["tools/authority.json"])
    old_tools = old_authority["apps"][old_app.app_id]["tools"]
    new_tools = new_authority["apps"][new_app.app_id]["tools"]
    assert old_tools.keys() == new_tools.keys()
    # 1.0.4 corrected `search_threads`; 1.0.5 followed Google adding a sentence
    # to `get_thread`. Two releases, two tools, and nothing else moved -- which
    # is what this control is for.
    rewritten = {"search_threads", "get_thread"}
    # Every tool's `allowed_mime_types` widened in the 2026-09-08 release, so
    # that field is checked once for all of them and excluded from the
    # tool-by-tool comparison this control is actually about.
    assert all(
        tool["allowed_mime_types"] == ["application/json", "text/plain"]
        for tool in new_tools.values()
    )
    for name in old_tools:
        if name not in rewritten:
            assert {k: v for k, v in old_tools[name].items() if k != "allowed_mime_types"} == {
                k: v for k, v in new_tools[name].items() if k != "allowed_mime_types"
            }, name
    for name in sorted(rewritten):
        before, after = old_tools[name], new_tools[name]
        for key in set(before) - {
            "input_schema",
            "input_schema_sha256",
            "allowed_mime_types",
        }:
            assert before[key] == after[key], name
        assert before["input_schema_sha256"] != after["input_schema_sha256"], name
    # This is provider-authored Unicode, not the Windows locale's mojibake.
    description = new_tools["search_threads"]["input_schema"]["properties"]["query"]["description"]
    assert "`from:` \u2014 Sent from a specific person." in description
    # The whole of 1.0.5: one sentence Google appended, inside a description.
    thread_format = new_tools["get_thread"]["input_schema"]["properties"]["messageFormat"]
    assert thread_format["description"].endswith("`RAW` format is not supported here.")
    assert (
        old_tools["get_thread"]["input_schema"]["properties"]["messageFormat"]["enum"]
        == thread_format["enum"]
    )
    assert _update_permission_changes(
        old, new, installed_provenance=None, candidate_provenance=None
    ) == ("manifest_changed", "package_changed", f"capabilities_changed:{new_app.app_id}")
    prior_catalog = verify_plugin_catalog(tmp_path / "catalog.json", ring)
    current_catalog = verify_plugin_catalog(ROOT / "plugin_platform/marketplace/catalog.json", ring)
    # Packages added after the prior release (slack.official) are new entries,
    # not changes to the ones this review covers.
    added_since_prior = {entry.plugin_id for entry in current_catalog.entries} - {
        entry.plugin_id for entry in prior_catalog.entries
    }
    # Whatever has been added since the fixture, not a pinned name: this test
    # is about what the Gmail update changed, and a plugin released later is
    # not that.
    assert "slack.official" in added_since_prior
    # Excluded by name, not by weakening the comparison: each had its own
    # release since this fixture, so each is outside this review's blast radius.
    # github.official 1.0.5 corrected an authorization_issuer it could not
    # resolve an OAuth profile with; google.workspace.calendar 1.0.4 followed
    # Google changing the output schemas of seven of its nine tools.
    # google.workspace.drive is still compared exactly, which is what the
    # control is for.
    # This control used to assert that every OTHER catalog entry was
    # byte-identical to the 1.0.3-era fixture. That premise is gone: the
    # 2026-09-08 release widened `allowed_mime_types` on every package at once,
    # so every entry legitimately carries a new version and new digests.
    #
    # What the control was really protecting is still checked, and is the part
    # that matters -- a release must never quietly change a plugin's IDENTITY
    # or its trust tier. Version, digests, URI, release history and timestamp
    # are exactly what a release is allowed to move.
    prior_entries = {entry.plugin_id: entry for entry in prior_catalog.entries}
    current_entries = {entry.plugin_id: entry for entry in current_catalog.entries}
    released_fields = {
        "plugin_version",
        "package_uri",
        "manifest_sha256",
        "package_sha256",
        "provenance_sha256",
        "release_history",
        "released_at",
    }

    def _identity(entry: object) -> dict[str, object]:
        return {
            key: value
            for key, value in entry.to_dict().items()
            if key not in released_fields
        }

    for plugin_id, prior_entry in prior_entries.items():
        # The package under test is the one whose listing this release changed.
        if plugin_id in added_since_prior or plugin_id == old.manifest.plugin_id:
            continue
        assert _identity(prior_entry) == _identity(current_entries[plugin_id]), plugin_id
    entry = next(entry for entry in current_catalog.entries if entry.plugin_id == new.manifest.plugin_id)
    assert ("1.0.3", old.package_sha256) in {
        (release.plugin_version, release.package_sha256) for release in entry.release_history
    }

"""Signed provider refreshes must connect without weakening identity or consent."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import types

from mcp_platform.app_registry import InstalledMcpAppRecord
from mcp_platform.app_sources import TrustedRemoteAppSource
from mcp_platform.client import (
    CurrentMcpClient,
    _issue_sdk_client_factory,
    _SDK_CLIENT_FACTORY_ISSUER,
)
from mcp_platform.client_contracts import (
    DiscoverySource,
    McpClientDeployment,
    McpClientError,
    McpClientErrorReason,
    McpOperationStatus,
    McpTransport,
)
from mcp_platform.plugin_connections import descriptor_for_record, _verified_plugin_authority
from mcp_platform.plugin_lifecycle import _update_permission_changes
from plugin_platform.catalog import verify_plugin_catalog
from plugin_platform.package_verifier import verify_plugin_package
from plugin_platform.trust import load_embedded_key_ring
from tests.mcp_platform.test_client import (
    _FakeSdkClient,
    _FakeSdkClientFactory,
    _FakeSdkSession,
    _list_tools_request,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/provider_refresh_20260912"
PROVIDERS = ("netlify", "huggingface")


def _package(provider, *, prior=False):
    root = FIXTURES / "prior" if prior else ROOT / "plugin_platform/marketplace"
    return verify_plugin_package(
        root / "packages" / f"{provider}.official", load_embedded_key_ring(), "3.0.0"
    )


def _capture(provider):
    return json.loads((FIXTURES / f"{provider}.json").read_bytes())


@pytest.mark.parametrize("provider", PROVIDERS)
def test_signed_provider_identity_and_tools_match_independent_capture(provider):
    """Reject an outdated signed identity or tool schema before shipping it."""
    package = _package(provider)
    app = package.manifest.required_apps[0]
    capture = _capture(provider)
    authority = json.loads(package.asset_bytes["tools/authority.json"])["apps"][app.app_id]
    compatibility = authority["runtime_compatibility"]
    assert app.remote_origin == capture["endpoint"]
    assert compatibility["wire_protocol_version"] == capture["protocol_version"]
    assert compatibility["missing_metadata"] == {
        "server_id": capture["server_info"]["name"],
        "server_version": capture["server_info"]["version"],
    }
    observed = {tool["name"]: tool for tool in capture["tools"]}
    assert set(authority["tools"]) == set(observed)
    for name, policy in authority["tools"].items():
        assert policy["input_schema"] == observed[name]["inputSchema"], name
        assert policy["output_schema"] == observed[name].get(
            "outputSchema", {"type": "object", "additionalProperties": True}
        ), name
    # A refreshed tool contract must not grant the provider's separate document surface.
    assert all(cap.kind.value == "tool" for cap in app.capabilities)
    assert authority["resources"] == authority["resource_templates"] == authority["prompts"] == {}


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("mutation", [None, "name", "version", "schema", "missing"])
def test_captured_provider_passes_real_client_but_identity_and_schema_drift_fail(provider, mutation):
    """Replay only the provider boundary; retain signed descriptor and real discovery gates."""
    package = _package(provider)
    app = package.manifest.required_apps[0]
    descriptor = descriptor_for_record(
        InstalledMcpAppRecord.from_source(TrustedRemoteAppSource(app)),
        signed_capability_authority=_verified_plugin_authority(package, app.app_id),
    )
    capture = _capture(provider)
    if mutation in {"name", "version"}:
        capture["server_info"][mutation] = "wrong-provider-identity"
    elif mutation == "schema":
        capture["tools"][0]["inputSchema"] = {"type": "object", "properties": {}}
    elif mutation == "missing":
        capture["tools"].pop(0)
    session = _FakeSdkSession(descriptor)
    session.pages["tools"] = [
        types.ListToolsResult(
            tools=[types.Tool.model_validate(tool) for tool in capture["tools"]],
            ttlMs=0,
            cacheScope="private",
        )
    ]
    # Hugging Face also advertises 155 documentation resources. None is signed
    # into this tool-only package, so requesting that surface is a scope failure.
    for group in ("resources", "resource_templates", "prompts"):
        session.errors[f"list_{group}"] = AssertionError("undeclared provider surface requested")

    class CapturedClient(_FakeSdkClient):
        async def list_tools(self, *, cursor=None, cache_mode):
            assert cache_mode in {"use", "bypass"}
            params = None if cursor is None else types.PaginatedRequestParams(cursor=cursor)
            return await self.session.list_tools(params=params)

    connected = CapturedClient(session)
    connected.protocol_version = capture["protocol_version"]
    connected.server_info = SimpleNamespace(**capture["server_info"])
    connected.server_capabilities = types.ServerCapabilities.model_validate(capture["server_capabilities"])
    builder = _FakeSdkClientFactory(connected)
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
                expected = (
                    McpClientErrorReason.IDENTITY_MISMATCH
                    if mutation in {"name", "version"}
                    else McpClientErrorReason.CAPABILITY_DRIFT
                )
                assert raised.value.reason is expected
                assert client.drift_status is not None
                assert client.drift_status.phase == "discovery"
                assert client.drift_status.reason is expected
                assert client.drift_status.retry_attempted is False
                return
            receipt = await client.start()
            assert (receipt.server_id, receipt.server_version) == (
                capture["server_info"]["name"], capture["server_info"]["version"]
            )
            assert set(receipt.tools) == {cap.name for cap in app.capabilities}
            assert not receipt.resources and not receipt.resource_templates and not receipt.prompts
            result = await client.list_tools(_list_tools_request(descriptor, "provider-refresh-list"))
            assert result.status is McpOperationStatus.COMPLETED
            assert {tool["name"] for tool in result.data["tools"]} == set(receipt.tools)
        finally:
            await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("provider", PROVIDERS)
def test_refresh_changes_authority_and_requires_review_without_widening_scopes(provider):
    """A release must invalidate old capability consent, not silently inherit it."""
    old, new = _package(provider, prior=True), _package(provider)
    old_app, new_app = old.manifest.required_apps[0], new.manifest.required_apps[0]
    changes = _update_permission_changes(
        old, new, installed_provenance=None, candidate_provenance=None
    )
    assert "package_changed" in changes
    assert f"capabilities_changed:{new_app.app_id}" in changes
    assert new_app.authentication == old_app.authentication
    assert new_app.remote_origin == old_app.remote_origin
    assert new_app.authorization_issuer == old_app.authorization_issuer
    assert {(cap.kind, cap.name) for cap in new_app.capabilities} == {
        (cap.kind, cap.name) for cap in old_app.capabilities
    }
    ring = load_embedded_key_ring()
    prior_catalog = verify_plugin_catalog(FIXTURES / "prior/catalog.json", ring)
    current_catalog = verify_plugin_catalog(ROOT / "plugin_platform/marketplace/catalog.json", ring)
    old_entry = next(entry for entry in prior_catalog.entries if entry.plugin_id == old.manifest.plugin_id)
    new_entry = next(entry for entry in current_catalog.entries if entry.plugin_id == new.manifest.plugin_id)
    assert old_entry.package_sha256 == old.package_sha256
    assert new_entry.package_sha256 == new.package_sha256
    assert current_catalog.sequence > prior_catalog.sequence


def test_netlify_new_operation_is_explicitly_reviewed_in_the_signed_update() -> None:
    """The environment-variable reader is a reviewed capability, not drift repair."""
    prior = _package("netlify", prior=True)
    current = _package("netlify")
    prior_authority = json.loads(prior.asset_bytes["tools/authority.json"])["apps"][
        prior.manifest.required_apps[0].app_id
    ]
    current_authority = json.loads(current.asset_bytes["tools/authority.json"])["apps"][
        current.manifest.required_apps[0].app_id
    ]
    prior_schema = prior_authority["tools"]["netlify-team-services-reader"]["input_schema"]
    current_schema = current_authority["tools"]["netlify-team-services-reader"]["input_schema"]
    prior_operations = {
        item["properties"]["operation"]["const"]
        for item in prior_schema["properties"]["selectSchema"]["anyOf"]
    }
    current_operations = {
        item["properties"]["operation"]["const"]
        for item in current_schema["properties"]["selectSchema"]["anyOf"]
    }
    assert "get-team-env-vars" not in prior_operations
    assert "get-team-env-vars" in current_operations
    assert current.package_sha256 != prior.package_sha256


def test_huggingface_version_and_schema_description_drift_is_repaired_by_signed_update() -> None:
    """HF's harmless description drift is pinned to the refreshed signed package."""
    prior = _package("huggingface", prior=True)
    current = _package("huggingface")
    prior_authority = json.loads(prior.asset_bytes["tools/authority.json"])["apps"][
        prior.manifest.required_apps[0].app_id
    ]
    current_authority = json.loads(current.asset_bytes["tools/authority.json"])["apps"][
        current.manifest.required_apps[0].app_id
    ]
    capture = _capture("huggingface")
    assert current.manifest.required_apps[0].app_id == prior.manifest.required_apps[0].app_id
    assert current_authority["runtime_compatibility"]["missing_metadata"]["server_version"] == capture[
        "server_info"
    ]["version"]
    observed = {tool["name"]: tool for tool in capture["tools"]}
    assert current_authority["tools"] != prior_authority["tools"]
    assert all(
        policy["input_schema"] == observed[name]["inputSchema"]
        for name, policy in current_authority["tools"].items()
    )
    assert current.package_sha256 != prior.package_sha256

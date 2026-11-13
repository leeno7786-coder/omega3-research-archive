from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_platform.oauth_broker import OAuthBroker
from mcp_platform.oauth_vault import OAuthCredentialVault
from mcp_platform.plugin_backend import CurrentMcpPluginBackend
from mcp_platform.plugin_authority import PluginAuthorityStore
from mcp_platform.plugin_catalog import (
    PluginCatalogAppAuthority,
    compile_plugin_runtime_catalog,
)
from mcp_platform.plugin_connections import (
    PluginConnectionService,
    descriptor_for_record,
    signed_capability_authority_from_package,
)
from mcp_platform.plugin_grants import PluginGrantService
from mcp_platform.plugin_execution_worker import PluginExecutionWorker
from mcp_platform.plugin_runtime import (
    PluginOperationRequest,
    PluginRuntime,
    PluginRuntimeOperation,
)
from tests.helpers.plugin_consent import full_connection_grant
from tests.mcp_platform.generic_plugin_install import install_signed_northwind_plugins
from tests.mcp_platform.generic_plugin_peers import TRACKER_ORIGIN, ZERO_TOOL_PEERS
from tests.mcp_platform.test_oauth_broker import _Clock as _RealClock
from tests.mcp_platform.test_oauth_broker import _Protocol as _RealProtocol
from tests.mcp_platform.test_plugin_connections import (
    ACCOUNT_KEY,
    _Client,
    _ExactProfileRegistry,
    _callback_uri,
    _direct_submit,
    _profile_match,
    _real_profile,
)


def _surface_worker_operation(
    body: dict[str, object], credential: dict[str, object]
) -> dict[str, object]:
    assert set(credential) == {"access_token"}
    descriptor = body["descriptor"]
    if body.get("discovery") is True:
        signed = descriptor["signed_capability_authority"]
        return {
            "discovery": {
                "app_id": descriptor["app_id"],
                "publisher_id": descriptor["publisher_id"],
                "deployment": descriptor["deployment"],
                "transport": descriptor["transport"],
                "protocol_version": descriptor["protocol_version"],
                "source": "live_authenticated",
                "server_id": descriptor["app_id"],
                "server_version": "1.0.0",
                "tools": {name: {} for name in signed["tools"]},
                "prompts": {name: {} for name in signed["prompts"]},
                "resources": {name: {} for name in signed["resources"]},
                "resource_templates": {
                    name: {} for name in signed["resource_templates"]
                },
                "extensions": {},
                "cache_metadata": {},
                "capability_revision": descriptor["capability_revision"],
                "signed_capability_authority": signed,
            }
        }
    authority = body["worker_authority"]
    request = body["request"]
    kind = authority["capability_kind"]
    capability = authority["capability_id"]
    arguments = request["arguments"]
    if kind == "resource":
        policy = descriptor["signed_capability_authority"]["resources"][capability]
        data = {
            "contents": [
                {
                    "uri": policy["uri"],
                    "mime_type": policy["mime_type"],
                    "text": '{"record":"current"}',
                }
            ]
        }
    elif kind == "resource_template":
        policy = descriptor["signed_capability_authority"]["resource_templates"][
            capability
        ]
        data = {
            "contents": [
                {
                    "uri": arguments["uri"],
                    "mime_type": policy["mime_type"],
                    "text": '{"record":"selected"}',
                }
            ]
        }
    else:
        assert kind == "prompt"
        data = {
            "description": "Signed record summary",
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": f"Summarize {arguments['topic']}",
                    },
                }
            ],
        }
    return {
        "request_id": request["request_id"],
        "app_id": request["app_id"],
        "capability_id": request["capability_id"],
        "capability_revision": request["capability_revision"],
        "status": "completed",
        "data": data,
    }


def _worker_submit(operation, **_kwargs):
    return asyncio.run(operation())


def _service(
    tmp_path: Path,
    *,
    installed: dict[str, object],
    peer: str,
    clock: _RealClock,
    protocol: _RealProtocol,
) -> PluginConnectionService:
    plugin_id, _app_id, _kind, _capability_id = ZERO_TOOL_PEERS[peer]
    registry = installed["plugin_registry"]
    app_registry = installed["app_registry"]
    package_registry = installed["package_registry"]
    vault = OAuthCredentialVault(
        tmp_path / "zero-tool-vault",
        key_provider=lambda: b"z" * 32,
        clock=clock,
    )
    broker = OAuthBroker(
        registry=_ExactProfileRegistry(
            _real_profile(resource_origin=f"{TRACKER_ORIGIN}/mcp/v1")
        ),
        vault=vault,
        protocol=protocol,
        clock=clock,
    )
    grants = PluginGrantService(
        broker=broker,
        authority_store=PluginAuthorityStore(path=tmp_path / "zero-tool-authority.json"),
        permission_store=SimpleNamespace(invalidate_plugin=lambda _plugin_id: 0),
        journal_path=tmp_path / "zero-tool-grants.json",
    )

    def descriptor(_plugin_id: str, app_id: str):
        record = app_registry.get(app_id)
        authority = signed_capability_authority_from_package(
            package_registry,
            record,
            plugin_registry=registry,
            plugin_id=plugin_id,
        )
        return descriptor_for_record(record, signed_capability_authority=authority)

    return PluginConnectionService(
        app_registry=app_registry,
        plugin_registry=registry,
        broker=broker,
        grant_service=grants,
        descriptor_provider=descriptor,
        requirement_provider=lambda _plugin_id, _record: (
            registry.reverify_installed(plugin_id).manifest.required_apps[0]
        ),
        offered_scopes_provider=lambda _plugin_id, _app_id: ("surface.read",),
        profile_match_provider=_profile_match,
        operation_submitter=_direct_submit,
        client_factory=lambda _descriptor, _authorization: _Client(()),
        execution_worker=PluginExecutionWorker(
            child_handler=_surface_worker_operation,
            shutdown_timeout_seconds=10.0,
        ),
    )


@pytest.mark.anyio
@pytest.mark.parametrize("peer", ("resource", "resource_template", "prompt"))
async def test_signed_zero_tool_app_connects_restarts_and_prepares_exact_worker_operation(
    tmp_path: Path,
    peer: str,
) -> None:
    plugin_id, app_id, capability_kind, capability_id = ZERO_TOOL_PEERS[peer]
    installed = install_signed_northwind_plugins(
        tmp_path,
        peers=(peer,),
        tracker_authentication_kind="oauth_authorization_code_pkce",
        tracker_remote_origin=f"{TRACKER_ORIGIN}/mcp/v1",
        tracker_scopes=("surface.read",),
    )
    clock = _RealClock()
    protocol = _RealProtocol()
    service = _service(
        tmp_path,
        installed=installed,
        peer=peer,
        clock=clock,
        protocol=protocol,
    )

    started = await service.start(
        plugin_id,
        app_id=app_id,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, plugin_id, app_id),
    )
    connected = await service.complete(
        started.request_id or "",
        _callback_uri(
            started,
            _real_profile(resource_origin=f"{TRACKER_ORIGIN}/mcp/v1"),
        ),
    )

    assert connected.status == "connected"
    assert connected.capability_ceiling == ()
    activation = service._grants.current(
        plugin_id,
        app_id=app_id,
        account_key_sha256=connected.account_key_sha256,
    ).activation
    assert tuple(
        identity.capability_kind
        for identity in activation.capability_binding_identities
    ) == (capability_kind,)
    await service.close_all()
    await asyncio.to_thread(service._execution_worker.close)

    restarted = _service(
        tmp_path,
        installed=installed,
        peer=peer,
        clock=clock,
        protocol=protocol,
    )
    preparation = await restarted.prepare_execution_worker(
        plugin_id,
        app_id=app_id,
        account_key=ACCOUNT_KEY,
        capability_kind=capability_kind,
        capability_id=capability_id,
    )

    assert preparation.authority.plugin_id == plugin_id
    assert preparation.authority.app_id == app_id
    assert preparation.authority.capability_kind == capability_kind
    assert preparation.authority.capability_id == capability_id
    assert set(preparation.credential) == {"access_token"}

    def current_descriptor(_plugin_id: str, candidate_app_id: str):
        record = installed["app_registry"].get(candidate_app_id)
        authority = signed_capability_authority_from_package(
            installed["package_registry"],
            record,
            plugin_registry=installed["plugin_registry"],
            plugin_id=plugin_id,
        )
        return descriptor_for_record(record, signed_capability_authority=authority)

    catalog = compile_plugin_runtime_catalog(
        plugin_snapshot=installed["plugin_registry"].snapshot(),
        app_snapshot=installed["app_registry"].snapshot(),
        descriptor_for_app=current_descriptor,
        account_key_hash=connected.account_key_sha256,
        app_authority_provider=lambda *_args: PluginCatalogAppAuthority(
            app_id=app_id,
            account_key_hash=connected.account_key_sha256,
            connection_revision=preparation.authority.connection_revision,
            authority_revision=preparation.authority.authority_revision,
            capability_revision=preparation.authority.capability_revision,
        ),
    )
    binding = next(
        entry
        for entry in catalog.entries
        if entry.capability_kind == capability_kind
        and entry.capability == capability_id
    )
    operation = (
        PluginRuntimeOperation.PROMPT_GET
        if capability_kind == "prompt"
        else PluginRuntimeOperation.RESOURCE_READ
    )
    arguments = (
        {"topic": "quarterly"}
        if capability_kind == "prompt"
        else {"uri": "omega://records/42"}
        if capability_kind == "resource_template"
        else {}
    )
    backend = CurrentMcpPluginBackend(
        connection_service=restarted,
        descriptor_provider=current_descriptor,
        plugin_id_for_app={app_id: plugin_id},
        account_key_provider=lambda: ACCOUNT_KEY,
        operation_submitter=_worker_submit,
        execution_worker=restarted._execution_worker,
    )
    runtime = PluginRuntime(
        plugin_registry=installed["plugin_registry"],
        app_registry=installed["app_registry"],
        backend=backend,
        catalog_provider=lambda: catalog,
    )
    request = PluginOperationRequest(
        plugin_id=plugin_id,
        app_id=app_id,
        capability=capability_id,
        capability_kind=capability_kind,
        operation=operation,
        arguments=arguments,
        candidate_id=binding.candidate_id,
        account_key_hash=binding.account_key_hash,
        package_revision=binding.package_revision,
        connection_revision=binding.connection_revision,
        catalog_authority_revision=binding.authority_revision,
        capability_revision=binding.capability_revision,
        binding_revision=binding.binding_revision,
    )

    result = await asyncio.to_thread(runtime.execute, request)

    assert result.status == "completed"
    assert result.capability_kind == capability_kind
    assert result.attribution["candidate_id"] == binding.candidate_id
    await asyncio.to_thread(backend.close)

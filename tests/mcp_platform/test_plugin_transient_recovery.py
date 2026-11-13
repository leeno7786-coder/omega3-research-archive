"""An unavailable worker must not withdraw the operator's durable consent."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_platform.oauth_broker import OAuthBroker
from mcp_platform.oauth_vault import OAuthCredentialVault
from mcp_platform.plugin_authority import PluginAuthorityStore
from mcp_platform.plugin_connections import PluginConnectionCoordinator, PluginConnectionService
from mcp_platform.plugin_execution_worker import PluginExecutionWorkerError
from mcp_platform.plugin_grants import PluginGrantService
from tests.helpers.plugin_consent import full_connection_grant
from tests.mcp_platform.generic_plugin_install import (
    descriptor_from_installed,
    install_signed_northwind_plugins,
)
from tests.mcp_platform.generic_plugin_peers import TRACKER_APP, TRACKER_PLUGIN
from tests.mcp_platform.test_plugin_connections import (
    ACCOUNT_DIGEST,
    ACCOUNT_KEY,
    SCOPES,
    _DiscoveryWorker,
    _ExactProfileRegistry,
    _RealClock,
    _RealProtocol,
    _callback_uri,
    _direct_submit,
    _profile_match,
    _real_profile,
)


class _InterruptibleDiscoveryWorker(_DiscoveryWorker):
    failure: BaseException | None = None
    fail_on_call: int | None = None
    calls = 0

    async def run_async(self, **kwargs):
        self.calls += 1
        if self.failure is not None and (
            self.fail_on_call is None or self.calls == self.fail_on_call
        ):
            raise self.failure
        return await super().run_async(**kwargs)


def _service_factory(tmp_path: Path, *, credentialless: bool = False):
    """Keep actual registries, encrypted vault and grants across service restarts."""

    installed = install_signed_northwind_plugins(
        tmp_path,
        peers=("tracker",),
        tracker_authentication_kind=(
            "none" if credentialless else "oauth_authorization_code_pkce"
        ),
    )
    profile = _real_profile()
    clock = _RealClock()
    protocol = _RealProtocol()
    worker = _InterruptibleDiscoveryWorker()

    def build():
        broker = OAuthBroker(
            registry=_ExactProfileRegistry(profile),
            vault=OAuthCredentialVault(
                tmp_path / "oauth-vault",
                key_provider=lambda: b"v" * 32,
                clock=clock,
            ),
            protocol=protocol,
            clock=clock,
        )
        grants = PluginGrantService(
            broker=broker,
            authority_store=PluginAuthorityStore(path=tmp_path / "authority.json"),
            permission_store=SimpleNamespace(invalidate_plugin=lambda _plugin_id: 0),
            journal_path=tmp_path / "grants.json",
        )
        registry = installed["plugin_registry"]
        return PluginConnectionService(
            app_registry=installed["app_registry"],
            plugin_registry=registry,
            broker=broker,
            grant_service=grants,
            descriptor_provider=lambda _plugin_id, app_id: descriptor_from_installed(
                installed["app_registry"], installed["package_registry"], app_id
            ),
            requirement_provider=lambda _plugin_id, _record: registry.reverify_installed(
                TRACKER_PLUGIN
            ).manifest.required_apps[0],
            offered_scopes_provider=lambda _plugin_id, _app_id: SCOPES,
            profile_match_provider=_profile_match,
            operation_submitter=_direct_submit,
            execution_worker=worker,
        )

    return build, worker, protocol, profile


async def _connect(service, profile) -> None:
    started = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    if started.status == "authorizing":
        connected = await service.complete(
            started.request_id, _callback_uri(started, profile)
        )
        assert connected.status == "connected"
    else:
        assert started.status == "connected"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [
        PluginExecutionWorkerError("plugin_operation_timeout"),
        PluginExecutionWorkerError("plugin_worker_unavailable"),
        PluginExecutionWorkerError("plugin_operation_cancelled"),
        PluginExecutionWorkerError("plugin_operation_in_progress"),
        PluginExecutionWorkerError("plugin_runtime_closed"),
        PluginExecutionWorkerError("plugin_worker_reap_failed"),
        PluginExecutionWorkerError("plugin_worker_reap_pending"),
        ValueError("plugin_transport_unavailable"),
        ValueError("oauth_provider_unreachable"),
        asyncio.CancelledError(),
    ],
    ids=lambda failure: str(failure) or type(failure).__name__,
)
async def test_failed_live_probe_preserves_exact_durable_grant_for_restart(
    tmp_path: Path, failure: BaseException
) -> None:
    build, worker, protocol, profile = _service_factory(tmp_path)
    service = build()
    await _connect(service, profile)
    before = (tmp_path / "grants.json").read_bytes()
    authority = service.authority_store.connection(
        TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST
    )
    worker.failure = failure

    expected_error = (
        asyncio.CancelledError
        if isinstance(failure, asyncio.CancelledError)
        else (ValueError, PluginExecutionWorkerError)
    )
    with pytest.raises(expected_error, match=f"^{str(failure)}$"):
        await service.prepare_authority_context(
            TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY
        )

    assert (tmp_path / "grants.json").read_bytes() == before
    assert service.authority_store.connection(
        TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST
    ) == authority
    assert (TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST) not in service._sessions
    worker.failure = None
    restarted = build()
    recovered = await restarted.prepare_authority_context(
        TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY
    )
    assert recovered.authority == authority
    assert recovered.server_id == TRACKER_APP
    assert len(protocol.exchange_calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("credentialless", [False, True])
@pytest.mark.parametrize("cold_session", [False, True])
@pytest.mark.parametrize("entrypoint", ["prepare_authority_context", "ensure_client"])
async def test_warm_and_cold_sessions_recover_without_renewing_consent(
    tmp_path: Path, credentialless: bool, cold_session: bool, entrypoint: str
) -> None:
    build, worker, protocol, profile = _service_factory(
        tmp_path, credentialless=credentialless
    )
    service = build()
    await _connect(service, profile)
    authority = service.authority_store.connection(
        TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST
    )
    if cold_session:
        service = build()
    worker.failure = PluginExecutionWorkerError("plugin_operation_timeout")
    with pytest.raises(ValueError, match="^plugin_operation_timeout$"):
        await getattr(service, entrypoint)(
            TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY
        )

    assert service.authority_store.connection(
        TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST
    ) == authority
    worker.failure = None
    recovered = await service.prepare_authority_context(
        TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY
    )
    assert recovered.authority == authority
    assert len(protocol.exchange_calls) == (0 if credentialless else 1)


@pytest.mark.anyio
async def test_repeated_connect_probe_does_not_withdraw_existing_consent(
    tmp_path: Path,
) -> None:
    build, worker, _protocol, profile = _service_factory(tmp_path)
    service = build()
    await _connect(service, profile)
    before = (tmp_path / "grants.json").read_bytes()
    worker.failure = PluginExecutionWorkerError("plugin_operation_timeout")

    with pytest.raises(ValueError, match="^plugin_operation_timeout$"):
        await service.start(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=ACCOUNT_KEY,
            **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
        )

    assert (tmp_path / "grants.json").read_bytes() == before


@pytest.mark.anyio
@pytest.mark.parametrize("entrypoint", ["service", "coordinator"])
@pytest.mark.parametrize("cold_restart", [False, True])
async def test_connect_reuses_exact_durable_consent_after_session_loss(
    tmp_path: Path, entrypoint: str, cold_restart: bool
) -> None:
    build, worker, protocol, profile = _service_factory(tmp_path)
    service = build()
    await _connect(service, profile)
    before = (tmp_path / "grants.json").read_bytes()
    if cold_restart:
        service = build()
    else:
        worker.failure = PluginExecutionWorkerError("plugin_operation_timeout")
        with pytest.raises(ValueError, match="^plugin_operation_timeout$"):
            await service.prepare_authority_context(
                TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY
            )
        worker.failure = None

    grant = full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP)
    if entrypoint == "service":
        started = await service.start(
            TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY, **grant
        )
    else:
        started = await PluginConnectionCoordinator(
            plugin_registry=service._plugin_registry, connection_service=service
        ).start(TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_KEY, None, **grant)

    assert started.status == "connected"
    assert started.request_id is None
    assert started.authorization_url is None
    assert started.authorization_flow is None
    assert (tmp_path / "grants.json").read_bytes() == before
    assert len(protocol.exchange_calls) == 1
    visible = await PluginConnectionCoordinator(
        plugin_registry=service._plugin_registry, connection_service=service
    ).status(TRACKER_PLUGIN, ACCOUNT_KEY)
    assert visible.enabled
    assert visible.state.value == "connected"


@pytest.mark.anyio
@pytest.mark.parametrize("changed", ["scopes", "policy", "constraints", "catalog"])
async def test_cold_connect_does_not_reuse_consent_for_changed_authority(
    tmp_path: Path, changed: str
) -> None:
    build, _worker, _protocol, profile = _service_factory(tmp_path)
    service = build()
    await _connect(service, profile)
    service = build()
    settings = full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP)
    if changed == "scopes":
        settings["scopes"] = ("issues.write",)
    elif changed == "policy":
        settings["execution_policy"] = "direct_turn"
    elif changed == "constraints":
        settings["source_constraints"] = {"workspace": "signed"}
    else:
        original = service._descriptor_provider
        service._descriptor_provider = lambda plugin_id, app_id: replace(
            original(plugin_id, app_id), capability_revision="9" * 64
        )

    started = await service.start(
        TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY, **settings
    )

    assert started.status == "authorizing"
    assert service._grants.current(
        TRACKER_PLUGIN, app_id=TRACKER_APP, account_key_sha256=ACCOUNT_DIGEST
    ) is None
    with pytest.raises(ValueError, match="^plugin_connection_authority_not_found$"):
        service.authority_store.connection(TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST)


@pytest.mark.anyio
@pytest.mark.parametrize("changed", ["challenge", "context", "scopes", "revoked"])
async def test_coordinator_rechecks_challenge_free_connected_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    build, _worker, _protocol, profile = _service_factory(tmp_path)
    service = build()
    await _connect(service, profile)
    grant = full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP)
    started = await service.start(
        TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY, **grant
    )
    if changed == "challenge":
        started = replace(started, request_id="d" * 64)
    elif changed == "context":
        started = replace(started, authorization_context_revision="d" * 64)
    elif changed == "scopes":
        started = replace(started, provider_scopes=("issues.read",))
    else:
        service._grants.suspend(
            TRACKER_PLUGIN, app_id=TRACKER_APP, account_key_sha256=ACCOUNT_DIGEST
        )

    async def inconsistent_start(*_args, **_kwargs):
        return started

    monkeypatch.setattr(service, "start", inconsistent_start)
    coordinator = PluginConnectionCoordinator(
        plugin_registry=service._plugin_registry, connection_service=service
    )
    with pytest.raises(ValueError, match="^plugin_authentication_mismatch$"):
        await coordinator.start(TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_KEY, None, **grant)


@pytest.mark.anyio
async def test_failure_after_grant_commit_preserves_approved_connection(
    tmp_path: Path,
) -> None:
    build, worker, protocol, profile = _service_factory(tmp_path)
    service = build()
    started = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    assert service._plugin_registry.get(TRACKER_PLUGIN).enabled
    # Authorization probes once before committing and once after committing.
    # Losing the latter must not roll back the operator's completed approval.
    worker.failure = PluginExecutionWorkerError("plugin_operation_timeout")
    worker.fail_on_call = 2
    with pytest.raises(ValueError, match="^plugin_operation_timeout$"):
        await service.complete(started.request_id, _callback_uri(started, profile))

    grant = service._grants.current(
        TRACKER_PLUGIN, app_id=TRACKER_APP, account_key_sha256=ACCOUNT_DIGEST
    )
    assert grant is not None
    worker.failure = None
    restarted = build()
    retried = await restarted.complete(
        started.request_id, _callback_uri(started, profile)
    )
    assert retried.status == "connected"
    visible = await PluginConnectionCoordinator(
        plugin_registry=restarted._plugin_registry,
        connection_service=restarted,
    ).status(TRACKER_PLUGIN, ACCOUNT_KEY)
    assert visible.enabled
    assert visible.state.value == "connected"
    recovered = await restarted.prepare_authority_context(
        TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY
    )
    assert recovered.authority == grant.authority
    assert len(protocol.exchange_calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("reason", ["oauth_reauthorization_required", "plugin_capability_invalid"])
async def test_authentication_and_catalog_drift_still_withdraw_durable_authority(
    tmp_path: Path, reason: str
) -> None:
    build, worker, _protocol, profile = _service_factory(tmp_path)
    service = build()
    await _connect(service, profile)
    worker.failure = ValueError(reason)
    with pytest.raises(ValueError, match=f"^{reason}$"):
        await service.prepare_authority_context(
            TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY
        )

    assert service._grants.current(
        TRACKER_PLUGIN, app_id=TRACKER_APP, account_key_sha256=ACCOUNT_DIGEST
    ) is None
    with pytest.raises(ValueError, match="^plugin_connection_authority_not_found$"):
        service.authority_store.connection(TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST)
    worker.failure = None
    restarted = build()
    with pytest.raises(ValueError, match="^plugin_connection_not_live$"):
        await restarted.prepare_authority_context(
            TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY
        )

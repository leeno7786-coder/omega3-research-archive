from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.oauth_broker import OAuthBroker
from mcp_platform.oauth_profiles import OAuthAuthorizationFlow
from mcp_platform.oauth_protocol import OAuthNetworkError
from mcp_platform.oauth_vault import OAuthCredentialVault
from mcp_platform.plugin_authority import PluginAuthorityStore
from mcp_platform.plugin_connections import (
    PluginConnectionCoordinator,
    PluginConnectionService,
    descriptor_for_record,
    signed_capability_authority_for_candidate,
)
from mcp_platform.plugin_dispatch import plugin_runtime_catalog
from mcp_platform.plugin_grants import PluginGrantService
from mcp_platform.plugin_lifecycle import (
    PluginLifecycleService,
    PluginUpdatePending,
    _LifecycleMutationBoundary,
)
from mcp_platform.plugin_state import PluginLifecycleState
from mcp_platform.state_store import McpStateStore
from plugin_platform.records import PluginAppBinding
from tests.helpers.plugin_consent import full_connection_grant, full_permission_settings
from tests.mcp_platform.generic_plugin_install import (
    install_signed_northwind_plugins,
    review_signed_northwind_candidate,
    stage_signed_northwind_candidate,
)
from tests.mcp_platform.test_plugin_connections import (
    ACCOUNT_DIGEST,
    ACCOUNT_KEY,
    CALENDAR_APP,
    CALENDAR_PLUGIN,
    SCOPES,
    TRACKER_APP,
    TRACKER_ORIGIN,
    TRACKER_PLUGIN,
    _callback_uri,
    _Client,
    _direct_submit,
    _DiscoveryWorker,
    _ExactProfileRegistry,
    _full_candidate_grant,
    _profile_match,
    _real_profile,
    _RealClock,
    _RealProtocol,
    _tool_documents,
)


def _real_candidate_service(
    tmp_path: Path,
    authentication_kind: str,
    *,
    optional_tracker: bool = False,
):
    plugin_id = CALENDAR_PLUGIN if optional_tracker else TRACKER_PLUGIN
    installed = install_signed_northwind_plugins(
        tmp_path,
        peers=("calendar", "tracker") if optional_tracker else ("tracker",),
        tracker_authentication_kind=authentication_kind,
        tracker_remote_origin=f"{TRACKER_ORIGIN}/mcp/v1",
        optional_tracker_plugin_ids=(plugin_id,) if optional_tracker else (),
    )
    authorization_flow = (
        OAuthAuthorizationFlow.DEVICE_CODE
        if authentication_kind == "oauth_device_code"
        else OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE
    )
    profile = _real_profile(
        resource_origin=f"{TRACKER_ORIGIN}/mcp/v1",
        authorization_flow=authorization_flow,
    )
    clock = _RealClock()
    protocol = _RealProtocol()
    vault = OAuthCredentialVault(
        tmp_path / "oauth-vault",
        key_provider=lambda: b"v" * 32,
        clock=clock,
    )
    broker = OAuthBroker(
        registry=_ExactProfileRegistry(profile),
        vault=vault,
        protocol=protocol,
        clock=clock,
    )
    authority_store = PluginAuthorityStore(path=tmp_path / "authority.json")
    grants = PluginGrantService(
        broker=broker,
        authority_store=authority_store,
        permission_store=SimpleNamespace(invalidate_plugin=lambda _plugin_id: 0),
        journal_path=tmp_path / "grants.json",
    )
    registry = installed["plugin_registry"]
    app_registry = installed["app_registry"]
    package_registry = installed["package_registry"]
    provisioner = installed["installer"].provisioner

    def live_descriptor(_plugin_id: str, app_id: str):
        record = app_registry.get(app_id)
        authority = signed_capability_authority_for_candidate(
            package_registry,
            record,
            registry.reverify_installed(plugin_id),
        )
        return descriptor_for_record(record, signed_capability_authority=authority)

    service = PluginConnectionService(
        app_registry=app_registry,
        plugin_registry=registry,
        broker=broker,
        grant_service=grants,
        descriptor_provider=live_descriptor,
        candidate_descriptor_provider=lambda verified, binding, app_id: (
            descriptor_for_record(
                provisioner.candidate_record(binding.dependency_receipt_id, app_id),
                signed_capability_authority=signed_capability_authority_for_candidate(
                    package_registry,
                    provisioner.candidate_record(
                        binding.dependency_receipt_id,
                        app_id,
                    ),
                    verified,
                ),
            )
        ),
        candidate_record_provider=lambda _verified, binding, app_id: (
            provisioner.candidate_record(binding.dependency_receipt_id, app_id)
        ),
        requirement_provider=lambda _plugin_id, record: next(
            requirement
            for requirement in (
                registry.reverify_installed(plugin_id).manifest.required_apps
                + registry.reverify_installed(plugin_id).manifest.optional_apps
            )
            if requirement.app_id == record.app_id
        ),
        offered_scopes_provider=lambda _plugin_id, _app_id: SCOPES,
        profile_match_provider=_profile_match,
        operation_submitter=_direct_submit,
        client_factory=lambda _descriptor, _authorization: _Client(
            _tool_documents("tracker")
        ),
        execution_worker=_DiscoveryWorker(),
    )
    return service, protocol, clock, profile, installed


@pytest.mark.anyio
async def test_signed_changed_retained_optional_authority_is_candidate_prepared(
    tmp_path: Path,
) -> None:
    service, _protocol, _clock, profile, installed = _real_candidate_service(
        tmp_path,
        "oauth_authorization_code_pkce",
        optional_tracker=True,
    )
    started = await service.start(
        CALENDAR_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, CALENDAR_PLUGIN, TRACKER_APP),
    )
    await service.complete(
        started.request_id or "",
        _callback_uri(started, profile),
    )
    registry = installed["plugin_registry"]
    base = registry.get(CALENDAR_PLUGIN)
    selected = registry.reverify_installed(CALENDAR_PLUGIN)
    record = installed["app_registry"].get(TRACKER_APP)
    selected_descriptor = descriptor_for_record(
        record,
        signed_capability_authority=signed_capability_authority_for_candidate(
            installed["package_registry"],
            record,
            selected,
        ),
    )
    verified, result = stage_signed_northwind_candidate(
        tmp_path,
        installed,
        plugin_id=CALENDAR_PLUGIN,
        peer="calendar",
        tracker_authentication_kind="oauth_authorization_code_pkce",
        tracker_remote_origin=f"{TRACKER_ORIGIN}/mcp/v1",
        tracker_scopes=("issues.read", "issues.write", "issues.admin"),
        tracker_app_id=TRACKER_APP,
        include_optional_tracker=True,
    )
    assert selected.package_sha256 != verified.package_sha256
    assert selected.manifest.optional_apps[0].authentication.scopes == SCOPES
    assert verified.manifest.optional_apps[0].authentication.scopes == (
        "issues.read",
        "issues.write",
        "issues.admin",
    )
    binding = result.record.app_binding
    assert binding is not None

    lifecycle = object.__new__(PluginLifecycleService)
    lifecycle.plugin_registry = registry
    lifecycle._connection_service = service
    lifecycle.plugin_grants = service._grants
    lifecycle.operator_account_key = lambda: ACCOUNT_KEY
    lifecycle.descriptor_for_plugin_app = lambda candidate_plugin_id, app_id: (
        selected_descriptor
        if candidate_plugin_id == CALENDAR_PLUGIN and app_id == TRACKER_APP
        else (_ for _ in ()).throw(AssertionError("unexpected app"))
    )

    assert lifecycle._candidate_update_app_ids(
        CALENDAR_PLUGIN,
        base,
        binding,
    ) == (CALENDAR_APP, TRACKER_APP)
    await service.close_all()


@pytest.mark.anyio
@pytest.mark.parametrize("connected", (True, False), ids=("connected", "never-connected"))
async def test_a_retained_credentialless_optional_app_is_kept_exactly_when_it_is_live(
    tmp_path: Path,
    connected: bool,
) -> None:
    """An update keeps a retained optional app that holds live authority, and only then.

    Liveness is the authority the runtime serves (planning_app_authority): for an
    unauthenticated remote app that is its durable credentialless connection -- an
    OAuth grant can never exist for it, so the OAuth-only check dropped it even
    while connected.
    """

    service, _protocol, _clock, _profile, installed = _real_candidate_service(
        tmp_path,
        "none",
        optional_tracker=True,
    )
    if connected:
        started = await service.start(
            CALENDAR_PLUGIN,
            app_id=TRACKER_APP,
            account_key=ACCOUNT_KEY,
            **full_connection_grant(service, CALENDAR_PLUGIN, TRACKER_APP),
        )
        assert started.status == "connected"
    registry = installed["plugin_registry"]
    base = registry.get(CALENDAR_PLUGIN)
    selected = registry.reverify_installed(CALENDAR_PLUGIN)
    record = installed["app_registry"].get(TRACKER_APP)
    selected_descriptor = descriptor_for_record(
        record,
        signed_capability_authority=signed_capability_authority_for_candidate(
            installed["package_registry"],
            record,
            selected,
        ),
    )
    _verified, result = stage_signed_northwind_candidate(
        tmp_path,
        installed,
        plugin_id=CALENDAR_PLUGIN,
        peer="calendar",
        tracker_authentication_kind="none",
        tracker_remote_origin=f"{TRACKER_ORIGIN}/mcp/v1",
        tracker_scopes=(),
        tracker_app_id=TRACKER_APP,
        include_optional_tracker=True,
    )
    binding = result.record.app_binding
    assert binding is not None and TRACKER_APP in binding.optional_app_ids

    lifecycle = object.__new__(PluginLifecycleService)
    lifecycle.plugin_registry = registry
    lifecycle._connection_service = service
    lifecycle.plugin_grants = service._grants
    lifecycle.operator_account_key = lambda: ACCOUNT_KEY
    lifecycle.descriptor_for_plugin_app = lambda candidate_plugin_id, app_id: (
        selected_descriptor
        if candidate_plugin_id == CALENDAR_PLUGIN and app_id == TRACKER_APP
        else (_ for _ in ()).throw(AssertionError("unexpected app"))
    )

    assert lifecycle._candidate_update_app_ids(CALENDAR_PLUGIN, base, binding) == (
        (CALENDAR_APP, TRACKER_APP) if connected else (CALENDAR_APP,)
    )
    await service.close_all()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "authentication_kind",
    ("oauth_authorization_code_pkce", "oauth_device_code"),
    ids=("browser", "device"),
)
@pytest.mark.parametrize(
    "boundary",
    ("terminal_before_compile", "prepared_before_promotion"),
)
async def test_real_pre_cutover_restore_keeps_identical_predecessor_offline(
    tmp_path: Path,
    authentication_kind: str,
    boundary: str,
) -> None:
    service, protocol, clock, profile, installed = _real_candidate_service(
        tmp_path,
        authentication_kind,
    )
    authorization_flow = profile.authorization_flow
    old_start = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    if authorization_flow is OAuthAuthorizationFlow.DEVICE_CODE:
        with pytest.raises(ValueError, match="oauth_device_authorization_pending"):
            await service.complete_device(old_start.request_id or "")
        clock.value += 5
        old_status = await service.complete_device(old_start.request_id or "")
        if old_status.status == "finalizing":
            old_status = await service.complete_device(old_start.request_id or "")
        assert old_status.status == "connected"
        protocol.device_poll_calls = 0
    else:
        await service.complete(
            old_start.request_id or "",
            _callback_uri(old_start, profile),
        )
    old_authority = service.authority_store.connection(
        TRACKER_PLUGIN,
        TRACKER_APP,
        ACCOUNT_DIGEST,
    )
    key = (TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST)
    old_session = service._sessions[key]
    previous_record = installed["plugin_registry"].get(TRACKER_PLUGIN)
    verified, result = stage_signed_northwind_candidate(
        tmp_path,
        installed,
        tracker_authentication_kind=authentication_kind,
        tracker_remote_origin=f"{TRACKER_ORIGIN}/mcp/v1",
        tracker_scopes=("issues.read", "issues.write", "issues.admin"),
        tracker_app_id=TRACKER_APP,
    )
    binding_material = {
        "required_app_ids": [TRACKER_APP],
        "optional_app_ids": [],
        "dependency_receipt_id": result.dependency_receipt.receipt_id,
    }
    binding = PluginAppBinding(
        required_app_ids=(TRACKER_APP,),
        optional_app_ids=(),
        dependency_receipt_id=result.dependency_receipt.receipt_id,
        revision=sha256_hex(canonical_bytes(binding_material)),
    )
    recovery = service.begin_update_recovery(
        previous_record=previous_record,
        candidate_binding=binding,
        account_key=ACCOUNT_KEY,
        checkpoint_id="plugin-update-recovery-" + "7" * 64,
    )
    candidate_start = await service.start_candidate(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        candidate_verified=verified,
        candidate_binding=binding,
        account_key=ACCOUNT_KEY,
        expected_registry_revision=result.revision,
        **_full_candidate_grant(
            service,
            TRACKER_PLUGIN,
            TRACKER_APP,
            candidate_verified=verified,
            candidate_binding=binding,
            expected_registry_revision=result.revision,
        ),
    )
    if boundary == "terminal_before_compile":
        if authorization_flow is OAuthAuthorizationFlow.DEVICE_CODE:
            with pytest.raises(
                ValueError,
                match="oauth_device_authorization_pending",
            ):
                await service.complete_candidate_device(
                    candidate_start.request_id or "",
                    candidate_verified=verified,
                    candidate_binding=binding,
                    expected_registry_revision=result.revision,
                )
            service.cancel_candidate(
                candidate_start.request_id or "",
                candidate_verified=verified,
                candidate_binding=binding,
            )
        else:
            with pytest.raises(ValueError, match="^oauth_authorization_denied$"):
                await service.complete_candidate(
                    candidate_start.request_id or "",
                    _callback_uri(candidate_start, profile, error="access_denied"),
                    candidate_verified=verified,
                    candidate_binding=binding,
                    expected_registry_revision=result.revision,
                )
    elif authorization_flow is OAuthAuthorizationFlow.DEVICE_CODE:
        with pytest.raises(ValueError, match="oauth_device_authorization_pending"):
            await service.complete_candidate_device(
                candidate_start.request_id or "",
                candidate_verified=verified,
                candidate_binding=binding,
                expected_registry_revision=result.revision,
            )
        clock.value += 5
        await service.complete_candidate_device(
            candidate_start.request_id or "",
            candidate_verified=verified,
            candidate_binding=binding,
            expected_registry_revision=result.revision,
        )
    else:
        await service.complete_candidate(
            candidate_start.request_id or "",
            _callback_uri(candidate_start, profile),
            candidate_verified=verified,
            candidate_binding=binding,
            expected_registry_revision=result.revision,
        )

    protocol.revoke_error = OAuthNetworkError("oauth_provider_unreachable")
    revoke_calls = len(protocol.revoke_calls)
    await service.restore_update_recovery(recovery)

    assert service._sessions[key] is old_session
    assert service._states[key] == "connected"
    assert service.authority_store.connection(
        TRACKER_PLUGIN,
        TRACKER_APP,
        ACCOUNT_DIGEST,
    ) == old_authority
    assert installed["plugin_registry"].get(TRACKER_PLUGIN).selected_version == "1.0.0"
    assert len(protocol.revoke_calls) == revoke_calls
    await service.restore_update_recovery(recovery)
    assert service._sessions[key] is old_session
    await service.close_all()


# An app the 2.0.0 release requires that 1.0.0 never had.
ADDED_APP = "northwind.tracker.next.mcp"


def _real_update_lifecycle(
    tmp_path: Path,
    service: PluginConnectionService,
    installed: dict[str, object],
    *,
    catalog: object,
    verified: object,
) -> PluginLifecycleService:
    """The real lifecycle update path over the real connection service and registries.

    Fixed here: the catalog source (the one signed release under review), the
    operator's account key and the displayed lifecycle revision. Every consent,
    candidate, cutover, retirement and status step is real.
    """
    lifecycle = _real_lifecycle(tmp_path, service, installed)
    lifecycle._verified = lambda _plugin_id: (catalog, catalog.entries[0], verified)
    return lifecycle


def _real_lifecycle(
    tmp_path: Path,
    service: PluginConnectionService,
    installed: dict[str, object],
) -> PluginLifecycleService:
    """The real lifecycle over the real connection service and registries."""
    registry = installed["plugin_registry"]
    state_root = tmp_path / "lifecycle"
    state_root.mkdir()
    lifecycle = object.__new__(PluginLifecycleService)
    lifecycle.plugin_registry = registry
    lifecycle.app_registry = installed["app_registry"]
    lifecycle.package_registry = installed["package_registry"]
    lifecycle._installer = installed["installer"]
    lifecycle._connection_service = service
    lifecycle.plugin_grants = service._grants
    lifecycle.permission_store = None
    lifecycle._authority_mutation_lock = _LifecycleMutationBoundary()
    lifecycle._configure_lifecycle_owner_state(state_root)
    lifecycle._update_state = McpStateStore(
        state_root=state_root,
        document_path=state_root / "update.json",
        invalid_path_reason="plugin_update_recovery_required",
        max_document_bytes=1024 * 1024,
    )
    lifecycle._installed_verified = registry.reverify_installed
    lifecycle.operator_account_key = lambda: ACCOUNT_KEY
    lifecycle._lifecycle_revision_snapshot = lambda: SimpleNamespace(revision="d" * 64)

    def submit(operation):
        # The lifecycle submits connection work synchronously, even from inside
        # its own coroutines (a rollback); production owns a separate loop for it.
        with ThreadPoolExecutor(max_workers=1) as connection_loop:
            return connection_loop.submit(asyncio.run, operation()).result()

    lifecycle._submit_connection_operation = submit
    lifecycle._connection_coordinator = PluginConnectionCoordinator(
        plugin_registry=registry,
        connection_service=service,
    )
    return lifecycle


async def _pending_update_adding_an_app(
    tmp_path: Path,
    authentication_kind: str,
):
    """Connect 1.0.0, then start the 2.0.0 update that requires ``ADDED_APP``.

    The selected version's binding does not own the added app, so it has no
    live connection: the lifecycle must not ask the connection service about it.
    """

    service, _protocol, _clock, profile, installed = _real_candidate_service(
        tmp_path,
        authentication_kind,
    )
    registry = installed["plugin_registry"]
    predecessor = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    if authentication_kind != "none":
        await service.complete(
            predecessor.request_id or "",
            _callback_uri(predecessor, profile),
        )
    # A connected plugin is enabled, as publishing its connection leaves it.
    registry.set_enabled(TRACKER_PLUGIN, True, expected_revision=registry.snapshot().revision)
    catalog, verified, review = review_signed_northwind_candidate(
        tmp_path,
        installed,
        tracker_authentication_kind=authentication_kind,
        tracker_remote_origin=f"{TRACKER_ORIGIN}/mcp/v1",
        tracker_app_id=ADDED_APP,
    )
    lifecycle = _real_update_lifecycle(
        tmp_path,
        service,
        installed,
        catalog=catalog,
        verified=verified,
    )
    pending = await asyncio.to_thread(
        lifecycle.update,
        TRACKER_PLUGIN,
        "2.0.0",
        review.sha256,
    )
    return service, profile, installed, lifecycle, pending


@pytest.mark.anyio
async def test_real_update_adding_a_credentialless_app_waits_for_its_consent(
    tmp_path: Path,
) -> None:
    """A credentialless added app has no consent to carry forward: the update waits.

    It is reported as the next app to set up, nothing is recorded for it, and
    its consent form offers the candidate's full offer with nothing selected.
    The operator's selection then carries the update through cutover: the
    plugin compiles on the added app's credentialless authority, exactly as
    the runtime catalog serves it.
    """

    service, _profile, installed, lifecycle, pending = await _pending_update_adding_an_app(
        tmp_path,
        "none",
    )
    registry = installed["plugin_registry"]

    assert pending == PluginUpdatePending(
        plugin_id=TRACKER_PLUGIN,
        app_ids=(ADDED_APP,),
        next_required_app_id=ADDED_APP,
    )
    operation = lifecycle._read_update_document()["operation"]
    assert operation["candidate_apps"] == [
        {
            "app_id": ADDED_APP,
            "state": "setup_required",
            "request_id": None,
            "connection_revision": None,
            "activation": None,
            "settings": None,
        }
    ]
    selection = await lifecycle.permissions(TRACKER_PLUGIN, ADDED_APP)
    assert selection["candidate"] is True
    assert selection["offered"]["provider_scopes"] == []
    assert selection["offered"]["capability_ids"]
    assert selection["selected"]["provider_scopes"] == []
    assert selection["selected"]["capability_ids"] == []

    settings = full_permission_settings(selection)
    await lifecycle.start_connection(
        TRACKER_PLUGIN,
        ADDED_APP,
        revision=selection["revision"],
        settings=settings,
    )

    record = registry.get(TRACKER_PLUGIN)
    assert record.selected_version == "2.0.0"
    assert record.enabled is True
    assert record.app_binding is not None
    assert record.app_binding.required_app_ids == (ADDED_APP,)
    assert lifecycle._read_update_document()["operation"] is None
    added = await service.status(TRACKER_PLUGIN, app_id=ADDED_APP, account_key=ACCOUNT_KEY)
    assert added.status == "connected"
    assert added.capability_ceiling == tuple(settings["capability_ids"])
    served = plugin_runtime_catalog(service=lifecycle)
    assert {
        (item.app_id, item.capability)
        for item in served.tools
        if item.plugin_id == TRACKER_PLUGIN
    } == {
        (ADDED_APP, capability.removeprefix("tool:"))
        for capability in settings["capability_ids"]
    }
    await service.close_all()


@pytest.mark.anyio
async def test_real_update_adding_an_oauth_app_completes_through_its_own_consent(
    tmp_path: Path,
) -> None:
    """The added app's consent is the operator's fresh selection, and the update
    completes through it: sign-in, cutover, and the predecessor app's retirement."""

    service, profile, installed, lifecycle, pending = await _pending_update_adding_an_app(
        tmp_path,
        "oauth_authorization_code_pkce",
    )
    registry = installed["plugin_registry"]
    assert pending == PluginUpdatePending(
        plugin_id=TRACKER_PLUGIN,
        app_ids=(ADDED_APP,),
        next_required_app_id=ADDED_APP,
    )

    selection = await lifecycle.permissions(TRACKER_PLUGIN, ADDED_APP)
    assert selection["candidate"] is True
    # Nothing is carried into an app the selected version never had.
    assert selection["selected"]["provider_scopes"] == []
    assert selection["selected"]["capability_ids"] == []
    settings = full_permission_settings(selection)
    started = await lifecycle.start_connection(
        TRACKER_PLUGIN,
        ADDED_APP,
        revision=selection["revision"],
        settings=settings,
    )
    recorded = lifecycle._read_update_document()["operation"]["candidate_apps"][0]
    assert recorded["state"] == "authorizing"
    assert recorded["settings"] == settings
    await lifecycle.complete_connection(
        _callback_uri(started, profile),
        request_id=started.request_id,
    )

    record = registry.get(TRACKER_PLUGIN)
    assert record.selected_version == "2.0.0"
    assert record.app_binding is not None
    assert record.app_binding.required_app_ids == (ADDED_APP,)
    assert lifecycle._read_update_document()["operation"] is None
    added = await service.status(TRACKER_PLUGIN, app_id=ADDED_APP, account_key=ACCOUNT_KEY)
    assert added.status == "connected"
    assert added.provider_scopes == tuple(settings["provider_scopes"])
    assert added.capability_ceiling == tuple(settings["capability_ids"])
    await service.close_all()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("authentication_kind", "connected"),
    (
        ("none", True),
        ("oauth_authorization_code_pkce", True),
        ("none", False),
        ("oauth_authorization_code_pkce", False),
    ),
    ids=(
        "credentialless-connected",
        "oauth-connected",
        "credentialless-unconnected",
        "oauth-unconnected",
    ),
)
async def test_compile_gate_admits_exactly_what_the_runtime_catalog_serves(
    tmp_path: Path,
    authentication_kind: str,
    connected: bool,
) -> None:
    """The enable and cutover gate compiles a plugin the way the runtime catalog does.

    An enabled plugin passes only on live authority the runtime will serve: a
    connected credentialless plugin as much as a connected OAuth one. With no
    live authority, neither passes and the runtime serves nothing for it.
    """

    service, _protocol, _clock, profile, installed = _real_candidate_service(
        tmp_path,
        authentication_kind,
    )
    registry = installed["plugin_registry"]
    if connected:
        started = await service.start(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=ACCOUNT_KEY,
            **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
        )
        if authentication_kind != "none":
            await service.complete(started.request_id or "", _callback_uri(started, profile))
    registry.set_enabled(TRACKER_PLUGIN, True, expected_revision=registry.snapshot().revision)
    lifecycle = _real_lifecycle(tmp_path, service, installed)
    served = {
        item.capability
        for item in plugin_runtime_catalog(service=lifecycle).tools
        if item.plugin_id == TRACKER_PLUGIN
    }

    if connected:
        lifecycle._assert_plugin_compiled(TRACKER_PLUGIN)
        assert served
    else:
        with pytest.raises(ValueError, match="^plugin_catalog_authority_missing$"):
            lifecycle._assert_plugin_compiled(TRACKER_PLUGIN)
        assert served == set()
    await service.close_all()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "authentication_kind",
    ("none", "oauth_authorization_code_pkce"),
    ids=("credentialless", "oauth"),
)
async def test_status_names_the_added_app_a_pending_update_waits_on(
    tmp_path: Path,
    authentication_kind: str,
) -> None:
    """After a restart the panel has only status: it must name the app the update needs.

    The update response and ``permissions()`` name the added app; status names
    the same app, in the setup-required state the panel's Continue setup reads,
    while the predecessor keeps serving at its own version.
    """

    service, _profile, _installed, lifecycle, pending = await _pending_update_adding_an_app(
        tmp_path,
        authentication_kind,
    )
    operation = lifecycle._read_update_document()["operation"]

    snapshot = await lifecycle.status(TRACKER_PLUGIN)

    assert pending.next_required_app_id == ADDED_APP
    assert lifecycle._next_candidate_app(operation) == ADDED_APP
    assert snapshot.state is PluginLifecycleState.SETUP_REQUIRED
    assert snapshot.next_required_app_id == ADDED_APP
    assert snapshot.error_code == "plugin_setup_required"
    apps = {app.app_id: app for app in snapshot.apps}
    assert apps[ADDED_APP].required is True
    assert apps[ADDED_APP].state is PluginLifecycleState.SETUP_REQUIRED
    assert apps[TRACKER_APP].state is PluginLifecycleState.CONNECTED
    assert snapshot.package_version == "1.0.0"
    selection = await lifecycle.permissions(TRACKER_PLUGIN, snapshot.next_required_app_id)
    assert selection["candidate"] is True
    await service.close_all()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("authentication_kind", "setup_action"),
    (("none", "none"), ("oauth_authorization_code_pkce", "browser")),
    ids=("credentialless", "oauth"),
)
async def test_connection_route_gives_the_added_app_its_own_setup_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authentication_kind: str,
    setup_action: str,
) -> None:
    """Continue setup polls GET /connection for the app status names.

    That app exists only in the pending update's candidate, so the route must
    read its requirement from the candidate's manifest -- the installed one has
    no such app and used to answer plugin_connection_state_invalid.
    """
    import httpx

    import hud.server as hud_server
    from tests.hud.test_plugin_lifecycle_api import _marketplace_catalog_source

    service, _profile, _installed, lifecycle, _pending = await _pending_update_adding_an_app(
        tmp_path,
        authentication_kind,
    )
    # create_app() eagerly builds the plugin directory from catalog_source(); this
    # route does not use it, so the HUD tests' incidental source serves.
    lifecycle.catalog_source = _marketplace_catalog_source
    monkeypatch.setattr(hud_server, "get_default_plugin_lifecycle_service", lambda: lifecycle)
    transport = httpx.ASGITransport(app=hud_server.create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://hud.test") as client:
        response = await client.get(f"/api/plugins/{TRACKER_PLUGIN}/connection")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["next_required_app_id"] == ADDED_APP
    assert body["state"] == "setup_required"
    assert body["setup_action"] == setup_action
    assert lifecycle.setup_requirement(TRACKER_PLUGIN, ADDED_APP).authentication.kind.value == (
        authentication_kind
    )
    await service.close_all()

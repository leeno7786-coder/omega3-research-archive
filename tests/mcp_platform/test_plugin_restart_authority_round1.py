from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from mcp_platform.canonical import sha256_hex
from mcp_platform.plugin_authority import PluginAuthorityStore
from mcp_platform.plugin_connections import (
    PluginConnectionService,
    descriptor_for_record,
    signed_capability_authority_from_package,
)
from mcp_platform.plugin_lifecycle import PluginLifecycleService
from mcp_platform.plugin_catalog import compile_plugin_tool_bindings
from plugin_platform.contracts import PluginKind
from tests.helpers.plugin_consent import full_connection_grant
from tests.mcp_platform.generic_plugin_install import (
    install_signed_northwind_plugins,
)
from tests.mcp_platform.generic_plugin_peers import (
    CALENDAR_APP,
    CALENDAR_PLUGIN,
    TRACKER_APP,
    TRACKER_PLUGIN,
)
from tests.mcp_platform.test_plugin_connections import (
    ACCOUNT_KEY,
    _Broker,
    _DiscoveryWorker,
    _GrantService,
    _direct_submit,
    _remote_service,
)


@pytest.mark.anyio
async def test_credentialless_restart_uses_durable_authority_without_parent_session(
    tmp_path: Path,
) -> None:
    service, broker, installed = _remote_service(
        tmp_path,
        authentication_kind="none",
        execution_worker=_DiscoveryWorker(),
    )
    started = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    assert started.status == "connected"
    grants = service._grants
    restarted, _broker, _installed = _remote_service(
        tmp_path,
        installed=installed,
        broker=broker,
        grants=grants,
        authentication_kind="none",
        execution_worker=_DiscoveryWorker(),
    )

    assert restarted._sessions == {}
    status = await restarted.status(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
    )
    preparation = await restarted.prepare_execution_worker(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        capability_kind="tool",
        capability_id="create_issue",
    )

    assert status.status == "connected"
    assert preparation.authority.connection_revision == (
        grants.authority_store.connection(
            TRACKER_PLUGIN,
            TRACKER_APP,
            sha256_hex(ACCOUNT_KEY.encode("utf-8")),
        ).connection_revision
    )


def _planning_lifecycle(
    installed: dict[str, object],
    connection_service: PluginConnectionService,
) -> PluginLifecycleService:
    lifecycle = object.__new__(PluginLifecycleService)
    lifecycle.plugin_registry = installed["plugin_registry"]
    lifecycle.package_registry = installed["package_registry"]
    lifecycle.app_registry = installed["app_registry"]
    lifecycle.plugin_grants = None
    lifecycle._connection_service = connection_service
    lifecycle._installed_verified = installed["plugin_registry"].reverify_installed
    return lifecycle


def _different_revision(revision: str) -> str:
    return ("0" if revision[0] != "0" else "1") * 64


@pytest.mark.anyio
async def test_auth_none_restart_planning_rejects_the_same_drift_as_preparation(
    tmp_path: Path,
) -> None:
    service, broker, installed = _remote_service(
        tmp_path,
        authentication_kind="none",
        execution_worker=_DiscoveryWorker(),
    )
    started = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    assert started.status == "connected"
    restarted, _broker, _installed = _remote_service(
        tmp_path,
        installed=installed,
        broker=broker,
        grants=service._grants,
        authentication_kind="none",
        execution_worker=_DiscoveryWorker(),
    )
    lifecycle = _planning_lifecycle(installed, restarted)
    current = lifecycle.descriptor_for_plugin_app(TRACKER_PLUGIN, TRACKER_APP)
    drifted = replace(
        current,
        capability_revision=_different_revision(current.capability_revision),
    )
    account_hash = sha256_hex(ACCOUNT_KEY.encode("utf-8"))

    assert (
        lifecycle.planning_app_authority(
            TRACKER_PLUGIN,
            TRACKER_APP,
            account_hash,
            drifted,
        )
        is None
    )
    restarted._descriptor_provider = lambda _plugin_id, _app_id: drifted
    with pytest.raises(ValueError, match="^plugin_connection_not_live$"):
        await restarted.prepare_execution_worker(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=ACCOUNT_KEY,
            capability_kind="tool",
            capability_id="create_issue",
        )


def _local_service(
    tmp_path: Path,
    *,
    installed: dict[str, object],
    grants: _GrantService,
) -> PluginConnectionService:
    registry = installed["plugin_registry"]
    app_registry = installed["app_registry"]
    package_registry = installed["package_registry"]

    def descriptor(plugin_id: str, app_id: str):
        record = app_registry.get(app_id)
        selected = descriptor_for_record(
            record,
            signed_capability_authority=signed_capability_authority_from_package(
                package_registry,
                record,
                plugin_registry=registry,
                plugin_id=plugin_id,
            ),
        )
        if record.deployment.value == "local":
            selected = replace(
                selected,
                verified_launch_sha256=(
                    package_registry.reverify_installed(app_id).manifest.artifact_sha256
                ),
            )
        return selected
    return PluginConnectionService(
        app_registry=app_registry,
        plugin_registry=registry,
        package_registry=package_registry,
        broker=grants._broker,
        grant_service=grants,
        descriptor_provider=descriptor,
        requirement_provider=lambda _plugin_id, _record: (
            registry.reverify_installed(CALENDAR_PLUGIN).manifest.required_apps[0]
        ),
        offered_scopes_provider=lambda *_args: (),
        operation_submitter=_direct_submit,
        local_connector=None,
        transport_authority=object(),
        execution_worker=_DiscoveryWorker(),
    )


@pytest.mark.anyio
async def test_local_restart_uses_durable_authority_without_parent_session(
    tmp_path: Path,
) -> None:
    installed = install_signed_northwind_plugins(tmp_path, peers=("calendar",))
    installed["package_registry"].set_enabled(
        CALENDAR_APP,
        True,
        expected_revision=installed["package_registry"].snapshot().revision,
    )
    broker = _Broker()
    grants = _GrantService(broker)
    grants.authority_store = PluginAuthorityStore(path=tmp_path / "local-authority.json")
    service = _local_service(tmp_path, installed=installed, grants=grants)
    started = await service.start(
        CALENDAR_PLUGIN,
        app_id=CALENDAR_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, CALENDAR_PLUGIN, CALENDAR_APP),
    )
    assert started.status == "connected"

    restarted = _local_service(tmp_path, installed=installed, grants=grants)
    assert restarted._sessions == {}
    status = await restarted.status(
        CALENDAR_PLUGIN,
        app_id=CALENDAR_APP,
        account_key=ACCOUNT_KEY,
    )
    preparation = await restarted.prepare_execution_worker(
        CALENDAR_PLUGIN,
        app_id=CALENDAR_APP,
        account_key=ACCOUNT_KEY,
        capability_kind="tool",
        capability_id="list_events",
    )

    assert status.status == "connected"
    assert preparation.local_execution is not None


@pytest.mark.anyio
async def test_local_restart_planning_rejects_the_same_drift_as_preparation(
    tmp_path: Path,
) -> None:
    installed = install_signed_northwind_plugins(tmp_path, peers=("calendar",))
    installed["package_registry"].set_enabled(
        CALENDAR_APP,
        True,
        expected_revision=installed["package_registry"].snapshot().revision,
    )
    broker = _Broker()
    grants = _GrantService(broker)
    grants.authority_store = PluginAuthorityStore(path=tmp_path / "local-authority.json")
    service = _local_service(tmp_path, installed=installed, grants=grants)
    started = await service.start(
        CALENDAR_PLUGIN,
        app_id=CALENDAR_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, CALENDAR_PLUGIN, CALENDAR_APP),
    )
    assert started.status == "connected"
    restarted = _local_service(tmp_path, installed=installed, grants=grants)
    lifecycle = _planning_lifecycle(installed, restarted)
    current = lifecycle.descriptor_for_plugin_app(CALENDAR_PLUGIN, CALENDAR_APP)
    drifted = replace(
        current,
        capability_revision=_different_revision(current.capability_revision),
    )
    account_hash = sha256_hex(ACCOUNT_KEY.encode("utf-8"))

    assert (
        lifecycle.planning_app_authority(
            CALENDAR_PLUGIN,
            CALENDAR_APP,
            account_hash,
            drifted,
        )
        is None
    )
    restarted._descriptor_provider = lambda _plugin_id, _app_id: drifted
    with pytest.raises(ValueError, match="^plugin_connection_not_live$"):
        await restarted.prepare_execution_worker(
            CALENDAR_PLUGIN,
            app_id=CALENDAR_APP,
            account_key=ACCOUNT_KEY,
            capability_kind="tool",
            capability_id="list_events",
        )


def _composite_service(
    tmp_path: Path,
    *,
    installed: dict[str, object],
    grants: _GrantService,
) -> PluginConnectionService:
    registry = installed["plugin_registry"]
    app_registry = installed["app_registry"]
    package_registry = installed["package_registry"]

    def requirement(plugin_id: str, record: object):
        verified = registry.reverify_installed(plugin_id)
        return next(
            item
            for item in (*verified.manifest.required_apps, *verified.manifest.optional_apps)
            if item.app_id == record.app_id
        )

    def descriptor(plugin_id: str, app_id: str):
        record = app_registry.get(app_id)
        selected = descriptor_for_record(
            record,
            signed_capability_authority=signed_capability_authority_from_package(
                package_registry,
                record,
                plugin_registry=registry,
                plugin_id=plugin_id,
            ),
        )
        if record.deployment.value == "local":
            selected = replace(
                selected,
                verified_launch_sha256=(
                    package_registry.reverify_installed(app_id).manifest.artifact_sha256
                ),
            )
        return selected

    return PluginConnectionService(
        app_registry=app_registry,
        plugin_registry=registry,
        package_registry=package_registry,
        broker=grants._broker,
        grant_service=grants,
        descriptor_provider=descriptor,
        requirement_provider=requirement,
        offered_scopes_provider=lambda *_args: ("issues.read", "issues.write"),
        operation_submitter=_direct_submit,
        local_connector=None,
        transport_authority=object(),
        execution_worker=_DiscoveryWorker(),
    )


@pytest.mark.anyio
async def test_composite_restart_keeps_each_app_visible_and_exactly_executable(
    tmp_path: Path,
) -> None:
    installed = install_signed_northwind_plugins(
        tmp_path,
        peers=("calendar", "tracker"),
        tracker_authentication_kind="none",
        composite_plugin_ids=(CALENDAR_PLUGIN,),
    )
    packages = installed["package_registry"]
    packages.set_enabled(
        CALENDAR_APP,
        True,
        expected_revision=packages.snapshot().revision,
    )
    broker = _Broker()
    grants = _GrantService(broker)
    grants.authority_store = PluginAuthorityStore(path=tmp_path / "composite-authority.json")
    service = _composite_service(tmp_path, installed=installed, grants=grants)
    verified = installed["plugin_registry"].reverify_installed(CALENDAR_PLUGIN)
    assert verified.manifest.kind is PluginKind.COMPOSITE
    assert tuple(item.app_id for item in verified.manifest.required_apps) == (
        CALENDAR_APP,
        TRACKER_APP,
    )
    for app_id in (CALENDAR_APP, TRACKER_APP):
        started = await service.start(
            CALENDAR_PLUGIN,
            app_id=app_id,
            account_key=ACCOUNT_KEY,
            **full_connection_grant(service, CALENDAR_PLUGIN, app_id),
        )
        assert started.status == "connected"

    restarted = _composite_service(tmp_path, installed=installed, grants=grants)
    assert restarted._sessions == {}
    calendar_status = await restarted.status(
        CALENDAR_PLUGIN,
        app_id=CALENDAR_APP,
        account_key=ACCOUNT_KEY,
    )
    tracker_status = await restarted.status(
        CALENDAR_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
    )
    calendar = await restarted.prepare_execution_worker(
        CALENDAR_PLUGIN,
        app_id=CALENDAR_APP,
        account_key=ACCOUNT_KEY,
        capability_kind="tool",
        capability_id="list_events",
    )
    tracker = await restarted.prepare_execution_worker(
        CALENDAR_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        capability_kind="tool",
        capability_id="create_issue",
    )
    lifecycle = object.__new__(PluginLifecycleService)
    lifecycle.plugin_registry = installed["plugin_registry"]
    lifecycle.package_registry = packages
    lifecycle.app_registry = installed["app_registry"]
    lifecycle.plugin_grants = None
    lifecycle._connection_service = restarted
    lifecycle._installed_verified = installed["plugin_registry"].reverify_installed
    calendar_descriptor = lifecycle.descriptor_for_plugin_app(
        CALENDAR_PLUGIN,
        CALENDAR_APP,
    )
    tracker_descriptor = lifecycle.descriptor_for_plugin_app(
        CALENDAR_PLUGIN,
        TRACKER_APP,
    )
    calendar_plan = lifecycle.planning_app_authority(
        CALENDAR_PLUGIN,
        CALENDAR_APP,
        sha256_hex(ACCOUNT_KEY.encode("utf-8")),
        calendar_descriptor,
    )
    tracker_plan = lifecycle.planning_app_authority(
        CALENDAR_PLUGIN,
        TRACKER_APP,
        sha256_hex(ACCOUNT_KEY.encode("utf-8")),
        tracker_descriptor,
    )
    bindings = compile_plugin_tool_bindings(
        plugin_snapshot=installed["plugin_registry"].snapshot(),
        app_snapshot=installed["app_registry"].snapshot(),
        descriptor_for_app=lifecycle.descriptor_for_plugin_app,
        account_key_hash=sha256_hex(ACCOUNT_KEY.encode("utf-8")),
        app_authority_provider=lifecycle.planning_app_authority,
    )
    composite_bindings = {
        (item.app_id, item.capability): item
        for item in bindings
        if item.plugin_id == CALENDAR_PLUGIN
    }

    assert calendar_status.status == tracker_status.status == "connected"
    assert calendar.authority.plugin_id == tracker.authority.plugin_id == CALENDAR_PLUGIN
    assert calendar.authority.app_id == CALENDAR_APP
    assert tracker.authority.app_id == TRACKER_APP
    assert calendar.local_execution is not None
    assert tracker.local_execution is None
    assert set(composite_bindings) == {
        (CALENDAR_APP, "list_events"),
        (TRACKER_APP, "create_issue"),
    }
    assert calendar_plan is not None
    assert tracker_plan is not None
    assert (
        calendar_plan.connection_revision,
        calendar_plan.authority_revision,
        calendar_plan.capability_revision,
    ) == (
        calendar.authority.connection_revision,
        calendar.authority.authority_revision,
        calendar.authority.capability_revision,
    )
    assert (
        tracker_plan.connection_revision,
        tracker_plan.authority_revision,
        tracker_plan.capability_revision,
    ) == (
        tracker.authority.connection_revision,
        tracker.authority.authority_revision,
        tracker.authority.capability_revision,
    )
    preparations = {
        CALENDAR_APP: calendar,
        TRACKER_APP: tracker,
    }
    for (app_id, _capability), binding in composite_bindings.items():
        execution = preparations[app_id].authority
        durable = grants.authority_store.connection(
            CALENDAR_PLUGIN,
            app_id,
            sha256_hex(ACCOUNT_KEY.encode("utf-8")),
        )
        assert (
            binding.app_id,
            binding.capability_kind,
            binding.capability,
            binding.connection_revision,
            binding.authority_revision,
            binding.capability_revision,
        ) == (
            execution.app_id,
            execution.capability_kind,
            execution.capability_id,
            execution.connection_revision,
            execution.authority_revision,
            execution.capability_revision,
        )
        assert (binding.connection_revision, binding.authority_revision) == (
            durable.connection_revision,
            durable.revision,
        )

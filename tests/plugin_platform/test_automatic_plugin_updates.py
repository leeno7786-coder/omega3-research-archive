"""Contracts for the lifecycle-owned plugin update policy."""

from __future__ import annotations

from contextlib import nullcontext
import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_platform.plugin_lifecycle import PluginLifecycleService
from mcp_platform.plugin_lifecycle import compose_plugin_lifecycle_service
import mcp_platform.plugin_lifecycle as lifecycle_module
from mcp_platform.plugin_lifecycle import complete_authority_equivalent
from mcp_platform.app_registry import InstalledMcpAppRecord
from mcp_platform.app_sources import TrustedRemoteAppSource
from mcp_platform.client import McpProviderDriftStatus, McpProviderRecovery
from mcp_platform.client_contracts import McpClientErrorReason
from mcp_platform.plugin_runtime import (
    PluginOperationRequest,
    PluginRetryAfterFence,
    PluginRuntime,
    PluginRuntimeError,
    PluginRuntimeOperation,
)
from auto_loop.execute.plugin_tool import run_plugin_operation
from mcp_platform.package_contracts import RiskClass
from plugin_platform.package_verifier import VerifiedPlugin
from plugin_platform.package_verifier import verify_plugin_package
from tests.plugin_platform.support import build_signed_plugin, ephemeral_publisher_ring
from mcp_platform.plugin_update_policy import (
    DEFAULT_UPDATE_INTERVAL_SECONDS,
    PluginUpdatePolicy,
    PluginUpdateCheckResult,
    PluginUpdateResult,
    policy_store,
    defer_policy_if_current,
    read_policy,
    read_update_status,
    update_status_store,
    write_policy,
    write_update_status,
)
from plugin_platform.catalog_sync import CatalogSyncResult, CatalogSyncStatus


def _bare_service(tmp_path: Path) -> PluginLifecycleService:
    service = object.__new__(PluginLifecycleService)
    service._closed = False
    service._authority_mutation_lock = nullcontext()
    service._update_policy_store = policy_store(tmp_path)
    service._update_policy = read_policy(service._update_policy_store)
    service._operation_submitter = object()
    return service


def _verified(authority: dict[str, object], version: str) -> VerifiedPlugin:
    manifest = type("Manifest", (), {"to_dict": lambda self: {
        "plugin_id": "example",
        "plugin_version": version,
        "required_apps": [],
        "optional_apps": [],
        "assets": {"bundle.js": "sha256:" + "a" * 64},
    }})()
    return VerifiedPlugin(
        manifest=manifest, package_root=Path("."), manifest_sha256="a" * 64,
        package_sha256="b" * 64, asset_paths={}, asset_bytes={},
        capability_authority=authority,
    )


def test_authority_equivalence_includes_signed_capability_policy() -> None:
    installed = _verified(
        {"generic.app": {"tools": {"read": {"effect": "read", "risk": "low"}}}},
        "1.0.0",
    )
    candidate = _verified(
        {"generic.app": {"tools": {"read": {"effect": "read", "risk": "low"}}}},
        "1.1.0",
    )
    assert complete_authority_equivalent(
        installed, candidate, installed_provenance=None, candidate_provenance=None
    )
    changed = _verified(
        {"generic.app": {"tools": {"read": {"effect": "delete", "risk": "high"}}}},
        "1.1.0",
    )
    assert not complete_authority_equivalent(
        installed, changed, installed_provenance=None, candidate_provenance=None
    )


def test_real_verified_package_authority_projection_is_self_equivalent(
    tmp_path: Path,
) -> None:
    private_key, ring = ephemeral_publisher_ring()
    package_root = tmp_path / "marketplace"
    package_root.mkdir()
    package = build_signed_plugin(package_root, private_key)
    verified = verify_plugin_package(package, ring, "3.0.0")
    assert complete_authority_equivalent(
        verified,
        verified,
        installed_provenance=None,
        candidate_provenance=None,
    )


def test_policy_defaults_are_enabled_and_strict(tmp_path: Path) -> None:
    policy = read_policy(policy_store(tmp_path))
    assert policy == PluginUpdatePolicy(
        enabled=True,
        interval_seconds=DEFAULT_UPDATE_INTERVAL_SECONDS,
        last_check_at=None,
        next_check_at=None,
    )
    with pytest.raises(ValueError, match="plugin_update_policy_invalid"):
        PluginUpdatePolicy.from_dict({**policy.to_dict(), "unexpected": True})


def test_due_check_runs_once_and_persists_next_due(tmp_path: Path) -> None:
    service = _bare_service(tmp_path)
    calls: list[int] = []
    service.sync_catalog = lambda: (
        calls.append(1)
        or CatalogSyncResult(
            status=CatalogSyncStatus.UNCHANGED,
            sequence=1,
            revision="a" * 64,
        )
    )
    service._update_candidates = lambda: (("example", "1.1.0"),)

    first = service.check_updates(now=100.0)
    second = service.check_updates(now=100.0)

    assert first.status == "checked"
    assert first.candidate_versions == (("example", "1.1.0"),)
    assert second.status == "not_due"
    assert calls == [1]
    assert read_policy(service._update_policy_store).last_check_at == 100.0


def test_due_check_applies_candidates_through_lifecycle_owner(tmp_path: Path) -> None:
    service = _bare_service(tmp_path)
    service.plugin_registry = object()
    service.sync_catalog = lambda: CatalogSyncResult(
        status=CatalogSyncStatus.UNCHANGED,
        sequence=1,
        revision="a" * 64,
    )
    service._update_candidates = lambda: (("example", "1.1.0"),)
    applied: list[tuple[str, str]] = []

    def apply(plugin_id: str, version: str) -> PluginUpdateResult:
        applied.append((plugin_id, version))
        return PluginUpdateResult(plugin_id, version, "applied")

    service.apply_compatible_update = apply
    result = service.check_updates(now=100.0)
    assert applied == [("example", "1.1.0")]
    assert result.staged == ()


def test_probe_only_check_keeps_due_deadline_until_promotion(
    tmp_path: Path,
) -> None:
    service = _bare_service(tmp_path)
    service.sync_catalog = lambda: CatalogSyncResult(
        status=CatalogSyncStatus.UNCHANGED,
        sequence=1,
        revision="a" * 64,
    )
    service._update_candidates = lambda: (("example", "1.1.0"),)
    result = service.check_updates(
        now=100.0,
        force=True,
        _already_locked=True,
        _apply=False,
    )
    assert result.candidate_versions == (("example", "1.1.0"),)
    assert read_policy(service._update_policy_store).next_check_at is None


def test_deferred_policy_cas_preserves_newer_completed_check(tmp_path: Path) -> None:
    store = policy_store(tmp_path)
    expected = read_policy(store)
    newer = expected.after_success(200.0)
    write_policy(store, newer)
    assert defer_policy_if_current(store, expected, 100.0) is None
    assert read_policy(store) == newer


def test_deferred_heartbeat_does_not_clobber_newer_failed_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _bare_service(tmp_path)
    expected = PluginUpdatePolicy(last_check_at=0.0, next_check_at=90.0)
    write_policy(service._update_policy_store, expected)
    newer = PluginUpdateCheckResult(
        status="failed",
        checked_at=100.0,
        next_check_at=200.0,
        candidate_versions=(("example", "1.1.0"),),
        staged=(("example", "1.1.0"),),
        error="plugin_catalog_unavailable",
    )
    status_store = update_status_store(tmp_path)
    write_update_status(status_store, newer)

    def interleaved_defer(
        store: object, policy: PluginUpdatePolicy, now: float
    ) -> PluginUpdatePolicy:
        assert store is service._update_policy_store
        assert policy == expected
        assert now == 100.0
        # Simulate a completed failed check winning the interleaving after
        # deferral has read the old policy but before it could publish status.
        write_update_status(status_store, newer)
        current = policy.after_failure(100.0)
        write_policy(service._update_policy_store, current)
        return current

    monkeypatch.setattr(lifecycle_module, "defer_policy_if_current", interleaved_defer)
    service._defer_update_check(expected, 100.0, status="not_due")
    assert read_update_status(status_store) == newer


def test_failed_candidate_is_persisted_and_backed_off(
    tmp_path: Path,
) -> None:
    service = _bare_service(tmp_path)
    service.plugin_registry = object()
    service.sync_catalog = lambda: CatalogSyncResult(
        status=CatalogSyncStatus.UNCHANGED,
        sequence=1,
        revision="a" * 64,
    )
    service._update_candidates = lambda: (("example", "1.1.0"),)
    service.apply_compatible_update = lambda *_args: PluginUpdateResult(
        "example", "1.1.0", "failed", error="plugin_catalog_unavailable"
    )
    result = service.check_updates(now=100.0)
    assert result.staged == (("example", "1.1.0"),)
    assert result.error == "plugin_catalog_unavailable"
    assert read_update_status(update_status_store(tmp_path)) == result
    assert read_policy(service._update_policy_store).next_check_at is not None
    assert read_policy(service._update_policy_store).next_check_at > (
        100.0 + DEFAULT_UPDATE_INTERVAL_SECONDS
    )
    assert result.next_check_at == read_policy(service._update_policy_store).next_check_at


def test_delivery_exception_is_persisted_as_failure_with_backoff(
    tmp_path: Path,
) -> None:
    service = _bare_service(tmp_path)
    service.plugin_registry = object()
    service.sync_catalog = lambda: CatalogSyncResult(
        status=CatalogSyncStatus.UNCHANGED,
        sequence=1,
        revision="a" * 64,
    )
    service._update_candidates = lambda: (("example", "1.1.0"), ("other", "2.0.0"))

    def apply(*_args: object) -> PluginUpdateResult:
        raise RuntimeError("plugin_catalog_unavailable")

    service.apply_compatible_update = apply
    result = service.check_updates(now=100.0)
    assert result.status == "failed"
    assert result.staged == result.candidate_versions
    assert result.error == "plugin_catalog_unavailable"
    policy = read_policy(service._update_policy_store)
    assert result.next_check_at == policy.next_check_at
    assert policy.next_check_at is not None
    assert policy.next_check_at > 100.0 + DEFAULT_UPDATE_INTERVAL_SECONDS


@pytest.mark.anyio
async def test_live_status_boundary_rechecks_when_deadline_is_due(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _bare_service(tmp_path)
    service.plugin_registry = object()
    service._update_policy = PluginUpdatePolicy(
        last_check_at=0.0, next_check_at=90.0
    )
    write_policy(service._update_policy_store, service._update_policy)
    monkeypatch.setattr(lifecycle_module.time, "time", lambda: 100.0)
    calls: list[int] = []
    service.sync_catalog = lambda: (
        calls.append(1)
        or CatalogSyncResult(
            status=CatalogSyncStatus.UNCHANGED,
            sequence=1,
            revision="a" * 64,
        )
    )
    service._update_candidates = lambda: ()
    await service._run_due_update_check()
    await service._run_due_update_check()
    assert calls == [1]


def test_idle_heartbeat_services_persisted_due_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _bare_service(tmp_path)
    service._update_policy = PluginUpdatePolicy(
        last_check_at=0.0, next_check_at=90.0
    )
    write_policy(service._update_policy_store, service._update_policy)
    monkeypatch.setattr(lifecycle_module.time, "time", lambda: 100.0)
    calls: list[int] = []
    service.sync_catalog = lambda: (
        calls.append(1)
        or CatalogSyncResult(
            status=CatalogSyncStatus.UNCHANGED,
            sequence=1,
            revision="a" * 64,
        )
    )
    service._update_candidates = lambda: ()
    service._update_heartbeat_tick()
    service._update_heartbeat_tick()
    assert calls == [1]


@pytest.mark.parametrize("enabled", [False, True])
def test_heartbeat_never_zero_waits_when_disabled_or_fence_busy(
    tmp_path: Path, enabled: bool
) -> None:
    service = _bare_service(tmp_path)
    service._update_policy = PluginUpdatePolicy(
        enabled=enabled,
        last_check_at=0.0,
        next_check_at=90.0,
    )
    write_policy(service._update_policy_store, service._update_policy)
    monkeypatch_time = lifecycle_module.time.time
    lifecycle_module.time.time = lambda: 100.0
    try:
        if enabled:
            service._authority_mutation_lock = type(
                "BusyBoundary", (), {"_lock": threading.Lock()}
            )()
            service._authority_mutation_lock._lock.acquire()

        waits: list[float] = []

        class StopAfterOneWait:
            def __init__(self) -> None:
                self.calls = 0

            def is_set(self) -> bool:
                return False

            def wait(self, timeout: float) -> bool:
                waits.append(timeout)
                self.calls += 1
                return self.calls >= 2

        service._update_heartbeat_stop = StopAfterOneWait()
        service._update_heartbeat_loop()
        assert waits and all(delay > 0 for delay in waits)
        if enabled:
            policy = read_policy(service._update_policy_store)
            assert policy.next_check_at is not None and policy.next_check_at > 100.0
    finally:
        lifecycle_module.time.time = monkeypatch_time
        if enabled:
            service._authority_mutation_lock._lock.release()


def test_provider_lookup_does_not_reenter_the_composition_fence(
    tmp_path: Path,
) -> None:
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"q" * 32,
        operation_submitter=lambda operation, **_kwargs: asyncio.run(operation()),
    )
    try:
        service.sync_catalog = lambda: CatalogSyncResult(
            status=CatalogSyncStatus.UNCHANGED,
            sequence=1,
            revision="a" * 64,
        )
        service._update_candidates = lambda: ()
        status = McpProviderDriftStatus(
            phase="discovery",
            reason=McpClientErrorReason.CAPABILITY_DRIFT,
            plugin_id="example.plugin",
        )
        assert service.plugin_backend is not None
        heartbeat = service._update_heartbeat_thread
        assert heartbeat is not None and heartbeat.is_alive()
        with service.plugin_execution_mutation_scope():
            recovery = service.plugin_backend._provider_update_lookup(status)
        assert recovery is not None
        assert recovery.status == "reconnect_required"
    finally:
        service.close()
    assert service._update_heartbeat_stop.is_set()


def test_fenced_compatible_recovery_is_queued_until_fence_release(
    tmp_path: Path,
) -> None:
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"q" * 32,
        operation_submitter=lambda operation, **_kwargs: asyncio.run(operation()),
    )
    applied: list[tuple[str, str]] = []
    try:
        service.check_updates = lambda **_kwargs: type(
            "Result",
            (),
            {
                "status": "checked",
                "candidate_versions": (("example.plugin", "1.1.0"),),
                "staged": (),
                "approval_required": (),
                "error": None,
            },
        )()
        service.apply_compatible_update = lambda plugin_id, version: (
            applied.append((plugin_id, version))
            or PluginUpdateResult(plugin_id, version, "applied")
        )
        status = McpProviderDriftStatus(
            phase="discovery",
            reason=McpClientErrorReason.CAPABILITY_DRIFT,
            plugin_id="example.plugin",
        )
        assert service.plugin_backend is not None
        with service.plugin_execution_mutation_scope():
            recovery = service.plugin_backend._provider_update_lookup(status)
            assert recovery.status == "approval_required"
            assert recovery.approval_diff == ("update_pending",)
            assert applied == []
        assert applied == [("example.plugin", "1.1.0")]
    finally:
        service.close()


def test_runtime_backend_nested_fence_drains_only_after_outer_release(
    tmp_path: Path,
) -> None:
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"q" * 32,
        operation_submitter=lambda operation, **_kwargs: asyncio.run(operation()),
    )
    lock_states: list[bool] = []
    try:
        service.check_updates = lambda **_kwargs: type(
            "Result",
            (),
            {
                "status": "checked",
                "candidate_versions": (("example.plugin", "1.1.0"),),
                "staged": (),
                "approval_required": (),
                "error": None,
            },
        )()

        def apply(plugin_id: str, version: str) -> PluginUpdateResult:
            lock_states.append(service._authority_mutation_lock._lock.locked())
            return PluginUpdateResult(plugin_id, version, "applied")

        service.apply_compatible_update = apply
        status = McpProviderDriftStatus(
            phase="discovery",
            reason=McpClientErrorReason.CAPABILITY_DRIFT,
            plugin_id="example.plugin",
        )
        assert service.plugin_backend is not None
        assert service.plugin_runtime is not None
        service.plugin_backend._execute_mcp_unfenced = lambda *_args: (
            service.plugin_backend._provider_update_lookup(status)
        )
        service.plugin_runtime._execute_unfenced = lambda _request: (
            service.plugin_backend.execute_sync_for_plugin(
                object(), PluginRuntimeOperation.TOOL_CALL, {}
            )
        )
        service.plugin_runtime.execute(object())
        assert lock_states == [False]
    finally:
        service.close()


def test_composed_runtime_retries_after_fenced_compatible_update(
    tmp_path: Path,
) -> None:
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"q" * 32,
        operation_submitter=lambda operation, **_kwargs: asyncio.run(operation()),
    )
    try:
        backend = service.plugin_backend
        assert backend is not None
        verified = service._verified("google.workspace.drive")[2]
        record = InstalledMcpAppRecord.from_source(
            TrustedRemoteAppSource(requirement=verified.manifest.required_apps[0])
        )
        plugin_registry = SimpleNamespace(
            get=lambda plugin_id: SimpleNamespace(
                plugin_id=plugin_id,
                status="installed",
                enabled=True,
            )
        )
        app_registry = SimpleNamespace(get=lambda _app_id: record)
        runtime = PluginRuntime(
            plugin_registry=plugin_registry,
            app_registry=app_registry,
            backend=backend,
            mutation_scope=service.plugin_execution_mutation_scope,
        )
        service.check_updates = lambda **_kwargs: type(
            "Result",
            (),
            {
                "status": "checked",
                "candidate_versions": (("google.workspace.drive", "1.1.0"),),
                "staged": (),
                "approval_required": (),
                "error": None,
            },
        )()
        applied: list[tuple[str, str]] = []
        service.apply_compatible_update = lambda plugin_id, version: (
            applied.append((plugin_id, version))
            or PluginUpdateResult(plugin_id, version, "applied")
        )
        provider_calls: list[str] = []

        def call_once(
            _plugin_id: str,
            _app_id: str,
            _name: str,
            _arguments: dict[str, object],
        ) -> dict[str, object]:
            provider_calls.append("call")
            if len(provider_calls) == 1:
                backend._record_provider_failure(
                    McpClientErrorReason.CAPABILITY_DRIFT,
                    phase="discovery",
                    plugin_id="google.workspace.drive",
                )
                raise PluginRuntimeError("plugin_connection_unavailable")
            return {"ok": True}

        backend._call_tool_once = call_once
        result = runtime.invoke_tool(
            PluginOperationRequest(
                plugin_id="google.workspace.drive",
                app_id=record.app_id,
                capability="search_files",
                arguments={},
            )
        )
        assert result.to_dict()["data"] == {"ok": True}
        assert provider_calls == ["call", "call"]
        assert applied == [("google.workspace.drive", "1.1.0")]
    finally:
        service.close()


def test_composed_auto_outer_scope_retries_after_fenced_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"q" * 32,
        operation_submitter=lambda operation, **_kwargs: asyncio.run(operation()),
    )
    try:
        backend = service.plugin_backend
        assert backend is not None
        verified = service._verified("google.workspace.drive")[2]
        record = InstalledMcpAppRecord.from_source(
            TrustedRemoteAppSource(requirement=verified.manifest.required_apps[0])
        )
        runtime = PluginRuntime(
            plugin_registry=SimpleNamespace(
                get=lambda plugin_id: SimpleNamespace(
                    plugin_id=plugin_id, status="installed", enabled=True
                )
            ),
            app_registry=SimpleNamespace(get=lambda _app_id: record),
            backend=backend,
        )
        runtime_client = SimpleNamespace(
            plugin_runtime=runtime,
            plugin_execution_mutation_scope=service.plugin_execution_mutation_scope,
        )
        binding = SimpleNamespace(
            plugin_id="google.workspace.drive",
            app_id=record.app_id,
            capability="search_files",
            risk_class=RiskClass.READ_ONLY,
            candidate_id="a" * 64,
            input_schema_sha256="b" * 64,
            account_key_hash="c" * 64,
            package_revision="d" * 64,
            connection_revision="e" * 64,
            authority_revision="f" * 64,
            capability_revision="1" * 64,
            binding_revision="2" * 64,
        )
        monkeypatch.setattr(
            "auto_loop.execute.plugin_tool._signed_binding_for",
            lambda *_args: binding,
        )
        service.check_updates = lambda **_kwargs: type(
            "Result",
            (),
            {
                "status": "checked",
                "candidate_versions": (("google.workspace.drive", "1.1.0"),),
                "staged": (),
                "approval_required": (),
                "error": None,
            },
        )()
        applied: list[tuple[str, str]] = []
        service.apply_compatible_update = lambda plugin_id, version: (
            applied.append((plugin_id, version))
            or PluginUpdateResult(plugin_id, version, "applied")
        )
        provider_calls: list[str] = []

        def call_once(
            _plugin_id: str,
            _app_id: str,
            _name: str,
            _arguments: dict[str, object],
        ) -> dict[str, object]:
            provider_calls.append("call")
            if len(provider_calls) == 1:
                backend._record_provider_failure(
                    McpClientErrorReason.CAPABILITY_DRIFT,
                    phase="discovery",
                    plugin_id="google.workspace.drive",
                )
                raise PluginRuntimeError("plugin_connection_unavailable")
            return {"ok": True}

        backend._call_tool_once = call_once
        result = run_plugin_operation(
            {
                "plugin_id": "google.workspace.drive",
                "app_id": record.app_id,
                "capability": "search_files",
                "arguments": {},
            },
            runtime_client=runtime_client,
        )
        assert result["status"] == "success"
        assert provider_calls == ["call", "call"]
        assert applied == [("google.workspace.drive", "1.1.0")]
    finally:
        service.close()


def test_recovery_budget_is_shared_with_the_single_post_update_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"q" * 32,
        operation_submitter=lambda operation, **_kwargs: asyncio.run(operation()),
    )
    try:
        backend = service.plugin_backend
        assert backend is not None
        verified = service._verified("google.workspace.drive")[2]
        record = InstalledMcpAppRecord.from_source(
            TrustedRemoteAppSource(requirement=verified.manifest.required_apps[0])
        )
        runtime = PluginRuntime(
            plugin_registry=SimpleNamespace(
                get=lambda plugin_id: SimpleNamespace(
                    plugin_id=plugin_id, status="installed", enabled=True
                )
            ),
            app_registry=SimpleNamespace(get=lambda _app_id: record),
            backend=backend,
        )
        runtime_client = SimpleNamespace(
            plugin_runtime=runtime,
            plugin_execution_mutation_scope=service.plugin_execution_mutation_scope,
        )
        binding = SimpleNamespace(
            plugin_id="google.workspace.drive",
            app_id=record.app_id,
            capability="search_files",
            risk_class=RiskClass.READ_ONLY,
            candidate_id="a" * 64,
            input_schema_sha256="b" * 64,
            account_key_hash="c" * 64,
            package_revision="d" * 64,
            connection_revision="e" * 64,
            authority_revision="f" * 64,
            capability_revision="1" * 64,
            binding_revision="2" * 64,
        )
        monkeypatch.setattr(
            "auto_loop.execute.plugin_tool._signed_binding_for",
            lambda *_args: binding,
        )
        lookups: list[object] = []

        def check_updates(**_kwargs: object) -> object:
            lookups.append(True)
            return type(
                "Result",
                (),
                {
                    "status": "checked",
                    "candidate_versions": (("google.workspace.drive", "1.1.0"),),
                    "staged": (),
                    "approval_required": (),
                    "error": None,
                },
            )()

        service.check_updates = check_updates
        applied: list[tuple[str, str]] = []
        service.apply_compatible_update = lambda plugin_id, version: (
            applied.append((plugin_id, version))
            or PluginUpdateResult(plugin_id, version, "applied")
        )
        provider_calls: list[str] = []

        def call_once(
            _plugin_id: str,
            _app_id: str,
            _name: str,
            _arguments: dict[str, object],
        ) -> dict[str, object]:
            provider_calls.append("call")
            backend._record_provider_failure(
                McpClientErrorReason.CAPABILITY_DRIFT,
                phase="discovery",
                plugin_id="google.workspace.drive",
            )
            raise PluginRuntimeError("plugin_connection_unavailable")

        backend._call_tool_once = call_once
        with pytest.raises(PluginRuntimeError, match="^plugin_connection_unavailable$"):
            run_plugin_operation(
                {
                    "plugin_id": "google.workspace.drive",
                    "app_id": record.app_id,
                    "capability": "search_files",
                    "arguments": {},
                },
                runtime_client=runtime_client,
            )
        assert provider_calls == ["call", "call"]
        assert applied == [("google.workspace.drive", "1.1.0")]
        assert len(lookups) == 1
    finally:
        service.close()


def test_prepare_retry_clears_stale_approval_and_marks_retry_attempted(
    tmp_path: Path,
) -> None:
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"q" * 32,
        operation_submitter=lambda operation, **_kwargs: asyncio.run(operation()),
    )
    try:
        backend = service.plugin_backend
        assert backend is not None
        captured_status = McpProviderDriftStatus(
            phase="discovery",
            reason=McpClientErrorReason.CAPABILITY_DRIFT,
            plugin_id="example.plugin",
            update_lookup_attempted=True,
            approval_required=True,
            approval_diff=("scope_changed",),
        )
        backend._drift_status = captured_status
        key = ("a" * 32, "example.plugin", "example.app", "read")
        backend.prepare_plugin_retry(key, captured_status)
        status = backend._plugin_retry_states[key]
        assert status is not None
        assert status.retry_attempted is True
        assert status.update_lookup_attempted is True
        assert status.approval_required is False
        assert status.approval_diff == ()
        assert status.reconnect_required is False

        def authorization_failure(*_args: object) -> object:
            backend._record_provider_failure(
                McpClientErrorReason.AUTHORIZATION_REQUIRED,
                phase="tool-call",
                plugin_id="example.plugin",
            )
            raise PluginRuntimeError("plugin_authorization_required")

        backend._call_tool_once = authorization_failure
        from mcp_platform.plugin_runtime import plugin_retry_context

        with plugin_retry_context(key):
            with pytest.raises(PluginRuntimeError, match="^plugin_authorization_required$"):
                backend._call_tool_unfenced("example.plugin", "example.app", "read", {})
        status = backend.drift_status
        assert status is not None
        assert status.reconnect_required is True
        assert status.approval_required is False
        assert status.approval_diff == ()
    finally:
        service.close()


def test_retry_lease_is_request_scoped_across_concurrent_action_interleave(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"q" * 32,
        operation_submitter=lambda operation, **_kwargs: asyncio.run(operation()),
    )
    try:
        backend = service.plugin_backend
        assert backend is not None
        captured_status = McpProviderDriftStatus(
            phase="discovery",
            reason=McpClientErrorReason.CAPABILITY_DRIFT,
            plugin_id="plugin-a",
            update_lookup_attempted=True,
        )
        backend._drift_status = captured_status
        key_a = ("a" * 32, "plugin-a", "app-a", "read")
        retry = PluginRetryAfterFence(
            PluginRuntimeError("plugin_connection_unavailable"),
            key_a,
            captured_status,
        )
        # Action B completes after A releases its fence and clears the shared
        # backend telemetry. A's preparation must still use the immutable
        # status captured in its retry token.
        backend._drift_status = None
        backend.prepare_plugin_retry(key_a, retry.recovery_status)
        backend._call_tool_once = lambda *_args: {"ok": True}
        from mcp_platform.plugin_runtime import plugin_retry_context

        with plugin_retry_context(None):
            assert backend._call_tool_unfenced(
                "plugin-b",
                "app-b",
                "read",
                {},
            ) == {"ok": True}
        assert key_a in backend._plugin_retry_states

        lookups: list[object] = []
        monkeypatch.setattr(
            backend,
            "_provider_update_lookup",
            lambda status: lookups.append(status) or None,
        )

        def fail(*_args: object) -> object:
            raise PluginRuntimeError("plugin_connection_unavailable")

        backend._call_tool_once = fail
        with plugin_retry_context(key_a):
            with pytest.raises(PluginRuntimeError, match="^plugin_connection_unavailable$"):
                backend._call_tool_unfenced(
                    "plugin-a",
                    "app-a",
                    "read",
                    {},
                )
        assert lookups == []
        status = backend.drift_status
        assert status is not None
        assert status.update_lookup_attempted is True
        assert status.retry_attempted is True
    finally:
        service.close()


def test_prepare_retry_does_not_publish_a_snapshot_into_another_action(
    tmp_path: Path,
) -> None:
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"q" * 32,
        operation_submitter=lambda operation, **_kwargs: asyncio.run(operation()),
    )
    started = threading.Event()
    release = threading.Event()
    lookup_calls: list[str | None] = []
    try:
        backend = service.plugin_backend
        assert backend is not None
        captured_status = McpProviderDriftStatus(
            phase="discovery",
            reason=McpClientErrorReason.CAPABILITY_DRIFT,
            plugin_id="plugin-a",
            update_lookup_attempted=True,
        )
        key_a = ("a" * 32, "plugin-a", "app-a", "read")
        retry_a = PluginRetryAfterFence(
            PluginRuntimeError("plugin_connection_unavailable"),
            key_a,
            captured_status,
        )
        backend._drift_status = captured_status

        def provider_lookup(status: McpProviderDriftStatus) -> McpProviderRecovery:
            lookup_calls.append(status.plugin_id)
            return McpProviderRecovery(status="reconnect_required")

        backend._provider_update_lookup = provider_lookup

        def provider_call(
            plugin_id: str, *_args: object
        ) -> object:
            assert plugin_id == "plugin-b"
            started.set()
            assert release.wait(2.0)
            backend._record_provider_failure(
                McpClientErrorReason.CAPABILITY_DRIFT,
                phase="discovery",
                plugin_id="plugin-b",
            )
            raise PluginRuntimeError("plugin_connection_unavailable")

        backend._call_tool_once = provider_call
        errors: list[BaseException] = []

        def run_b() -> None:
            try:
                backend._call_tool_unfenced("plugin-b", "app-b", "read", {})
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(
            target=run_b,
            daemon=True,
        )
        worker.start()
        assert started.wait(2.0)
        # B is between operation admission and its provider failure. A's
        # preparation must only arm A's keyed lease, never publish A's stale
        # exhausted budget into the shared active status slot.
        backend.prepare_plugin_retry(key_a, retry_a.recovery_status)
        release.set()
        worker.join(2.0)
        assert not worker.is_alive()
        assert len(errors) == 1
        assert lookup_calls == ["plugin-b"]
        assert key_a in backend._plugin_retry_states
    finally:
        release.set()
        service.close()


@pytest.mark.parametrize("newer_time", [100.0, 200.0])
def test_older_delivery_failure_cannot_overwrite_newer_forced_check(
    tmp_path: Path,
    newer_time: float,
) -> None:
    service = _bare_service(tmp_path)
    service.plugin_registry = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(records=())
    )
    service.sync_catalog = lambda: CatalogSyncResult(
        status=CatalogSyncStatus.UNCHANGED,
        sequence=1,
        revision="a" * 64,
    )
    apply_started = threading.Event()
    release_apply = threading.Event()
    calls = 0

    def candidates() -> tuple[tuple[str, str], ...]:
        nonlocal calls
        calls += 1
        return (("example.plugin", "1.1.0"),) if calls == 1 else ()

    service._update_candidates = candidates

    def apply(_plugin_id: str, _version: str) -> PluginUpdateResult:
        apply_started.set()
        assert release_apply.wait(2.0)
        return PluginUpdateResult(
            "example.plugin", "1.1.0", "failed", error="plugin_update_failed"
        )

    service.apply_compatible_update = apply
    first: list[PluginUpdateCheckResult] = []
    worker = threading.Thread(
        target=lambda: first.append(service.check_updates(now=100.0)), daemon=True
    )
    worker.start()
    assert apply_started.wait(2.0)
    newer = service.check_updates(now=newer_time, force=True)
    assert newer.status == "checked"
    assert newer.checked_at == newer_time
    release_apply.set()
    worker.join(2.0)
    assert not worker.is_alive()
    assert first and first[0].checked_at == newer_time
    assert first[0].status == "checked"
    durable = read_update_status(service._update_status_store)
    assert durable is not None
    assert durable.checked_at == newer_time
    policy = read_policy(service._update_policy_store)
    assert policy.last_check_at == newer_time
    assert policy.next_check_at == newer_time + DEFAULT_UPDATE_INTERVAL_SECONDS


def test_deferred_publication_cas_preserves_newer_durable_generation(
    tmp_path: Path,
) -> None:
    service = _bare_service(tmp_path)
    service._update_status_store = update_status_store(tmp_path)
    first = PluginUpdateCheckResult(
        status="checked",
        checked_at=100.0,
        next_check_at=86_500.0,
        candidate_versions=(("example.plugin", "1.1.0"),),
        generation=1,
    )
    write_update_status(service._update_status_store, first)
    write_policy(
        service._update_policy_store,
        PluginUpdatePolicy(last_check_at=100.0, next_check_at=86_500.0),
    )
    second = PluginUpdateCheckResult(
        status="checked",
        checked_at=100.0,
        next_check_at=86_500.0,
        generation=2,
    )
    # A newer check commits before the deferred generation-1 delivery result
    # gets to its publication boundary.
    write_update_status(service._update_status_store, second)
    service._persist_deferred_update_results(
        (("example.plugin", "1.1.0"),),
        failures=["plugin_update_failed"],
        approval_required=[],
        origins={("example.plugin", "1.1.0"): 1},
    )
    durable = read_update_status(service._update_status_store)
    assert durable == second
    policy = read_policy(service._update_policy_store)
    assert policy.last_check_at == 100.0
    assert policy.next_check_at == 86_500.0


def test_mixed_generation_drain_keeps_current_failure_and_discards_old_one(
    tmp_path: Path,
) -> None:
    service = _bare_service(tmp_path)
    service._update_status_store = update_status_store(tmp_path)
    current = PluginUpdateCheckResult(
        status="checked",
        checked_at=200.0,
        next_check_at=86_600.0,
        generation=2,
    )
    write_update_status(service._update_status_store, current)
    write_policy(
        service._update_policy_store,
        PluginUpdatePolicy(last_check_at=200.0, next_check_at=86_600.0),
    )
    service._deferred_update_lock = threading.Lock()
    service._deferred_update_applications = [
        ("old.plugin", "1.1.0"),
        ("current.plugin", "2.1.0"),
    ]
    service._deferred_update_generations = {
        ("old.plugin", "1.1.0"): 1,
        ("current.plugin", "2.1.0"): 2,
    }

    def apply(plugin_id: str, version: str) -> PluginUpdateResult:
        return PluginUpdateResult(
            plugin_id,
            version,
            "failed",
            error=(
                "plugin_update_not_available"
                if plugin_id == "old.plugin"
                else "plugin_update_failed"
            ),
        )

    service.apply_compatible_update = apply
    service._drain_deferred_update_applications()
    durable = read_update_status(service._update_status_store)
    assert durable is not None
    assert durable.generation == 2
    assert durable.status == "failed"
    assert durable.error == "plugin_update_failed"
    assert durable.staged == (("current.plugin", "2.1.0"),)
    policy = read_policy(service._update_policy_store)
    assert policy.next_check_at is not None and policy.next_check_at > 86_600.0


def test_fenced_recovery_delivery_failure_is_durable_and_backed_off(
    tmp_path: Path,
) -> None:
    service = _bare_service(tmp_path)
    service._update_status_store = update_status_store(tmp_path)
    checked = PluginUpdateCheckResult(
        status="checked",
        checked_at=100.0,
        next_check_at=86_500.0,
        candidate_versions=(("example.plugin", "1.1.0"),),
    )
    write_update_status(service._update_status_store, checked)
    service._deferred_update_lock = threading.Lock()
    service._deferred_update_applications = [("example.plugin", "1.1.0")]
    service.apply_compatible_update = lambda *_args: PluginUpdateResult(
        "example.plugin",
        "1.1.0",
        "failed",
        error="plugin_catalog_unavailable",
    )
    service._drain_deferred_update_applications()
    result = read_update_status(service._update_status_store)
    assert result is not None
    assert result.status == "failed"
    assert result.error == "plugin_catalog_unavailable"
    policy = read_policy(service._update_policy_store)
    assert policy.next_check_at is not None
    assert policy.next_check_at > 100.0 + DEFAULT_UPDATE_INTERVAL_SECONDS


def test_delivery_failure_becomes_typed_update_result(
    tmp_path: Path,
) -> None:
    service = _bare_service(tmp_path)
    service.update_review = lambda *_args: type(
        "Review", (), {"sha256": "a" * 64, "permission_changes": ()}
    )()

    def unavailable(*_args: object) -> object:
        raise RuntimeError("plugin_catalog_unavailable")

    service._installed_verified_with_provenance = unavailable
    result = service.apply_compatible_update("example", "1.1.0")
    assert result.status == "failed"
    assert result.error == "plugin_catalog_unavailable"


@pytest.mark.anyio
async def test_due_worker_clears_transport_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _bare_service(tmp_path)
    service._update_policy = PluginUpdatePolicy(
        last_check_at=0.0, next_check_at=90.0
    )
    write_policy(service._update_policy_store, service._update_policy)
    monkeypatch.setattr(lifecycle_module.time, "time", lambda: 100.0)
    observed: list[bool] = []

    def check() -> PluginUpdateCheckResult:
        observed.append(lifecycle_module._PLUGIN_TRANSPORT_CONTEXT.get())
        return PluginUpdateCheckResult(
            status="checked", checked_at=100.0, next_check_at=200.0
        )

    service.check_updates = check
    token = lifecycle_module._PLUGIN_TRANSPORT_CONTEXT.set(True)
    try:
        await service._run_due_update_check()
    finally:
        lifecycle_module._PLUGIN_TRANSPORT_CONTEXT.reset(token)
    assert observed == [False]


def test_failed_check_is_bounded_and_does_not_expose_exception_text(tmp_path: Path) -> None:
    service = _bare_service(tmp_path)
    service.sync_catalog = lambda: (_ for _ in ()).throw(
        RuntimeError("provider-authored secret should not escape")
    )

    result = service.check_updates(now=100.0)

    assert result.status == "failed"
    assert result.error == "plugin_catalog_unavailable"
    assert result.next_check_at is not None
    assert result.next_check_at <= 100.0 + 7 * 24 * 60 * 60


def test_shutdown_check_does_no_work(tmp_path: Path) -> None:
    service = _bare_service(tmp_path)
    service._closed = True
    service.sync_catalog = lambda: (_ for _ in ()).throw(AssertionError("work"))

    result = service.check_updates(now=100.0)

    assert type(result) is PluginUpdateCheckResult
    assert result.status == "shutdown"


def test_check_result_round_trips_across_restart(tmp_path: Path) -> None:
    store = update_status_store(tmp_path)
    expected = PluginUpdateCheckResult(
        status="checked",
        checked_at=100.0,
        next_check_at=200.0,
        candidate_versions=(("example", "1.1.0"),),
        staged=(("example", "1.1.0"),),
        approval_required=(("other", "2.0.0", ("scope_changed",)),),
    )
    write_update_status(store, expected)
    assert read_update_status(update_status_store(tmp_path)) == expected

"""Boot composition and content-free MCP host status tests."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, cast

import anyio
import pytest

import mcp_platform.authority as authority_module
import mcp_platform.host as host_module
import mcp_platform.service as service_module
from mcp_platform import action_ledger
from mcp_platform.authority import McpAuthorityGateway
from mcp_platform.external_switch import ExternalMcpSwitch
from mcp_platform.host import McpHost, McpHostControlError
from mcp_platform.operation_contracts import McpExternalDisableOutcome
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION, RiskClass
from mcp_platform.registry import McpPackageNotInstalled, McpRegistryConflict
from mcp_platform.service import (
    get_default_mcp_host,
    mcp_status,
    shutdown_default_mcp_host,
    start_default_mcp_host,
)
from mcp_platform.supervisor import McpProcessSupervisor, McpServerBlocked
from tests.mcp_platform.test_supervisor import (
    FakeClock,
    FakeSessionFactory,
)
from tests.mcp_platform.test_authority import Harness

pytest_plugins = ("tests.mcp_platform.test_supervisor",)


class _InertAuthorityGateway:
    @staticmethod
    def wait_for_cleanup_sync(_timeout_s: float) -> bool:
        return True

    @staticmethod
    async def drain_package(_server_id: str) -> dict[str, int]:
        return {
            "drained_calls": 0,
            "uncertain_writes": 0,
            "failed_reads": 0,
            "errors": 0,
        }


class _DrainingAuthorityGateway(_InertAuthorityGateway):
    def __init__(
        self,
        switch: ExternalMcpSwitch,
        supervisor: McpProcessSupervisor,
    ) -> None:
        self.switch = switch
        self.supervisor = supervisor
        self.calls: list[str] = []

    async def disable_external(self, expected_revision: str) -> McpExternalDisableOutcome:
        self.calls.append(expected_revision)
        self.switch.set_enabled(False, expected_revision=expected_revision)
        stopped = self.supervisor.stop_external_sync()
        return McpExternalDisableOutcome(
            drained_calls=0,
            uncertain_writes=0,
            failed_reads=0,
            sessions_closed=stopped["sessions_closed"],
            sessions_forced=stopped["sessions_forced"],
            errors=stopped["errors"],
        )


_INERT_AUTHORITY_GATEWAY = cast(McpAuthorityGateway, _InertAuthorityGateway())


class BlockingServiceHost:
    def __init__(self, *, block_shutdown: bool) -> None:
        self.block_shutdown = block_shutdown
        self.started = False
        self.shutdown_entered = threading.Event()
        self.release_shutdown = threading.Event()
        self.observed_owner: object | None = None

    def start(self) -> tuple[object, ...]:
        self.started = True
        return ()

    def status(self) -> dict[str, object]:
        return {"started": self.started}

    def shutdown(self) -> dict[str, int]:
        self.shutdown_entered.set()
        self.observed_owner = get_default_mcp_host()
        if self.block_shutdown:
            assert self.release_shutdown.wait(timeout=2.0)
        return {"sessions_closed": 0, "sessions_forced": 0, "errors": 0}


class ReentrantServiceHost:
    def __init__(self) -> None:
        self.started = False
        self.shutdown_calls = 0
        self.reentry_started = threading.Event()
        self.observed_owner: object | None = None
        self.nested_result: dict[str, int] | None = None

    def start(self) -> tuple[object, ...]:
        self.started = True
        return ()

    def status(self) -> dict[str, object]:
        return {"started": self.started}

    def shutdown(self) -> dict[str, int]:
        self.shutdown_calls += 1
        self.observed_owner = get_default_mcp_host()
        if self.shutdown_calls == 1:
            self.reentry_started.set()
            self.nested_result = shutdown_default_mcp_host()
            raise RuntimeError("host_shutdown_failed")
        return {"sessions_closed": 0, "sessions_forced": 0, "errors": 0}


class RetryableServiceHost:
    def __init__(self) -> None:
        self.started = False
        self.shutdown_calls = 0

    def start(self) -> tuple[object, ...]:
        self.started = True
        return ()

    def status(self) -> dict[str, object]:
        return {"started": self.started}

    def shutdown(self) -> dict[str, int]:
        self.shutdown_calls += 1
        return {
            "sessions_closed": 0,
            "sessions_forced": 0,
            "errors": 1 if self.shutdown_calls == 1 else 0,
        }


class BlockingRetryableServiceHost(RetryableServiceHost):
    def __init__(self) -> None:
        super().__init__()
        self.shutdown_entered = threading.Event()
        self.release_shutdown = threading.Event()

    def shutdown(self) -> dict[str, int]:
        self.shutdown_calls += 1
        if self.shutdown_calls == 1:
            self.shutdown_entered.set()
            assert self.release_shutdown.wait(timeout=2.0)
            return {"sessions_closed": 0, "sessions_forced": 0, "errors": 1}
        return {"sessions_closed": 0, "sessions_forced": 0, "errors": 0}


def test_host_boot_reconciles_only_currently_eligible_packages(
    installed_packages: tuple[Any, ExternalMcpSwitch, list[Path]],
) -> None:
    registry, switch, _calls = installed_packages
    factory = FakeSessionFactory()
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,
        session_factory=factory,
        monotonic=FakeClock(),
    )
    host = McpHost(
        registry=registry,
        external_switch=switch,
        supervisor=supervisor,
        authority_gateway=_INERT_AUTHORITY_GATEWAY,
    )

    started = host.start()
    try:
        assert [snapshot.server_id for snapshot in started] == ["omega.first_party.local"]
        assert [launch.verified_package.manifest.server_id for launch in factory.started] == [
            "omega.first_party.local"
        ]
    finally:
        host.shutdown()


def test_host_requires_the_authority_gateway_dependency(
    installed_packages: tuple[Any, ExternalMcpSwitch, list[Path]],
) -> None:
    registry, switch, _calls = installed_packages
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,
        session_factory=FakeSessionFactory(),
        monotonic=FakeClock(),
    )

    with pytest.raises(TypeError):
        McpHost(
            registry=registry,
            external_switch=switch,
            supervisor=supervisor,
        )


def test_host_shutdown_never_overlaps_or_orphans_deferred_session_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(authority_module, "EXTERNAL_CALL_DRAIN_TIMEOUT_S", 0.05)
    monkeypatch.setattr(
        host_module,
        "MCP_CLEANUP_SHUTDOWN_TIMEOUT_S",
        0.05,
        raising=False,
    )
    harness = Harness(tmp_path)
    stop_entered = threading.Event()
    stop_returned = threading.Event()
    release_stop = threading.Event()
    shutdown_observations: list[bool] = []

    def slow_stop(_remaining: float) -> dict[str, int]:
        stop_entered.set()
        assert release_stop.wait(2.0)
        stop_returned.set()
        return {"sessions_closed": 1, "sessions_forced": 0, "errors": 0}

    def shutdown() -> dict[str, int]:
        shutdown_observations.append(stop_returned.is_set())
        return {"sessions_closed": 0, "sessions_forced": 0, "errors": 0}

    harness.supervisor.stop_external_sync = slow_stop  # type: ignore[method-assign]
    harness.supervisor.shutdown = shutdown  # type: ignore[attr-defined]
    host = McpHost(
        registry=harness.registry,
        external_switch=harness.switch,
        supervisor=harness.supervisor,  # type: ignore[arg-type]
        authority_gateway=harness.gateway,
    )

    drain = anyio.run(harness.gateway.disable_external, harness.switch.load().revision)
    assert drain.errors == 1
    assert stop_entered.wait(1.0)
    started = time.monotonic()
    first = host.shutdown()
    elapsed = time.monotonic() - started
    release_stop.set()
    assert stop_returned.wait(1.0)
    second = host.shutdown()

    assert elapsed < 0.20
    assert first == {"sessions_closed": 0, "sessions_forced": 0, "errors": 1}
    assert second == {"sessions_closed": 0, "sessions_forced": 0, "errors": 0}
    assert shutdown_observations == [True]
    assert harness.gateway._cleanup_threads == set()


def test_host_shutdown_retains_pending_deadline_cleanup_until_storage_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(authority_module, "EXTERNAL_CALL_DRAIN_TIMEOUT_S", 0.05)
    monkeypatch.setattr(host_module, "MCP_CLEANUP_SHUTDOWN_TIMEOUT_S", 0.05)
    harness = Harness(tmp_path, RiskClass.MUTATING_WRITE)
    harness.supervisor.block_call = True
    shutdown_calls = 0
    call_finished = threading.Event()
    storage_held = threading.Event()
    release_storage = threading.Event()

    def shutdown() -> dict[str, int]:
        nonlocal shutdown_calls
        shutdown_calls += 1
        return {"sessions_closed": 0, "sessions_forced": 0, "errors": 0}

    harness.supervisor.shutdown = shutdown  # type: ignore[attr-defined]
    host = McpHost(
        registry=harness.registry,
        external_switch=harness.switch,
        supervisor=harness.supervisor,  # type: ignore[arg-type]
        authority_gateway=harness.gateway,
    )

    def execute() -> None:
        try:
            anyio.run(harness.gateway.execute, harness.proposal)
        finally:
            call_finished.set()

    def hold_storage() -> None:
        with action_ledger._lock:
            storage_held.set()
            assert release_storage.wait(2.0)

    caller = threading.Thread(target=execute)
    holder = threading.Thread(target=hold_storage)
    caller.start()
    assert harness.supervisor.call_entered.wait(2.0)
    holder.start()
    assert storage_held.wait(2.0)
    first_disable = anyio.run(
        harness.gateway.disable_external,
        harness.switch.load().revision,
    )
    harness.supervisor.release_call.set()
    assert call_finished.wait(0.20)
    first_shutdown = host.shutdown()
    release_storage.set()
    holder.join(2.0)
    caller.join(2.0)
    second_shutdown = host.shutdown()

    record = action_ledger.find_by_idempotency(
        harness.proposal.idempotency_key,
        path=harness.ledger_path,
    )
    assert not holder.is_alive()
    assert not caller.is_alive()
    assert first_disable.errors == 1
    assert first_shutdown == {"sessions_closed": 0, "sessions_forced": 0, "errors": 1}
    assert second_shutdown == {"sessions_closed": 0, "sessions_forced": 0, "errors": 0}
    assert shutdown_calls == 1
    assert record is not None and record["state"] == "outcome_unknown"
    assert harness.supervisor.call_count == 1
    with harness.gateway._active_condition:
        assert harness.gateway._active_external == {}


def test_host_shutdown_retains_local_package_cleanup_until_storage_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(authority_module, "PACKAGE_CALL_DRAIN_TIMEOUT_S", 0.05)
    monkeypatch.setattr(host_module, "MCP_CLEANUP_SHUTDOWN_TIMEOUT_S", 0.05)
    harness = Harness(
        tmp_path,
        RiskClass.MUTATING_WRITE,
        network_class=authority_module.NetworkClass.LOCAL_ONLY,
    )
    harness.supervisor.block_call = True
    shutdown_calls = 0
    call_finished = threading.Event()
    storage_held = threading.Event()
    release_storage = threading.Event()

    def shutdown() -> dict[str, int]:
        nonlocal shutdown_calls
        shutdown_calls += 1
        return {"sessions_closed": 0, "sessions_forced": 0, "errors": 0}

    harness.supervisor.shutdown = shutdown  # type: ignore[attr-defined]
    host = McpHost(
        registry=harness.registry,
        external_switch=harness.switch,
        supervisor=harness.supervisor,  # type: ignore[arg-type]
        authority_gateway=harness.gateway,
    )

    def execute() -> None:
        try:
            anyio.run(harness.gateway.execute, harness.proposal)
        finally:
            call_finished.set()

    def hold_storage() -> None:
        with action_ledger._lock:
            storage_held.set()
            assert release_storage.wait(2.0)

    caller = threading.Thread(target=execute)
    holder = threading.Thread(target=hold_storage)
    caller.start()
    assert harness.supervisor.call_entered.wait(2.0)
    holder.start()
    assert storage_held.wait(2.0)
    first_drain = anyio.run(
        harness.gateway.drain_package,
        harness.proposal.server_id,
    )
    harness.supervisor.release_call.set()
    assert call_finished.wait(0.20)
    first_shutdown = host.shutdown()
    release_storage.set()
    holder.join(2.0)
    caller.join(2.0)
    second_shutdown = host.shutdown()

    record = action_ledger.find_by_idempotency(
        harness.proposal.idempotency_key,
        path=harness.ledger_path,
    )
    assert not holder.is_alive()
    assert not caller.is_alive()
    assert first_drain["errors"] == 1
    assert first_shutdown == {"sessions_closed": 0, "sessions_forced": 0, "errors": 1}
    assert second_shutdown == {"sessions_closed": 0, "sessions_forced": 0, "errors": 0}
    assert shutdown_calls == 1
    assert record is not None and record["state"] == "outcome_unknown"
    assert record["failure_reason"] == "package_disabled"
    with harness.gateway._active_condition:
        assert harness.gateway._active_calls == {}


def test_failed_boot_reconciliation_closes_the_new_transport_thread(
    installed_packages: tuple[Any, ExternalMcpSwitch, list[Path]],
) -> None:
    registry, switch, _calls = installed_packages
    record = registry.get("omega.first_party.local")
    (registry.package_path(record) / "omega-mcp-fake.exe").write_bytes(b"corrupt")
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,
        session_factory=FakeSessionFactory(),
        monotonic=FakeClock(),
    )
    host = McpHost(
        registry=registry,
        external_switch=switch,
        supervisor=supervisor,
        authority_gateway=_INERT_AUTHORITY_GATEWAY,
    )

    try:
        with pytest.raises(McpServerBlocked, match="^artifact_digest_mismatch$"):
            host.start()
        assert supervisor.thread_alive() is False
    finally:
        if supervisor.thread_alive():
            host.shutdown()


def test_status_is_content_free_and_reports_switch_packages_sessions_and_digests(
    installed_packages: tuple[Any, ExternalMcpSwitch, list[Path]],
) -> None:
    registry, switch, _calls = installed_packages
    factory = FakeSessionFactory()
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,
        session_factory=factory,
        monotonic=FakeClock(),
    )
    host = McpHost(
        registry=registry,
        external_switch=switch,
        supervisor=supervisor,
        authority_gateway=_INERT_AUTHORITY_GATEWAY,
    )
    host.start()
    try:
        status = host.status()
    finally:
        host.shutdown()

    assert set(status) == {"external_mcp", "registry_revision", "packages"}
    assert status["external_mcp"] == {
        "enabled": False,
        "revision": switch.load().revision,
    }
    assert status["registry_revision"] == registry.snapshot().revision
    assert [package["server_id"] for package in status["packages"]] == [
        "omega.first_party.external",
        "omega.first_party.local",
    ]
    local = status["packages"][1]
    assert local["session_state"] == "ready"
    assert local["protocol_version"] == MCP_PROTOCOL_VERSION
    assert local["crash_count"] == 0
    assert len(local["tool_catalog_sha256"]) == 64
    assert len(local["resource_catalog_sha256"]) == 64
    serialized = json.dumps(status).lower()
    for forbidden in (
        "content",
        "token",
        "secret",
        "password",
        "arguments",
        "command",
        "account",
        "tenant",
    ):
        assert forbidden not in serialized


def test_external_enable_cas_reconciles_enabled_eligible_external_packages(
    installed_packages: tuple[Any, ExternalMcpSwitch, list[Path]],
) -> None:
    registry, switch, _calls = installed_packages
    factory = FakeSessionFactory()
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,
        session_factory=factory,
        monotonic=FakeClock(),
    )
    host = McpHost(
        registry=registry,
        external_switch=switch,
        supervisor=supervisor,
        authority_gateway=_INERT_AUTHORITY_GATEWAY,
    )
    host.start()
    try:
        result = anyio.run(
            host.set_external_enabled,
            True,
            switch.load().revision,
        )
    finally:
        host.shutdown()

    assert result["reconciliation"] == {"started": 1, "blocked": 0, "errors": 0}
    assert result["status"]["external_mcp"]["enabled"] is True
    assert [launch.verified_package.manifest.server_id for launch in factory.started] == [
        "omega.first_party.local",
        "omega.first_party.external",
    ]


def test_external_disable_delegates_authority_drain_before_final_status(
    installed_packages: tuple[Any, ExternalMcpSwitch, list[Path]],
) -> None:
    registry, switch, _calls = installed_packages
    factory = FakeSessionFactory()
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,
        session_factory=factory,
        monotonic=FakeClock(),
    )
    switch.set_enabled(True, expected_revision=switch.load().revision)
    gateway = _DrainingAuthorityGateway(switch, supervisor)
    host = McpHost(
        registry=registry,
        external_switch=switch,
        supervisor=supervisor,
        authority_gateway=cast(McpAuthorityGateway, gateway),
    )
    host.start()
    revision = switch.load().revision
    try:
        result = anyio.run(host.set_external_enabled, False, revision)
    finally:
        host.shutdown()

    assert gateway.calls == [revision]
    assert result["drain"]["sessions_closed"] == 1
    assert result["status"]["external_mcp"]["enabled"] is False
    external = result["status"]["packages"][0]
    assert external["session_state"] == "stopped"


def test_package_disable_stops_exact_session_before_returning_final_row(
    installed_packages: tuple[Any, ExternalMcpSwitch, list[Path]],
) -> None:
    registry, switch, _calls = installed_packages
    factory = FakeSessionFactory()
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,
        session_factory=factory,
        monotonic=FakeClock(),
    )
    host = McpHost(
        registry=registry,
        external_switch=switch,
        supervisor=supervisor,
        authority_gateway=_INERT_AUTHORITY_GATEWAY,
    )
    host.start()
    try:
        result = anyio.run(
            host.set_package_enabled,
            "omega.first_party.local",
            False,
            registry.snapshot().revision,
        )
    finally:
        host.shutdown()

    assert result["package"]["server_id"] == "omega.first_party.local"
    assert result["package"]["enabled"] is False
    assert result["package"]["session_state"] == "stopped"
    assert result["registry_revision"] == registry.snapshot().revision


def test_package_disable_authority_drains_before_stopping_exact_session(
    installed_packages: tuple[Any, ExternalMcpSwitch, list[Path]],
) -> None:
    registry, switch, _calls = installed_packages
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,
        session_factory=FakeSessionFactory(),
        monotonic=FakeClock(),
    )
    order: list[str] = []

    class OrderedGateway(_InertAuthorityGateway):
        @staticmethod
        async def drain_package(server_id: str) -> dict[str, int]:
            assert server_id == "omega.first_party.local"
            order.append("drain")
            return {
                "drained_calls": 1,
                "uncertain_writes": 0,
                "failed_reads": 0,
                "errors": 0,
            }

    original_stop = supervisor.stop_server_sync

    def ordered_stop(server_id: str) -> bool:
        order.append("stop")
        return original_stop(server_id)

    supervisor.stop_server_sync = ordered_stop  # type: ignore[method-assign]
    host = McpHost(
        registry=registry,
        external_switch=switch,
        supervisor=supervisor,
        authority_gateway=cast(McpAuthorityGateway, OrderedGateway()),
    )
    host.start()
    try:
        result = anyio.run(
            host.set_package_enabled,
            "omega.first_party.local",
            False,
            registry.snapshot().revision,
        )
    finally:
        host.shutdown()

    assert order == ["drain", "stop"]
    assert result["drain"]["drained_calls"] == 1


@pytest.mark.parametrize(
    "drain",
    [
        {"drained_calls": 0, "uncertain_writes": 0, "failed_reads": 0},
        {
            "drained_calls": True,
            "uncertain_writes": 0,
            "failed_reads": 0,
            "errors": 0,
        },
        {
            "drained_calls": 0,
            "uncertain_writes": 0,
            "failed_reads": 0,
            "errors": 1,
        },
    ],
)
def test_package_disable_rejects_invalid_or_failed_drain_before_stop(
    installed_packages: tuple[Any, ExternalMcpSwitch, list[Path]],
    drain: dict[str, Any],
) -> None:
    registry, switch, _calls = installed_packages
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,
        session_factory=FakeSessionFactory(),
        monotonic=FakeClock(),
    )

    class InvalidGateway(_InertAuthorityGateway):
        @staticmethod
        async def drain_package(_server_id: str) -> dict[str, int]:
            return cast(dict[str, int], drain)

    stopped: list[str] = []
    original_stop = supervisor.stop_server_sync

    def record_stop(server_id: str) -> bool:
        stopped.append(server_id)
        return original_stop(server_id)

    supervisor.stop_server_sync = record_stop  # type: ignore[method-assign]
    host = McpHost(
        registry=registry,
        external_switch=switch,
        supervisor=supervisor,
        authority_gateway=cast(McpAuthorityGateway, InvalidGateway()),
    )
    host.start()
    try:
        with pytest.raises(McpHostControlError, match="mcp_control_reconcile_failed"):
            anyio.run(
                host.set_package_enabled,
                "omega.first_party.local",
                False,
                registry.snapshot().revision,
            )
    finally:
        host.shutdown()

    assert stopped == []


@pytest.mark.parametrize(
    ("server_id", "should_start"),
    [
        ("omega.first_party.external", False),
        ("omega.first_party.local", True),
    ],
)
def test_package_enable_respects_external_off_but_keeps_local_only_independent(
    installed_packages: tuple[Any, ExternalMcpSwitch, list[Path]],
    server_id: str,
    should_start: bool,
) -> None:
    registry, switch, _calls = installed_packages
    registry.set_enabled(
        server_id,
        False,
        expected_revision=registry.snapshot().revision,
    )
    factory = FakeSessionFactory()
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,
        session_factory=factory,
        monotonic=FakeClock(),
    )
    host = McpHost(
        registry=registry,
        external_switch=switch,
        supervisor=supervisor,
        authority_gateway=_INERT_AUTHORITY_GATEWAY,
    )
    host.start()
    try:
        result = anyio.run(
            host.set_package_enabled,
            server_id,
            True,
            registry.snapshot().revision,
        )
    finally:
        host.shutdown()

    assert result["package"]["enabled"] is True
    assert (result["package"]["session_state"] == "ready") is should_start
    assert result["package"]["blocker"] is None


def test_package_enable_verification_failure_is_blocked_and_redacted(
    installed_packages: tuple[Any, ExternalMcpSwitch, list[Path]],
) -> None:
    registry, switch, _calls = installed_packages
    registry.set_enabled(
        "omega.first_party.local",
        False,
        expected_revision=registry.snapshot().revision,
    )
    record = registry.get("omega.first_party.local")
    (registry.package_path(record) / "omega-mcp-fake.exe").write_bytes(b"secret package corruption")
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,
        session_factory=FakeSessionFactory(),
        monotonic=FakeClock(),
    )
    host = McpHost(
        registry=registry,
        external_switch=switch,
        supervisor=supervisor,
        authority_gateway=_INERT_AUTHORITY_GATEWAY,
    )
    host.start()
    try:
        result = anyio.run(
            host.set_package_enabled,
            "omega.first_party.local",
            True,
            registry.snapshot().revision,
        )
    finally:
        host.shutdown()

    assert result["reconciliation"] == "blocked"
    assert result["package"]["blocker"] == "artifact_digest_mismatch"
    assert "secret package corruption" not in json.dumps(result)


def test_package_enable_reverifies_and_stops_an_existing_ready_session(
    installed_packages: tuple[Any, ExternalMcpSwitch, list[Path]],
) -> None:
    registry, switch, _calls = installed_packages
    factory = FakeSessionFactory()
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,
        session_factory=factory,
        monotonic=FakeClock(),
    )
    host = McpHost(
        registry=registry,
        external_switch=switch,
        supervisor=supervisor,
        authority_gateway=_INERT_AUTHORITY_GATEWAY,
    )
    host.start()
    record = registry.get("omega.first_party.local")
    (registry.package_path(record) / "omega-mcp-fake.exe").write_bytes(b"corrupt")
    try:
        result = anyio.run(
            host.set_package_enabled,
            "omega.first_party.local",
            True,
            registry.snapshot().revision,
        )
    finally:
        host.shutdown()

    assert result["reconciliation"] == "blocked"
    assert result["package"]["blocker"] == "artifact_digest_mismatch"
    assert result["package"]["session_state"] == "stopped"
    assert factory.open_sessions == 0


def test_package_control_rejects_stale_and_unknown_without_lifecycle_work(
    installed_packages: tuple[Any, ExternalMcpSwitch, list[Path]],
) -> None:
    registry, switch, _calls = installed_packages
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,
        session_factory=FakeSessionFactory(),
        monotonic=FakeClock(),
    )
    host = McpHost(
        registry=registry,
        external_switch=switch,
        supervisor=supervisor,
        authority_gateway=_INERT_AUTHORITY_GATEWAY,
    )
    host.start()
    stale = registry.snapshot().revision
    registry.block("omega.first_party.local", "operator_blocked")
    try:
        with pytest.raises(McpRegistryConflict, match="^mcp_registry_conflict$"):
            anyio.run(
                host.set_package_enabled,
                "omega.first_party.local",
                False,
                stale,
            )
        with pytest.raises(McpPackageNotInstalled, match="^package_not_installed$"):
            anyio.run(
                host.set_package_enabled,
                "omega.first_party.missing",
                False,
                registry.snapshot().revision,
            )
    finally:
        host.shutdown()


def test_unexpected_package_reconciliation_failure_is_content_free(
    installed_packages: tuple[Any, ExternalMcpSwitch, list[Path]],
) -> None:
    registry, switch, _calls = installed_packages
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,
        session_factory=FakeSessionFactory(),
        monotonic=FakeClock(),
    )
    host = McpHost(
        registry=registry,
        external_switch=switch,
        supervisor=supervisor,
        authority_gateway=_INERT_AUTHORITY_GATEWAY,
    )
    host.start()
    supervisor.stop_server_sync = lambda _server_id: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("secret provider failure")
    )
    try:
        with pytest.raises(
            McpHostControlError,
            match="^mcp_control_reconcile_failed$",
        ):
            anyio.run(
                host.set_package_enabled,
                "omega.first_party.local",
                False,
                registry.snapshot().revision,
            )
    finally:
        host.shutdown()


def test_public_package_controls_are_serialized_across_reconciliation(
    installed_packages: tuple[Any, ExternalMcpSwitch, list[Path]],
) -> None:
    registry, switch, _calls = installed_packages
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,
        session_factory=FakeSessionFactory(),
        monotonic=FakeClock(),
    )
    host = McpHost(
        registry=registry,
        external_switch=switch,
        supervisor=supervisor,
        authority_gateway=_INERT_AUTHORITY_GATEWAY,
    )
    host.start()
    original_stop = supervisor.stop_server_sync
    stop_entered = threading.Event()
    release_stop = threading.Event()

    def blocking_stop(server_id: str) -> bool:
        if server_id == "omega.first_party.local":
            stop_entered.set()
            assert release_stop.wait(timeout=2.0)
        return original_stop(server_id)

    supervisor.stop_server_sync = blocking_stop  # type: ignore[method-assign]

    async def exercise() -> None:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                host.set_package_enabled,
                "omega.first_party.local",
                False,
                registry.snapshot().revision,
            )
            await anyio.to_thread.run_sync(stop_entered.wait, 2.0)
            second_revision = registry.snapshot().revision
            tasks.start_soon(
                host.set_package_enabled,
                "omega.first_party.external",
                False,
                second_revision,
            )
            await anyio.sleep(0.05)
            assert registry.get("omega.first_party.external").enabled is True
            release_stop.set()

    try:
        anyio.run(exercise)
    finally:
        release_stop.set()
        host.shutdown()

    assert registry.get("omega.first_party.local").enabled is False
    assert registry.get("omega.first_party.external").enabled is False


def test_legacy_disable_serializes_with_package_control(
    installed_packages: tuple[Any, ExternalMcpSwitch, list[Path]],
) -> None:
    registry, switch, _calls = installed_packages
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,
        session_factory=FakeSessionFactory(),
        monotonic=FakeClock(),
    )
    disable_entered = anyio.Event()
    release_disable = anyio.Event()

    class BlockingGateway(_InertAuthorityGateway):
        async def disable_external(
            self,
            _expected_revision: str,
        ) -> McpExternalDisableOutcome:
            disable_entered.set()
            await release_disable.wait()
            return McpExternalDisableOutcome(0, 0, 0, 0, 0, 0)

    host = McpHost(
        registry=registry,
        external_switch=switch,
        supervisor=supervisor,
        authority_gateway=cast(McpAuthorityGateway, BlockingGateway()),
    )
    host.start()

    async def exercise() -> None:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(host.disable_external, switch.load().revision)
            await disable_entered.wait()
            revision = registry.snapshot().revision
            tasks.start_soon(
                host.set_package_enabled,
                "omega.first_party.local",
                False,
                revision,
            )
            await anyio.sleep(0.05)
            assert registry.get("omega.first_party.local").enabled is True
            release_disable.set()

    try:
        anyio.run(exercise)
    finally:
        release_disable.set()
        host.shutdown()

    assert registry.get("omega.first_party.local").enabled is False


def test_default_service_accepts_an_injected_boot_owned_host(
    installed_packages: tuple[Any, ExternalMcpSwitch, list[Path]],
) -> None:
    registry, switch, _calls = installed_packages
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=switch,
        verifier=registry._verifier,
        session_factory=FakeSessionFactory(),
        monotonic=FakeClock(),
    )
    injected = McpHost(
        registry=registry,
        external_switch=switch,
        supervisor=supervisor,
        authority_gateway=_INERT_AUTHORITY_GATEWAY,
    )

    assert start_default_mcp_host(host=injected) is injected
    assert get_default_mcp_host() is injected
    assert mcp_status() == injected.status()
    result = shutdown_default_mcp_host()

    assert result["sessions_closed"] == 1
    assert supervisor.thread_alive() is False


def test_default_service_reserves_singleton_ownership_until_shutdown_finishes() -> None:
    old_host = BlockingServiceHost(block_shutdown=True)
    new_host = BlockingServiceHost(block_shutdown=False)
    start_default_mcp_host(host=old_host)  # type: ignore[arg-type]
    shutdown_errors: list[BaseException] = []
    start_errors: list[BaseException] = []
    start_returned = threading.Event()

    def shutdown_old() -> None:
        try:
            shutdown_default_mcp_host()
        except BaseException as exc:
            shutdown_errors.append(exc)

    def start_new() -> None:
        try:
            start_default_mcp_host(host=new_host)  # type: ignore[arg-type]
            start_returned.set()
        except BaseException as exc:
            start_errors.append(exc)

    shutdown_thread = threading.Thread(target=shutdown_old)
    start_thread = threading.Thread(target=start_new)
    shutdown_thread.start()
    assert old_host.shutdown_entered.wait(timeout=2.0)
    start_thread.start()
    try:
        assert start_returned.wait(timeout=0.05) is False
        assert new_host.started is False
        assert old_host.observed_owner is old_host
        old_host.release_shutdown.set()
        shutdown_thread.join(timeout=2.0)
        start_thread.join(timeout=2.0)
        assert start_returned.is_set()
        assert get_default_mcp_host() is new_host
        assert shutdown_errors == []
        assert start_errors == []
    finally:
        old_host.release_shutdown.set()
        shutdown_thread.join(timeout=2.0)
        start_thread.join(timeout=2.0)
        if new_host.started:
            shutdown_default_mcp_host()


def test_default_service_retains_ownership_until_retryable_shutdown_succeeds() -> None:
    host = RetryableServiceHost()
    replacement = BlockingServiceHost(block_shutdown=False)
    start_default_mcp_host(host=host)  # type: ignore[arg-type]

    first = shutdown_default_mcp_host()

    assert first == {"sessions_closed": 0, "sessions_forced": 0, "errors": 1}
    assert get_default_mcp_host() is host
    with pytest.raises(RuntimeError, match="^default_mcp_host_already_started$"):
        start_default_mcp_host(host=replacement)  # type: ignore[arg-type]
    assert replacement.started is False

    second = shutdown_default_mcp_host()

    assert second == {"sessions_closed": 0, "sessions_forced": 0, "errors": 0}
    assert host.shutdown_calls == 2
    with pytest.raises(RuntimeError, match="^default_mcp_host_not_started$"):
        get_default_mcp_host()


def test_concurrent_shutdown_waiter_receives_the_exact_owner_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ObservedCondition(threading.Condition):
        def __init__(self) -> None:
            super().__init__()
            self.wait_entered = threading.Event()

        def wait(self, timeout: float | None = None) -> bool:
            self.wait_entered.set()
            return super().wait(timeout)

    observed_condition = ObservedCondition()
    monkeypatch.setattr(service_module, "_default_condition", observed_condition)
    host = BlockingRetryableServiceHost()
    replacement = BlockingServiceHost(block_shutdown=False)
    start_default_mcp_host(host=host)  # type: ignore[arg-type]
    owner_results: list[dict[str, int]] = []
    waiter_results: list[dict[str, int]] = []

    owner = threading.Thread(target=lambda: owner_results.append(shutdown_default_mcp_host()))
    waiter = threading.Thread(target=lambda: waiter_results.append(shutdown_default_mcp_host()))
    owner.start()
    assert host.shutdown_entered.wait(timeout=2.0)
    waiter.start()
    assert observed_condition.wait_entered.wait(timeout=2.0)
    try:
        host.release_shutdown.set()
        owner.join(timeout=2.0)
        waiter.join(timeout=2.0)
        assert not owner.is_alive()
        assert not waiter.is_alive()
        failure = {"sessions_closed": 0, "sessions_forced": 0, "errors": 1}
        assert owner_results == [failure]
        assert waiter_results == [failure]
        assert host.shutdown_calls == 1
        assert get_default_mcp_host() is host
        with pytest.raises(RuntimeError, match="^default_mcp_host_already_started$"):
            start_default_mcp_host(host=replacement)  # type: ignore[arg-type]
        assert replacement.started is False
        assert shutdown_default_mcp_host() == {
            "sessions_closed": 0,
            "sessions_forced": 0,
            "errors": 0,
        }
        assert host.shutdown_calls == 2
        with pytest.raises(RuntimeError, match="^default_mcp_host_not_started$"):
            get_default_mcp_host()
    finally:
        host.release_shutdown.set()
        owner.join(timeout=2.0)
        waiter.join(timeout=2.0)


def test_default_service_shutdown_is_idempotent_for_same_thread_reentry() -> None:
    host = ReentrantServiceHost()
    start_default_mcp_host(host=host)  # type: ignore[arg-type]
    completed = threading.Event()
    errors: list[BaseException] = []

    def shutdown() -> None:
        try:
            shutdown_default_mcp_host()
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()

    thread = threading.Thread(target=shutdown)
    thread.start()
    assert host.reentry_started.wait(timeout=2.0)
    try:
        assert completed.wait(timeout=0.2)
        assert host.nested_result == {
            "sessions_closed": 0,
            "sessions_forced": 0,
            "errors": 0,
        }
        assert host.shutdown_calls == 1
        assert host.observed_owner is host
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)
        assert str(errors[0]) == "host_shutdown_failed"
        assert get_default_mcp_host() is host
        assert shutdown_default_mcp_host() == {
            "sessions_closed": 0,
            "sessions_forced": 0,
            "errors": 0,
        }
        assert host.shutdown_calls == 2
        with pytest.raises(RuntimeError, match="^default_mcp_host_not_started$"):
            get_default_mcp_host()
    finally:
        if not completed.is_set():
            # Release the old implementation's self-wait so RED never leaks a
            # non-daemon test thread.
            with service_module._default_condition:
                service_module._default_state = "running"
                service_module._default_condition.notify_all()
        thread.join(timeout=2.0)

"""Boot composition and content-free status for signed MCP sessions."""

from __future__ import annotations

from typing import Any

import anyio

from mcp_platform.authority import McpAuthorityGateway
from mcp_platform.external_switch import ExternalMcpSwitch, mcp_eligible
from mcp_platform.operation_contracts import (
    McpDispatchOutcome,
    McpExternalDisableOutcome,
    McpOperationProposal,
)
from mcp_platform.package_contracts import NetworkClass
from mcp_platform.registry import InstalledPackageRecord, PackageRegistry
from mcp_platform.supervisor import (
    McpProcessSupervisor,
    McpServerBlocked,
    SupervisedSessionSnapshot,
)

MCP_CLEANUP_SHUTDOWN_TIMEOUT_S = 2.0


class McpHostControlError(RuntimeError):
    """A stable content-free user-control reconciliation failure."""


class McpHost:
    """Compose durable MCP control state with the boot-owned supervisor."""

    def __init__(
        self,
        *,
        registry: PackageRegistry,
        external_switch: ExternalMcpSwitch,
        supervisor: McpProcessSupervisor,
        authority_gateway: McpAuthorityGateway,
    ) -> None:
        self._registry = registry
        self._external_switch = external_switch
        self._supervisor = supervisor
        self._authority_gateway = authority_gateway
        self._control_lock = anyio.Lock()

    @property
    def supervisor(self) -> McpProcessSupervisor:
        """The boot-owned transport supervisor, exposed for plugin submission."""

        return self._supervisor

    def start(self) -> tuple[SupervisedSessionSnapshot, ...]:
        """Start transport infrastructure and reconcile enabled eligible packages."""
        self._supervisor.start()
        try:
            switch_state = self._external_switch.load()
            started: list[SupervisedSessionSnapshot] = []
            for record in self._registry.list_records():
                if (
                    record.enabled
                    and record.blocker is None
                    and mcp_eligible(NetworkClass(record.network_class), switch_state)
                ):
                    started.append(
                        self._supervisor.ensure_session_sync(record.server_id)
                    )
            return tuple(started)
        except BaseException:
            self._supervisor.shutdown()
            raise

    def status(self) -> dict[str, Any]:
        """Return only durable package identity/state and signed catalog digests."""
        switch_state = self._external_switch.load()
        registry_snapshot = self._registry.snapshot()
        sessions = self._supervisor.session_statuses_sync()
        packages = [
            _package_status(record, sessions.get(record.server_id))
            for record in registry_snapshot.records
        ]
        return {
            "external_mcp": {
                "enabled": switch_state.enabled,
                "revision": switch_state.revision,
            },
            "registry_revision": registry_snapshot.revision,
            "packages": packages,
        }

    async def set_external_enabled(
        self,
        enabled: bool,
        expected_revision: str,
    ) -> dict[str, Any]:
        """Compare-and-set the hard switch and reconcile through lifecycle owners."""
        if type(enabled) is not bool:
            raise ValueError("invalid_external_mcp_switch_enabled")
        async with self._control_lock:
            return await self._set_external_enabled_locked(
                enabled,
                expected_revision,
            )

    async def _set_external_enabled_locked(
        self,
        enabled: bool,
        expected_revision: str,
    ) -> dict[str, Any]:
        if not enabled:
            drain = await self._authority_gateway.disable_external(expected_revision)
            return {"drain": drain.to_dict(), "status": self.status()}

        self._external_switch.set_enabled(
            True,
            expected_revision=expected_revision,
        )
        started = 0
        blocked = 0
        errors = 0
        for record in self._registry.snapshot().records:
            if (
                record.network_class != NetworkClass.EXTERNAL.value
                or not record.enabled
                or record.blocker is not None
            ):
                continue
            try:
                await anyio.to_thread.run_sync(
                    self._supervisor.reconcile_package_sync,
                    record.server_id,
                )
            except McpServerBlocked:
                blocked += 1
            except Exception:
                errors += 1
            else:
                started += 1
        return {
            "reconciliation": {
                "started": started,
                "blocked": blocked,
                "errors": errors,
            },
            "status": self.status(),
        }

    async def set_package_enabled(
        self,
        server_id: str,
        enabled: bool,
        expected_registry_revision: str,
    ) -> dict[str, Any]:
        """CAS one installed package and reconcile only its owned session."""
        async with self._control_lock:
            mutation = await anyio.to_thread.run_sync(
                lambda: self._registry.set_enabled(
                    server_id,
                    enabled,
                    expected_revision=expected_registry_revision,
                )
            )
            reconciliation = "stopped"
            drain: dict[str, int] | None = None
            try:
                if not enabled:
                    drain = _validate_package_drain(
                        await self._authority_gateway.drain_package(
                            mutation.record.server_id
                        )
                    )
                    if drain["errors"] != 0:
                        raise McpHostControlError("mcp_control_reconcile_failed")
                    await anyio.to_thread.run_sync(
                        self._supervisor.stop_server_sync,
                        mutation.record.server_id,
                    )
                elif mutation.record.blocker is not None:
                    reconciliation = "blocked"
                elif not mcp_eligible(
                    NetworkClass(mutation.record.network_class),
                    self._external_switch.load(),
                ):
                    reconciliation = "stopped"
                else:
                    await anyio.to_thread.run_sync(
                        self._supervisor.reconcile_package_sync,
                        mutation.record.server_id,
                    )
                    reconciliation = "ready"
            except McpServerBlocked:
                reconciliation = "blocked"
            except McpHostControlError:
                raise
            except Exception:
                raise McpHostControlError("mcp_control_reconcile_failed") from None
            status = self.status()
            package = next(
                row
                for row in status["packages"]
                if row["server_id"] == mutation.record.server_id
            )
            result: dict[str, Any] = {
                "registry_revision": status["registry_revision"],
                "package": package,
                "reconciliation": reconciliation,
            }
            if drain is not None:
                result["drain"] = drain
            return result

    def shutdown(self) -> dict[str, int]:
        if not self._authority_gateway.wait_for_cleanup_sync(
            MCP_CLEANUP_SHUTDOWN_TIMEOUT_S
        ):
            return {"sessions_closed": 0, "sessions_forced": 0, "errors": 1}
        return self._supervisor.shutdown()

    async def execute_operation(
        self,
        proposal: McpOperationProposal | dict[str, Any],
    ) -> McpDispatchOutcome:
        """Execute through the sole public tool-operation authority boundary.

        The supervisor's transport primitives remain private host collaborators;
        callers outside the boot-owned MCP platform receive no raw tool/resource
        execution surface from this host.
        """
        return await self._authority_gateway.execute(proposal)

    async def disable_external(
        self,
        expected_revision: str,
    ) -> McpExternalDisableOutcome:
        async with self._control_lock:
            result = await self._set_external_enabled_locked(
                False,
                expected_revision,
            )
            drain = result["drain"]
            return McpExternalDisableOutcome(**drain)


def _package_status(
    record: InstalledPackageRecord,
    session: SupervisedSessionSnapshot | None,
) -> dict[str, object]:
    return {
        "server_id": record.server_id,
        "server_version": record.server_version,
        "platform": record.platform,
        "publisher_id": record.publisher_id,
        "signing_key_id": record.signing_key_id,
        "manifest_digest": record.manifest_digest,
        "network_class": record.network_class,
        "enabled": record.enabled,
        "status": record.status,
        "blocker": record.blocker,
        "crash_count": record.crash_count,
        "session_state": session.state if session is not None else "stopped",
        "protocol_version": session.protocol_version if session is not None else None,
        "tool_catalog_sha256": record.tool_catalog_sha256,
        "resource_catalog_sha256": record.resource_catalog_sha256,
    }


def _validate_package_drain(value: object) -> dict[str, int]:
    keys = {"drained_calls", "uncertain_writes", "failed_reads", "errors"}
    if type(value) is not dict or set(value) != keys:
        raise McpHostControlError("mcp_control_reconcile_failed")
    if any(type(value[key]) is not int or value[key] < 0 for key in keys):
        raise McpHostControlError("mcp_control_reconcile_failed")
    return {key: value[key] for key in sorted(keys)}

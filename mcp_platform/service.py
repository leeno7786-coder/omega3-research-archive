"""Boot-owned default access to the signed MCP host."""

from __future__ import annotations

import threading
from functools import partial
from pathlib import Path
from typing import Any, NoReturn

from core import paths as core_paths
from mcp_platform.authority import (
    AuthorityResolver,
    ConnectionResolver,
    McpAuthorityGateway,
)
from mcp_platform.external_switch import ExternalMcpSwitch
from mcp_platform.host import McpHost
from mcp_platform.package_verifier import current_platform_tag, verify_package
from mcp_platform.registry import PackageRegistry
from mcp_platform.supervisor import McpProcessSupervisor
from mcp_platform.trust_store import load_embedded_key_ring

_OMEGA_VERSION = "3.0.0"
_default_condition = threading.Condition()
_default_host: McpHost | None = None
_default_state = "empty"
_default_generation = 0
_default_shutdown_attempt = 0
_default_stopping_owner: tuple[int, int, int] | None = None
_default_shutdown_waiters: dict[tuple[int, int, int], int] = {}
_default_shutdown_results: dict[tuple[int, int, int], dict[str, int]] = {}


def _connection_resolver_unavailable(_connection_id: str) -> NoReturn:
    raise RuntimeError("mcp_connection_resolver_unavailable")


def _authority_resolver_unavailable(_kind: str, _authority_id: str) -> NoReturn:
    raise RuntimeError("mcp_authority_resolver_unavailable")


def _build_default_host(
    *,
    connection_resolver: ConnectionResolver = _connection_resolver_unavailable,
    authority_resolver: AuthorityResolver = _authority_resolver_unavailable,
    action_ledger_path: Path | None = None,
) -> McpHost:
    key_ring = load_embedded_key_ring()
    verifier = partial(
        verify_package,
        key_ring=key_ring,
        omega_version=_OMEGA_VERSION,
        platform_tag=current_platform_tag(),
    )
    registry = PackageRegistry(verifier=verifier)
    external_switch = ExternalMcpSwitch(core_paths.MCP_EXTERNAL_SWITCH_PATH)
    supervisor = McpProcessSupervisor(
        registry=registry,
        external_switch=external_switch,
        verifier=verifier,
    )
    authority_gateway = McpAuthorityGateway(
        registry=registry,
        external_switch=external_switch,
        supervisor=supervisor,
        verifier=verifier,
        connection_resolver=connection_resolver,
        authority_resolver=authority_resolver,
        action_ledger_path=action_ledger_path,
    )
    return McpHost(
        registry=registry,
        external_switch=external_switch,
        supervisor=supervisor,
        authority_gateway=authority_gateway,
    )


def start_default_mcp_host(*, host: McpHost | None = None) -> McpHost:
    """Create or inject, start, and publish the one boot-owned MCP host."""
    global _default_generation, _default_host, _default_state
    with _default_condition:
        while _default_state in {"starting", "stopping"}:
            _default_condition.wait()
        if _default_host is not None:
            if host is not None and host is not _default_host:
                raise RuntimeError("default_mcp_host_already_started")
            return _default_host
        _default_state = "starting"
    try:
        candidate = host or _build_default_host()
        candidate.start()
    except BaseException:
        with _default_condition:
            _default_state = "empty"
            _default_condition.notify_all()
        raise
    with _default_condition:
        _default_host = candidate
        _default_generation += 1
        _default_state = "running"
        _default_condition.notify_all()
        return candidate


def get_default_mcp_host() -> McpHost:
    with _default_condition:
        if _default_host is None:
            raise RuntimeError("default_mcp_host_not_started")
        return _default_host


def mcp_status() -> dict[str, Any]:
    return get_default_mcp_host().status()


async def set_external_mcp_enabled(
    enabled: bool,
    expected_revision: str,
) -> dict[str, Any]:
    return await get_default_mcp_host().set_external_enabled(
        enabled,
        expected_revision,
    )


async def set_mcp_package_enabled(
    server_id: str,
    enabled: bool,
    expected_registry_revision: str,
) -> dict[str, Any]:
    return await get_default_mcp_host().set_package_enabled(
        server_id,
        enabled,
        expected_registry_revision,
    )


def shutdown_default_mcp_host() -> dict[str, int]:
    global _default_host, _default_shutdown_attempt, _default_state
    global _default_stopping_owner
    caller = threading.get_ident()
    with _default_condition:
        while _default_state == "starting":
            _default_condition.wait()
        if _default_state == "stopping":
            stopping_owner = _default_stopping_owner
            if stopping_owner is not None and stopping_owner[0] == caller:
                return {"sessions_closed": 0, "sessions_forced": 0, "errors": 0}
            if stopping_owner is None:
                return {"sessions_closed": 0, "sessions_forced": 0, "errors": 1}
            _default_shutdown_waiters[stopping_owner] = (
                _default_shutdown_waiters.get(stopping_owner, 0) + 1
            )
            try:
                while (
                    _default_state == "stopping"
                    and _default_stopping_owner == stopping_owner
                ):
                    _default_condition.wait()
                return dict(
                    _default_shutdown_results.get(
                        stopping_owner,
                        {
                            "sessions_closed": 0,
                            "sessions_forced": 0,
                            "errors": 1,
                        },
                    )
                )
            finally:
                waiters = _default_shutdown_waiters.get(stopping_owner, 0) - 1
                if waiters > 0:
                    _default_shutdown_waiters[stopping_owner] = waiters
                else:
                    _default_shutdown_waiters.pop(stopping_owner, None)
                    _default_shutdown_results.pop(stopping_owner, None)
        if _default_host is None:
            return {"sessions_closed": 0, "sessions_forced": 0, "errors": 0}
        host = _default_host
        _default_state = "stopping"
        _default_shutdown_attempt += 1
        stopping_owner = (caller, _default_generation, _default_shutdown_attempt)
        _default_stopping_owner = stopping_owner
    shutdown_complete = False
    waiter_outcome = {"sessions_closed": 0, "sessions_forced": 0, "errors": 1}
    try:
        outcome = host.shutdown()
        shutdown_complete = (
            type(outcome) is dict
            and set(outcome) == {"sessions_closed", "sessions_forced", "errors"}
            and all(type(value) is int and value >= 0 for value in outcome.values())
            and outcome["errors"] == 0
        )
        if (
            type(outcome) is dict
            and set(outcome) == {"sessions_closed", "sessions_forced", "errors"}
            and all(type(value) is int and value >= 0 for value in outcome.values())
        ):
            waiter_outcome = dict(outcome)
        return outcome
    finally:
        with _default_condition:
            if _default_shutdown_waiters.get(stopping_owner, 0) > 0:
                _default_shutdown_results[stopping_owner] = waiter_outcome
            if (
                _default_host is host
                and _default_stopping_owner == stopping_owner
                and shutdown_complete
            ):
                _default_host = None
            if _default_stopping_owner == stopping_owner:
                _default_stopping_owner = None
                _default_state = "empty" if _default_host is None else "running"
            _default_condition.notify_all()

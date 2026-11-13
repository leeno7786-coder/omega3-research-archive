"""Boot-owned event-loop supervision for on-demand signed MCP sessions."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

from mcp_platform.artifact_guard import ArtifactGuardError
from mcp_platform.capabilities import project_declared_capabilities
from mcp_platform.client import CurrentMcpClient
from mcp_platform.client_contracts import (
    McpClientDeployment,
    McpClientError,
    McpClientLimits,
    McpConnectionDescriptor,
    McpDiscoveryReceipt,
    McpOperationKind,
    McpOperationRequest,
    McpOperationResult,
    McpOperationStatus,
    McpTransport,
)
from mcp_platform.external_switch import ExternalMcpSwitch, mcp_eligible
from mcp_platform.extensions import (
    MCP_APPS_EXTENSION_IDENTIFIER,
    MCP_APPS_SIGNED_VERSION,
)
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION, NetworkClass
from mcp_platform.package_verifier import (
    PackageVerificationError,
    VerifiedPackage,
)
from mcp_platform.registry import InstalledPackageRecord, PackageRegistry
from mcp_platform.redaction import RedactingTextSink
from mcp_platform.transports.stdio import (
    VerifiedStdioLaunchGuard,
    build_verified_stdio_transport,
)
from plugin_platform.contracts import AppRequirement, PluginAuthentication

SESSION_START_TIMEOUT_S = 10.0
SESSION_SHUTDOWN_TIMEOUT_S = 2.0
EXTERNAL_DRAIN_TIMEOUT_S = 2.0
CRASH_BACKOFF_SECONDS = (1.0, 2.0, 4.0)
PERMANENT_BLOCK_CRASH_COUNT = 3


class McpSupervisorError(RuntimeError):
    """A stable, content-free supervisor lifecycle failure."""


class McpServerBlocked(McpSupervisorError):
    """A requested package is not currently eligible to launch."""


class _BaseExceptionCarrier(Exception):
    def __init__(self, primary: BaseException) -> None:
        super().__init__("supervisor_baseexception_carrier")
        self.primary = primary


@dataclass(frozen=True)
class SupervisedSessionSnapshot:
    """Content-free ready state published for one live child session."""

    server_id: str
    state: str
    protocol_version: str
    tool_catalog_sha256: str
    resource_catalog_sha256: str


class ManagedSession(Protocol):
    async def start(self) -> McpDiscoveryReceipt: ...

    async def call_tool(self, request: McpOperationRequest) -> McpOperationResult: ...

    async def read_resource(self, request: McpOperationRequest) -> McpOperationResult: ...

    async def close(self) -> None: ...


SessionFactory = Callable[
    [McpConnectionDescriptor, VerifiedStdioLaunchGuard, RedactingTextSink],
    ManagedSession,
]
Verifier = Callable[[Path], VerifiedPackage]
Clock = Callable[[], float]
ResultT = TypeVar("ResultT")


@dataclass
class _SessionEntry:
    record: InstalledPackageRecord
    package: VerifiedPackage
    descriptor: McpConnectionDescriptor
    session: ManagedSession
    receipt: McpDiscoveryReceipt
    snapshot: SupervisedSessionSnapshot


def _default_session_factory(
    descriptor: McpConnectionDescriptor,
    launch_guard: VerifiedStdioLaunchGuard,
    errlog: RedactingTextSink,
) -> CurrentMcpClient:
    return CurrentMcpClient(
        descriptor,
        build_verified_stdio_transport(descriptor, launch_guard, errlog),
    )


class McpProcessSupervisor:
    """Own exactly one asyncio loop thread and on-demand MCP child contexts."""

    def __init__(
        self,
        *,
        registry: PackageRegistry,
        external_switch: ExternalMcpSwitch,
        verifier: Verifier,
        session_factory: SessionFactory | None = None,
        monotonic: Clock = time.monotonic,
    ) -> None:
        self._registry = registry
        self._external_switch = external_switch
        self._verifier = verifier
        self._session_factory = session_factory or _default_session_factory
        self._monotonic = monotonic
        self._state_lock = threading.Lock()
        self._shutdown_attempt_lock = threading.Lock()
        self._shutdown_helper: threading.Thread | None = None
        self._deferred_shutdown_result: dict[str, int] | None = None
        self._loop_ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._accepting = False
        self._shutdown = False
        self._sessions: dict[str, _SessionEntry] = {}
        self._launches: dict[str, asyncio.Task[SupervisedSessionSnapshot]] = {}
        self._next_eligible: dict[str, float] = {}
        self._request_sequence = 0

    def start(self) -> McpProcessSupervisor:
        """Start the one non-daemon transport loop thread."""
        with self._state_lock:
            if self._shutdown:
                raise McpSupervisorError("supervisor_shutdown")
            if self._thread is None or not self._thread.is_alive():
                self._loop_ready.clear()
                self._thread = threading.Thread(
                    target=self._run_transport_loop,
                    name="omega-mcp-transport",
                    daemon=False,
                )
                self._thread.start()
        if not self._loop_ready.wait(timeout=SESSION_START_TIMEOUT_S):
            with self._state_lock:
                self._accepting = False
                self._shutdown = True
                loop = self._loop
                thread = self._thread
            if not self._stop_transport_thread(thread=thread, loop=loop):
                raise McpSupervisorError("supervisor_stop_timeout")
            raise McpSupervisorError("supervisor_start_timeout")
        with self._state_lock:
            if self._shutdown:
                raise McpSupervisorError("supervisor_shutdown")
            self._accepting = True
        return self

    def _run_transport_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._state_lock:
            self._loop = loop
        try:
            with self._state_lock:
                shutdown_before_run = self._shutdown
            if shutdown_before_run:
                self._loop_ready.set()
                return
            loop.call_soon(self._loop_ready.set)
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
            with self._state_lock:
                self._loop = None

    def thread_alive(self) -> bool:
        with self._state_lock:
            return self._thread is not None and self._thread.is_alive()

    def _stop_transport_thread(
        self,
        *,
        thread: threading.Thread | None,
        loop: asyncio.AbstractEventLoop | None,
    ) -> bool:
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        if thread is not None:
            thread.join(timeout=SESSION_SHUTDOWN_TIMEOUT_S)
            return not thread.is_alive()
        return True

    def _submit(
        self,
        operation: Callable[[], Awaitable[ResultT]],
        *,
        during_shutdown: bool = False,
    ) -> Future[ResultT]:
        with self._state_lock:
            if not during_shutdown and not self._accepting:
                reason = "supervisor_shutdown" if self._shutdown else "supervisor_not_started"
                raise McpSupervisorError(reason)
            loop = self._loop
            if loop is None or not loop.is_running():
                raise McpSupervisorError("supervisor_not_started")
            coroutine = operation()
            return asyncio.run_coroutine_threadsafe(coroutine, loop)

    async def _await_submission(
        self,
        operation: Callable[[], Awaitable[ResultT]],
    ) -> ResultT:
        future = self._submit(operation)
        try:
            return await asyncio.shield(asyncio.wrap_future(future))
        except _BaseExceptionCarrier as carrier:
            raise carrier.primary

    def _sync_submission(
        self,
        operation: Callable[[], Awaitable[ResultT]],
        *,
        timeout_s: float | None = None,
    ) -> ResultT:
        completed = threading.Event()
        start_gate = threading.Lock()
        started = False
        abandoned = False

        def joined_operation() -> Awaitable[ResultT]:
            async def run() -> ResultT:
                nonlocal started
                with start_gate:
                    if abandoned:
                        completed.set()
                        raise asyncio.CancelledError
                    started = True
                try:
                    awaitable = operation()
                    return await awaitable
                finally:
                    # ``Future.cancel`` completes the concurrent future before
                    # the loop task has necessarily run its cancellation
                    # cleanup.  Synchronous callers may own process-wide
                    # leases that cannot be released while that cleanup still
                    # mutates sessions or authority state.
                    completed.set()

            return run()

        future = self._submit(joined_operation)
        try:
            return future.result(timeout=timeout_s)
        except FutureTimeoutError:
            if future.done():
                return future.result()
            with start_gate:
                if not started:
                    abandoned = True
                    wait_for_cleanup = False
                else:
                    wait_for_cleanup = True
            future.cancel()
            if wait_for_cleanup:
                completed.wait()
            raise McpSupervisorError("supervisor_operation_timeout") from None
        except _BaseExceptionCarrier as carrier:
            raise carrier.primary

    async def ensure_session(self, server_id: str) -> SupervisedSessionSnapshot:
        return await self._await_submission(lambda: self._ensure_session(server_id))

    def ensure_session_sync(self, server_id: str) -> SupervisedSessionSnapshot:
        return self._sync_submission(
            lambda: self._ensure_session(server_id),
            timeout_s=SESSION_START_TIMEOUT_S + 1.0,
        )

    async def reconcile_package(
        self,
        server_id: str,
    ) -> SupervisedSessionSnapshot:
        """Reverify the exact installed record before reusing or launching it."""
        return await self._await_submission(lambda: self._reconcile_package(server_id))

    def reconcile_package_sync(
        self,
        server_id: str,
    ) -> SupervisedSessionSnapshot:
        return self._sync_submission(
            lambda: self._reconcile_package(server_id),
            timeout_s=SESSION_START_TIMEOUT_S + SESSION_SHUTDOWN_TIMEOUT_S + 1.0,
        )

    async def _reconcile_package(
        self,
        server_id: str,
    ) -> SupervisedSessionSnapshot:
        record = await self._require_eligible(server_id)
        try:
            self._verify_installed_record(record)
        except Exception:
            try:
                await self._stop_server(server_id)
            except McpSupervisorError:
                pass
            raise
        current = self._sessions.get(server_id)
        if current is not None:
            if _same_launch_identity(current.record, record):
                return current.snapshot
            await self._stop_server(server_id)
        return await self._ensure_session(server_id)

    async def _ensure_session(self, server_id: str) -> SupervisedSessionSnapshot:
        await self._require_eligible(server_id)
        current = self._sessions.get(server_id)
        if current is not None:
            return current.snapshot
        launch = self._launches.get(server_id)
        if launch is None:
            launch = asyncio.create_task(
                self._launch_session(server_id),
                name=f"mcp-launch:{server_id}",
            )
            self._launches[server_id] = launch
            launch.add_done_callback(
                lambda completed, identity=server_id: self._launch_finished(identity, completed)
            )
        snapshot = await asyncio.shield(launch)
        current_record = await self._require_eligible(server_id)
        entry = self._sessions.get(server_id)
        if entry is None or not _same_launch_identity(entry.record, current_record):
            if entry is not None:
                await self._stop_server(server_id)
            raise McpServerBlocked("package_identity_changed")
        return snapshot

    async def _require_eligible(self, server_id: str) -> InstalledPackageRecord:
        record = self._registry.get(server_id)
        blocker = self._eligibility_blocker(record)
        if blocker is not None:
            entry = self._sessions.pop(server_id, None)
            if entry is not None:
                try:
                    await asyncio.wait_for(
                        entry.session.close(), timeout=SESSION_SHUTDOWN_TIMEOUT_S
                    )
                except BaseException as cleanup_error:
                    if not isinstance(cleanup_error, Exception):
                        raise _BaseExceptionCarrier(cleanup_error) from None
            raise McpServerBlocked(blocker)
        return record

    def _eligibility_blocker(
        self,
        record: InstalledPackageRecord,
    ) -> str | None:
        if not record.enabled:
            return "package_disabled"
        if record.blocker is not None:
            return record.blocker
        network_class = NetworkClass(record.network_class)
        if not mcp_eligible(network_class, self._external_switch.load()):
            return "external_mcp_off"
        return None

    def _launch_finished(
        self,
        server_id: str,
        completed: asyncio.Task[SupervisedSessionSnapshot],
    ) -> None:
        if self._launches.get(server_id) is completed:
            self._launches.pop(server_id, None)

    async def _launch_session(self, server_id: str) -> SupervisedSessionSnapshot:
        record = self._registry.get(server_id)
        blocker = self._eligibility_blocker(record)
        if blocker is not None:
            raise McpServerBlocked(blocker)
        if record.crash_count == 0:
            self._next_eligible.pop(server_id, None)
        eligible_at = self._next_eligible.get(server_id)
        if eligible_at is not None and self._monotonic() < eligible_at:
            raise McpServerBlocked("crash_backoff")

        package_root = self._registry.package_path(record)
        package = self._verify_installed_record(record)

        descriptor = _local_descriptor(package)
        package = self._reverify_launch_boundary(record)
        launch_guard: VerifiedStdioLaunchGuard | None = None
        session: ManagedSession | None = None
        try:
            launch_guard = VerifiedStdioLaunchGuard.acquire(
                package,
                package_root=package_root,
                environment={
                    "OMEGA_MCP_SERVER_ID": server_id,
                    "OMEGA_MCP_PACKAGE_ROOT": str(package_root.resolve()),
                },
            )
            session = self._session_factory(
                descriptor,
                launch_guard,
                RedactingTextSink(max_bytes=descriptor.limits.max_stderr_bytes),
            )
            receipt = await asyncio.wait_for(session.start(), timeout=SESSION_START_TIMEOUT_S)
            launch_guard.close()
        except BaseException as exc:
            try:
                if session is not None:
                    await asyncio.wait_for(session.close(), timeout=SESSION_SHUTDOWN_TIMEOUT_S)
            except BaseException:
                pass
            if launch_guard is not None:
                try:
                    launch_guard.close()
                except BaseException:
                    pass
            if not isinstance(exc, Exception):
                raise _BaseExceptionCarrier(exc) from None
            reason = _stable_session_failure(exc)
            self._registry.block(server_id, reason)
            raise McpServerBlocked(reason) from None
        snapshot = SupervisedSessionSnapshot(
            server_id=server_id,
            state="ready",
            protocol_version=receipt.protocol_version,
            tool_catalog_sha256=record.tool_catalog_sha256,
            resource_catalog_sha256=record.resource_catalog_sha256,
        )
        self._sessions[server_id] = _SessionEntry(
            record=record,
            package=package,
            descriptor=descriptor,
            session=session,
            receipt=receipt,
            snapshot=snapshot,
        )
        return snapshot

    def _verify_installed_record(
        self,
        record: InstalledPackageRecord,
    ) -> VerifiedPackage:
        package_root = self._registry.package_path(record)
        try:
            package = self._verifier(package_root)
        except PackageVerificationError as exc:
            reason = str(exc)
            self._registry.block(record.server_id, reason)
            raise McpServerBlocked(reason) from None
        except Exception:
            self._registry.block(record.server_id, "package_verification_failed")
            raise McpServerBlocked("package_verification_failed") from None
        if not _verified_package_matches_record(package, record):
            self._registry.block(record.server_id, "package_identity_mismatch")
            raise McpServerBlocked("package_identity_mismatch")
        return package

    def _reverify_launch_boundary(
        self,
        record: InstalledPackageRecord,
    ) -> VerifiedPackage:
        try:
            package = self._registry.reverify_installed(record.server_id)
        except Exception as exc:
            observed = str(exc)
            reason = (
                observed
                if observed
                in {
                    "package_identity_collision",
                    "package_not_installed",
                    "package_verifier_required",
                }
                else "package_identity_collision"
            )
            self._registry.block(record.server_id, reason)
            raise McpServerBlocked(reason) from None
        if not _verified_package_matches_record(package, record):
            self._registry.block(record.server_id, "package_identity_mismatch")
            raise McpServerBlocked("package_identity_mismatch")
        return package

    async def call_tool(
        self,
        server_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._await_submission(lambda: self._call_tool(server_id, name, arguments))

    async def _call_tool(
        self,
        server_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        await self._ensure_session(server_id)
        entry = self._sessions[server_id]
        request = self._operation_request(
            entry,
            operation=McpOperationKind.TOOL_CALL,
            capability_id=f"tool:{name}",
            arguments=arguments,
        )
        result = await entry.session.call_tool(request)
        data = _completed_data(result, request)
        if data.get("is_error") is not False:
            raise McpSupervisorError("tool_call_failed")
        structured = data.get("structured_content")
        if not isinstance(structured, Mapping):
            raise McpSupervisorError("structured_content_required")
        return _plain_mapping(structured)

    def call_tool_sync(
        self,
        server_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        return self._sync_submission(lambda: self._call_tool(server_id, name, arguments))

    def submit_plugin_operation_sync(
        self,
        operation: Callable[[], Awaitable[ResultT]],
        *,
        timeout_s: float | None = None,
    ) -> ResultT:
        """Run one signed plugin operation on the one transport loop.

        Plugin sessions are owned by the plugin connection service, but every
        coroutine that touches those sessions rides this loop so the platform
        keeps exactly one forever transport loop.
        """
        return self._sync_submission(operation, timeout_s=timeout_s)

    async def read_resource(self, server_id: str, uri: str) -> tuple[str, ...]:
        return await self._await_submission(lambda: self._read_resource(server_id, uri))

    async def _read_resource(self, server_id: str, uri: str) -> tuple[str, ...]:
        await self._ensure_session(server_id)
        entry = self._sessions[server_id]
        capability_id, arguments = _resource_authority(entry.package, uri)
        request = self._operation_request(
            entry,
            operation=McpOperationKind.RESOURCE_READ,
            capability_id=capability_id,
            arguments=arguments,
        )
        data = _completed_data(await entry.session.read_resource(request), request)
        contents = data.get("contents")
        if type(contents) is not tuple:
            raise McpSupervisorError("resource_read_failed")
        texts: list[str] = []
        for content in contents:
            if not isinstance(content, Mapping) or type(content.get("text")) is not str:
                raise McpSupervisorError("resource_read_failed")
            texts.append(content["text"])
        return tuple(texts)

    def read_resource_sync(self, server_id: str, uri: str) -> tuple[str, ...]:
        return self._sync_submission(lambda: self._read_resource(server_id, uri))

    def _operation_request(
        self,
        entry: _SessionEntry,
        *,
        operation: McpOperationKind,
        capability_id: str,
        arguments: Mapping[str, Any],
    ) -> McpOperationRequest:
        self._request_sequence += 1
        identity = f"supervisor.{self._request_sequence}"
        return McpOperationRequest(
            request_id=identity,
            app_id=entry.descriptor.app_id,
            operation=operation,
            capability_id=capability_id,
            arguments=arguments,
            capability_revision=entry.receipt.capability_revision,
            deadline_monotonic=(
                asyncio.get_running_loop().time()
                + entry.descriptor.limits.max_operation_duration_ms / 1_000
            ),
            cancellation_id=f"cancel.{identity}",
            task_id=None,
            input_request_id=None,
            request_state=None,
        )

    async def stop_server(self, server_id: str) -> bool:
        return await self._await_submission(lambda: self._stop_server(server_id))

    def stop_server_sync(self, server_id: str) -> bool:
        return self._sync_submission(lambda: self._stop_server(server_id))

    async def _stop_server(
        self,
        server_id: str,
        *,
        timeout_s: float = SESSION_SHUTDOWN_TIMEOUT_S,
    ) -> bool:
        entry = self._sessions.pop(server_id, None)
        if entry is None:
            return False
        try:
            await asyncio.wait_for(entry.session.close(), timeout=timeout_s)
        except TimeoutError:
            raise McpSupervisorError("session_close_timeout") from None
        except Exception:
            raise McpSupervisorError("session_close_failed") from None
        return True

    async def stop_external(
        self,
        drain_timeout_s: float = EXTERNAL_DRAIN_TIMEOUT_S,
    ) -> dict[str, int]:
        return await self._await_submission(lambda: self._stop_external(drain_timeout_s))

    def stop_external_sync(
        self,
        drain_timeout_s: float = EXTERNAL_DRAIN_TIMEOUT_S,
    ) -> dict[str, int]:
        return self._sync_submission(lambda: self._stop_external(drain_timeout_s))

    async def _stop_external(self, drain_timeout_s: float) -> dict[str, int]:
        if drain_timeout_s < 0:
            raise ValueError("invalid_external_drain_timeout")
        deadline = asyncio.get_running_loop().time() + drain_timeout_s
        external_launches = [
            launch
            for server_id, launch in list(self._launches.items())
            if self._is_external(server_id)
        ]
        for launch in external_launches:
            launch.cancel()
        if external_launches:
            await asyncio.gather(*external_launches, return_exceptions=True)
        server_ids = [
            server_id
            for server_id, entry in self._sessions.items()
            if entry.record.network_class == NetworkClass.EXTERNAL.value
        ]
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        return await self._close_sessions_concurrently(
            server_ids,
            timeout_s=remaining,
        )

    def _is_external(self, server_id: str) -> bool:
        return self._registry.get(server_id).network_class == NetworkClass.EXTERNAL.value

    async def record_unexpected_exit(self, server_id: str, *, exit_code: int) -> None:
        await self._await_submission(
            lambda: self._record_unexpected_exit(server_id, exit_code=exit_code)
        )

    def record_unexpected_exit_sync(self, server_id: str, *, exit_code: int) -> None:
        self._sync_submission(lambda: self._record_unexpected_exit(server_id, exit_code=exit_code))

    async def _record_unexpected_exit(self, server_id: str, *, exit_code: int) -> None:
        if type(exit_code) is not int:
            raise ValueError("invalid_exit_code")
        entry = self._sessions.pop(server_id, None)
        if entry is None:
            raise McpSupervisorError("session_not_running")
        record = self._registry.record_crash(
            server_id,
            permanent_block_at=PERMANENT_BLOCK_CRASH_COUNT,
            permanent_blocker="repeated_crash",
        )
        backoff_index = min(record.crash_count, len(CRASH_BACKOFF_SECONDS)) - 1
        self._next_eligible[server_id] = self._monotonic() + CRASH_BACKOFF_SECONDS[backoff_index]
        try:
            await asyncio.wait_for(entry.session.close(), timeout=SESSION_SHUTDOWN_TIMEOUT_S)
        except BaseException as cleanup_error:
            if not isinstance(cleanup_error, Exception):
                raise _BaseExceptionCarrier(cleanup_error) from None

    def session_statuses_sync(self) -> dict[str, SupervisedSessionSnapshot]:
        return self._sync_submission(self._session_statuses)

    async def _session_statuses(self) -> dict[str, SupervisedSessionSnapshot]:
        return {server_id: entry.snapshot for server_id, entry in sorted(self._sessions.items())}

    async def _close_sessions(
        self,
        server_ids: list[str],
        *,
        timeout_s: float,
    ) -> dict[str, int]:
        result = {"sessions_closed": 0, "sessions_forced": 0, "errors": 0}
        for server_id in server_ids:
            entry = self._sessions.pop(server_id, None)
            if entry is None:
                continue
            try:
                await asyncio.wait_for(entry.session.close(), timeout=timeout_s)
            except TimeoutError:
                result["sessions_forced"] += 1
            except BaseException:
                result["errors"] += 1
            else:
                result["sessions_closed"] += 1
        return result

    async def _close_sessions_concurrently(
        self,
        server_ids: list[str],
        *,
        timeout_s: float,
    ) -> dict[str, int]:
        """Close sessions under one deadline, then await SDK cancellation cleanup."""
        result = {"sessions_closed": 0, "sessions_forced": 0, "errors": 0}
        entries = [
            entry
            for server_id in server_ids
            if (entry := self._sessions.pop(server_id, None)) is not None
        ]
        if not entries:
            return result
        tasks = [asyncio.create_task(entry.session.close()) for entry in entries]
        done, pending = await asyncio.wait(tasks, timeout=timeout_s)
        for task in done:
            try:
                task.result()
            except BaseException:
                result["errors"] += 1
            else:
                result["sessions_closed"] += 1
        for task in pending:
            task.cancel()
        if pending:
            # SdkMcpSession cancellation unwinds the official stdio context. AnyIO
            # shields pipe cleanup, kills the transport, and waits for termination.
            # Await that cleanup before reporting a session as forced.
            await asyncio.gather(*pending, return_exceptions=True)
            result["sessions_forced"] += len(pending)
        return result

    def shutdown(self) -> dict[str, int]:
        """Reject submissions, close every context, stop the loop, and join it."""
        with self._state_lock:
            transport_thread = self._thread
        if transport_thread is threading.current_thread():
            return self._defer_shutdown_from_transport_loop()

        with self._state_lock:
            helper = self._shutdown_helper
        if helper is not None and helper.is_alive():
            return self._join_deferred_shutdown(helper)

        with self._shutdown_attempt_lock:
            with self._state_lock:
                helper = self._shutdown_helper
            if helper is not None and helper.is_alive():
                wait_for_helper = True
                result = None
            else:
                wait_for_helper = False
                result = self._deferred_shutdown_result
                self._deferred_shutdown_result = None
                if result is None:
                    result = self._shutdown_attempt()
        if wait_for_helper:
            return self._join_deferred_shutdown(helper)
        assert result is not None
        return result

    def _defer_shutdown_from_transport_loop(self) -> dict[str, int]:
        deferred = {"sessions_closed": 0, "sessions_forced": 0, "errors": 1}
        if not self._shutdown_attempt_lock.acquire(blocking=False):
            return deferred
        try:
            with self._state_lock:
                if self._shutdown_helper is not None:
                    return deferred
                self._accepting = False
                self._shutdown = True
                helper = threading.Thread(
                    target=self._complete_deferred_shutdown,
                    name="omega-mcp-shutdown",
                    daemon=False,
                )
                self._shutdown_helper = helper
            try:
                helper.start()
            except BaseException:
                with self._state_lock:
                    if self._shutdown_helper is helper:
                        self._shutdown_helper = None
                raise McpSupervisorError("supervisor_shutdown_handoff_failed") from None
        finally:
            self._shutdown_attempt_lock.release()
        return deferred

    def _complete_deferred_shutdown(self) -> None:
        with self._shutdown_attempt_lock:
            try:
                result = self._shutdown_attempt()
            except BaseException:
                result = {
                    "sessions_closed": 0,
                    "sessions_forced": 0,
                    "errors": 1,
                }
            with self._state_lock:
                self._deferred_shutdown_result = result

    def _join_deferred_shutdown(
        self,
        helper: threading.Thread,
    ) -> dict[str, int]:
        maximum_wait = (len(self._registry.list_records()) + 3) * SESSION_SHUTDOWN_TIMEOUT_S
        helper.join(timeout=maximum_wait)
        if helper.is_alive():
            return {"sessions_closed": 0, "sessions_forced": 0, "errors": 1}
        with self._shutdown_attempt_lock:
            with self._state_lock:
                result = self._deferred_shutdown_result
                self._deferred_shutdown_result = None
            if result is not None:
                return result
            return self._shutdown_attempt()

    def _shutdown_attempt(self) -> dict[str, int]:
        with self._state_lock:
            thread = self._thread
            if self._shutdown and (thread is None or not thread.is_alive()):
                return {"sessions_closed": 0, "sessions_forced": 0, "errors": 0}
            self._accepting = False
            self._shutdown = True
        if thread is not None and not self._loop_ready.is_set():
            self._loop_ready.wait(timeout=SESSION_START_TIMEOUT_S)
        with self._state_lock:
            loop = self._loop
        result = {"sessions_closed": 0, "sessions_forced": 0, "errors": 0}
        if loop is not None and loop.is_running():
            future = self._submit(
                self._shutdown_sessions,
                during_shutdown=True,
            )
            maximum_wait = (
                len(self._registry.list_records()) * SESSION_SHUTDOWN_TIMEOUT_S
                + SESSION_SHUTDOWN_TIMEOUT_S
            )
            try:
                result = future.result(timeout=maximum_wait)
            except Exception:
                result["errors"] += 1
        if not self._stop_transport_thread(thread=thread, loop=loop):
            result["errors"] += 1
        return result

    async def _shutdown_sessions(self) -> dict[str, int]:
        launches = list(self._launches.values())
        for launch in launches:
            launch.cancel()
        if launches:
            await asyncio.gather(*launches, return_exceptions=True)
        return await self._close_sessions(
            list(self._sessions), timeout_s=SESSION_SHUTDOWN_TIMEOUT_S
        )


def _local_descriptor(package: VerifiedPackage) -> McpConnectionDescriptor:
    capabilities = [
        {
            "kind": "tool",
            "name": tool.name,
            "schema_sha256": tool.input_schema_sha256,
            "version": None,
        }
        for tool in package.tool_catalog.tools
    ]
    capabilities.extend(
        {
            "kind": "resource",
            "name": resource.name,
            "schema_sha256": None,
            "version": None,
        }
        for resource in package.resource_catalog.resources
    )
    capabilities.extend(
        {
            "kind": "resource",
            "name": template.name,
            "schema_sha256": None,
            "version": None,
        }
        for template in package.resource_catalog.resource_templates
    )
    apps_metadata = package.manifest.mcp_apps_metadata
    signed_authority: dict[str, Any] = {
        "tools": {
            tool.name: {
                "title": tool.title,
                "input_schema": _plain_json(tool.input_schema),
                "input_schema_sha256": tool.input_schema_sha256,
                "output_schema": _plain_json(tool.output_schema),
                "output_schema_sha256": tool.output_schema_sha256,
                "risk_class": tool.risk_class.value,
                "effect": tool.effect,
                "requires_revision": tool.requires_revision,
                "max_result_bytes": tool.max_result_bytes,
                "allowed_mime_types": list(tool.allowed_mime_types),
            }
            for tool in package.tool_catalog.tools
        },
        "resources": {
            resource.name: {
                "uri": resource.uri,
                "mime_type": resource.mime_type,
                "max_bytes": resource.max_bytes,
            }
            for resource in package.resource_catalog.resources
        },
        "resource_templates": {
            template.name: {
                "uri_template": template.uri_template,
                "mime_type": template.mime_type,
                "max_bytes": template.max_bytes,
            }
            for template in package.resource_catalog.resource_templates
        },
    }
    extension_versions: dict[str, str] = {}
    if apps_metadata is not None and any(apps_metadata[group] for group in apps_metadata):
        plain_apps_metadata = _plain_json(apps_metadata)
        if any(
            set(plain_apps_metadata[group]) - set(signed_authority[group])
            for group in ("tools", "resources", "resource_templates")
        ):
            raise McpSupervisorError("signed_apps_metadata_invalid")
        signed_authority["mcp_apps_metadata"] = plain_apps_metadata
        capabilities.append(
            {
                "kind": "extension",
                "name": MCP_APPS_EXTENSION_IDENTIFIER,
                "schema_sha256": None,
                "version": MCP_APPS_SIGNED_VERSION,
            }
        )
        extension_versions[MCP_APPS_EXTENSION_IDENTIFIER] = MCP_APPS_SIGNED_VERSION
    requirement = AppRequirement.from_dict(
        {
            "app_id": package.manifest.server_id,
            "publisher_id": package.manifest.publisher_id,
            "deployment": "local",
            "provider_id": None,
            "protocol_version": MCP_PROTOCOL_VERSION,
            "transports": ["stdio"],
            "remote_origin": None,
            "authorization_issuer": None,
            "authentication": _local_package_authentication(package).to_dict(),
            "capabilities": capabilities,
        }
    )
    projection = project_declared_capabilities(requirement)
    return McpConnectionDescriptor(
        app_id=requirement.app_id,
        publisher_id=requirement.publisher_id,
        deployment=McpClientDeployment.LOCAL,
        provider_id=None,
        protocol_version=MCP_PROTOCOL_VERSION,
        transport=McpTransport.STDIO,
        verified_launch_sha256=package.manifest.artifact_sha256,
        remote_origin=None,
        authorization_issuer=None,
        authentication=requirement.authentication,
        declared_capabilities={
            f"{capability.kind.value}:{capability.name}": capability.schema_sha256
            for capability in requirement.capabilities
            if capability.kind.value != "extension"
        },
        extension_versions=extension_versions,
        capability_revision=projection.snapshot.capability_revision,
        limits=McpClientLimits(),
        signed_capability_authority=signed_authority,
    )


def _local_package_authentication(package: VerifiedPackage) -> PluginAuthentication:
    if package.manifest.oauth_providers:
        raise ValueError("local MCP package authentication is invalid")
    return PluginAuthentication.from_dict(
        {"kind": "none", "scopes": [], "secret_fields": []}
    )


def _completed_data(
    result: McpOperationResult,
    request: McpOperationRequest,
) -> Mapping[str, Any]:
    if (
        type(result) is not McpOperationResult
        or result.request_id != request.request_id
        or result.app_id != request.app_id
        or result.capability_id != request.capability_id
        or result.capability_revision != request.capability_revision
    ):
        raise McpSupervisorError("operation_identity_mismatch")
    if result.status is McpOperationStatus.FAILED and result.error is not None:
        raise McpSupervisorError(result.error.reason.value)
    if result.status is not McpOperationStatus.COMPLETED:
        raise McpSupervisorError("operation_incomplete")
    return result.data


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_plain_json(item) for item in value]
    return value


def _plain_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _plain_json(item) for key, item in value.items()}


def _resource_authority(
    package: VerifiedPackage,
    uri: str,
) -> tuple[str, dict[str, str]]:
    for resource in package.resource_catalog.resources:
        if resource.uri == uri:
            return f"resource:{resource.name}", {}
    for template in package.resource_catalog.resource_templates:
        if _matches_simple_uri_template(template.uri_template, uri):
            return f"resource:{template.name}", {"uri": uri}
    raise McpSupervisorError("undeclared_resource")


def _matches_simple_uri_template(template: str, uri: str) -> bool:
    pieces: list[str] = []
    position = 0
    for match in re.finditer(r"\{[A-Za-z0-9_]+\}", template):
        pieces.append(re.escape(template[position : match.start()]))
        pieces.append(r"[^/?#]+")
        position = match.end()
    pieces.append(re.escape(template[position:]))
    return re.fullmatch("".join(pieces), uri) is not None


def _verified_package_matches_record(
    package: VerifiedPackage,
    record: InstalledPackageRecord,
) -> bool:
    manifest = package.manifest
    return (
        manifest.server_id == record.server_id
        and manifest.server_version == record.server_version
        and manifest.platform == record.platform
        and manifest.publisher_id == record.publisher_id
        and manifest.signing_key_id == record.signing_key_id
        and package.manifest_sha256 == record.manifest_digest
        and manifest.artifact_sha256 == record.artifact_sha256
        and manifest.tool_catalog_sha256 == record.tool_catalog_sha256
        and manifest.resource_catalog_sha256 == record.resource_catalog_sha256
        and manifest.network_class.value == record.network_class
    )


def _same_launch_identity(
    left: InstalledPackageRecord,
    right: InstalledPackageRecord,
) -> bool:
    return (
        left.server_id,
        left.server_version,
        left.platform,
        left.publisher_id,
        left.signing_key_id,
        left.manifest_digest,
        left.artifact_sha256,
        left.tool_catalog_sha256,
        left.resource_catalog_sha256,
        left.network_class,
    ) == (
        right.server_id,
        right.server_version,
        right.platform,
        right.publisher_id,
        right.signing_key_id,
        right.manifest_digest,
        right.artifact_sha256,
        right.tool_catalog_sha256,
        right.resource_catalog_sha256,
        right.network_class,
    )


def _stable_session_failure(error: BaseException) -> str:
    if isinstance(error, asyncio.TimeoutError):
        return "session_start_timeout"
    if isinstance(error, (ArtifactGuardError, McpClientError)):
        message = str(error)
        return message if message else "session_start_failed"
    return "session_start_failed"

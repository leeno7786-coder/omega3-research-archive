"""Verified official-SDK stdio transport for current local MCP clients."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import stat
import sys
import threading
import weakref
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, AsyncGenerator, Callable, TextIO

import anyio
import anyio.lowlevel
from mcp import Client, StdioServerParameters, types
from anyio.abc import AsyncResource, Process
from mcp.os.posix.utilities import terminate_posix_process_tree
from mcp.os.win32.utilities import (
    ServerProcess,
    close_process_job,
    create_windows_process,
    terminate_windows_process_tree,
)
from mcp.shared.message import SessionMessage

from mcp_platform.artifact_guard import (
    ArtifactGuardError,
    ArtifactLaunchGuard,
    _capture_artifact_identity_chain,
    _open_posix_identity_chain,
)
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.capabilities import project_declared_capabilities
from mcp_platform.client import (
    SdkClientFactory,
    _issue_sdk_client_factory,
    _SDK_CLIENT_FACTORY_ISSUER,
)
from mcp_platform.client_contracts import (
    McpClientDeployment,
    McpClientError,
    McpClientErrorReason,
    McpConnectionDescriptor,
    McpTransport,
)
from mcp_platform.extensions import (
    MCP_APPS_EXTENSION_IDENTIFIER,
    MCP_APPS_SIGNED_VERSION,
)
from core.paths import portable_python_path
from mcp_platform.package_contracts import MCP_PROTOCOL_VERSION
from mcp_platform.package_verifier import VerifiedPackage, _is_verifier_issued_package
from mcp_platform.redaction import RedactingStderrCapture, RedactingTextSink
from plugin_platform.contracts import AppRequirement, PluginAuthentication

_ATTACHED_WORKER_PROCESS_MODEL = "attached-worker"
_POSIX_ATTACHED_EXEC = (
    "import os,sys\n"
    "gate=int(sys.argv[1])\n"
    "if sys.platform == 'darwin':\n"
    " import resource\n"
    " if os.geteuid() == 0: raise SystemExit(126)\n"
    " resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))\n"
    " if resource.getrlimit(resource.RLIMIT_NPROC) != (0, 0): raise SystemExit(126)\n"
    "try: token=os.read(gate, 2)\n"
    "finally: os.close(gate)\n"
    "if token != b'\\x01': raise SystemExit(126)\n"
    "os.execve(sys.argv[2], sys.argv[2:], os.environ)\n"
)


@dataclass(slots=True)
class _AttachedWorkerProcessOwner:
    """Retain and reap one exact Linux child created by the stdio owner."""

    pid: int
    _wait_status: int | None = None

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid <= 0:
            raise OSError("plugin_worker_process_model_unavailable")

    def poll_wait_status(self) -> int | None:
        if self._wait_status is not None:
            return self._wait_status
        observed_pid, status = os.waitpid(self.pid, os.WNOHANG)
        if observed_pid == 0:
            return None
        if observed_pid != self.pid:
            raise OSError("plugin_worker_process_model_unavailable")
        self._wait_status = status
        return status


def _fork_attached_worker() -> _AttachedWorkerProcessOwner | None:
    """Create one Linux child and retain its exact reap ownership in the parent."""

    if os.name == "nt" or sys.platform != "linux":
        raise OSError("plugin_worker_process_model_unavailable")
    child_pid = os.fork()
    if type(child_pid) is not int or child_pid < 0:
        raise OSError("plugin_worker_process_model_unavailable")
    return None if child_pid == 0 else _AttachedWorkerProcessOwner(child_pid)


def _identity_mismatch() -> None:
    raise McpClientError(McpClientErrorReason.IDENTITY_MISMATCH)


def _package_requirement(package: VerifiedPackage) -> AppRequirement:
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
    if apps_metadata is not None and any(apps_metadata[group] for group in apps_metadata):
        capabilities.append(
            {
                "kind": "extension",
                "name": MCP_APPS_EXTENSION_IDENTIFIER,
                "schema_sha256": None,
                "version": MCP_APPS_SIGNED_VERSION,
            }
        )
    return AppRequirement.from_dict(
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


def _local_package_authentication(package: VerifiedPackage) -> PluginAuthentication:
    if package.manifest.oauth_providers:
        raise ValueError("local MCP package authentication is invalid")
    return PluginAuthentication.from_dict(
        {"kind": "none", "scopes": [], "secret_fields": []}
    )


def _descriptor_matches_package(
    descriptor: McpConnectionDescriptor,
    package: VerifiedPackage,
) -> bool:
    try:
        projection = project_declared_capabilities(_package_requirement(package))
    except (TypeError, ValueError):
        return False
    expected_capabilities = {
        f"tool:{capability.name}": capability.schema_sha256
        for capability in projection.snapshot.tools
    }
    expected_capabilities.update(
        {
            f"resource:{capability.name}": capability.schema_sha256
            for capability in projection.snapshot.resources
        }
    )
    signed_authority = {
        "prompts": {},
        "tools": {
            tool.name: {
                "title": tool.title,
                "input_schema": tool.input_schema,
                "input_schema_sha256": tool.input_schema_sha256,
                "output_schema": tool.output_schema,
                "output_schema_sha256": tool.output_schema_sha256,
                "risk_class": tool.risk_class.value,
                "effect": tool.effect,
                "requires_revision": tool.requires_revision,
                "max_result_bytes": tool.max_result_bytes,
                "allowed_mime_types": tool.allowed_mime_types,
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
    expected_extension_versions: dict[str, str] = {}
    apps_metadata = package.manifest.mcp_apps_metadata
    if apps_metadata is not None and any(apps_metadata[group] for group in apps_metadata):
        signed_authority["mcp_apps_metadata"] = apps_metadata
        expected_extension_versions[MCP_APPS_EXTENSION_IDENTIFIER] = MCP_APPS_SIGNED_VERSION
    return (
        descriptor.app_id == package.manifest.server_id
        and descriptor.publisher_id == package.manifest.publisher_id
        and descriptor.deployment is McpClientDeployment.LOCAL
        and descriptor.provider_id is None
        and descriptor.protocol_version == MCP_PROTOCOL_VERSION
        and descriptor.transport is McpTransport.STDIO
        and descriptor.verified_launch_sha256 == package.manifest.artifact_sha256
        and descriptor.remote_origin is None
        and descriptor.authorization_issuer is None
        and dict(descriptor.declared_capabilities) == expected_capabilities
        and dict(descriptor.extension_versions) == expected_extension_versions
        and descriptor.capability_revision == projection.snapshot.capability_revision
        and descriptor.signed_capability_authority == signed_authority
    )


class _PosixArtifactLaunchGuard:
    """Retain the exact root-to-executable descriptor chain through POSIX spawn."""

    __slots__ = (
        "_component_fds",
        "_component_identities",
        "_expected_sha256",
        "launch_command",
        "launch_cwd",
    )

    def __init__(
        self,
        *,
        component_fds: tuple[int, ...],
        component_identities: tuple[tuple[int, int, int], ...],
        expected_sha256: str,
        descriptor_root: str,
    ) -> None:
        self._component_fds = component_fds
        self._component_identities = component_identities
        self._expected_sha256 = expected_sha256
        self.launch_command = f"{descriptor_root}/{component_fds[-1]}"
        self.launch_cwd = f"{descriptor_root}/{component_fds[0]}"

    @classmethod
    def acquire(
        cls,
        *,
        package_root: Path,
        artifact_path: Path,
        expected_sha256: str,
        expected_identity: tuple[int, int],
        expected_component_identities: tuple[tuple[int, int, int], ...],
    ) -> _PosixArtifactLaunchGuard:
        if sys.platform == "win32":
            _identity_mismatch()
        component_fds: tuple[int, ...] = ()
        issued = False
        try:
            descriptor_root = next(
                path
                for path in ("/proc/self/fd", "/dev/fd")
                if Path(path).is_dir()
            )
            component_fds, component_identities = _open_posix_identity_chain(
                package_root,
                artifact_path,
            )
            artifact_fd = component_fds[-1]
            artifact_status = os.fstat(artifact_fd)
            artifact_identity = (artifact_status.st_dev, artifact_status.st_ino)
            if (
                component_identities != expected_component_identities
                or artifact_identity != expected_identity
                or _descriptor_sha256(artifact_fd) != expected_sha256
            ):
                _identity_mismatch()
            guard = cls(
                component_fds=component_fds,
                component_identities=component_identities,
                expected_sha256=expected_sha256,
                descriptor_root=descriptor_root,
            )
            issued = True
            return guard
        except (
            ArtifactGuardError,
            AttributeError,
            OSError,
            StopIteration,
            TypeError,
            ValueError,
        ):
            _identity_mismatch()
        finally:
            if not issued:
                for file_descriptor in reversed(component_fds):
                    with suppress(OSError):
                        os.close(file_descriptor)

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return (self._component_fds[0], self._component_fds[-1])

    def revalidate(self) -> None:
        if not self._component_fds:
            _identity_mismatch()
        current_identities: list[tuple[int, int, int]] = []
        for index, file_descriptor in enumerate(self._component_fds):
            status = os.fstat(file_descriptor)
            if not (
                stat.S_ISDIR(status.st_mode)
                if index < len(self._component_fds) - 1
                else stat.S_ISREG(status.st_mode)
            ):
                _identity_mismatch()
            current_identities.append((0, status.st_dev, status.st_ino))
        if tuple(current_identities) != self._component_identities or (
            _descriptor_sha256(self._component_fds[-1]) != self._expected_sha256
        ):
            _identity_mismatch()

    def close(self) -> None:
        file_descriptors = self._component_fds
        self._component_fds = ()
        for file_descriptor in reversed(file_descriptors):
            with suppress(OSError):
                os.close(file_descriptor)


def _descriptor_sha256(file_descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(file_descriptor, 0, os.SEEK_SET)
    for chunk in iter(lambda: os.read(file_descriptor, 1024 * 1024), b""):
        digest.update(chunk)
    os.lseek(file_descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


class VerifiedStdioLaunchGuard:
    """Exact verified package launch material plus retained native guard handles."""

    __slots__ = ("_proof", "__weakref__")

    def __init__(self, issuer: object) -> None:
        raise TypeError("verified stdio launch guards are verifier-issued")

    @classmethod
    def acquire(
        cls,
        verified_package: VerifiedPackage,
        *,
        package_root: Path,
        environment: Mapping[str, str],
    ) -> VerifiedStdioLaunchGuard:
        if (
            not _is_verifier_issued_package(verified_package)
            or not isinstance(package_root, Path)
            or type(environment) is not dict
            or any(
                type(key) is not str or type(value) is not str for key, value in environment.items()
            )
            or verified_package.artifact_identity == (0, 0)
        ):
            _identity_mismatch()
        try:
            root = package_root.resolve(strict=True)
            expected_root = verified_package.tool_catalog_path.parent.resolve(strict=True)
        except (OSError, RuntimeError):
            _identity_mismatch()
        if root != expected_root:
            _identity_mismatch()
        expected_environment = {
            "OMEGA_MCP_SERVER_ID": verified_package.manifest.server_id,
            "OMEGA_MCP_PACKAGE_ROOT": str(root),
        }
        if environment != expected_environment:
            _identity_mismatch()
        try:
            expected_component_identities = _capture_artifact_identity_chain(
                root,
                verified_package.artifact_path,
            )
        except (ArtifactGuardError, OSError):
            _identity_mismatch()
        retained = ArtifactLaunchGuard.acquire(
            package_root=root,
            artifact_path=verified_package.artifact_path,
            expected_sha256=verified_package.manifest.artifact_sha256,
            expected_identity=verified_package.artifact_identity,
            expected_component_identities=expected_component_identities,
        )
        return _issue_launch_guard(
            verified_package=verified_package,
            package_root=root,
            environment=expected_environment,
            artifact_guard=retained,
        )

    @classmethod
    def _acquire_worker_contained(
        cls,
        verified_package: VerifiedPackage,
        *,
        package_root: Path,
        environment: Mapping[str, str],
        descriptor: McpConnectionDescriptor,
        expected_component_identities: tuple[tuple[int, int, int], ...],
        provider_pin: Callable[[int], None] | None,
    ) -> VerifiedStdioLaunchGuard:
        """Retain one child-reverified executable for an inherited worker tree."""
        if (
            not _is_verifier_issued_package(verified_package)
            or not isinstance(package_root, Path)
            or type(environment) is not dict
            or type(descriptor) is not McpConnectionDescriptor
            or type(expected_component_identities) is not tuple
            or len(expected_component_identities) < 2
            or any(
                type(identity) is not tuple
                or len(identity) != 3
                or any(type(value) is not int or value < 0 for value in identity)
                or identity[0] not in {0, 1, 2}
                for identity in expected_component_identities
            )
            or any(
                type(key) is not str or type(value) is not str
                for key, value in environment.items()
            )
            or verified_package.artifact_identity == (0, 0)
            or sys.platform != "win32"
            and not callable(provider_pin)
            or sys.platform == "win32"
            and provider_pin is not None
        ):
            _identity_mismatch()
        try:
            root = package_root.resolve(strict=True)
            expected_root = verified_package.tool_catalog_path.parent.resolve(strict=True)
        except (OSError, RuntimeError):
            _identity_mismatch()
        expected_environment = {
            "OMEGA_MCP_SERVER_ID": verified_package.manifest.server_id,
            "OMEGA_MCP_PACKAGE_ROOT": str(root),
        }
        if (
            root != expected_root
            or environment != expected_environment
            or descriptor.app_id != verified_package.manifest.server_id
            or descriptor.publisher_id != verified_package.manifest.publisher_id
            or descriptor.deployment is not McpClientDeployment.LOCAL
            or descriptor.transport is not McpTransport.STDIO
            or descriptor.verified_launch_sha256
            != verified_package.manifest.artifact_sha256
            or descriptor.remote_origin is not None
            or descriptor.authorization_issuer is not None
        ):
            _identity_mismatch()
        try:
            if sys.platform == "win32":
                retained: ArtifactLaunchGuard | _PosixArtifactLaunchGuard = (
                    ArtifactLaunchGuard.acquire(
                        package_root=root,
                        artifact_path=verified_package.artifact_path,
                        expected_sha256=verified_package.manifest.artifact_sha256,
                        expected_identity=verified_package.artifact_identity,
                        expected_component_identities=expected_component_identities,
                    )
                )
                launch_command = str(verified_package.artifact_path.resolve(strict=True))
                launch_cwd: Path | str = root
                pass_fds: tuple[int, ...] = ()
            else:
                retained = _PosixArtifactLaunchGuard.acquire(
                    package_root=root,
                    artifact_path=verified_package.artifact_path,
                    expected_sha256=verified_package.manifest.artifact_sha256,
                    expected_identity=verified_package.artifact_identity,
                    expected_component_identities=expected_component_identities,
                )
                launch_command = retained.launch_command
                launch_cwd = retained.launch_cwd
                pass_fds = retained.pass_fds
            return _issue_launch_guard(
                verified_package=verified_package,
                package_root=root,
                environment=expected_environment,
                artifact_guard=retained,
                worker_contained=True,
                process_model=_ATTACHED_WORKER_PROCESS_MODEL,
                descriptor_sha256=sha256_hex(canonical_bytes(descriptor.to_dict())),
                launch_command=launch_command,
                launch_cwd=launch_cwd,
                pass_fds=pass_fds,
                provider_pin=provider_pin,
            )
        except BaseException:
            if "retained" in locals():
                retained.close()
            raise

    @property
    def verified_package(self) -> VerifiedPackage:
        return _launch_record(self).verified_package

    @property
    def package_root(self) -> Path:
        return _launch_record(self).package_root

    @property
    def artifact_path(self) -> Path:
        return _launch_record(self).artifact_path

    @property
    def arguments(self) -> tuple[str, ...]:
        return _launch_record(self).arguments

    @property
    def environment(self) -> Mapping[str, str]:
        return _launch_record(self).environment

    @property
    def closed(self) -> bool:
        record = _launch_record_for_close(self)
        if record is None:
            return True
        with record.lock:
            return record.artifact_guard is None

    def _bind(self, descriptor: McpConnectionDescriptor) -> None:
        record = _launch_record(self)
        with record.lock:
            if record.artifact_guard is None or record.bound:
                _identity_mismatch()
            try:
                exact_descriptor = (
                    record.descriptor_sha256 is not None
                    and type(descriptor) is McpConnectionDescriptor
                    and sha256_hex(canonical_bytes(descriptor.to_dict()))
                    == record.descriptor_sha256
                )
            except (TypeError, ValueError):
                exact_descriptor = False
            if not _is_verifier_issued_package(record.verified_package) or not (
                exact_descriptor
                if record.worker_contained
                else _descriptor_matches_package(descriptor, record.verified_package)
            ):
                _identity_mismatch()
            record.bound = True

    def _claim_process_creation(self) -> None:
        record = _launch_record(self)
        with record.lock:
            if (
                record.artifact_guard is None
                or not record.bound
                or record.claimed
                or not _is_verifier_issued_package(record.verified_package)
            ):
                _identity_mismatch()
            revalidate = getattr(record.artifact_guard, "revalidate", None)
            if revalidate is not None:
                try:
                    revalidate()
                except (OSError, RuntimeError, ValueError):
                    _identity_mismatch()
            record.claimed = True

    def close(self) -> None:
        record = _launch_record_for_close(self)
        if record is None:
            return
        with record.lock:
            guard = record.artifact_guard
            record.artifact_guard = None
        if guard is not None:
            guard.close()

    def __enter__(self) -> VerifiedStdioLaunchGuard:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@dataclass(slots=True)
class _LaunchRecord:
    reference: weakref.ReferenceType[VerifiedStdioLaunchGuard] | None
    proof: object
    verified_package: VerifiedPackage
    package_root: Path
    artifact_path: Path
    arguments: tuple[str, ...]
    environment: Mapping[str, str]
    artifact_guard: ArtifactLaunchGuard | _PosixArtifactLaunchGuard | None
    lock: threading.Lock
    launch_command: str
    launch_cwd: Path | str
    pass_fds: tuple[int, ...]
    provider_pin: Callable[[int], None] | None
    worker_contained: bool
    process_model: str | None
    descriptor_sha256: str | None
    bound: bool = False
    claimed: bool = False


_LAUNCH_RECORDS_LOCK = threading.Lock()
_LAUNCH_RECORDS: dict[int, _LaunchRecord] = {}


def _issue_launch_guard(
    *,
    verified_package: VerifiedPackage,
    package_root: Path,
    environment: Mapping[str, str],
    artifact_guard: ArtifactLaunchGuard | _PosixArtifactLaunchGuard,
    worker_contained: bool = False,
    process_model: str | None = None,
    descriptor_sha256: str | None = None,
    launch_command: str | None = None,
    launch_cwd: Path | str | None = None,
    pass_fds: tuple[int, ...] = (),
    provider_pin: Callable[[int], None] | None = None,
) -> VerifiedStdioLaunchGuard:
    guard = object.__new__(VerifiedStdioLaunchGuard)
    proof = secrets.token_bytes(32)
    object.__setattr__(guard, "_proof", proof)
    guard_id = id(guard)
    record = _LaunchRecord(
        reference=None,
        proof=proof,
        verified_package=verified_package,
        package_root=package_root,
        artifact_path=verified_package.artifact_path.resolve(),
        arguments=tuple(verified_package.manifest.arguments),
        environment=MappingProxyType(dict(environment)),
        artifact_guard=artifact_guard,
        lock=threading.Lock(),
        launch_command=(
            str(verified_package.artifact_path.resolve())
            if launch_command is None
            else launch_command
        ),
        launch_cwd=package_root if launch_cwd is None else launch_cwd,
        pass_fds=pass_fds,
        provider_pin=provider_pin,
        worker_contained=worker_contained,
        process_model=process_model,
        descriptor_sha256=descriptor_sha256,
    )

    def abandon(reference: weakref.ReferenceType[VerifiedStdioLaunchGuard]) -> None:
        with _LAUNCH_RECORDS_LOCK:
            retained = _LAUNCH_RECORDS.get(guard_id)
            if retained is None or retained.reference is not reference:
                return
            _LAUNCH_RECORDS.pop(guard_id, None)
        with retained.lock:
            native_guard = retained.artifact_guard
            retained.artifact_guard = None
        if native_guard is not None:
            try:
                native_guard.close()
            except BaseException:
                pass

    record.reference = weakref.ref(guard, abandon)
    with _LAUNCH_RECORDS_LOCK:
        _LAUNCH_RECORDS[guard_id] = record
    return guard


def _launch_record_for_close(guard: VerifiedStdioLaunchGuard) -> _LaunchRecord | None:
    if type(guard) is not VerifiedStdioLaunchGuard:
        return None
    with _LAUNCH_RECORDS_LOCK:
        record = _LAUNCH_RECORDS.get(id(guard))
    if record is None or record.reference is None or record.reference() is not guard:
        return None
    return record


def _launch_record(guard: VerifiedStdioLaunchGuard) -> _LaunchRecord:
    record = _launch_record_for_close(guard)
    try:
        proof = guard._proof
    except AttributeError:
        _identity_mismatch()
    if record is None or proof is not record.proof:
        _identity_mismatch()
    return record


_LOGGER = logging.getLogger(__name__)
_PROCESS_TERMINATION_TIMEOUT = 2.0
_FORCE_KILL_TIMEOUT = 2.0
_KILL_REAP_TIMEOUT = 2.0
_WRITER_FLUSH_TIMEOUT = 0.5
_EXIT_POLL_INTERVAL = 0.01


class _BoundedStdioFrameBuffer:
    """Incrementally split newline frames without ever retaining over the cap."""

    __slots__ = ("_buffer", "_max_bytes")

    def __init__(self, *, max_bytes: int) -> None:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self._max_bytes = max_bytes
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> tuple[bytes, ...]:
        if type(chunk) is not bytes:
            raise McpClientError(McpClientErrorReason.RESULT_INVALID)
        frames: list[bytes] = []
        offset = 0
        while offset < len(chunk):
            newline = chunk.find(b"\n", offset)
            end = len(chunk) if newline < 0 else newline
            segment_size = end - offset
            if len(self._buffer) + segment_size > self._max_bytes:
                self._buffer.clear()
                raise McpClientError(McpClientErrorReason.RESULT_LIMIT_EXCEEDED)
            self._buffer.extend(chunk[offset:end])
            if newline < 0:
                break
            frames.append(bytes(self._buffer))
            self._buffer.clear()
            offset = newline + 1
        return tuple(frames)

    def finish(self) -> tuple[bytes, ...]:
        if self._buffer:
            self._buffer.clear()
            raise McpClientError(McpClientErrorReason.RESULT_INVALID)
        return ()


@asynccontextmanager
async def _stdio_client(
    server: StdioServerParameters,
    errlog: TextIO = sys.stderr,
    *,
    max_frame_bytes: int = 1_048_576,
    shutdown_timeout_seconds: float = 10.0,
    worker_contained: bool = False,
    process_model: str | None = None,
    pass_fds: tuple[int, ...] = (),
    provider_pin: Callable[[int], None] | None = None,
) -> AsyncGenerator[tuple[Any, Any], None]:
    """Launch one process with the descriptor's exact closed environment."""
    if type(max_frame_bytes) is not int or max_frame_bytes <= 0:
        raise ValueError("max_frame_bytes must be a positive integer")
    if type(shutdown_timeout_seconds) not in {int, float} or shutdown_timeout_seconds <= 0:
        raise ValueError("shutdown_timeout_seconds must be positive")
    if (
        type(worker_contained) is not bool
        or (worker_contained and process_model != _ATTACHED_WORKER_PROCESS_MODEL)
        or (not worker_contained and process_model is not None)
        or worker_contained
        and sys.platform != "win32"
        and not callable(provider_pin)
        or (not worker_contained or sys.platform == "win32")
        and provider_pin is not None
        or type(pass_fds) is not tuple
        or any(
            type(file_descriptor) is not int or file_descriptor < 0
            for file_descriptor in pass_fds
        )
    ):
        raise ValueError("stdio containment configuration invalid")
    process = await _create_platform_process(
        command=server.command,
        args=server.args,
        env=dict(server.env or {}),
        errlog=errlog,
        cwd=server.cwd,
        worker_contained=worker_contained,
        pass_fds=pass_fds,
        provider_pin=provider_pin,
    )
    read_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_stream, write_reader = anyio.create_memory_object_stream[SessionMessage](0)
    shutting_down = False
    writer_done = anyio.Event()

    async def stdout_reader() -> None:
        assert process.stdout is not None
        frames = _BoundedStdioFrameBuffer(max_bytes=max_frame_bytes)
        try:
            async with read_writer:
                try:
                    async for chunk in process.stdout:
                        try:
                            lines = frames.feed(chunk)
                        except McpClientError as error:
                            await read_writer.send(error)
                            return
                        for line in lines:
                            try:
                                decoded = line.decode(
                                    encoding=server.encoding,
                                    errors=server.encoding_error_handler,
                                )
                                await read_writer.send(_parse_line(decoded))
                            except UnicodeError:
                                await read_writer.send(
                                    McpClientError(McpClientErrorReason.RESULT_INVALID)
                                )
                            except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                                return
                finally:
                    try:
                        frames.finish()
                    except McpClientError as error:
                        with suppress(anyio.ClosedResourceError, anyio.BrokenResourceError):
                            await read_writer.send(error)
        except anyio.ClosedResourceError:
            pass
        except (anyio.BrokenResourceError, ConnectionError):
            if not shutting_down:
                _LOGGER.exception("Reading from the MCP server stdout failed")

    async def stdin_writer() -> None:
        assert process.stdin is not None
        try:
            async with write_reader:
                async for session_message in write_reader:
                    payload = session_message.message.model_dump_json(
                        by_alias=True,
                        exclude_unset=True,
                    )
                    await process.stdin.send(
                        (payload + "\n").encode(
                            encoding=server.encoding,
                            errors=server.encoding_error_handler,
                        )
                    )
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
            await read_writer.aclose()
        finally:
            writer_done.set()

    async def shutdown() -> None:
        deadline = anyio.current_time() + float(shutdown_timeout_seconds)
        read_stream.close()
        write_stream.close()
        flush_timeout = min(
            _WRITER_FLUSH_TIMEOUT,
            max(0.0, deadline - anyio.current_time()),
        )
        with anyio.move_on_after(flush_timeout) as flush_scope:
            await writer_done.wait()
        if flush_scope.cancelled_caught:
            await anyio.lowlevel.cancel_shielded_checkpoint()
        if worker_contained:
            await _stop_server_process(
                process,
                deadline=deadline,
                worker_contained=True,
            )
        else:
            await _stop_server_process(process, deadline=deadline)
        with anyio.move_on_after(max(0.0, deadline - anyio.current_time())):
            await _aclose_all(read_stream, write_stream, read_writer, write_reader)
        if anyio.current_time() > deadline:
            raise McpClientError(McpClientErrorReason.CLEANUP_REQUIRED)
        await anyio.lowlevel.checkpoint()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(stdout_reader)
        task_group.start_soon(stdin_writer)
        try:
            yield read_stream, write_stream
        finally:
            shutting_down = True
            with anyio.CancelScope(shield=True):
                await shutdown()
            task_group.cancel_scope.cancel()
    await anyio.lowlevel.cancel_shielded_checkpoint()


@asynccontextmanager
async def stdio_client(
    server: StdioServerParameters,
    errlog: TextIO = sys.stderr,
    *,
    max_frame_bytes: int = 1_048_576,
    shutdown_timeout_seconds: float = 10.0,
    worker_contained: bool = False,
    process_model: str | None = None,
    pass_fds: tuple[int, ...] = (),
) -> AsyncGenerator[tuple[Any, Any], None]:
    """Launch a normal stdio client; worker containment uses the private seam."""

    async with _stdio_client(
        server,
        errlog,
        max_frame_bytes=max_frame_bytes,
        shutdown_timeout_seconds=shutdown_timeout_seconds,
        worker_contained=worker_contained,
        process_model=process_model,
        pass_fds=pass_fds,
        provider_pin=None,
    ) as streams:
        yield streams


def _parse_line(line: str) -> SessionMessage | Exception:
    try:
        message = types.jsonrpc_message_adapter.validate_json(line, by_name=False)
    except ValueError as exc:
        return exc
    return SessionMessage(message)


async def _stop_server_process(
    process: ServerProcess,
    *,
    deadline: float,
    worker_contained: bool = False,
) -> None:
    assert process.stdin is not None and process.stdout is not None
    remaining = max(0.0, deadline - anyio.current_time())
    with anyio.move_on_after(remaining):
        await _close_pipe(process.stdin)
    remaining = max(0.0, deadline - anyio.current_time())
    if not await _wait_for_process_exit(
        process,
        min(_PROCESS_TERMINATION_TIMEOUT, remaining),
    ):
        termination_operation = (
            _terminate_process_tree(process, worker_contained=True)
            if worker_contained
            else _terminate_process_tree(process)
        )
        termination = asyncio.create_task(
            termination_operation,
            name=f"omega-mcp-process-tree-stop:{process.pid}",
        )
        try:
            done, _pending = await asyncio.wait(
                {termination},
                timeout=max(0.0, deadline - anyio.current_time()),
            )
        except BaseException:
            termination.cancel()
            termination.add_done_callback(_consume_asyncio_task)
            raise
        if termination not in done:
            termination.cancel()
            termination.add_done_callback(_consume_asyncio_task)
            close_process_job(process)
            with anyio.move_on_after(max(0.0, deadline - anyio.current_time())):
                await _close_pipe(process.stdout)
            raise McpClientError(McpClientErrorReason.CLEANUP_REQUIRED)
        termination.result()
        remaining = max(0.0, deadline - anyio.current_time())
        if not await _wait_for_process_exit(process, min(_KILL_REAP_TIMEOUT, remaining)):
            close_process_job(process)
            with anyio.move_on_after(max(0.0, deadline - anyio.current_time())):
                await _close_pipe(process.stdout)
            raise McpClientError(McpClientErrorReason.CLEANUP_REQUIRED)
    close_process_job(process)
    with anyio.move_on_after(max(0.0, deadline - anyio.current_time())):
        await _close_pipe(process.stdout)


def _consume_asyncio_task(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    with suppress(BaseException):
        task.exception()


async def _close_pipe(stream: AsyncResource) -> None:
    with suppress(OSError, anyio.BrokenResourceError, anyio.ClosedResourceError):
        await stream.aclose()


async def _wait_for_process_exit(process: ServerProcess, timeout: float) -> bool:
    deadline = anyio.current_time() + timeout
    while process.returncode is None:
        if anyio.current_time() >= deadline:
            return False
        await anyio.sleep(_EXIT_POLL_INTERVAL)
    return True


async def _terminate_process_tree(
    process: ServerProcess,
    *,
    worker_contained: bool = False,
) -> None:
    if sys.platform == "win32":
        await terminate_windows_process_tree(process)
    elif worker_contained:
        assert isinstance(process, Process)
        with suppress(ProcessLookupError):
            process.terminate()
        if not await _wait_for_process_exit(process, _FORCE_KILL_TIMEOUT):
            with suppress(ProcessLookupError):
                process.kill()
    else:
        assert isinstance(process, Process)
        await terminate_posix_process_tree(process, _FORCE_KILL_TIMEOUT)


async def _create_platform_process(
    *,
    command: str,
    args: list[str],
    env: dict[str, str],
    errlog: TextIO,
    cwd: Path | str | None,
    worker_contained: bool = False,
    pass_fds: tuple[int, ...] = (),
    provider_pin: Callable[[int], None] | None = None,
) -> ServerProcess:
    if sys.platform == "win32":
        if pass_fds or provider_pin is not None:
            raise ValueError("stdio containment configuration invalid")
        return await create_windows_process(command, args, env, errlog, cwd)
    if not worker_contained:
        if provider_pin is not None:
            raise ValueError("stdio containment configuration invalid")
        return await anyio.open_process(
            [command, *args],
            env=env,
            stderr=errlog,
            cwd=cwd,
            start_new_session=True,
            pass_fds=pass_fds,
        )
    if not callable(provider_pin):
        raise ValueError("stdio containment configuration invalid")

    gate_read, gate_write = os.pipe()
    process: Process | None = None
    try:
        command_line = [
            str(portable_python_path(platform=sys.platform)),
            "-I",
            "-S",
            "-c",
            _POSIX_ATTACHED_EXEC,
            str(gate_read),
            command,
            *args,
        ]
        process = await anyio.open_process(
            command_line,
            env=env,
            stderr=errlog,
            cwd=cwd,
            start_new_session=False,
            pass_fds=(*pass_fds, gate_read),
        )
    except BaseException:
        os.close(gate_read)
        os.close(gate_write)
        raise
    os.close(gate_read)
    try:
        if type(process.pid) is not int or process.pid <= 0:
            raise ValueError("stdio containment configuration invalid")
        provider_pin(process.pid)
        if os.write(gate_write, b"\x01") != 1:
            raise OSError("stdio containment gate unavailable")
    except BaseException:
        os.close(gate_write)
        with suppress(ProcessLookupError):
            process.kill()
        with anyio.move_on_after(_FORCE_KILL_TIMEOUT):
            await process.wait()
        raise
    os.close(gate_write)
    return process


async def _aclose_all(*streams: AsyncResource) -> None:
    for stream in streams:
        await stream.aclose()


class _CapturedVerifiedStdioTransport:
    """Own stderr capture and the retained guard through SDK process creation."""

    def __init__(
        self,
        transport: Any,
        capture: RedactingStderrCapture,
        launch_guard: VerifiedStdioLaunchGuard,
    ) -> None:
        self._transport = transport
        self._capture = capture
        self._launch_guard = launch_guard
        self._entered = False

    async def __aenter__(self) -> Any:
        entered = False
        try:
            self._capture.start()
            streams = await self._transport.__aenter__()
            entered = True
            self._entered = True
            self._launch_guard.close()
            self._capture.close_parent_writer()
            return streams
        except BaseException as primary:
            if entered:
                try:
                    await self._transport.__aexit__(type(primary), primary, primary.__traceback__)
                except BaseException:
                    pass
            self._entered = False
            try:
                self._launch_guard.close()
            except BaseException:
                pass
            try:
                self._capture.close()
            except BaseException:
                pass
            raise

    async def __aexit__(self, *args: Any) -> Any:
        if not self._entered:
            return None
        primary = args[1] if len(args) >= 2 and isinstance(args[1], BaseException) else None
        transport_result: Any = None
        cleanup_error: BaseException | None = None
        try:
            transport_result = await self._transport.__aexit__(*args)
        except BaseException as exc:
            cleanup_error = exc
        finally:
            try:
                self._capture.close()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
            self._entered = False
        if primary is None and cleanup_error is not None:
            raise cleanup_error
        return transport_result


@dataclass(frozen=True, slots=True, eq=False)
class _StdioSdkClientBuilder:
    descriptor: McpConnectionDescriptor
    launch_guard: VerifiedStdioLaunchGuard
    errlog: RedactingTextSink

    def __call__(
        self,
        *,
        mode: str,
        client_info: types.Implementation,
        extensions: tuple[object, ...],
        cache: None,
    ) -> Client:
        capture: RedactingStderrCapture | None = None
        try:
            self.launch_guard._claim_process_creation()
            launch = _launch_record(self.launch_guard)
            params = StdioServerParameters(
                command=launch.launch_command,
                args=list(self.launch_guard.arguments),
                env=dict(self.launch_guard.environment),
                cwd=launch.launch_cwd,
                encoding="utf-8",
                encoding_error_handler="strict",
            )
            capture = RedactingStderrCapture(
                self.errlog,
                shutdown_timeout_seconds=(self.descriptor.limits.max_shutdown_duration_ms / 1_000),
            )
            containment: dict[str, object] = {}
            transport_factory = stdio_client
            if launch.worker_contained:
                transport_factory = _stdio_client
                containment = {
                    "worker_contained": True,
                    "process_model": launch.process_model,
                    "pass_fds": launch.pass_fds,
                    "provider_pin": launch.provider_pin,
                }
            transport = _CapturedVerifiedStdioTransport(
                transport_factory(
                    params,
                    errlog=capture,
                    max_frame_bytes=max(
                        self.descriptor.limits.max_discovery_document_bytes,
                        self.descriptor.limits.max_result_document_bytes,
                    ),
                    shutdown_timeout_seconds=(
                        self.descriptor.limits.max_shutdown_duration_ms / 1_000
                    ),
                    **containment,
                ),
                capture,
                self.launch_guard,
            )
            return Client(
                transport,
                mode=mode,
                client_info=client_info,
                extensions=extensions,
                cache=cache,
                read_timeout_seconds=(self.descriptor.limits.max_operation_duration_ms / 1_000),
            )
        except BaseException:
            try:
                self.launch_guard.close()
            except BaseException:
                pass
            if capture is not None:
                try:
                    capture.close()
                except BaseException:
                    pass
            raise


def build_verified_stdio_transport(
    descriptor: McpConnectionDescriptor,
    launch_guard: VerifiedStdioLaunchGuard,
    errlog: RedactingTextSink,
) -> SdkClientFactory:
    """Issue one descriptor-bound SDK factory for an exact verified stdio launch."""
    try:
        if (
            type(descriptor) is not McpConnectionDescriptor
            or type(launch_guard) is not VerifiedStdioLaunchGuard
            or type(errlog) is not RedactingTextSink
            or errlog.max_bytes != descriptor.limits.max_stderr_bytes
        ):
            _identity_mismatch()
        launch_guard._bind(descriptor)
        builder = _StdioSdkClientBuilder(descriptor, launch_guard, errlog)
        return _issue_sdk_client_factory(
            _SDK_CLIENT_FACTORY_ISSUER,
            deployment=McpClientDeployment.LOCAL,
            transport=McpTransport.STDIO,
            builder=builder,
            builder_identity=launch_guard,
        )
    except BaseException:
        if type(launch_guard) is VerifiedStdioLaunchGuard:
            try:
                launch_guard.close()
            except BaseException:
                pass
        raise


__all__ = ["VerifiedStdioLaunchGuard", "build_verified_stdio_transport"]

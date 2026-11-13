"""Killable, generation-fenced provider execution in a spawned process.

The worker is intentionally internal.  It accepts one authenticated operation on
anonymous pipes, returns one authenticated terminal result, and exits.  Provider
credentials never enter argv, the environment, or a filesystem path.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ctypes
import json
import logging
import math
import multiprocessing
import os
import re
import secrets
import signal
import struct
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from multiprocessing.connection import Connection
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from mcp_platform.canonical import canonical_bytes

_HEX = frozenset("0123456789abcdef")
_AUTHORITY_KINDS = frozenset({"tool", "resource", "resource_template", "prompt"})
_FRAME_VERSION = 1
_FRAME_OVERHEAD_BYTES = 65_536
# Mirrors the canonical MCP result-document boundary plus authenticated frame
# overhead without importing the SDK-heavy client module in every fresh child.
_DEFAULT_MAX_FRAME_BYTES = 1_048_576 + _FRAME_OVERHEAD_BYTES
_MAX_GENERATION = 9_223_372_036_854_775_807
_OMEGA_VERSION = "3.0.0"
_ATTACHED_WORKER_PROCESS_MODEL = "attached-worker"
_LOCAL_PROCESS_QUARANTINE_SCHEMA = 1
_LOCAL_PROCESS_QUARANTINE_NAME = "plugin_execution_process_quarantine.json"
_LOCAL_PROCESS_QUARANTINE_MAX_BYTES = 1_048_576
_LOCAL_PROCESS_QUARANTINE_LOCK = threading.RLock()
_POSIX_PROCESS_POLL_SECONDS = 0.01
_POSIX_PROCESS_CLEANUP_SECONDS = 2.0
_LINUX_GUARDIAN_CLEAN_EXIT = 0
_LINUX_GUARDIAN_VIOLATION_EXIT = 73
_LINUX_GUARDIAN_REAP_FAILED_EXIT = 74
_LINUX_GUARDIAN_START_FAILED_EXIT = 75
_LINUX_GUARDIAN_RETRY_SIGNAL = getattr(signal, "SIGUSR1", 10)
_LINUX_GUARDIAN_INLINE_CLEANUP_ATTEMPTS = 2


class PluginExecutionWorkerError(RuntimeError):
    """Stable worker failure that never includes provider-controlled content."""


def _stable_text(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError("plugin_worker_authority_invalid")
    return value


def _revision(value: object) -> str:
    value = _stable_text(value)
    if len(value) != 64 or any(character not in _HEX for character in value):
        raise ValueError("plugin_worker_authority_invalid")
    return value


def _absolute_path_text(value: object) -> str:
    value = _stable_text(value)
    try:
        path = Path(value)
        if (
            not path.is_absolute()
            or ".." in path.parts
            or path == Path(path.anchor)
            or str(path) != value
        ):
            raise ValueError
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ValueError("plugin_worker_local_execution_invalid") from None
    return value


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("plugin_worker_payload_invalid")
        return {key: _plain_json(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_plain_json(item) for item in value]
    if value is None or type(value) in {str, bool, int, float}:
        canonical_bytes(value)
        return value
    raise ValueError("plugin_worker_payload_invalid")


@dataclass(frozen=True, slots=True)
class PluginExecutionAuthority:
    """Exact immutable authority the parent validates before worker launch."""

    plugin_id: str
    app_id: str
    account_key_sha256: str
    package_revision: str
    connection_revision: str
    authority_revision: str
    capability_revision: str
    capability_kind: str
    capability_id: str
    server_id: str | None = None
    server_version: str | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "plugin_id", _stable_text(self.plugin_id))
            object.__setattr__(self, "app_id", _stable_text(self.app_id))
            object.__setattr__(self, "account_key_sha256", _revision(self.account_key_sha256))
            object.__setattr__(self, "package_revision", _revision(self.package_revision))
            object.__setattr__(self, "connection_revision", _revision(self.connection_revision))
            object.__setattr__(self, "authority_revision", _revision(self.authority_revision))
            object.__setattr__(self, "capability_revision", _revision(self.capability_revision))
            if self.capability_kind not in _AUTHORITY_KINDS:
                raise ValueError
            object.__setattr__(self, "capability_id", _stable_text(self.capability_id))
            if (self.server_id is None) != (self.server_version is None):
                raise ValueError
            if self.server_id is not None:
                object.__setattr__(self, "server_id", _stable_text(self.server_id))
                object.__setattr__(
                    self,
                    "server_version",
                    _stable_text(self.server_version),
                )
        except (TypeError, ValueError):
            raise ValueError("plugin_worker_authority_invalid") from None

    @property
    def execution_key(self) -> tuple[str, str, str]:
        return (self.plugin_id, self.app_id, self.account_key_sha256)

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "app_id": self.app_id,
            "account_key_sha256": self.account_key_sha256,
            "package_revision": self.package_revision,
            "connection_revision": self.connection_revision,
            "authority_revision": self.authority_revision,
            "capability_revision": self.capability_revision,
            "capability_kind": self.capability_kind,
            "capability_id": self.capability_id,
            "server_id": self.server_id,
            "server_version": self.server_version,
        }

    @classmethod
    def from_dict(cls, value: object) -> PluginExecutionAuthority:
        if not isinstance(value, Mapping) or set(value) != {
            "plugin_id",
            "app_id",
            "account_key_sha256",
            "package_revision",
            "connection_revision",
            "authority_revision",
            "capability_revision",
            "capability_kind",
            "capability_id",
            "server_id",
            "server_version",
        }:
            raise ValueError("plugin_worker_authority_invalid")
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class _LocalMcpWorkerExecution:
    """Authenticated, content-free pointers to one exact installed local app."""

    plugin_registry_root: str
    plugin_registry_revision: str
    plugin_package_root: str
    mcp_state_root: str
    app_registry_revision: str
    package_registry_revision: str
    mcp_package_root: str
    mcp_package_manifest_sha256: str
    executable_path: str
    executable_sha256: str
    executable_identity: tuple[int, int]
    process_model: str = _ATTACHED_WORKER_PROCESS_MODEL

    def __post_init__(self) -> None:
        try:
            for field_name in (
                "plugin_registry_root",
                "plugin_package_root",
                "mcp_state_root",
                "mcp_package_root",
                "executable_path",
            ):
                object.__setattr__(
                    self,
                    field_name,
                    _absolute_path_text(getattr(self, field_name)),
                )
            for field_name in (
                "plugin_registry_revision",
                "app_registry_revision",
                "package_registry_revision",
                "mcp_package_manifest_sha256",
                "executable_sha256",
            ):
                object.__setattr__(
                    self,
                    field_name,
                    _revision(getattr(self, field_name)),
                )
            if (
                type(self.executable_identity) is not tuple
                or len(self.executable_identity) != 2
                or any(type(value) is not int or value <= 0 for value in self.executable_identity)
                or self.process_model != _ATTACHED_WORKER_PROCESS_MODEL
            ):
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("plugin_worker_local_execution_invalid") from None

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_registry_root": self.plugin_registry_root,
            "plugin_registry_revision": self.plugin_registry_revision,
            "plugin_package_root": self.plugin_package_root,
            "mcp_state_root": self.mcp_state_root,
            "app_registry_revision": self.app_registry_revision,
            "package_registry_revision": self.package_registry_revision,
            "mcp_package_root": self.mcp_package_root,
            "mcp_package_manifest_sha256": self.mcp_package_manifest_sha256,
            "executable_path": self.executable_path,
            "executable_sha256": self.executable_sha256,
            "executable_identity": [str(value) for value in self.executable_identity],
            "process_model": self.process_model,
        }

    @classmethod
    def from_dict(cls, value: object) -> _LocalMcpWorkerExecution:
        expected = {
            "plugin_registry_root",
            "plugin_registry_revision",
            "plugin_package_root",
            "mcp_state_root",
            "app_registry_revision",
            "package_registry_revision",
            "mcp_package_root",
            "mcp_package_manifest_sha256",
            "executable_path",
            "executable_sha256",
            "executable_identity",
            "process_model",
        }
        if type(value) is not dict or set(value) != expected:
            raise ValueError("plugin_worker_local_execution_invalid")
        document = dict(value)
        identity = document["executable_identity"]
        if (
            type(identity) is not list
            or len(identity) != 2
            or any(
                type(item) is not str
                or not item.isascii()
                or not item.isdigit()
                or int(item) <= 0
                for item in identity
            )
        ):
            raise ValueError("plugin_worker_local_execution_invalid")
        document["executable_identity"] = tuple(int(item) for item in identity)
        return cls(**document)


def _local_process_quarantine_path(execution: _LocalMcpWorkerExecution) -> Path:
    if type(execution) is not _LocalMcpWorkerExecution:
        raise ValueError("plugin_worker_local_execution_invalid")
    return Path(execution.mcp_state_root) / _LOCAL_PROCESS_QUARANTINE_NAME


def _local_process_identity(
    authority: PluginExecutionAuthority,
    execution: _LocalMcpWorkerExecution,
) -> dict[str, object]:
    if (
        type(authority) is not PluginExecutionAuthority
        or type(execution) is not _LocalMcpWorkerExecution
    ):
        raise ValueError("plugin_worker_local_execution_invalid")
    return {
        "plugin_id": authority.plugin_id,
        "app_id": authority.app_id,
        "package_revision": authority.package_revision,
        "mcp_package_root": execution.mcp_package_root,
        "mcp_package_manifest_sha256": execution.mcp_package_manifest_sha256,
        "executable_path": execution.executable_path,
        "executable_sha256": execution.executable_sha256,
        "executable_identity": [str(item) for item in execution.executable_identity],
        "process_model": execution.process_model,
    }


def _validate_local_process_identity(value: object) -> dict[str, object]:
    expected = {
        "plugin_id",
        "app_id",
        "package_revision",
        "mcp_package_root",
        "mcp_package_manifest_sha256",
        "executable_path",
        "executable_sha256",
        "executable_identity",
        "process_model",
    }
    if type(value) is not dict or set(value) != expected:
        raise ValueError
    identity = value["executable_identity"]
    if (
        type(identity) is not list
        or len(identity) != 2
        or any(
            type(item) is not str
            or not item.isascii()
            or not item.isdigit()
            or int(item) <= 0
            for item in identity
        )
        or value["process_model"] != _ATTACHED_WORKER_PROCESS_MODEL
    ):
        raise ValueError
    for field_name in ("plugin_id", "app_id"):
        _stable_text(value[field_name])
    for field_name in (
        "package_revision",
        "mcp_package_manifest_sha256",
        "executable_sha256",
    ):
        _revision(value[field_name])
    for field_name in ("mcp_package_root", "executable_path"):
        _absolute_path_text(value[field_name])
    return dict(value)


def _read_local_process_quarantine(path: Path) -> list[dict[str, object]]:
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return []
    if not payload or len(payload) > _LOCAL_PROCESS_QUARANTINE_MAX_BYTES:
        raise ValueError
    document = json.loads(payload)
    if (
        type(document) is not dict
        or set(document) != {"schema_version", "incompatible"}
        or document["schema_version"] != _LOCAL_PROCESS_QUARANTINE_SCHEMA
        or type(document["incompatible"]) is not list
        or canonical_bytes(document) != payload
    ):
        raise ValueError
    entries = [
        _validate_local_process_identity(item) for item in document["incompatible"]
    ]
    encoded = [canonical_bytes(item) for item in entries]
    if encoded != sorted(encoded) or len(encoded) != len(set(encoded)):
        raise ValueError
    return entries


def _write_local_process_quarantine(
    path: Path,
    entries: list[dict[str, object]],
) -> None:
    payload = canonical_bytes(
        {
            "schema_version": _LOCAL_PROCESS_QUARANTINE_SCHEMA,
            "incompatible": entries,
        }
    )
    if len(payload) > _LOCAL_PROCESS_QUARANTINE_MAX_BYTES:
        raise ValueError
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(16)}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _quarantine_local_execution(
    authority: PluginExecutionAuthority,
    execution: _LocalMcpWorkerExecution,
) -> None:
    from mcp_platform.file_lock import file_lock

    identity = _local_process_identity(authority, execution)
    path = _local_process_quarantine_path(execution)
    try:
        with _LOCAL_PROCESS_QUARANTINE_LOCK, file_lock(path, timeout=2.0):
            entries = _read_local_process_quarantine(path)
            if identity not in entries:
                entries.append(identity)
                entries.sort(key=canonical_bytes)
                _write_local_process_quarantine(path, entries)
    except (OSError, TypeError, ValueError):
        raise PluginExecutionWorkerError("plugin_worker_quarantine_unavailable") from None


def _assert_local_execution_not_quarantined(
    authority: PluginExecutionAuthority,
    execution: _LocalMcpWorkerExecution,
) -> None:
    from mcp_platform.file_lock import file_lock

    identity = _local_process_identity(authority, execution)
    path = _local_process_quarantine_path(execution)
    try:
        with _LOCAL_PROCESS_QUARANTINE_LOCK, file_lock(path, timeout=2.0):
            entries = _read_local_process_quarantine(path)
    except (OSError, TypeError, ValueError):
        raise PluginExecutionWorkerError("plugin_worker_quarantine_unavailable") from None
    if identity in entries:
        raise PluginExecutionWorkerError("plugin_local_process_model_incompatible")


@dataclass(frozen=True, slots=True)
class PluginExecutionTerminalOutcome:
    authority: PluginExecutionAuthority
    generation: int
    status: str


@dataclass(frozen=True, slots=True)
class _PosixProcessRecord:
    pid: int
    ppid: int
    process_group: int
    session: int
    start_time: int
    state: str = "S"
    parent_start_time: int | None = None

    def __post_init__(self) -> None:
        if (
            any(
                type(value) is not int or value < 0
                for value in (
                    self.pid,
                    self.ppid,
                    self.process_group,
                    self.session,
                    self.start_time,
                )
            )
            or self.pid <= 0
            or self.process_group <= 0
            or self.session <= 0
            or type(self.state) is not str
            or len(self.state) != 1
            or self.parent_start_time is not None
            and (
                type(self.parent_start_time) is not int
                or self.parent_start_time <= 0
            )
        ):
            raise ValueError("plugin_worker_process_table_invalid")

    @property
    def identity(self) -> tuple[int, int]:
        return (self.pid, self.start_time)

    @property
    def live(self) -> bool:
        return self.state not in {"X", "Z"}

    def attached_to(self, worker_pid: int) -> bool:
        return self.process_group == worker_pid and self.session == worker_pid


class _DarwinProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class _DarwinProcUniqueIdentifierInfo(ctypes.Structure):
    _fields_ = [
        ("p_uuid", ctypes.c_uint8 * 16),
        ("p_uniqueid", ctypes.c_uint64),
        ("p_puniqueid", ctypes.c_uint64),
        ("p_idversion", ctypes.c_int32),
        ("p_orig_ppidversion", ctypes.c_int32),
        ("p_reserve2", ctypes.c_uint64),
        ("p_reserve3", ctypes.c_uint64),
    ]


class _DarwinProcBsdInfoWithUniqueId(ctypes.Structure):
    _fields_ = [
        ("pbsd", _DarwinProcBsdInfo),
        ("p_uniqidentifier", _DarwinProcUniqueIdentifierInfo),
    ]


def _load_darwin_libproc() -> tuple[Callable[..., int], Callable[..., int]]:
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    list_all_pids = libproc.proc_listallpids
    list_all_pids.argtypes = [ctypes.c_void_p, ctypes.c_int]
    list_all_pids.restype = ctypes.c_int
    pid_info = libproc.proc_pidinfo
    pid_info.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    pid_info.restype = ctypes.c_int
    return list_all_pids, pid_info


def _darwin_session_id(pid: int) -> int:
    return os.getsid(pid)


def _read_darwin_process_table() -> dict[int, _PosixProcessRecord]:
    try:
        list_all_pids, pid_info = _load_darwin_libproc()
        capacity = int(list_all_pids(None, 0))
        if capacity <= 0:
            raise OSError
        capacity += 128
        pids = (ctypes.c_int32 * capacity)()
        count = int(list_all_pids(pids, ctypes.sizeof(pids)))
        if count <= 0 or count > capacity:
            raise OSError
        structure_size = ctypes.sizeof(_DarwinProcBsdInfoWithUniqueId)
        if structure_size != 192:
            raise OSError
        table: dict[int, _PosixProcessRecord] = {}
        for pid in sorted({int(pids[index]) for index in range(count)}):
            if pid <= 0:
                continue
            information = _DarwinProcBsdInfoWithUniqueId()
            read = int(
                pid_info(
                    pid,
                    18,  # PROC_PIDT_BSDINFOWITHUNIQID
                    0,
                    ctypes.byref(information),
                    structure_size,
                )
            )
            if read == 0:
                continue
            if read != structure_size or information.pbsd.pbi_pid != pid:
                raise OSError
            unique_id = int(information.p_uniqidentifier.p_uniqueid)
            parent_unique_id = int(information.p_uniqidentifier.p_puniqueid)
            if unique_id <= 0:
                raise OSError
            try:
                session = int(_darwin_session_id(pid))
            except OSError:
                continue
            table[pid] = _PosixProcessRecord(
                pid=pid,
                ppid=int(information.pbsd.pbi_ppid),
                process_group=int(information.pbsd.pbi_pgid),
                session=session,
                start_time=unique_id,
                state="Z" if information.pbsd.pbi_status == 5 else "S",
                parent_start_time=(
                    None if parent_unique_id <= 0 else parent_unique_id
                ),
            )
        if not table:
            raise OSError
        return table
    except (AttributeError, OSError, TypeError, ValueError):
        raise OSError("plugin_worker_process_table_unavailable") from None


def _read_posix_process_table() -> dict[int, _PosixProcessRecord]:
    if sys.platform == "darwin":
        return _read_darwin_process_table()
    if os.name == "nt" or sys.platform != "linux":
        raise OSError("plugin_worker_process_table_unavailable")
    table: dict[int, _PosixProcessRecord] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isascii() or not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="ascii")
            command_end = raw.rfind(")")
            if command_end <= 0:
                continue
            fields = raw[command_end + 2 :].split()
            if len(fields) < 20:
                continue
            pid = int(entry.name)
            table[pid] = _PosixProcessRecord(
                pid=pid,
                state=fields[0],
                ppid=int(fields[1]),
                process_group=int(fields[2]),
                session=int(fields[3]),
                start_time=int(fields[19]),
            )
        except (FileNotFoundError, PermissionError, ProcessLookupError, UnicodeError, ValueError):
            continue
    return table


def _discover_posix_descendants(
    root_pid: int,
    table: Mapping[int, _PosixProcessRecord],
    *,
    known: tuple[_PosixProcessRecord, ...] = (),
) -> dict[int, _PosixProcessRecord]:
    if (
        type(root_pid) is not int
        or root_pid <= 0
        or not isinstance(table, Mapping)
        or any(
            type(pid) is not int
            or type(record) is not _PosixProcessRecord
            or pid != record.pid
            for pid, record in table.items()
        )
        or type(known) is not tuple
        or any(type(record) is not _PosixProcessRecord for record in known)
    ):
        raise ValueError("plugin_worker_process_table_invalid")
    retained_root = next((record for record in known if record.pid == root_pid), None)
    ancestor_pids: set[int] = set()
    ancestor_start_times = {record.start_time for record in known}
    for record in known:
        observed = table.get(record.pid)
        if observed is not None and observed.start_time == record.start_time:
            ancestor_pids.add(record.pid)
    root = table.get(root_pid)
    if retained_root is None:
        ancestor_pids.add(root_pid)
    if root is not None and (
        retained_root is None or root.start_time == retained_root.start_time
    ):
        ancestor_pids.add(root_pid)
        ancestor_start_times.add(root.start_time)
    descendants: dict[int, _PosixProcessRecord] = {}
    changed = True
    while changed:
        changed = False
        for pid, record in table.items():
            if pid in ancestor_pids or (
                record.ppid not in ancestor_pids
                and record.parent_start_time not in ancestor_start_times
            ):
                continue
            ancestor_pids.add(pid)
            ancestor_start_times.add(record.start_time)
            descendants[pid] = record
            changed = True
    return descendants


def _enable_posix_subreaper() -> bool:
    if os.name == "nt":
        return True
    if sys.platform != "linux" or not Path("/proc/self/stat").is_file():
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        prctl.restype = ctypes.c_int
        return prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _set_linux_parent_death_signal(expected_parent_pid: int) -> bool:
    if (
        sys.platform != "linux"
        or os.name == "nt"
        or type(expected_parent_pid) is not int
        or expected_parent_pid <= 0
    ):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        prctl.restype = ctypes.c_int
        return (
            prctl(1, int(signal.SIGKILL), 0, 0, 0) == 0  # PR_SET_PDEATHSIG
            and os.getppid() == expected_parent_pid
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _prepare_posix_process_model() -> bool:
    if sys.platform == "linux":
        return _enable_posix_subreaper()
    if sys.platform == "darwin":
        try:
            _read_darwin_process_table()
        except OSError:
            return False
        return True
    return False


def _posix_process_model_available() -> bool:
    if sys.platform == "linux":
        return Path("/proc/self/stat").is_file()
    if sys.platform == "darwin":
        try:
            _read_darwin_process_table()
        except OSError:
            return False
        return True
    return False


class _PosixDescendantMonitor:
    """Track exact worker descendants, including later reparented escapees."""

    def __init__(
        self,
        worker_pid: int,
        *,
        monitor_deadline: float,
        enforce_attachment: bool = True,
    ) -> None:
        if (
            os.name == "nt"
            or sys.platform not in {"darwin", "linux"}
            or type(worker_pid) is not int
            or worker_pid <= 0
            or type(monitor_deadline) not in {int, float}
            or not math.isfinite(monitor_deadline)
            or monitor_deadline <= 0
            or type(enforce_attachment) is not bool
        ):
            raise OSError("plugin_worker_process_table_unavailable")
        _read_posix_process_table()
        self._worker_pid = worker_pid
        self._monitor_deadline = float(monitor_deadline)
        self._enforce_attachment = enforce_attachment
        self._lock = threading.Lock()
        self._finish_lock = threading.Lock()
        self._stop = threading.Event()
        self._root_record: _PosixProcessRecord | None = None
        self._tracked: dict[int, _PosixProcessRecord] = {}
        self._escape_detected = False
        self._thread = threading.Thread(
            target=self._run,
            name=f"omega-plugin-descendants:{worker_pid}",
            daemon=True,
        )
        self._started = False
        self._finished = False
        self._final_violation = False
        self._final_clean = False

    def start(self) -> None:
        if self._started:
            raise RuntimeError("plugin_worker_process_monitor_invalid")
        self._sample()
        self._started = True
        self._thread.start()

    def _sample(self) -> dict[int, _PosixProcessRecord]:
        table = _read_posix_process_table()
        with self._lock:
            if self._root_record is None:
                root = table.get(self._worker_pid)
                if root is None or not root.live:
                    raise OSError("plugin_worker_process_table_unavailable")
                self._root_record = root
            known = (self._root_record, *self._tracked.values())
        descendants = _discover_posix_descendants(
            self._worker_pid,
            table,
            known=known,
        )
        with self._lock:
            for pid, record in descendants.items():
                prior = self._tracked.get(pid)
                if prior is None or prior.start_time == record.start_time:
                    self._tracked[pid] = record
                if self._enforce_attachment and not record.attached_to(
                    self._worker_pid
                ):
                    self._escape_detected = True
            current: dict[int, _PosixProcessRecord] = {}
            for pid, expected in self._tracked.items():
                observed = table.get(pid)
                if observed is not None and observed.start_time == expected.start_time:
                    current[pid] = observed
                    if self._enforce_attachment and not observed.attached_to(
                        self._worker_pid
                    ):
                        self._escape_detected = True
            return current

    def pin(
        self,
        pid: int,
        *,
        expected_start_time: int | None = None,
    ) -> _PosixProcessRecord:
        """Pin one still-attached direct child by exact native identity."""

        with self._finish_lock:
            if (
                not self._started
                or self._finished
                or self._stop.is_set()
                or type(pid) is not int
                or pid <= 0
                or pid == self._worker_pid
                or expected_start_time is not None
                and (type(expected_start_time) is not int or expected_start_time <= 0)
            ):
                raise OSError("plugin_worker_process_pin_invalid")
            table = _read_posix_process_table()
            with self._lock:
                root = self._root_record
                observed_root = table.get(self._worker_pid)
                observed = table.get(pid)
                if (
                    root is None
                    or observed_root is None
                    or observed_root.start_time != root.start_time
                    or observed is None
                    or not observed.live
                    or expected_start_time is not None
                    and observed.start_time != expected_start_time
                    or not observed.attached_to(self._worker_pid)
                    or observed.ppid != self._worker_pid
                ):
                    raise OSError("plugin_worker_process_pin_invalid")
                prior = self._tracked.get(pid)
                if prior is not None and prior.start_time != observed.start_time:
                    raise OSError("plugin_worker_process_pin_invalid")
                self._tracked[pid] = observed
                return observed

    def _run(self) -> None:
        monitor_deadline = self._monitor_deadline
        while not self._stop.wait(_POSIX_PROCESS_POLL_SECONDS):
            try:
                self._sample()
            except OSError:
                with self._lock:
                    self._escape_detected = True
                return
            if time.monotonic() >= monitor_deadline:
                return

    def _kill_exact(self, records: Mapping[int, _PosixProcessRecord]) -> None:
        try:
            table = _read_posix_process_table()
        except OSError:
            return
        for pid, expected in sorted(records.items(), reverse=True):
            observed = table.get(pid)
            if (
                observed is None
                or observed.start_time != expected.start_time
                or not observed.live
            ):
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except (PermissionError, ProcessLookupError):
                pass

    @staticmethod
    def _reap_children(maximum: int) -> None:
        for _index in range(maximum):
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except (ChildProcessError, OSError):
                return
            if pid <= 0:
                return

    def finish(self, *, survivors_are_violation: bool) -> tuple[bool, bool]:
        with self._finish_lock:
            return self._finish(survivors_are_violation=survivors_are_violation)

    def _finish(self, *, survivors_are_violation: bool) -> tuple[bool, bool]:
        if self._finished:
            return self._final_violation, self._final_clean
        if not self._started:
            raise RuntimeError("plugin_worker_process_monitor_invalid")
        try:
            initial = self._sample()
        except OSError:
            initial = {}
            with self._lock:
                self._escape_detected = True
        with self._lock:
            violation = self._escape_detected or (
                survivors_are_violation
                and any(record.live for record in initial.values())
            )
        deadline = time.monotonic() + _POSIX_PROCESS_CLEANUP_SECONDS
        current = initial
        while any(record.live for record in current.values()):
            self._kill_exact(current)
            if self._worker_pid == os.getpid():
                self._reap_children(len(current))
            if time.monotonic() >= deadline:
                break
            time.sleep(_POSIX_PROCESS_POLL_SECONDS)
            try:
                current = self._sample()
            except OSError:
                break
        if self._worker_pid == os.getpid():
            self._reap_children(len(current))
        try:
            current = self._sample()
            clean = not any(record.live for record in current.values())
        except OSError:
            clean = False
        self._stop.set()
        self._thread.join(timeout=_POSIX_PROCESS_POLL_SECONDS * 4)
        self._final_violation = violation
        self._final_clean = clean and not self._thread.is_alive()
        self._finished = self._final_clean
        return self._final_violation, self._final_clean


@dataclass(frozen=True, slots=True)
class _ReverifiedLocalMcpExecution:
    verified_package: object
    package_root: Path
    component_identities: tuple[tuple[int, int, int], ...]
    environment: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))


@dataclass(slots=True)
class _SpawnedWorker:
    process: multiprocessing.Process
    job: _WindowsJob | None
    descendant_monitor: _PosixDescendantMonitor | None
    linux_guarded: bool = False
    worker_identity: tuple[int, int] | None = None
    guardian_reap_failure_seen: bool = False


@dataclass(slots=True)
class _RunningWorker:
    authority: PluginExecutionAuthority
    generation: int
    nonce: str
    spawned: _SpawnedWorker
    bootstrap_send: Connection
    command_send: Connection
    result_recv: Connection
    local_execution: _LocalMcpWorkerExecution | None
    lifeline_send: Connection | None = None
    terminal_recorded: bool = False
    reap_failure_recorded: bool = False
    cleanup_pending: bool = False
    cancellation_requested: bool = False
    #: Set once the authenticated result frame decodes as a genuine
    #: completed effect while this generation is still current.  Once set,
    #: a cancellation or a post-authentication join timeout must deliver
    #: the result as `completed` (cleanup-proven) instead of discarding an
    #: already-committed provider effect as `cancelled`/`timeout`.
    result_authenticated: bool = False
    reap_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass(slots=True)
class _ManagedWorkerOperation:
    """Joinable async management state bound to one exact worker generation."""

    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    ready: threading.Event = field(default_factory=threading.Event, repr=False)
    done: threading.Event = field(default_factory=threading.Event, repr=False)
    running: _RunningWorker | None = None
    cancel_requested: bool = False
    result: Mapping[str, Any] | None = None
    error: BaseException | None = None
    thread: threading.Thread | None = None
    fence_token: int | None = None
    delivered: bool = False
    abandoned: bool = False


def _reverify_local_mcp_execution(
    descriptor: object,
    authority: PluginExecutionAuthority,
    execution: _LocalMcpWorkerExecution,
    *,
    plugin_registry: object,
    app_registry: object,
    package_registry: object,
) -> _ReverifiedLocalMcpExecution:
    """Rebuild one local launch solely from current durable signed authority."""

    from mcp_platform.app_registry import McpAppRegistry
    from mcp_platform.app_sources import _validate_local_source_state
    from mcp_platform.artifact_guard import _capture_artifact_identity_chain
    from mcp_platform.client_contracts import (
        McpClientDeployment,
        McpConnectionDescriptor,
        McpTransport,
    )
    from mcp_platform.package_verifier import VerifiedPackage, _is_verifier_issued_package
    from mcp_platform.plugin_connections import (
        _record_requirement,
        _verified_plugin_authority,
        descriptor_for_record,
    )
    from mcp_platform.registry import PackageRegistry
    from plugin_platform.contracts import AppDeployment, PluginAuthenticationKind
    from plugin_platform.package_verifier import VerifiedPlugin
    from plugin_platform.records import InstalledPluginRecord, PluginAppBinding
    from plugin_platform import PluginRegistry

    try:
        if (
            type(descriptor) is not McpConnectionDescriptor
            or type(authority) is not PluginExecutionAuthority
            or type(execution) is not _LocalMcpWorkerExecution
            or type(plugin_registry) is not PluginRegistry
            or type(app_registry) is not McpAppRegistry
            or type(package_registry) is not PackageRegistry
            or Path(plugin_registry.state_root).resolve(strict=True)
            != Path(execution.plugin_registry_root)
            or Path(app_registry.state_root).resolve(strict=True)
            != Path(execution.mcp_state_root)
            or Path(package_registry.state_root).resolve(strict=True)
            != Path(execution.mcp_state_root)
        ):
            raise ValueError

        plugin_snapshot = plugin_registry.snapshot()
        plugin_records = tuple(
            record
            for record in plugin_snapshot.records
            if record.plugin_id == authority.plugin_id
        )
        if (
            plugin_snapshot.revision != execution.plugin_registry_revision
            or len(plugin_records) != 1
        ):
            raise ValueError
        plugin_record = plugin_records[0]
        expected_plugin_root = Path(execution.plugin_package_root)
        selected = next(
            (
                version
                for version in plugin_record.versions
                if version.plugin_version == plugin_record.selected_version
            ),
            None,
        )
        selected_root = (
            Path(plugin_registry.packages_root)
            / authority.plugin_id
            / plugin_record.selected_version
        ).resolve(strict=True)
        candidate_versions = tuple(
            version
            for version in plugin_record.versions
            if version.state == "candidate"
            and version.package_sha256 == authority.package_revision
            and (
                Path(plugin_registry.packages_root)
                / authority.plugin_id
                / version.plugin_version
            ).resolve(strict=True)
            == expected_plugin_root
        )
        candidate_mode = not (
            selected is not None
            and selected.package_sha256 == authority.package_revision
            and selected_root == expected_plugin_root
        )
        if candidate_mode:
            if len(candidate_versions) != 1:
                raise ValueError
            verified_plugin = plugin_registry.reverify_candidate(
                authority.plugin_id,
                candidate_versions[0],
            )
        else:
            installed = plugin_registry.verified_installed_snapshot(
                authority.plugin_id
            )
            if installed.revision != execution.plugin_registry_revision:
                raise ValueError
            verified_plugin = installed.verified
        binding = plugin_record.app_binding
        owned_apps = (
            ()
            if binding is None
            else binding.required_app_ids + binding.optional_app_ids
        )
        if (
            type(plugin_record) is not InstalledPluginRecord
            or type(verified_plugin) is not VerifiedPlugin
            or type(binding) is not PluginAppBinding
            or plugin_record.status != "installed"
            or plugin_record.blocker is not None
            or not plugin_record.enabled
            or not candidate_mode
            and authority.app_id not in owned_apps
            or verified_plugin.package_sha256 != authority.package_revision
            or verified_plugin.manifest.plugin_id != authority.plugin_id
            or verified_plugin.package_root.resolve(strict=True)
            != Path(execution.plugin_package_root)
        ):
            raise ValueError

        requirements = tuple(
            requirement
            for requirement in (
                verified_plugin.manifest.required_apps
                + verified_plugin.manifest.optional_apps
            )
            if requirement.app_id == authority.app_id
        )
        app_snapshot = app_registry.snapshot()
        app_records = tuple(
            record for record in app_snapshot.records if record.app_id == authority.app_id
        )
        if (
            app_snapshot.revision != execution.app_registry_revision
            or len(requirements) != 1
            or len(app_records) != 1
        ):
            raise ValueError
        requirement = requirements[0]
        app_record = app_records[0]
        if (
            requirement != _record_requirement(app_record)
            or requirement.deployment is not AppDeployment.LOCAL
            or requirement.authentication.kind is not PluginAuthenticationKind.NONE
            or not app_record.installed
            or not app_record.trusted
        ):
            raise ValueError

        package_snapshot = package_registry.snapshot()
        package_records = tuple(
            record for record in package_snapshot.records if record.server_id == authority.app_id
        )
        if (
            package_snapshot.revision != execution.package_registry_revision
            or len(package_records) != 1
        ):
            raise ValueError
        package_record = package_records[0]
        package_root = package_registry.package_path(package_record).resolve(strict=True)
        component_identities = _capture_artifact_identity_chain(
            package_root,
            Path(execution.executable_path),
        )
        verified_package = package_registry.reverify_installed(authority.app_id)
        if (
            type(verified_package) is not VerifiedPackage
            or not _is_verifier_issued_package(verified_package)
            or not package_record.enabled
            or package_record.blocker is not None
            or package_root != Path(execution.mcp_package_root)
            or verified_package.manifest_sha256
            != execution.mcp_package_manifest_sha256
            or verified_package.manifest.server_id != authority.app_id
            or verified_package.manifest.publisher_id != requirement.publisher_id
            or verified_package.artifact_path.resolve(strict=True)
            != Path(execution.executable_path)
            or verified_package.manifest.artifact_sha256
            != execution.executable_sha256
            or verified_package.artifact_identity != execution.executable_identity
        ):
            raise ValueError
        _validate_local_source_state(requirement, verified_package)
        if (
            _capture_artifact_identity_chain(
                package_root,
                verified_package.artifact_path,
            )
            != component_identities
        ):
            raise ValueError

        signed_authority = _verified_plugin_authority(
            verified_plugin,
            authority.app_id,
        )
        if signed_authority is None:
            raise ValueError
        expected_descriptor = replace(
            descriptor_for_record(
                app_record,
                signed_capability_authority=signed_authority,
            ),
            verified_launch_sha256=verified_package.manifest.artifact_sha256,
        )
        signed_group = descriptor.signed_capability_authority.get(
            {
                "tool": "tools",
                "prompt": "prompts",
                "resource": "resources",
                "resource_template": "resource_templates",
            }[authority.capability_kind]
        )
        declared_kind = (
            "resource"
            if authority.capability_kind == "resource_template"
            else authority.capability_kind
        )
        if (
            descriptor != expected_descriptor
            or descriptor.deployment is not McpClientDeployment.LOCAL
            or descriptor.transport is not McpTransport.STDIO
            or descriptor.app_id != authority.app_id
            or descriptor.capability_revision != authority.capability_revision
            or not isinstance(signed_group, Mapping)
            or authority.capability_id not in signed_group
            or f"{declared_kind}:{authority.capability_id}"
            not in descriptor.declared_capabilities
        ):
            raise ValueError

        # The authenticated parent seals the exact durable connection and
        # authority revisions after validating the current grant/credentialless
        # record.  Those revisions intentionally include account, policy, and
        # capability-binding state and therefore are not descriptor digests.
        # The child independently reverifies every signed package/descriptor/
        # capability input above while retaining the parent's exact revisions
        # as the generation fence returned with the result.

        if (
            plugin_registry.snapshot().revision != execution.plugin_registry_revision
            or app_registry.snapshot().revision != execution.app_registry_revision
            or package_registry.snapshot().revision != execution.package_registry_revision
        ):
            raise ValueError
    except (AttributeError, KeyError, LookupError, OSError, RuntimeError, TypeError, ValueError):
        raise ValueError("plugin_worker_local_authority_invalid") from None

    return _ReverifiedLocalMcpExecution(
        verified_package=verified_package,
        package_root=package_root,
        component_identities=component_identities,
        environment={
            "OMEGA_MCP_SERVER_ID": authority.app_id,
            "OMEGA_MCP_PACKAGE_ROOT": str(package_root),
        },
    )


_LOG = logging.getLogger("omega.mcp.plugin.worker")


def _invalid_protocol() -> PluginExecutionWorkerError:
    return PluginExecutionWorkerError("worker_protocol_invalid")


# A diagnosis is built from validated parts, never from free text: a type name,
# one or two source sites, and -- when the exception's whole message is a member
# of one of Omega's own closed enums -- that reason. Enforced on the way out AND
# on the way in: the child is the process that touches provider bytes, so what
# it says is validated, never trusted. A diagnosis that does not match is
# dropped, and dropping it can never fail an operation -- it explains a failure
# that already happened.
_WORKER_NAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_WORKER_SITE_RE = re.compile(r"\A[A-Za-z0-9_.\-]{1,64}:\d{1,7} in [A-Za-z0-9_<>.]{1,64}\Z")
_WORKER_REASON_RE = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")
_WORKER_DIAGNOSIS_RE = re.compile(
    r"\A[A-Za-z_][A-Za-z0-9_]{0,63} at [A-Za-z0-9_.\-]{1,64}:\d{1,7} in [A-Za-z0-9_<>.]{1,64}"
    r"(?: <- [A-Za-z0-9_.\-]{1,64}:\d{1,7} in [A-Za-z0-9_<>.]{1,64})?"
    r"(?: reason=[a-z][a-z0-9_]{0,63})?\Z"
)


def _worker_closed_reasons() -> frozenset[str]:
    """Omega's own closed failure vocabulary, or nothing if unavailable.

    Imported lazily: this module is the jailed child's entry point and keeps
    its import surface deliberately small. A missing import costs the reason,
    never the diagnosis.
    """

    try:
        from mcp_platform.client_contracts import McpClientErrorReason

        return frozenset(member.value for member in McpClientErrorReason)
    except Exception:  # noqa: BLE001 - the reason is optional detail
        return frozenset()


def _worker_failure_site(frame: Any) -> str | None:
    try:
        code = frame.tb_frame.f_code
        site = f"{os.path.basename(code.co_filename)}:{frame.tb_lineno} in {code.co_name}"
    except Exception:  # noqa: BLE001
        return None
    return site if _WORKER_SITE_RE.match(site) else None


def _worker_failure_diagnosis(error: BaseException) -> str | None:
    """Name the exception type, where it was raised, and why -- in Omega's words.

    The innermost traceback frame is the `raise` itself, which for a shared
    helper such as ``capabilities._fail`` is the same line for every distinct
    cause; its CALLER is the check that actually failed, so both are named.

    ``str(error)`` is included only when the whole message is a member of one
    of Omega's closed enums. That is the one part a provider could otherwise
    have authored, so anything outside those sets is dropped, never printed.
    """

    try:
        frames = []
        traceback = error.__traceback__
        while traceback is not None:
            frames.append(traceback)
            traceback = traceback.tb_next
        if not frames:
            return None
        name = type(error).__name__
        if not _WORKER_NAME_RE.match(name):
            return None
        site = _worker_failure_site(frames[-1])
        if site is None:
            return None
        diagnosis = f"{name} at {site}"
        if len(frames) > 1:
            caller = _worker_failure_site(frames[-2])
            if caller is not None:
                diagnosis = f"{diagnosis} <- {caller}"
        message = str(error)
        if _WORKER_REASON_RE.match(message) and message in _worker_closed_reasons():
            diagnosis = f"{diagnosis} reason={message}"
    except Exception:  # noqa: BLE001 - a failure here must not mask the failure
        return None
    return diagnosis if _WORKER_DIAGNOSIS_RE.match(diagnosis) else None



def _classify_result_body(
    result_body: object,
) -> tuple[dict[str, Any] | None, str | None]:
    """Classify one authenticated result body without any side effect.

    Returns ``(data, failure_reason)`` where exactly one is non-``None`` for
    a well-formed body; a malformed body raises the same protocol violation
    as before.  Being pure, this is safe to run immediately once the result
    frame authenticates -- before the child is joined -- so a cancellation
    or deadline expiry racing that join can tell a genuine completed
    provider effect from a failure without re-deriving it later.
    """

    if type(result_body) is not dict:
        raise _invalid_protocol()
    if result_body.get("status") == "completed" and set(result_body) == {
        "status",
        "data",
    }:
        data = result_body["data"]
        if type(data) is not dict:
            raise _invalid_protocol()
        return data, None
    if set(result_body) - {"diagnosis"} != {"status", "reason"} or (
        result_body.get("status") != "failed"
    ):
        raise _invalid_protocol()
    failure_reason = result_body.get("reason")
    if failure_reason not in {
        "plugin_operation_failed",
        "plugin_local_process_model_incompatible",
        "plugin_worker_quarantine_unavailable",
    }:
        raise _invalid_protocol()
    return None, failure_reason


def _frame_document(
    *,
    kind: str,
    nonce: str,
    generation: int,
    sequence: int,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        type(kind) is not str
        or not kind
        or type(nonce) is not str
        or len(nonce) != 64
        or any(character not in _HEX for character in nonce)
        or type(generation) is not int
        or not 1 <= generation <= _MAX_GENERATION
        or type(sequence) is not int
        or sequence <= 0
        or not isinstance(body, Mapping)
    ):
        raise _invalid_protocol()
    return {
        "v": _FRAME_VERSION,
        "kind": kind,
        "nonce": nonce,
        "generation": generation,
        "seq": sequence,
        "body": _plain_json(body),
    }


def _encode_authenticated_frame(
    *,
    secret: bytes,
    kind: str,
    nonce: str,
    generation: int,
    sequence: int,
    body: Mapping[str, Any],
    max_frame_bytes: int,
) -> bytes:
    try:
        if type(secret) is not bytes or len(secret) != 32:
            raise ValueError
        if type(max_frame_bytes) is not int or max_frame_bytes <= 0:
            raise ValueError
        unsigned = _frame_document(
            kind=kind,
            nonce=nonce,
            generation=generation,
            sequence=sequence,
            body=body,
        )
        mac = hmac.new(secret, canonical_bytes(unsigned), hashlib.sha256).hexdigest()
        payload = canonical_bytes({**unsigned, "mac": mac})
        if not payload or len(payload) > max_frame_bytes:
            raise ValueError
        return struct.pack(">I", len(payload)) + payload
    except (PluginExecutionWorkerError, TypeError, ValueError):
        raise _invalid_protocol() from None


def _decode_authenticated_frame(
    frame: bytes,
    *,
    secret: bytes,
    expected_kind: str | tuple[str, ...],
    expected_nonce: str | None,
    expected_generation: int | None,
    expected_sequence: int,
    max_frame_bytes: int,
) -> dict[str, Any]:
    try:
        if type(expected_kind) is str:
            expected_kinds = (expected_kind,)
        elif (
            type(expected_kind) is tuple
            and expected_kind
            and all(type(kind) is str and kind for kind in expected_kind)
        ):
            expected_kinds = expected_kind
        else:
            raise ValueError
        if (
            type(frame) is not bytes
            or len(frame) < 5
            or type(secret) is not bytes
            or len(secret) != 32
            or type(max_frame_bytes) is not int
            or max_frame_bytes <= 0
        ):
            raise ValueError
        declared = struct.unpack(">I", frame[:4])[0]
        payload = frame[4:]
        if declared != len(payload) or not payload or declared > max_frame_bytes:
            raise ValueError

        def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
            decoded: dict[str, object] = {}
            for key, value in pairs:
                if key in decoded:
                    raise ValueError
                decoded[key] = value
            return decoded

        document = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicates)
        if type(document) is not dict or set(document) != {
            "v",
            "kind",
            "nonce",
            "generation",
            "seq",
            "body",
            "mac",
        }:
            raise ValueError
        if canonical_bytes(document) != payload:
            raise ValueError
        mac = document.pop("mac")
        if type(mac) is not str or len(mac) != 64 or any(character not in _HEX for character in mac):
            raise ValueError
        expected_mac = hmac.new(secret, canonical_bytes(document), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected_mac):
            raise ValueError
        validated = _frame_document(
            kind=document["kind"],
            nonce=document["nonce"],
            generation=document["generation"],
            sequence=document["seq"],
            body=document["body"],
        )
        if (
            document["v"] != _FRAME_VERSION
            or validated != document
            or document["kind"] not in expected_kinds
            or expected_nonce is not None
            and document["nonce"] != expected_nonce
            or expected_generation is not None
            and document["generation"] != expected_generation
            or document["seq"] != expected_sequence
        ):
            raise ValueError
        return validated
    except (KeyError, TypeError, UnicodeError, ValueError, json.JSONDecodeError):
        raise _invalid_protocol() from None


def _send_frame(connection: Connection, frame: bytes) -> None:
    try:
        connection.send_bytes(frame)
    except (BrokenPipeError, EOFError, OSError):
        raise _invalid_protocol() from None


def _receive_frame(connection: Connection, *, timeout_seconds: float, max_bytes: int) -> bytes:
    try:
        if not connection.poll(max(0.0, timeout_seconds)):
            raise TimeoutError
        return connection.recv_bytes(maxlength=max_bytes + 4)
    except TimeoutError:
        raise
    except (EOFError, OSError):
        raise PluginExecutionWorkerError("plugin_worker_unavailable") from None


class _ProviderPinProtocol:
    """Authenticate one exact provider identity before its pre-exec gate opens."""

    def __init__(
        self,
        *,
        secret: bytes,
        nonce: str,
        generation: int,
        command_recv: Connection,
        result_send: Connection,
        max_frame_bytes: int,
        monitor: _PosixDescendantMonitor,
    ) -> None:
        self._secret = secret
        self._nonce = nonce
        self._generation = generation
        self._command_recv = command_recv
        self._result_send = result_send
        self._max_frame_bytes = max_frame_bytes
        self._monitor = monitor
        self._used = False
        self._notice_sent = False

    @property
    def used(self) -> bool:
        return self._used

    def pin(self, pid: int) -> None:
        if self._used or self._notice_sent:
            raise _invalid_protocol()
        record = self._monitor.pin(pid)
        identity = {"pid": record.pid, "start_time": record.start_time}
        self._notice_sent = True
        _send_frame(
            self._result_send,
            _encode_authenticated_frame(
                secret=self._secret,
                kind="provider_started",
                nonce=self._nonce,
                generation=self._generation,
                sequence=5,
                body=identity,
                max_frame_bytes=self._max_frame_bytes,
            ),
        )
        release = _decode_authenticated_frame(
            self._command_recv.recv_bytes(maxlength=self._max_frame_bytes + 4),
            secret=self._secret,
            expected_kind="provider_release",
            expected_nonce=self._nonce,
            expected_generation=self._generation,
            expected_sequence=6,
            max_frame_bytes=self._max_frame_bytes,
        )
        if release["body"] != identity:
            raise _invalid_protocol()
        self._monitor.pin(pid, expected_start_time=record.start_time)
        self._used = True


_PROVIDER_PIN_CONTEXT = threading.local()


def _pin_worker_provider_process(pid: int) -> None:
    protocol = getattr(_PROVIDER_PIN_CONTEXT, "protocol", None)
    if type(protocol) is not _ProviderPinProtocol:
        raise PluginExecutionWorkerError("plugin_local_process_model_incompatible")
    protocol.pin(pid)


def _current_worker_provider_pin() -> Callable[[int], None]:
    protocol = getattr(_PROVIDER_PIN_CONTEXT, "protocol", None)
    if type(protocol) is not _ProviderPinProtocol:
        raise PluginExecutionWorkerError("plugin_local_process_model_incompatible")
    return _pin_worker_provider_process


class _ParentDeathGuardian:
    """Terminate a blocked worker when its authenticated parent endpoint dies."""

    def __init__(self, lifeline_recv: Connection, cleanup: Callable[[], None]) -> None:
        if not callable(cleanup):
            raise ValueError("plugin_worker_guardian_invalid")
        self._lifeline_recv = lifeline_recv
        self._cleanup = cleanup
        self._thread = threading.Thread(
            target=self._run,
            name=f"omega-plugin-parent-guardian:{os.getpid()}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        try:
            # The only valid post-authentication state is an open, silent pipe.
            # EOF proves every copy of the owning parent's send endpoint closed.
            self._lifeline_recv.recv_bytes(maxlength=1)
        except (EOFError, OSError):
            pass
        try:
            self._cleanup()
        finally:
            os._exit(72)


def _child_process_main(
    bootstrap_recv: Connection,
    command_recv: Connection,
    lifeline_recv: Connection,
    result_send: Connection,
    max_frame_bytes: int,
    child_handler: Callable[[dict[str, Any], dict[str, Any]], Mapping[str, Any]],
    linux_guarded: bool = False,
) -> None:
    posix_process_model = True
    if os.name != "nt":
        try:
            os.setsid()
        except OSError:
            os._exit(70)
        posix_process_model = _prepare_posix_process_model()
    try:
        secret = bootstrap_recv.recv_bytes(maxlength=32)
        if type(secret) is not bytes or len(secret) != 32:
            raise _invalid_protocol()
        hello = _decode_authenticated_frame(
            command_recv.recv_bytes(maxlength=max_frame_bytes + 4),
            secret=secret,
            expected_kind="hello",
            expected_nonce=None,
            expected_generation=None,
            expected_sequence=1,
            max_frame_bytes=max_frame_bytes,
        )
        nonce = hello["nonce"]
        generation = hello["generation"]
        ready_body: dict[str, int] = {}
        if linux_guarded:
            if sys.platform != "linux" or os.name == "nt":
                raise _invalid_protocol()
            own_record = _read_posix_process_table().get(os.getpid())
            if own_record is None or not own_record.live:
                raise _invalid_protocol()
            ready_body = {
                "pid": own_record.pid,
                "start_time": own_record.start_time,
            }
        _send_frame(
            result_send,
            _encode_authenticated_frame(
                secret=secret,
                kind="ready",
                nonce=nonce,
                generation=generation,
                sequence=2,
                body=ready_body,
                max_frame_bytes=max_frame_bytes,
            ),
        )
        guardian_frame = _decode_authenticated_frame(
            lifeline_recv.recv_bytes(maxlength=max_frame_bytes + 4),
            secret=secret,
            expected_kind="guardian",
            expected_nonce=nonce,
            expected_generation=generation,
            expected_sequence=3,
            max_frame_bytes=max_frame_bytes,
        )
        if guardian_frame["body"] != {}:
            raise _invalid_protocol()
        execute = _decode_authenticated_frame(
            command_recv.recv_bytes(maxlength=max_frame_bytes + 4),
            secret=secret,
            expected_kind="execute",
            expected_nonce=nonce,
            expected_generation=generation,
            expected_sequence=4,
            max_frame_bytes=max_frame_bytes,
        )
        payload = execute["body"]
        if type(payload) is not dict or set(payload) != {
            "authority",
            "body",
            "credential",
            "deadline_monotonic",
        }:
            raise _invalid_protocol()
        authority = PluginExecutionAuthority.from_dict(payload["authority"])
        if (
            type(payload["body"]) is not dict
            or type(payload["credential"]) is not dict
            or type(payload["deadline_monotonic"]) not in {int, float}
            or not math.isfinite(payload["deadline_monotonic"])
            or payload["deadline_monotonic"] <= 0
            or not callable(child_handler)
        ):
            raise _invalid_protocol()
        worker_body = dict(payload["body"])
        if "worker_authority" in worker_body:
            raise _invalid_protocol()
        local_execution = (
            None
            if "local_execution" not in worker_body
            else _LocalMcpWorkerExecution.from_dict(worker_body["local_execution"])
        )
        monitor: _PosixDescendantMonitor | None = None
        provider_protocol: _ProviderPinProtocol | None = None
        failure_reason: str | None = None
        failure_diagnosis: str | None = None
        data: Any = None
        try:
            if local_execution is not None and os.name != "nt":
                if not posix_process_model:
                    _quarantine_local_execution(authority, local_execution)
                    raise PluginExecutionWorkerError(
                        "plugin_local_process_model_incompatible"
                    )
                monitor = _PosixDescendantMonitor(
                    os.getpid(),
                    monitor_deadline=float(payload["deadline_monotonic"]),
                )
                monitor.start()
                provider_protocol = _ProviderPinProtocol(
                    secret=secret,
                    nonce=nonce,
                    generation=generation,
                    command_recv=command_recv,
                    result_send=result_send,
                    max_frame_bytes=max_frame_bytes,
                    monitor=monitor,
                )
            cleanup_lock = threading.Lock()

            def parent_died() -> None:
                with cleanup_lock:
                    if monitor is not None:
                        violation, clean = monitor.finish(
                            survivors_are_violation=False
                        )
                        if violation or not clean:
                            assert local_execution is not None
                            try:
                                _quarantine_local_execution(
                                    authority,
                                    local_execution,
                                )
                            except PluginExecutionWorkerError:
                                pass
                    if os.name != "nt":
                        try:
                            os.killpg(os.getpid(), signal.SIGKILL)
                        except (OSError, ProcessLookupError):
                            pass

            guardian = _ParentDeathGuardian(lifeline_recv, parent_died)
            guardian.start()
            worker_body["worker_authority"] = dict(payload["authority"])
            if provider_protocol is not None:
                if hasattr(_PROVIDER_PIN_CONTEXT, "protocol"):
                    raise _invalid_protocol()
                _PROVIDER_PIN_CONTEXT.protocol = provider_protocol
            try:
                raw = child_handler(worker_body, dict(payload["credential"]))
            finally:
                if provider_protocol is not None:
                    try:
                        del _PROVIDER_PIN_CONTEXT.protocol
                    except AttributeError:
                        pass
            if not isinstance(raw, Mapping):
                raise ValueError
            data = _plain_json(raw)
        except PluginExecutionWorkerError as exc:
            failure_reason = str(exc)
            failure_diagnosis = _worker_failure_diagnosis(exc)
        except Exception as exc:  # noqa: BLE001 - reported as a reason, never raised
            failure_reason = "plugin_operation_failed"
            # One literal stood for every cause in here and the exception died
            # with the process. The type and the line that raised it are
            # Omega's own words and are safe to carry out.
            failure_diagnosis = _worker_failure_diagnosis(exc)
        finally:
            if monitor is not None:
                violation, clean = monitor.finish(survivors_are_violation=True)
                if violation or not clean:
                    try:
                        assert local_execution is not None
                        _quarantine_local_execution(authority, local_execution)
                        failure_reason = "plugin_local_process_model_incompatible"
                    except PluginExecutionWorkerError:
                        failure_reason = "plugin_worker_quarantine_unavailable"
        body = (
            {"status": "completed", "data": data}
            if failure_reason is None
            else (
                {"status": "failed", "reason": failure_reason}
                | ({} if failure_diagnosis is None else {"diagnosis": failure_diagnosis})
            )
        )
        _send_frame(
            result_send,
            _encode_authenticated_frame(
                secret=secret,
                kind="result",
                nonce=nonce,
                generation=generation,
                sequence=(
                    7
                    if provider_protocol is not None and provider_protocol.used
                    else 5
                ),
                body=body,
                max_frame_bytes=max_frame_bytes,
            ),
        )
    except BaseException:
        os._exit(71)
    finally:
        for connection in (bootstrap_recv, command_recv, result_send):
            try:
                connection.close()
            except BaseException:
                pass


def _finish_linux_guardian_monitor(
    monitor: _PosixDescendantMonitor,
) -> tuple[bool, bool]:
    """Make a fixed number of bounded cleanup attempts before parent retry."""

    violation = False
    clean = False
    for attempt in range(_LINUX_GUARDIAN_INLINE_CLEANUP_ATTEMPTS):
        if attempt:
            time.sleep(_POSIX_PROCESS_POLL_SECONDS)
        try:
            observed_violation, clean = monitor.finish(
                survivors_are_violation=True,
            )
        except BaseException:
            observed_violation = True
            clean = False
        violation = violation or observed_violation
        if clean:
            break
    return violation, clean


def _await_linux_guardian_cleanup_retry(
    monitor: _PosixDescendantMonitor,
    *,
    exit_code: int,
) -> None:
    """Remain dormant until the parent requests another exact cleanup proof."""

    resolved_exit = exit_code

    def retry_cleanup(_signum: int, _frame: object) -> None:
        nonlocal resolved_exit
        violation, clean = _finish_linux_guardian_monitor(monitor)
        if (
            violation
            and resolved_exit
            not in {
                _LINUX_GUARDIAN_REAP_FAILED_EXIT,
                _LINUX_GUARDIAN_START_FAILED_EXIT,
            }
        ):
            resolved_exit = _LINUX_GUARDIAN_VIOLATION_EXIT
        if clean:
            os._exit(resolved_exit)

    signal.signal(_LINUX_GUARDIAN_RETRY_SIGNAL, retry_cleanup)
    threading.Event().wait()
    os._exit(_LINUX_GUARDIAN_REAP_FAILED_EXIT)


def _linux_guardian_process_main(
    bootstrap_recv: Connection,
    command_recv: Connection,
    lifeline_recv: Connection,
    result_send: Connection,
    max_frame_bytes: int,
    child_handler: Callable[[dict[str, Any], dict[str, Any]], Mapping[str, Any]],
    monitor_deadline: float,
) -> None:
    """Own one Linux worker as a living subreaper until its whole tree is clean."""

    connections = (bootstrap_recv, command_recv, lifeline_recv, result_send)
    monitor: _PosixDescendantMonitor | None = None
    try:
        if (
            sys.platform != "linux"
            or os.name == "nt"
            or type(monitor_deadline) not in {int, float}
            or not math.isfinite(monitor_deadline)
            or monitor_deadline <= time.monotonic()
        ):
            raise OSError
        signal.signal(_LINUX_GUARDIAN_RETRY_SIGNAL, signal.SIG_IGN)
        os.setsid()
        if not _enable_posix_subreaper():
            raise OSError
        from mcp_platform.transports.stdio import _fork_attached_worker

        worker_process = _fork_attached_worker()
        if worker_process is None:
            try:
                if not _set_linux_parent_death_signal(os.getppid()):
                    os._exit(_LINUX_GUARDIAN_START_FAILED_EXIT)
                _child_process_main(
                    bootstrap_recv,
                    command_recv,
                    lifeline_recv,
                    result_send,
                    max_frame_bytes,
                    child_handler,
                    True,
                )
            except BaseException:
                os._exit(71)
            os._exit(0)
        worker_pid = worker_process.pid
        if worker_pid <= 0:
            raise OSError
        for connection in connections:
            connection.close()
        monitor = _PosixDescendantMonitor(
            os.getpid(),
            monitor_deadline=monitor_deadline,
            enforce_attachment=False,
        )
        monitor.start()
        worker_status: int | None = None
        worker_reap_failed = False
        wait_attempts = max(
            1,
            math.ceil(
                max(0.0, monitor_deadline - time.monotonic())
                / _POSIX_PROCESS_POLL_SECONDS
            ),
        )
        for _attempt in range(wait_attempts):
            if time.monotonic() >= monitor_deadline:
                break
            try:
                observed_status = worker_process.poll_wait_status()
            except InterruptedError:
                continue
            except ChildProcessError:
                worker_reap_failed = True
                break
            if observed_status is not None:
                worker_status = observed_status
                break
            time.sleep(_POSIX_PROCESS_POLL_SECONDS)
        violation, clean = _finish_linux_guardian_monitor(monitor)
        if worker_status is None:
            violation = True
            try:
                observed_status = worker_process.poll_wait_status()
            except ChildProcessError:
                observed_status = None
            if observed_status is not None:
                worker_status = observed_status
        if worker_status is not None and not os.waitstatus_to_exitcode(worker_status) == 0:
            violation = True
        exit_code = (
            _LINUX_GUARDIAN_REAP_FAILED_EXIT
            if worker_reap_failed
            else (
            _LINUX_GUARDIAN_VIOLATION_EXIT
            if violation
            else _LINUX_GUARDIAN_CLEAN_EXIT
            )
        )
        if not clean:
            _await_linux_guardian_cleanup_retry(monitor, exit_code=exit_code)
        os._exit(exit_code)
    except BaseException:
        clean = False
        if monitor is not None:
            _violation, clean = _finish_linux_guardian_monitor(monitor)
        for connection in connections:
            try:
                connection.close()
            except BaseException:
                pass
        if monitor is not None and not clean:
            _await_linux_guardian_cleanup_retry(
                monitor,
                exit_code=_LINUX_GUARDIAN_START_FAILED_EXIT,
            )
        os._exit(_LINUX_GUARDIAN_START_FAILED_EXIT)


class _WindowsJob:
    """Kill-on-close Job Object assigned before the authenticated start gate opens."""

    def __init__(self, handle: int) -> None:
        self._handle = handle
        self._closed = False

    @classmethod
    def create(cls) -> _WindowsJob | None:
        if os.name != "nt":
            return None
        import ctypes
        from ctypes import wintypes

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
        information = _ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            handle,
            9,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error, "SetInformationJobObject")
        return cls(int(handle))

    def assign(self, pid: int) -> None:
        if os.name != "nt" or self._closed:
            raise OSError("job_unavailable")
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        process = kernel32.OpenProcess(0x0001 | 0x0100 | 0x0400, False, pid)
        if not process:
            raise OSError(ctypes.get_last_error(), "OpenProcess")
        try:
            if not kernel32.AssignProcessToJobObject(self._handle, process):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject")
        finally:
            kernel32.CloseHandle(process)

    def close(self) -> None:
        if self._closed or os.name != "nt":
            return
        import ctypes

        self._closed = True
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self._handle)


class PluginExecutionWorker:
    """Own one-shot worker processes and fence every result by generation."""

    def __init__(
        self,
        *,
        child_handler: Callable[[dict[str, Any], dict[str, Any]], Mapping[str, Any]],
        shutdown_timeout_seconds: float,
        max_frame_bytes: int = _DEFAULT_MAX_FRAME_BYTES,
        reap_pending_callback: Callable[[], None] | None = None,
    ) -> None:
        if (
            not callable(child_handler)
            or type(shutdown_timeout_seconds) not in {int, float}
            or shutdown_timeout_seconds <= 0
            or type(max_frame_bytes) is not int
            or max_frame_bytes <= _FRAME_OVERHEAD_BYTES
            or (
                reap_pending_callback is not None
                and not callable(reap_pending_callback)
            )
        ):
            raise ValueError("plugin_worker_configuration_invalid")
        self._child_handler = child_handler
        self._shutdown_timeout_seconds = float(shutdown_timeout_seconds)
        self._max_frame_bytes = max_frame_bytes
        self._reap_pending_callback = reap_pending_callback
        self._context = multiprocessing.get_context("spawn")
        self._lock = threading.RLock()
        self._starting_condition = threading.Condition(self._lock)
        self._generations: dict[tuple[str, str, str], int] = {}
        self._active: dict[tuple[str, str, str], _RunningWorker] = {}
        self._starting_operations: set[
            tuple[tuple[str, str, str], int]
        ] = set()
        # ``_active`` is the exact per-key owner.  ``_tracked`` retains every
        # spawned generation until a terminal outcome proves its tree reaped,
        # so no generation can be detached from admission, fencing, or close.
        self._tracked: dict[int, _RunningWorker] = {}
        # Reservations whose cancellation could not obtain an exact killable
        # handle inside the bound.  They fence globally until their manager
        # thread finishes and proves the reservation retired.
        self._unproven_starts: dict[int, threading.Thread] = {}
        self._next_fence_token = 0
        self._outcomes: list[PluginExecutionTerminalOutcome] = []
        self._management_threads: set[threading.Thread] = set()
        self._closed = False

    @property
    def terminal_outcomes(self) -> tuple[PluginExecutionTerminalOutcome, ...]:
        with self._lock:
            return tuple(self._outcomes)

    @property
    def active_process_ids(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(
                sorted(
                    running.spawned.process.pid
                    for running in self._known_running_locked()
                    if running.spawned.process.pid is not None
                    and running.spawned.process.is_alive()
                )
            )

    @property
    def active_management_thread_count(self) -> int:
        with self._lock:
            return sum(thread.is_alive() for thread in self._management_threads)

    def _next_generation(self, key: tuple[str, str, str]) -> int:
        generation = self._generations.get(key, 0) + 1
        if generation > _MAX_GENERATION:
            raise PluginExecutionWorkerError("plugin_worker_unavailable")
        self._generations[key] = generation
        return generation

    def _invalidate_generation(self, running: _RunningWorker) -> None:
        key = running.authority.execution_key
        if self._generations.get(key) == running.generation:
            self._next_generation(key)

    def _known_running_locked(self) -> tuple[_RunningWorker, ...]:
        """Return every generation this worker is still accountable for."""

        known: dict[int, _RunningWorker] = dict(self._tracked)
        for running in self._active.values():
            known.setdefault(id(running), running)
        return tuple(known.values())

    def _prune_unproven_starts_locked(self) -> None:
        for token, thread in tuple(self._unproven_starts.items()):
            # A finished manager either never published a generation or left
            # one tracked, so its reservation is no longer unaccounted for.
            if not thread.is_alive():
                del self._unproven_starts[token]

    def _cleanup_fence_locked(self) -> bool:
        """Report whether any spawned generation remains cleanup-unproven."""

        self._prune_unproven_starts_locked()
        return bool(self._unproven_starts) or any(
            running.cleanup_pending or running.reap_failure_recorded
            for running in self._known_running_locked()
        )

    def _register_unproven_start(self, thread: threading.Thread) -> int:
        with self._lock:
            self._next_fence_token += 1
            token = self._next_fence_token
            self._unproven_starts[token] = thread
        if self._reap_pending_callback is not None:
            self._reap_pending_callback()
        return token

    def _retire_unproven_start(self, token: int) -> None:
        with self._lock:
            self._unproven_starts.pop(token, None)

    def _spawn(
        self,
        bootstrap_recv: Connection,
        command_recv: Connection,
        lifeline_recv: Connection,
        result_send: Connection,
        *,
        local_contained: bool,
        monitor_deadline: float,
    ) -> _SpawnedWorker:
        """Create the child and return an exact killable handle immediately.

        Post-start containment is deliberately excluded here.  Job assignment
        and monitor startup can fail or stall, and no such step may run before
        the generation it belongs to is published as killable and tracked.
        """

        job = _WindowsJob.create()
        linux_guarded = (
            job is None
            and local_contained
            and os.name != "nt"
            and sys.platform == "linux"
        )
        process = self._context.Process(
            target=(
                _linux_guardian_process_main if linux_guarded else _child_process_main
            ),
            args=(
                bootstrap_recv,
                command_recv,
                lifeline_recv,
                result_send,
                self._max_frame_bytes,
                self._child_handler,
                *((monitor_deadline,) if linux_guarded else ()),
            ),
            name=(
                "omega-plugin-linux-guardian"
                if linux_guarded
                else "omega-plugin-execution-worker"
            ),
        )
        try:
            process.start()
        except BaseException:
            # No child exists yet, so nothing can be left untracked.
            if job is not None:
                job.close()
            raise
        return _SpawnedWorker(
            process,
            job,
            None,
            linux_guarded=linux_guarded,
        )

    def _attach_containment(
        self,
        spawned: _SpawnedWorker,
        *,
        local_contained: bool,
        monitor_deadline: float,
    ) -> None:
        """Complete post-start containment for an already published generation.

        Any failure here propagates to the caller, which owns the exact
        killable generation and fails it closed rather than abandoning a live
        child that global admission and close cannot see.
        """

        pid = spawned.process.pid
        if pid is None:
            raise OSError("plugin_worker_process_identity_unavailable")
        if spawned.job is not None:
            spawned.job.assign(int(pid))
        elif local_contained and not spawned.linux_guarded:
            descendant_monitor = _PosixDescendantMonitor(
                int(pid),
                monitor_deadline=monitor_deadline,
            )
            descendant_monitor.start()
            spawned.descendant_monitor = descendant_monitor

    def _record_terminal(
        self,
        running: _RunningWorker,
        status: str,
        *,
        cleanup_proven: bool = False,
    ) -> None:
        with self._lock:
            if running.terminal_recorded:
                return
            if running.cleanup_pending and not cleanup_proven:
                # Cancellation/close may begin after the management thread's
                # final generation check.  Only the path that proves the exact
                # tree reaped may retire that fenced generation afterward.
                return
            if cleanup_proven:
                running.cleanup_pending = False
            running.terminal_recorded = True
            self._tracked.pop(id(running), None)
            if self._active.get(running.authority.execution_key) is running:
                self._active.pop(running.authority.execution_key, None)
            self._outcomes.append(
                PluginExecutionTerminalOutcome(
                    running.authority,
                    running.generation,
                    status,
                )
            )

    def _record_reap_failure(self, running: _RunningWorker) -> None:
        notify = False
        with self._lock:
            if running.terminal_recorded or running.reap_failure_recorded:
                return
            running.cleanup_pending = True
            running.reap_failure_recorded = True
            self._outcomes.append(
                PluginExecutionTerminalOutcome(
                    running.authority,
                    running.generation,
                    "reap_failed",
                )
            )
            notify = True
        if notify and self._reap_pending_callback is not None:
            self._reap_pending_callback()

    @staticmethod
    def _close_connections(*connections: Connection | None) -> None:
        for connection in connections:
            if connection is None:
                continue
            try:
                connection.close()
            except BaseException:
                pass

    @classmethod
    def _close_pipes(cls, running: _RunningWorker) -> None:
        cls._close_connections(
            running.bootstrap_send,
            running.command_send,
            running.result_recv,
            running.lifeline_send,
        )

    def _kill_and_reap(
        self,
        running: _RunningWorker,
        *,
        survivors_are_violation: bool = False,
    ) -> tuple[bool, bool]:
        with running.reap_lock:
            return self._kill_and_reap_locked(
                running,
                survivors_are_violation=survivors_are_violation,
            )

    def _kill_and_reap_fail_closed(
        self,
        running: _RunningWorker,
        *,
        survivors_are_violation: bool = False,
    ) -> tuple[bool, bool]:
        """Convert cleanup faults into the same durable reap-pending state."""

        try:
            return self._kill_and_reap(
                running,
                survivors_are_violation=survivors_are_violation,
            )
        except BaseException:
            self._record_reap_failure(running)
            return False, False

    def _kill_and_reap_locked(
        self,
        running: _RunningWorker,
        *,
        survivors_are_violation: bool,
    ) -> tuple[bool, bool]:
        process = running.spawned.process
        job = running.spawned.job
        descendant_monitor = running.spawned.descendant_monitor
        process_violation = False
        descendants_clean = True
        self._close_pipes(running)
        try:
            if descendant_monitor is not None:
                stop_pid = (
                    running.spawned.worker_identity[0]
                    if running.spawned.linux_guarded
                    and running.spawned.worker_identity is not None
                    else process.pid
                )
                if stop_pid is not None:
                    try:
                        if running.spawned.linux_guarded:
                            identity = running.spawned.worker_identity
                            assert identity is not None
                            observed = _read_posix_process_table().get(identity[0])
                            if (
                                observed is not None
                                and observed.live
                                and observed.start_time == identity[1]
                            ):
                                os.kill(identity[0], signal.SIGSTOP)
                        elif process.is_alive():
                            os.kill(int(stop_pid), signal.SIGSTOP)
                    except (OSError, ProcessLookupError):
                        pass
                process_violation, descendants_clean = descendant_monitor.finish(
                    survivors_are_violation=survivors_are_violation
                )
            if job is not None:
                job.close()
            elif running.spawned.linux_guarded:
                identity = running.spawned.worker_identity
                if identity is not None:
                    try:
                        observed = _read_posix_process_table().get(identity[0])
                        if (
                            observed is not None
                            and observed.live
                            and observed.start_time == identity[1]
                        ):
                            os.kill(identity[0], signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        descendants_clean = False
                if (
                    running.spawned.guardian_reap_failure_seen
                    and process.is_alive()
                    and process.pid is not None
                ):
                    try:
                        os.kill(process.pid, _LINUX_GUARDIAN_RETRY_SIGNAL)
                    except (OSError, ProcessLookupError):
                        descendants_clean = False
                process.join(
                    self._shutdown_timeout_seconds + _POSIX_PROCESS_CLEANUP_SECONDS
                )
                if process.is_alive():
                    running.spawned.guardian_reap_failure_seen = True
                    return False, process_violation
                guardian_exit = process.exitcode
                if guardian_exit == _LINUX_GUARDIAN_VIOLATION_EXIT:
                    process_violation = True
                elif guardian_exit == _LINUX_GUARDIAN_REAP_FAILED_EXIT:
                    if not running.spawned.guardian_reap_failure_seen:
                        running.spawned.guardian_reap_failure_seen = True
                        return False, process_violation
                    process_violation = True
                elif guardian_exit not in {
                    _LINUX_GUARDIAN_CLEAN_EXIT,
                    _LINUX_GUARDIAN_START_FAILED_EXIT,
                }:
                    process_violation = True
                if identity is not None:
                    try:
                        observed = _read_posix_process_table().get(identity[0])
                    except OSError:
                        return False, process_violation
                    if (
                        observed is not None
                        and observed.live
                        and observed.start_time == identity[1]
                    ):
                        return False, process_violation
                return descendants_clean, process_violation
            elif process.pid is not None:
                if process.is_alive():
                    try:
                        process_group = os.getpgid(process.pid)
                        if process_group == process.pid:
                            os.killpg(process_group, signal.SIGKILL)
                        else:
                            process.kill()
                    except (OSError, ProcessLookupError):
                        try:
                            process.kill()
                        except (OSError, ProcessLookupError):
                            pass
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass
            process.join(self._shutdown_timeout_seconds)
            if process.is_alive():
                try:
                    process.kill()
                except (OSError, ProcessLookupError):
                    pass
                process.join(self._shutdown_timeout_seconds)
            return not process.is_alive() and descendants_clean, process_violation
        finally:
            if job is not None:
                job.close()

    def _fail_running(
        self,
        running: _RunningWorker,
        *,
        status: str,
        reason: str,
    ) -> None:
        with self._lock:
            running.cleanup_pending = True
            self._invalidate_generation(running)
        reaped, process_violation = self._kill_and_reap_fail_closed(
            running,
            survivors_are_violation=status == "timeout",
        )
        if process_violation and running.local_execution is not None:
            try:
                _quarantine_local_execution(running.authority, running.local_execution)
                status = "quarantined"
                reason = "plugin_local_process_model_incompatible"
            except PluginExecutionWorkerError:
                reason = "plugin_worker_quarantine_unavailable"
        if reaped:
            self._record_terminal(running, status, cleanup_proven=True)
        else:
            self._record_reap_failure(running)
            raise PluginExecutionWorkerError("plugin_worker_reap_failed")
        raise PluginExecutionWorkerError(reason)

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        return remaining

    def run_sync(
        self,
        *,
        authority: PluginExecutionAuthority,
        body: Mapping[str, Any],
        credential: Mapping[str, Any],
        timeout_seconds: float,
        _management: _ManagedWorkerOperation | None = None,
    ) -> Mapping[str, Any]:
        if type(authority) is not PluginExecutionAuthority:
            raise ValueError("plugin_worker_authority_invalid")
        if (
            not isinstance(body, Mapping)
            or not isinstance(credential, Mapping)
            or type(timeout_seconds) not in {int, float}
            or timeout_seconds <= 0
        ):
            raise ValueError("plugin_worker_payload_invalid")
        plain_body = _plain_json(body)
        plain_credential = _plain_json(credential)
        if type(plain_body) is not dict or type(plain_credential) is not dict:
            raise ValueError("plugin_worker_payload_invalid")
        local_contained = "local_execution" in plain_body
        local_execution: _LocalMcpWorkerExecution | None = None
        if local_contained:
            local_execution = _LocalMcpWorkerExecution.from_dict(
                plain_body["local_execution"]
            )
            _assert_local_execution_not_quarantined(authority, local_execution)
            if (
                os.name != "nt"
                and not _posix_process_model_available()
            ):
                _quarantine_local_execution(authority, local_execution)
                raise PluginExecutionWorkerError(
                    "plugin_local_process_model_incompatible"
                )
        key = authority.execution_key
        with self._lock:
            if self._closed:
                raise PluginExecutionWorkerError("plugin_runtime_closed")
            if self._cleanup_fence_locked():
                raise PluginExecutionWorkerError("plugin_worker_reap_pending")
            if key in self._active or any(
                starting_key == key
                for starting_key, _starting_generation in self._starting_operations
            ):
                # Admission is exact per key.  A reserved but not yet published
                # start owns the key as strongly as a registered generation, so
                # no later registration can displace an exact reap owner.
                raise PluginExecutionWorkerError("plugin_operation_in_progress")
            if _management is not None:
                with _management.lock:
                    if _management.cancel_requested:
                        raise PluginExecutionWorkerError("plugin_operation_cancelled")
            generation = self._next_generation(key)
            starting_identity = (key, generation)
            self._starting_operations.add(starting_identity)
        deadline = time.monotonic() + float(timeout_seconds)
        bootstrap_recv: Connection | None = None
        bootstrap_send: Connection | None = None
        command_recv: Connection | None = None
        command_send: Connection | None = None
        lifeline_recv: Connection | None = None
        lifeline_send: Connection | None = None
        result_recv: Connection | None = None
        result_send: Connection | None = None
        running: _RunningWorker | None = None
        try:
            nonce = secrets.token_hex(32)
            secret = secrets.token_bytes(32)
            bootstrap_recv, bootstrap_send = self._context.Pipe(duplex=False)
            command_recv, command_send = self._context.Pipe(duplex=False)
            lifeline_recv, lifeline_send = self._context.Pipe(duplex=False)
            result_recv, result_send = self._context.Pipe(duplex=False)
            spawned = self._spawn(
                bootstrap_recv,
                command_recv,
                lifeline_recv,
                result_send,
                local_contained=local_contained,
                monitor_deadline=deadline,
            )
            running = _RunningWorker(
                authority,
                generation,
                nonce,
                spawned,
                bootstrap_send,
                command_send,
                result_recv,
                local_execution,
                lifeline_send,
            )
            with self._starting_condition:
                # Publish the exact killable generation before any failable or
                # stalling containment work, so cancellation and close always
                # have a bounded, exact handle for a live child.
                self._tracked[id(running)] = running
                self._starting_operations.discard(starting_identity)
                self._starting_condition.notify_all()
                closed_after_spawn = self._closed
                if closed_after_spawn:
                    running.cleanup_pending = True
                    self._invalidate_generation(running)
                if _management is not None:
                    with _management.lock:
                        _management.running = running
                        if _management.cancel_requested:
                            running.cleanup_pending = True
                            running.cancellation_requested = True
                            self._invalidate_generation(running)
                    _management.ready.set()
                if self._active.setdefault(key, running) is not running:
                    raise PluginExecutionWorkerError("plugin_operation_in_progress")
            self._attach_containment(
                spawned,
                local_contained=local_contained,
                monitor_deadline=deadline,
            )
        except BaseException:
            if running is not None:
                self._fail_running(
                    running,
                    status="failed",
                    reason="plugin_worker_unavailable",
                )
            with self._lock:
                try:
                    self._next_generation(key)
                    self._outcomes.append(
                        PluginExecutionTerminalOutcome(
                            authority,
                            generation,
                            "spawn_failed",
                        )
                    )
                except BaseException:
                    # Reservation retirement below is unconditional.  Setup
                    # accounting must never turn a no-process failure into a
                    # permanent cleanup fence.
                    pass
            raise PluginExecutionWorkerError("plugin_worker_unavailable") from None
        finally:
            self._close_connections(
                bootstrap_recv,
                command_recv,
                lifeline_recv,
                result_send,
            )
            with self._starting_condition:
                self._starting_operations.discard(starting_identity)
                self._starting_condition.notify_all()
        assert running is not None
        if closed_after_spawn:
            self._fail_running(
                running,
                status="closed",
                reason="plugin_runtime_closed",
            )
        try:
            if running.cancellation_requested:
                self._reap_abandoned_cancellation(running, _management)
                raise PluginExecutionWorkerError("plugin_operation_cancelled")
            bootstrap_send.send_bytes(secret)
            bootstrap_send.close()
            _send_frame(
                command_send,
                _encode_authenticated_frame(
                    secret=secret,
                    kind="hello",
                    nonce=nonce,
                    generation=generation,
                    sequence=1,
                    body={},
                    max_frame_bytes=self._max_frame_bytes,
                ),
            )
            ready = _receive_frame(
                result_recv,
                timeout_seconds=self._remaining(deadline),
                max_bytes=self._max_frame_bytes,
            )
            ready_frame = _decode_authenticated_frame(
                ready,
                secret=secret,
                expected_kind="ready",
                expected_nonce=nonce,
                expected_generation=generation,
                expected_sequence=2,
                max_frame_bytes=self._max_frame_bytes,
            )
            if spawned.linux_guarded:
                identity = ready_frame["body"]
                guardian_pid = spawned.process.pid
                if (
                    type(identity) is not dict
                    or set(identity) != {"pid", "start_time"}
                    or type(identity["pid"]) is not int
                    or identity["pid"] <= 0
                    or type(identity["start_time"]) is not int
                    or identity["start_time"] <= 0
                    or type(guardian_pid) is not int
                    or guardian_pid <= 0
                ):
                    raise _invalid_protocol()
                table = _read_posix_process_table()
                worker_record = table.get(identity["pid"])
                guardian_record = table.get(guardian_pid)
                if (
                    worker_record is None
                    or not worker_record.live
                    or worker_record.start_time != identity["start_time"]
                    or worker_record.ppid != guardian_pid
                    or guardian_record is None
                    or not guardian_record.live
                ):
                    raise _invalid_protocol()
                descendant_monitor = _PosixDescendantMonitor(
                    worker_record.pid,
                    monitor_deadline=deadline,
                )
                descendant_monitor.start()
                spawned.descendant_monitor = descendant_monitor
                spawned.worker_identity = worker_record.identity
            elif ready_frame["body"] != {}:
                raise _invalid_protocol()
            _send_frame(
                lifeline_send,
                _encode_authenticated_frame(
                    secret=secret,
                    kind="guardian",
                    nonce=nonce,
                    generation=generation,
                    sequence=3,
                    body={},
                    max_frame_bytes=self._max_frame_bytes,
                ),
            )
            _send_frame(
                command_send,
                _encode_authenticated_frame(
                    secret=secret,
                    kind="execute",
                    nonce=nonce,
                    generation=generation,
                    sequence=4,
                    body={
                        "authority": authority.to_dict(),
                        "body": plain_body,
                        "credential": plain_credential,
                        "deadline_monotonic": deadline,
                    },
                    max_frame_bytes=self._max_frame_bytes,
                ),
            )
            first_response_frame = _receive_frame(
                result_recv,
                timeout_seconds=self._remaining(deadline),
                max_bytes=self._max_frame_bytes,
            )
            first_response = _decode_authenticated_frame(
                first_response_frame,
                secret=secret,
                expected_kind=(
                    ("provider_started", "result")
                    if spawned.descendant_monitor is not None
                    else "result"
                ),
                expected_nonce=nonce,
                expected_generation=generation,
                expected_sequence=5,
                max_frame_bytes=self._max_frame_bytes,
            )
            if first_response["kind"] == "provider_started":
                identity = first_response["body"]
                if (
                    type(identity) is not dict
                    or set(identity) != {"pid", "start_time"}
                    or type(identity["pid"]) is not int
                    or identity["pid"] <= 0
                    or type(identity["start_time"]) is not int
                    or identity["start_time"] <= 0
                    or spawned.descendant_monitor is None
                ):
                    raise _invalid_protocol()
                pinned = spawned.descendant_monitor.pin(
                    identity["pid"],
                    expected_start_time=identity["start_time"],
                )
                if pinned.identity != (identity["pid"], identity["start_time"]):
                    raise _invalid_protocol()
                with self._lock:
                    if (
                        self._closed
                        or self._active.get(key) is not running
                        or self._generations.get(key) != generation
                    ):
                        raise PluginExecutionWorkerError("plugin_worker_unavailable")
                    _send_frame(
                        command_send,
                        _encode_authenticated_frame(
                            secret=secret,
                            kind="provider_release",
                            nonce=nonce,
                            generation=generation,
                            sequence=6,
                            body=identity,
                            max_frame_bytes=self._max_frame_bytes,
                        ),
                    )
                result = _decode_authenticated_frame(
                    _receive_frame(
                        result_recv,
                        timeout_seconds=self._remaining(deadline),
                        max_bytes=self._max_frame_bytes,
                    ),
                    secret=secret,
                    expected_kind="result",
                    expected_nonce=nonce,
                    expected_generation=generation,
                    expected_sequence=7,
                    max_frame_bytes=self._max_frame_bytes,
                )
            else:
                result = first_response
            authenticated_data, failure_reason = _classify_result_body(result["body"])
            # Re-validated here, not trusted: the child is the process that
            # handled provider bytes. Anything that is not Omega's own
            # ``TypeName at file.py:line in func`` is dropped.
            failure_diagnosis = result["body"].get("diagnosis")
            if type(failure_diagnosis) is not str or not _WORKER_DIAGNOSIS_RE.match(
                failure_diagnosis
            ):
                failure_diagnosis = None
            with self._lock:
                # Mark the generation authenticated together with the exact
                # same staleness test the join-and-check below repeats, so a
                # cancellation or deadline expiry racing the join can tell a
                # genuinely committed provider effect from one that never
                # became current.  Only a completed effect (not a failure)
                # is worth protecting: a failure has nothing to deliver.
                if authenticated_data is not None and not (
                    self._closed
                    or self._active.get(key) is not running
                    or self._generations.get(key) != generation
                ):
                    running.result_authenticated = True
            remaining = min(self._remaining(deadline), self._shutdown_timeout_seconds)
            spawned.process.join(remaining)
            if spawned.process.is_alive():
                if running.result_authenticated:
                    reaped, process_violation = self._kill_and_reap_fail_closed(
                        running, survivors_are_violation=True
                    )
                    status = "completed"
                    reason: str | None = None
                    if process_violation and running.local_execution is not None:
                        try:
                            _quarantine_local_execution(
                                running.authority, running.local_execution
                            )
                            status = "quarantined"
                            reason = "plugin_local_process_model_incompatible"
                        except PluginExecutionWorkerError:
                            reason = "plugin_worker_quarantine_unavailable"
                    if reaped:
                        self._record_terminal(running, status, cleanup_proven=True)
                    else:
                        self._record_reap_failure(running)
                        raise PluginExecutionWorkerError("plugin_worker_reap_failed")
                    if reason is not None:
                        raise PluginExecutionWorkerError(reason)
                    # The result frame already authenticated as a completed
                    # effect before this deadline expired and cleanup up to
                    # this exact tree is now proven, so the already-committed
                    # provider effect is delivered instead of discarded as a
                    # timeout.
                    assert authenticated_data is not None
                    return MappingProxyType(authenticated_data)
                self._fail_running(
                    running,
                    status="timeout",
                    reason="plugin_operation_timeout",
                )
            guardian_violation = False
            if spawned.linux_guarded:
                guardian_exit = spawned.process.exitcode
                if guardian_exit == _LINUX_GUARDIAN_REAP_FAILED_EXIT:
                    self._fail_running(
                        running,
                        status="failed",
                        reason="plugin_worker_reap_failed",
                    )
                guardian_violation = guardian_exit not in {
                    _LINUX_GUARDIAN_CLEAN_EXIT,
                    _LINUX_GUARDIAN_START_FAILED_EXIT,
                }
            if spawned.descendant_monitor is not None:
                process_violation, descendants_clean = (
                    spawned.descendant_monitor.finish(
                        survivors_are_violation=True,
                    )
                )
                if guardian_violation or process_violation or not descendants_clean:
                    assert local_execution is not None
                    _quarantine_local_execution(authority, local_execution)
                    self._close_pipes(running)
                    self._record_terminal(running, "quarantined")
                    raise PluginExecutionWorkerError(
                        "plugin_local_process_model_incompatible"
                    )
            if spawned.job is not None:
                spawned.job.close()
            self._close_pipes(running)
            with self._lock:
                if (
                    self._closed
                    or self._active.get(key) is not running
                    or self._generations.get(key) != generation
                ):
                    reason = "plugin_runtime_closed" if self._closed else "plugin_worker_unavailable"
                    self._record_terminal(running, "stale")
                    raise PluginExecutionWorkerError(reason)
            if authenticated_data is None:
                assert failure_reason is not None
                if (
                    failure_reason == "plugin_local_process_model_incompatible"
                    and local_execution is not None
                ):
                    _quarantine_local_execution(authority, local_execution)
                    self._record_terminal(running, "quarantined")
                else:
                    self._record_terminal(running, "failed")
                # `plugin_operation_failed` is one literal standing for every
                # cause a jailed child can hit, and the exception used to die
                # with the process. This is the only place that cause can be
                # said out loud, and it is said in Omega's words alone.
                _LOG.warning(
                    "plugin worker failed: %s (%s)",
                    failure_reason,
                    failure_diagnosis or "no diagnosis",
                )
                raise PluginExecutionWorkerError(failure_reason)
            self._record_terminal(running, "completed")
            return MappingProxyType(authenticated_data)
        except TimeoutError:
            self._fail_running(
                running,
                status="timeout",
                reason="plugin_operation_timeout",
            )
        except PluginExecutionWorkerError as exc:
            if running.terminal_recorded:
                raise
            if running.cancellation_requested:
                self._reap_abandoned_cancellation(running, _management)
                raise PluginExecutionWorkerError("plugin_operation_cancelled") from None
            with self._lock:
                closed = self._closed
            self._fail_running(
                running,
                status="closed" if closed else "failed",
                reason="plugin_runtime_closed" if closed else str(exc),
            )
        except BaseException:
            if running.cancellation_requested:
                self._reap_abandoned_cancellation(running, _management)
                raise PluginExecutionWorkerError("plugin_operation_cancelled") from None
            self._fail_running(
                running,
                status="failed",
                reason="plugin_worker_unavailable",
            )
        raise AssertionError("unreachable")

    def _reap_abandoned_cancellation(
        self,
        running: _RunningWorker,
        management: _ManagedWorkerOperation | None,
    ) -> None:
        """Prove cleanup for a generation the canceller could not reach.

        Cancellation waits only a bounded window for an exact killable handle.
        When it gave up before this generation existed, the publisher is the
        only remaining owner and must kill and reap it; otherwise the canceller
        already owns that exact cleanup and a second prover would race it.
        """

        if management is None:
            return
        with management.lock:
            abandoned = management.abandoned
        if abandoned:
            self._cancel_running(running)

    def _cancel_running(self, running: _RunningWorker) -> None:
        """Invalidate, kill, and reap exactly one cancelled generation."""

        with self._lock:
            if running.terminal_recorded:
                return
            if running.result_authenticated:
                # The authenticated result frame already decoded as a
                # completed provider effect for this exact generation, so
                # cancelling now cannot undo it.  Leave the generation
                # untouched: the run_sync thread's own join/record-terminal
                # path delivers it as `completed` instead of racing this
                # call to a `cancelled` outcome.
                return
            running.cleanup_pending = True
            running.cancellation_requested = True
            self._invalidate_generation(running)
        reaped, _process_violation = self._kill_and_reap_fail_closed(running)
        if reaped:
            self._record_terminal(running, "cancelled", cleanup_proven=True)
        else:
            self._record_reap_failure(running)

    def _cancel_managed_operation(
        self,
        management: _ManagedWorkerOperation,
    ) -> None:
        with management.lock:
            management.cancel_requested = True
            running = management.running
            thread = management.thread
        if running is None:
            # Wait only for the bounded publication window.  A start that
            # cannot produce an exact killable handle inside it stays globally
            # fenced instead of blocking cancellation indefinitely.
            management.ready.wait(self._shutdown_timeout_seconds)
            with management.lock:
                # Reading the handle and handing cleanup back are one step, so
                # a generation published a moment later always observes that
                # it, and not this caller, must prove its own tree reaped.
                # The fence is registered outside this lock: publication takes
                # the worker lock before the management lock, so acquiring
                # them in the opposite order here could deadlock.
                running = management.running
                published = management.ready.is_set()
                if running is None:
                    management.abandoned = True
            if running is None:
                if not published and thread is not None and thread.is_alive():
                    management.fence_token = self._register_unproven_start(thread)
                return
        self._cancel_running(running)

    async def run_async(
        self,
        *,
        authority: PluginExecutionAuthority,
        body: Mapping[str, Any],
        credential: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        """Run one operation with joined cleanup before cancellation escapes."""

        management = _ManagedWorkerOperation()

        def manage() -> None:
            try:
                management.result = self.run_sync(
                    authority=authority,
                    body=body,
                    credential=credential,
                    timeout_seconds=timeout_seconds,
                    _management=management,
                )
            except BaseException as exc:
                management.error = exc
            finally:
                management.ready.set()
                management.done.set()

        thread = threading.Thread(
            target=manage,
            name="omega-plugin-worker-management",
            daemon=False,
        )
        management.thread = thread
        with self._lock:
            if self._closed:
                raise PluginExecutionWorkerError("plugin_runtime_closed")
            self._management_threads.add(thread)
            try:
                # Registration and start are one close-visible transition.
                # The manager may begin immediately but will block on this
                # RLock before it can enter worker admission.
                thread.start()
            except BaseException:
                self._management_threads.discard(thread)
                raise PluginExecutionWorkerError("plugin_worker_unavailable") from None
        try:
            try:
                # Waiting in the executor keeps the event loop responsive to
                # cancellation without introducing another persistent work
                # loop.  Cancellation below still joins the manager only
                # after it has killed and reaped the exact worker generation.
                await asyncio.to_thread(management.done.wait)
            except asyncio.CancelledError:
                try:
                    self._cancel_managed_operation(management)
                finally:
                    # The exact generation is already killed and reaped, or is
                    # globally fenced, so a manager stuck inside stalled setup
                    # cannot make cancellation unbounded.  Releasing the
                    # terminal wait keeps the executor thread from outliving
                    # the cancelled operation.
                    management.done.set()
                    thread.join(self._shutdown_timeout_seconds)
                    if (
                        management.fence_token is not None
                        and not thread.is_alive()
                    ):
                        self._retire_unproven_start(management.fence_token)
                        management.fence_token = None
                delivered = self._deliver_completed_terminal(management, thread)
                if delivered is not None:
                    task = asyncio.current_task()
                    if task is not None:
                        # This cancellation was absorbed by an already
                        # authoritative provider effect; consume it exactly
                        # once instead of discarding the committed result.
                        task.uncancel()
                    return delivered
                raise
            thread.join()
            if management.error is not None:
                raise management.error
            if management.result is None:
                raise PluginExecutionWorkerError("plugin_worker_unavailable")
            return management.result
        finally:
            if not thread.is_alive():
                with self._lock:
                    self._management_threads.discard(thread)

    def _deliver_completed_terminal(
        self,
        management: _ManagedWorkerOperation,
        thread: threading.Thread,
    ) -> Mapping[str, Any] | None:
        """Deliver a terminally completed, cleanup-proven result exactly once.

        Cancellation cannot undo a provider effect that already happened.  When
        the exact authenticated operation completed and its tree is proven
        reaped before the cancellation was delivered, that result is handed to
        the caller once so an authoritative provider mutation is committed
        rather than discarded.  Anything short of a proven terminal completion
        stays cancelled, and the invalidated generation can never publish.
        """

        if thread.is_alive():
            return None
        with management.lock:
            if management.delivered or management.error is not None:
                return None
            result = management.result
            running = management.running
        if result is None or running is None:
            return None
        with self._lock:
            if (
                not running.terminal_recorded
                or running.cleanup_pending
                or running.reap_failure_recorded
            ):
                return None
        with management.lock:
            if management.delivered:
                return None
            management.delivered = True
        return result

    def close(self) -> None:
        with self._starting_condition:
            first_close = not self._closed
            self._closed = True
            self._starting_condition.wait_for(
                lambda: not self._starting_operations,
                timeout=self._shutdown_timeout_seconds,
            )
            starting_pending = bool(self._starting_operations)
            active = self._known_running_locked()
            for running in active:
                running.cleanup_pending = True
                if first_close:
                    self._invalidate_generation(running)
        attempted = {id(running) for running in active}
        failures = starting_pending
        for running in active:
            reaped, _process_violation = self._kill_and_reap_fail_closed(running)
            failures = failures or not reaped
            if reaped:
                self._record_terminal(running, "closed", cleanup_proven=True)
            else:
                self._record_reap_failure(running)
        with self._lock:
            management_threads = tuple(self._management_threads)
        join_deadline = time.monotonic() + self._shutdown_timeout_seconds
        for thread in management_threads:
            if thread is not threading.current_thread():
                thread.join(max(0.0, join_deadline - time.monotonic()))
            if thread.is_alive():
                failures = True
            if not thread.is_alive():
                with self._lock:
                    self._management_threads.discard(thread)
        # A manager is registered before it spawns, while its generation is
        # tracked only after process creation succeeds.  Closing joins every
        # registered manager and then collects generations that appeared after
        # the first snapshot, so a late reap failure cannot be reported as a
        # clean close.
        with self._lock:
            late_active = tuple(
                running
                for running in self._known_running_locked()
                if id(running) not in attempted
            )
            for running in late_active:
                running.cleanup_pending = True
                self._invalidate_generation(running)
        for running in late_active:
            reaped, _process_violation = self._kill_and_reap_fail_closed(running)
            failures = failures or not reaped
            if reaped:
                self._record_terminal(running, "closed", cleanup_proven=True)
            else:
                self._record_reap_failure(running)
        with self._lock:
            self._prune_unproven_starts_locked()
            if self._unproven_starts:
                failures = True
        if failures:
            raise PluginExecutionWorkerError("plugin_worker_reap_failed")


def _descriptor_from_worker_document(value: object) -> object:
    from mcp_platform.client_contracts import (
        McpClientDeployment,
        McpClientLimits,
        McpConnectionDescriptor,
        McpTransport,
    )
    from plugin_platform.contracts import PluginAuthentication

    if type(value) is not dict or set(value) != {
        "app_id",
        "publisher_id",
        "deployment",
        "provider_id",
        "protocol_version",
        "transport",
        "verified_launch_sha256",
        "remote_origin",
        "authorization_issuer",
        "authentication",
        "declared_capabilities",
        "extension_versions",
        "capability_revision",
        "limits",
        "signed_capability_authority",
        "runtime_compatibility",
    }:
        raise ValueError("plugin_worker_payload_invalid")
    limits = value["limits"]
    if type(limits) is not dict:
        raise ValueError("plugin_worker_payload_invalid")
    return McpConnectionDescriptor(
        app_id=value["app_id"],
        publisher_id=value["publisher_id"],
        deployment=McpClientDeployment(value["deployment"]),
        provider_id=value["provider_id"],
        protocol_version=value["protocol_version"],
        transport=McpTransport(value["transport"]),
        verified_launch_sha256=value["verified_launch_sha256"],
        remote_origin=value["remote_origin"],
        authorization_issuer=value["authorization_issuer"],
        authentication=PluginAuthentication.from_dict(value["authentication"]),
        declared_capabilities=value["declared_capabilities"],
        extension_versions=value["extension_versions"],
        capability_revision=value["capability_revision"],
        limits=McpClientLimits(**limits),
        signed_capability_authority=value["signed_capability_authority"],
        runtime_compatibility=value["runtime_compatibility"],
    )


def _request_from_worker_document(value: object) -> object:
    import base64

    from mcp_platform.client_contracts import McpOperationKind, McpOperationRequest

    if type(value) is not dict or set(value) != {
        "request_id",
        "app_id",
        "operation",
        "capability_id",
        "arguments",
        "capability_revision",
        "deadline_monotonic",
        "cancellation_id",
        "task_id",
        "input_request_id",
        "request_state_base64",
    }:
        raise ValueError("plugin_worker_payload_invalid")
    encoded_state = value["request_state_base64"]
    if encoded_state is None:
        request_state = None
    elif type(encoded_state) is str:
        request_state = base64.b64decode(encoded_state, validate=True)
    else:
        raise ValueError("plugin_worker_payload_invalid")
    return McpOperationRequest(
        request_id=value["request_id"],
        app_id=value["app_id"],
        operation=McpOperationKind(value["operation"]),
        capability_id=value["capability_id"],
        arguments=value["arguments"],
        capability_revision=value["capability_revision"],
        deadline_monotonic=value["deadline_monotonic"],
        cancellation_id=value["cancellation_id"],
        task_id=value["task_id"],
        input_request_id=value["input_request_id"],
        request_state=request_state,
    )


def _validate_mcp_worker_operation(
    authority: PluginExecutionAuthority,
    request: object,
) -> None:
    from mcp_platform.client_contracts import McpOperationKind, McpOperationRequest

    if type(authority) is not PluginExecutionAuthority or type(request) is not McpOperationRequest:
        raise ValueError("plugin_worker_authority_invalid")
    expected = {
        ("tool", McpOperationKind.TOOL_CALL): f"tool:{authority.capability_id}",
        ("prompt", McpOperationKind.PROMPTS_LIST): "prompts/list",
        ("prompt", McpOperationKind.PROMPT_GET): f"prompt:{authority.capability_id}",
        ("resource", McpOperationKind.RESOURCES_LIST): "resources/list",
        ("resource", McpOperationKind.RESOURCE_READ): (
            f"resource:{authority.capability_id}"
        ),
        (
            "resource_template",
            McpOperationKind.RESOURCE_TEMPLATES_LIST,
        ): "resources/templates/list",
        ("resource_template", McpOperationKind.RESOURCE_READ): (
            f"resource:{authority.capability_id}"
        ),
    }.get((authority.capability_kind, request.operation))
    if (
        expected is None
        or request.app_id != authority.app_id
        or request.capability_revision != authority.capability_revision
        or request.capability_id != expected
    ):
        raise ValueError("plugin_worker_authority_invalid")


def _open_local_mcp_registries(
    execution: _LocalMcpWorkerExecution,
) -> tuple[object, object, object]:
    """Reopen current durable registries with repository-owned verifier roots."""

    from mcp_platform.app_registry import McpAppRegistry
    from mcp_platform.package_verifier import current_platform_tag, verify_package
    from mcp_platform.registry import PackageRegistry
    from mcp_platform.trust_store import load_embedded_key_ring as load_mcp_key_ring
    from plugin_platform.package_verifier import verify_plugin_package
    from plugin_platform import PluginRegistry
    from plugin_platform.trust import load_embedded_key_ring as load_plugin_key_ring

    if type(execution) is not _LocalMcpWorkerExecution:
        raise ValueError("plugin_worker_local_execution_invalid")
    plugin_key_ring = load_plugin_key_ring()
    mcp_key_ring = load_mcp_key_ring()
    platform_tag = current_platform_tag()
    plugin_registry = PluginRegistry(
        state_root=Path(execution.plugin_registry_root),
        verifier=lambda path: verify_plugin_package(
            path,
            plugin_key_ring,
            _OMEGA_VERSION,
        ),
    )
    app_registry = McpAppRegistry(state_root=Path(execution.mcp_state_root))
    package_registry = PackageRegistry(
        state_root=Path(execution.mcp_state_root),
        verifier=lambda path: verify_package(
            path,
            mcp_key_ring,
            omega_version=_OMEGA_VERSION,
            platform_tag=platform_tag,
        ),
    )
    return plugin_registry, app_registry, package_registry


def _build_local_mcp_factory(
    descriptor: object,
    prepared: _ReverifiedLocalMcpExecution,
) -> object:
    """Issue the one-shot descriptor-bound stdio factory inside the worker."""

    from mcp_platform.client_contracts import McpConnectionDescriptor
    from mcp_platform.redaction import RedactingTextSink
    from mcp_platform.transports.stdio import (
        VerifiedStdioLaunchGuard,
        build_verified_stdio_transport,
    )

    if (
        type(descriptor) is not McpConnectionDescriptor
        or type(prepared) is not _ReverifiedLocalMcpExecution
    ):
        raise ValueError("plugin_worker_local_authority_invalid")
    launch_guard = VerifiedStdioLaunchGuard._acquire_worker_contained(
        prepared.verified_package,
        package_root=prepared.package_root,
        environment=dict(prepared.environment),
        descriptor=descriptor,
        expected_component_identities=prepared.component_identities,
        provider_pin=(
            None if sys.platform == "win32" else _current_worker_provider_pin()
        ),
    )
    return build_verified_stdio_transport(
        descriptor,
        launch_guard,
        RedactingTextSink(
            omega_root=prepared.package_root,
            max_bytes=descriptor.limits.max_stderr_bytes,
        ),
    )


def execute_mcp_operation(
    body: dict[str, Any],
    credential: dict[str, Any],
) -> dict[str, Any]:
    """Child-only exact MCP dispatcher used by the production worker."""

    import asyncio

    from mcp_platform.authorization import _issue_worker_authorization_config
    from mcp_platform.client import CurrentMcpClient
    from mcp_platform.client_contracts import (
        McpClientError,
        McpClientDeployment,
        McpOperationKind,
    )
    from mcp_platform.transports.streamable_http import (
        StreamableHttpClientConfig,
        build_streamable_http_transport,
    )

    if type(body) is not dict or type(credential) is not dict:
        raise ValueError("plugin_worker_payload_invalid")
    if body.get("operation") == "oauth_refresh":
        from mcp_platform.oauth_broker import _execute_oauth_refresh_operation

        return _execute_oauth_refresh_operation(body, credential)
    remote_operation_fields = {"descriptor", "request", "worker_authority"}
    local_operation_fields = {*remote_operation_fields, "local_execution"}
    remote_discovery_fields = {"descriptor", "discovery", "worker_authority"}
    local_discovery_fields = {*remote_discovery_fields, "local_execution"}
    supplied_fields = frozenset(body)
    supported_fields = {
        frozenset(remote_operation_fields),
        frozenset(local_operation_fields),
        frozenset(remote_discovery_fields),
        frozenset(local_discovery_fields),
    }
    if supplied_fields not in supported_fields:
        raise ValueError("plugin_worker_payload_invalid")
    discovery = "discovery" in body
    if discovery and body["discovery"] is not True:
        raise ValueError("plugin_worker_payload_invalid")
    authority = PluginExecutionAuthority.from_dict(body["worker_authority"])
    descriptor = _descriptor_from_worker_document(body["descriptor"])
    request = (
        None
        if discovery
        else _request_from_worker_document(body["request"])
    )
    if request is not None:
        _validate_mcp_worker_operation(authority, request)
    signed = descriptor.signed_capability_authority
    signed_group = (
        None
        if not isinstance(signed, Mapping)
        else signed.get(
            {
                "tool": "tools",
                "prompt": "prompts",
                "resource": "resources",
                "resource_template": "resource_templates",
            }[authority.capability_kind]
        )
    )
    declared_kind = (
        "resource"
        if authority.capability_kind == "resource_template"
        else authority.capability_kind
    )
    if (
        descriptor.app_id != authority.app_id
        or descriptor.capability_revision != authority.capability_revision
        or not isinstance(signed_group, Mapping)
        or authority.capability_id not in signed_group
        or f"{declared_kind}:{authority.capability_id}"
        not in descriptor.declared_capabilities
    ):
        raise ValueError("plugin_worker_authority_invalid")
    if supplied_fields in {
        frozenset(local_operation_fields),
        frozenset(local_discovery_fields),
    }:
        if credential or descriptor.deployment is not McpClientDeployment.LOCAL:
            raise ValueError("plugin_worker_payload_invalid")
        execution = _LocalMcpWorkerExecution.from_dict(body["local_execution"])
        plugin_registry, app_registry, package_registry = _open_local_mcp_registries(
            execution
        )
        prepared = _reverify_local_mcp_execution(
            descriptor,
            authority,
            execution,
            plugin_registry=plugin_registry,
            app_registry=app_registry,
            package_registry=package_registry,
        )
        factory = _build_local_mcp_factory(descriptor, prepared)
    else:
        if descriptor.deployment is not McpClientDeployment.REMOTE:
            raise ValueError("plugin_worker_authority_invalid")
        if credential:
            if set(credential) != {"access_token"}:
                raise ValueError("plugin_worker_payload_invalid")
            authorization = _issue_worker_authorization_config(
                descriptor,
                access_token=credential["access_token"],
                authorization_context_revision=authority.authority_revision,
            )
            config = StreamableHttpClientConfig.authenticated(authorization)
        else:
            config = StreamableHttpClientConfig.unauthenticated()
        factory = build_streamable_http_transport(descriptor, config)
    client = CurrentMcpClient(descriptor, factory)

    async def run() -> dict[str, Any]:
        try:
            receipt = await client.start()
            if discovery:
                return {"discovery": receipt.to_dict()}
            assert request is not None
            if (
                authority.server_id is None
                or authority.server_version is None
                or receipt.server_id != authority.server_id
                or receipt.server_version != authority.server_version
            ):
                raise ValueError("plugin_worker_authority_invalid")
            operation = {
                McpOperationKind.TOOL_CALL: client.invoke_plugin_tool,
                McpOperationKind.PROMPTS_LIST: client.list_prompts,
                McpOperationKind.PROMPT_GET: client.get_prompt,
                McpOperationKind.RESOURCES_LIST: client.list_resources,
                McpOperationKind.RESOURCE_TEMPLATES_LIST: (
                    client.list_resource_templates
                ),
                McpOperationKind.RESOURCE_READ: client.read_resource,
            }.get(request.operation)
            if operation is None:
                raise ValueError("plugin_worker_operation_invalid")
            return (await operation(request)).to_dict()
        except McpClientError as exc:
            if discovery:
                return {
                    "discovery_error": {
                        "phase": "discovery",
                        "reason": exc.reason.value,
                    }
                }
            assert request is not None
            return {
                "request_id": request.request_id,
                "app_id": request.app_id,
                "capability_id": request.capability_id,
                "capability_revision": request.capability_revision,
                "status": "failed",
                "error": {"reason": exc.reason.value},
            }
        finally:
            await client.close()

    return asyncio.run(run())


__all__ = [
    "PluginExecutionAuthority",
    "PluginExecutionTerminalOutcome",
    "PluginExecutionWorker",
    "PluginExecutionWorkerError",
    "execute_mcp_operation",
]

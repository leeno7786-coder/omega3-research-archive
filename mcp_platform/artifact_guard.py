"""Retained native artifact identity guards for verified MCP process launch."""

from __future__ import annotations

import ctypes
import hashlib
import os
import stat
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any, Final, Iterable

_GENERIC_READ: Final = 0x80000000
_FILE_SHARE_READ: Final = 0x00000001
_OPEN_EXISTING: Final = 3
_FILE_ATTRIBUTE_NORMAL: Final = 0x00000080
_FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x00000400
_FILE_FLAG_BACKUP_SEMANTICS: Final = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT: Final = 0x00200000
_INVALID_HANDLE_VALUE: Final = ctypes.c_void_p(-1).value
_READ_CHUNK: Final = 1024 * 1024
_IDENTITY_POSIX: Final = 0
_IDENTITY_WINDOWS_LEGACY: Final = 1
_IDENTITY_WINDOWS_FILE_ID: Final = 2
_WINDOWS_FILE_ID_UNSUPPORTED_ERRORS: Final = frozenset({1, 50, 87})

_ComponentIdentity = tuple[int, int, int]
_ArtifactIdentityChain = tuple[_ComponentIdentity, ...]


class ArtifactGuardError(RuntimeError):
    """A stable launch-guard rejection reason."""


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = (
        ("file_attributes", wintypes.DWORD),
        ("creation_time", wintypes.FILETIME),
        ("last_access_time", wintypes.FILETIME),
        ("last_write_time", wintypes.FILETIME),
        ("volume_serial_number", wintypes.DWORD),
        ("file_size_high", wintypes.DWORD),
        ("file_size_low", wintypes.DWORD),
        ("number_of_links", wintypes.DWORD),
        ("file_index_high", wintypes.DWORD),
        ("file_index_low", wintypes.DWORD),
    )


class _FileIdInformation(ctypes.Structure):
    _fields_ = (
        ("volume_serial_number", ctypes.c_ulonglong),
        ("file_id", ctypes.c_ubyte * 16),
    )


class ArtifactLaunchGuard:
    """Deny artifact/directory write-delete sharing through child creation."""

    def __init__(self, handles: tuple[int, ...]) -> None:
        self._handles = handles

    @classmethod
    def acquire(
        cls,
        *,
        package_root: Path,
        artifact_path: Path,
        expected_sha256: str,
        expected_identity: tuple[int, int],
        expected_component_identities: _ArtifactIdentityChain,
    ) -> ArtifactLaunchGuard:
        if sys.platform != "win32":
            raise ArtifactGuardError("artifact_guard_unsupported_platform")

        handles: tuple[int, ...] = ()
        try:
            handles, component_identities = _open_windows_identity_chain(
                package_root,
                artifact_path,
            )
            if component_identities != expected_component_identities:
                raise ArtifactGuardError("artifact_identity_mismatch")
            artifact_handle = handles[-1]
            identity, digest = _identity_and_digest(artifact_handle)
            if identity != expected_identity:
                raise ArtifactGuardError("artifact_identity_mismatch")
            if digest != expected_sha256:
                raise ArtifactGuardError("artifact_digest_mismatch")
            return cls(handles)
        except BaseException:
            _close_handles(reversed(handles))
            raise

    def close(self) -> None:
        handles = self._handles
        self._handles = ()
        _close_handles(reversed(handles))

    def __enter__(self) -> ArtifactLaunchGuard:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _kernel32() -> Any:
    return ctypes.windll.kernel32


def descriptor_file_identity(file_descriptor: int) -> tuple[int, int]:
    """Return the native identity for the exact open descriptor being authenticated."""
    if sys.platform != "win32":
        status = os.fstat(file_descriptor)
        return status.st_dev, status.st_ino
    import msvcrt

    handle = msvcrt.get_osfhandle(file_descriptor)
    information = _file_information(handle)
    return (
        information.volume_serial_number,
        (information.file_index_high << 32) | information.file_index_low,
    )


def _open_guarded(path: Path, *, directory: bool) -> int:
    kernel32 = _kernel32()
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    flags = _FILE_FLAG_OPEN_REPARSE_POINT
    access = 0
    if directory:
        flags |= _FILE_FLAG_BACKUP_SEMANTICS
    else:
        flags |= _FILE_ATTRIBUTE_NORMAL
        access = _GENERIC_READ
    handle = create_file(
        str(path),
        access,
        _FILE_SHARE_READ,
        None,
        _OPEN_EXISTING,
        flags,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise ArtifactGuardError("artifact_guard_unavailable")
    try:
        information = _file_information(handle)
    except BaseException:
        _close_handles((handle,))
        raise
    is_directory = bool(information.file_attributes & 0x10)
    is_reparse = bool(information.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
    if is_reparse or is_directory != directory:
        _close_handles((handle,))
        raise ArtifactGuardError("artifact_identity_mismatch")
    return handle


def _file_information(handle: int) -> _ByHandleFileInformation:
    information = _ByHandleFileInformation()
    get_information = _kernel32().GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    get_information.restype = wintypes.BOOL
    if not get_information(handle, ctypes.byref(information)):
        raise ArtifactGuardError("artifact_guard_unavailable")
    return information


def _file_id_information(handle: int) -> tuple[int, int] | None:
    information = _FileIdInformation()
    kernel32 = _kernel32()
    try:
        get_information = kernel32.GetFileInformationByHandleEx
    except AttributeError:
        return None
    try:
        get_last_error = kernel32.GetLastError
    except AttributeError:
        raise ArtifactGuardError("artifact_guard_unavailable") from None
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    get_information.restype = wintypes.BOOL
    get_last_error.argtypes = ()
    get_last_error.restype = wintypes.DWORD
    if not get_information(
        handle,
        18,  # FileIdInfo
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        if int(get_last_error()) in _WINDOWS_FILE_ID_UNSUPPORTED_ERRORS:
            return None
        raise ArtifactGuardError("artifact_guard_unavailable")
    return (
        information.volume_serial_number,
        int.from_bytes(bytes(information.file_id), byteorder="little"),
    )


def _component_file_identity(handle: int) -> _ComponentIdentity:
    file_id = _file_id_information(handle)
    if file_id is not None:
        return (_IDENTITY_WINDOWS_FILE_ID, *file_id)
    information = _file_information(handle)
    return (
        _IDENTITY_WINDOWS_LEGACY,
        information.volume_serial_number,
        (information.file_index_high << 32) | information.file_index_low,
    )


def _artifact_component_paths(
    package_root: Path,
    artifact_path: Path,
) -> tuple[Path, ...]:
    try:
        root = Path(package_root)
        artifact = Path(artifact_path)
        if (
            not root.is_absolute()
            or not artifact.is_absolute()
            or ".." in root.parts
            or ".." in artifact.parts
            or root != Path(os.path.abspath(root))
            or artifact != Path(os.path.abspath(artifact))
        ):
            raise ValueError
        relative = artifact.relative_to(root)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        raise ArtifactGuardError("artifact_identity_mismatch") from None
    if not relative.parts:
        raise ArtifactGuardError("artifact_identity_mismatch")
    components = [root]
    current = root
    for part in relative.parts:
        if part in {"", ".", ".."}:
            raise ArtifactGuardError("artifact_identity_mismatch")
        current /= part
        components.append(current)
    return tuple(components)


def _open_windows_identity_chain(
    package_root: Path,
    artifact_path: Path,
) -> tuple[tuple[int, ...], _ArtifactIdentityChain]:
    handles: list[int] = []
    identities: list[_ComponentIdentity] = []
    try:
        paths = _artifact_component_paths(package_root, artifact_path)
        for index, path in enumerate(paths):
            handle = _open_guarded(path, directory=index < len(paths) - 1)
            handles.append(handle)
            identities.append(_component_file_identity(handle))
        return tuple(handles), tuple(identities)
    except BaseException:
        _close_handles(reversed(handles))
        raise


def _open_posix_identity_chain(
    package_root: Path,
    artifact_path: Path,
) -> tuple[tuple[int, ...], _ArtifactIdentityChain]:
    file_descriptors: list[int] = []
    identities: list[_ComponentIdentity] = []
    try:
        paths = _artifact_component_paths(package_root, artifact_path)
        no_follow = getattr(os, "O_NOFOLLOW")
        close_on_exec = getattr(os, "O_CLOEXEC")
        directory_flag = getattr(os, "O_DIRECTORY")
        for index, path in enumerate(paths):
            directory = index < len(paths) - 1
            flags = os.O_RDONLY | close_on_exec | no_follow
            if directory:
                flags |= directory_flag
            if index == 0:
                file_descriptor = os.open(path, flags)
            else:
                file_descriptor = os.open(
                    path.name,
                    flags,
                    dir_fd=file_descriptors[-1],
                )
            file_descriptors.append(file_descriptor)
            status = os.fstat(file_descriptor)
            if not (
                stat.S_ISDIR(status.st_mode)
                if directory
                else stat.S_ISREG(status.st_mode)
            ):
                raise ArtifactGuardError("artifact_identity_mismatch")
            identities.append(
                (_IDENTITY_POSIX, status.st_dev, status.st_ino)
            )
        return tuple(file_descriptors), tuple(identities)
    except ArtifactGuardError:
        for file_descriptor in reversed(file_descriptors):
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        raise
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        for file_descriptor in reversed(file_descriptors):
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        raise ArtifactGuardError("artifact_guard_unavailable") from None
    except BaseException:
        for file_descriptor in reversed(file_descriptors):
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        raise


def _capture_artifact_identity_chain(
    package_root: Path,
    artifact_path: Path,
) -> _ArtifactIdentityChain:
    """Sample every exact root-to-executable component through native handles."""
    if sys.platform == "win32":
        handles, identities = _open_windows_identity_chain(package_root, artifact_path)
        _close_handles(reversed(handles))
        return identities
    file_descriptors, identities = _open_posix_identity_chain(
        package_root,
        artifact_path,
    )
    for file_descriptor in reversed(file_descriptors):
        try:
            os.close(file_descriptor)
        except OSError:
            pass
    return identities


def _identity_and_digest(handle: int) -> tuple[tuple[int, int], str]:
    information = _file_information(handle)
    identity = (
        information.volume_serial_number,
        (information.file_index_high << 32) | information.file_index_low,
    )
    digest = hashlib.sha256()
    read_file = _kernel32().ReadFile
    read_file.argtypes = (
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    )
    read_file.restype = wintypes.BOOL
    while True:
        buffer = ctypes.create_string_buffer(_READ_CHUNK)
        read = wintypes.DWORD()
        if not read_file(
            handle,
            buffer,
            len(buffer),
            ctypes.byref(read),
            None,
        ):
            raise ArtifactGuardError("artifact_guard_unavailable")
        if read.value == 0:
            break
        digest.update(buffer.raw[: read.value])
    return identity, digest.hexdigest()


def _close_handles(handles: Iterable[int]) -> None:
    retained = tuple(handles)
    if not retained:
        return
    close_handle = _kernel32().CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    for handle in retained:
        close_handle(handle)

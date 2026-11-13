"""Retained no-follow filesystem primitives for plugin package trees."""

from __future__ import annotations

import ctypes
import hashlib
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_TREE_ENTRIES = 4096
MAX_TREE_DEPTH = 32

_REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_PINNED_VERIFICATION_PATH: ContextVar[Path | None] = ContextVar(
    "plugin_pinned_verification_path",
    default=None,
)

if os.name == "nt":
    from ctypes import wintypes

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _DELETE = 0x00010000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_TYPE_DISK = 0x0001
    _FILE_BEGIN = 0
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _FILE_DISPOSITION_INFO = 4
    _FILE_DISPOSITION_INFO_EX = 21
    _FILE_RENAME_INFORMATION = 10
    _FILE_DISPOSITION_FLAG_DELETE = 0x00000001
    _FILE_DISPOSITION_FLAG_POSIX_SEMANTICS = 0x00000002
    _FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE = 0x00000010

    class _FileTime(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation_time", _FileTime),
            ("access_time", _FileTime),
            ("write_time", _FileTime),
            ("volume_serial", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("internal", ctypes.c_void_p),
            ("internal_high", ctypes.c_void_p),
            ("offset", wintypes.DWORD),
            ("offset_high", wintypes.DWORD),
            ("event", wintypes.HANDLE),
        ]

    class _FileDispositionInfo(ctypes.Structure):
        _fields_ = [("delete_file", wintypes.BOOL)]

    class _FileDispositionInfoEx(ctypes.Structure):
        _fields_ = [("flags", wintypes.DWORD)]

    class _FileRenameInfo(ctypes.Structure):
        _fields_ = [
            ("replace_if_exists", wintypes.BOOLEAN),
            ("root_directory", wintypes.HANDLE),
            ("file_name_length", wintypes.DWORD),
            ("file_name", wintypes.WCHAR * 1),
        ]

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [
            ("status", ctypes.c_void_p),
            ("information", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _create_file = _kernel32.CreateFileW
    _create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _create_file.restype = wintypes.HANDLE
    _lock_file_ex = _kernel32.LockFileEx
    _lock_file_ex.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    )
    _lock_file_ex.restype = wintypes.BOOL
    _unlock_file_ex = _kernel32.UnlockFileEx
    _unlock_file_ex.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    )
    _unlock_file_ex.restype = wintypes.BOOL
    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = (wintypes.HANDLE,)
    _close_handle.restype = wintypes.BOOL
    _get_file_information = _kernel32.GetFileInformationByHandle
    _get_file_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    _get_file_information.restype = wintypes.BOOL
    _get_file_type = _kernel32.GetFileType
    _get_file_type.argtypes = (wintypes.HANDLE,)
    _get_file_type.restype = wintypes.DWORD
    _read_file = _kernel32.ReadFile
    _read_file.argtypes = (
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    )
    _read_file.restype = wintypes.BOOL
    _set_file_pointer = _kernel32.SetFilePointerEx
    _set_file_pointer.argtypes = (
        wintypes.HANDLE,
        ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong),
        wintypes.DWORD,
    )
    _set_file_pointer.restype = wintypes.BOOL
    _flush_file_buffers = _kernel32.FlushFileBuffers
    _flush_file_buffers.argtypes = (wintypes.HANDLE,)
    _flush_file_buffers.restype = wintypes.BOOL
    _set_file_information = _kernel32.SetFileInformationByHandle
    _set_file_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _set_file_information.restype = wintypes.BOOL
    _ntdll = ctypes.WinDLL("ntdll")
    _nt_set_information_file = _ntdll.NtSetInformationFile
    _nt_set_information_file.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.c_int,
    )
    _nt_set_information_file.restype = ctypes.c_long
else:
    import fcntl


@dataclass(frozen=True)
class _NodeInfo:
    identity: tuple[int, int]
    size: int
    is_directory: bool


@dataclass(frozen=True)
class _Node:
    path: Path
    relative: PurePosixPath
    handle: int
    info: _NodeInfo


@dataclass
class _WalkFrame:
    path: Path
    relative: PurePosixPath
    handle: int
    info: _NodeInfo
    entries: Iterator[os.DirEntry[str]]
    depth: int
    entry_count: int = 0


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def ensure_contained(root: Path, candidate: Path) -> Path:
    """Return a lexical absolute candidate only when it stays beneath root."""
    safe_root = _absolute(Path(root))
    safe_candidate = _absolute(Path(candidate))
    try:
        safe_candidate.relative_to(safe_root)
    except ValueError:
        raise ValueError("package_tree_invalid") from None
    return safe_candidate


def _windows_api_path(path: Path) -> str:
    value = os.fspath(_absolute(path))
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _windows_info(handle: int) -> _NodeInfo:
    information = _ByHandleFileInformation()
    if not _get_file_information(handle, ctypes.byref(information)):
        raise OSError(ctypes.get_last_error(), "file information unavailable")
    if information.attributes & _REPARSE_FLAG or _get_file_type(handle) != _FILE_TYPE_DISK:
        raise ValueError("package_tree_invalid")
    return _NodeInfo(
        identity=(
            information.volume_serial,
            (information.file_index_high << 32) | information.file_index_low,
        ),
        size=(information.size_high << 32) | information.size_low,
        is_directory=bool(information.attributes & _FILE_ATTRIBUTE_DIRECTORY),
    )


def _windows_open(
    path: Path,
    *,
    directory: bool,
    writable: bool = False,
    share_mutations: bool = False,
    delete_access: bool = False,
) -> tuple[int, _NodeInfo]:
    access = _GENERIC_READ | (_GENERIC_WRITE if writable else 0)
    if delete_access:
        access |= _DELETE
    pinned_verification = _PINNED_VERIFICATION_PATH.get()
    verification_read = (
        pinned_verification is not None
        and not writable
        and not delete_access
        and _absolute(path) == pinned_verification
    )
    if share_mutations:
        sharing = _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE
    elif verification_read:
        sharing = _FILE_SHARE_READ | _FILE_SHARE_DELETE
    else:
        sharing = _FILE_SHARE_READ
    handle = _create_file(
        _windows_api_path(path),
        access,
        sharing,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), "file open failed")
    try:
        information = _windows_info(handle)
        if information.is_directory is not directory:
            raise ValueError("package_tree_invalid")
    except BaseException:
        _close_handle(handle)
        raise
    return int(handle), information


def _posix_flags(*, directory: bool, writable: bool = False) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise ValueError("package_tree_invalid")
    flags = (os.O_RDWR if writable else os.O_RDONLY) | no_follow
    flags |= getattr(os, "O_CLOEXEC", 0)
    if directory:
        directory_flag = getattr(os, "O_DIRECTORY", None)
        if directory_flag is None:
            raise ValueError("package_tree_invalid")
        flags |= directory_flag
    else:
        flags |= getattr(os, "O_NONBLOCK", 0)
    return flags


def _posix_info(handle: int) -> _NodeInfo:
    observed = os.fstat(handle)
    if stat.S_ISDIR(observed.st_mode):
        is_directory = True
    elif stat.S_ISREG(observed.st_mode):
        is_directory = False
    else:
        raise ValueError("package_tree_invalid")
    return _NodeInfo(
        identity=(observed.st_dev, observed.st_ino),
        size=observed.st_size,
        is_directory=is_directory,
    )


def _posix_open(
    path: Path | str,
    *,
    directory: bool,
    writable: bool = False,
    dir_fd: int | None = None,
) -> tuple[int, _NodeInfo]:
    handle = os.open(path, _posix_flags(directory=directory, writable=writable), dir_fd=dir_fd)
    try:
        information = _posix_info(handle)
        if information.is_directory is not directory:
            raise ValueError("package_tree_invalid")
    except BaseException:
        os.close(handle)
        raise
    return handle, information


def _open_node(
    path: Path,
    *,
    directory: bool,
    writable: bool = False,
    parent_handle: int | None = None,
    share_mutations: bool = False,
    delete_access: bool = False,
) -> tuple[int, _NodeInfo]:
    if os.name == "nt":
        return _windows_open(
            path,
            directory=directory,
            writable=writable,
            share_mutations=share_mutations,
            delete_access=delete_access,
        )
    name: Path | str = path.name if parent_handle is not None else path
    return _posix_open(name, directory=directory, writable=writable, dir_fd=parent_handle)


def _close_native(handle: int) -> None:
    if os.name == "nt":
        _close_handle(handle)
    else:
        os.close(handle)


def _same_node(left: _NodeInfo, right: _NodeInfo, *, compare_size: bool) -> bool:
    return (
        left.identity == right.identity
        and left.is_directory is right.is_directory
        and (not compare_size or left.size == right.size)
    )


def _path_components(path: Path) -> tuple[Path, tuple[str, ...]]:
    candidate = _absolute(path)
    anchor = Path(candidate.anchor)
    parts = candidate.parts
    if not candidate.is_absolute() or not candidate.anchor or not parts:
        raise ValueError("package_tree_invalid")
    return anchor, tuple(parts[1:])


class _PinnedPath:
    """An absolute path opened one no-follow component at a time."""

    def __init__(self, path: Path, nodes: list[_Node], *, share_mutations: bool) -> None:
        self.path = path
        self.nodes = nodes
        self.share_mutations = share_mutations
        self._closed = False

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        directory: bool,
        writable: bool = False,
        share_mutations: bool = False,
        delete_access: bool = False,
    ) -> _PinnedPath:
        candidate = _absolute(path)
        anchor, parts = _path_components(candidate)
        nodes: list[_Node] = []
        try:
            handle, information = _open_node(
                anchor,
                directory=True,
                share_mutations=share_mutations,
            )
            nodes.append(_Node(anchor, PurePosixPath(), handle, information))
            current = anchor
            relative = PurePosixPath()
            for index, part in enumerate(parts):
                current /= part
                relative /= part
                final = index == len(parts) - 1
                handle, information = _open_node(
                    current,
                    directory=directory if final else True,
                    writable=writable if final else False,
                    parent_handle=nodes[-1].handle,
                    share_mutations=share_mutations,
                    delete_access=delete_access if final else False,
                )
                nodes.append(_Node(current, relative, handle, information))
        except BaseException:
            while nodes:
                _close_native(nodes.pop().handle)
            raise
        return cls(candidate, nodes, share_mutations=share_mutations)

    @property
    def leaf(self) -> _Node:
        return self.nodes[-1]

    def release_leaf(self) -> _Node:
        """Close the leaf while retaining its ancestors and identity receipt."""
        leaf = self.nodes[-1]
        _close_native(leaf.handle)
        self.nodes[-1] = _Node(leaf.path, leaf.relative, -1, leaf.info)
        return leaf

    def detach_leaf(self) -> _Node:
        """Transfer ownership of the exact leaf handle to the caller."""
        if self._closed or not self.nodes:
            raise ValueError("package_tree_invalid")
        return self.nodes.pop()

    def validate_binding(self) -> None:
        """Reopen the logical path component-wise and compare every identity."""
        expected = tuple(self.nodes)
        if not expected:
            raise ValueError("package_tree_invalid")
        validation = _PinnedPath.open(
            self.path,
            directory=expected[-1].info.is_directory,
            share_mutations=self.share_mutations,
        )
        try:
            if len(validation.nodes) != len(expected):
                raise ValueError("package_tree_invalid")
            for original, observed in zip(expected, validation.nodes, strict=True):
                if not _same_node(
                    original.info,
                    observed.info,
                    compare_size=not original.info.is_directory,
                ):
                    raise ValueError("package_tree_invalid")
        finally:
            validation.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        while self.nodes:
            handle = self.nodes.pop().handle
            if handle == -1:
                continue
            try:
                _close_native(handle)
            except OSError:
                continue


class PinnedPackageTree:
    """One exact tree verified with O(depth) live no-follow handles."""

    def __init__(self, root: Path, root_chain: _PinnedPath) -> None:
        self.root = _absolute(root)
        self._root_chain = root_chain
        self._nodes: dict[str, _NodeInfo] = {
            "": root_chain.leaf.info,
        }
        self._digests: dict[str, bytes] = {}
        self._file_paths: set[Path] = set()
        self._total_file_bytes = 0
        self._closed = False

    @classmethod
    def open(cls, root: Path) -> PinnedPackageTree:
        try:
            root_chain = _PinnedPath.open(root, directory=True)
        except (OSError, ValueError):
            raise ValueError("package_tree_invalid") from None
        tree = cls(root, root_chain)
        try:
            tree._walk()
        except BaseException:
            tree.close()
            raise
        return tree

    @property
    def files(self) -> frozenset[Path]:
        return frozenset(self._file_paths)

    @property
    def total_file_bytes(self) -> int:
        return self._total_file_bytes

    def __enter__(self) -> PinnedPackageTree:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._root_chain.close()

    @staticmethod
    def _scan(node: _Node) -> Iterator[os.DirEntry[str]]:
        target: Path | int = node.path if os.name == "nt" else node.handle
        return os.scandir(target)

    @staticmethod
    def _close_frame(frame: _WalkFrame) -> None:
        close = getattr(frame.entries, "close", None)
        if close is not None:
            close()

    @staticmethod
    def _canonical_relative(relative_path: str) -> PurePosixPath:
        relative = PurePosixPath(relative_path)
        if (
            not relative_path
            or relative.is_absolute()
            or relative.as_posix() != relative_path
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ValueError("package_tree_invalid")
        return relative

    def _check_directory_binding(self, frame: _WalkFrame, parent_handle: int) -> None:
        handle = -1
        try:
            handle, information = _open_node(
                frame.path,
                directory=True,
                parent_handle=parent_handle,
            )
            if not _same_node(frame.info, information, compare_size=False):
                raise ValueError("package_tree_invalid")
        finally:
            if handle != -1:
                _close_native(handle)

    def _walk(self) -> None:
        root_node = _Node(
            self.root,
            PurePosixPath(),
            self._root_chain.leaf.handle,
            self._root_chain.leaf.info,
        )
        try:
            root_entries = self._scan(root_node)
        except OSError:
            raise ValueError("package_tree_invalid") from None
        stack = [
            _WalkFrame(
                root_node.path,
                root_node.relative,
                root_node.handle,
                root_node.info,
                root_entries,
                0,
            )
        ]
        entry_count = 0
        try:
            while stack:
                frame = stack[-1]
                try:
                    entry = next(frame.entries)
                except StopIteration:
                    if frame.depth and not frame.entry_count:
                        raise ValueError("package_file_set_mismatch")
                    self._close_frame(frame)
                    stack.pop()
                    if stack:
                        try:
                            self._check_directory_binding(frame, stack[-1].handle)
                        finally:
                            _close_native(frame.handle)
                    continue

                frame.entry_count += 1
                entry_count += 1
                if entry_count > MAX_TREE_ENTRIES:
                    raise ValueError("package_tree_too_large")
                relative = frame.relative / entry.name
                candidate = self.root.joinpath(*relative.parts)
                observed = entry.stat(follow_symlinks=False)
                attributes = getattr(observed, "st_file_attributes", 0)
                if stat.S_ISLNK(observed.st_mode) or attributes & _REPARSE_FLAG:
                    raise ValueError("package_tree_invalid")
                key = relative.as_posix()
                if key in self._nodes:
                    raise ValueError("package_tree_invalid")
                if stat.S_ISDIR(observed.st_mode):
                    child_depth = frame.depth + 1
                    if child_depth > MAX_TREE_DEPTH:
                        raise ValueError("package_tree_too_deep")
                    handle, information = _open_node(
                        candidate,
                        directory=True,
                        parent_handle=frame.handle,
                    )
                    self._nodes[key] = information
                    node = _Node(candidate, relative, handle, information)
                    try:
                        entries = self._scan(node)
                    except BaseException:
                        _close_native(handle)
                        raise
                    stack.append(
                        _WalkFrame(
                            node.path,
                            node.relative,
                            node.handle,
                            node.info,
                            entries,
                            child_depth,
                        )
                    )
                elif stat.S_ISREG(observed.st_mode):
                    handle = -1
                    try:
                        handle, information = _open_node(
                            candidate,
                            directory=False,
                            parent_handle=frame.handle,
                        )
                        self._nodes[key] = information
                        self._file_paths.add(candidate)
                        self._total_file_bytes += information.size
                    finally:
                        if handle != -1:
                            _close_native(handle)
                else:
                    raise ValueError("package_tree_invalid")
        except ValueError:
            raise
        except OSError:
            raise ValueError("package_tree_invalid") from None
        finally:
            while stack:
                frame = stack.pop()
                self._close_frame(frame)
                if frame.depth:
                    try:
                        _close_native(frame.handle)
                    except OSError:
                        continue

    def _open_relative(self, relative: PurePosixPath) -> list[_Node]:
        handles: list[_Node] = []
        parent_handle = self._root_chain.leaf.handle
        current = self.root
        current_relative = PurePosixPath()
        try:
            for index, part in enumerate(relative.parts):
                current /= part
                current_relative /= part
                key = current_relative.as_posix()
                expected = self._nodes.get(key)
                if expected is None:
                    raise ValueError("package_tree_invalid")
                final = index == len(relative.parts) - 1
                handle, information = _open_node(
                    current,
                    directory=expected.is_directory,
                    parent_handle=parent_handle,
                )
                node = _Node(current, current_relative, handle, information)
                handles.append(node)
                if not _same_node(expected, information, compare_size=final):
                    raise ValueError("package_tree_invalid")
                parent_handle = handle
        except BaseException:
            while handles:
                _close_native(handles.pop().handle)
            raise
        return handles

    def read_bytes(self, relative_path: str, *, limit: int) -> bytes:
        """Read one regular file through a component-relative no-follow chain."""
        relative = self._canonical_relative(relative_path)
        expected = self._nodes.get(relative.as_posix())
        if expected is None or expected.is_directory:
            raise ValueError("package_tree_invalid")
        if expected.size > limit:
            raise ValueError("file_too_large")
        handles = self._open_relative(relative)
        try:
            payload = _read_native(handles[-1].handle, limit)
            after = _native_info(handles[-1].handle)
            if (
                len(payload) > limit
                or not _same_node(expected, after, compare_size=True)
                or len(payload) != after.size
            ):
                raise ValueError(
                    "file_too_large" if len(payload) > limit else "package_tree_invalid"
                )
            self._digests[relative.as_posix()] = hashlib.sha256(payload).digest()
            return payload
        finally:
            while handles:
                _close_native(handles.pop().handle)

    def validate_bindings(self) -> None:
        """Component-wise revalidate identities, file digests, and the exact tree."""
        try:
            self._root_chain.validate_binding()
            seen: set[str] = {""}
            self._validate_directory(
                _Node(
                    self.root,
                    PurePosixPath(),
                    self._root_chain.leaf.handle,
                    self._root_chain.leaf.info,
                ),
                seen,
            )
            if seen != set(self._nodes):
                raise ValueError("package_tree_invalid")
            self._root_chain.validate_binding()
        except ValueError:
            raise
        except OSError:
            raise ValueError("package_tree_invalid") from None

    def _validate_directory(self, root_node: _Node, seen: set[str]) -> None:
        """Iteratively rescan and rehash through O(depth) pinned handles."""
        root_entries = self._scan(root_node)
        stack = [
            _WalkFrame(
                root_node.path,
                root_node.relative,
                root_node.handle,
                root_node.info,
                root_entries,
                0,
            )
        ]
        entry_count = 0
        try:
            while stack:
                frame = stack[-1]
                try:
                    entry = next(frame.entries)
                except StopIteration:
                    self._close_frame(frame)
                    stack.pop()
                    if stack:
                        try:
                            self._check_directory_binding(frame, stack[-1].handle)
                        finally:
                            _close_native(frame.handle)
                    continue
                frame.entry_count += 1
                entry_count += 1
                if entry_count > MAX_TREE_ENTRIES:
                    raise ValueError("package_tree_invalid")
                relative = frame.relative / entry.name
                key = relative.as_posix()
                if key in seen:
                    raise ValueError("package_tree_invalid")
                expected = self._nodes.get(key)
                if expected is None:
                    raise ValueError("package_tree_invalid")
                candidate = self.root.joinpath(*relative.parts)
                handle, information = _open_node(
                    candidate,
                    directory=expected.is_directory,
                    parent_handle=frame.handle,
                )
                seen.add(key)
                if not _same_node(expected, information, compare_size=not expected.is_directory):
                    _close_native(handle)
                    raise ValueError("package_tree_invalid")
                node = _Node(candidate, relative, handle, information)
                if expected.is_directory:
                    try:
                        entries = self._scan(node)
                    except BaseException:
                        _close_native(handle)
                        raise
                    stack.append(
                        _WalkFrame(
                            node.path,
                            node.relative,
                            node.handle,
                            node.info,
                            entries,
                            frame.depth + 1,
                        )
                    )
                    continue
                try:
                    expected_digest = self._digests.get(key)
                    if expected_digest is not None:
                        payload = _read_native(handle, expected.size)
                        after = _native_info(handle)
                        if (
                            len(payload) != expected.size
                            or not _same_node(expected, after, compare_size=True)
                            or hashlib.sha256(payload).digest() != expected_digest
                        ):
                            raise ValueError("package_tree_invalid")
                finally:
                    _close_native(handle)
        finally:
            while stack:
                frame = stack.pop()
                self._close_frame(frame)
                if frame.depth:
                    try:
                        _close_native(frame.handle)
                    except OSError:
                        continue


def _windows_read(handle: int, limit: int) -> bytes:
    if not _set_file_pointer(handle, 0, None, _FILE_BEGIN):
        raise OSError(ctypes.get_last_error(), "file seek failed")
    payload = bytearray()
    while len(payload) <= limit:
        capacity = min(65536, limit + 1 - len(payload))
        buffer = ctypes.create_string_buffer(capacity)
        read = wintypes.DWORD()
        if not _read_file(handle, buffer, capacity, ctypes.byref(read), None):
            raise OSError(ctypes.get_last_error(), "file read failed")
        if not read.value:
            break
        payload.extend(buffer.raw[: read.value])
    return bytes(payload)


def _native_info(handle: int) -> _NodeInfo:
    return _windows_info(handle) if os.name == "nt" else _posix_info(handle)


def _read_native(handle: int, limit: int) -> bytes:
    if os.name == "nt":
        return _windows_read(handle, limit)
    os.lseek(handle, 0, os.SEEK_SET)
    payload = bytearray()
    while len(payload) <= limit:
        chunk = os.read(handle, min(65536, limit + 1 - len(payload)))
        if not chunk:
            break
        payload.extend(chunk)
    return bytes(payload)


def reject_reparse(path: Path) -> None:
    """Reject a missing path, reparse point, symlink, or special file."""
    candidate = _absolute(path)
    handle = -1
    try:
        observed = candidate.lstat()
        directory = stat.S_ISDIR(observed.st_mode)
        if not directory and not stat.S_ISREG(observed.st_mode):
            raise ValueError("package_tree_invalid")
        handle, _ = _open_node(candidate, directory=directory)
    except (OSError, ValueError):
        raise ValueError("package_tree_invalid") from None
    finally:
        if handle != -1:
            _close_native(handle)


def reject_reparse_chain(root: Path, candidate: Path) -> None:
    """Reject reparses from one pinned root through a contained candidate."""
    safe_root = _absolute(root)
    safe_candidate = ensure_contained(safe_root, candidate)
    handles: list[int] = []
    try:
        root_handle, _ = _open_node(safe_root, directory=True)
        handles.append(root_handle)
        current = safe_root
        parent_handle = root_handle
        parts = safe_candidate.relative_to(safe_root).parts
        for index, part in enumerate(parts):
            current /= part
            observed = (
                os.stat(part, dir_fd=parent_handle, follow_symlinks=False)
                if os.name != "nt"
                else current.lstat()
            )
            directory = stat.S_ISDIR(observed.st_mode)
            if not directory and (index != len(parts) - 1 or not stat.S_ISREG(observed.st_mode)):
                raise ValueError("package_tree_invalid")
            handle, _ = _open_node(current, directory=directory, parent_handle=parent_handle)
            handles.append(handle)
            parent_handle = handle
    except (OSError, ValueError):
        raise ValueError("package_tree_invalid") from None
    finally:
        while handles:
            _close_native(handles.pop())


def exact_regular_files(root: Path) -> frozenset[Path]:
    """Enumerate an iterative, no-follow tree within closed entry/depth limits."""
    with PinnedPackageTree.open(root) as tree:
        return tree.files


def fsync_file(path: Path) -> None:
    """Flush a regular file through a fully pinned no-follow component chain."""
    candidate = _absolute(path)
    chain: _PinnedPath | None = None
    try:
        chain = _PinnedPath.open(candidate, directory=False, writable=True)
        if os.name == "nt":
            if not _flush_file_buffers(chain.leaf.handle):
                raise OSError(ctypes.get_last_error(), "file flush failed")
        else:
            os.fsync(chain.leaf.handle)
        chain.release_leaf()
        chain.validate_binding()
    except (OSError, ValueError):
        raise ValueError("package_tree_invalid") from None
    finally:
        if chain is not None:
            chain.close()


def fsync_directory(path: Path) -> None:
    """Flush directory metadata through a fully pinned no-follow component chain."""
    candidate = _absolute(path)
    chain: _PinnedPath | None = None
    try:
        chain = _PinnedPath.open(candidate, directory=True)
        if os.name != "nt":
            os.fsync(chain.leaf.handle)
        chain.validate_binding()
    except (OSError, ValueError):
        raise ValueError("package_tree_invalid") from None
    finally:
        if chain is not None:
            chain.close()


def _mutation_parts(relative_path: str | Path) -> tuple[str, ...]:
    raw = Path(relative_path).as_posix()
    relative = PurePosixPath(raw)
    if (
        not raw
        or relative.is_absolute()
        or relative.as_posix() != raw
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("package_tree_invalid")
    return relative.parts


def _write_all(handle: int, payload: bytes) -> None:
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        written = os.write(handle, view[offset:])
        if written <= 0:
            raise OSError("file write failed")
        offset += written


def _after_tree_pinned(_path: Path) -> None:
    return


def _windows_mark_delete(handle: int) -> None:
    extended = _FileDispositionInfoEx(
        _FILE_DISPOSITION_FLAG_DELETE
        | _FILE_DISPOSITION_FLAG_POSIX_SEMANTICS
        | _FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE
    )
    if _set_file_information(
        handle,
        _FILE_DISPOSITION_INFO_EX,
        ctypes.byref(extended),
        ctypes.sizeof(extended),
    ):
        return
    basic = _FileDispositionInfo(True)
    if not _set_file_information(
        handle,
        _FILE_DISPOSITION_INFO,
        ctypes.byref(basic),
        ctypes.sizeof(basic),
    ):
        raise OSError(ctypes.get_last_error(), "file deletion failed")


def _windows_rename_handle(
    handle: int,
    parent_handle: int,
    target_name: str,
) -> None:
    encoded_name = target_name.encode("utf-16-le")
    buffer = ctypes.create_string_buffer(ctypes.sizeof(_FileRenameInfo) + len(encoded_name))
    information = ctypes.cast(
        buffer,
        ctypes.POINTER(_FileRenameInfo),
    ).contents
    information.replace_if_exists = True
    information.root_directory = parent_handle
    information.file_name_length = len(encoded_name)
    ctypes.memmove(
        ctypes.addressof(buffer) + _FileRenameInfo.file_name.offset,
        encoded_name,
        len(encoded_name),
    )
    status = _IoStatusBlock()
    result = _nt_set_information_file(
        handle,
        ctypes.byref(status),
        buffer,
        len(buffer),
        _FILE_RENAME_INFORMATION,
    )
    if result < 0:
        raise OSError(result & 0xFFFFFFFF, "file rename failed")


def _windows_remove_tree(path: Path, handle: int) -> None:
    with os.scandir(path) as entries:
        for entry in entries:
            observed = entry.stat(follow_symlinks=False)
            attributes = getattr(observed, "st_file_attributes", 0)
            if stat.S_ISLNK(observed.st_mode) or attributes & _REPARSE_FLAG:
                raise ValueError("package_tree_invalid")
            if stat.S_ISDIR(observed.st_mode):
                directory = True
            elif stat.S_ISREG(observed.st_mode):
                directory = False
            else:
                raise ValueError("package_tree_invalid")
            child_handle = -1
            try:
                child_handle, information = _windows_open(
                    path / entry.name,
                    directory=directory,
                    delete_access=True,
                )
                # Windows may report zero DirEntry identities while the retained
                # delete handle blocks rebinding. Compare when the filesystem
                # supplies one; the exact child handle remains authoritative.
                if observed.st_ino and information.identity != (
                    observed.st_dev,
                    observed.st_ino,
                ):
                    raise ValueError("package_tree_invalid")
                if directory:
                    _windows_remove_tree(path / entry.name, child_handle)
                _windows_mark_delete(child_handle)
            finally:
                if child_handle != -1:
                    _close_native(child_handle)
    _windows_mark_delete(handle)


def _posix_remove_tree(handle: int) -> None:
    with os.scandir(handle) as entries:
        for entry in entries:
            observed = entry.stat(follow_symlinks=False)
            attributes = getattr(observed, "st_file_attributes", 0)
            if stat.S_ISLNK(observed.st_mode) or attributes & _REPARSE_FLAG:
                raise ValueError("package_tree_invalid")
            if stat.S_ISDIR(observed.st_mode):
                directory = True
            elif stat.S_ISREG(observed.st_mode):
                directory = False
            else:
                raise ValueError("package_tree_invalid")
            child_handle = -1
            try:
                child_handle, information = _posix_open(
                    entry.name,
                    directory=directory,
                    dir_fd=handle,
                )
                if information.identity != (observed.st_dev, observed.st_ino):
                    raise ValueError("package_tree_invalid")
                if directory:
                    _posix_remove_tree(child_handle)
            finally:
                if child_handle != -1:
                    _close_native(child_handle)
            if directory:
                os.rmdir(entry.name, dir_fd=handle)
            else:
                os.unlink(entry.name, dir_fd=handle)


class PinnedTree:
    """One retained ordinary child tree deleted only through its pinned identity."""

    def __init__(self, path: Path, chain: _PinnedPath) -> None:
        self.path = _absolute(path)
        self._chain = chain
        self._closed = False
        self._removed = False

    def __enter__(self) -> PinnedTree:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def identity(self) -> tuple[int, int]:
        if self._closed:
            raise ValueError("package_tree_invalid")
        return self._chain.leaf.info.identity

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._chain.close()

    def verify(self, verifier: object) -> object:
        """Run a path verifier while the exact Windows delete pin is retained."""
        if self._closed or not callable(verifier):
            raise ValueError("package_tree_invalid")
        token = _PINNED_VERIFICATION_PATH.set(self.path)
        try:
            return verifier(self.path)
        finally:
            _PINNED_VERIFICATION_PATH.reset(token)

    def remove(self) -> None:
        if self._removed or self._closed:
            raise ValueError("package_tree_invalid")
        try:
            if os.name == "nt":
                observed = _native_info(self._chain.leaf.handle)
                if not _same_node(
                    self._chain.leaf.info,
                    observed,
                    compare_size=False,
                ):
                    raise ValueError("package_tree_invalid")
                _windows_remove_tree(self.path, self._chain.leaf.handle)
                self._removed = True
                self.close()
            else:
                self._chain.validate_binding()
                leaf = self._chain.leaf
                parent = self._chain.nodes[-2]
                _posix_remove_tree(leaf.handle)
                observed = os.stat(
                    self.path.name,
                    dir_fd=parent.handle,
                    follow_symlinks=False,
                )
                if (observed.st_dev, observed.st_ino) != leaf.info.identity:
                    raise ValueError("package_tree_invalid")
                os.rmdir(self.path.name, dir_fd=parent.handle)
                self._removed = True
                self.close()
        except (OSError, ValueError):
            raise ValueError("package_tree_invalid") from None


class PinnedDirectory:
    """Retain a no-follow directory chain while mutating children beneath it."""

    def __init__(self, path: Path, chain: _PinnedPath) -> None:
        self.path = _absolute(path)
        self._chain = chain
        self._closed = False

    @property
    def identity(self) -> tuple[int, int]:
        if self._closed:
            raise ValueError("package_tree_invalid")
        return self._chain.leaf.info.identity

    @classmethod
    def open(cls, path: Path) -> PinnedDirectory:
        try:
            chain = _PinnedPath.open(path, directory=True, share_mutations=True)
        except (OSError, ValueError):
            raise ValueError("package_tree_invalid") from None
        return cls(path, chain)

    @classmethod
    def create(cls, path: Path) -> PinnedDirectory:
        candidate = _absolute(path)
        parent = cls.open(candidate.parent)
        try:
            child = parent.ensure_directory(candidate.name)
            parent.fsync()
            return child
        finally:
            parent.close()

    def __enter__(self) -> PinnedDirectory:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._chain.close()

    def validate_binding(self) -> None:
        try:
            self._chain.validate_binding()
        except (OSError, ValueError):
            raise ValueError("package_tree_invalid") from None

    def fsync(self) -> None:
        try:
            if os.name != "nt":
                os.fsync(self._chain.leaf.handle)
            self.validate_binding()
        except (OSError, ValueError):
            raise ValueError("package_tree_invalid") from None

    def _validate_guard_prefix(self, guard: _PinnedPath) -> None:
        expected = self._chain.nodes
        if len(guard.nodes) < len(expected):
            raise ValueError("package_tree_invalid")
        for original, observed in zip(
            expected,
            guard.nodes[: len(expected)],
            strict=True,
        ):
            if not _same_node(original.info, observed.info, compare_size=False):
                raise ValueError("package_tree_invalid")

    @contextmanager
    def _windows_mutation_guard(
        self,
        path: Path | None = None,
        *,
        expected_leaf: _NodeInfo | None = None,
    ) -> Iterator[None]:
        if os.name != "nt":
            yield
            return
        guard = _PinnedPath.open(path or self.path, directory=True)
        try:
            self._validate_guard_prefix(guard)
            if expected_leaf is not None and not _same_node(
                expected_leaf,
                guard.leaf.info,
                compare_size=False,
            ):
                raise ValueError("package_tree_invalid")
            yield
            guard.validate_binding()
            self._validate_guard_prefix(guard)
        finally:
            guard.close()

    def _open_directories(
        self,
        parts: tuple[str, ...],
        *,
        create: bool,
    ) -> list[_Node]:
        nodes: list[_Node] = []
        parent_handle = self._chain.leaf.handle
        current = self.path
        relative = PurePosixPath()
        try:
            for part in parts:
                current /= part
                relative /= part
                try:
                    handle, information = _open_node(
                        current,
                        directory=True,
                        parent_handle=parent_handle,
                        share_mutations=True,
                    )
                except OSError:
                    if not create:
                        raise
                    if os.name == "nt":
                        expected_parent = nodes[-1].info if nodes else self._chain.leaf.info
                        with self._windows_mutation_guard(
                            current.parent,
                            expected_leaf=expected_parent,
                        ):
                            os.mkdir(current)
                    else:
                        os.mkdir(part, mode=0o700, dir_fd=parent_handle)
                    handle, information = _open_node(
                        current,
                        directory=True,
                        parent_handle=parent_handle,
                        share_mutations=True,
                    )
                    if os.name != "nt":
                        os.fsync(parent_handle)
                node = _Node(current, relative, handle, information)
                nodes.append(node)
                parent_handle = handle
            return nodes
        except BaseException:
            while nodes:
                _close_native(nodes.pop().handle)
            raise

    @staticmethod
    def _close_nodes(nodes: list[_Node]) -> None:
        while nodes:
            _close_native(nodes.pop().handle)

    def ensure_directory(self, relative_path: str | Path) -> PinnedDirectory:
        parts = _mutation_parts(relative_path)
        nodes = self._open_directories(parts, create=True)
        child: _PinnedPath | None = None
        try:
            self.fsync()
            child = _PinnedPath.open(
                nodes[-1].path,
                directory=True,
                share_mutations=True,
            )
            if not _same_node(nodes[-1].info, child.leaf.info, compare_size=False):
                raise ValueError("package_tree_invalid")
            self.validate_binding()
            return PinnedDirectory(nodes[-1].path, child)
        except BaseException:
            if child is not None:
                child.close()
            raise
        finally:
            self._close_nodes(nodes)

    def create_directory(self, relative_path: str | Path) -> PinnedDirectory:
        """Create and pin one previously absent direct child directory."""
        parts = _mutation_parts(relative_path)
        if len(parts) != 1:
            raise ValueError("package_tree_invalid")
        name = parts[0]
        try:
            if os.name == "nt":
                with self._windows_mutation_guard():
                    os.mkdir(self.path / name)
                self.fsync()
                return self.ensure_directory(name)
            else:
                os.mkdir(name, mode=0o700, dir_fd=self._chain.leaf.handle)
            self.fsync()
            return self.ensure_directory(name)
        except (OSError, ValueError):
            raise ValueError("package_tree_invalid") from None

    def write_bytes(
        self,
        relative_path: str | Path,
        payload: bytes,
        *,
        exclusive: bool = True,
    ) -> None:
        if type(payload) is not bytes:
            raise ValueError("package_tree_invalid")
        parts = _mutation_parts(relative_path)
        parents = self._open_directories(parts[:-1], create=True)
        parent_handle = parents[-1].handle if parents else self._chain.leaf.handle
        parent_path = parents[-1].path if parents else self.path
        leaf_name = parts[-1]
        leaf_path = parent_path / leaf_name
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0)
        flags |= os.O_EXCL if exclusive else os.O_TRUNC
        if os.name != "nt":
            flags |= getattr(os, "O_NOFOLLOW", 0)
        handle = -1
        write_guard: _PinnedPath | None = None
        try:
            if os.name == "nt":
                expected_parent = parents[-1].info if parents else self._chain.leaf.info
                write_guard = _PinnedPath.open(parent_path, directory=True)
                if not _same_node(
                    expected_parent,
                    write_guard.leaf.info,
                    compare_size=False,
                ):
                    raise ValueError("package_tree_invalid")
                self.validate_binding()
            handle = (
                os.open(leaf_path, flags, 0o600)
                if os.name == "nt"
                else os.open(leaf_name, flags, 0o600, dir_fd=parent_handle)
            )
            observed = os.fstat(handle)
            if not stat.S_ISREG(observed.st_mode):
                raise ValueError("package_tree_invalid")
            _write_all(handle, payload)
            os.fsync(handle)
            if os.name != "nt":
                os.fsync(parent_handle)
            elif write_guard is not None:
                write_guard.validate_binding()
            self.validate_binding()
        except (OSError, ValueError):
            raise ValueError("package_tree_invalid") from None
        finally:
            if handle != -1:
                os.close(handle)
            if write_guard is not None:
                write_guard.close()
            self._close_nodes(parents)

    def read_bytes(self, relative_path: str | Path, *, limit: int) -> bytes:
        parts = _mutation_parts(relative_path)
        parents = self._open_directories(parts[:-1], create=False)
        parent_handle = parents[-1].handle if parents else self._chain.leaf.handle
        parent_path = parents[-1].path if parents else self.path
        handle = -1
        try:
            handle, information = _open_node(
                parent_path / parts[-1],
                directory=False,
                parent_handle=parent_handle,
            )
            if information.size > limit:
                raise ValueError("file_too_large")
            payload = _read_native(handle, limit)
            if len(payload) > limit:
                raise ValueError("file_too_large")
            self.validate_binding()
            return payload
        finally:
            if handle != -1:
                _close_native(handle)
            self._close_nodes(parents)

    def exists(self, relative_path: str | Path) -> bool:
        parts = _mutation_parts(relative_path)
        parents: list[_Node] = []
        try:
            try:
                parents = self._open_directories(parts[:-1], create=False)
            except FileNotFoundError:
                return False
            parent_handle = parents[-1].handle if parents else self._chain.leaf.handle
            parent_path = parents[-1].path if parents else self.path
            try:
                if os.name == "nt":
                    (parent_path / parts[-1]).lstat()
                else:
                    os.stat(parts[-1], dir_fd=parent_handle, follow_symlinks=False)
            except FileNotFoundError:
                return False
            return True
        finally:
            self._close_nodes(parents)

    def replace(self, source_name: str, target_name: str) -> None:
        source = _mutation_parts(source_name)
        target = _mutation_parts(target_name)
        if len(source) != 1 or len(target) != 1:
            raise ValueError("package_tree_invalid")
        source_chain: _PinnedPath | None = None
        source_node: _Node | None = None
        try:
            if os.name == "nt":
                source_chain = _PinnedPath.open(
                    self.path / source[0],
                    directory=False,
                    delete_access=True,
                )
                self._validate_guard_prefix(source_chain)
                source_node = source_chain.detach_leaf()
                source_chain.close()
                source_chain = None
                _windows_rename_handle(
                    source_node.handle,
                    self._chain.leaf.handle,
                    target[0],
                )
                observed = _native_info(self._chain.leaf.handle)
                if not _same_node(
                    self._chain.leaf.info,
                    observed,
                    compare_size=False,
                ):
                    raise ValueError("package_tree_invalid")
                _close_native(source_node.handle)
                source_node = None
            else:
                os.replace(
                    source[0],
                    target[0],
                    src_dir_fd=self._chain.leaf.handle,
                    dst_dir_fd=self._chain.leaf.handle,
                )
            self.fsync()
        except (OSError, ValueError):
            raise ValueError("package_tree_invalid") from None
        finally:
            if source_chain is not None:
                source_chain.close()
            if source_node is not None:
                _close_native(source_node.handle)

    def rename_tree(self, source_name: str, target_name: str) -> None:
        """Rename one retained direct-child directory to an absent sibling."""

        source = _mutation_parts(source_name)
        target = _mutation_parts(target_name)
        if len(source) != 1 or len(target) != 1:
            raise ValueError("package_tree_invalid")
        source_chain: _PinnedPath | None = None
        source_node: _Node | None = None
        try:
            if self.exists(target[0]):
                raise ValueError("package_tree_invalid")
            if os.name == "nt":
                source_chain = _PinnedPath.open(
                    self.path / source[0],
                    directory=True,
                    delete_access=True,
                )
                self._validate_guard_prefix(source_chain)
                source_node = source_chain.detach_leaf()
                source_chain.close()
                source_chain = None
                _windows_rename_handle(
                    source_node.handle,
                    self._chain.leaf.handle,
                    target[0],
                )
                observed = _native_info(self._chain.leaf.handle)
                if not _same_node(
                    self._chain.leaf.info,
                    observed,
                    compare_size=False,
                ):
                    raise ValueError("package_tree_invalid")
                _close_native(source_node.handle)
                source_node = None
            else:
                os.rename(
                    source[0],
                    target[0],
                    src_dir_fd=self._chain.leaf.handle,
                    dst_dir_fd=self._chain.leaf.handle,
                )
            self.fsync()
        except (OSError, ValueError):
            raise ValueError("package_tree_invalid") from None
        finally:
            if source_chain is not None:
                source_chain.close()
            if source_node is not None:
                _close_native(source_node.handle)

    def unlink(self, relative_path: str | Path, *, missing_ok: bool = False) -> None:
        parts = _mutation_parts(relative_path)
        if len(parts) != 1:
            raise ValueError("package_tree_invalid")
        target_chain: _PinnedPath | None = None
        target_node: _Node | None = None
        try:
            if os.name == "nt":
                try:
                    target_chain = _PinnedPath.open(
                        self.path / parts[0],
                        directory=False,
                        delete_access=True,
                    )
                except FileNotFoundError:
                    if missing_ok:
                        return
                    raise
                self._validate_guard_prefix(target_chain)
                target_node = target_chain.detach_leaf()
                target_chain.close()
                target_chain = None
                _windows_mark_delete(target_node.handle)
                observed = _native_info(self._chain.leaf.handle)
                if not _same_node(
                    self._chain.leaf.info,
                    observed,
                    compare_size=False,
                ):
                    raise ValueError("package_tree_invalid")
                _close_native(target_node.handle)
                target_node = None
            else:
                os.unlink(parts[0], dir_fd=self._chain.leaf.handle)
            self.fsync()
        except FileNotFoundError:
            if not missing_ok:
                raise ValueError("package_tree_invalid") from None
        except (OSError, ValueError):
            raise ValueError("package_tree_invalid") from None
        finally:
            if target_chain is not None:
                target_chain.close()
            if target_node is not None:
                _close_native(target_node.handle)

    def pin_tree(self, relative_path: str | Path) -> PinnedTree:
        """Retain one ordinary descendant tree for validation and exact deletion."""
        parts = _mutation_parts(relative_path)
        candidate = self.path.joinpath(*parts)
        chain: _PinnedPath | None = None
        try:
            chain = _PinnedPath.open(
                candidate,
                directory=True,
                delete_access=os.name == "nt",
            )
            self._validate_guard_prefix(chain)
            _after_tree_pinned(candidate)
            return PinnedTree(candidate, chain)
        except (OSError, ValueError):
            if chain is not None:
                chain.close()
            raise ValueError("package_tree_invalid") from None

    @contextmanager
    def lock_file(
        self,
        relative_path: str | Path,
        *,
        timeout: float = 10.0,
    ) -> Iterator[None]:
        """Open one child under a Windows mutation guard, then retain its lock."""
        with self._lock_file_under_guard(relative_path, timeout=timeout):
            yield

    @contextmanager
    def _lock_file_under_guard(
        self,
        relative_path: str | Path,
        *,
        timeout: float,
    ) -> Iterator[None]:
        parts = _mutation_parts(relative_path)
        if len(parts) != 1:
            raise ValueError("package_tree_invalid")
        name = parts[0]
        handle = -1
        overlapped = None
        locked = False
        try:
            with self._windows_mutation_guard():
                if not self.exists(name):
                    try:
                        self.write_bytes(name, b"", exclusive=True)
                    except ValueError:
                        if not self.exists(name):
                            raise
                handle, before = _open_node(
                    self.path / name,
                    directory=False,
                    writable=True,
                    parent_handle=self._chain.leaf.handle,
                    share_mutations=True,
                )
            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                if os.name == "nt":
                    overlapped = _Overlapped()
                    locked = bool(
                        _lock_file_ex(
                            handle,
                            _LOCKFILE_EXCLUSIVE_LOCK | _LOCKFILE_FAIL_IMMEDIATELY,
                            0,
                            1,
                            0,
                            ctypes.byref(overlapped),
                        )
                    )
                else:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                    except BlockingIOError:
                        locked = False
                if locked:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("pinned file lock timed out")
                time.sleep(0.05)
            self.validate_binding()
            yield
            self.validate_binding()
            if os.name == "nt":
                after = _native_info(handle)
                if not _same_node(before, after, compare_size=False):
                    raise ValueError("package_tree_invalid")
            else:
                check_handle, after = _open_node(
                    self.path / name,
                    directory=False,
                    parent_handle=self._chain.leaf.handle,
                )
                try:
                    if not _same_node(before, after, compare_size=False):
                        raise ValueError("package_tree_invalid")
                finally:
                    _close_native(check_handle)
        finally:
            if locked and handle != -1:
                if os.name == "nt":
                    if overlapped is not None:
                        _unlock_file_ex(handle, 0, 1, 0, ctypes.byref(overlapped))
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)
            if handle != -1:
                _close_native(handle)

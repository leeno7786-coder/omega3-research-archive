"""One retained-handle lock and atomic publication boundary for MCP state."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from runtime.pinned_filesystem import PinnedDirectory

if os.name == "nt":
    import msvcrt
else:
    import fcntl

_DEFAULT_MAX_STATE_DOCUMENT_BYTES = 64 * 1024 * 1024


class McpStateLease:
    """One retained, process-released lease on a pinned state-root child."""

    def __init__(
        self,
        *,
        directory: PinnedDirectory,
        directory_lock_handle: int | None,
        lock_context: object,
        identity_handle: BinaryIO,
        name: str,
        identity: tuple[int, int],
        invalid_path_reason: str,
    ) -> None:
        self._directory = directory
        self._directory_lock_handle = directory_lock_handle
        self._lock_context = lock_context
        self._identity_handle = identity_handle
        self._name = name
        self._identity = identity
        self._invalid_path_reason = invalid_path_reason
        self._closed = False

    @property
    def identity(self) -> tuple[int, int]:
        if self._closed:
            raise ValueError(self._invalid_path_reason)
        return self._identity

    def validate(self) -> None:
        """Require the retained kernel lock to remain bound to its exact path node."""

        if self._closed:
            raise ValueError(self._invalid_path_reason)
        try:
            self._directory.validate_binding()
            if self._directory_lock_handle is not None:
                directory_stat = os.fstat(self._directory_lock_handle)
                if (
                    not stat.S_ISDIR(directory_stat.st_mode)
                    or (directory_stat.st_dev, directory_stat.st_ino)
                    != self._directory.identity
                ):
                    raise ValueError
            handle_stat = os.fstat(self._identity_handle.fileno())
            path = self._directory.path / self._name
            validate_existing_path(
                path,
                directory=False,
                reason=self._invalid_path_reason,
            )
            path_stat = os.stat(path, follow_symlinks=False)
            if (
                not stat.S_ISREG(handle_stat.st_mode)
                or not stat.S_ISREG(path_stat.st_mode)
                or (handle_stat.st_dev, handle_stat.st_ino) != self._identity
                or (path_stat.st_dev, path_stat.st_ino) != self._identity
            ):
                raise ValueError
        except (OSError, RuntimeError, TypeError, ValueError):
            raise ValueError(self._invalid_path_reason) from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        error: BaseException | None = None
        try:
            exit_method = getattr(self._lock_context, "__exit__")
            exit_method(None, None, None)
        except BaseException as exc:  # retain cleanup while normalizing path errors
            error = exc
        finally:
            try:
                if os.name == "nt":
                    self._identity_handle.seek(1)
                    msvcrt.locking(
                        self._identity_handle.fileno(),
                        msvcrt.LK_UNLCK,
                        1,
                    )
            except BaseException as exc:
                if error is None:
                    error = exc
            try:
                self._identity_handle.close()
            except BaseException as exc:
                if error is None:
                    error = exc
            if self._directory_lock_handle is not None:
                try:
                    fcntl.flock(self._directory_lock_handle, fcntl.LOCK_UN)
                except BaseException as exc:
                    if error is None:
                        error = exc
                try:
                    os.close(self._directory_lock_handle)
                except BaseException as exc:
                    if error is None:
                        error = exc
            try:
                self._directory.close()
            except BaseException as exc:
                if error is None:
                    error = exc
        if error is not None:
            if not isinstance(error, Exception):
                raise error
            raise ValueError(self._invalid_path_reason) from None

    def __enter__(self) -> McpStateLease:
        if self._closed:
            raise ValueError(self._invalid_path_reason)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


class McpStateStore:
    """Pin one MCP state root while reading and publishing its state document."""

    def __init__(
        self,
        *,
        state_root: Path,
        document_path: Path,
        additional_roots: tuple[tuple[Path, bool], ...] = (),
        invalid_path_reason: str,
        max_document_bytes: int = _DEFAULT_MAX_STATE_DOCUMENT_BYTES,
    ) -> None:
        if type(max_document_bytes) is not int or max_document_bytes <= 0:
            raise ValueError("max_document_bytes must be a positive integer")
        self.state_root = Path(os.path.abspath(os.fspath(Path(state_root))))
        self.document_path = Path(os.path.abspath(os.fspath(Path(document_path))))
        self.additional_roots = tuple(
            (Path(os.path.abspath(os.fspath(Path(path)))), directory)
            for path, directory in additional_roots
        )
        self.invalid_path_reason = invalid_path_reason
        self._max_document_bytes = max_document_bytes
        self._active_directory: ContextVar[PinnedDirectory | None] = ContextVar(
            f"mcp_state_store_{id(self)}",
            default=None,
        )

    def validate(self) -> None:
        """Reject reparses and wrong-kind nodes at every durable surface."""
        reason = self.invalid_path_reason
        validate_existing_path(self.state_root, directory=True, reason=reason)
        validate_existing_path(self.document_path.parent, directory=True, reason=reason)
        validate_existing_path(self.document_path, directory=False, reason=reason)
        for path, directory in self.additional_roots:
            validate_existing_path(path, directory=directory, reason=reason)
        lock_path = Path(f"{self.document_path}.lock")
        reject_reparse_chain(self.state_root, lock_path, reason=reason)
        validate_existing_path(lock_path, directory=False, reason=reason)

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the real sidecar through one pinned state-root handle chain."""
        self.validate()
        directory: PinnedDirectory | None = None
        caller_error: BaseException | None = None
        try:
            directory = _open_or_create_directory(self.state_root)
            if directory.path != self.state_root:
                raise ValueError("package_tree_invalid")
            directory.validate_binding()
            lock_name = f"{self.document_path.name}.lock"
            with directory.lock_file(lock_name):
                directory.validate_binding()
                self.validate()
                token = self._active_directory.set(directory)
                try:
                    try:
                        yield
                    except BaseException as error:
                        caller_error = error
                        raise
                    directory.validate_binding()
                    self.validate()
                finally:
                    self._active_directory.reset(token)
        except BaseException as exc:
            if caller_error is not None:
                if not isinstance(caller_error, Exception):
                    raise caller_error
                if exc is caller_error:
                    raise
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, (OSError, RuntimeError, TypeError, ValueError)):
                raise ValueError(self.invalid_path_reason) from None
            raise
        finally:
            if directory is not None:
                directory.close()

    def read_bytes(self) -> bytes | None:
        """Read the locked document through the retained state-root handle."""
        directory = self._require_active_directory()
        try:
            if not directory.exists(self.document_path.name):
                return None
            return directory.read_bytes(
                self.document_path.name,
                limit=self._max_document_bytes,
            )
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise ValueError(self.invalid_path_reason) from None

    def try_acquire_lease(self, name: str) -> McpStateLease | None:
        """Acquire a long-lived fixed child lock, or return None while it is live."""

        if (
            type(name) is not str
            or not name
            or len(name) > 128
            or Path(name).name != name
            or name in {".", ".."}
        ):
            raise ValueError(self.invalid_path_reason)
        self.validate()
        directory: PinnedDirectory | None = None
        directory_lock_handle: int | None = None
        directory_locked = False
        lock_context: object | None = None
        identity_handle: BinaryIO | None = None
        primary_locked = False
        identity_locked = False
        try:
            directory = _open_or_create_directory(self.state_root)
            if directory.path != self.state_root:
                raise ValueError("package_tree_invalid")
            directory.validate_binding()
            if os.name != "nt":
                directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                directory_flag = getattr(os, "O_DIRECTORY", None)
                no_follow = getattr(os, "O_NOFOLLOW", None)
                if directory_flag is None or no_follow is None:
                    raise ValueError(self.invalid_path_reason)
                directory_lock_handle = os.open(
                    self.state_root,
                    directory_flags | directory_flag | no_follow,
                )
                locked_directory_stat = os.fstat(directory_lock_handle)
                if (
                    not stat.S_ISDIR(locked_directory_stat.st_mode)
                    or (
                        locked_directory_stat.st_dev,
                        locked_directory_stat.st_ino,
                    )
                    != directory.identity
                ):
                    raise ValueError(self.invalid_path_reason)
                try:
                    fcntl.flock(
                        directory_lock_handle,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                except BlockingIOError:
                    raise TimeoutError("mcp state lease is live") from None
                directory_locked = True
                directory.validate_binding()
            lease_path = self.state_root / name
            reject_reparse_chain(self.state_root, lease_path, reason=self.invalid_path_reason)
            validate_existing_path(
                lease_path,
                directory=False,
                reason=self.invalid_path_reason,
            )
            lock_context = directory.lock_file(name, timeout=0.0)
            lock_context.__enter__()
            primary_locked = True
            flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(
                os,
                "O_CLOEXEC",
                0,
            )
            if os.name != "nt":
                no_follow = getattr(os, "O_NOFOLLOW", None)
                if no_follow is None:
                    raise ValueError(self.invalid_path_reason)
                flags |= no_follow
            raw_handle = os.open(lease_path, flags)
            identity_handle = os.fdopen(raw_handle, "r+b", buffering=0)
            if os.name == "nt":
                identity_handle.seek(1)
                try:
                    msvcrt.locking(
                        identity_handle.fileno(),
                        msvcrt.LK_NBLCK,
                        1,
                    )
                except OSError as exc:
                    if exc.errno in {
                        errno.EACCES,
                        errno.EAGAIN,
                        errno.EDEADLK,
                    }:
                        raise TimeoutError("mcp state lease is live") from None
                    raise
                identity_locked = True
            observed = os.fstat(identity_handle.fileno())
            identity = (observed.st_dev, observed.st_ino)
            if (
                not stat.S_ISREG(observed.st_mode)
                or any(type(item) is not int or item < 0 for item in identity)
            ):
                raise ValueError(self.invalid_path_reason)
            directory.validate_binding()
            validate_existing_path(
                lease_path,
                directory=False,
                reason=self.invalid_path_reason,
            )
            path_stat = os.stat(lease_path, follow_symlinks=False)
            if (
                not stat.S_ISREG(path_stat.st_mode)
                or (path_stat.st_dev, path_stat.st_ino) != identity
            ):
                raise ValueError(self.invalid_path_reason)
            return McpStateLease(
                directory=directory,
                directory_lock_handle=directory_lock_handle,
                lock_context=lock_context,
                identity_handle=identity_handle,
                name=name,
                identity=identity,
                invalid_path_reason=self.invalid_path_reason,
            )
        except TimeoutError:
            if identity_locked and identity_handle is not None and os.name == "nt":
                try:
                    identity_handle.seek(1)
                    msvcrt.locking(identity_handle.fileno(), msvcrt.LK_UNLCK, 1)
                except BaseException:
                    pass
            if identity_handle is not None:
                identity_handle.close()
            if primary_locked and lock_context is not None:
                try:
                    lock_context.__exit__(None, None, None)
                except BaseException:
                    pass
            if directory is not None:
                directory.close()
            if directory_locked and directory_lock_handle is not None:
                try:
                    fcntl.flock(directory_lock_handle, fcntl.LOCK_UN)
                except BaseException:
                    pass
            if directory_lock_handle is not None:
                os.close(directory_lock_handle)
            return None
        except BaseException as exc:
            if identity_locked and identity_handle is not None and os.name == "nt":
                try:
                    identity_handle.seek(1)
                    msvcrt.locking(identity_handle.fileno(), msvcrt.LK_UNLCK, 1)
                except BaseException:
                    pass
            if identity_handle is not None:
                try:
                    identity_handle.close()
                except BaseException:
                    pass
            if primary_locked and lock_context is not None:
                try:
                    lock_context.__exit__(None, None, None)
                except BaseException:
                    pass
            if directory is not None:
                directory.close()
            if directory_locked and directory_lock_handle is not None:
                try:
                    fcntl.flock(directory_lock_handle, fcntl.LOCK_UN)
                except BaseException:
                    pass
            if directory_lock_handle is not None:
                try:
                    os.close(directory_lock_handle)
                except BaseException:
                    pass
            if not isinstance(exc, Exception):
                raise
            raise ValueError(self.invalid_path_reason) from None

    def write_bytes(self, payload: bytes) -> None:
        """Publish the locked document with pinned temp, replace, cleanup, and fsync."""
        if type(payload) is not bytes:
            raise ValueError("mcp_state_payload_invalid")
        if len(payload) > self._max_document_bytes:
            raise ValueError("mcp_state_payload_limit_exceeded")
        directory = self._require_active_directory()
        document_name = self.document_path.name
        temporary_name = f".{document_name}.{uuid4().hex}.part"
        active: BaseException | None = None
        try:
            try:
                directory.write_bytes(temporary_name, payload)
                directory.replace(temporary_name, document_name)
            except BaseException as error:
                active = error
                raise
            finally:
                try:
                    directory.unlink(temporary_name, missing_ok=True)
                except BaseException:
                    if active is not None and not isinstance(active, Exception):
                        raise active
                    raise
        except Exception as error:
            # Pinned-filesystem failures belong to this store's public error
            # boundary, including failures during temporary-file cleanup.
            # Retain the cause; cancellation BaseExceptions keep their identity.
            raise ValueError(self.invalid_path_reason) from error

    def _require_active_directory(self) -> PinnedDirectory:
        directory = self._active_directory.get()
        if directory is None:
            raise RuntimeError("mcp_state_store_lock_required")
        return directory

    @property
    def max_document_bytes(self) -> int:
        return self._max_document_bytes


def _open_or_create_directory(path: Path) -> PinnedDirectory:
    candidate = Path(os.path.abspath(os.fspath(path)))
    current = candidate
    missing: list[str] = []
    while not current.exists():
        if current == current.parent:
            raise ValueError("package_tree_invalid")
        missing.append(current.name)
        current = current.parent
    parent = PinnedDirectory.open(current)
    if not missing:
        return parent
    try:
        child = parent.ensure_directory(Path(*reversed(missing)))
    finally:
        parent.close()
    return child


def reject_reparse(path: Path, *, reason: str) -> None:
    """Reject a symlink, junction, or other reparse-point path."""
    candidate = Path(path)
    try:
        is_junction = candidate.is_junction()
    except AttributeError:
        is_junction = False
    try:
        attributes = candidate.lstat().st_file_attributes
    except (AttributeError, OSError):
        attributes = 0
    if candidate.is_symlink() or is_junction or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError(reason)


def reject_reparse_chain(root: Path, candidate: Path, *, reason: str) -> None:
    """Reject reparses on the lexical path from ``root`` through ``candidate``."""
    durable_root = Path(root)
    target = Path(candidate)
    try:
        relative = target.relative_to(durable_root)
    except ValueError:
        raise ValueError(reason) from None
    current = durable_root
    reject_reparse(current, reason=reason)
    for part in relative.parts:
        current /= part
        reject_reparse(current, reason=reason)


def validate_existing_path(path: Path, *, directory: bool, reason: str) -> None:
    """Validate a durable path when present without creating it."""
    candidate = Path(path)
    reject_reparse(candidate, reason=reason)
    if not candidate.exists():
        return
    valid_kind = candidate.is_dir() if directory else candidate.is_file()
    if not valid_kind:
        raise ValueError(reason)

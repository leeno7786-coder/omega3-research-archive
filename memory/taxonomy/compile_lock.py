"""Stage 5 — single-writer file lock for the disposable-graph compile.

The taxonomy package never had an internal lock (it was Guardian-free but also never
live). With compile-on-episode-close (debounced) + compile-on-boot + reindex all able
to fire a full rebuild, two `reset_graph`+compile+manifest sequences must NEVER interleave
— an unlocked overlap can split graph.sqlite vs the manifest. This is an advisory, cross-
process, in-process-reentrant-safe file lock at compiled_root/.compile.lock.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from memory.taxonomy import paths as P

_PROC_LOCK = threading.Lock()  # serialize same-process compiles before touching the file
_POLL_S = 0.02
_STALE_S = 600.0  # a lockfile older than this is presumed orphaned (crashed compiler)


def lock_path(root: Path | str | None = None) -> Path:
    return P.compiled_root(root) / ".compile.lock"


def _holder_pid(path: Path) -> int | None:
    """The pid written into the lock file, or None when unreadable."""
    try:
        return int(path.read_text(encoding="ascii").strip() or "0") or None
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` names a running process. Unknown ⇒ True (never reclaim
    on a guess). On Windows os.kill(pid, 0) would TERMINATE the process, so the
    check goes through OpenProcess + the exit code instead."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, wintypes.DWORD(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                # ERROR_INVALID_PARAMETER means the PID does not exist. Any
                # other failure, especially ERROR_ACCESS_DENIED, is unknown
                # process state and must not reclaim a live holder.
                return int(kernel32.GetLastError()) != 87
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


@contextmanager
def compile_lock(root: Path | str | None = None, *, timeout_s: float = 120.0) -> Iterator[None]:
    """Block until the single-writer compile lock is held, then release on exit."""
    path = lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _PROC_LOCK.acquire()
    deadline = time.monotonic() + timeout_s
    acquired = False
    try:
        while True:
            try:
                # O_CREAT|O_EXCL: atomic "create iff absent" — the cross-process gate.
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("ascii"))
                os.close(fd)
                acquired = True
                break
            except FileExistsError:
                try:
                    holder = _holder_pid(path)
                    if holder is not None and holder != os.getpid() and not _pid_alive(holder):
                        # the compiler that held it is gone (crash / kill mid-compile):
                        # reclaim now. Live 2026-09-06: a brain force-killed during a
                        # compile left its pid here, and the next boot died on the
                        # 120 s timeout — the age rule alone needs ten minutes.
                        path.unlink(missing_ok=True)
                        continue
                    age = time.time() - path.stat().st_mtime
                    if age > _STALE_S:  # orphaned by a crashed compiler — reclaim
                        path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"compile_lock timeout after {timeout_s}s: {path}"
                    ) from None
                time.sleep(_POLL_S)
        yield
    finally:
        if acquired:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        _PROC_LOCK.release()

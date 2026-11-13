"""Cross-process sidecar lock for durable MCP/plugin state."""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator


class FileLockTimeout(TimeoutError):
    """The lock could not be acquired within the timeout."""


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fh: IO[bytes]) -> bool:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fh: IO[bytes]) -> None:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl

    def _try_lock(fh: IO[bytes]) -> bool:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fh: IO[bytes]) -> None:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


@contextmanager
def file_lock(path: Path | str, *, timeout: float = 10.0, poll: float = 0.05) -> Iterator[None]:
    """Hold an exclusive lock on ``<path>.lock`` for one durable mutation."""

    lock_path = Path(f"{path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")
    try:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while not _try_lock(fh):
            if time.monotonic() >= deadline:
                raise FileLockTimeout(f"could not acquire {lock_path} within {timeout}s")
            time.sleep(max(0.005, float(poll)))
        try:
            yield
        finally:
            _unlock(fh)
    finally:
        fh.close()


__all__ = ["FileLockTimeout", "file_lock"]

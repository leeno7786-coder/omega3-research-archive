"""Durable filesystem commits for episode / window persistence."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

# Windows os.replace (MoveFileEx) can transiently raise PermissionError("Access is denied")
# when the destination is briefly locked — e.g. a concurrent same-process writer replacing the
# same target, or an AV/indexer holding a handle. POSIX rename is atomic and never hits this, so
# the retry is a harmless no-op there. Bounded so a genuine permission fault still surfaces fast.
_REPLACE_ATTEMPTS = 8
_REPLACE_BACKOFF_S = 0.01


def _replace_with_retry(src: Path, dst: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_S * (attempt + 1))


def append_jsonl_line(path: Path, line: str) -> None:
    """Append one JSONL line and fsync so the record is on disk before return."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (line if line.endswith("\n") else f"{line}\n").encode("utf-8")
    with path.open("ab") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())


def write_text_fsync(path: Path, text: str) -> None:
    """Write a full file and fsync before return."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(text.encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())


def write_text_atomic(path: Path, text: str) -> None:
    """Atomically replace a file: write a temp sibling, fsync it, then os.replace
    over the target (atomic on the same volume). A crash mid-write leaves the prior
    COMPLETE file intact — never a truncated half-write that a fail-hard JSON loader
    would brick boot on. The temp is cleaned up on any failure. Use for canonical
    stores that are read fail-hard (deepsleep mastery/identity/semantic/etc.)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{path.name}.tmp-",
        dir=str(path.parent),
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise

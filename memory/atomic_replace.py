"""Exact-path atomic replacement for EXISTING canonical JSON files
(memory-organ Cut 4 rework 4, CUT4_REVIEW_REWORK_4.md §5).

The canonical filesystem is durable truth. A post-commit enrichment hook that
rewrites an existing canonical record (entity-link edges, supersedes edges,
a revert) may fail to add its derived structure, but it may NEVER destroy
the prior valid bytes. ``open(path, "w")`` / ``Path.write_text`` truncate
before the body is durable, so an I/O error mid-write leaves a torn file.

This helper is the repository's existing pattern
(``memory/contradiction_resolution.py``, ``memory/demotion.py``) made
exact-path and failure-clean:

    write replacement bytes to a SAME-DIRECTORY temp file
    flush + fsync
    atomic os.replace(temp -> canonical)
    on any failure: remove the temp; the canonical bytes are untouched

Guarantee: failure before (or during) the atomic replace leaves the previous
canonical bytes byte-identical. Not a truth store, not a validator, no path
or identity semantics — it only makes the last hop of a rewrite atomic.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

__all__ = ["atomic_replace_bytes", "atomic_replace_json"]


def _fsync_file(fh) -> None:  # seam for fault injection in tests
    fh.flush()
    os.fsync(fh.fileno())


def _replace(tmp: Path, path: Path) -> None:  # seam for fault injection in tests
    os.replace(tmp, path)


def atomic_replace_bytes(path: str | Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` atomically. Raises on failure; on
    failure the target file is untouched and the temp file is removed."""
    target = Path(path)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            _fsync_file(fh)
        _replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_replace_json(path: str | Path, doc: Any, *, indent: int = 2) -> None:
    """``atomic_replace_bytes`` of the canonical-file JSON encoding used by the
    hooks (``indent=2, ensure_ascii=False``). Serialisation happens BEFORE any
    file is touched, so an unserialisable doc cannot harm the target."""
    data = json.dumps(doc, indent=indent, ensure_ascii=False).encode("utf-8")
    atomic_replace_bytes(path, data)

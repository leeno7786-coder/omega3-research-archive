"""Monotonic write-ordinal — a durable, per-memory-root recency signal.

Committed records carry observed canonical timestamps. This counter preserves the
exact commit sequence when timestamps coincide: each committed record is stamped
once with the next ordinal, and the temporal lane sorts by it descending.

The caller supplies the counter file path (``atomic_write`` uses
``<MEMORY_ROOT>/indexes/write_ordinal.json``). Keeping it caller-supplied — rather
than resolving a global root here — is what makes it isolation-correct: the file
always lands in the SAME tree the record is written to, so test isolation and the
determinism replay (which both redirect ``validate_write.MEMORY_ROOT``) get a fresh
per-root counter automatically. That freshness is why a monotonic counter does NOT
break replay reproducibility: same fresh start + same write order -> identical
ordinals. Day-zero clears it with
``memory/indexes/*.json``.

Failure-soft: any IO fault degrades recency (best-effort value) but must never
block a memory write.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Union

_LOCK = threading.RLock()


def _read_counter(path: Path) -> int:
    try:
        if path.exists():
            return int(json.loads(path.read_text(encoding="utf-8")).get("counter", 0))
    except Exception:
        return 0
    return 0


def next_write_ordinal(counter_path: Union[str, Path]) -> int:
    """Atomically read + increment + persist the counter at *counter_path*; return
    the new ordinal (>= 1).

    File-backed with no in-memory cache so each memory root is its own source of
    truth (isolation-correct). Failure-soft: on IO fault still returns the computed
    ordinal rather than blocking the write — recency degrades, the write lands.
    """
    path = Path(counter_path)
    with _LOCK:
        nxt = _read_counter(path) + 1
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps({"counter": nxt}), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            pass  # never block a memory write on the recency counter
        return nxt


def reserve_write_ordinals(
    counter_path: Union[str, Path],
    count: int,
) -> tuple[int, ...]:
    """Reserve and persist one contiguous ordinal range for a group commit."""
    size = max(int(count), 0)
    if size == 0:
        return ()
    path = Path(counter_path)
    with _LOCK:
        current = _read_counter(path)
        final = current + size
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps({"counter": final}), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            pass
        return tuple(range(current + 1, final + 1))


def current_write_ordinal(counter_path: Union[str, Path]) -> int:
    """Return the current counter value at *counter_path* without incrementing."""
    with _LOCK:
        return _read_counter(Path(counter_path))

"""Observed-at sidecar — the calendar date a record was first committed.

Canonical records carry observed intake time, while later rewrites can update the
record body. This assign-once index preserves the original commit date and supports
legacy records that predate canonical event timestamps. Determinism replays pin the
canonical clock and keep this operational index outside their projection hashes.

The path is caller-supplied (``atomic_write`` uses ``<MEMORY_ROOT>/indexes/observed_at.json``) so the
sidecar lands in the SAME tree the record is written to — test isolation and the determinism replay,
which both redirect ``validate_write.MEMORY_ROOT``, get a fresh per-root sidecar automatically. That
is why a wall-clock sidecar does not leak non-determinism into the substrate: each isolated root has
its own, and it is never compared. Day-zero clears it with ``memory/indexes/*.json``.

Failure-soft: any IO fault degrades temporal grounding (best-effort value) but must never block a
memory write. Assign-once: a re-commit (decay / embed-backfill) preserves the ORIGINAL creation date.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Union

_LOCK = threading.RLock()


def _read_map(path: Path) -> Dict[str, str]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stamp_observed_at(
    map_path: Union[str, Path],
    memory_id: str,
    *,
    now: Optional[str] = None,
) -> Optional[str]:
    """Record the wall-clock ISO time *memory_id* was FIRST observed, ASSIGN-ONCE (a re-commit keeps
    the original date). Returns the observed_at in effect for the record (existing or newly stamped),
    or None for an empty id. *now* is injectable for deterministic tests. Failure-soft: on IO fault
    still returns the value rather than blocking the write."""
    mid = str(memory_id or "").strip()
    if not mid:
        return None
    path = Path(map_path)
    stamp = str(now or _now_iso())
    with _LOCK:
        data = _read_map(path)
        existing = data.get(mid)
        if isinstance(existing, str) and existing:
            return existing  # assign-once — never overwrite the creation date
        data[mid] = stamp
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            pass  # never block a memory write on the temporal sidecar
        return stamp


def stamp_observed_at_many(
    map_path: Union[str, Path],
    memory_ids,
    *,
    now: Optional[str] = None,
) -> Dict[str, str]:
    """Assign one observation stamp to a committed batch and persist once."""
    path = Path(map_path)
    stamp = str(now or _now_iso())
    with _LOCK:
        data = _read_map(path)
        changed = False
        for memory_id in memory_ids:
            mid = str(memory_id or "").strip()
            if not mid:
                continue
            existing = data.get(mid)
            if isinstance(existing, str) and existing:
                continue
            data[mid] = stamp
            changed = True
        if changed:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text(json.dumps(data), encoding="utf-8")
                tmp.replace(path)
            except Exception:
                pass
        return dict(data)


def observed_at_for(map_path: Union[str, Path], memory_id: str) -> Optional[str]:
    """Return the recorded observed_at ISO for *memory_id*, or None if absent."""
    with _LOCK:
        return _read_map(Path(map_path)).get(str(memory_id or ""))


def all_observed_at(map_path: Union[str, Path]) -> Dict[str, str]:
    """Return a copy of the full {memory_id: observed_at_iso} map (empty if the sidecar is absent)."""
    with _LOCK:
        return dict(_read_map(Path(map_path)))

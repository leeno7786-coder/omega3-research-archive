"""
Reversible tombstone / archive of a memory record
===================================================

DeepSleep's conflict reconciliation removes a contradiction's LOSER — but never destructively.
A tombstone:

    1. ARCHIVES the record's bytes (a full envelope) OUTSIDE the memory tree, so recall and the
       filesystem-derived graph both stop seeing it on the next reindex, yet it stays REVERSIBLE.
    2. UNLINKS the canonical file — path-guarded to the memory tree (reusing memory.rollback's
       guard shape), so nothing outside the tree can ever be removed.
    3. UNPATCHES the coord index, so recall drops it immediately and the next graph rebuild
       (two-organ doctrine: graph is DERIVED from the filesystem) excludes it.

Ordering is archive-BEFORE-unlink: if the archive write fails we abort and the record is never
removed — no irreversible loss. CANONICAL-IMMUNE: a ``flags.canonical`` record is never
auto-removed (operator 2026-07-15); the caller also gates on this, this is defense-in-depth.

The archive lives under ``guardian/state/tombstones`` — OUTSIDE the memory tree — so reindex, which
only walks the memory tree, never re-ingests a tombstoned record. Per "don't change the memory to
greet the logic": this ADDS a reversal capability; it does not touch the memory write path.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from core.paths import OMEGA_ROOT

log = logging.getLogger("omega.memory.tombstone")

TOMBSTONE_DIR = OMEGA_ROOT / "guardian" / "state" / "tombstones"


def _is_canonical(doc: Dict[str, Any]) -> bool:
    flags = doc.get("flags") if isinstance(doc.get("flags"), dict) else {}
    return bool(flags.get("canonical") or doc.get("canonical"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_doc(memory_id: str) -> Optional[Dict[str, Any]]:
    """Load a record by id via the coord index (best-effort)."""
    try:
        from memory.engine.walker import path_for_id

        path = path_for_id(memory_id)
        if not path:
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("tombstone: load %s failed: %s", memory_id, exc)
        return None


def tombstone_memory_record(
    memory_id: str,
    *,
    reason: str = "contradiction_loser",
    load_doc: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
    archive_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Reversibly tombstone (archive + unlink + de-index) a NON-canonical memory record.

    Returns a receipt dict: ``{tombstoned, reason, archived, file_removed, index_entry_removed,
    archive_path, errors}``. ``tombstoned`` is True only when the record was archived AND removed.
    Never raises — housekeeping degrades, it does not crash the close path."""
    from memory.engine.validate_write import MEMORY_ROOT, unpatch_index
    from memory.engine.walker import path_for_id

    result: Dict[str, Any] = {
        "memory_id": str(memory_id or ""),
        "tombstoned": False,
        "reason": "",
        "archived": False,
        "file_removed": False,
        "index_entry_removed": False,
        "errors": [],
    }
    mid = str(memory_id or "").strip()
    if not mid:
        result["reason"] = "missing_id"
        return result

    load = load_doc or _load_doc
    doc = load(mid)
    if not isinstance(doc, dict):
        result["reason"] = "not_found"
        return result

    # CANONICAL IMMUNE — defense in depth (the caller also gates on this).
    if _is_canonical(doc):
        result["reason"] = "canonical_immune"
        return result

    final_path = path_for_id(mid)
    if not final_path:
        result["reason"] = "path_unresolved"
        return result

    # PATH GUARD — refuse to touch anything outside the memory tree.
    try:
        p = Path(final_path).resolve()
        memory_root = Path(MEMORY_ROOT).resolve()
        try:
            p.relative_to(memory_root)
        except ValueError:
            msg = f"refusing to tombstone {p} — outside {memory_root} (path guard)"
            log.error("tombstone: %s", msg)
            result["errors"].append(msg)
            result["reason"] = "outside_memory_root"
            return result
    except OSError as exc:
        result["errors"].append(f"resolve failed: {exc}")
        result["reason"] = "resolve_failed"
        return result

    # 1. Archive bytes (reversible) OUTSIDE the memory tree. If this fails we abort BEFORE any
    #    removal — a record is never lost.
    adir = Path(archive_dir) if archive_dir is not None else TOMBSTONE_DIR
    envelope = {
        "memory_id": mid,
        "reason": str(reason),
        "tombstoned_at": _now_iso(),
        "original_path": str(p),
        "record": doc,
    }
    try:
        adir.mkdir(parents=True, exist_ok=True)
        apath = adir / f"{mid}.json"
        tmp = apath.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(envelope, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(apath)
        result["archived"] = True
        result["archive_path"] = str(apath)
    except OSError as exc:
        result["errors"].append(f"archive failed: {exc}")
        result["reason"] = "archive_failed"
        return result

    # 2. Unlink the canonical file (path-guarded above).
    try:
        if p.exists():
            p.unlink()
        result["file_removed"] = True
    except OSError as exc:
        result["errors"].append(f"unlink failed: {exc}")
        result["reason"] = "unlink_failed"
        return result

    # 3. Unpatch the coord index so recall + graph drop it (graph is rebuilt from the filesystem).
    try:
        result["index_entry_removed"] = bool(unpatch_index(mid))
    except Exception as exc:  # pragma: no cover - defensive
        result["errors"].append(f"unpatch_index raised: {exc}")

    # 4. Drop the projection receipt so integrity audit does not keep an orphan
    #    binding after the canonical file is gone. Best-effort: tombstone already
    #    succeeded if archive + unlink completed.
    try:
        from memory.projection_receipt import receipt_path

        rpath = receipt_path(mid)
        if rpath.exists():
            rpath.unlink()
            result["projection_receipt_removed"] = True
    except Exception as exc:  # pragma: no cover - defensive
        result["errors"].append(f"projection receipt cleanup raised: {exc}")
        result["projection_receipt_removed"] = False

    result["tombstoned"] = True
    result["reason"] = "tombstoned"

    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "tombstone",
            summary=f"memory_id={mid} reason={reason}",
            memory_id=mid,
            archived=result["archived"],
            file_removed=result["file_removed"],
            index_entry_removed=result["index_entry_removed"],
            archive_path=result.get("archive_path"),
            error_count=len(result["errors"]),
        )
    except Exception:
        pass
    return result

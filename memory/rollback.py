"""
Memory canonical-write rollback
=================================

Inverse of `memory.engine.validate_write.atomic_write`. Called by
`memory.encoding` when a post-commit encoding step fails (FLM down,
contradiction precheck refusing identity write, queue I/O failure)
and the doctrine demands no partial-state writes.

Per `project_memory_api_atomic_encoding.md`: every write must reach
ALL 8 atomic encoding steps or roll back fully. The first 2 steps
(canonical commit + index update) are owned by `atomic_write`; the
remaining 6 are owned by `memory.encoding`. If any of those 6 fail,
this module reverses steps 1+2.

Operations:
    1. Delete the canonical memory file at `final_path`.
    2. Remove the entry from `coord_index.json`.

Both are best-effort: if either fails partway, log + report.
A reindex pass can recover index drift; an orphan memory file
(file present but no index entry) is harmless because retrieval
goes through the index. The reverse — index entry pointing at a
deleted file — is marginally worse (consumer attempts to open a
missing path) but reindex repairs it on next run.

Per the rule "don't change the memory to greet the logic": this
module ADDS a reversal capability that the existing memory pipeline
already implies (the rollback dict in commit_promotion already lists
['delete_memory_file', 'remove_index_entry'] as the actions). We are
NOT modifying the memory write path itself.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from core.paths import OMEGA_ROOT
from memory.engine.validate_write import patch_index, unpatch_index

log = logging.getLogger("omega.memory.rollback")


def rollback_canonical_write(
    memory_dict: Dict[str, Any],
    final_path: str,
) -> Dict[str, Any]:
    """Reverse a canonical write that completed atomic_write but whose
    post-commit encoding then failed.

    Returns a dict describing what got undone, for inclusion in the
    failure audit / write receipt.
    """
    memory_id = memory_dict.get("id") or ""
    result: Dict[str, Any] = {
        "memory_id": memory_id,
        "final_path": final_path,
        "memory_file_removed": False,
        "index_entry_removed": False,
        "errors": [],
    }

    # Step 1 — delete the canonical memory file.
    try:
        p = Path(final_path).resolve()
        # Defense: refuse to delete anything outside the memory tree.
        memory_root = (OMEGA_ROOT / "memory").resolve()
        try:
            p.relative_to(memory_root)
        except ValueError:
            msg = f"refusing to delete {p} — outside {memory_root} (path-traversal guard)"
            log.error("rollback: %s", msg)
            result["errors"].append(msg)
        else:
            if p.exists():
                p.unlink()
                result["memory_file_removed"] = True
                log.info("rollback: removed memory file %s", p)
            else:
                log.info("rollback: memory file already absent at %s", p)
    except OSError as exc:
        msg = f"unlink failed: {exc}"
        log.error("rollback: %s", msg)
        result["errors"].append(msg)

    # Step 2 — remove the index entry.
    try:
        if memory_id:
            removed = unpatch_index(memory_id)
            result["index_entry_removed"] = removed
            if removed:
                log.info("rollback: removed index entry for %s", memory_id)
            else:
                log.info("rollback: no index entry to remove for %s", memory_id)
    except Exception as exc:  # pragma: no cover — unpatch_index is defensive
        msg = f"unpatch_index raised: {exc}"
        log.error("rollback: %s", msg)
        result["errors"].append(msg)

    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "rollback",
            summary=f"memory_id={memory_id}",
            memory_id=memory_id,
            memory_file_removed=result["memory_file_removed"],
            index_entry_removed=result["index_entry_removed"],
            error_count=len(result["errors"]),
        )
    except Exception:
        pass
    return result


def restore_canonical_write(
    memory_dict: Dict[str, Any],
    final_path: str,
    prior_bytes: bytes,
) -> Dict[str, Any]:
    """Restore a pre-existing canonical record after a failed RE-WRITE whose
    post-commit encoding failed — instead of deleting it. atomic_write
    overwrites in place with no backup, so without this a failed re-write
    (e.g. an identity-lobe trait reinforce/decay-suspend while the embed
    server is down) would DESTROY the prior committed record. Writes
    `prior_bytes` back to `final_path` (atomic tmp+replace) and re-patches the
    index from the prior content. Best-effort; on failure falls back to the
    delete rollback so a half-written record is never left behind."""
    memory_id = memory_dict.get("id") or ""
    result: Dict[str, Any] = {
        "memory_id": memory_id,
        "final_path": final_path,
        "restored": False,
        "index_repatched": False,
        "errors": [],
    }
    try:
        p = Path(final_path).resolve()
        memory_root = (OMEGA_ROOT / "memory").resolve()
        try:
            p.relative_to(memory_root)
        except ValueError:
            msg = f"refusing to restore {p} — outside {memory_root} (path-traversal guard)"
            log.error("rollback: %s", msg)
            result["errors"].append(msg)
            return result
        tmp = p.with_suffix(p.suffix + ".restore.tmp")
        tmp.write_bytes(prior_bytes)
        tmp.replace(p)
        result["restored"] = True
        log.info("rollback: RESTORED prior canonical record %s (%d bytes)", p, len(prior_bytes))
    except Exception as exc:
        msg = f"restore failed: {exc}"
        log.error("rollback: %s", msg)
        result["errors"].append(msg)
        # Fall back to a clean delete so we never leave a clobbered record.
        try:
            return rollback_canonical_write(memory_dict, final_path)
        except Exception as exc2:
            result["errors"].append(f"fallback delete failed: {exc2}")
            return result
    # Re-patch the index from the prior content (the new write already
    # patched it to the now-reverted content; restore the prior snapshot).
    try:
        prior_meta = json.loads(prior_bytes.decode("utf-8"))
        patch_index(prior_meta)
        result["index_repatched"] = True
    except Exception as exc:
        msg = f"index repatch failed: {exc}"
        log.warning("rollback: %s", msg)
        result["errors"].append(msg)

    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "rollback_restore",
            summary=f"restored={result.get('restored')}",
            memory_id=memory_id,
            restored=result.get("restored"),
            index_repatched=result.get("index_repatched"),
            error_count=len(result["errors"]),
        )
    except Exception:
        pass
    return result

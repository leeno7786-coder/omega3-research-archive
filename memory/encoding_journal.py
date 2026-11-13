"""
Memory atomic-encoding write-ahead journal
============================================

Per `project_memory_api_atomic_encoding.md` + the Omega Cognitive Law:

> "No cognition may operate on unencoded memory. Every Guardian-approved
>  write completes ALL 8 atomic encoding steps OR rolls back."

The 8-step encoding pipeline (`memory.encoding`) handles in-process
failure correctly via `EncodingResult.ok=False` -> caller rolls back.
The remaining gap (audit issue M1) is **process crash mid-encoding**:

    atomic_write done -> kill -9 before encoding completes
    -> canonical file + index entry on disk
    -> no embedding, no SI register, no queue entries
    -> memory is "live" in the index but UNENCODED
    -> doctrine violation: cognition may operate on unencoded memory.

This module closes that window with a write-ahead journal:

    1. `journal_open(memory_id, staging_ref, final_path)`   BEFORE atomic_write
    2. `journal_step(journal_id, memory_id, step_name)`     after each step
    3. `journal_close(journal_id, ...)`                     after success
    4. `journal_abort(journal_id, reason, rollback_summary)` after failure

On boot, `recover_unclosed(rollback_fn)` scans the journal for any
open entry without a matching close/abort. For each one:

    - call rollback_fn(memory_dict_stub, final_path) to undo the
      canonical commit + index entry (idempotent)
    - append an abort entry naming "boot_recovery"

Design choice: **always roll back on recovery, never resume.**
Resuming an interrupted encoding is risky (process state was uncertain
when it crashed; in-flight model-server connections may have leaked
half-formed responses). Rollback is conservative and matches the
doctrine. The lost write becomes a lost write; operator can re-issue.

ARCHITECTURAL RULES:
    - Append-only; entries never edited in place.
    - Every append is fsynced so the entry survives `kill -9`.
    - Recovery is idempotent (rollback ops are idempotent;
      double-rollback is safe; double-abort is safe).
    - The journal does NOT call rollback itself -- caller passes
      `rollback_fn` to `recover_unclosed`. This avoids a circular
      import between memory.encoding_journal and memory.rollback.
    - Compaction is operator-triggered (no automatic file growth
      management at write time -- keep the hot path simple).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set

from core.paths import OMEGA_ROOT

log = logging.getLogger("omega.memory.encoding_journal")


# =====================================================================
# Paths + locking
# =====================================================================

JOURNAL_PATH = OMEGA_ROOT / "guardian" / "state" / "encoding_journal.jsonl"

# Single lock for all journal writes. Appends are short, locks
# uncontended in practice. Cheap insurance against concurrent
# commit_promotion calls landing interleaved bytes.
_lock = threading.Lock()


# =====================================================================
# Internal helpers
# =====================================================================


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _journal_process_lock():
    """Serialize journal mutation across independent Omega/test processes."""
    lock_path = JOURNAL_PATH.with_name(f"{JOURNAL_PATH.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            acquired = False
            for _ in range(200):
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    time.sleep(0.025)
            if not acquired:
                raise TimeoutError(f"timed out acquiring encoding journal lock: {lock_path}")
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _append(entry: Dict[str, Any]) -> bool:
    """Atomic append + fsync. Returns True on success, False on I/O
    failure (caller decides how loud to be)."""
    try:
        JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with _journal_process_lock():
                with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
        return True
    except OSError as exc:
        log.error("encoding_journal append failed: %s", exc)
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "encoding_journal_append",
            status="fail",
            ok=False,
            reason="journal_append_failed",
            error=str(exc),
        )
        return False


# =====================================================================
# Public API — write side
# =====================================================================


CANONICAL_FINALIZED_STEP = "canonical_finalized"


def _prior_snapshot_dir() -> Path:
    return JOURNAL_PATH.parent / "encoding_journal_prior"


def _journal_profile_root() -> Path:
    """Return the Omega profile that owns the active journal.

    The journal is persisted at ``<profile>/guardian/state``.  Resolving the
    profile from that path keeps recovery aligned with the journal it is
    reading, including after the profile is moved to another drive and in
    isolated recovery runs that redirect only ``JOURNAL_PATH``.  Using the
    import-time ``core.paths.OMEGA_ROOT`` here could turn an otherwise valid
    relative canonical path back into the development machine's tree.
    """
    return JOURNAL_PATH.parent.parent.parent.resolve(strict=False)


def _portable_canonical_path(path: str | Path) -> str:
    """Serialize an in-profile canonical path without its machine prefix.

    Journal rows survive a restart and may survive the product being moved to
    another drive.  Canonical memory records are therefore stored relative to
    the active Omega profile.  External paths are retained verbatim because
    the journal API is also used by isolated recovery tests and must not
    reinterpret a path that is not an Omega memory record.
    """
    raw = str(path)
    candidate = Path(raw)
    root = _journal_profile_root()
    if not candidate.is_absolute() and not raw.startswith(("/", "\\")):
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return raw


def _resolve_canonical_path(raw_path: str | Path) -> str:
    """Resolve a journal path against the current profile.

    New rows contain a profile-relative path.  A legacy absolute row is also
    remapped when it clearly names an old profile's ``memory`` tree, which
    lets crash recovery complete after the SSD receives a new drive letter.
    Unknown external paths remain unchanged.
    """
    raw = str(raw_path or "")
    if not raw:
        return ""
    candidate = Path(raw)
    root = _journal_profile_root()
    if not candidate.is_absolute() and not raw.startswith(("/", "\\")):
        return str((root / candidate).resolve(strict=False))

    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        parts = list(candidate.parts)
        for index, part in enumerate(parts):
            if part.rstrip("\\/").casefold() == "memory":
                suffix = Path(*parts[index:])
                return str((root / suffix).resolve(strict=False))
        return raw
    return str(resolved)


def _portable_snapshot_path(path: str | Path) -> str:
    snapshot = Path(path).resolve(strict=False)
    journal_root = JOURNAL_PATH.parent.resolve(strict=False)
    try:
        return snapshot.relative_to(journal_root).as_posix()
    except ValueError:
        return str(path)


def _resolve_snapshot_path(raw_path: str | Path) -> Path:
    raw = str(raw_path or "")
    candidate = Path(raw)
    if not candidate.is_absolute() and not raw.startswith(("/", "\\")):
        return (JOURNAL_PATH.parent / candidate).resolve(strict=False)
    resolved = candidate.resolve(strict=False)
    journal_root = JOURNAL_PATH.parent.resolve(strict=False)
    try:
        resolved.relative_to(journal_root)
    except ValueError:
        parts = list(candidate.parts)
        for index, part in enumerate(parts):
            if part.rstrip("\\/").casefold() == "encoding_journal_prior":
                suffix = Path(*parts[index:])
                return (journal_root / suffix).resolve(strict=False)
        return candidate
    return resolved


def journal_open(
    memory_id: str,
    staging_ref: str,
    final_path: str,
    *,
    prior_snapshot: Optional[bytes] = None,
) -> Optional[str]:
    """Open a journal entry BEFORE atomic_write fires. Returns a
    journal_id used to correlate subsequent step/close/abort entries, or
    ``None`` when the write-ahead state could NOT be made durable — the
    caller MUST NOT enter the crash-vulnerable atomic-write/encoding window
    in that case (CUT4_REVIEW_REWORK_2 §5: a WAL is durable BEFORE the
    destructive step or the step does not happen).

    Caller MUST eventually call `journal_close` or `journal_abort`
    with the returned journal_id; an open entry without a matching
    close/abort is handled by `recover_unclosed` on next boot: rolled back
    (or RESTORED from ``prior_snapshot`` when the write overwrote a prior
    record) if the write never reached canonical finalization, PRESERVED if
    it did (see ``journal_finalize``).

    ``prior_snapshot`` — the bytes of any pre-existing canonical record at
    ``final_path`` (memory-organ Cut 4 rework): persisted + fsynced to a
    sidecar so a crash before finalization can restore the prior record
    instead of deleting it (atomic_write overwrites in place with no backup).
    For an overwrite the old bytes are truth until the replacement finalizes,
    so a snapshot that cannot be persisted refuses the open (returns None).
    """
    journal_id = uuid.uuid4().hex[:16]
    snapshot_path = ""
    if prior_snapshot is not None:
        try:
            d = _prior_snapshot_dir()
            d.mkdir(parents=True, exist_ok=True)
            sp = d / f"{journal_id}.bin"
            with open(sp, "wb") as fh:
                fh.write(prior_snapshot)
                fh.flush()
                os.fsync(fh.fileno())
            snapshot_path = _portable_snapshot_path(sp)
        except Exception as exc:  # noqa: BLE001 — NOT best-effort: refuse the open
            log.error("encoding_journal: prior snapshot NOT persisted for %s (%s) — refusing journal_open", memory_id, exc)
            try:
                from guardian.terminal_feedback import emit_data

                emit_data("memory", "encoding_journal_open", status="fail", ok=False,
                          reason="prior_snapshot_not_persisted", memory_id=memory_id, error=str(exc))
            except Exception:  # noqa: BLE001
                pass
            return None
    ok = _append(
        {
            "op": "open",
            "journal_id": journal_id,
            "memory_id": memory_id,
            "staging_ref": staging_ref,
            "final_path": _portable_canonical_path(final_path),
            "prior_snapshot": snapshot_path,
            "started_at": _iso(),
        }
    )
    if not ok:
        _discard_snapshot(snapshot_path)
        log.error("encoding_journal: open entry NOT durable for %s — refusing journal_open", memory_id)
        return None
    return journal_id


PROJECTION_OBLIGATION_KIND = "projection_obligation"


def journal_open_obligation(memory_id: str, canonical_path: str, owner: str) -> Optional[str]:
    """Memory-organ Cut 4 rework 2: the write OWNER's durable projection
    obligation, opened BEFORE its synchronous canonical-mutating post-commit
    hooks run and closed only after the FINAL projection work is durable (or a
    durable rebuild-required marker is confirmed). An obligation left open by
    a crash makes boot mark projection dirty for that record's profile — the
    hooks may have rewritten the record and peers that no delta represents.
    Returns the journal_id, or None when the entry could not be made durable
    (the caller must then establish durable dirty evidence another way)."""
    journal_id = uuid.uuid4().hex[:16]
    ok = _append(
        {
            "op": "open",
            "kind": PROJECTION_OBLIGATION_KIND,
            "journal_id": journal_id,
            "memory_id": memory_id,
            "staging_ref": "",
            "final_path": _portable_canonical_path(canonical_path),
            "owner": str(owner or ""),
            "prior_snapshot": "",
            "started_at": _iso(),
        }
    )
    return journal_id if ok else None


def journal_finalize(journal_id: str, memory_id: str, mode: str = "native") -> bool:
    """Memory-organ Cut 4 rework: durably record that the CANONICAL write is
    FINAL — encoding complete — before any projection/outbox/cleanup work.
    From this entry on, boot recovery must PRESERVE the canonical record
    (canonical commit clock != projection clock; projection success is never
    part of canonical rollback authority). ``mode`` is 'native' or 'legacy'."""
    return journal_step(journal_id, memory_id, f"{CANONICAL_FINALIZED_STEP}:{mode}")


def journal_step(journal_id: str, memory_id: str, step: str) -> bool:
    """Record a successful encoding step. Best-effort: failure to
    journal a step does NOT fail the encoding (the audit trail just
    misses one entry; the close entry still records the full list)."""
    return _append(
        {
            "op": "step",
            "journal_id": journal_id,
            "memory_id": memory_id,
            "step": step,
            "completed_at": _iso(),
        }
    )


def journal_close(
    journal_id: str,
    memory_id: str,
    steps_completed: List[str],
) -> bool:
    """Close a journal entry after successful encoding. Idempotent:
    a second close on the same journal_id appends another close
    record but recovery treats either as 'closed'. Discards the entry's
    prior-record snapshot sidecar (no longer needed once closed)."""
    ok = _append(
        {
            "op": "close",
            "journal_id": journal_id,
            "memory_id": memory_id,
            "steps_completed": list(steps_completed),
            "closed_at": _iso(),
        }
    )
    if ok:
        _discard_snapshot(str(_prior_snapshot_dir() / f"{journal_id}.bin"))
    return ok


def journal_abort(
    journal_id: str,
    memory_id: str,
    reason: str,
    rollback_summary: Optional[Dict[str, Any]] = None,
) -> bool:
    """Abort a journal entry after rollback completes. Idempotent.
    Discards the entry's prior-record snapshot sidecar (the rollback/restore
    that precedes an abort has already consumed it)."""
    ok = _append(
        {
            "op": "abort",
            "journal_id": journal_id,
            "memory_id": memory_id,
            "reason": reason,
            "rollback": rollback_summary or {},
            "aborted_at": _iso(),
        }
    )
    if ok:
        _discard_snapshot(str(_prior_snapshot_dir() / f"{journal_id}.bin"))
    return ok


# =====================================================================
# Public API — read / recovery side
# =====================================================================


def _scan_journal(*, warn_malformed: bool) -> tuple[List[Dict[str, Any]], List[int], str]:
    """Return unclosed entries, malformed line numbers, and any scan error."""
    if not JOURNAL_PATH.exists():
        return [], [], ""

    opens: Dict[str, Dict[str, Any]] = {}
    last_step: Dict[str, str] = {}
    closed_or_aborted: Set[str] = set()
    malformed_lines: List[int] = []

    try:
        with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
            for line_no, raw in enumerate(f, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    malformed_lines.append(line_no)
                    if warn_malformed:
                        log.warning(
                            "encoding_journal line %d not valid JSON; skipping",
                            line_no,
                        )
                    continue

                op = entry.get("op")
                jid = entry.get("journal_id")
                if not jid:
                    continue

                if op == "open":
                    opens[jid] = entry
                elif op == "step":
                    last_step[jid] = entry.get("step", "") or ""
                elif op in ("close", "abort"):
                    closed_or_aborted.add(jid)
    except OSError as exc:
        log.error("encoding_journal scan failed: %s", exc)
        return [], malformed_lines, f"{type(exc).__name__}: {exc}"

    unclosed: List[Dict[str, Any]] = []
    for jid, open_entry in opens.items():
        if jid in closed_or_aborted:
            continue
        unclosed.append(
            {
                **open_entry,
                "last_step_completed": last_step.get(jid),
            }
        )

    return unclosed, malformed_lines, ""


def find_unclosed_entries() -> List[Dict[str, Any]]:
    """Return open entries without a matching close/abort for boot recovery."""
    unclosed, _malformed, _error = _scan_journal(warn_malformed=True)
    return unclosed


def journal_status() -> Dict[str, Any]:
    """Read-only health snapshot used by HUD polling without log spam."""
    unclosed, malformed_lines, error = _scan_journal(warn_malformed=False)
    status: Dict[str, Any] = {
        "clean": not unclosed and not malformed_lines and not error,
        "unclosed": unclosed,
        "unclosed_count": len(unclosed),
        "malformed_count": len(malformed_lines),
        "malformed_lines": malformed_lines,
    }
    if error:
        status["error"] = error
    return status


def recover_unclosed(
    rollback_fn: Callable[[Dict[str, Any], str], Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Boot recovery hook. For every unclosed journal entry, call
    rollback_fn(memory_dict_stub, final_path) and append an abort
    entry naming "boot_recovery".

    `rollback_fn` is typically `memory.rollback.rollback_canonical_write`
    (passed as an argument to avoid circular imports). It receives a
    minimal `{"id": memory_id}` stub plus the final_path recorded at
    journal_open time -- enough for rollback to delete the file +
    unpatch the index.

    Returns one result dict per recovered entry:
        {"memory_id": ..., "rollback": <rollback_summary>, "journal_id": ...}
    or
        {"memory_id": ..., "error": "...", "journal_id": ...}

    Idempotent: re-running recover_unclosed after a partial recovery
    just rolls back + aborts the still-unclosed entries again
    (rollback_fn must be idempotent, which rollback_canonical_write
    is by design -- second delete is no-op, second unpatch is no-op).
    """
    unclosed = find_unclosed_entries()
    results: List[Dict[str, Any]] = []

    for entry in unclosed:
        memory_id = entry.get("memory_id", "")
        final_path = _resolve_canonical_path(entry.get("final_path", ""))
        journal_id = entry.get("journal_id", "")
        last_step = entry.get("last_step_completed")
        snapshot_path = _resolve_snapshot_path(entry.get("prior_snapshot") or "")

        # ── Cut 4 rework 2: the owner's projection obligation → canonical is
        # truth (it was finalized by its own commit entry); the hooks may have
        # rewritten it and peers no delta represents → durable rebuild-required
        if str(entry.get("kind") or "") == PROJECTION_OBLIGATION_KIND:
            log.warning(
                "encoding_journal recovery: open projection obligation journal_id=%s memory_id=%s "
                "(owner=%s) -- marking projection dirty",
                journal_id, memory_id, entry.get("owner"),
            )
            results.append(_reconcile_obligation(journal_id, memory_id, final_path))
            continue

        # ── Cut 4 rework: canonical finalized → PRESERVE, never delete ──
        if str(last_step or "").startswith(CANONICAL_FINALIZED_STEP):
            mode = str(last_step).split(":", 1)[1] if ":" in str(last_step) else "native"
            log.warning(
                "encoding_journal recovery: unclosed entry journal_id=%s memory_id=%s "
                "reached %s -- preserving canonical, reconciling projection",
                journal_id, memory_id, last_step,
            )
            result = _reconcile_finalized(journal_id, memory_id, final_path, mode)
            if not result.get("retry"):
                _discard_snapshot(snapshot_path)
            results.append(result)
            continue

        log.warning(
            "encoding_journal recovery: unclosed entry journal_id=%s "
            "memory_id=%s last_step=%s -- rolling back",
            journal_id,
            memory_id,
            last_step,
        )

        try:
            prior_bytes = _read_snapshot(str(snapshot_path))
            if prior_bytes is not None:
                # the write overwrote a previously committed record and died
                # before finalization: RESTORE that record instead of deleting
                from memory.rollback import restore_canonical_write

                rb_summary = restore_canonical_write({"id": memory_id}, final_path, prior_bytes)
                rb_summary = {**dict(rb_summary or {}), "restored": True}
            else:
                rb_summary = rollback_fn({"id": memory_id}, final_path)
            _discard_snapshot(str(snapshot_path))
            journal_abort(
                journal_id,
                memory_id,
                f"boot_recovery: unclosed entry from prior session (last_step={last_step})",
                rb_summary,
            )
            results.append(
                {
                    "journal_id": journal_id,
                    "memory_id": memory_id,
                    "rollback": rb_summary,
                    "restored": bool(isinstance(rb_summary, dict) and rb_summary.get("restored")),
                }
            )
        except Exception as exc:
            log.error(
                "encoding_journal recovery: rollback raised for memory_id=%s: %s",
                memory_id,
                exc,
            )
            from guardian.terminal_feedback import emit_data

            emit_data(
                "memory",
                "encoding_journal_recovery",
                status="fail",
                ok=False,
                reason="rollback_failed",
                memory_id=memory_id,
                error=str(exc),
            )
            # Don't abort the journal entry on rollback failure --
            # next boot will see it again + retry.
            results.append(
                {
                    "journal_id": journal_id,
                    "memory_id": memory_id,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    return results


def _read_snapshot(snapshot_path: str) -> Optional[bytes]:
    if not snapshot_path:
        return None
    try:
        return _resolve_snapshot_path(snapshot_path).read_bytes()
    except OSError:
        return None


def _discard_snapshot(snapshot_path: str) -> None:
    if not snapshot_path:
        return
    try:
        _resolve_snapshot_path(snapshot_path).unlink(missing_ok=True)
    except OSError:
        pass


def _reconcile_finalized(journal_id: str, memory_id: str, final_path: str, mode: str) -> Dict[str, Any]:
    """A finalized-but-unclosed entry: the canonical write is truth. Native
    mode: if the LATEST outbox delta for this memory_id hashes the file's
    current bytes the entry is simply closed; otherwise the durable
    projection-dirty (rebuild-required) marker is set in the record's own
    profile — and ONLY when that marker is confirmed durable is the entry
    closed. If neither durable projection work nor a durable dirty marker
    can be established the entry stays OPEN so the next boot retries
    (CUT4_REVIEW_REWORK_2 §6: the finalized entry is itself the recovery
    evidence). Legacy mode: close (its pending-embed sweep is its recovery)."""
    result: Dict[str, Any] = {"journal_id": journal_id, "memory_id": memory_id, "preserved": True, "mode": mode,
                              "projection_dirty": False}
    try:
        exists = bool(final_path) and Path(final_path).exists()
        result["canonical_exists"] = exists
        if mode != "legacy":
            import hashlib

            from memory.projection_delta import mark_projection_dirty, pending_projection_deltas
            from memory.projection_receipt import memory_root_for_canonical_path

            root = memory_root_for_canonical_path(final_path)
            covered = False
            if exists:
                current = hashlib.sha256(Path(final_path).read_bytes()).hexdigest()
                deltas, _malformed = pending_projection_deltas(root=root)
                latest = None
                for d in deltas:
                    if d.memory_id == memory_id:
                        latest = d
                covered = latest is not None and latest.canonical_hash == current
            else:
                result["error"] = "finalized entry but canonical file missing"
            if not covered:
                durable = mark_projection_dirty(
                    reason="crash after canonical finalization before projection work was durable",
                    memory_id=memory_id, root=root,
                )
                if not durable:
                    # no durable evidence could be established → keep the entry
                    # open; the next boot sees it again and retries
                    result["retry"] = True
                    result["error"] = "projection dirty marker not durable; journal entry left open for retry"
                    log.error("encoding_journal recovery: %s (memory_id=%s)", result["error"], memory_id)
                    return result
                result["projection_dirty"] = True
        journal_close(journal_id, memory_id, [f"boot_recovery:preserved:{mode}"])
    except Exception as exc:  # noqa: BLE001
        log.error("encoding_journal recovery: reconcile failed for %s: %s", memory_id, exc)
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["retry"] = True
    return result


def _reconcile_obligation(journal_id: str, memory_id: str, final_path: str) -> Dict[str, Any]:
    """An open projection obligation: the owner died somewhere between
    'about to run canonical-mutating hooks' and 'final projection work
    durable'. Peer mutations are not enumerable after the fact, so the only
    truthful posture is a durable rebuild-required marker in the record's
    profile; the entry closes ONLY once that marker is confirmed durable."""
    result: Dict[str, Any] = {"journal_id": journal_id, "memory_id": memory_id, "preserved": True,
                              "obligation": True, "projection_dirty": False}
    try:
        from memory.projection_delta import mark_projection_dirty
        from memory.projection_receipt import memory_root_for_canonical_path

        root = memory_root_for_canonical_path(final_path) if final_path else None
        durable = mark_projection_dirty(
            reason="crash inside the write owner's post-commit hook / final projection window; "
                   "hook peer mutations unknown",
            memory_id=memory_id, root=root,
        )
        if not durable:
            result["retry"] = True
            result["error"] = "projection dirty marker not durable; obligation left open for retry"
            log.error("encoding_journal recovery: %s (memory_id=%s)", result["error"], memory_id)
            return result
        result["projection_dirty"] = True
        journal_close(journal_id, memory_id, ["boot_recovery:projection_obligation:dirty"])
    except Exception as exc:  # noqa: BLE001
        log.error("encoding_journal recovery: obligation reconcile failed for %s: %s", memory_id, exc)
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["retry"] = True
    return result


# =====================================================================
# Operator tooling — compaction
# =====================================================================


def quarantine_malformed_entries() -> Dict[str, Any]:
    """Preserve malformed rows in a sidecar, then atomically remove them.

    A torn JSONL row cannot participate in recovery, but silently deleting it
    would erase the only surviving forensic bytes.  This operator repair keeps
    those bytes with their original line numbers before rewriting the journal
    to its valid entries.
    """
    if not JOURNAL_PATH.exists():
        return {"kept": 0, "malformed": 0, "quarantine": ""}

    try:
        with _lock:
            with _journal_process_lock():
                raw_lines = JOURNAL_PATH.read_text(encoding="utf-8").splitlines()
                valid_lines: List[str] = []
                malformed: List[Dict[str, Any]] = []
                for line_no, raw in enumerate(raw_lines, start=1):
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        json.loads(line)
                    except json.JSONDecodeError:
                        malformed.append({"line": line_no, "raw": raw})
                    else:
                        valid_lines.append(line)

                if not malformed:
                    return {
                        "kept": len(valid_lines),
                        "malformed": 0,
                        "quarantine": "",
                    }

                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                quarantine = JOURNAL_PATH.with_name(
                    f"{JOURNAL_PATH.stem}.malformed-{stamp}.jsonl"
                )
                quarantine_tmp = quarantine.with_suffix(".jsonl.tmp")
                with open(quarantine_tmp, "w", encoding="utf-8") as handle:
                    for item in malformed:
                        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                quarantine_tmp.replace(quarantine)

                journal_tmp = JOURNAL_PATH.with_suffix(".jsonl.repair.tmp")
                with open(journal_tmp, "w", encoding="utf-8") as handle:
                    for line in valid_lines:
                        handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                journal_tmp.replace(JOURNAL_PATH)
    except OSError as exc:
        log.error("encoding_journal malformed-row repair failed: %s", exc)
        return {
            "kept": 0,
            "malformed": 0,
            "quarantine": "",
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {
        "kept": len(valid_lines),
        "malformed": len(malformed),
        "quarantine": quarantine.name,
    }


def compact_journal(keep_recent_seconds: int = 7 * 86400) -> Dict[str, int]:
    """Drop close+abort entries older than `keep_recent_seconds`, and drop
    matching open+step lines for the same journal_id so compaction cannot
    orphan a terminal close while retaining its open (audit HIGH-1).

    Unclosed journal_ids (open with no close/abort) are always kept even
    when stale — boot recovery still needs them.

    NOT called automatically on the hot path. An operator script
    runs this periodically; a 7-day default keeps a week of audit
    trail by default.

    Returns {"kept": N, "dropped": M, "total_before": K}.
    """
    if not JOURNAL_PATH.exists():
        return {"kept": 0, "dropped": 0, "total_before": 0}

    cutoff_iso = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - keep_recent_seconds,
        tz=timezone.utc,
    ).isoformat()

    parsed: List[tuple[str, Dict[str, Any]]] = []
    total = 0

    try:
        with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                total += 1
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    parsed.append((line, {}))
                    continue
                parsed.append((line, entry))
    except OSError as exc:
        log.error("encoding_journal compact_journal scan failed: %s", exc)
        return {"kept": 0, "dropped": 0, "total_before": 0, "error": str(exc)}

    # Pass 1: journal_ids whose terminal close/abort is older than cutoff.
    drop_journal_ids: Set[str] = set()
    for _line, entry in parsed:
        if not entry:
            continue
        op = entry.get("op", "")
        jid = entry.get("journal_id")
        if not jid or op not in ("close", "abort"):
            continue
        ts = entry.get("closed_at") or entry.get("aborted_at") or ""
        if ts and ts < cutoff_iso:
            drop_journal_ids.add(jid)

    kept: List[str] = []
    dropped = 0

    for line, entry in parsed:
        if not entry:
            kept.append(line)
            continue

        op = entry.get("op", "")
        jid = entry.get("journal_id", "")

        if op in ("open", "step"):
            if jid and jid in drop_journal_ids:
                dropped += 1
                continue
            kept.append(line)
            continue

        if op in ("close", "abort"):
            ts = entry.get("closed_at") or entry.get("aborted_at") or ""
            if ts and ts >= cutoff_iso:
                kept.append(line)
            else:
                dropped += 1
            continue

        kept.append(line)

    if dropped == 0:
        return {"kept": total, "dropped": 0, "total_before": total}

    # Atomic rewrite: tmp + rename.
    try:
        tmp = JOURNAL_PATH.with_suffix(".jsonl.tmp")
        with _lock:
            with _journal_process_lock():
                with open(tmp, "w", encoding="utf-8") as f:
                    for line in kept:
                        f.write(line + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                tmp.replace(JOURNAL_PATH)
    except OSError as exc:
        log.error("encoding_journal compact_journal rewrite failed: %s", exc)
        return {"kept": total, "dropped": 0, "total_before": total, "error": str(exc)}

    log.info(
        "encoding_journal compaction: kept=%d dropped=%d total_before=%d",
        len(kept),
        dropped,
        total,
    )
    return {"kept": len(kept), "dropped": dropped, "total_before": total}

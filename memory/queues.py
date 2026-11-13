"""
Memory candidacy queues — append-only jsonl drains
====================================================

Two append-only queues are written at memory write time as part of
the atomic L0 encoding (per `project_memory_api_atomic_encoding.md`,
steps 7 + 8):

    reflection_eligible_queue  — items L11 (Reflection) considers when
                                  it interprets "what does this mean?"
    deepsleep_candidate_queue  — items L12 (DeepSleep) considers when
                                  it consolidates traits / tensions /
                                  emerging skills.

These are SIDECARS, not extensions of the memory schema. Per the user
directive: "don't change the memory to greet the logic, change the
logic to embrace the memory." The queues live next to memory; memory
records themselves stay schema-clean.

Format (one line per entry):

    {"memory_id": "<id>",
     "coord":     [lobe, chapter, leaf],
     "queued_at": "<iso>",
     "reason":    "<short why-it-was-queued>"}

Drain pattern: the consumer engine reads the queue, processes
entries, and either truncates the queue file (if it processed
everything) or writes a new file with the unprocessed tail. Either
strategy keeps the file durable and replayable across restarts —
which is the persistence invariant from the graceful-drain doctrine.

ARCHITECTURAL RULES:
    - Append-only at write time. Atomic file appends only — fsync
      after each append so the entry survives `kill -9`.
    - Drain mechanics (read + atomic-truncate-or-rewrite-tail) live
      here under the same lock as the appends — the file format is
      this module's concern. WHAT the drained entries mean +
      whether/how to act on them lives in the consuming engine
      (DeepSleep today; a future Reflection layer when one exists).
    - Never crash the caller. If the queue can't be written, log a
      warning and return False; the encoding step deciding whether
      to roll back inspects the bool. Drain is similarly best-effort
      — on read failure it logs + returns an empty list rather than
      raising up to the consuming loop.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.paths import OMEGA_ROOT

log = logging.getLogger("omega.memory.queues")


_QUEUES_DIR = OMEGA_ROOT / "guardian" / "state" / "queues"
REFLECTION_QUEUE_PATH = _QUEUES_DIR / "reflection_eligible.jsonl"
DEEPSLEEP_QUEUE_PATH = _QUEUES_DIR / "deepsleep_candidates.jsonl"

# One lock per queue file — appends are short, locks are uncontended in
# practice but cheap insurance against concurrent writers.
_locks: Dict[str, threading.Lock] = {}
_group_local = threading.local()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    if key not in _locks:
        _locks[key] = threading.Lock()
    return _locks[key]


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: Path, entry: Dict[str, Any]) -> bool:
    """Atomic append-then-fsync to a jsonl queue. Returns True on
    success, False on any I/O failure (caller decides rollback)."""
    buffers = getattr(_group_local, "buffers", None)
    if buffers is not None:
        buffers.setdefault(path, []).append(dict(entry))
        return True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock_for(path), open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return True
    except OSError as exc:
        log.warning("queues: append to %s failed: %s", path.name, exc)
        return False


def _flush_grouped_entries(path: Path, entries: List[Dict[str, Any]]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock_for(path), open(path, "a", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except OSError as exc:
        log.warning("queues: grouped append to %s failed: %s", path.name, exc)
        return False


@contextmanager
def group_queue_writes():
    """Buffer derived queue entries and fsync each queue once on group exit."""
    if getattr(_group_local, "buffers", None) is not None:
        yield
        return
    buffers: Dict[Path, List[Dict[str, Any]]] = {}
    _group_local.buffers = buffers
    body_failed = False
    try:
        yield
    except BaseException:
        body_failed = True
        raise
    finally:
        delattr(_group_local, "buffers")
        failed = [
            path
            for path, entries in buffers.items()
            if entries and not _flush_grouped_entries(path, entries)
        ]
        if DEEPSLEEP_QUEUE_PATH in failed and not body_failed:
            raise RuntimeError("grouped deepsleep candidate queue flush failed")


def enqueue_reflection_eligible(
    memory_id: str,
    coord: List[int],
    *,
    reason: str = "post_commit_eligibility",
) -> bool:
    """Add a memory to the L11 reflection-eligible drain. Returns True
    on durable append, False on failure."""
    if not memory_id:
        return False
    ok = _append_jsonl(
        REFLECTION_QUEUE_PATH,
        {
            "memory_id": memory_id,
            "coord": list(coord or []),
            "queued_at": _iso(),
            "reason": reason,
        },
    )
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "enqueue_reflection",
            summary=f"memory_id={memory_id}",
            memory_id=memory_id,
            coord=list(coord or []),
            ok=ok,
        )
    except Exception:
        pass
    return ok


def enqueue_deepsleep_candidate(
    memory_id: str,
    coord: List[int],
    *,
    reason: str = "post_commit_candidacy",
) -> bool:
    """Add a memory to the DeepSleep consolidation candidate drain.
    Returns True on durable append, False on failure."""
    if not memory_id:
        return False
    ok = _append_jsonl(
        DEEPSLEEP_QUEUE_PATH,
        {
            "memory_id": memory_id,
            "coord": list(coord or []),
            "queued_at": _iso(),
            "reason": reason,
        },
    )
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "enqueue_deepsleep",
            summary=f"memory_id={memory_id}",
            memory_id=memory_id,
            coord=list(coord or []),
            ok=ok,
            reason=reason,
        )
    except Exception:
        pass
    return ok


# =====================================================================
# Drain mechanics — atomic read + truncate/rewrite under the same lock
# =====================================================================


def drain_jsonl(
    path: Path,
    max_items: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Drain (read + remove) entries from a jsonl queue file.

    Atomically reads the file under the same lock writers use, then
    either truncates it (full drain) or rewrites it with the
    unprocessed tail (partial drain). Per the doctrine, the queue
    file remains present after drain so subsequent enqueues find it.

    Args:
        path:       the queue file to drain.
        max_items:  when set, drain at most this many entries (the
                    tail stays in the file). When None / <=0, drain
                    everything.

    Returns:
        list of decoded entries in queue (FIFO) order. Malformed
        lines are logged + skipped (best-effort drain — the queue
        should never block the consuming loop because of a corrupt
        line).

    Never raises on I/O failure — logs + returns whatever was
    parsed before the failure (or [] if nothing was parsed). The
    consuming loop's job is to make progress, not to debug disk.
    """
    if not path.exists():
        return []

    drained: List[Dict[str, Any]] = []
    tail_lines: List[str] = []
    n_max = int(max_items) if (max_items is not None and max_items > 0) else None

    try:
        with _lock_for(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw_lines = f.readlines()
            except OSError as exc:
                log.warning("queues: drain read of %s failed: %s", path.name, exc)
                return drained

            for idx, line in enumerate(raw_lines):
                stripped = line.strip()
                if not stripped:
                    continue
                if n_max is not None and len(drained) >= n_max:
                    # Hit the partial-drain cap: keep this and any
                    # subsequent line in the tail (preserve original
                    # formatting incl. trailing newline).
                    tail_lines.append(line if line.endswith("\n") else line + "\n")
                    continue
                try:
                    drained.append(json.loads(stripped))
                except (json.JSONDecodeError, ValueError) as exc:
                    log.warning(
                        "queues: malformed line in %s (line %d, skipping): %s",
                        path.name,
                        idx + 1,
                        exc,
                    )
                    continue

            # Rewrite the file with the unprocessed tail (or empty on
            # full drain). Open with "w" so the file truncates atomically.
            try:
                with open(path, "w", encoding="utf-8") as f:
                    if tail_lines:
                        f.writelines(tail_lines)
                    f.flush()
                    os.fsync(f.fileno())
            except OSError as exc:
                # The drained entries are already lost from the file's
                # view — we can't safely undo. Log loudly so an operator
                # can investigate; the caller still gets the entries.
                log.error(
                    "queues: drain rewrite of %s failed AFTER read: %s "
                    "(returning %d drained entries; tail of %d may have "
                    "been lost)",
                    path.name,
                    exc,
                    len(drained),
                    len(tail_lines),
                )
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("queues: drain of %s raised unexpectedly: %s", path.name, exc)

    return drained


def drain_reflection_eligible(
    max_items: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Drain the reflection-eligible queue. Same semantics as
    `drain_jsonl(REFLECTION_QUEUE_PATH, max_items)`."""
    return drain_jsonl(REFLECTION_QUEUE_PATH, max_items=max_items)


def drain_deepsleep_candidates(
    max_items: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Drain the DeepSleep-candidate queue. Same semantics as
    `drain_jsonl(DEEPSLEEP_QUEUE_PATH, max_items)`."""
    return drain_jsonl(DEEPSLEEP_QUEUE_PATH, max_items=max_items)


def take_deepsleep_candidates_where(
    match: Callable[[Dict[str, Any]], bool],
) -> List[Dict[str, Any]]:
    """Remove entries matching `match` from the queue; retain all others."""
    path = DEEPSLEEP_QUEUE_PATH
    taken: List[Dict[str, Any]] = []
    retained_lines: List[str] = []

    try:
        with _lock_for(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw_lines = f.readlines()
            except OSError as exc:
                log.warning("queues: take read of %s failed: %s", path.name, exc)
                return taken

            for idx, line in enumerate(raw_lines):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except (json.JSONDecodeError, ValueError) as exc:
                    log.warning(
                        "queues: malformed line in %s (line %d, keeping): %s",
                        path.name,
                        idx + 1,
                        exc,
                    )
                    retained_lines.append(line if line.endswith("\n") else line + "\n")
                    continue
                if match(entry):
                    taken.append(entry)
                else:
                    retained_lines.append(line if line.endswith("\n") else line + "\n")

            try:
                with open(path, "w", encoding="utf-8") as f:
                    if retained_lines:
                        f.writelines(retained_lines)
                    f.flush()
                    os.fsync(f.fileno())
            except OSError as exc:
                log.error(
                    "queues: take rewrite of %s failed AFTER read: %s",
                    path.name,
                    exc,
                )
    except Exception as exc:  # pragma: no cover
        log.warning("queues: take of %s raised unexpectedly: %s", path.name, exc)

    return taken


def compact_jsonl_queue(path: Path, *, keep_max_lines: int = 5000) -> int:
    """Best-effort tail retention for append-only queues (audit LOW-11)."""
    keep = max(100, int(keep_max_lines))
    try:
        with _lock_for(path):
            if not path.is_file():
                return 0
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) <= keep:
                return 0
            tail = lines[-keep:]
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text("\n".join(tail) + "\n", encoding="utf-8")
            tmp.replace(path)
            return len(lines) - keep
    except Exception as exc:
        log.warning("queues: compact_jsonl_queue failed for %s: %s", path.name, exc)
        return 0


def compact_reflection_eligible(*, keep_max_lines: int = 5000) -> int:
    return compact_jsonl_queue(REFLECTION_QUEUE_PATH, keep_max_lines=keep_max_lines)


# =====================================================================
# Diagnostic
# =====================================================================


def queue_status() -> Dict[str, Any]:
    """Diagnostic — return per-queue line counts. Cheap; reads file
    only to count newlines."""
    out: Dict[str, Any] = {}
    for label, path in (
        ("reflection_eligible", REFLECTION_QUEUE_PATH),
        ("deepsleep_candidates", DEEPSLEEP_QUEUE_PATH),
    ):
        if not path.exists():
            out[label] = {"path": str(path), "exists": False, "lines": 0}
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                count = sum(1 for _ in f)
            out[label] = {"path": str(path), "exists": True, "lines": count}
        except OSError as exc:
            out[label] = {"path": str(path), "exists": True, "error": str(exc)}
    return out

"""
Curiosity Open-Questions Store
================================

Per `docs/contracts/03_curiosity_development.md` § 12 (Curiosity
Engine v3) and the associated TaskList task #10: every detected
incompleteness is persisted as a `CuriosityEngineEntry` so that
future cycles, DeepSleep consolidation, and the Development Engine
can read it.

The store lives under:

    memory/000_identity/omega/open_questions/<curiosity_id>.json

Lobe 0 (identity), subdomain (0, 6) "omega" — Omega's question stream
IS Omega: what it's chasing, what it noticed, what it deferred. Moved
here from `memory/600_journal/open_questions/` 2026-05-14 because the
journal lobe is restricted to DeepSleep writes only; curiosity is not
journal content. The store is intentionally simple file-per-entry
JSON — ad-hoc tooling (`ls memory/000_identity/omega/open_questions`,
`grep`) can answer "what's currently open" without booting Omega.

Lifecycle:
    1. Curiosity Engine produces a CuriosityEngineEntry (status="open")
    2. `persist(entry)` writes it
    3. When pursued, `mark_active(curiosity_id)` flips status
    4. When resolved, `mark_resolved(...)` flips status + writes a
       short resolution summary
    5. When deferred, `mark_deferred(...)` flips status + writes
       defer_reason
    6. DeepSleep consolidation may `archive(curiosity_id)` after
       the entry has been resolved long enough

Used by:
    - core.curiosity_engine            (canonical writer)
    - autonomous loop                   (consumer for proactive
                                         activation per autonomous_activation
                                         schema)
    - development engine                (consumer for failure-pattern
                                         detection)
    - DeepSleep                         (consumer for consolidation)

ARCHITECTURAL RULES:
    - The store is APPEND-ONLY in spirit. Status updates rewrite the
      entry's file, but the entry's `created_at` + `curiosity_id`
      are immutable.
    - `persist()` enforces curiosity.yaml :: limits.max_parallel_curiosity_threads
      at the canonical write path (new live entries only; rewrites pass).
      `count_open()` is the counter it consults.
    - Schema validity is enforced by `schema.curiosity_engine.
      CuriosityEngineEntry` (frozen dataclass); we do not reach
      around it.
    - This module MUST NOT be redefined elsewhere.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, replace
from pathlib import Path
from typing import List, Optional

from core.paths import OMEGA_ROOT
from schema.curiosity_engine import CuriosityEngineEntry, CuriosityStatus
from guardian.terminal_feedback import phase_span

log = logging.getLogger("omega.hud.curiosity_store")

_STORE_DIR = OMEGA_ROOT / "memory" / "000_identity" / "omega" / "open_questions"
# Reentrant so update_status / mark_failed_retry can hold the lock
# across the load → replace → persist triple while persist itself
# re-acquires for the file write. Without RLock, those wrappers had
# a read-modify-write race: two threads doing concurrent
# mark_active / mark_failed_retry on the same entry would both load
# the same base, both apply their mutation independently, and the
# second persist would overwrite the first — silently losing the
# attempt_count increment.
_lock = threading.RLock()

# Status values for which "the question is still being chased."
# Used by count_open() and list_open(). Per the schema's CuriosityStatus
# Literal: open | active | resolved | deferred — we treat the first
# two as "live" and the latter two as "closed."
_LIVE_STATUSES: frozenset = frozenset({"open", "active"})


# =====================================================================
# Internal helpers
# =====================================================================


def _now_iso() -> str:
    # Phase 2 §P2-S20: curiosity store persistence timestamps route
    # through the temporal authority (pressure_field reads these for
    # evidence-terminal age math).
    from core.symphony_time import now_iso

    return now_iso()


def _path_for(curiosity_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in curiosity_id)
    if not safe:
        raise ValueError(f"curiosity_id sanitizes to empty: {curiosity_id!r}")
    return _STORE_DIR / f"{safe}.json"


def _ensure_dir() -> None:
    _STORE_DIR.mkdir(parents=True, exist_ok=True)


def _serialize(entry: CuriosityEngineEntry) -> dict:
    """Dataclass → JSON-friendly dict. Wraps `asdict` so future
    schema changes go through this single seam.
    """
    return asdict(entry)


def _deserialize(blob: dict) -> CuriosityEngineEntry:
    """Dict → CuriosityEngineEntry. Rejects records the schema can't
    accept (typos in Literal fields, missing required fields).
    """
    return CuriosityEngineEntry(**blob)


# =====================================================================
# Write API
# =====================================================================


def _max_parallel_threads() -> int:
    """The parallel-pursuit cap from config/curiosity.yaml (governed load —
    schema-validated). Fail-soft to the doctrine default 3 when the config is
    unreadable: a broken config must not brick the store, but the cap always
    exists."""
    try:
        from config.loader import load_curiosity_config

        return int(load_curiosity_config()["limits"]["max_parallel_curiosity_threads"])
    except Exception:
        return 3


def persist(entry: CuriosityEngineEntry) -> str:
    """Write the entry to its canonical path. Overwrites if the
    `curiosity_id` already exists (status updates flow through this
    same path). Returns the absolute file path written.

    Raises:
        ValueError: If entry.curiosity_id is empty, or if the entry is NEW and
                    the store is already at curiosity.yaml ::
                    limits.max_parallel_curiosity_threads open entries
                    ("Infinite exploration is failure" — the cap is enforced
                    HERE at the canonical write path, wired 2026-07-04; it was
                    previously only claimed in docstrings).
        OSError:    If the write itself fails.
    """
    with phase_span(
        "curiosity", "persist", curiosity_id=entry.curiosity_id, cycle_id=entry.cycle_id
    ) as span:
        if not entry.curiosity_id:
            raise ValueError("CuriosityEngineEntry.curiosity_id is required")
        _ensure_dir()
        path = _path_for(entry.curiosity_id)
        # Parallel-pursuit cap: applies to NEW entries only — a rewrite of an
        # existing id (status updates flow through this same path) must never
        # be blocked, or open entries could not be resolved at the cap.
        if not path.is_file() and entry.status in _LIVE_STATUSES:
            if count_open() >= _max_parallel_threads():
                raise ValueError(
                    f"curiosity store at max_parallel_curiosity_threads="
                    f"{_max_parallel_threads()} open entries — resolve or "
                    f"defer an open pursuit before creating {entry.curiosity_id!r}"
                )
        blob = _serialize(entry)
        line = json.dumps(blob, ensure_ascii=False, indent=2)
        with _lock:
            # Write to tmp then rename — same atomic-write discipline used
            # by the memory cortex elsewhere.
            tmp = path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(line)
                f.flush()
            tmp.replace(path)
        span.set(
            trigger_source=entry.trigger_source,
            gap_type=entry.gap_type,
            recommended_path=entry.recommended_path,
            entry_status=entry.status,
            tension_score=float(entry.tension_score),
            urgency_score=float(entry.urgency_score),
        )
        return str(path)


def update_status(
    curiosity_id: str,
    new_status: CuriosityStatus,
    *,
    note: Optional[str] = None,
) -> CuriosityEngineEntry:
    """Load the entry, replace status (and optional ignore_reason
    annotation for deferred), persist back, return the new entry.

    Raises:
        FileNotFoundError: If no entry exists for curiosity_id.
    """
    with _lock:
        entry = load(curiosity_id)
        if entry is None:
            raise FileNotFoundError(f"CuriosityEngineEntry {curiosity_id!r} not found in store")
    with phase_span(
        "curiosity",
        "status_transition",
        curiosity_id=curiosity_id,
        new_status=new_status,
        cycle_id=entry.cycle_id,
    ) as span:
        # Hold the module RLock across the full load → replace → persist
        # triple so concurrent status mutations on the same entry don't
        # interleave: two threads (e.g., mark_active + mark_failed_retry)
        # would otherwise both read the same base, mutate independently,
        # and the second persist would silently clobber the first.
        with _lock:
            entry = load(curiosity_id)
            if entry is None:
                raise FileNotFoundError(f"CuriosityEngineEntry {curiosity_id!r} not found in store")
            # `replace` returns a new frozen dataclass; we rewrite ignore_reason
            # only when the caller supplied one to avoid clobbering existing
            # justifications.
            fields = {"status": new_status}
            if note is not None:
                fields["ignore_reason"] = note
            new_entry = replace(entry, **fields)
            persist(new_entry)
            span.set(
                prev_status=entry.status,
                new_status=new_status,
                note=note[:80] if note else None,
            )
            return new_entry


def mark_active(curiosity_id: str) -> CuriosityEngineEntry:
    """Curiosity has been picked up by the Autonomous Loop."""
    entry = load(curiosity_id)
    with phase_span(
        "curiosity",
        "mark_active",
        curiosity_id=curiosity_id,
        cycle_id=entry.cycle_id if entry else "",
    ) as span:
        result = update_status(curiosity_id, "active")
        span.set(new_status=result.status)
        return result


def mark_resolved(
    curiosity_id: str,
    resolution_summary: str,
) -> CuriosityEngineEntry:
    """Curiosity has been answered. `resolution_summary` is appended
    to ignore_reason — slight semantic stretch, but the schema field
    that's free-form per the contract. Future schema rev may add
    an explicit `resolution_summary` field.
    """
    entry = load(curiosity_id)
    with phase_span(
        "curiosity",
        "mark_resolved",
        curiosity_id=curiosity_id,
        cycle_id=entry.cycle_id if entry else "",
    ) as span:
        if not (resolution_summary and resolution_summary.strip()):
            raise ValueError("resolution_summary must be non-empty")
        result = update_status(curiosity_id, "resolved", note=resolution_summary)
        span.set(new_status=result.status, resolution_summary=resolution_summary[:80])
        return result


def mark_deferred(
    curiosity_id: str,
    defer_reason: str,
) -> CuriosityEngineEntry:
    """Curiosity is intentionally postponed. defer_reason is required
    per the Failure Flow Law (deferring is a decision, not silence).
    """
    entry = load(curiosity_id)
    with phase_span(
        "curiosity",
        "mark_deferred",
        curiosity_id=curiosity_id,
        cycle_id=entry.cycle_id if entry else "",
    ) as span:
        if not (defer_reason and defer_reason.strip()):
            raise ValueError(
                "defer_reason must be non-empty per Failure Flow Law: "
                "deferring without explanation is drift"
            )
        result = update_status(curiosity_id, "deferred", note=defer_reason)
        span.set(new_status=result.status, defer_reason=defer_reason[:80])
        return result


# =====================================================================
# SLICE-10 — escalated + failed-retry transitions
# =====================================================================


def mark_escalated(
    curiosity_id: str,
    escalation_reason: str,
) -> CuriosityEngineEntry:
    """A brain cycle initiated for this curiosity hit a posture that
    requires operator attention (security_block, identity_boundary,
    omega_lock pause, guardian_escalation flag). The entry leaves the
    live sweep until the operator surface re-routes it.

    `escalation_reason` is required — operator needs the why to decide
    re-route, deny, or rephrase.
    """
    entry = load(curiosity_id)
    with phase_span(
        "curiosity",
        "mark_escalated",
        curiosity_id=curiosity_id,
        cycle_id=entry.cycle_id if entry else "",
    ) as span:
        if not (escalation_reason and escalation_reason.strip()):
            raise ValueError("escalation_reason must be non-empty: operator needs context")
        result = update_status(curiosity_id, "escalated", note=escalation_reason)
        span.set(new_status=result.status, escalation_reason=escalation_reason[:80])
        return result


def mark_failed_retry(
    curiosity_id: str,
    failure_reason: str,
    *,
    max_retries: int = 3,
) -> CuriosityEngineEntry:
    """A brain cycle initiated for this curiosity failed to deliver
    (empty surface text, fallback canned response). Increments
    `attempt_count`. If the new count is below `max_retries` the entry
    re-enters the live sweep at status "open" so the next sweep can
    retry. At/above the cap it transitions to `deferred` so we don't
    burn epochs on a curiosity that consistently fails.

    The retry policy is per-call so callers (the consumer in particular)
    can pass a configured cap. Default 3 mirrors the typical retry
    discipline elsewhere in the codebase (e.g., autonomous OrderQueue).
    """
    _pre = load(curiosity_id)
    with phase_span(
        "curiosity",
        "mark_failed_retry",
        curiosity_id=curiosity_id,
        max_retries=max_retries,
        cycle_id=_pre.cycle_id if _pre else "",
    ) as span:
        if not (failure_reason and failure_reason.strip()):
            raise ValueError("failure_reason must be non-empty")

        # Hold the RLock across load → replace → persist so the
        # attempt_count increment can't race with another thread
        # mutating the same entry. See update_status for the same
        # invariant on the simple status-set path.
        with _lock:
            entry = load(curiosity_id)
            if entry is None:
                raise FileNotFoundError(f"CuriosityEngineEntry {curiosity_id!r} not found in store")

            new_count = int(entry.attempt_count) + 1
            if new_count >= max_retries:
                new_entry = replace(
                    entry,
                    status="deferred",
                    attempt_count=new_count,
                    ignore_reason=(
                        f"max_retries_exceeded ({new_count}/{max_retries}): {failure_reason}"
                    ),
                )
                span.set(outcome="deferred", attempt_count=new_count)
            else:
                new_entry = replace(
                    entry,
                    status="open",
                    attempt_count=new_count,
                    ignore_reason=(f"retry_pending ({new_count}/{max_retries}): {failure_reason}"),
                )
                span.set(outcome="open", attempt_count=new_count)
            persist(new_entry)
            return new_entry


# =====================================================================
# Read API
# =====================================================================


def load(curiosity_id: str) -> Optional[CuriosityEngineEntry]:
    """Load one entry by id. Returns None if not found.

    Returns None on JSON-parse / schema errors too — the caller can
    log the corruption; the store is not the right place to crash on
    one bad record.
    """
    if not curiosity_id:
        return None
    path = _path_for(curiosity_id)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            blob = json.load(f)
        return _deserialize(blob)
    except Exception as exc:
        log.warning("curiosity_store.load failed for %s: %s", curiosity_id, exc)
        return None


def list_all() -> List[CuriosityEngineEntry]:
    """Return every entry in the store, regardless of status. Order
    is filesystem-dependent — callers that need temporal order should
    sort on `created_at`.
    """
    if not _STORE_DIR.exists():
        return []
    out: List[CuriosityEngineEntry] = []
    for p in sorted(_STORE_DIR.glob("*.json")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                blob = json.load(f)
            out.append(_deserialize(blob))
        except Exception as exc:
            log.warning("curiosity_store skipping malformed file %s: %s", p, exc)
            continue
    return out


def list_open() -> List[CuriosityEngineEntry]:
    """Return entries whose status is in _LIVE_STATUSES (open / active).
    These are the entries the Curiosity Engine treats as "still pending."
    """
    return [e for e in list_all() if e.status in _LIVE_STATUSES]


def count_open() -> int:
    """Cheap count of live entries. `persist()` consults it to enforce
    `curiosity.yaml :: limits.max_parallel_curiosity_threads` on new entries.
    """
    return len(list_open())


# =====================================================================
# Maintenance
# =====================================================================


def archive(curiosity_id: str) -> bool:
    """Remove a closed entry from the live store (DeepSleep calls this
    after consolidation). Refuses to remove live (open / active) entries
    — those should pass through resolved/deferred first.

    Returns True if a file was removed, False if no-op (not found).
    Raises ValueError if the entry is still live.
    """
    entry = load(curiosity_id)
    with phase_span(
        "curiosity", "archive", curiosity_id=curiosity_id, cycle_id=entry.cycle_id if entry else ""
    ) as span:
        if entry is None:
            span.skip("not found")
            return False
        if entry.status in _LIVE_STATUSES:
            raise ValueError(
                f"refuse to archive live entry {curiosity_id!r} (status={entry.status!r})"
            )
        path = _path_for(curiosity_id)
        try:
            path.unlink()
            span.set(final_status=entry.status)
            return True
        except FileNotFoundError:
            span.skip("not found")
            return False

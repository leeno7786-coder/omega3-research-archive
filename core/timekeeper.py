"""The timekeeper at the commit region — finish-line arrival order for memory.

Operator design 2026-08-09 (Phase 3 cut 3): lane-parallel episodes may finish
in any order relative to their ignition — "it doesn't matter which episode got
started first, it matters the order in which they finished." Memory sequencing
is decided at the ONE place every episode's WRITES must pass, by stamping each
arrival with ``(commit_seq, commit_ms, event_date, lane)``, ms-precise. The
stamp lock IS the finish-line tape: whoever acquires it first arrived first,
and the monotonic ``commit_seq`` keeps that order total even when a
four-episode storm lands inside the same millisecond.

Canonical ordering key for consumers: ``(commit_ms, commit_seq)`` — see
``finish_line_key``. Within one process ``commit_seq`` alone is the exact
arrival order; across boots ``commit_ms`` carries the ordering even if the
counter file is lost. The counter is durable (guardian/state/timekeeper.json,
cleared by a day-zero factory reset with the rest of guardian/state) and never
regresses while the file survives; persisting it is failure-soft because a
missed write dents nothing — in-process monotonicity is held by the lock and
cross-boot ordering by wall-clock ms.

WHERE the stamp is taken — Phase 4 cut 13 moved it, and the reason is the whole
correctness content of that cut. Through Phase 3 it was taken at GATE ENTRY
(``run_deepsleep_for_episode``'s first statement), and stamp order equalled
landed order for exactly one reason: ONE process-wide lock was held from the
gate through the writes, so nothing could arrive in between. Cut 13 unlocks the
lane-local half of the terminus, and that argument dies with it — a lane can
stamp first, consolidate for three seconds and land last. The stamp is now the
FIRST statement inside the ONE commit lock
(``core.commit_region.serialized_commit``), and everything that lands does so
while that lock is held. Stamp order is landed order BY CONSTRUCTION, not by an
argument about what else happens to be serialized.

One write is deliberately left outside that region: ``archive_episode``, which
appends the episode's PriorEpisode row. Rows carry their stamp and
``journal_window`` re-sorts by ``finish_line_key``, so the file's physical order
is not the ordering authority and never has been.

Phase 4 cut 12 — ``commit_seq`` IS the memory epoch. The ratified memory design
(operator 2026-08-10 §5) wants every episode to capture a memory_epoch at open
and to commit at a later epoch; the counter for that could have lived in
``memory.write_ordinal``, but a second monotonic counter would be a second
ordering authority, and two authorities eventually disagree. Memory order is
finish-line order by operator law, so the finish line's counter is the epoch.
``last_commit_seq`` is the read-only half of that contract: it observes the
epoch WITHOUT advancing it, which is what an episode does at admission.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional

from core.pipelines import PIPELINE_IDS, validate_pipeline_id

log = logging.getLogger("omega.core.timekeeper")

_LOCK = threading.Lock()
_last_seq: Optional[int] = None  # lazy-loaded from the durable counter


# ── memory-organ Cut 13 (D-13-59/64/68/73/74/77): coverage-clock TYPES ──────
# Additive. The clock identity {clock_epoch_id, commit_seq} is the projection
# coverage authority carried by generation receipts / publication rows /
# manifests. Cut 13 Task A1 lands ONLY these types; the reservation-block
# state machine, entrance headroom, landed frontier and strict persistence
# seams (D-13-73/74/77/78) land with the builder tasks. `commit_seq` stays
# the sole ordering counter — `clock_epoch_id` names its durable lineage.


@dataclass(frozen=True, kw_only=True)
class CoverageClock:
    """One coverage-clock identity: which durable counter lineage, and how far
    along it. Compared as identity-then-number (D-13-58): a different
    ``clock_epoch_id`` is a regression regardless of the integers."""

    clock_epoch_id: str
    commit_seq: int

    def to_dict(self) -> Dict[str, Any]:
        return {"clock_epoch_id": str(self.clock_epoch_id), "commit_seq": int(self.commit_seq)}

    @classmethod
    def from_dict(cls, d: Any) -> "CoverageClock":
        if not isinstance(d, dict) or "clock_epoch_id" not in d or "commit_seq" not in d:
            raise ValueError("CoverageClock requires {clock_epoch_id, commit_seq}")
        epoch = d["clock_epoch_id"]
        seq = d["commit_seq"]
        if not isinstance(epoch, str) or not epoch:
            raise ValueError("CoverageClock.clock_epoch_id must be a non-empty string")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise ValueError("CoverageClock.commit_seq must be a non-negative int")
        return cls(clock_epoch_id=epoch, commit_seq=seq)


class TimekeeperUnavailable(RuntimeError):
    """The finish-line stamp cannot be admitted (D-13-73: confirmed reservation
    headroom below the safety floor). Raised at the terminus ENTRANCE, before
    any lane-local durable work."""


class CoverageClockUnavailable(RuntimeError):
    """No durably established coverage-clock epoch (D-13-64): the strict
    reservation-authority write has not succeeded, so no Candidate may be
    built and no projection state may change."""


def _state_path():
    from core.paths import GUARDIAN_STATE_ROOT

    return GUARDIAN_STATE_ROOT / "timekeeper.json"


# ── memory-organ Cut 13 — block reservation (D-13-73/77/78) ─────────────────
#
# The ONLY strictly durable timekeeper file is the reservation authority
# ``guardian/state/timekeeper.json = {"clock_epoch_id", "reserved_through"}``.
# It is written by the strict seam (temp → fsync → os.replace → parent fsync)
# ONLY at epoch creation / legacy migration and at BLOCK RESERVATION — never on
# the commit hot path. ``finish_line_stamp`` issues ``last_issued + 1`` from the
# CONFIRMED block under a short ``_LOCK`` with NO file I/O. A restart begins at
# the durable ``reserved_through + 1`` (the unissued tail is a lawful gap; a
# sequence is never reused). Runtime completion state (``last_issued``, the
# landed frontier) is in memory only (D-13-74/77): landing writes nothing.

RESERVATION_BLOCK = 256           # numbers reserved per strict write
RESERVE_AHEAD_THRESHOLD = 128     # proactive reserve-ahead trigger (performance, not correctness)


MAX_CONCURRENT_TERMINI = len(PIPELINE_IDS)
MIN_SAFE_STAMP_HEADROOM = MAX_CONCURRENT_TERMINI + 1

_RESERVATION_IO_LOCK = threading.Lock()   # serialises reservation WRITERS only (never held with _LOCK during I/O)
_state: Optional[Dict[str, Any]] = None   # {clock_epoch_id, reserved_through (confirmed), unconfirmed_through, last_issued, outstanding: set}
_landed_floor: int = 0                    # boot reconciliation floor (coverage seal / journal), same epoch


class ReservationAuthorityMigration(RuntimeError):
    """The legacy {last_seq} file could not be migrated to a reservation authority."""


def _read_authority_file() -> Dict[str, Any]:
    """Raw read of the durable file. Returns {} when absent; raises ValueError when unreadable."""
    path = _state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("timekeeper authority is not an object")
    return payload


def _strict_write_authority(clock_epoch_id: str, reserved_through: int) -> None:
    """temp (same dir) → flush + fsync → os.replace → parent-directory fsync. Raises OSError."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    data = json.dumps({"clock_epoch_id": str(clock_epoch_id), "reserved_through": int(reserved_through)}, sort_keys=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _fsync_dir(directory) -> None:
    if os.name != "posix":  # pragma: no cover - Windows: FlushFileBuffers via the projection sync helper
        from memory.projection.generation import sync_dir

        sync_dir(directory)
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mint_epoch_id() -> str:
    return os.urandom(16).hex()


def _establish_locked_free() -> None:
    """Load (or create / migrate) the reservation authority. Runs WITHOUT ``_LOCK`` held
    (it may perform the strict write). Sets ``_state``. Raises CoverageClockUnavailable
    when the authority cannot be made durable."""
    global _state
    if _state is not None:
        return
    try:
        payload = _read_authority_file()
        corrupt = False
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        log.warning("timekeeper authority unreadable (%s) — minting a NEW clock epoch; commit_ms still orders across the gap", exc)
        payload, corrupt = {}, True
    if not corrupt and "clock_epoch_id" in payload and "reserved_through" in payload:
        epoch = str(payload["clock_epoch_id"])
        reserved = max(0, int(payload["reserved_through"]))
        # restart: begin strictly beyond the durable boundary; reserve the next block now (boot-time write)
        target = reserved + RESERVATION_BLOCK
        try:
            _strict_write_authority(epoch, target)
        except OSError as exc:
            raise CoverageClockUnavailable(f"reservation authority write failed at boot: {exc}") from exc
        state = {"clock_epoch_id": epoch, "reserved_through": target, "unconfirmed_through": None,
                 "last_issued": reserved, "outstanding": set(), "landed_seed": reserved}
    elif not corrupt and "last_seq" in payload:
        # legacy {last_seq}: a MIGRATION, not a reset — ordering unaffected, first new stamp = last_seq + 1
        n = max(0, int(payload.get("last_seq") or 0))
        epoch = _mint_epoch_id()
        target = n + RESERVATION_BLOCK
        try:
            _strict_write_authority(epoch, target)
        except OSError as exc:
            raise CoverageClockUnavailable(f"reservation authority migration failed: {exc}") from exc
        state = {"clock_epoch_id": epoch, "reserved_through": target, "unconfirmed_through": None,
                 "last_issued": n, "outstanding": set(), "landed_seed": n}
    else:
        # missing / corrupt: NEW epoch identity — never recreated from a numeric value
        epoch = _mint_epoch_id()
        target = RESERVATION_BLOCK
        try:
            _strict_write_authority(epoch, target)
        except OSError as exc:
            raise CoverageClockUnavailable(f"reservation authority creation failed: {exc}") from exc
        state = {"clock_epoch_id": epoch, "reserved_through": target, "unconfirmed_through": None,
                 "last_issued": 0, "outstanding": set(), "landed_seed": 0}
    with _LOCK:
        if _state is None:
            _state = state


def _ensure_established() -> None:
    with _LOCK:
        if _state is not None:
            return
    with _RESERVATION_IO_LOCK:          # one establisher; others wait for the strict write, never race it
        with _LOCK:
            if _state is not None:
                return
        _establish_locked_free()


def _confirmed_headroom_locked() -> int:
    return int(_state["reserved_through"]) - int(_state["last_issued"])


def _reserve_ahead(*, blocking: bool) -> bool:
    """Two-phase reservation (D-13-78): short _LOCK read → release → strict write with NO
    _LOCK held (writers serialised by _RESERVATION_IO_LOCK) → short _LOCK confirm.
    Returns True when a reservation was confirmed. ``blocking=False`` skips when another
    writer is active (a safe entrance never queues behind the disk writer)."""
    acquired = _RESERVATION_IO_LOCK.acquire(blocking=blocking)
    if not acquired:
        return False
    try:
        with _LOCK:
            if _state is None:
                return False
            if _state["unconfirmed_through"] is not None:
                target = int(_state["unconfirmed_through"])          # retry confirming an unconfirmed reservation
                mode = "confirm"
            elif _confirmed_headroom_locked() >= RESERVE_AHEAD_THRESHOLD:
                return False                                          # someone else already reserved this target
            else:
                target = int(_state["reserved_through"]) + RESERVATION_BLOCK
                mode = "write"
            epoch = _state["clock_epoch_id"]
        replaced = False
        try:
            if mode == "write":
                path = _state_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".json.tmp")
                data = json.dumps({"clock_epoch_id": str(epoch), "reserved_through": int(target)}, sort_keys=True)
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
                replaced = True
            _fsync_dir(_state_path().parent)
        except OSError as exc:
            with _LOCK:
                if replaced or mode == "confirm":
                    # os.replace succeeded (or an earlier one did) but the parent sync failed: UNCONFIRMED —
                    # never issue beyond the old confirmed boundary; retry the parent sync later
                    _state["unconfirmed_through"] = target
                    log.warning("timekeeper reservation unconfirmed through %s (parent sync failed: %s)", target, exc)
                else:
                    log.warning("timekeeper reserve-ahead failed before replace (%s) — confirmed slack still usable", exc)
            return False
        with _LOCK:
            _state["reserved_through"] = max(int(_state["reserved_through"]), int(target))
            _state["unconfirmed_through"] = None
        return True
    finally:
        _RESERVATION_IO_LOCK.release()


def ensure_stamp_headroom(*, min_headroom: Optional[int] = None) -> int:
    """The terminus ENTRANCE gate (D-13-73/78): call BEFORE any lane-local durable work.
    Reserves ahead when confirmed headroom is below RESERVE_AHEAD_THRESHOLD (skipping the
    attempt when another writer is busy and headroom is still safe); admits iff confirmed
    headroom >= MIN_SAFE_STAMP_HEADROOM (>= 6 admissible, < 6 refused — review errata 1);
    raises TimekeeperUnavailable BEFORE anything is written. Returns the confirmed
    headroom."""
    floor = MIN_SAFE_STAMP_HEADROOM if min_headroom is None else int(min_headroom)
    try:
        _ensure_established()
    except CoverageClockUnavailable as exc:
        raise TimekeeperUnavailable(str(exc)) from exc
    with _LOCK:
        headroom = _confirmed_headroom_locked()
    if headroom < RESERVE_AHEAD_THRESHOLD:
        _reserve_ahead(blocking=headroom < floor)
        with _LOCK:
            headroom = _confirmed_headroom_locked()
    if headroom < floor:
        raise TimekeeperUnavailable(
            f"confirmed stamp headroom {headroom} < safety floor {floor}; terminus refused before any lane-local write")
    return headroom


def finish_line_stamp(pipeline_id: int, work_kind: str) -> Dict[str, Any]:
    """Stamp one episode's arrival at the finish line. Thread-safe; acquisition
    order under the internal lock IS the finish-line order.

    Call it from ONE place: ``core.commit_region.serialized_commit``, as the
    commit lock's first statement (cut 13). Stamping anywhere the commit lock is
    not held hands out a place in the memory sequence that nothing guarantees
    the writes will honour.

    Memory-organ Cut 13 (D-13-73/78): the number is issued from the CONFIRMED
    reservation block under a short ``_LOCK`` with NO file I/O. Every DeepSleep
    terminus called ``ensure_stamp_headroom()`` at its entrance, so the block can
    never be exhausted here; a direct caller that skipped the entrance and finds
    no headroom triggers a reservation OUTSIDE ``_LOCK`` (never disk I/O under
    the stamp lock) and is refused only if that fails."""
    global _state
    pid = validate_pipeline_id(pipeline_id)
    from core.symphony_time import now_iso, now_wall

    try:
        _ensure_established()
    except CoverageClockUnavailable as exc:
        raise TimekeeperUnavailable(str(exc)) from exc
    for _attempt in range(3):
        with _LOCK:
            if _confirmed_headroom_locked() >= 1:
                _state["last_issued"] = int(_state["last_issued"]) + 1
                seq = int(_state["last_issued"])
                _state["outstanding"].add(seq)
                committed_at = now_iso()
                return {
                    "commit_seq": seq,
                    "commit_ms": int(now_wall() * 1000),
                    "committed_at": committed_at,
                    "event_date": committed_at[:10],
                    "pipeline_id": pid,
                    "work_kind": str(work_kind),
                }
        _reserve_ahead(blocking=True)   # outside _LOCK
    raise TimekeeperUnavailable("no confirmed reservation headroom; stamp refused (reserve-ahead failing)")


def _landed_frontier_locked() -> int:
    """The largest sequence such that every issued sequence at or below it has landed or
    been burned; never a reserved-but-outstanding number (D-13-74)."""
    outstanding = _state["outstanding"]
    frontier = int(_state["last_issued"]) if not outstanding else min(outstanding) - 1
    # After a restart nothing outstanding survives and no sequence <= the old durable boundary
    # can ever land later (issuance resumed beyond it), so ``last_issued`` is a truthful
    # landed-or-burned frontier; the boot floor (seal / journal evidence) can only confirm it.
    return max(frontier, min(int(_landed_floor), int(_state["last_issued"])))


def mark_landed(stamp: Dict[str, Any]) -> None:
    """The commit body exited normally: the stamped sequence has LANDED (in memory only —
    no file write on the commit path, D-13-74/77)."""
    seq = int((stamp or {}).get("commit_seq") or 0)
    with _LOCK:
        if _state is not None:
            _state["outstanding"].discard(seq)


def mark_burned(stamp: Dict[str, Any]) -> None:
    """The commit body raised after the stamp: the sequence is spent and BURNED (a lawful
    gap); the landed frontier may advance across it."""
    seq = int((stamp or {}).get("commit_seq") or 0)
    with _LOCK:
        if _state is not None:
            _state["outstanding"].discard(seq)


def reconcile_landed(floor_seq: int, *, clock_epoch_id: str) -> None:
    """Boot reconciliation (D-13-74/77): raise the landed frontier to a durable floor proved
    by projection coverage evidence (the strict coverage seal / the episode journal) in
    the SAME epoch. Never uses the allocation counter; a different epoch is ignored."""
    global _landed_floor
    _ensure_established()
    with _LOCK:
        if _state is None or str(clock_epoch_id) != str(_state["clock_epoch_id"]):
            return
        floor = min(int(floor_seq), int(_state["last_issued"]))
        _landed_floor = max(_landed_floor, floor)


def last_coverage_clock() -> CoverageClock:
    """The projector's ONE clock-capture seam (D-13-64/74): the LANDED frontier of the
    established epoch, read under ``_LOCK`` — never the reserved/allocation counter.
    Raises CoverageClockUnavailable until the epoch identity is strictly durable."""
    _ensure_established()  # raises CoverageClockUnavailable when the authority cannot be written
    with _LOCK:
        return CoverageClock(clock_epoch_id=str(_state["clock_epoch_id"]), commit_seq=_landed_frontier_locked())


def reservation_state() -> Dict[str, Any]:
    """Health view: reserved/live sequence vs landed coverage (never conflated)."""
    _ensure_established()
    with _LOCK:
        return {
            "clock_epoch_id": _state["clock_epoch_id"],
            "reserved_through": int(_state["reserved_through"]),
            "unconfirmed_through": _state["unconfirmed_through"],
            "last_issued": int(_state["last_issued"]),
            "landed_seq": _landed_frontier_locked(),
            "outstanding": sorted(_state["outstanding"]),
            "confirmed_headroom": _confirmed_headroom_locked(),
        }


def last_commit_seq() -> int:
    """The current memory epoch: the last commit_seq handed out, read WITHOUT
    advancing the counter (Phase 4 cut 12).

    An episode calls this at admission to learn "which memory state am I
    reading". 0 means no episode has ever crossed the finish line on this
    counter — a truthful epoch for the first episode after a day-zero reset,
    not an error.

    Takes the same lock the stamp does, so a concurrent ``finish_line_stamp``
    can never expose a half-advanced counter; it lazily seeds from the durable
    file exactly as the stamp path does, so an epoch read before the first
    stamp of a boot still sees the pre-reboot value.
    """
    try:
        _ensure_established()
    except CoverageClockUnavailable:
        with _LOCK:
            return int(_state["last_issued"]) if _state is not None else 0
    with _LOCK:
        return int(_state["last_issued"])


def finish_line_key(stamp: Optional[Dict[str, Any]]) -> tuple[int, int]:
    """Canonical sort key for finish-line ordering of stamped records.

    Unstamped records (pre-cut-3 journal rows) sort to the epoch start —
    they predate every stamped record, which is truthful."""
    s = stamp if isinstance(stamp, dict) else {}
    try:
        return (int(s.get("commit_ms") or 0), int(s.get("commit_seq") or 0))
    except (TypeError, ValueError):
        return (0, 0)


def reset_for_tests() -> None:
    """Drop ALL in-memory timekeeper state (epoch, reserved_through, last_issued, landed
    frontier, unconfirmed reservation) so the next call re-reads the durable authority —
    a simulated process restart."""
    global _state, _landed_floor, _last_seq
    with _LOCK:
        _state = None
        _landed_floor = 0
        _last_seq = None

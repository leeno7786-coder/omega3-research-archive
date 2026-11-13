"""
StrengthIndex — Per-memory engram robustness tracker
=====================================================

Persistent per-memory state. Tracks for each memory id:
    - write_strength: confidence at the time of ingest (0..1)
    - touch_count:    number of recalls / accesses since ingest
    - last_access:    most recent retrieval timestamp
    - ingested_at:    initial ingest timestamp

Used by:
    - memory.engine.walker.composite_score — reads per-memory strength
      to weight retrieval ranking (hot memories surface first).
    - memory.gates.base.MemoryGate.recall — calls touch(mid) on every
      retrieved memory, reinforcing the engram (reconsolidation model).
    - autonomic._memory_stats.aggregate — reads hot/cold counts and
      recall activity to produce the autonomic strength signal.

Biology:
    Engrams strengthen with retrieval (reconsolidation) and weaken with
    disuse. Hot memories — recently accessed — are easily retrievable;
    cold memories drift toward forgetting unless reactivated.
    (Tonegawa et al. 2018; Josselyn & Tonegawa 2020).

Persistence:
    All state is serialized to guardian/state/strength_index.json. The
    file is loaded once at boot and rewritten in-place when dirty
    (batched save with configurable threshold).
"""

from __future__ import annotations

import json
import logging
import math
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from core.paths import GUARDIAN_STATE_ROOT

log = logging.getLogger("omega.memory.strength_index")


def _emit_strength(op: str, *, mutation: bool = True, **fields: Any) -> None:
    """Failure-soft observability for StrengthIndex mutations and scans."""
    try:
        if mutation:
            from guardian.terminal_feedback import emit_memory

            emit_memory(op, status="ok", **fields)
        else:
            from guardian.terminal_feedback import emit_data

            emit_data("memory", op, **fields)
        from guardian.terminal_feedback import emit_guardian_event

        emit_guardian_event("omega.memory.strength", op, **fields)
    except Exception:
        pass


# ---------------------------------------------------------------------
# Strength function parameters
# ---------------------------------------------------------------------
# These are biology-grounded defaults; tuning lives here rather than in
# a yaml config because they describe internal mechanics, not policy.

# Time constants (seconds)
DECAY_TAU_SECONDS = 24 * 3600.0  # exponential decay half-time ~ 16.6 hours
DECAY_FLOOR = 0.30  # never decays below 30% of write_strength

# Touch reinforcement
TOUCH_BOOST = 0.40  # max additive boost from touches
TOUCH_FULL = 25  # touches required for full boost (saturating)

# Default hot age for is_hot() — in-call default only. Aggregate-side
# hot/cold windows live in config/strength_index.yaml (`recency.*_hours`)
# and are passed through autonomic._memory_stats.aggregate.
_IS_HOT_DEFAULT_SECONDS = 1.0 * 3600.0

# Persistence
SAVE_DIRTY_THRESHOLD = 25  # save after this many unwritten changes
SCHEMA_VERSION = 1

# ---------------------------------------------------------------------
# Single-socket path resolution
# ---------------------------------------------------------------------
# All consumers MUST resolve through STRENGTH_INDEX_PATH + ensure_strength_index().
# Hardcoding the path string elsewhere is a doctrine violation.
STRENGTH_INDEX_PATH: Path = GUARDIAN_STATE_ROOT / "strength_index.json"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso() -> str:
    return _now_utc().isoformat()


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


class StrengthIndex:
    """Persistent per-memory robustness tracker."""

    def __init__(self, persist_path: Path):
        self._persist_path = Path(persist_path)
        self._records: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._dirty = 0
        self._load()

    # ----- I/O -----------------------------------------------------

    def _load(self) -> None:
        if not self._persist_path.exists():
            return
        try:
            with open(self._persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            file_version = data.get("version")
            if file_version is not None and file_version != SCHEMA_VERSION:
                # Refuse to silently misinterpret a different schema. A future
                # v2 with a shape change would corrupt downstream readers.
                log.error(
                    "StrengthIndex schema version mismatch: file=%s expected=%s "
                    "(refusing load to avoid silent corruption)",
                    file_version,
                    SCHEMA_VERSION,
                )
                return
            self._records = data.get("records") or {}
            log.info(
                "StrengthIndex loaded %d records from %s", len(self._records), self._persist_path
            )
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("StrengthIndex failed to load (%s) — starting empty", exc)
            self._records = {}

    def save(self, force: bool = False) -> None:
        """Write current state to disk if dirty (or always if force=True)."""
        with self._lock:
            if self._dirty == 0 and not force:
                return
            try:
                self._persist_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._persist_path.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "version": SCHEMA_VERSION,
                            "updated_at": _iso(),
                            "records": self._records,
                        },
                        f,
                        indent=2,
                    )
                tmp.replace(self._persist_path)
                n_records = len(self._records)
                self._dirty = 0
                _emit_strength(
                    "save",
                    mutation=False,
                    force=force,
                    record_count=n_records,
                )
            except OSError as exc:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "memory",
                    "strength_index_save_failed",
                    ok=False,
                    error=str(exc),
                )
                log.error("StrengthIndex save failed: %s", exc)

    def _maybe_save(self) -> None:
        """Save if the dirty threshold has been crossed."""
        if self._dirty >= SAVE_DIRTY_THRESHOLD:
            self.save()

    # ----- Mutation hooks (called from gates/walker) ---------------

    def register(self, mid: str, write_strength: float = 0.5, salience=None) -> None:
        """Record a new memory. Called by the gate after atomic_write succeeds.

        If the mid already exists, this is a no-op (re-registration of the
        same id — e.g., during reindex — must not overwrite history).
        """
        if not mid:
            return
        with self._lock:
            if mid in self._records:
                return
            if salience is None:
                try:
                    from autonomic.symphony_neuromod import current_memory_salience

                    salience = current_memory_salience()
                except Exception:
                    salience = None
            if salience is None:
                scale, tau_factor = 1.0, 1.0
            else:
                scale = float(salience.get("scale", 1.0))
                tau_factor = float(salience.get("tau_factor", 1.0))
            self._records[mid] = {
                "write_strength": float(max(0.0, min(1.0, write_strength * scale))),
                "encode_tau": float(DECAY_TAU_SECONDS * tau_factor),
                "touch_count": 0,
                "ingested_at": _iso(),
                "last_access": _iso(),
            }
            self._dirty += 1
            self._maybe_save()
        _emit_strength("register", memory_id=mid, write_strength=write_strength)

    def advance_below_floor_cycles(
        self,
        mid: str,
        *,
        confidence: float,
        cycles: float,
        floor: float,
    ) -> int:
        """Track pressure cycles spent below the demotion confidence floor."""
        if not mid:
            return 0
        from memory.demotion import compute_next_below_floor_cycles

        with self._lock:
            rec = self._records.get(mid)
            if rec is None:
                return 0
            current = int(rec.get("below_floor_cycles") or 0)
            next_val = compute_next_below_floor_cycles(
                current,
                confidence=confidence,
                cycles=cycles,
                floor=floor,
            )
            if next_val != current:
                rec["below_floor_cycles"] = next_val
                self._dirty += 1
                self._maybe_save()
            return next_val

    def reset_below_floor_cycles(self, mid: str) -> None:
        if not mid:
            return
        with self._lock:
            rec = self._records.get(mid)
            if rec is None:
                return
            if int(rec.get("below_floor_cycles") or 0) != 0:
                rec["below_floor_cycles"] = 0
                self._dirty += 1
                self._maybe_save()

    def touch(self, mid: str) -> None:
        """Record a retrieval/access. Called by the gate on every recall hit."""
        if not mid:
            return
        with self._lock:
            rec = self._records.get(mid)
            if rec is None:
                # Lazy-register for a memory not yet tracked. After
                # bulk_register_from_coord_index runs at boot this should
                # be rare (only fires for memories ingested between boot
                # and first touch). ingested_at is left None — we don't
                # know the real ingest time and lying with _iso() would
                # falsely inflate recent_writes in the autonomic signal.
                self._records[mid] = {
                    "write_strength": 0.5,
                    "touch_count": 1,
                    "ingested_at": None,
                    "last_access": _iso(),
                }
                touch_count = 1
            else:
                rec["touch_count"] = int(rec.get("touch_count", 0)) + 1
                rec["last_access"] = _iso()
                touch_count = rec["touch_count"]
            self._dirty += 1
            self._maybe_save()
        _emit_strength("touch", memory_id=mid, touch_count=touch_count)

    def reinforce(self, mid: str, delta: float) -> Optional[float]:
        """RPE choice-credit: adjust an EXISTING engram's write_strength by
        `delta` (positive on a rewarded choice, negative on a punished one),
        clamped to [0, 1]. No-op for an unknown mid -- we credit ONLY memories
        that exist and genuinely drove a choice (never lazy-create here, unlike
        touch). Returns the new write_strength, or None if the mid is absent.
        Distinct from touch() (retrieval count, outcome-blind) and register()
        (set-once at ingest): this is the outcome-keyed, per-choice plasticity."""
        if not mid:
            return None
        with self._lock:
            rec = self._records.get(mid)
            if rec is None:
                return None
            new_ws = float(max(0.0, min(1.0, float(rec.get("write_strength", 0.5)) + float(delta))))
            rec["write_strength"] = new_ws
            rec["last_access"] = _iso()
            self._dirty += 1
            self._maybe_save()
        _emit_strength(
            "reinforce",
            memory_id=mid,
            delta=delta,
            write_strength=new_ws,
        )
        return new_ws

    def set_write_strength(self, mid: str, value: float) -> Optional[float]:
        """Authoritatively set an EXISTING engram's write_strength to
        ``value`` (clamped [0, 1]). No-op (returns None) for an unknown
        mid -- never lazy-creates (like reinforce, unlike touch).

        This is the forgetting/decay primitive: DeepSleep phase-3
        reconciliation computes a decayed target and writes it here.
        Distinct from register() (set-once-at-ingest -- a NO-OP on an
        already-tracked mid, which is exactly why forgetting must not use
        it) and reinforce() (relative +/- delta credit). Returns the new
        write_strength, or None if the mid is absent."""
        if not mid:
            return None
        with self._lock:
            rec = self._records.get(mid)
            if rec is None:
                return None
            new_ws = float(max(0.0, min(1.0, float(value))))
            rec["write_strength"] = new_ws
            # Do not stamp last_access here — decay is not a recall touch.
            # touch() owns last_access refresh on the verified recall path.
            self._dirty += 1
            self._maybe_save()
        _emit_strength(
            "set_write_strength",
            memory_id=mid,
            write_strength=new_ws,
        )
        return new_ws

    def forget(self, mid: str) -> None:
        """Drop a memory from tracking — called when a memory is deleted."""
        if not mid:
            return
        with self._lock:
            if mid not in self._records:
                return
            del self._records[mid]
            self._dirty += 1
            self._maybe_save()
        _emit_strength("forget", memory_id=mid)

    # ----- Bulk operations (boot-time only) ------------------------

    def bulk_register_from_coord_index(self, coord_index_ids: Iterable[str]) -> int:
        """Register every coord-index id that is not yet tracked.

        Run once at boot after the coord index is loaded. Untracked
        memories carry no `ingested_at`/`last_access` (we don't know the
        real ingest time and cannot lie with `now` — that would inflate
        the recent-write signal). They count toward `total_tracked` so
        the autonomic hot/cold ratios become honest by construction.

        Returns the number of records added (already-tracked ids skip).
        """
        added = 0
        with self._lock:
            for mid in coord_index_ids:
                if not mid or mid in self._records:
                    continue
                self._records[mid] = {
                    "write_strength": 0.5,
                    "touch_count": 0,
                    "ingested_at": None,
                    "last_access": None,
                }
                added += 1
            if added:
                self._dirty += added
                # Force a save so a crash mid-boot doesn't lose the bulk
                # registration (the threshold-batched path could swallow
                # it under SAVE_DIRTY_THRESHOLD writes).
                self.save(force=True)
        if added:
            _emit_strength("bulk_register", added=added, mutation=True)
        return added

    # ----- Read API -------------------------------------------------

    def get_strength(self, mid: str) -> float:
        """Compute the current strength of a memory in [0, 1].

        strength = write_strength
                 × (decay_floor + (1 - decay_floor) × exp(-age / tau))
                 × (1 + log(1 + touch_count) / log(1 + touch_full) × touch_boost)
        """
        with self._lock:
            rec = self._records.get(mid)
        if rec is None:
            return 0.0

        ws = float(rec.get("write_strength", 0.5))
        touches = int(rec.get("touch_count", 0))
        last_access = _parse_iso(rec.get("last_access")) or _parse_iso(rec.get("ingested_at"))

        # Age decay
        if last_access is None:
            recency = 1.0
        else:
            age_seconds = max(0.0, (_now_utc() - last_access).total_seconds())
            tau = float(rec.get("encode_tau", DECAY_TAU_SECONDS))
            recency = math.exp(-age_seconds / tau)
        decay_factor = DECAY_FLOOR + (1.0 - DECAY_FLOOR) * recency

        # Touch boost — log-saturating
        if touches <= 0:
            touch_factor = 0.0
        else:
            touch_factor = math.log(1.0 + touches) / math.log(1.0 + TOUCH_FULL)
        boost = 1.0 + min(1.0, touch_factor) * TOUCH_BOOST

        return max(0.0, min(1.0, ws * decay_factor * boost))

    def get_record(self, mid: str) -> Optional[Dict[str, Any]]:
        """Read raw record for a memory id (or None)."""
        with self._lock:
            rec = self._records.get(mid)
            return dict(rec) if rec is not None else None

    def is_hot(self, mid: str, hot_age_seconds: float = _IS_HOT_DEFAULT_SECONDS) -> bool:
        rec = self.get_record(mid)
        if rec is None:
            return False
        last = _parse_iso(rec.get("last_access"))
        if last is None:
            return False
        return (_now_utc() - last).total_seconds() <= hot_age_seconds

    # ----- Aggregate (for autonomic strength signal) ---------------

    def aggregate(
        self,
        *,
        hot_age_seconds: float = 1.0 * 3600.0,
        cold_age_seconds: float = 72.0 * 3600.0,
        recall_window_seconds: float = 2.0 * 3600.0,
        write_window_seconds: float = 6.0 * 3600.0,
    ) -> Dict[str, float]:
        """Snapshot of per-memory tracking state. Used by autonomic
        _memory_stats to feed the strength_index signal.

        These per-call defaults are fallbacks; the real windows live in
        config/strength_index.yaml `recency.*_hours` and are passed
        through autonomic._memory_stats.aggregate."""
        now = _now_utc()
        with self._lock:
            records = list(self._records.items())

        total = 0
        hot_count = 0
        cold_count = 0
        recent_writes = 0
        recent_recalls = 0
        recent_write_strengths = []
        total_touches = 0

        for _mid, rec in records:
            total += 1
            ws = float(rec.get("write_strength", 0.5))
            touches = int(rec.get("touch_count", 0))
            total_touches += touches
            ingested = _parse_iso(rec.get("ingested_at"))
            last_access = _parse_iso(rec.get("last_access")) or ingested

            if ingested is not None:
                age = (now - ingested).total_seconds()
                if age <= write_window_seconds:
                    recent_writes += 1
                    recent_write_strengths.append(ws)

            if last_access is not None:
                last_age = (now - last_access).total_seconds()
                # A "recent recall" is one where the last_access is recent
                # AND there's been at least one touch beyond ingest.
                if last_age <= recall_window_seconds and touches > 0:
                    recent_recalls += touches  # count touches, not memories
                if last_age <= hot_age_seconds:
                    hot_count += 1
                elif last_age >= cold_age_seconds:
                    cold_count += 1

        mean_ws = (
            sum(recent_write_strengths) / len(recent_write_strengths)
            if recent_write_strengths
            else 0.0
        )

        result = {
            "total_tracked": float(total),
            "hot_count": float(hot_count),
            "cold_count": float(cold_count),
            "recent_write_count": float(recent_writes),
            "recent_recall_count": float(recent_recalls),
            "mean_recent_write_strength": float(mean_ws),
            "total_touches": float(total_touches),
        }
        _emit_strength("aggregate", mutation=False, **result)
        return result

    # ----- Public enumeration (for snapshot builders) ---------------

    def items_snapshot(self) -> list[tuple[str, Dict[str, Any]]]:
        """Thread-safe snapshot of (mid, record_copy) pairs for the
        full record set.

        Returns deep-enough copies (per-record dict copy) so callers
        ranking / filtering at top-N can iterate without holding the
        lock. The copy is intentional — callers that need a live view
        should use get_record() per id.

        Introduced for symphony §S8 `memory.symphony_strength.
        build_strength_index_snapshot`, which ranks memories by current
        get_strength() to produce the GLOBAL_A orient body-state frame.
        Public so the symphony module need not reach into _records,
        keeping the private/public boundary clean.
        """
        with self._lock:
            return [(mid, dict(rec)) for mid, rec in self._records.items()]


# ---------------------------------------------------------------------
# Singleton helpers
# ---------------------------------------------------------------------

_singleton: Optional[StrengthIndex] = None
_singleton_lock = threading.Lock()


def get_strength_index() -> Optional[StrengthIndex]:
    """Return the wired singleton (or None if boot hasn't wired it)."""
    return _singleton


def init_strength_index(persist_path: Path) -> StrengthIndex:
    """Instantiate the singleton at boot. Idempotent."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = StrengthIndex(persist_path)
        return _singleton


def ensure_strength_index() -> StrengthIndex:
    """Return the singleton, lazy-initializing at the canonical path
    if no boot wire has fired yet.

    THIS is the single socket. Consumers must NOT hardcode the path or
    repeat the get-or-init dance — call this and use the return value.
    """
    si = _singleton
    if si is not None:
        return si
    return init_strength_index(STRENGTH_INDEX_PATH)


# ---------------------------------------------------------------------
# Graceful-shutdown flush (audit AUDIT-1 / production-required #3)
# ---------------------------------------------------------------------
# Without this, up to SAVE_DIRTY_THRESHOLD-1 (24) touch/decay updates
# can be lost on crash or graceful shutdown — touches happen on every
# recall, so a cluster of in-memory updates was sitting in RAM only.
#
# Registers three handlers:
#   - atexit:  universal Python-shutdown hook (works on Windows + POSIX).
#   - SIGINT:  Ctrl+C / equivalent (works on Windows + POSIX).
#   - SIGTERM: POSIX-only graceful shutdown signal (kill <pid> default).
#              Windows raises ValueError on signal.signal(SIGTERM, ...);
#              we catch it and fall through — atexit still covers Windows.
#
# All handlers are idempotent and safe to fire multiple times. The
# signal handlers flush + then re-raise the default behavior (so the
# process actually exits as the user/system intended).

import atexit as _atexit
import signal as _signal

_shutdown_registered = False
_shutdown_lock = threading.Lock()


def _flush_singleton(reason: str) -> None:
    """Force-save the singleton if it exists. Idempotent + crash-safe."""
    si = _singleton
    if si is None:
        return
    try:
        # force=True ensures we write even if dirty==0, so the
        # "updated_at" timestamp reflects the shutdown time.
        si.save(force=True)
        log.info(
            "StrengthIndex flushed at shutdown (reason=%s, %d records)", reason, len(si._records)
        )
    except Exception as exc:
        # Never let a flush failure prevent shutdown — log loud and move on.
        log.error("StrengthIndex flush at shutdown FAILED (reason=%s): %s", reason, exc)
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "strength_index_flush",
            status="degraded",
            ok=False,
            reason="shutdown_flush_failed",
            error=str(exc),
        )


def register_graceful_shutdown() -> Dict[str, bool]:
    """Register atexit + signal handlers that flush the singleton on
    graceful shutdown. Idempotent — calling more than once returns the
    same registration map without double-registering anything.

    Returns a dict naming which handlers actually registered:
        {"atexit": True, "sigint": True, "sigterm": True/False}

    Per the graceful-drain teardown doctrine: the engine substrate is
    first-up, last-down; flushing dirty engram state is part of the
    "every write ends in {written, queued, denied}" contract — touches
    that happened in RAM but never reached disk are written.
    """
    global _shutdown_registered
    with _shutdown_lock:
        result = {"atexit": False, "sigint": False, "sigterm": False}
        if _shutdown_registered:
            # Already registered; report what got registered last time.
            return {"atexit": True, "sigint": True, "sigterm": True}

        # 1. atexit — universal, always registers.
        _atexit.register(_flush_singleton, "atexit")
        result["atexit"] = True

        # 2. SIGINT — Ctrl+C. Works on Windows + POSIX. We chain to
        #    the previous handler so we don't break operator workflows
        #    that already installed one (e.g., debuggers, REPLs).
        try:
            _prev_sigint = _signal.getsignal(_signal.SIGINT)

            def _sigint_handler(signum, frame):
                # DIAGNOSTIC: capture stack trace of where SIGINT lands.
                # Operator reports HUD dying silently after ~1-5 min of
                # uptime — investigation shows the trigger is SIGINT
                # delivered to the process from an unknown source on
                # Windows (likely console group propagation from a
                # short-lived child process like the auto-browser-open
                # at boot.py:818, OR an unintended Ctrl+C from a
                # focused terminal). Without this stack capture there
                # is no way to tell what the main thread was doing
                # when the signal arrived.
                try:
                    import sys as _sys
                    import traceback as _tb
                    import threading as _th

                    stack = "".join(_tb.format_stack(frame, limit=20))
                    where_file = getattr(frame, "f_code", None) and frame.f_code.co_filename
                    where_line = getattr(frame, "f_lineno", 0)
                    # Print to stderr immediately so the operator SEES
                    # the signal arrive — log-only feedback was hiding
                    # the cause behind a silent shutdown.
                    print(
                        "\n"
                        + "=" * 64
                        + f"\n!! SIGINT received  thread={_th.current_thread().name}"
                        f"\n!! main frame  {where_file}:{where_line}"
                        f"\n!! shutting down — set OMEGA_IGNORE_SIGINT=1 to ignore unattended signals"
                        f"\n" + "=" * 64,
                        file=_sys.stderr,
                        flush=True,
                    )
                    log.warning(
                        "SIGINT received — thread=%s frame=%s:%d\n%s",
                        _th.current_thread().name,
                        where_file,
                        where_line,
                        stack,
                    )
                    # Operator opt-in: if OMEGA_IGNORE_SIGINT=1 is set,
                    # eat the signal instead of re-raising. Lets the
                    # operator keep the substrate alive while the
                    # source of the spurious SIGINT is being diagnosed.
                    import os as _os

                    if _os.environ.get("OMEGA_IGNORE_SIGINT", "").lower() in ("1", "true", "yes"):
                        log.warning("SIGINT ignored (OMEGA_IGNORE_SIGINT=1)")
                        return
                except Exception:
                    pass
                _flush_singleton("SIGINT")
                # Restore + re-raise so the process exits as intended.
                if callable(_prev_sigint) and _prev_sigint not in (
                    _signal.SIG_DFL,
                    _signal.SIG_IGN,
                ):
                    _prev_sigint(signum, frame)
                else:
                    _signal.signal(_signal.SIGINT, _signal.SIG_DFL)
                    _signal.raise_signal(_signal.SIGINT)

            _signal.signal(_signal.SIGINT, _sigint_handler)
            result["sigint"] = True
        except (ValueError, OSError) as exc:
            # ValueError: signal only works in main thread of main interpreter
            log.warning("StrengthIndex shutdown: SIGINT handler not registered (%s)", exc)

        # 3. SIGTERM — POSIX only. On modern Python/Windows, signal.signal
        #    accepts the install silently but Windows never DELIVERS SIGTERM
        #    to a Python handler (TerminateProcess bypasses the runtime).
        #    Installing a handler that won't fire is misleading, so we skip
        #    explicitly on Windows. atexit covers shutdown there.
        import sys as _sys

        if _sys.platform.startswith("win"):
            log.info(
                "StrengthIndex shutdown: SIGTERM skipped on Windows "
                "(undeliverable; atexit covers shutdown)"
            )
        else:
            try:
                _prev_sigterm = _signal.getsignal(_signal.SIGTERM)

                def _sigterm_handler(signum, frame):
                    _flush_singleton("SIGTERM")
                    if callable(_prev_sigterm) and _prev_sigterm not in (
                        _signal.SIG_DFL,
                        _signal.SIG_IGN,
                    ):
                        _prev_sigterm(signum, frame)
                    else:
                        _signal.signal(_signal.SIGTERM, _signal.SIG_DFL)
                        _signal.raise_signal(_signal.SIGTERM)

                _signal.signal(_signal.SIGTERM, _sigterm_handler)
                result["sigterm"] = True
            except (ValueError, OSError, AttributeError) as exc:
                # Threaded contexts or unusual interpreter builds.
                log.warning("StrengthIndex shutdown: SIGTERM handler not registered (%s)", exc)

        _shutdown_registered = True
        return result

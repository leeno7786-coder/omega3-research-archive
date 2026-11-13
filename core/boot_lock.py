"""Single-instance boot — one live substrate per state tree, fresh every boot.

Why this exists
---------------
``boot.py``'s ``run_continuous()`` drives the pressure loop and owns
``guardian/state``. Nothing prevented a SECOND ``python boot.py`` from
starting against the same state tree — and a double-boot is corrupting:
two pressure loops increment the tick counter, open episodes, and write
SharedState / StrengthIndex / the JSONL stores concurrently, interleaving
two cognitions into one set of files. (Observed 2026-05-31: a relaunch
left the prior boot running; both wrote the same logs until the stale one
was killed by hand, polluting the baseline telemetry.)

Operator directive (2026-05-31): **every boot starts fresh.** Rather than
refuse when a prior substrate is running, ``acquire()`` KILLS any prior
substrate first, then takes the lock. One command always yields exactly
one freshly-started substrate.

PROCESS-ONLY — NOT a state wipe
-------------------------------
"Fresh start" here means a fresh PROCESS. This module kills duplicate
boot processes; it NEVER touches ``guardian/state`` or ``memory/`` on
disk. State/factory reset is the separate, MANUAL ``scripts/
day_zero_reset.py`` — per the no-auto-wipe-on-boot LAW, boot must never
wipe disk. Do not add state deletion here.

Contract
--------
``acquire()``:
  1. Reap any OTHER live substrate (boot.py running against THIS state
     tree) — kill the process + its child tree.
  2. If a target survives the kill, raise ``BootAlreadyRunning`` (refuse
     to double-boot rather than risk corruption — the safe failure mode).
  3. Write ``guardian/state/boot.lock`` with our PID + start time.
  4. Register ``release`` via ``atexit``.

Targets are discovered two ways (union): the recorded lock owner, and a
``psutil`` scan for python processes running ``boot.py`` whose cwd is
exactly ``OMEGA_ROOT`` (excludes the portable-lite runtime, worktree
boots, and pytest). Self + our own process ancestry are always protected.

Never raises except the deliberate ``BootAlreadyRunning``; every other
fault degrades to "proceed without a durable lock" (logged), because a
lock/reap fault must not stop a legitimate boot.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from core.paths import GUARDIAN_STATE_ROOT, OMEGA_ROOT

log = logging.getLogger("omega.core.boot_lock")

BOOT_LOCK_PATH = GUARDIAN_STATE_ROOT / "boot.lock"
_KILL_WAIT_SECONDS = 5.0


class BootAlreadyRunning(RuntimeError):
    """Raised when a prior substrate could not be killed for a fresh boot."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_alive(pid: int, create_time: Optional[float] = None) -> bool:
    """True if `pid` belongs to a currently-running process.

    With psutil + a recorded create_time we also reject a RECYCLED pid
    (same number, different process). Without psutil we fall back to
    os.kill(pid, 0): no exception => alive.
    """
    if pid <= 0:
        return False
    try:
        import psutil

        if not psutil.pid_exists(pid):
            return False
        if create_time is not None:
            try:
                proc = psutil.Process(pid)
                if abs(proc.create_time() - float(create_time)) > 2.0:
                    return False  # pid recycled to a different process
            except Exception:
                return False
        return True
    except ImportError:
        pass
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return True
    return True


def _read_lock() -> Optional[dict]:
    try:
        raw = BOOT_LOCK_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("boot_lock: could not read %s (%s)", BOOT_LOCK_PATH, exc)
        return None
    try:
        rec = json.loads(raw)
        return rec if isinstance(rec, dict) else None
    except (json.JSONDecodeError, ValueError):
        log.warning("boot_lock: lock file unreadable/corrupt — treating as stale")
        return None


def _write_lock() -> dict:
    pid = os.getpid()
    create_time: Optional[float] = None
    try:
        import psutil

        create_time = psutil.Process(pid).create_time()
    except Exception:
        create_time = None
    rec = {"pid": pid, "create_time": create_time, "acquired_at": _now()}
    BOOT_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = BOOT_LOCK_PATH.with_suffix(".lock.tmp")
    tmp.write_text(json.dumps(rec), encoding="utf-8")
    os.replace(tmp, BOOT_LOCK_PATH)  # atomic on same filesystem
    return rec


def _protected_pids() -> set:
    """Our own PID + all ancestor PIDs — never kill these (self-kill guard)."""
    me = os.getpid()
    protected = {me}
    try:
        import psutil

        for anc in psutil.Process(me).parents():
            protected.add(anc.pid)
    except Exception:
        pass
    return protected


def _scan_substrate_pids(protected: set) -> List[int]:
    """PIDs of OTHER python processes running boot.py against THIS state
    tree. Matched by: 'boot.py' in argv AND cwd == OMEGA_ROOT. Excludes
    the portable-lite runtime, worktree boots, pytest, and protected PIDs.
    Empty list if psutil is unavailable (the lock-owner path still covers
    the common case)."""
    out: List[int] = []
    try:
        import psutil
    except ImportError:
        return out
    root = str(OMEGA_ROOT.resolve())
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            pid = proc.info["pid"]
            if pid in protected:
                continue
            argv = proc.info.get("cmdline") or []
            joined = " ".join(str(a) for a in argv)
            if "boot.py" not in joined:
                continue
            if any(s in joined for s in ("omega-portable-lite", "worktrees", "pytest")):
                continue
            try:
                cwd = str(Path(proc.cwd()).resolve())
            except Exception:
                continue  # cwd unknowable -> can't confirm it's our tree; skip
            if cwd != root:
                continue
            out.append(pid)
        except Exception:
            continue
    return out


def _kill_pid(pid: int) -> bool:
    """Kill `pid` and its child tree. Return True if it is dead afterward."""
    try:
        import psutil

        try:
            victim = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return True
        procs = victim.children(recursive=True) + [victim]
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass
        _gone, alive = psutil.wait_procs(procs, timeout=_KILL_WAIT_SECONDS)
        return not alive
    except ImportError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return True  # already gone
        except Exception:
            return False
        return not _pid_alive(pid)
    except Exception:
        return False


def reap_other_substrates() -> dict:
    """Kill any OTHER live substrate so this boot starts fresh.

    PROCESS-ONLY — never touches disk state. Returns
    ``{"killed": [...], "survivors": [...]}``: killed died, survivors
    resisted the kill (caller should refuse to double-boot)."""
    protected = _protected_pids()
    targets = set(_scan_substrate_pids(protected))

    # Also honor the recorded lock owner (covers a boot launched from a
    # subdir, or a pre-lock orphan psutil's cwd match missed).
    rec = _read_lock()
    if rec:
        opid = int(rec.get("pid", 0) or 0)
        if opid and opid not in protected and _pid_alive(opid, rec.get("create_time")):
            targets.add(opid)

    killed: List[int] = []
    survivors: List[int] = []
    for pid in sorted(targets):
        if _kill_pid(pid):
            killed.append(pid)
        else:
            survivors.append(pid)
    result = {"killed": killed, "survivors": survivors}
    if killed or survivors:
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "runtime",
                "boot_lock_reap",
                summary=f"killed={len(killed)} survivors={len(survivors)}",
                killed=killed,
                survivors=survivors,
            )
        except Exception:
            pass
    return result


def acquire() -> dict:
    """Reap any prior substrate, then take the single-instance lock.

    Returns the lock record. Raises ``BootAlreadyRunning`` only if a prior
    substrate survived the kill (refuse rather than corrupt). Any other
    fault degrades to a best-effort proceed (``{"degraded": True}``).
    """
    try:
        # Idempotent re-entry: we already hold the lock.
        existing = _read_lock()
        if existing is not None and int(existing.get("pid", 0) or 0) == os.getpid():
            return existing

        # Operator directive 2026-05-31: every boot starts fresh by killing
        # any prior substrate. PROCESS-ONLY — never wipes guardian/state
        # (state reset is the separate manual day_zero_reset; no-auto-wipe
        # LAW). See module docstring.
        reaped = reap_other_substrates()
        if reaped["killed"]:
            log.warning(
                "boot_lock: reaped prior substrate(s) for a fresh start: %s", reaped["killed"]
            )
        if reaped["survivors"]:
            raise BootAlreadyRunning(
                f"prior substrate(s) {reaped['survivors']} would not die — "
                f"refusing to double-boot against {GUARDIAN_STATE_ROOT}. "
                f"Kill them manually, then retry."
            )

        rec = _write_lock()
        atexit.register(release)
        log.info("boot_lock: acquired pid=%s (reaped=%s)", rec["pid"], reaped["killed"])
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "runtime",
                "boot_lock_acquire",
                status="ok",
                summary=f"pid={rec['pid']}",
                pid=rec.get("pid"),
                reaped=reaped.get("killed"),
                degraded=bool(rec.get("degraded")),
            )
        except Exception:
            pass
        return rec
    except BootAlreadyRunning:
        raise
    except Exception as exc:
        # A lock/reap fault must not stop a legitimate boot.
        log.error("boot_lock: acquire degraded (%s) — proceeding without a durable lock", exc)
        return {"pid": os.getpid(), "degraded": True}


def release() -> None:
    """Remove the lock file iff this process owns it. Never raises."""
    try:
        rec = _read_lock()
        if rec is not None and int(rec.get("pid", 0) or 0) == os.getpid():
            BOOT_LOCK_PATH.unlink(missing_ok=True)
            log.info("boot_lock: released pid=%s", os.getpid())
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "runtime",
                    "boot_lock_release",
                    status="ok",
                    summary=f"pid={os.getpid()}",
                    pid=os.getpid(),
                )
            except Exception:
                pass
    except Exception as exc:
        log.debug("boot_lock: release best-effort skipped (%s)", exc)


def owner() -> Optional[dict]:
    """Return the current lock record (for diagnostics), or None."""
    return _read_lock()

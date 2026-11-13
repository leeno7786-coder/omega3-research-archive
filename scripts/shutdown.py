"""Gracefully stop the running Omega substrate (boot.py + in-process HUD).

Reads ``guardian/state/boot.lock`` (see ``core.boot_lock``), verifies the
recorded PID is a live ``boot.py`` against this repo, sends a graceful stop
signal, and lets the boot process run its own teardown
(``core.boot_lifecycle.shutdown_system`` — pressure loop stop, HUD drain,
substrate state flush, boot-lock release) before the lock is cleared.

The local services are owned by the Omega boot process and are stopped by its
normal lifecycle. The final portable-runtime sweep is independently scoped to
the Omega product tree and its sibling portable runtime folders on the same SSD.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.boot_lock import BOOT_LOCK_PATH, _pid_alive  # noqa: E402
from core.paths import OMEGA_ROOT

log = logging.getLogger("omega.scripts.shutdown")

_STOP_WAIT_SECONDS = 10.0
_MODEL_RUNTIME_NOTE = "Portable runtime teardown is not performed in dry-run."
_DEV_BRIDGE_NOTE = "Live-chat agent: no standalone bridge to sweep."

_HELP_EPILOG = """
Examples:
  .\\.venv-brain\\Scripts\\python.exe scripts\\shutdown.py
  .\\.venv-brain\\Scripts\\python.exe scripts\\shutdown.py --dry-run
  .\\.venv-brain\\Scripts\\python.exe scripts\\shutdown.py --json
  .\\.venv-brain\\Scripts\\python.exe scripts\\shutdown.py --dry-run --json
"""


@dataclass(frozen=True)
class ShutdownResult:
    """Structured outcome for agents and human renderers."""

    status: str
    pid: Optional[int]
    lock_cleared: bool
    dry_run: bool
    exit_code: int
    message: str
    model_runtime_note: str = _MODEL_RUNTIME_NOTE
    runtime_swept: Optional[bool] = None
    dev_bridge_note: str = _DEV_BRIDGE_NOTE
    dev_bridge_swept: Optional[bool] = None


def resolve_lock() -> Optional[dict[str, Any]]:
    """Return the boot lock record, or None if missing/unreadable."""
    try:
        raw = BOOT_LOCK_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("shutdown: could not read %s (%s)", BOOT_LOCK_PATH, exc)
        return None
    try:
        rec = json.loads(raw)
        return rec if isinstance(rec, dict) else None
    except (json.JSONDecodeError, ValueError):
        log.warning("shutdown: lock file unreadable — treating as stale")
        return None


def _clear_lock() -> None:
    try:
        BOOT_LOCK_PATH.unlink(missing_ok=True)
    except OSError as exc:
        log.debug("shutdown: lock clear best-effort skipped (%s)", exc)


def _psutil_module():
    try:
        import psutil

        return psutil
    except ImportError:
        return None


def _verify_boot_owner(pid: int) -> bool:
    """True if ``pid`` is a live python process running boot.py in OMEGA_ROOT."""
    if pid <= 0:
        return False
    psutil = _psutil_module()
    if psutil is None:
        return False
    root = str(OMEGA_ROOT.resolve())
    try:
        proc = psutil.Process(pid)
        argv = " ".join(str(a) for a in (proc.cmdline() or []))
        if "boot.py" not in argv:
            return False
        if any(s in argv for s in ("omega-portable-lite", "worktrees", "pytest")):
            return False
        try:
            cwd = str(Path(proc.cwd()).resolve())
        except Exception:
            return False
        return cwd == root
    except Exception:
        return False


def _systemd_unit_stop(pid: int, create_time: Any = None) -> Optional[bool]:
    """Stop through systemd when ``pid`` is omega.service's main process.

    Operator ruling 2026-08-10: "when i shut it down it stays down." Signaling
    the pid directly looks like a CRASH to systemd — a drain that outlives the
    stop wait escalates to SIGKILL, the unit lands 'failed', and
    Restart=on-failure resurrects the substrate the operator just killed. An
    explicit ``systemctl stop`` never evaluates Restart=, however ugly the
    stop gets, so appliance resurrection stays reserved for real crashes.

    Returns None when the pid is not under omega.service (dev/console runs
    fall back to the signal path), else whether the stop landed.
    """
    if sys.platform == "win32" or shutil.which("systemctl") is None:
        return None
    try:
        probe = subprocess.run(
            ["systemctl", "--user", "show", "omega.service",
             "--property=MainPID", "--value"],
            capture_output=True, text=True, timeout=10,
        )
        main_pid = int((probe.stdout or "0").strip() or 0)
    except Exception:
        return None
    if main_pid <= 0 or main_pid != pid:
        return None
    try:
        # Blocks for the unit's full TimeoutStopSec on a slow drain; the
        # subprocess timeout only guards a wedged systemctl itself.
        subprocess.run(
            ["systemctl", "--user", "stop", "omega.service"],
            capture_output=True, text=True, timeout=600,
        )
    except Exception as exc:
        log.warning("shutdown: systemctl stop failed for pid=%s (%s)", pid, exc)
        return False
    return not _pid_alive(pid, create_time)


def _default_send_stop(pid: int) -> bool:
    """Graceful stop: SIGINT on Unix; CTRL_BREAK then terminate on Windows."""
    try:
        import psutil

        proc = psutil.Process(pid)
        if sys.platform == "win32":
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            except Exception:
                proc.terminate()
        else:
            proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=_STOP_WAIT_SECONDS)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=_STOP_WAIT_SECONDS)
            return False
        return not proc.is_running()
    except ImportError:
        try:
            if sys.platform == "win32":
                os.kill(pid, signal.CTRL_BREAK_EVENT)
            else:
                os.kill(pid, signal.SIGINT)
        except OSError:
            return True
        deadline = time.monotonic() + _STOP_WAIT_SECONDS
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                return True
            time.sleep(0.2)
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return True
        return not _pid_alive(pid)
    except Exception as exc:
        log.warning("shutdown: graceful stop failed for pid=%s (%s)", pid, exc)
        return False


def emit_shutdown_result(result: ShutdownResult, *, as_json: bool = False) -> None:
    """Write ``result`` to stdout in human or JSON form."""
    if as_json:
        print(json.dumps(asdict(result), indent=2))
        return
    print(result.message)
    if result.status != "refused":
        print(result.dev_bridge_note)
        print(result.model_runtime_note)


def _default_runtime_sweep() -> dict[str, Any]:
    """Stop orphaned local and portable LM Studio runtime processes."""
    from scripts.portable_runtime_shutdown import sweep_portable_runtime_processes

    return sweep_portable_runtime_processes(OMEGA_ROOT)


def _default_dev_bridge_sweep() -> dict[str, Any]:
    """The retired standalone agent has no process to reap."""
    return {"ok": True, "swept": False, "reason": "not_running"}


def _sweep_dev_bridge_after_stop(
    result: ShutdownResult,
    *,
    dry_run: bool,
    sweep: Optional[Callable[[], dict[str, Any]]] = None,
) -> ShutdownResult:
    """Backstop boot teardown and report a surviving owned bridge as failure."""
    if dry_run or result.status == "refused":
        return result
    outcome = (sweep or _default_dev_bridge_sweep)() or {}
    swept = bool(outcome.get("swept"))
    if outcome.get("ok"):
        note = (
            "Live-chat agent: standalone process cleared."
            if swept
            else "Live-chat agent: no standalone process remained."
        )
        return replace(result, dev_bridge_swept=swept, dev_bridge_note=note)
    reason = str(outcome.get("reason") or "unknown")
    note = f"Live-chat agent teardown failed ({reason})."
    return replace(
        result,
        status="stop_failed",
        exit_code=1,
        message=f"{result.message}; {note}",
        dev_bridge_swept=False,
        dev_bridge_note=note,
    )


def _sweep_runtime_after_stop(
    result: ShutdownResult,
    *,
    dry_run: bool,
    sweep: Optional[Callable[[], dict[str, Any]]] = None,
) -> ShutdownResult:
    """Run the ownership-independent runtime sweep unless previewing or refused.

    Reaps a runtime orphaned by a crashed boot (status nothing_running /
    stale_lock_cleared) and backstops a normal stop, so nothing lingers on
    :11434 (operator 2026-07-21). Folds the outcome into ``runtime_swept`` + the
    operator-facing note. Never raises."""
    if dry_run or result.status == "refused":
        return result
    outcome = (sweep or _default_runtime_sweep)() or {}
    swept = bool(outcome.get("swept"))
    if not outcome.get("ok", False):
        reason = str(outcome.get("reason") or "unknown")
        note = f"Portable runtime teardown failed ({reason})."
        return replace(
            result,
            status="stop_failed",
            exit_code=1,
            message=f"{result.message}; {note}",
            runtime_swept=swept,
            model_runtime_note=note,
        )
    if swept:
        note = "Portable Omega runtimes and model processes were stopped."
    else:
        note = "No portable Omega runtime processes remained."
    return replace(result, runtime_swept=swept, model_runtime_note=note)


def shutdown_substrate(
    *,
    dry_run: bool = False,
    send_stop: Callable[[int], bool] = _default_send_stop,
) -> ShutdownResult:
    """Stop the substrate if boot.lock points at a live boot.py. Never raises."""
    rec = resolve_lock()
    if rec is None:
        prefix = "would do nothing" if dry_run else "nothing running"
        return ShutdownResult(
            status="nothing_running",
            pid=None,
            lock_cleared=False,
            dry_run=dry_run,
            exit_code=0,
            message=f"{prefix} (no boot lock)",
        )

    pid = int(rec.get("pid", 0) or 0)
    create_time = rec.get("create_time")

    if not pid or not _pid_alive(pid, create_time):
        pid_label = pid or "?"
        if dry_run:
            return ShutdownResult(
                status="stale_lock_cleared",
                pid=pid or None,
                lock_cleared=False,
                dry_run=True,
                exit_code=0,
                message=f"would clear stale boot lock for pid={pid_label}",
            )
        _clear_lock()
        return ShutdownResult(
            status="stale_lock_cleared",
            pid=pid or None,
            lock_cleared=True,
            dry_run=False,
            exit_code=0,
            message=f"nothing running (stale boot lock cleared for pid={pid_label})",
        )

    if not _verify_boot_owner(pid):
        return ShutdownResult(
            status="refused",
            pid=pid,
            lock_cleared=False,
            dry_run=dry_run,
            exit_code=1,
            message=(
                f"refusing to stop pid={pid}: lock owner is not boot.py in {OMEGA_ROOT}\n"
                f"  .\\.venv-brain\\Scripts\\python.exe scripts\\shutdown.py --dry-run\n"
                f"  .\\.venv-brain\\Scripts\\python.exe scripts\\shutdown.py --json"
            ),
        )

    if dry_run:
        return ShutdownResult(
            status="stopped",
            pid=pid,
            lock_cleared=False,
            dry_run=True,
            exit_code=0,
            message=f"would stop substrate boot pid={pid}",
        )

    # systemd-managed boots stop through the unit (never resurrected);
    # injected send_stop callables (tests) keep the direct path.
    stopped = None
    if send_stop is _default_send_stop:
        stopped = _systemd_unit_stop(pid, create_time)
    if stopped is None:
        stopped = send_stop(pid)
    if stopped:
        _clear_lock()
        return ShutdownResult(
            status="stopped",
            pid=pid,
            lock_cleared=True,
            dry_run=False,
            exit_code=0,
            message=f"stopped substrate boot pid={pid}",
        )

    return ShutdownResult(
        status="stop_failed",
        pid=pid,
        lock_cleared=False,
        dry_run=False,
        exit_code=1,
        message=f"substrate boot pid={pid} did not stop cleanly — lock retained",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gracefully stop the running Omega substrate (boot.py + HUD)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_HELP_EPILOG,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the stop plan without sending signals or clearing locks",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON on stdout (for agents and scripts)",
    )
    args = parser.parse_args(argv)
    result = shutdown_substrate(dry_run=args.dry_run)
    result = _sweep_dev_bridge_after_stop(result, dry_run=args.dry_run)
    result = _sweep_runtime_after_stop(result, dry_run=args.dry_run)
    emit_shutdown_result(result, as_json=args.json)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

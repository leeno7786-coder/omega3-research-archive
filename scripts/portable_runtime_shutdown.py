"""Stop only the runtime processes belonging to this portable Omega bundle.

The shutdown button is allowed to stop portable Omega, LM Studio, and the
optional WSL LM Studio payload. It must not use process names alone: another
user's Python, LM Studio, or model server may be running on the same host.
Every target is therefore proven by its portable root and an owned command
line before a process tree is terminated.
"""

from __future__ import annotations

import os
import signal
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

from scripts.omega_launcher import (
    LM_STUDIO_ROOT_RELATIVE,
    LINUX_LM_STUDIO_ROOT_RELATIVE,
    LINUX_RUNTIME_SCRIPT_RELATIVE,
    _windows_directory_to_wsl,
    ssd_root,
)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_OMEGA_PROCESS_MARKERS = (
    "boot.py",
    "runtime.local_services",
    "hud.standalone",
    "hud.server",
    "crispasr",
)

_LINUX_STOP_COMMAND = r'''
target="$PWD/lm-studio.AppImage"
collect_descendants() {
    pending="$1"
    while [ -n "$pending" ]; do
        pid="${pending%% *}"
        pending="${pending#"$pid"}"
        pending="${pending# }"
        printf '%s\n' "$pid"
        children="$(ps -eo pid=,ppid= | awk -v parent="$pid" '$2 == parent {print $1}')"
        for child in $children; do
            pending="$pending $child"
        done
    done
}
roots="$(ps -eo pid=,args= | awk -v target="$target" 'index($0, target) {print $1}')"
targets=""
for root in $roots; do
    targets="$targets $(collect_descendants "$root")"
done
for pid in $targets; do
    kill -TERM "$pid" 2>/dev/null || true
done
sleep 1
for pid in $targets; do
    kill -KILL "$pid" 2>/dev/null || true
done
'''.strip()


def _resolved_path(value: Any) -> Path | None:
    if not value:
        return None
    try:
        return Path(str(value)).resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _is_under(candidate: Path | None, root: Path) -> bool:
    return candidate is not None and (candidate == root or root in candidate.parents)


def _command_line(info: dict[str, Any]) -> str:
    cmdline = info.get("cmdline") or []
    if isinstance(cmdline, str):
        return cmdline
    return " ".join(str(item) for item in cmdline)


def _is_owned_process(info: dict[str, Any], product: Path, windows_runtime: Path) -> bool:
    command = _command_line(info).casefold()
    cwd = _resolved_path(info.get("cwd"))
    executable = _resolved_path(info.get("exe"))
    if _is_under(cwd, product) or _is_under(executable, product):
        return any(marker in command for marker in _OMEGA_PROCESS_MARKERS)
    if _is_under(cwd, windows_runtime) or _is_under(executable, windows_runtime):
        return True
    return False


def _taskkill_tree(
    pid: int,
    *,
    run: Callable[..., Any],
) -> None:
    if sys.platform == "win32":
        run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )
        return
    os.kill(pid, signal.SIGTERM)


def _stop_linux_runtime(
    linux_root: Path,
    *,
    run: Callable[..., Any],
    which: Callable[[str], str | None],
) -> str:
    script = linux_root / LINUX_RUNTIME_SCRIPT_RELATIVE.name
    if not script.is_file():
        return "not_available"
    if sys.platform != "win32":
        return "not_available"
    if which("wsl.exe") is None:
        return "wsl_unavailable"
    run(
        [
            "wsl.exe",
            "--cd",
            _windows_directory_to_wsl(linux_root),
            "sh",
            "-lc",
            _LINUX_STOP_COMMAND,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        creationflags=CREATE_NO_WINDOW,
    )
    return "stop_requested"


def sweep_portable_runtime_processes(
    product: Path,
    *,
    process_iter: Callable[[list[str]], Iterable[Any]] | None = None,
    run: Callable[..., Any] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Terminate owned local runtime trees and request a WSL runtime stop."""
    product = Path(product).resolve()
    portable_root = ssd_root(product)
    windows_runtime = portable_root / LM_STUDIO_ROOT_RELATIVE
    linux_runtime = portable_root / LINUX_LM_STUDIO_ROOT_RELATIVE
    runner = run or subprocess.run
    wsl_which = which or shutil.which
    try:
        if process_iter is None:
            import psutil

            process_iter = psutil.process_iter
        processes = process_iter(["pid", "ppid", "cwd", "cmdline", "exe"])
    except Exception as exc:
        return {
            "ok": False,
            "swept": False,
            "reason": "process_scan_failed",
            "detail": str(exc)[:240],
        }

    owned: dict[int, int] = {}
    failures: list[str] = []
    for process in processes:
        try:
            info = dict(process.info)
            pid = int(info.get("pid") or 0)
            if pid <= 0 or pid == os.getpid():
                continue
            if not _is_owned_process(info, product, windows_runtime):
                continue
            owned[pid] = int(info.get("ppid") or 0)
        except Exception as exc:
            failures.append(str(exc)[:160])

    targets: list[int] = []
    for pid, parent in owned.items():
        seen: set[int] = set()
        has_owned_ancestor = False
        while parent in owned and parent not in seen:
            has_owned_ancestor = True
            seen.add(parent)
            parent = owned[parent]
        if not has_owned_ancestor:
            targets.append(pid)
    for pid in targets:
        try:
            _taskkill_tree(pid, run=runner)
        except Exception as exc:
            failures.append(str(exc)[:160])

    try:
        linux_status = _stop_linux_runtime(linux_runtime, run=runner, which=wsl_which)
    except Exception as exc:
        linux_status = "stop_failed"
        failures.append(str(exc)[:160])

    if linux_status == "wsl_unavailable":
        failures.append("WSL is unavailable for the transferred Linux runtime")
    ok = not failures
    swept = bool(targets or linux_status == "stop_requested")
    result: dict[str, Any] = {
        "ok": ok,
        "swept": swept,
        "processes": len(targets),
        "linux": linux_status,
    }
    if failures:
        result["reason"] = "runtime_stop_failed"
        result["detail"] = "; ".join(failures)[:240]
    return result


__all__ = ["CREATE_NO_WINDOW", "sweep_portable_runtime_processes"]

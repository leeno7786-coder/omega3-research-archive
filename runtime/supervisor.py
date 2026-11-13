"""Brain-owned Omega local-services process lifecycle.

The brain boots the Omega-owned local services when they are down. Those
services provide Parakeet, Qwen TTS, and DME; the mind seat is selected separately
through the operator's LM Studio or cloud connector settings.

The spawn path launches ``.venv-brain\\Scripts\\python.exe -m
runtime.local_services`` from ``core.paths.OMEGA_ROOT`` and polls /health until
the local services answer.

Spawning is not owning (operator 2026-08-11). The brain used to spawn the
gateway and then never look at it again: no reap, no probe, no respawn. When the
gateway died mid-session the handle went <defunct> and STAYED there, and every
runtime call — cognition, TTS, and voice — failed "Connection refused" against an
appliance that still looked alive. ``start_gateway_watchdog`` closes that: a
daemon thread probes /health on a short interval, reaps the corpse, and respawns
through ``ensure_runtime_process`` so teardown ownership stays correct. Child
output is sunk to guardian/logs/runtime.gateway.log rather than DEVNULL, because
a death you cannot explain is a death you will get again.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from core.paths import portable_python_path

log = logging.getLogger("omega.runtime.supervisor")

# Ceiling for the gateway to answer /health after spawn, and — because the
# timeout path TREE-KILLS the child — also the point at which we declare a
# gateway that is merely slow to be a gateway that is broken.
#
# Was 120s on the premise that the gateway "binds fast (model autoload runs in a
# background thread of its lifespan), so this covers exe + venv cold start". The
# first half is right and the second half was not: uvicorn binds only after
# gateway/cli.build_runtime_app returns, and that runs the SYNCHRONOUS host
# probe (Vulkan/CUDA detection plus the ORT NPU smoke tests) before the app
# object exists.
#
# Measured 2026-08-11 (RISK 4(a)). Warm, this whole path takes 0.5s to /health.
# With 18GB of venv + weights evicted from page cache first, still 0.5s — disk
# is not the cost. But eleven seconds after a machine boot, contending with
# everything else systemd started and with GPU/NPU drivers still initializing,
# the gateway spawned at 12:56:00 had not answered by 12:58:01, when this
# timeout expired and killed it. It was not hung; it was starting. The brain
# then ran degraded for two and a half minutes and crashed on the cognition
# probe at 13:00:40.
#
# 300s because the true cold ceiling is unknown — all we measured is that it
# exceeds 121s — and because the cost of being wrong is asymmetric: waiting an
# extra minute for a slow gateway costs a slow boot, killing one that was still
# starting costs the whole boot. Deliberately the same number as
# runtime.model_control.COGNITION_PROBE_WAIT_S: these are two guards on the same
# cold start, and a spawn ceiling shorter than the probe's wait would leave the
# probe waiting out a deadline for a gateway this function had already killed.
_DEFAULT_BOOT_TIMEOUT_S = 300.0
_SWEEP_TIMEOUT_S = 90.0
_GRACEFUL_EXIT_WAIT_S = 10.0

# Gateway watchdog (operator 2026-08-11 — "the gateway died and nobody noticed").
#
# Measured from guardian/logs + journalctl: the brain spawned the gateway at
# 14:15:42 (pid 74622), served its last request at 14:15:54, and the process
# then died. Nothing ever called poll() on the handle, so it sat <defunct> as an
# unreaped zombie while the brain kept issuing calls that all failed with
# "Connection refused" — the first observed at 14:41:15 — until teardown finally
# swept it at 14:55:45. Forty minutes of an appliance that looked alive and
# answered nothing. A crash-loop is loud; this was silent, which is worse.
#
# Three seconds keeps a dead local-services process inside the brain's retry
# envelope rather than across a whole session.
_WATCHDOG_INTERVAL_S = 3.0
# Strictly shorter than the interval — a probe that outlived its own tick would
# stack ticks behind it and turn the cadence into a queue.
_WATCHDOG_PROBE_TIMEOUT_S = 2.0
# Consecutive silent probes required before recycling a gateway whose process is
# STILL ALIVE. Measured the hard way on the first live run of this watchdog: a
# single missed probe respawned a healthy gateway, and because respawning sweeps
# first (runtime.sh stop), the sweep POSTed /admin/orchestration/shutdown to the
# live process and got 200 OK — the watchdog killed exactly what it was built to
# protect, then charged the operator a 262k-KV reload for it.
#
# So the two verdicts get different evidentiary bars, by how certain the evidence
# is. A reaped exit code is proof of death and revives on the spot (~3s — the
# zombie case this whole watchdog exists for). Silence from a living process is
# only a rumour: it is equally the signature of a big model load or a long
# generate holding the loop, so it must persist ~15s before we act on it.
_WATCHDOG_UNREACHABLE_STRIKES = 5
# Single-generation rotation. The gateway's stderr is the ONLY account of why it
# died, so it must survive; capping keeps it from becoming the next
# firehose.jsonl (633MB in guardian/logs today).
_GATEWAY_LOG_MAX_BYTES = 8 * 1024 * 1024

_owned: dict[str, Any] = {}
_lock = threading.RLock()
_watchdog_thread: Optional[threading.Thread] = None
# Set by stop_gateway_watchdog AND by shutdown_runtime_process itself, so a
# deliberate teardown closes the watchdog gate no matter which path called it.
_watchdog_stop = threading.Event()
# Consecutive failed probes against a gateway whose process is still alive.
_watchdog_strikes = 0


# ── Win32 Job Object — the runtime dies with the brain, enforced by the kernel ──
#
# Linux gets this from systemd (KillMode=mixed, 330s stop timeout): the init system
# is the backstop, so boot.py never has to survive its own death. Windows has no
# equivalent, and a HARD kill of boot.py (crash, taskkill /F, harness teardown)
# leaves the gateway, its workers and the FLM sidecar alive — holding :11434 and the
# XDNA2 claim until something invokes the sweep. A signal handler cannot close that
# gap, because a hard kill never runs one.
#
# A job with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE moves the guarantee into the kernel.
# Processes we spawn are assigned to the job and their own children inherit
# membership, so the whole tree is covered. When THIS process dies by any means its
# handles close, the job's last handle goes with them, and the kernel terminates the
# set. Nested jobs are fine on Win8+, so running under another job does not break it.
#
# Failure-soft by contract, like every other path in this module: if the job cannot
# be created or assigned we log and continue — the service process is still
# tracked for explicit teardown. Set OMEGA_RUNTIME_NO_JOB=1 to opt out and let
# the local service outlive boot.
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001

_job_handle: Any = None
_job_disabled = False


def _win32_job_api() -> Any:
    """kernel32 with explicit signatures + the extended-limit struct.

    argtypes/restype are declared because ctypes defaults a return to C ``int``:
    on 64-bit that TRUNCATES a HANDLE to its low 32 bits, and the resulting bogus
    handle fails every later call for reasons that look nothing like the cause.
    """
    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return ctypes, k32, _ExtendedLimits


def _ensure_kill_on_close_job() -> Any:
    """Create the job once and keep its handle for this process's lifetime.

    The handle is held in a module global on purpose: if it were garbage collected
    the job would close early and take the runtime down with it.
    """
    global _job_handle, _job_disabled
    if os.name != "nt" or _job_disabled:
        return None
    with _lock:
        if _job_handle is not None:
            return _job_handle
        if (os.environ.get("OMEGA_RUNTIME_NO_JOB") or "").strip():
            log.info("runtime job object disabled by OMEGA_RUNTIME_NO_JOB — sweep-only teardown")
            _job_disabled = True
            return None
        try:
            ctypes, k32, extended_cls = _win32_job_api()
            handle = k32.CreateJobObjectW(None, None)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            info = extended_cls()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(
                handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                err = ctypes.get_last_error()
                k32.CloseHandle(handle)
                raise ctypes.WinError(err)
            _job_handle = handle
            log.info("runtime job object armed — spawned runtime dies with this process")
            return _job_handle
        except Exception as exc:  # noqa: BLE001 — never block a boot on this
            log.warning(
                "runtime job object unavailable (%s) — falling back to sweep-only teardown", exc
            )
            _job_disabled = True
            return None


def bind_to_brain_lifetime(pid: int) -> bool:
    """Assign ``pid`` to the kill-on-close job. True when the kernel now owns it."""
    job = _ensure_kill_on_close_job()
    if job is None:
        return False
    try:
        ctypes, k32, _ = _win32_job_api()
        handle = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid))
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not k32.AssignProcessToJobObject(job, handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            k32.CloseHandle(handle)
        log.info("runtime pid=%s bound to brain lifetime (kill-on-close job)", pid)
        return True
    except Exception as exc:  # noqa: BLE001 — the sweep is still the backstop
        log.warning(
            "could not bind runtime pid=%s to the job (%s) — sweep remains the backstop", pid, exc
        )
        return False


def runtime_root() -> Path:
    """Return the relocatable Omega product root that owns local services."""
    from core.paths import OMEGA_ROOT

    return OMEGA_ROOT.resolve()


def _gateway_exe(root: Path) -> Path:
    """The product venv's own Python, never a generated console launcher.

    pip bakes an ABSOLUTE interpreter path into the console-script launcher, so the
    .exe stops working the moment the bundle changes drive letter or mount point —
    the exact failure that bricked a boot when this tree moved. The venv's own
    python locates its own prefix, so it survives any relocation. Invoked as
    ``-m runtime.local_services`` so the process stays inside the product tree.
    """
    return portable_python_path(root)


def _gateway_python(root: Path) -> Path:
    return portable_python_path(root)


def _gateway_launch_command(root: Path, host: str, port: int) -> list[str] | None:
    """Launch the Omega-owned local-services module from the product venv.

    The console-script launcher bakes an absolute interpreter path and stops working
    the moment the bundle relocates (see ``_gateway_exe``); the venv's own python is
    the one relocatable entrypoint, so it is the only form this ever spawns.
    """
    python = _gateway_python(root)
    if python.is_file():
        return [
            str(python),
            "-m",
            "runtime.local_services",
            "--host",
            host,
            "--port",
            str(port),
        ]
    return None


def _host_port(base_url: str) -> tuple[str, int]:
    parsed = urlparse(base_url)
    return parsed.hostname or "127.0.0.1", int(parsed.port or 11434)


def _http_health(base_url: str, timeout_s: float = 3.0) -> bool:
    # Direct probe — RuntimeClient.health() caches, and a spawn-wait loop must
    # see the real transition, not a cached miss.
    try:
        import requests

        response = requests.get(f"{base_url}/health", timeout=timeout_s)
        if response.status_code != 200:
            return False
        payload = response.json()
        return isinstance(payload, dict) and payload.get("service") == "omega-local-services"
    except Exception:
        return False


def gateway_log_path() -> Path:
    """guardian/logs/runtime.gateway.log — the gateway's stdout+stderr sink."""
    from core.paths import GUARDIAN_LOGS_ROOT

    return Path(GUARDIAN_LOGS_ROOT) / "runtime.gateway.log"


def _open_gateway_log_sink() -> Any:
    """Append handle for child output, or DEVNULL when the sink can't be opened.

    Every runtime child used to be spawned with stdout/stderr=DEVNULL, so when
    the gateway died on 2026-08-11 there was NOTHING to read: no traceback, no
    exit banner, no OOM notice (the kernel logged no OOM kill either). The cause
    of that death is permanently unrecoverable. Sinking to a real file is what
    makes the next one explainable. Mirrors the HUD shutdown-script sink in
    hud/server.py: open a file, hand it to Popen, merge stderr into it.

    Failure-soft by contract — a sink we cannot open degrades to DEVNULL rather
    than blocking the spawn. Losing the log is bad; refusing to boot is worse.
    """
    try:
        path = gateway_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size > _GATEWAY_LOG_MAX_BYTES:
                os.replace(path, path.with_suffix(".log.1"))
        except FileNotFoundError:
            pass
        return open(path, "a", encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 — a log sink never blocks a spawn
        log.warning("gateway log sink unavailable (%s) — falling back to DEVNULL", exc)
        return subprocess.DEVNULL


def _close_sink(sink: Any) -> None:
    """Drop the PARENT's handle on a sink after the child inherited it.

    The child holds its own dup of the fd, so closing ours does not truncate its
    output. Skipping this would leak one fd per spawn — invisible at boot, but
    the watchdog respawns, so over a long session it is a real leak.
    """
    if sink is subprocess.DEVNULL:
        return
    try:
        sink.close()
    except Exception:  # noqa: BLE001 — a sink close never breaks a lifecycle path
        pass


def _script_command(script: Path, *args: str) -> Optional[list[str]]:
    """Platform launcher for an optional local lifecycle script.

    Windows runs the ``.ps1``; POSIX runs the ``.sh`` sibling of the same
    name. A missing script means "nothing to run".
    """
    if sys.platform == "win32":
        if not script.is_file():
            return None
        return [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *args,
        ]
    sh = script.with_suffix(".sh")
    if not sh.is_file():
        return None
    return ["bash", str(sh), *args]


def _default_sweep(root: Path) -> None:
    """Retain the pre-spawn hook for callers; ownership is process-local now."""
    del root


def _default_tree_kill(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return
    # POSIX: the gateway is spawned with start_new_session=True, so its process
    # group id == its pid — SIGKILL the whole group (gateway + workers), falling
    # back to the single pid for a process we didn't session-lead.
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _default_stop_all(root: Path) -> None:
    """There is no second process registry; the supervisor owns the service PID."""
    del root


def sweep_runtime_processes(
    root: Optional[Path] = None,
    *,
    stop_all: Optional[Callable[[Path], None]] = None,
) -> dict[str, Any]:
    """Reap orphaned runtime processes for this bundle, independent of ownership.

    Public entry for teardown paths that have no live-boot ownership to route
    through — chiefly ``scripts/shutdown.py`` when boot.py already died and left
    the runtime up. Returns ``{"ok", "swept", ...}``. Never raises."""
    root = root or runtime_root()
    try:
        (stop_all or _default_stop_all)(root)
        return {"ok": True, "swept": False, "reason": "owned_by_omega_boot"}
    except Exception as exc:  # noqa: BLE001 — never raise into a teardown path
        log.warning("runtime sweep failed: %s", exc)
        return {"ok": False, "swept": False, "reason": "sweep_error", "detail": str(exc)[:160]}


def runtime_owned() -> bool:
    with _lock:
        return bool(_owned.get("pid"))


def ensure_runtime_process(
    *,
    client: Any = None,
    popen: Optional[Callable[..., Any]] = None,
    health_probe: Optional[Callable[[str], bool]] = None,
    sweep: Optional[Callable[[Path], None]] = None,
    timeout_s: Optional[float] = None,
    poll_interval_s: float = 1.0,
    abort_check: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    """Boot the runtime gateway if it is down. Returns ``spawned`` honestly.

    Failure is explicit: a missing executable or health timeout returns
    ``{"ok": False, "reason": ...}``. Required callers must reject that result;
    ``boot.boot`` converts it to an attributable substrate startup failure.

    ``abort_check`` is for callers that can be OVERTAKEN by a shutdown while
    this is waiting: it is polled inside the health loop, and when it answers
    True the child we spawned is tree-killed before returning. Only the gateway
    watchdog passes one — boot's own call cannot be overtaken (nothing is
    tearing down a boot that has not finished) and leaves this None, so its
    behaviour is unchanged. See ``gateway_watchdog_tick`` for why the post-spawn
    re-check alone is not enough.
    """
    if client is None:
        from runtime.client import get_runtime_client

        client = get_runtime_client()
    probe = health_probe or _http_health
    base_url = str(client.base_url).rstrip("/")

    if probe(base_url):
        return {"ok": True, "spawned": False}

    root = runtime_root()
    host, port = _host_port(base_url)
    command = _gateway_launch_command(root, host, port)
    if command is None:
        python = _gateway_python(root)
        log.critical(
            "runtime gateway down and interpreter missing (%s) — cannot boot the brain's runtime",
            python,
        )
        return {
            "ok": False,
            "spawned": False,
            "reason": "runtime_exe_missing",
            "detail": str(python),
        }

    (sweep or _default_sweep)(root)

    env = dict(os.environ)
    env["OMEGA_MODEL_DIR"] = str(root / "models")
    # Cloud brain seat: tell the gateway NOT to warm/pin the local resident LLM
    # (gemma). Cognition runs on the frontier 27B, so a resident gemma is dead VRAM.
    # A1 needs no flag — it is lazy-load and the client.py cloud override diverts the
    # reflection before it ever loads. Local-brain boots leave the flag unset;
    # their response seat is still lazy and loads on the first generate request.
    try:
        from runtime import brain_cloud

        if brain_cloud.enabled():
            env["OMEGA_SKIP_RESIDENT_LLM"] = "1"
    except Exception:
        pass
    # Child output goes to a real file, never DEVNULL (operator 2026-08-11). The
    # gateway that died today left no account of itself because this was DEVNULL.
    sink = _open_gateway_log_sink()
    popen_kwargs: dict[str, Any] = {
        "cwd": str(root),
        "env": env,
        "stdout": sink,
        "stderr": subprocess.STDOUT,
    }
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        # POSIX: own session (os.setsid) — detaches the gateway from the boot
        # process group so a Ctrl-C into boot.py never SIGINTs it, and makes
        # _default_tree_kill's killpg reap the gateway plus all its workers.
        popen_kwargs["start_new_session"] = True
        # No job object here: the gateway is told which brain owns it and takes
        # its own process group down once that brain is gone
        # (local_services.exit_with_parent).
        env["OMEGA_RUNTIME_PARENT_PID"] = str(os.getpid())
    try:
        process = (popen or subprocess.Popen)(command, **popen_kwargs)
    finally:
        _close_sink(sink)
    log.info("runtime gateway spawned pid=%s (%s:%s)", process.pid, host, port)
    # Bind before the health wait, not after: a gateway that hangs during startup has
    # to be covered too, and every worker it spawns from here on inherits membership.
    # The gap between CreateProcess and the assignment is microseconds against a
    # gateway that takes seconds to reach its lifespan, so no worker escapes in it.
    bind_to_brain_lifetime(process.pid)

    if timeout_s is None:
        raw = (os.environ.get("OMEGA_RUNTIME_BOOT_TIMEOUT_S") or "").strip()
        try:
            timeout_s = float(raw) if raw else _DEFAULT_BOOT_TIMEOUT_S
        except ValueError:
            timeout_s = _DEFAULT_BOOT_TIMEOUT_S
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return {
                "ok": False,
                "spawned": True,
                "reason": "runtime_exited_early",
                "exit": process.returncode,
            }
        # A shutdown that began while we were waiting. Checked HERE and not only
        # after the loop because the wait is up to _DEFAULT_BOOT_TIMEOUT_S (300s
        # since the cold-boot measurement), teardown's watchdog join gives up
        # after 2s, and the caller is a DAEMON thread — so the process can exit
        # with this loop still running. The child is already spawned by now and
        # teardown's sweep has already gone past, so anything that binds :11434
        # after this point is an orphan the brain will never reap. Kill it here,
        # while we still have the pid.
        if abort_check is not None and abort_check():
            log.info(
                "runtime gateway spawn abandoned: shutdown began while waiting on /health "
                "— tree-killing the child we spawned (pid=%s)",
                process.pid,
            )
            _default_tree_kill(int(process.pid))
            return {"ok": False, "spawned": True, "reason": "shutdown_in_progress"}
        if probe(base_url):
            with _lock:
                _owned.clear()
                _owned.update({"pid": int(process.pid), "process": process, "base_url": base_url})
            return {"ok": True, "spawned": True, "pid": int(process.pid)}
        if poll_interval_s:
            time.sleep(poll_interval_s)

    _default_tree_kill(int(process.pid))
    return {"ok": False, "spawned": True, "reason": "runtime_health_timeout"}


def shutdown_runtime_process(
    *,
    post: Optional[Callable[[str], None]] = None,
    process_tree_kill: Optional[Callable[[int], None]] = None,
    stop_all: Optional[Callable[[Path], None]] = None,
    exit_wait_s: float = _GRACEFUL_EXIT_WAIT_S,
) -> dict[str, Any]:
    """Tear down the Omega-owned local-services process.

    Two layers, so teardown is reliable regardless of how boot ended:
      1. Ownership-based graceful POST + tree-kill of the pid the brain spawned
         (when this process still holds ``_owned``).
      2. An optional caller-supplied cleanup hook for platform process groups.

    External runtime installations are never inspected or modified."""
    # Close the watchdog gate BEFORE anything is killed. Doing it here rather
    # than in the callers means every teardown path — boot_lifecycle,
    # scripts/shutdown.py, the HUD button — is covered by construction, and a
    # deliberate shutdown can never lose a race with a 3s revival tick.
    _watchdog_stop.set()
    with _lock:
        owned = dict(_owned)
        _owned.clear()

    sweep = stop_all or _default_stop_all
    root = runtime_root()

    if not owned.get("pid"):
        # No process owned by this boot remains to terminate. A caller may still
        # provide a process-group cleanup hook for its own service tree.
        sweep(root)
        return {"ok": True, "owned": False, "swept": False}

    base_url = owned.get("base_url", "")
    process = owned.get("process")
    graceful = False

    def _default_post(url: str) -> None:
        import requests

        requests.post(url, timeout=5.0)

    try:
        # Graceful first — the local service lifespan closes its owned workers.
        (post or _default_post)(f"{base_url}/admin/orchestration/shutdown")
        deadline = time.monotonic() + exit_wait_s
        while time.monotonic() < deadline:
            if process is None or process.poll() is not None:
                graceful = True
                break
            time.sleep(0.25)
    except Exception as exc:
        log.warning("graceful runtime shutdown failed: %s", exc)

    if not graceful:
        (process_tree_kill or _default_tree_kill)(int(owned["pid"]))

    # Give the caller's process-tree hook one final chance after graceful exit.
    sweep(root)
    log.info("Omega local services torn down (pid=%s graceful=%s)", owned["pid"], graceful)
    return {"ok": True, "owned": True, "graceful": graceful, "pid": int(owned["pid"]), "swept": False}


# --------------------------------------------------------------- gateway watchdog


def _shutdown_in_progress() -> bool:
    """True once the boot lifecycle has begun tearing the appliance down.

    ``shutdown_system`` latches its flag BEFORE it walks the components, so this
    covers the window between "shutdown started" and "the runtime_process step
    actually kills the gateway" — during which a watchdog consulting only its
    own stop event would cheerfully resurrect a gateway that is about to die,
    and hand the next boot an orphan holding :11434.
    """
    try:
        from core.boot_lifecycle import shutdown_in_progress

        return bool(shutdown_in_progress())
    except Exception:  # noqa: BLE001 — an unreadable flag must not stall the watchdog
        return False


def _watchdog_should_stop() -> bool:
    """The one "is a teardown under way" question, asked from both sides.

    The tick gates on it before doing anything, and ``ensure_runtime_process``
    polls it through ``abort_check`` while waiting on /health — the same fact,
    so a shutdown cannot be seen by one and missed by the other.
    """
    return bool(_watchdog_stop.is_set() or _shutdown_in_progress())


def _watchdog_probe(base_url: str) -> bool:
    return _http_health(base_url, timeout_s=_WATCHDOG_PROBE_TIMEOUT_S)


def _reap_owned_process() -> Optional[int]:
    """waitpid the owned gateway if it has exited; return its exit code or None.

    ``Popen.poll()`` IS the reap — it waitpid()s with WNOHANG. That call is
    precisely what nothing made on 2026-08-11, which is why the dead gateway
    stayed <defunct> in the process table for the rest of the session.

    Clearing the handle also protects teardown: once reaped, the kernel is free
    to recycle that pid, and ``_default_tree_kill`` does killpg — so a stale
    owned pid is a loaded gun pointed at an unrelated process group.
    """
    with _lock:
        process = _owned.get("process")
        if process is None:
            return None
        try:
            code = process.poll()
        except Exception:  # noqa: BLE001 — a broken handle is not a reason to die
            return None
        if code is None:
            return None
        _owned.clear()
        return int(code)


def gateway_watchdog_tick(
    *,
    client: Any = None,
    health_probe: Optional[Callable[[str], bool]] = None,
    ensure: Optional[Callable[..., dict[str, Any]]] = None,
) -> dict[str, Any]:
    """One reap + probe + respawn pass over the brain-owned gateway.

    Serving is decided on HTTP, not on ``poll()`` — the same ruling the
    local-services watchdog reached: a pid that
    never answers is, to every caller that matters, a dead one.

    But the REMEDY is gated on ``poll()``, which is where the gateway parts
    company with the embed worker. Reviving means sweeping :11434 first, so a
    respawn aimed at a merely-busy gateway destroys it and its 262k KV pool.
    A reaped exit code is proof and revives immediately; silence from a live
    process must survive ``_WATCHDOG_UNREACHABLE_STRIKES`` ticks first.
    """
    if _watchdog_should_stop():
        return {"action": "skipped", "reason": "stopping"}

    exit_code = _reap_owned_process()
    if exit_code is not None:
        log.error(
            "gateway watchdog: owned gateway exited rc=%s — reaped; see %s for its output",
            exit_code,
            gateway_log_path(),
        )

    if client is None:
        from runtime.client import get_runtime_client

        client = get_runtime_client()
    base_url = str(client.base_url).rstrip("/")

    global _watchdog_strikes
    if (health_probe or _watchdog_probe)(base_url):
        _watchdog_strikes = 0
        return {"action": "healthy", "exit_code": exit_code}

    # Silence alone is not death. Respawning is DESTRUCTIVE — ensure_runtime_process
    # sweeps before it spawns, and that sweep shuts down whatever is on :11434 —
    # so the bar for acting scales with how certain the evidence is.
    if exit_code is None:
        _watchdog_strikes += 1
        if _watchdog_strikes < _WATCHDOG_UNREACHABLE_STRIKES:
            # A live process that missed a probe is most likely busy, not wedged.
            log.warning(
                "gateway watchdog: %s silent but its process is alive (strike %d/%d)",
                base_url,
                _watchdog_strikes,
                _WATCHDOG_UNREACHABLE_STRIKES,
            )
            return {"action": "unreachable", "strikes": _watchdog_strikes}
        log.error(
            "gateway watchdog: %s silent for %d consecutive probes — recycling a wedged gateway",
            base_url,
            _watchdog_strikes,
        )
    else:
        log.error(
            "gateway watchdog: %s is down and its process is a corpse (rc=%s) — respawning",
            base_url,
            exit_code,
        )

    result = (ensure or ensure_runtime_process)(client=client, abort_check=_watchdog_should_stop)
    _watchdog_strikes = 0

    # TWO checks against one race, because the respawn is long and the thread
    # running it is expendable. ensure_runtime_process blocks for up to
    # OMEGA_RUNTIME_BOOT_TIMEOUT_S (300s by default since the cold-boot
    # measurement) waiting on /health — ample time for the operator to have
    # asked the appliance to stop.
    #
    #   INSIDE the wait, via abort_check: teardown's stop_gateway_watchdog joins
    #   this thread for only 2s and then proceeds, and the thread is a daemon —
    #   so the interpreter can exit with the loop still running, and a gateway
    #   that binds after teardown's sweep has passed is an orphan holding :11434
    #   that nothing in the brain will ever reap. Measured: teardown returns in
    #   2.00s with the watchdog still mid-spawn.
    #
    #   AFTER it, here: covers the spawn that completed just before the flag was
    #   set. Reviving into a shutdown must not strand a gateway nobody owns.
    if _watchdog_should_stop():
        log.info("gateway watchdog: shutdown began mid-respawn — tearing the new gateway back down")
        shutdown_runtime_process()
        return {"action": "revived_then_stopped", "exit_code": exit_code, "spawn": result}

    # exit_code rides along on every post-reap verdict: it is the only
    # machine-readable evidence of WHY the gateway we just replaced died.
    if result.get("ok"):
        log.warning("gateway watchdog: gateway revived (pid=%s)", result.get("pid"))
        return {"action": "revived", "exit_code": exit_code, "spawn": result}
    log.error("gateway watchdog: respawn failed: %s", result.get("reason"))
    return {"action": "respawn_failed", "exit_code": exit_code, "spawn": result}


def _watchdog_loop(interval_s: float) -> None:
    # Event.wait doubles as the sleep AND the stop signal, so a teardown does
    # not have to outwait a full interval to shut the watchdog up.
    while not _watchdog_stop.wait(interval_s):
        try:
            gateway_watchdog_tick()
        except Exception:  # noqa: BLE001 — the watchdog outlives every tick it runs
            log.exception("gateway watchdog tick failed")


def start_gateway_watchdog(*, interval_s: Optional[float] = None) -> dict[str, Any]:
    """Start the gateway health watchdog. Idempotent; never raises.

    Deliberately NOT folded into any existing sweep — the same ruling as the
    gateway-side embed watchdog. A keeper that shares a cadence with other work
    inherits that work's stalls, and the brain's whole runtime surface is behind
    this one process.

    Kill switch: ``OMEGA_GATEWAY_WATCHDOG=0``.
    """
    global _watchdog_thread, _watchdog_strikes
    if (os.environ.get("OMEGA_GATEWAY_WATCHDOG") or "").strip() == "0":
        log.info("gateway watchdog disabled by OMEGA_GATEWAY_WATCHDOG=0")
        return {"ok": True, "started": False, "reason": "disabled"}

    interval = float(interval_s if interval_s is not None else _WATCHDOG_INTERVAL_S)
    with _lock:
        if _watchdog_thread is not None and _watchdog_thread.is_alive():
            return {"ok": True, "started": False, "reason": "already_running"}
        _watchdog_stop.clear()
        _watchdog_strikes = 0
        _watchdog_thread = threading.Thread(
            target=_watchdog_loop,
            args=(interval,),
            name="omega-gateway-watchdog",
            daemon=True,
        )
        _watchdog_thread.start()
    log.info("gateway watchdog started (probe every %.1fs)", interval)
    return {"ok": True, "started": True, "interval_s": interval}


def stop_gateway_watchdog(*, join_timeout_s: float = 2.0) -> dict[str, Any]:
    """Ask the watchdog to stop and wait briefly for the thread. Never raises."""
    global _watchdog_thread
    _watchdog_stop.set()
    thread = _watchdog_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=join_timeout_s)
    _watchdog_thread = None
    return {"ok": True, "stopped": True}


def watchdog_running() -> bool:
    thread = _watchdog_thread
    return bool(thread is not None and thread.is_alive())


def _reset_for_tests() -> None:
    global _watchdog_strikes
    with _lock:
        _owned.clear()
    stop_gateway_watchdog(join_timeout_s=0.5)
    _watchdog_stop.clear()
    _watchdog_strikes = 0

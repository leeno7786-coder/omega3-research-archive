"""
HUD spawner — start the FastAPI HUD inside the boot process.
============================================================

`.\\.venv-brain\\Scripts\\python.exe boot.py` spins up the cognitive substrate (Guardian, memory,
runtime readiness, and the pressure cascade). Until 2026-05-05 the HUD was a
SEPARATE process — operators were expected to run a second launcher in another
terminal AFTER boot. In
practice many operators opened `omega_interface.html` directly via
file:// because they didn't know about the second process.

Loading the HUD via `file://` is broken by browser CORS:
  - Origin is `null` for file:// pages
  - Every `/api/*` fetch is rejected before it even leaves the browser
  - Telemetry never updates, chat falls through to the demo synth path

The fix: boot.run_continuous now spawns the HUD inside the boot
process via this module. One portable venv command brings up
both the substrate AND the HUD on http://127.0.0.1:13310. The
operator browses to that URL and CORS works because origin is
http://127.0.0.1:13310 — same-origin as the API endpoints.

ARCHITECTURAL RULES:
    - The HUD runs in a DAEMON THREAD, not a subprocess. This shares
      the same Python interpreter so the FastAPI app talks to the
      live PressureRuntime + SharedState without IPC. boot.shutdown()
      tears the thread down by calling uvicorn server.should_exit.
    - A requested HUD is required operator substrate. Construction or
      thread-start failures raise an attributable error and abort readiness.
    - Port + host configurable via env vars (OMEGA_HUD_HOST,
      OMEGA_HUD_PORT) — defaults to 127.0.0.1:13310.
    - OMEGA_HUD_DISABLE=1 skips spawning entirely (for headless CI
      runs or when the operator wants the standalone hud/standalone.py
      flow instead — `.\\.venv-brain\\Scripts\\python.exe -m hud.standalone --no-boot`).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Optional

from core.required_substrate import RequiredSubstrateError

log = logging.getLogger("omega.hud.spawner")


def _emit_fail(
    operation: str, *, status: str = "fail", failure_class: str, summary: str, **fields: Any
) -> None:
    """Remote-diagnosable failure record for the in-boot HUD lifecycle
    (operator-surface availability). Lazy-import; its own failure
    handler is log.debug only (no recursion)."""
    try:
        from guardian.terminal_feedback import emit_phase

        emit_phase(
            "hud",
            operation,
            status=status,
            summary=summary,
            component="hud.spawner",
            failure_class=failure_class,
            **fields,
        )
    except Exception as _e:  # noqa: BLE001 - never recurse into emit
        log.debug("spawner emit_phase failed (%s): %s", operation, _e)


_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 13310

# Module-level so _shutdown() can reach across to ask uvicorn to stop.
# Typed as Any because uvicorn is imported lazily inside the spawn
# function (it isn't a hard dependency of the cognitive substrate).
_server: Optional[Any] = None
_thread: Optional[threading.Thread] = None
_disabled_reason: Optional[str] = None
# True only when shutdown_hud() is explicitly called — not when uvicorn
# returns spontaneously. Lets the watchdog distinguish "operator
# requested shutdown" (don't restart) from "uvicorn died on its own"
# (restart loudly with diagnostic). Operator reported the HUD closing
# silently mid-session; the previous design treated EVERY clean return
# as deliberate and gave up restarting, so the HUD stayed dead.
_explicit_shutdown_requested: bool = False


def hud_url() -> str:
    """Resolve the URL the operator should browse to. Same logic as
    spawn_hud_in_thread so banner labels stay in sync with the actual
    listener regardless of env override timing."""
    host = (os.environ.get("OMEGA_HUD_HOST") or _DEFAULT_HOST).strip()
    try:
        port = int(os.environ.get("OMEGA_HUD_PORT") or _DEFAULT_PORT)
    except (TypeError, ValueError):
        port = _DEFAULT_PORT
    # 0.0.0.0 binds all interfaces but isn't a browseable address —
    # show localhost instead so the banner is clickable.
    show_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    return f"http://{show_host}:{port}"


def is_disabled() -> bool:
    """Return True if HUD spawn is (or will be) skipped.

    Checks ``OMEGA_HUD_DISABLE`` env var directly so callers can query
    the gate BEFORE calling ``spawn_hud_in_thread`` (e.g. tests, boot
    preflight banners). Also returns True once ``spawn_hud_in_thread``
    has populated ``_disabled_reason`` after an explicit disable request.
    """
    if _disabled_reason is not None:
        return True
    disable_flag = (os.environ.get("OMEGA_HUD_DISABLE") or "").strip().lower()
    return disable_flag in ("1", "true", "yes", "on")


def disabled_reason() -> Optional[str]:
    """Human-readable reason for the disable, or None if spawn is permitted.

    Returns a non-empty string whenever ``is_disabled()`` is True so
    callers can surface the reason in boot banners or test assertions.
    """
    if _disabled_reason is not None:
        return _disabled_reason
    disable_flag = (os.environ.get("OMEGA_HUD_DISABLE") or "").strip().lower()
    if disable_flag in ("1", "true", "yes", "on"):
        return "OMEGA_HUD_DISABLE env set"
    return None


def spawn_hud_in_thread() -> Optional[str]:
    """Start the HUD FastAPI app in a daemon thread.

    Returns the URL the operator should browse to on success or ``None`` only
    for the explicit ``OMEGA_HUD_DISABLE`` policy. Requested startup failures
    raise ``RequiredSubstrateError``.

    Idempotent: a second call when the server is already running
    returns the existing URL without restarting.
    """
    global _server, _thread, _disabled_reason, _explicit_shutdown_requested

    # Operator-driven disable (CI, headless, or running standalone start_hud.py)
    disable_flag = (os.environ.get("OMEGA_HUD_DISABLE") or "").strip().lower()
    if disable_flag in ("1", "true", "yes", "on"):
        _disabled_reason = "OMEGA_HUD_DISABLE env set"
        log.info("hud spawner: %s — skipping HUD startup", _disabled_reason)
        return None

    if _thread is not None and _thread.is_alive():
        log.debug("hud spawner: thread already running on %s", hud_url())
        return hud_url()

    _explicit_shutdown_requested = False

    try:
        import uvicorn
    except ImportError as exc:
        reason = f"uvicorn not installed ({exc})"
        log.warning(
            "hud spawner: %s — install via `pip install uvicorn fastapi` to enable the in-boot HUD",
            reason,
        )
        _emit_fail(
            "hud_spawn",
            status="degraded",
            failure_class="uvicorn_not_installed",
            summary="in-boot HUD disabled — uvicorn not installed",
            error_type=type(exc).__name__,
            error=str(exc)[:120],
        )
        failure = RequiredSubstrateError(
            "hud",
            "dependency_import",
            reason="uvicorn_not_installed",
            detail=str(exc),
        )
        raise failure from exc

    try:
        # Importing hud.server constructs the FastAPI app at module
        # level (`app = create_app()`). Defer until we know uvicorn
        # is available so a missing fastapi install doesn't crash the
        # substrate on import.
        from hud.server import app  # noqa: F401 — used by uvicorn via string ref
    except Exception as exc:
        reason = f"hud.server import failed: {exc}"
        log.error(
            "hud spawner: %s — HUD will not start.", reason
        )
        _emit_fail(
            "hud_spawn",
            failure_class="hud_server_import_failed",
            summary="in-boot HUD will not start — hud.server import failed",
            error_type=type(exc).__name__,
            error=str(exc)[:120],
        )
        failure = RequiredSubstrateError.from_exception(
            "hud",
            "app_import",
            exc,
        )
        raise failure from exc

    host = (os.environ.get("OMEGA_HUD_HOST") or _DEFAULT_HOST).strip()
    try:
        port = int(os.environ.get("OMEGA_HUD_PORT") or _DEFAULT_PORT)
    except (TypeError, ValueError):
        port = _DEFAULT_PORT

    config = uvicorn.Config(
        app="hud.server:app",
        host=host,
        port=port,
        log_level=os.environ.get("OMEGA_HUD_LOG_LEVEL", "warning"),
        access_log=False,  # suppress per-request noise; HUD polls every 2s
        # No reload — we share the Orchestrator + SharedState with the
        # boot process; reloading would import a fresh boot tree and
        # double-register everything.
        reload=False,
        lifespan="on",
        # cognition_lock() is process-local; multi-worker spawn would break
        # the pressure/HUD chat serialization contract.
        workers=1,
    )
    _server = uvicorn.Server(config)

    # Restart-on-crash supervisor. The HUD has been observed to crash
    # mid-session — without a watchdog, the daemon thread dies silently
    # and the operator sees connection-refused with no indication that
    # uvicorn died (vs. simply being slow). Restart up to 5 times with
    # exponential backoff (2s, 4s, 8s, 16s, 32s) so a transient failure
    # heals on its own; surface the crash via log AND boot feedback so
    # the operator sees what broke.
    _max_restarts = 5

    def _run():
        global _server
        import sys as _sys
        import time as _time
        import traceback as _tb

        attempt = 0
        while attempt <= _max_restarts:
            try:
                if attempt > 0:
                    # Rebuild the Server — uvicorn's Server is single-shot.
                    backoff = 2**attempt
                    log.warning(
                        "hud spawner: restart attempt %d/%d in %ds",
                        attempt,
                        _max_restarts,
                        backoff,
                    )
                    _time.sleep(backoff)
                    _server = uvicorn.Server(config)
                _server.run()
                # Clean exit. Was it operator-initiated (shutdown_hud
                # called, _explicit_shutdown_requested=True) or did
                # uvicorn return on its own?
                if _explicit_shutdown_requested:
                    log.info("hud spawner: uvicorn returned cleanly (operator-requested shutdown)")
                    return
                # Anomaly. Uvicorn returned without us asking — the
                # asyncio loop was cancelled, lifespan exited, or some
                # internal path set should_exit. Loud banner so operator
                # SEES it, then restart with backoff so the HUD comes
                # back without manual intervention.
                msg = (
                    f"\n{'=' * 64}\n"
                    f"!! HUD: uvicorn returned WITHOUT explicit shutdown request\n"
                    f"!!      attempt {attempt + 1}/{_max_restarts + 1} — restarting with backoff\n"
                    f"!!      should_exit={getattr(_server, 'should_exit', '?')} "
                    f"force_exit={getattr(_server, 'force_exit', '?')}\n"
                    f"{'=' * 64}"
                )
                print(msg, file=_sys.stderr, flush=True)
                log.error(
                    "hud spawner: uvicorn returned WITHOUT explicit shutdown "
                    "(should_exit=%s force_exit=%s) — restarting (attempt %d/%d)",
                    getattr(_server, "should_exit", "?"),
                    getattr(_server, "force_exit", "?"),
                    attempt + 1,
                    _max_restarts + 1,
                )
                _emit_fail(
                    "hud_watchdog",
                    status="degraded",
                    failure_class="uvicorn_returned_unexpectedly",
                    summary="HUD uvicorn returned without shutdown request — watchdog restarting",
                    attempt=attempt + 1,
                    max_attempts=_max_restarts + 1,
                )
                attempt += 1
                continue
            except SystemExit as exc:
                if _explicit_shutdown_requested:
                    log.info("hud spawner: uvicorn SystemExit (requested shutdown)")
                    return
                log.error(
                    "hud spawner: uvicorn SystemExit without shutdown request "
                    "(attempt %d/%d): %s",
                    attempt + 1,
                    _max_restarts + 1,
                    exc,
                )
                _emit_fail(
                    "hud_watchdog",
                    failure_class="uvicorn_system_exit",
                    summary="HUD uvicorn exited without a shutdown request",
                    attempt=attempt + 1,
                    max_attempts=_max_restarts + 1,
                )
                attempt += 1
            except Exception as exc:
                log.error(
                    "hud spawner: uvicorn raised (attempt %d/%d): %s\n%s",
                    attempt + 1,
                    _max_restarts + 1,
                    exc,
                    _tb.format_exc(),
                )
                _emit_fail(
                    "hud_watchdog",
                    failure_class="uvicorn_raised",
                    summary="HUD uvicorn raised — watchdog restarting",
                    attempt=attempt + 1,
                    max_attempts=_max_restarts + 1,
                    error_type=type(exc).__name__,
                    error=str(exc)[:120],
                )
                attempt += 1
        log.error(
            "hud spawner: HUD crashed %d times — giving up. The cognitive "
            "substrate is still running; restart boot to recover the HUD.",
            _max_restarts + 1,
        )
        _emit_fail(
            "hud_watchdog",
            failure_class="hud_restart_exhausted",
            summary="HUD crashed past max restarts — watchdog gave up, operator surface down",
            max_attempts=_max_restarts + 1,
        )

    _thread = threading.Thread(
        target=_run,
        name="omega-hud-uvicorn",
        daemon=True,
    )
    try:
        _thread.start()
    except Exception as exc:
        _server = None
        _thread = None
        _emit_fail(
            "hud_spawn",
            failure_class="hud_thread_start_failed",
            summary="HUD daemon thread could not start",
            error_type=type(exc).__name__,
            error=str(exc)[:120],
        )
        failure = RequiredSubstrateError.from_exception(
            "hud",
            "thread_start",
            exc,
        )
        raise failure from exc
    raw_timeout = (os.environ.get("OMEGA_HUD_START_TIMEOUT_S") or "").strip()
    try:
        startup_timeout = max(0.1, float(raw_timeout)) if raw_timeout else 10.0
    except ValueError:
        startup_timeout = 10.0
    deadline = time.monotonic() + startup_timeout
    while not bool(getattr(_server, "started", False)):
        if _thread is None or not _thread.is_alive():
            _server = None
            _thread = None
            raise RequiredSubstrateError(
                "hud",
                "listener_start",
                reason="thread_exited_before_ready",
            )
        if time.monotonic() >= deadline:
            try:
                _server.should_exit = True
            except Exception:
                pass
            _thread.join(timeout=1.0)
            _server = None
            _thread = None
            raise RequiredSubstrateError(
                "hud",
                "listener_start",
                reason="readiness_timeout",
                detail=f"waited {startup_timeout:.1f}s",
            )
        time.sleep(0.05)
    url = hud_url()
    log.info("hud spawner: HUD started on %s (daemon thread, watchdog enabled)", url)
    return url


def shutdown_hud() -> None:
    """Ask uvicorn to stop. Best-effort; never raises.

    Called from boot._shutdown so closing the substrate also closes
    the HUD listener. Daemon thread would die with the process anyway,
    but calling should_exit gives uvicorn a chance to drain in-flight
    requests cleanly.

    Sets `_explicit_shutdown_requested=True` so the watchdog in `_run`
    treats uvicorn's subsequent clean return as deliberate (don't
    restart) instead of as an anomaly (restart with diagnostic).
    """
    global _server, _thread, _explicit_shutdown_requested
    if _server is None:
        return
    _explicit_shutdown_requested = True
    try:
        _server.should_exit = True
    except Exception as exc:
        log.debug("hud spawner: should_exit set raised: %s", exc)
    if _thread is not None and _thread.is_alive():
        _thread.join(timeout=3.0)
    log.info("hud spawner: HUD listener stopped")

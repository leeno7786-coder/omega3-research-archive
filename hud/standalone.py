"""
hud/standalone.py — STANDALONE HUD launcher.
============================================

The CANONICAL launch path is `.\\.venv-brain\\Scripts\\python.exe boot.py` — boot owns the HUD via
`hud.spawner.spawn_hud_in_thread()`. This module is the SECONDARY path
for HUD-first visual inspection: substrate boots paused (real memory,
guardian, panels) without the pressure tick loop.

Usage:
    .\\.venv-brain\\Scripts\\python.exe -m hud.standalone               # boot paused + HUD (default)
    .\\.venv-brain\\Scripts\\python.exe -m hud.standalone --no-boot     # HUD only — empty placeholders
    .\\.venv-brain\\Scripts\\python.exe -m hud.standalone --port 13311  # alternate port
    .\\.venv-brain\\Scripts\\python.exe -m hud.standalone --open-browser  # portable Edge debugger + HUD

If port 13310 is busy (WinError 10048), stop the stale listener or pick
``--port 13311``. Find the PID: ``netstat -ano | findstr :13310``.

Port 13310 is the canonical Omega HUD port (same-origin with the FastAPI
endpoints served by hud.server).

For live cognition (pressure ticks + cascade): `.\\.venv-brain\\Scripts\\python.exe boot.py`.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
# Standalone HUD runs without boot.py — install the per-component log router
# here too so every part still lands in guardian/logs (idempotent, failure-soft).
try:
    from guardian.log_router import install_component_logging

    install_component_logging()
except Exception:
    pass
log = logging.getLogger("omega.hud.standalone")


def _bind_probe_host(host: str) -> str:
    """Host address for a pre-flight bind probe."""
    h = (host or "127.0.0.1").strip()
    if h in ("0.0.0.0", "::", ""):
        return "127.0.0.1"
    if h.startswith("::"):
        return "::1"
    return h


def _port_in_use(host: str, port: int) -> bool:
    """True when another process already owns host:port."""
    import socket

    probe_host = _bind_probe_host(host)
    family = socket.AF_INET6 if ":" in probe_host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.bind((probe_host, port))
        return False
    except OSError:
        return True


def _log_port_in_use(host: str, port: int) -> None:
    log.error(
        "Port %d is already in use on %s — another HUD instance may still be running.",
        port,
        host,
    )
    if sys.platform == "win32":
        log.error("Find the listener:  netstat -ano | findstr :%d", port)
        log.error("Stop it:  taskkill /PID <pid> /F")
    else:
        log.error("Find the listener:  ss -tlnp | grep :%d", port)
        log.error("Stop it:  kill <pid>")
    log.error(
        "Or use another port:  .\\.venv-brain\\Scripts\\python.exe -m hud.standalone --port %d",
        port + 1,
    )


def _browser_open_requested(args: argparse.Namespace) -> bool:
    """Return whether this launcher owns a browser window for the HUD.

    The public ``Omega.exe --launch-hud`` path opts in through the environment
    marker. Direct ``hud.standalone`` use stays a server-only diagnostic unless
    ``--open-browser`` is supplied, so visual tests do not require Edge.
    """
    value = os.environ.get("OMEGA_HUD_OPEN_BROWSER", "0").strip().lower()
    requested = value not in {"", "0", "false", "off", "no"}
    if args.open_browser:
        requested = True
    if args.no_browser:
        requested = False
    return requested


def _open_browser_when_ready(
    url: str,
    *,
    host: str,
    port: int,
    omega_root: Path | None = None,
) -> None:
    """Prepare one debugger Edge and open the HUD after uvicorn binds."""
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if _port_in_use(host, port):
            try:
                from browser_agent.lifecycle import open_hud_window, prepare_hud_browser

                root = (omega_root or Path(os.environ.get("OMEGA_ROOT") or ROOT)).resolve()
                receipt = prepare_hud_browser(omega_root=root, initial_url=url)
                # Existing debugger sessions cannot be retargeted by the
                # initial launch command, so use the profile singleton only in
                # that case.  A fresh process already opened the HUD URL.
                if not receipt.get("initial_url_opened"):
                    open_hud_window(url)
            except Exception as exc:  # noqa: BLE001 - preserve server availability
                log.error("Failed to open the HUD in the debugger-enabled Edge session: %s", exc)
            return
        time.sleep(0.1)
    log.error("HUD server did not bind before the Edge window timeout: %s", url)


def _emit_fail(
    operation: str, *, status: str = "fail", failure_class: str, summary: str, **fields
) -> None:
    """Remote-diagnosable failure record for the standalone launcher.
    Lazy-import (terminal_feedback may not be initialized pre-boot);
    its own failure handler is log.debug only (no recursion)."""
    try:
        from guardian.terminal_feedback import emit_phase

        emit_phase(
            "hud",
            operation,
            status=status,
            summary=summary,
            component="hud.standalone",
            failure_class=failure_class,
            **fields,
        )
    except Exception as _e:  # noqa: BLE001 - never recurse into emit
        log.debug("standalone emit_phase failed (%s): %s", operation, _e)


def boot_substrate_paused() -> dict:
    """Boot Omega substrate paused for HUD visual inspection."""
    from boot import boot_paused_for_hud

    return boot_paused_for_hud()


def main(argv: list[str] | None = None) -> int:
    _STANDALONE_EPILOG = """
Examples:
  .\\.venv-brain\\Scripts\\python.exe -m hud.standalone
  .\\.venv-brain\\Scripts\\python.exe -m hud.standalone --no-boot
  .\\.venv-brain\\Scripts\\python.exe -m hud.standalone --port 13311
  .\\.venv-brain\\Scripts\\python.exe -m hud.standalone --presence-test
  .\\.venv-brain\\Scripts\\python.exe boot.py

Port busy (WinError 10048 / EADDRINUSE):
  Windows:  netstat -ano | findstr :13310   then  taskkill /PID <pid> /F
  POSIX:    ss -tlnp | grep :13310          then  kill <pid>
  or:       .\\.venv-brain\\Scripts\\python.exe -m hud.standalone --port 13311
"""
    parser = argparse.ArgumentParser(
        prog="hud.standalone",
        description="Launch the Omega 3.0 HUD server (standalone path; "
        "canonical live path is `.\\.venv-brain\\Scripts\\python.exe boot.py`).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_STANDALONE_EPILOG,
    )
    parser.add_argument("--port", type=int, default=13310, help="HUD port (default 13310)")
    parser.add_argument(
        "--host", type=str, default="127.0.0.1", help="HUD host (default 127.0.0.1)"
    )
    parser.add_argument(
        "--no-boot",
        action="store_true",
        help="Skip Omega boot (empty telemetry placeholders only).",
    )
    parser.add_argument(
        "--presence-test",
        action="store_true",
        help="Serve the S2 operator-presence LIVE TEST "
        "surface (hud.presence_test:app) instead of "
        "the full HUD. Implies --no-boot. Real face "
        "models + the real PresenceMachine; browser "
        "webcam. Pure diagnostic — no cognitive loop.",
    )
    parser.add_argument(
        "--https",
        action="store_true",
        help="Serve over HTTPS using certs at "
        "guardian/state/tls/server.{crt,key}. Generate "
        "first via: scripts/setup_local_https.py",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Prepare Omega's portable debugger-enabled Edge session and open the HUD.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not prepare or open Edge (overrides --open-browser and launcher defaults).",
    )
    args = parser.parse_args(argv)
    os.environ["OMEGA_HUD_HOST"] = args.host
    os.environ["OMEGA_HUD_PORT"] = str(args.port)

    if args.presence_test:
        args.no_boot = True

    if args.no_boot:
        log.warning(
            "--no-boot set; HUD telemetry will be empty/placeholder. "
            "Omit --no-boot for paused substrate boot (panels + memory graph).",
        )
    else:
        log.info("Booting Omega substrate (paused) before HUD server...")
        try:
            report = boot_substrate_paused()
            log.info(
                "Substrate ready: %s | pressure_loop=%s",
                report.get("summary", "ok"),
                report.get("pressure_loop", "paused"),
            )
        except Exception as exc:
            log.error("Omega boot failed: %s", exc)
            log.error(
                "HUD will start anyway in degraded mode; telemetry endpoints will return errors."
            )
            _emit_fail(
                "standalone_boot",
                status="degraded",
                failure_class="omega_boot_failed",
                summary="Omega boot failed — HUD starting in degraded mode",
                error_type=type(exc).__name__,
                error=str(exc)[:120],
            )

    ssl_kwargs: dict = {}
    if args.https:
        from core.paths import OMEGA_ROOT

        tls_dir = OMEGA_ROOT / "guardian" / "state" / "tls"
        srv_crt = tls_dir / "server.crt"
        srv_key = tls_dir / "server.key"
        if not (srv_crt.exists() and srv_key.exists()):
            log.error("--https set but certs missing at %s", tls_dir)
            log.error(
                "Generate first: .\\.venv-brain\\Scripts\\python.exe scripts\\setup_local_https.py"
            )
            _emit_fail(
                "standalone_launch",
                failure_class="tls_certs_missing",
                summary="HUD --https requested but TLS certs missing — launch aborted",
            )
            return 3
        ssl_kwargs = {
            "ssl_keyfile": str(srv_key),
            "ssl_certfile": str(srv_crt),
        }

    app_target = "hud.presence_test:app" if args.presence_test else "hud.server:app"
    scheme = "https" if args.https else "http"
    log.info("Starting uvicorn (%s) on %s://%s:%d", app_target, scheme, args.host, args.port)
    if args.presence_test:
        log.info(
            "PRESENCE TEST surface — open %s://%s:%d and enroll, then verify the live latch.",
            scheme,
            args.host,
            args.port,
        )
    else:
        log.info(
            "Browse to %s://%s:%d — pressure loop paused; panels use live memory",
            scheme,
            args.host,
            args.port,
        )

    if _port_in_use(args.host, args.port):
        _log_port_in_use(args.host, args.port)
        _emit_fail(
            "standalone_launch",
            failure_class="hud_port_in_use",
            summary=f"HUD port {args.port} already in use — launch aborted",
            port=args.port,
            host=args.host,
        )
        return 2

    if _browser_open_requested(args):
        try:
            omega_root = Path(os.environ.get("OMEGA_ROOT") or ROOT).resolve()
            browser_url = f"{scheme}://{_bind_probe_host(args.host)}:{args.port}"
            threading.Thread(
                target=_open_browser_when_ready,
                kwargs={
                    "url": browser_url,
                    "host": args.host,
                    "port": args.port,
                    "omega_root": omega_root,
                },
                name="omega-hud-browser-open",
                daemon=True,
            ).start()
            log.info("HUD browser handoff armed; debugger Edge will open at %s", browser_url)
        except Exception as exc:  # noqa: BLE001 - requested browser setup is required
            log.error("Portable Edge debugger setup failed: %s", exc)
            _emit_fail(
                "standalone_browser_setup",
                failure_class="edge_debugger_setup_failed",
                summary="HUD browser setup failed — debugger-enabled Edge was not started",
                error_type=type(exc).__name__,
                error=str(exc)[:120],
            )
            return 3

    mcp_host_started = False
    if not args.no_boot:
        try:
            from mcp_platform.service import start_default_mcp_host

            start_default_mcp_host()
            mcp_host_started = True
            log.info("MCP plugin host ready")
        except Exception as exc:
            log.error("MCP plugin host failed to start: %s", exc)
            _emit_fail(
                "standalone_mcp_host",
                status="degraded",
                failure_class="mcp_host_start_failed",
                summary="MCP plugin host failed to start — plugin access unavailable",
                error_type=type(exc).__name__,
                error=str(exc)[:120],
            )

    try:
        import errno

        import uvicorn

        uvicorn.run(
            app_target,
            host=args.host,
            port=args.port,
            log_level="info",
            access_log=True,
            reload=False,
            **ssl_kwargs,
        )
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — shutting down.")
    except OSError as exc:
        winerr = getattr(exc, "winerror", None)
        addr_in_use = exc.errno in {errno.EADDRINUSE, 10048} or winerr == 10048
        if addr_in_use:
            _log_port_in_use(args.host, args.port)
            _emit_fail(
                "standalone_launch",
                failure_class="hud_port_in_use",
                summary=f"HUD port {args.port} already in use — launch aborted",
                error_type=type(exc).__name__,
                error=str(exc)[:120],
                port=args.port,
                host=args.host,
            )
            return 2
        log.error("uvicorn raised: %s", exc)
        _emit_fail(
            "standalone_launch",
            failure_class="uvicorn_raised",
            summary="HUD uvicorn server raised — operator web surface down",
            error_type=type(exc).__name__,
            error=str(exc)[:120],
        )
        return 2
    except Exception as exc:
        log.error("uvicorn raised: %s", exc)
        _emit_fail(
            "standalone_launch",
            failure_class="uvicorn_raised",
            summary="HUD uvicorn server raised — operator web surface down",
            error_type=type(exc).__name__,
            error=str(exc)[:120],
        )
        return 2
    finally:
        if mcp_host_started:
            try:
                from mcp_platform.service import shutdown_default_mcp_host

                shutdown_default_mcp_host()
            except Exception:
                log.exception("MCP plugin host shutdown failed")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

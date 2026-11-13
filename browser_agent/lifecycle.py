"""Omega-owned Edge startup for the HUD and same-session browser windows."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from core.paths import OMEGA_ROOT

from .bootstrap import BrowserBootstrap, BrowserBootstrapError

HUD_DEBUGGER_ADDRESS = "127.0.0.1:9222"

_HUD_BOOTSTRAP: BrowserBootstrap | Any | None = None


class BrowserLifecycleError(RuntimeError):
    """Stable startup/window failure for the HUD browser session."""


def prepare_hud_browser(
    *,
    omega_root: Path = OMEGA_ROOT,
    debugger_address: str = HUD_DEBUGGER_ADDRESS,
    restart_existing_edge: bool = False,
    initial_url: str | None = None,
    bootstrap_factory: Callable[..., BrowserBootstrap] = BrowserBootstrap,
) -> dict[str, object]:
    """Ensure Edge is debugger-enabled before the HUD opens.

    ``initial_url`` is supplied by the in-process HUD boot path.  Passing it
    to the first Edge launch makes the HUD the first ordinary page and avoids
    the historical blank-debug-window-then-second-launch sequence.
    """
    global _HUD_BOOTSTRAP
    bootstrap = bootstrap_factory(omega_root=Path(omega_root))
    try:
        ensure_kwargs: dict[str, object] = {
            "debugger_address": debugger_address,
            "restart_existing_edge": restart_existing_edge,
        }
        if initial_url is not None:
            ensure_kwargs["initial_url"] = initial_url
        receipt = bootstrap.ensure_debugger(**ensure_kwargs)
    except BrowserBootstrapError:
        raise
    except Exception as exc:
        raise BrowserLifecycleError(f"hud Edge startup failed: {type(exc).__name__}: {exc}") from exc
    _HUD_BOOTSTRAP = bootstrap
    return receipt


def open_hud_window(url: str) -> Any:
    """Open the HUD in a new window of the already debugger-enabled Edge process."""
    if _HUD_BOOTSTRAP is None:
        raise BrowserLifecycleError("hud_edge_not_prepared")
    try:
        return _HUD_BOOTSTRAP.open_new_window(url)
    except Exception as exc:
        raise BrowserLifecycleError(f"hud Edge window failed: {type(exc).__name__}: {exc}") from exc


__all__ = [
    "BrowserLifecycleError",
    "HUD_DEBUGGER_ADDRESS",
    "open_hud_window",
    "prepare_hud_browser",
]

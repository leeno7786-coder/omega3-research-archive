"""Per-component log router — every logger gets its own file in guardian/logs.

One handler on the root logger demultiplexes every record by logger name into
``guardian/logs/<logger.name>.log`` (rotating, failure-soft). 380+ existing
``getLogger(...)`` call sites need zero changes: the dotted names they already
use become the filenames, so pinpointing a subsystem is "open its file".

guardian/logs is the SINGLE log directory (operator directive 2026-07-13):
structured jsonl domains (data_logger + telemetry writer), terminal/infra
streams, and these per-component files all live flat in one place.

Also captured here:
  - ``warnings`` module output (logging.captureWarnings) -> py.warnings.log
  - unhandled exceptions (sys.excepthook chain)          -> omega.crash.log

Failure-soft everywhere: a logging problem must never take down cognition.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Optional

from core.paths import guardian_logs_root

#: Kept for callers that import the name; the router itself resolves the
#: directory when it is constructed, so a redirected root is honoured.
LOGS_DIR = guardian_logs_root()

_MAX_BYTES_ENV = "OMEGA_COMPONENT_LOG_MAX_MB"
_LEVEL_ENV = "OMEGA_LOG_LEVEL"
_DISABLE_ENV = "OMEGA_COMPONENT_LOGS"  # =0 to disable the router entirely

_DEFAULT_MAX_MB = 4
_BACKUP_COUNT = 1

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _max_bytes() -> int:
    raw = (os.environ.get(_MAX_BYTES_ENV) or "").strip()
    try:
        mb = float(raw) if raw else _DEFAULT_MAX_MB
    except ValueError:
        mb = _DEFAULT_MAX_MB
    return max(int(mb * 1024 * 1024), 64 * 1024)


def _sanitize_component(name: str) -> str:
    """Logger name -> safe flat filename stem (dots kept — they group by prefix)."""
    cleaned = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in (name or "root"))
    cleaned = cleaned.strip("._") or "root"
    # Windows reserved device names (CON, NUL, ...) can't be file stems.
    if cleaned.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL"}:
        cleaned = f"_{cleaned}"
    return cleaned[:120]


class ComponentLogRouter(logging.Handler):
    """Route each record to a per-logger rotating file under guardian/logs."""

    def __init__(self, logs_dir: Optional[Path] = None) -> None:
        super().__init__(level=logging.DEBUG)
        self._dir = Path(logs_dir) if logs_dir else guardian_logs_root()
        self._files: dict[str, logging.Handler] = {}
        self._cache_lock = threading.Lock()
        self._formatter = logging.Formatter(_FORMAT)
        self._broken = False

    def _file_handler(self, component: str) -> Optional[logging.Handler]:
        handler = self._files.get(component)
        if handler is not None:
            return handler
        with self._cache_lock:
            handler = self._files.get(component)
            if handler is not None:
                return handler
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                handler = logging.handlers.RotatingFileHandler(
                    self._dir / f"{component}.log",
                    maxBytes=_max_bytes(),
                    backupCount=_BACKUP_COUNT,
                    encoding="utf-8",
                    delay=True,
                )
                handler.setFormatter(self._formatter)
                self._files[component] = handler
                return handler
            except Exception:
                return None

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102 — Handler contract
        if self._broken:
            return
        try:
            handler = self._file_handler(_sanitize_component(record.name))
            if handler is not None:
                handler.emit(record)
        except Exception:
            # One bad record must not spam; a broken filesystem must not loop.
            try:
                self._broken = self._dir.exists() is False
            except Exception:
                self._broken = True

    def close(self) -> None:
        with self._cache_lock:
            for handler in self._files.values():
                try:
                    handler.close()
                except Exception:
                    pass
            self._files.clear()
        super().close()


_ROUTER: Optional[ComponentLogRouter] = None
_PREV_EXCEPTHOOK = None
_install_lock = threading.Lock()


def _crash_hook(exc_type, exc, tb) -> None:
    try:
        logging.getLogger("omega.crash").critical(
            "UNHANDLED EXCEPTION: %s", "".join(traceback.format_exception(exc_type, exc, tb))
        )
    except Exception:
        pass
    if _PREV_EXCEPTHOOK is not None:
        _PREV_EXCEPTHOOK(exc_type, exc, tb)


def install_component_logging(logs_dir: Optional[Path] = None) -> Optional[ComponentLogRouter]:
    """Attach the router to the root logger (idempotent). Call once at boot."""
    global _ROUTER, _PREV_EXCEPTHOOK
    if (os.environ.get(_DISABLE_ENV) or "").strip().lower() in {"0", "false", "off"}:
        return None
    with _install_lock:
        if _ROUTER is not None:
            return _ROUTER
        router = ComponentLogRouter(logs_dir)
        root = logging.getLogger()
        root.addHandler(router)
        # Per-component DEBUG must be reachable: the router filters nothing, so
        # the effective floor is the root level. Honor an explicit env override;
        # otherwise leave whatever boot configured (INFO by default).
        raw_level = (os.environ.get(_LEVEL_ENV) or "").strip().upper()
        if raw_level and hasattr(logging, raw_level):
            root.setLevel(getattr(logging, raw_level))
        logging.captureWarnings(True)
        _PREV_EXCEPTHOOK = sys.excepthook
        sys.excepthook = _crash_hook
        _ROUTER = router
        return router


def uninstall_for_tests() -> None:
    """Detach and close the router (test isolation)."""
    global _ROUTER, _PREV_EXCEPTHOOK
    with _install_lock:
        if _ROUTER is None:
            return
        logging.getLogger().removeHandler(_ROUTER)
        _ROUTER.close()
        _ROUTER = None
        if _PREV_EXCEPTHOOK is not None:
            sys.excepthook = _PREV_EXCEPTHOOK
            _PREV_EXCEPTHOOK = None

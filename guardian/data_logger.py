"""
Data Logger
===========

Structured event logging for all Omega 3.0 subsystems.
Every system emits events here — memory, gates, autonomic, loops, orchestrator.

Events are written to JSONL log files, one per subsystem.
All events are timestamped, typed, and carry arbitrary data payloads.

This is the module that memory/gates/base.py and memory/engine/validate_write.py
have been trying to import. Now it exists.
"""

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.paths import guardian_logs_root

# guardian/logs in a live boot; OMEGA_GUARDIAN_LOGS_DIR redirects it (tests, the plain-script matrix).
_LOG_DIR = guardian_logs_root()
_LOG_DIR.mkdir(parents=True, exist_ok=True)

# Thread-safe file handles
_file_locks: Dict[str, threading.Lock] = {}
_global_lock = threading.Lock()

log = logging.getLogger("omega.guardian.data_logger")


def _get_lock(subsystem: str) -> threading.Lock:
    """Get or create a per-subsystem file lock."""
    if subsystem not in _file_locks:
        with _global_lock:
            if subsystem not in _file_locks:
                _file_locks[subsystem] = threading.Lock()
    return _file_locks[subsystem]


def _subsystem_to_filename(subsystem: str) -> str:
    """Convert subsystem name to log filename."""
    # omega.memory.gate -> memory.jsonl
    # omega.autonomous.phase3 -> autonomous.jsonl
    # omega.meta.phase2 -> meta.jsonl
    parts = subsystem.replace("omega.", "").split(".")
    return f"{parts[0]}.jsonl"


def emit_event(
    subsystem: str,
    event_name: str,
    **data: Any,
) -> None:
    """
    Emit a structured event to the subsystem's JSONL log file.

    Args:
        subsystem: Dotted subsystem name (e.g. "omega.memory.gate")
        event_name: Event type (e.g. "ingest_success", "classify", "write_complete")
        **data: Arbitrary key-value payload
    """
    filename = _subsystem_to_filename(subsystem)
    log_path = _LOG_DIR / filename

    skip_terminal = bool(data.pop("_skip_terminal_mirror", False))
    skip_telemetry = bool(data.pop("_skip_telemetry_mirror", False))

    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "subsystem": subsystem,
        "event": event_name,
        "data": data,
    }

    if not skip_terminal:
        try:
            from guardian.terminal_feedback import _loop_from_subsystem, _publish_event

            _publish_event(
                {
                    "kind": "data",
                    "loop": _loop_from_subsystem(subsystem),
                    "system": _loop_from_subsystem(subsystem),
                    "phase": event_name,
                    "status": str(data.get("status") or "ok"),
                    "summary": str(data.get("summary") or ""),
                    **{k: v for k, v in data.items() if k not in {"status", "summary"}},
                }
            )
        except Exception:
            log.debug("terminal mirror failed for %s", subsystem)

    # Guardian logging channel mirror (project-wide observability hook):
    # every structured event also flows through omega.* logging so it
    # lands in guardian/logs/*.log + guardian/logs/omega.jsonl.
    try:
        log.info(
            "%s :: %s",
            subsystem,
            event_name,
            extra={
                "event_subsystem": subsystem,
                "event_name": event_name,
                "event_payload": data,
            },
        )
    except Exception:
        pass

    lock = _get_lock(filename)
    try:
        with lock:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        log.debug("Event write failed for %s: %s", subsystem, exc)

    if not skip_telemetry:
        try:
            from guardian.telemetry import write_telemetry

            parts = subsystem.replace("omega.", "").split(".")
            system = parts[0] if parts else "guardian"
            write_telemetry(
                system=system,
                phase=event_name,
                event="data",
                status=("fail" if data.get("ok") is False else str(data.get("status") or "ok")),
                summary=str(data.get("summary") or ""),
                ok=data.get("ok"),
                payload=data,
            )
        except Exception as exc:
            log.debug("telemetry mirror failed for %s: %s", subsystem, exc)


def read_events(
    subsystem: str,
    limit: int = 100,
    event_filter: Optional[str] = None,
) -> list:
    """
    Read recent events from a subsystem log.

    Args:
        subsystem: Dotted subsystem name
        limit: Max events to return (most recent)
        event_filter: Optional event name filter
    """
    filename = _subsystem_to_filename(subsystem)
    log_path = _LOG_DIR / filename

    if not log_path.exists():
        return []

    events = []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    event = json.loads(line)
                    if event_filter and event.get("event") != event_filter:
                        continue
                    events.append(event)
    except Exception:
        return []

    return events[-limit:]


def clear_log(subsystem: str) -> None:
    """Clear a subsystem's log file."""
    filename = _subsystem_to_filename(subsystem)
    log_path = _LOG_DIR / filename
    lock = _get_lock(filename)
    with lock:
        if log_path.exists():
            log_path.write_text("", encoding="utf-8")

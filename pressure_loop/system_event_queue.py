"""Session system-event queue for cognition-visible dev facts.

This box is for facts produced by the integrated system itself, not user chat.
It currently carries rollback events from the HUD dev sandbox so the next
episode, and therefore Omega, can reason from the current workspace state.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Tuple

log = logging.getLogger("omega.pressure_loop.system_event_queue")

_QUEUE: list[dict[str, Any]] = []
_LOADED = False
_LOCK = threading.RLock()


def _store_path() -> Path:
    from core.paths import OMEGA_ROOT, dev_artifacts_root

    return dev_artifacts_root(OMEGA_ROOT) / "_session" / "rollback_events.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit_degraded(operation: str, exc: Exception) -> None:
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "pressure",
            "system_event_queue",
            status="degraded",
            summary=f"{operation} persistence failed; in-memory events serving",
            ok=False,
            error=str(exc),
        )
    except Exception:
        pass


def _persist() -> None:
    try:
        path = _store_path()
        if not _QUEUE:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(_QUEUE, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        _emit_degraded("persist", exc)


def _ensure_loaded() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        path = _store_path()
        if not path.is_file():
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            _QUEUE.extend(dict(item) for item in raw if isinstance(item, dict))
    except Exception as exc:
        _emit_degraded("load", exc)


def _event_key(event: Mapping[str, Any]) -> str:
    return str(event.get("event_id") or "")


def _normal_rollback_event(event: Mapping[str, Any]) -> dict[str, Any]:
    action = str(event.get("action") or "restore").strip() or "restore"
    snapshot_id = str(event.get("snapshot_id") or "").strip()
    file_path = str(event.get("file_path") or "").strip()
    scope = str(event.get("scope") or "repo").strip() or "repo"
    summary = str(event.get("summary") or "").strip()
    message = str(event.get("message") or "").strip()
    if not message:
        verb = "deleted created file" if action == "delete_created_file" else "rolled back"
        message = f"{verb} {file_path}" if file_path else verb
    return {
        "event_id": str(event.get("event_id") or f"rollback-{uuid.uuid4().hex[:12]}"),
        "type": "dev_rollback",
        "timestamp": str(event.get("timestamp") or _now_iso()),
        "snapshot_id": snapshot_id,
        "file_path": file_path,
        "scope": scope,
        "lifecycle": "session",
        "action": action,
        "deleted": bool(event.get("deleted", False)),
        "bytes_written": int(event.get("bytes_written") or 0),
        "summary": summary,
        "message": message,
    }


def enqueue_rollback_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Append a rollback event for the next episode; return the stored event."""
    normalized = _normal_rollback_event(event)
    with _LOCK:
        _ensure_loaded()
        key = _event_key(normalized)
        if key and any(_event_key(existing) == key for existing in _QUEUE):
            return normalized
        _QUEUE.append(normalized)
        _persist()
        return normalized


def peek_rollback_events() -> Tuple[dict[str, Any], ...]:
    """Return pending rollback events without consuming them."""
    with _LOCK:
        _ensure_loaded()
        return tuple(dict(event) for event in _QUEUE)


def drain_rollback_events() -> Tuple[dict[str, Any], ...]:
    """Consume all rollback events for attachment to the next episode."""
    with _LOCK:
        _ensure_loaded()
        events = tuple(dict(event) for event in _QUEUE)
        _QUEUE.clear()
        _persist()
        return events


def clear_rollback_events() -> int:
    """Reset session rollback events. Returns the number cleared."""
    with _LOCK:
        _ensure_loaded()
        count = len(_QUEUE)
        _QUEUE.clear()
        _persist()
        return count


def _reset_cache_for_tests() -> None:
    global _LOADED
    with _LOCK:
        _QUEUE.clear()
        _LOADED = False

"""Proactive chat messages — autonomous status updates to the operator.

The mirror of ``operator_chat_responses`` but for cognition-initiated speech:
``PROACTIVE_RESPONSE`` episodes land here at episode close
(``cognition_chain._record_proactive_chat``). The HUD right proactive pinboard
speaks each pending line once, keeps delivered notices pinned until the operator
clears them, and projects every state into read-only Settings history. These
records must NEVER feed back into pressure.

Durable (``guardian/state/proactive_chat_messages.json``, write-through) and
failure-soft — store I/O degrades and emits, it never halts cognition. The
day-zero factory wipe clears it with the rest of guardian/state.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from copy import deepcopy
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("omega.pressure_loop.proactive_chat_messages")

STATUS_PENDING = "pending"

#: The one notice kind exempt from text-identity dedupe: a boot greeting is meant to repeat every
#: time Omega wakes (see the note in ``append_update``). Kept here as a constant so the exemption is
#: a named rule rather than a string buried in a condition.
_REPEATABLE_KIND = "BOOT_GREETING"
STATUS_DELIVERED = "delivered"
STATUS_CLEARED = "cleared"

_MESSAGES: List[Dict[str, Any]] = []
_LOADED = False
_LOCK = threading.RLock()


def _store_path() -> Path:
    from core.paths import OMEGA_ROOT

    return OMEGA_ROOT / "guardian" / "state" / "proactive_chat_messages.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit_degraded(operation: str, exc: Exception) -> None:
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "pressure",
            "proactive_chat_messages",
            status="degraded",
            summary=f"{operation} persistence failed; in-memory store serving",
            ok=False,
            error=str(exc),
        )
    except Exception:
        pass


def _persist() -> None:
    try:
        path = _store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(_MESSAGES, ensure_ascii=False, indent=2),
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
            _MESSAGES.extend(d for d in raw if isinstance(d, dict))
    except Exception as exc:
        _emit_degraded("load", exc)


def _find(message_id: str) -> Optional[Dict[str, Any]]:
    for rec in _MESSAGES:
        if str(rec.get("id")) == str(message_id):
            return rec
    return None


def _find_by_episode(episode_id: str) -> Optional[Dict[str, Any]]:
    eid = str(episode_id or "").strip()
    if not eid:
        return None
    for rec in reversed(_MESSAGES):
        if str(rec.get("episode_id") or "") == eid:
            return rec
    return None


def _find_by_result(result_id: str) -> Optional[Dict[str, Any]]:
    rid = str(result_id or "").strip()
    if not rid:
        return None
    for rec in reversed(_MESSAGES):
        payload = rec.get("payload")
        if isinstance(payload, Mapping) and str(payload.get("result_id") or "") == rid:
            return rec
    return None


def append_update(
    *,
    text: str,
    episode_id: str,
    opened_by: str = "",
    intent: str = "PROACTIVE_RESPONSE",
    kind: str = "NOTICE",
    payload: Mapping[str, Any] | None = None,
) -> Optional[Dict[str, Any]]:
    """Record one autonomous proactive update (status ``pending``)."""
    body = str(text or "").strip()
    if not body:
        return None
    eid = str(episode_id or "").strip()
    detached_payload = deepcopy(dict(payload or {}))
    with _LOCK:
        _ensure_loaded()
        result_id = str(detached_payload.get("result_id") or "").strip()
        if str(kind or "").strip() == "PRESENT_RESULT" and result_id:
            existing = _find_by_result(result_id)
            if existing is not None:
                return dict(existing)
        if eid:
            existing = _find_by_episode(eid)
            if existing is not None:
                return dict(existing)
        # Identical autonomous text is spam by definition (operator 2026-08-09:
        # the workspace nudge landed 40x because every reconnect minted a fresh
        # connection UUID and the episode-key dedupe never matched). The text
        # IS the identity for notices — dedupe against full history, any status.
        #
        # Except the boot greeting (operator 2026-09-22), whose whole purpose is to be said again
        # every time Omega wakes: it draws from a handful of templates, so a collision across boots
        # is certain, and suppressing it would silence the audible proof that speech is resident.
        # Its once-per-boot guarantee comes from pressure_loop.boot_greeting's own state, not from
        # this spam filter.
        if str(kind or "").strip() != _REPEATABLE_KIND:
            for rec in _MESSAGES:
                if str(rec.get("text") or "") == body:
                    return dict(rec)
        record: Dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "ts": _now_iso(),
            "episode_id": str(episode_id or ""),
            "opened_by": str(opened_by or ""),
            "intent": str(intent or "PROACTIVE_RESPONSE"),
            "kind": str(kind or "NOTICE"),
            "payload": detached_payload,
            "text": body,
            "status": STATUS_PENDING,
        }
        _MESSAGES.append(record)
        _persist()
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "proactive_chat_appended",
                episode_id=record["episode_id"] or None,
                summary=body[:80],
                message_id=record["id"],
                opened_by=record["opened_by"],
            )
        except Exception:
            pass
        return dict(record)


def list_pending() -> List[Dict[str, Any]]:
    """Updates not yet acknowledged by the HUD delivery path."""
    with _LOCK:
        _ensure_loaded()
        return [dict(rec) for rec in _MESSAGES if rec.get("status") == STATUS_PENDING]


def list_pinned() -> List[Dict[str, Any]]:
    """Informational notices still visible in the right pinboard."""
    with _LOCK:
        _ensure_loaded()
        return [
            dict(rec)
            for rec in _MESSAGES
            if rec.get("status") in {STATUS_PENDING, STATUS_DELIVERED}
        ]


def list_messages() -> List[Dict[str, Any]]:
    """Complete durable notice history, oldest first."""
    with _LOCK:
        _ensure_loaded()
        return [dict(rec) for rec in _MESSAGES]


def mark_delivered(message_id: str) -> Optional[Dict[str, Any]]:
    """Acknowledge first presentation without changing pin state."""
    with _LOCK:
        _ensure_loaded()
        rec = _find(str(message_id or ""))
        if rec is None:
            return None
        if rec.get("status") == STATUS_CLEARED:
            return dict(rec)
        if rec.get("status") != STATUS_DELIVERED:
            rec["status"] = STATUS_DELIVERED
            rec["delivered_at"] = _now_iso()
            _persist()
        return dict(rec)


def clear_pinned_notices() -> int:
    """Unpin informational notices while preserving durable history."""
    with _LOCK:
        _ensure_loaded()
        changed = 0
        cleared_at = _now_iso()
        for rec in _MESSAGES:
            if rec.get("status") not in {STATUS_PENDING, STATUS_DELIVERED}:
                continue
            rec["status"] = STATUS_CLEARED
            rec["cleared_at"] = cleared_at
            changed += 1
        if changed:
            _persist()
        return changed


def clear_messages() -> None:
    """Empty the store (tests / explicit reset)."""
    with _LOCK:
        _ensure_loaded()
        _MESSAGES.clear()
        _persist()


def _reset_cache_for_tests() -> None:
    """Drop the in-memory cache so the next access reloads from disk."""
    global _LOADED
    with _LOCK:
        _MESSAGES.clear()
        _LOADED = False

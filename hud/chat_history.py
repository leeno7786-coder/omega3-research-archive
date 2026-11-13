"""Operator chat transcript ring for the React HUD."""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List

from core.paths import GUARDIAN_STATE_ROOT

_HISTORY_PATH = GUARDIAN_STATE_ROOT / "hud" / "chat_history.json"
_MAX_MESSAGES = 200
_lock = threading.Lock()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _normalize_message(raw: Dict[str, Any]) -> Dict[str, Any]:
    msg_id = str(raw.get("id") or f"msg-{_now_ms()}")
    role = str(raw.get("role") or "omega").strip().lower()
    if role == "user":
        role = "operator"
    if role == "assistant":
        role = "omega"
    content = str(raw.get("content") or raw.get("text") or raw.get("message") or "")
    ts = raw.get("timestamp")
    if not isinstance(ts, (int, float)):
        ts = _now_ms()
    out: Dict[str, Any] = {
        "id": msg_id,
        "role": role,
        "content": content,
        "timestamp": int(ts),
    }
    for key in ("imageUrl", "uploadName", "uploadKind"):
        if raw.get(key) is not None:
            out[key] = raw[key]
    return out


def _load_unlocked() -> List[Dict[str, Any]]:
    if not _HISTORY_PATH.is_file():
        return []
    try:
        data = json.loads(_HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"chat history is unreadable: {_HISTORY_PATH}") from exc
    if not isinstance(data, list):
        raise RuntimeError(f"chat history must contain a JSON list: {_HISTORY_PATH}")
    if any(not isinstance(message, dict) for message in data):
        raise RuntimeError(f"chat history contains an invalid message: {_HISTORY_PATH}")
    return [_normalize_message(message) for message in data]


def _save_unlocked(messages: List[Dict[str, Any]]) -> None:
    _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    trimmed = messages[-_MAX_MESSAGES:]
    tmp = _HISTORY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_HISTORY_PATH)


def clear_messages() -> int:
    """Empty the transcript ring — the chat panel is IN-SESSION ONLY (operator
    2026-07-12: 'clear the chat panel every shutdown'). Called at boot so a
    crash-shutdown clears too; durable content lives in the dedicated stores."""
    with _lock:
        count = len(_load_unlocked())
        _save_unlocked([])
        return count


def list_messages() -> List[Dict[str, Any]]:
    with _lock:
        return list(_load_unlocked())


def append_message(raw: Dict[str, Any]) -> Dict[str, Any]:
    msg = _normalize_message(raw)
    with _lock:
        messages = _load_unlocked()
        messages.append(msg)
        _save_unlocked(messages)
    return msg


def record_operator_turn(text: str) -> Dict[str, Any]:
    return append_message(
        {"id": f"op-{_now_ms()}", "role": "operator", "content": text, "timestamp": _now_ms()}
    )


def record_chat_result(result: Dict[str, Any], *, operator_text: str = "") -> None:
    """Persist operator + omega lines after POST /api/chat completes."""
    if operator_text.strip():
        record_operator_turn(operator_text.strip())

    message = str(result.get("message") or result.get("response_text") or "").strip()
    if not message:
        delivery = result.get("delivery") if isinstance(result.get("delivery"), dict) else {}
        message = str(delivery.get("text") or delivery.get("rendered_text") or "").strip()

    if message:
        entry: Dict[str, Any] = {
            "id": f"omega-{_now_ms()}",
            "role": "omega",
            "content": message,
            "timestamp": _now_ms(),
        }
        img = result.get("image")
        if isinstance(img, dict) and img.get("url"):
            entry["imageUrl"] = img["url"]
            entry["content"] = f'Image generated: "{operator_text.strip() or message}"'
        doc = result.get("document")
        if isinstance(doc, dict) and doc.get("url"):
            entry["content"] = message or f"Document created: {doc.get('name') or 'file'}"
        append_message(entry)

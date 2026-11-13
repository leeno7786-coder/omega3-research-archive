"""Read-only proactive history projected from the two existing durable stores.

This module owns no persistence. Questions and result presentations remain in
``system_messages``; informational notices remain in
``proactive_chat_messages``. The projection gives Settings one stable shape
without creating a second authoritative ledger.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List


def _dialogue_entry(record: Dict[str, Any]) -> Dict[str, Any]:
    is_result = str(record.get("kind") or "") == "result_presentation"
    return {
        "id": str(record.get("id") or ""),
        "ts": record.get("ts"),
        "type": "result" if is_result else "question",
        "status": str(record.get("status") or ""),
        "text": str(record.get("text") or ""),
        "response": deepcopy(record.get("response")),
        "episode_id": str(record.get("episode_id") or ""),
        "thread_id": str(record.get("proactive_thread_id") or ""),
        "result_id": str(record.get("result_id") or ""),
    }


def _notice_entry(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(record.get("id") or ""),
        "ts": record.get("ts"),
        "type": "notice",
        "status": str(record.get("status") or ""),
        "text": str(record.get("text") or ""),
        "response": None,
        "episode_id": str(record.get("episode_id") or ""),
        "thread_id": "",
        "result_id": str(record.get("result_id") or ""),
    }


def build_proactive_history() -> List[Dict[str, Any]]:
    """Return the complete proactive interaction history, newest first."""
    from pressure_loop.proactive_chat_messages import list_messages as list_notices
    from pressure_loop.system_messages import list_messages as list_dialogue

    entries = [_dialogue_entry(record) for record in list_dialogue()]
    entries.extend(_notice_entry(record) for record in list_notices())
    entries.sort(key=lambda row: str(row.get("ts") or ""), reverse=True)
    return entries

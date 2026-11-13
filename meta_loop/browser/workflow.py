"""Durable multi-turn state for the browser lane.

The browser lane is deliberately small and boring: AUTO's agent loop owns every browser
action, while this module remembers the operator's objective, their mid-run notes and a
bounded record of each run between operator turns. Page text, DOM and screenshots never
enter this state. Run receipts never decide semantic completion; Omega does, and only an
explicit completion command from the operator closes the workflow.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from schema.browser_perception import bounded_text, public_url

WORKFLOW_VERSION = "2.0.0"
_LOCK = threading.RLock()
_MAX_RUNS = 24
_MAX_NOTES = 32
# One model call's share of pending operator notes; the rest wait for the next step.
_NOTE_BATCH = 8
_NOTE_BATCH_CHARS = 8000
_MAX_FAILURES = 3
_RUNNABLE = frozenset({"active", "waiting_for_operator"})
_OPEN = _RUNNABLE | {"blocked"}
_STATUSES = _OPEN | {"completed", "stopped"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def workflow_path() -> Path:
    override = os.environ.get("OMEGA_BROWSER_WORKFLOW_PATH", "").strip()
    if override:
        return Path(override)
    from core.paths import GUARDIAN_STATE_ROOT

    return GUARDIAN_STATE_ROOT / "browser_workflow.json"


def _normalise(text: Any) -> str:
    return " ".join(str(text or "").split()).strip()


_COMPLETION_RE = re.compile(
    r"^(?:(?:you\s+can|please|ok(?:ay)?,?|now)\s+)?"
    r"(?:finished|finish|done|complete|completed|stop|close|end|exit|quit)"
    r"(?:\s+(?:with\s+)?(?:the\s+|this\s+|that\s+)?(?:browser\s+workflow|browser|browsing|workflow|session|tab|window))?[.!]?$|"
    r"^(?:(?:i\s*am|i['’]?m|we\s+are|we['’]re)\s+(?:all\s+)?(?:finished|done)"
    r"(?:\s+with\s+(?:the\s+|this\s+|that\s+)?(?:browser|browsing|workflow|session|tab|window))?|"
    r"that['’]s\s+(?:all|it|enough)|all\s+done|no\s+more\s+browsing)[.!]?$",
    re.IGNORECASE,
)


def is_browser_completion_command(text: Any) -> bool:
    """Return true only for an explicit, unambiguous workflow-close phrase."""
    return bool(_COMPLETION_RE.fullmatch(_normalise(text)))


def _read() -> dict[str, Any] | None:
    path = workflow_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("browser workflow is unreadable") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid browser workflow root")
    if payload.get("version") != WORKFLOW_VERSION:
        # A workflow an older build saved described a plan machinery that no longer exists;
        # its browser session died with that process. Nothing of it is resumed.
        return None
    bounded_text(payload.get("workflow_id"), "workflow_id", 128)
    bounded_text(payload.get("objective"), "workflow objective", 4000)
    if payload.get("status") not in _STATUSES:
        raise ValueError("invalid browser workflow status")
    if type(payload.get("revision")) is not int or payload["revision"] < 0:
        raise ValueError("invalid browser workflow revision")
    return payload


def _write(payload: Mapping[str, Any]) -> dict[str, Any]:
    path = workflow_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    return dict(payload)


def _bump(state: dict[str, Any]) -> None:
    state["revision"] = int(state.get("revision") or 0) + 1
    state["updated_at"] = _now()


def load_browser_workflow() -> dict[str, Any] | None:
    with _LOCK:
        value = _read()
        return dict(value) if value else None


def active_browser_workflow() -> dict[str, Any] | None:
    value = load_browser_workflow()
    if value and value.get("status") in _OPEN:
        return value
    return None


def _new_state(objective: str, *, cycle_id: str = "", workflow_id: str | None = None) -> dict[str, Any]:
    bounded_text(objective, "objective", 4000)
    now = _now()
    return {
        "version": WORKFLOW_VERSION,
        "workflow_id": workflow_id or f"browser-{uuid.uuid4().hex}",
        "status": "active",
        "objective": objective,
        "instruction": objective,
        "notes": [],
        "runs": [],
        "failures": 0,
        "last_error": "",
        "revision": 0,
        "created_cycle_id": str(cycle_id or ""),
        "created_at": now,
        "updated_at": now,
    }


def open_browser_workflow(objective: str, *, cycle_id: str = "") -> dict[str, Any]:
    with _LOCK:
        current = _read()
        if current and current.get("status") in _OPEN:
            return dict(current)
        return _write(_new_state(objective, cycle_id=cycle_id))


def prepare_browser_turn(objective: str, *, cycle_id: str = "") -> dict[str, Any]:
    """Bind an operator turn to the open workflow or start a new one.

    A completion phrase closes the open workflow. Any other message on an open workflow is
    the operator's newest instruction: it revives a blocked or waiting workflow and is
    queued as a note so a run already in progress reads it mid-run (operator 2026-09-15:
    chat with Omega while it works). The returned mapping carries no page observation.
    """
    bounded_text(objective, "objective", 4000)
    clean = _normalise(objective)
    with _LOCK:
        current = _read()
        if is_browser_completion_command(clean):
            if current and current.get("status") in _OPEN:
                current["status"] = "completed"
                current["completion_command"] = clean[:200]
                _bump(current)
                _write(current)
                return {"closed": True, "workflow": _receipt(current)}
            return {"closed": False, "workflow": None}
        if current and current.get("status") in _OPEN:
            current["status"] = "active"
            current["failures"] = 0
            current["last_error"] = ""
            current["instruction"] = clean[:4000]
            notes = [note for note in current.get("notes") or () if isinstance(note, dict)]
            notes.append({"text": clean, "cycle_id": str(cycle_id or "")[:120], "delivered": False,
                          "at": _now()})
            current["notes"] = [
                note for index, note in enumerate(notes)
                if not note.get("delivered") or index >= len(notes) - _MAX_NOTES
            ]
            _bump(current)
            _write(current)
        else:
            current = _new_state(objective, cycle_id=cycle_id)
            _write(current)
        return {"closed": False, "workflow": _receipt(current)}


def _pending_notes(state: Mapping[str, Any] | None, workflow_id: str | None) -> list[dict]:
    if not workflow_id or not state or str(state.get("workflow_id")) != str(workflow_id):
        return []
    return [note for note in state.get("notes") or () if isinstance(note, dict) and not note.get("delivered")]


def take_operator_notes(workflow_id: str | None) -> list[str]:
    """The oldest undelivered operator notes for a run in progress; each is handed over once.

    One model call carries at most a bounded batch (count and characters); the rest stay
    pending, oldest first, for the next step. At least one note is always taken, and a
    single note is at most 4,000 characters, so every note is delivered and none dropped.
    """
    with _LOCK:
        state = _read()
        pending = _pending_notes(state, workflow_id)
        batch: list[dict] = []
        chars = 0
        for note in pending:
            size = len(str(note.get("text") or ""))
            if batch and (len(batch) >= _NOTE_BATCH or chars + size > _NOTE_BATCH_CHARS):
                break
            batch.append(note)
            chars += size
        if not batch:
            return []
        for note in batch:
            note["delivered"] = True
        state["updated_at"] = _now()
        _write(state)
        return [str(note.get("text") or "") for note in batch]


def pending_operator_note_count(workflow_id: str | None) -> int:
    """How many operator notes are still waiting for a later step of this run."""
    with _LOCK:
        return len(_pending_notes(_read(), workflow_id))


def operator_notes_for_step(workflow_id: str | None) -> list[str]:
    """One step's bounded batch, saying when more are queued so the run does not finish early."""
    with _LOCK:
        notes = take_operator_notes(workflow_id)
        remaining = pending_operator_note_count(workflow_id)
    if notes and remaining:
        notes.append(f"({remaining} more operator update(s) are queued and arrive on your next step; "
                     "read them before finishing.)")
    return notes


def _receipt(state: Mapping[str, Any]) -> dict[str, Any]:
    runs = [row for row in state.get("runs") or () if isinstance(row, Mapping)]
    notes = [note for note in state.get("notes") or () if isinstance(note, Mapping)]
    receipt = {
        "version": WORKFLOW_VERSION,
        "workflow_id": str(state.get("workflow_id") or ""),
        "status": str(state.get("status") or "active"),
        "objective": bounded_text(state.get("objective"), "objective", 4000),
        "instruction": str(state.get("instruction") or state.get("objective") or "")[:4000],
        "run_count": len(runs),
        "action_count": sum(int(row.get("steps") or 0) for row in runs),
        "pending_notes": sum(not note.get("delivered") for note in notes),
        "failures": int(state.get("failures") or 0),
        "last_error": str(state.get("last_error") or "")[:500],
        "last_run": ({key: runs[-1].get(key) for key in ("status", "steps", "summary", "question", "stopped_reason")}
                     if runs else None),
        "revision": int(state.get("revision") or 0),
    }
    if isinstance(state.get("last_page"), dict):
        receipt["last_page"] = dict(state["last_page"])
    return receipt


def browser_workflow_receipt(workflow_id: str | None = None) -> dict[str, Any] | None:
    state = load_browser_workflow()
    if not state or (workflow_id and str(state.get("workflow_id")) != str(workflow_id)):
        return None
    return _receipt(state)


def browser_progress_digest(workflow_id: str | None, limit: int = 4000) -> str:
    """A host-authored account of earlier runs for the next run's brief; no page content."""
    state = load_browser_workflow()
    if not state or (workflow_id and str(state.get("workflow_id")) != str(workflow_id)):
        return ""
    lines = []
    for row in [row for row in state.get("runs") or () if isinstance(row, Mapping)][-6:]:
        actions = "; ".join(str(item) for item in row.get("actions") or ())[:600]
        line = f"run {row.get('index')}: {row.get('status')} after {row.get('steps')} actions"
        if row.get("summary"):
            line += f"; said: {str(row['summary'])[:300]}"
        if row.get("question"):
            line += f"; asked: {str(row['question'])[:300]}"
        if row.get("stopped_reason"):
            line += f"; stopped: {str(row['stopped_reason'])[:200]}"
        if actions:
            line += f"; actions: {actions}"
        if row.get("last_url"):
            line += f"; left at {row['last_url']}"
        lines.append(line)
    return "\n".join(lines)[:limit]


def record_browser_run(workflow_id: str | None, run: Mapping[str, Any]) -> dict[str, Any] | None:
    """Keep a bounded, page-free record of one agent run against the workflow."""
    if not workflow_id:
        return None
    with _LOCK:
        state = _read()
        if not state or str(state.get("workflow_id")) != str(workflow_id):
            return None
        if state.get("status") in {"completed", "stopped"}:
            return _receipt(state)
        status = str(run.get("status") or "blocked")[:40]
        final = run.get("final_page") if isinstance(run.get("final_page"), Mapping) else {}
        runs = [row for row in state.get("runs") or () if isinstance(row, dict)]
        row = {
            "index": len(runs) + 1,
            "status": status,
            "steps": int(run.get("steps") or 0),
            "actions": [str(item.get("summary") or item.get("action") or "")[:120]
                        for item in (run.get("actions") or ()) if isinstance(item, Mapping)][-16:],
            "summary": str(run.get("summary") or "")[:1000],
            "question": str(run.get("question") or "")[:1000],
            "stopped_reason": str(run.get("stopped_reason") or "")[:300],
            "last_url": public_url(str(final.get("url") or "")),
            "cycle_id": str(run.get("cycle_id") or "")[:120],
            "at": _now(),
        }
        runs.append(row)
        state["runs"] = runs[-_MAX_RUNS:]
        if final.get("url"):
            state["last_page"] = {"url": public_url(str(final["url"])), "title": str(final.get("title") or "")[:500]}
        if status in {"blocked", "inference_failed"}:
            state["failures"] = int(state.get("failures") or 0) + 1
            state["last_error"] = row["stopped_reason"] or f"browser run {status}"
            if state["failures"] >= _MAX_FAILURES:
                state["status"] = "blocked"
        elif status in {"done", "ask_operator", "budget_exhausted", "stopped"}:
            state["failures"] = 0
            state["last_error"] = ""
        _bump(state)
        _write(state)
        return _receipt(state)


def mark_browser_waiting(workflow_id: str | None) -> dict[str, Any] | None:
    if not workflow_id:
        return None
    with _LOCK:
        state = _read()
        if not state or str(state.get("workflow_id")) != str(workflow_id):
            return None
        if state.get("status") not in _RUNNABLE:
            return _receipt(state)
        # A pause is factual control state, never proof of semantic completion.
        state["status"] = "waiting_for_operator"
        state["updated_at"] = _now()
        _write(state)
        return _receipt(state)


def close_browser_workflow(workflow_id: str | None = None, *, command: str = "finished") -> dict[str, Any] | None:
    with _LOCK:
        state = _read()
        if not state or (workflow_id and str(state.get("workflow_id")) != str(workflow_id)):
            return None
        if state.get("status") in {"completed", "stopped"}:
            return _receipt(state)
        state["status"] = "completed"
        state["completion_command"] = _normalise(command)[:200]
        _bump(state)
        _write(state)
        return _receipt(state)


def browser_workflow_prompt(workflow_id: str | None) -> str:
    receipt = browser_workflow_receipt(workflow_id)
    if not receipt:
        return ""
    return (
        "BROWSER WORKFLOW RECEIPT (host-authored; no page content): "
        f"workflow_id={receipt['workflow_id']} status={receipt['status']} "
        f"runs={receipt['run_count']} actions={receipt['action_count']}. "
        "Run receipts are not semantic completion; only an explicit finished/done command closes it."
    )


__all__ = [
    "WORKFLOW_VERSION",
    "active_browser_workflow",
    "browser_progress_digest",
    "browser_workflow_prompt",
    "browser_workflow_receipt",
    "close_browser_workflow",
    "is_browser_completion_command",
    "load_browser_workflow",
    "mark_browser_waiting",
    "open_browser_workflow",
    "prepare_browser_turn",
    "record_browser_run",
    "take_operator_notes",
    "workflow_path",
]

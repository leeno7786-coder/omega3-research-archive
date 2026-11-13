"""Loop phase data logging — persist before/after phase events to guardian/logs.

Every ``phase_span`` enter/exit is mirrored here as structured JSONL under
``guardian/logs/{loop}.jsonl`` via :func:`guardian.data_logger.emit_event`.
"""

from __future__ import annotations

from typing import Any


def _subsystem(loop: str) -> str:
    name = str(loop or "guardian").strip().lower() or "guardian"
    return f"omega.{name}.loop"


def log_loop_phase(
    *,
    hook: str,
    loop: str,
    phase: str,
    status: str,
    **fields: Any,
) -> None:
    """Write one phase boundary event to the loop's guardian JSONL log.

    ``hook`` is ``before`` (phase starting) or ``after`` (phase finished).
    """
    hook_key = str(hook or "").strip().lower()
    if hook_key not in ("before", "after"):
        hook_key = "before" if str(status or "").lower() == "enter" else "after"
    event_name = f"phase_{hook_key}"
    payload = {
        "loop": str(loop or ""),
        "phase": str(phase or ""),
        "status": str(status or ""),
        **{k: v for k, v in fields.items() if v is not None},
    }
    try:
        from guardian.data_logger import emit_event

        emit_event(_subsystem(loop), event_name, **payload)
    except Exception:
        pass

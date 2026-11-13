"""AUTO validate phase — ExecutionActionPack hard-ban + slice-3 rules."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

from meta_loop.pack import validate_execution_action_pack


def validate_for_execution(
    execution_pack: Mapping[str, Any],
) -> Tuple[bool, Optional[str], Optional[str]]:
    """PHASE 0 — validate pack; reuse Meta A hard-ban + slice-3 rules."""
    try:
        validate_execution_action_pack(dict(execution_pack))
    except (TypeError, ValueError) as exc:
        valid = False
        failure_kind = "invalid_execution_pack"
        failure_reason = str(exc)
    else:
        valid = True
        failure_kind = None
        failure_reason = None

    try:
        from guardian.terminal_feedback import emit_data, emit_guardian_event

        pack = dict(execution_pack)
        ep_id = str(pack.get("episode_id") or "")
        emit_data(
            "auto",
            "validate",
            episode_id=ep_id or None,
            summary=f"valid={valid}",
            valid=valid,
            failure_kind=failure_kind,
            failure_reason=failure_reason,
            action=pack.get("action"),
        )
        emit_guardian_event(
            "omega.auto",
            "validate",
            episode_id=ep_id,
            valid=valid,
            failure_kind=failure_kind,
            failure_reason=failure_reason,
            action=pack.get("action"),
        )
    except Exception:
        pass

    return valid, failure_kind, failure_reason


def run(execution_pack: Mapping[str, Any]) -> Tuple[bool, Optional[str], Optional[str]]:
    return validate_for_execution(execution_pack)

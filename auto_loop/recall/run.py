"""AUTO recall phase — pre-execute memory envelope (reads happen in MRIL execute)."""

from __future__ import annotations

from typing import Any, Mapping

from auto_loop._auto_state import MemoryEnvelope
from auto_loop.mril_tools.run import memory_recall_required


def memory_envelope_before_execute(
    execution_pack: Mapping[str, Any],
) -> MemoryEnvelope:
    """Build the pre-execute envelope. Lobe reads run via remember_* in execute."""
    pack = dict(execution_pack)
    # A routed browser capability turn is action-grounded.  Its ingress route
    # takes precedence over stale memory flags carried by the classifier.
    required = memory_recall_required(pack)
    reason = pack.get("memory_reason")
    scope = tuple(pack.get("memory_scope") or ())

    if not required:
        envelope = MemoryEnvelope(
            memory_required=False,
            memory_status="skipped",
            memory_reason=reason,
            memory_scope=scope,
            items=(),
            error=None,
        )
    else:
        envelope = MemoryEnvelope(
            memory_required=True,
            memory_status="pending",
            memory_reason=reason,
            memory_scope=scope,
            items=(),
            error=None,
        )

    try:
        from guardian.terminal_feedback import emit_data, emit_guardian_event

        ep_id = str(pack.get("episode_id") or "")
        emit_data(
            "auto",
            "memory_envelope",
            episode_id=ep_id or None,
            summary=f"required={required} status={envelope.memory_status}",
            memory_required=required,
            memory_reason=reason,
            memory_status=envelope.memory_status,
        )
        emit_guardian_event(
            "omega.auto",
            "memory_envelope",
            episode_id=ep_id,
            memory_required=required,
            memory_reason=reason,
            memory_status=envelope.memory_status,
        )
    except Exception:
        pass

    return envelope


def run(execution_pack: Mapping[str, Any], episode_id: str, **_: Any) -> MemoryEnvelope:
    return memory_envelope_before_execute(execution_pack)

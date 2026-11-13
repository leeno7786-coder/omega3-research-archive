"""Meta B reflect phase — build MetaReflectionPack."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from auto_loop.fold.run import (
    plugin_execution_envelope,
    plugin_permission_is_pending,
    screen_plugin_outputs,
)
from meta_loop.consistency.run import MetaBInput


@dataclass(frozen=True)
class MetaReflectionPack:
    episode_id: str
    consistency: dict
    execution: dict
    trace_summary: dict
    neuromod_adjustments: dict = field(default_factory=dict)
    # WO-06: the deterministic criteria checks (meta_loop/checks). Always present on a pack this
    # phase builds; an objective with nothing to check says so with verdict ``no_criteria``.
    checks: dict = field(default_factory=dict)


def _auto_payload(auto_outcome: Mapping[str, Any]) -> dict:
    inner = auto_outcome.get("auto")
    if isinstance(inner, Mapping):
        return dict(inner)
    return dict(auto_outcome)


def _memory_status(auto: Mapping[str, Any]) -> str:
    if auto.get("memory_status") is not None:
        return str(auto.get("memory_status") or "skipped")
    mem = auto.get("memory")
    if isinstance(mem, Mapping):
        return str(mem.get("memory_status") or "skipped")
    return "skipped"


def _operator_stop(outputs: Mapping[str, Any]) -> dict | None:
    """A dispatch the operator stopped: the valid cancellation ``brain_skill_executor`` reports.

    The executor returns ``status: "partial"`` with ``job_status: "stopped"`` when the operator
    halts a build. Read as a bare partial it looks like unfinished work, and read as a failure it
    becomes a reasoning failure nobody made; it is neither, so it is recorded with its source.
    """
    for key in ("qwen_skill_result", "skill_result"):
        result = outputs.get(key)
        if isinstance(result, Mapping) and str(result.get("job_status") or "") == "stopped":
            return {
                "source": f"auto.outputs.{key}.job_status",
                "job_status": "stopped",
                "summary": str(result.get("summary") or ""),
            }
    return None


def meta_reflection_pack_to_dict(pack: MetaReflectionPack) -> dict:
    return asdict(pack)


def run(
    mbi: MetaBInput,
    consistency: dict,
    disposition: str,
    status: str,
) -> MetaReflectionPack:
    auto_inner = _auto_payload(mbi.auto_outcome)
    outputs = screen_plugin_outputs(
        dict(auto_inner.get("outputs") or {}),
        preserve_receipts=True,
    )
    tele = dict(auto_inner.get("telemetry") or {})
    action = str(mbi.execution_pack.get("action") or "")
    # Soft gate: Meta B reflects over execution ONLY IF execution actually ran.
    # IDLE / skipped did not execute -> executed False, result 'none' (never a
    # fabricated failure). When execution ran, fold success-vs-failure up to
    # Global B; failure_reason carries the reason-it-failed.
    permission_request = outputs.get("plugin_permission_request")
    permission_pending = plugin_permission_is_pending(auto_inner, outputs)
    pending_input = status in {"pending_input", "pending_approval"} or permission_pending
    executed = bool(
        action
        and action != "IDLE"
        and status not in {"idle", "skipped", "invalid"}
        and not pending_input
    )
    if not executed:
        result = "pending_input" if pending_input else "none"
    elif status == "success":
        result = "success"
    elif status == "partial":
        # AUTO uses partial for yielded/progress-owed work. It is not a failure,
        # and collapsing it here loses the executor's honest state upstream.
        result = "partial"
    else:
        result = "failure"
    execution_status = "pending_input" if pending_input else status
    from meta_loop.checks.run import run as run_criteria_checks

    checks = run_criteria_checks(mbi.episode_id, mbi.motor_program)
    pack = MetaReflectionPack(
        episode_id=mbi.episode_id,
        consistency=consistency,
        execution={
            "executed": executed,
            "result": result,
            "action": action,
            "tool": mbi.execution_pack.get("tool"),
            "status": execution_status,
            "disposition": disposition,
            "reason": mbi.execution_pack.get("tool_reason"),
            "execution_intent": mbi.execution_pack.get("execution_intent"),
            "failure_kind": auto_inner.get("failure_kind"),
            "failure_reason": auto_inner.get("failure_reason"),
            "memory_status": _memory_status(auto_inner),
            "warnings": list(tele.get("warnings") or []),
            "plugin_execution": plugin_execution_envelope(outputs),
            "plugin_failure": outputs.get("plugin_failure"),
            "cancellation": _operator_stop(outputs),
        },
        trace_summary={
            "tool_observation_present": outputs.get("tool_result") is not None,
            "pre_perception_present": (
                (mbi.execution_pack.get("execution_inputs") or {}).get("browser_perception") is not None
            ),
            "browser_run_present": outputs.get("browser_run") is not None,
            "has_response_text": outputs.get("response_text") is not None,
            "has_tool_result": outputs.get("tool_result") is not None,
            "has_image_artifact": outputs.get("image_artifact") is not None,
            "runtime_ms": tele.get("runtime_ms"),
            "has_plugin_evidence": plugin_execution_envelope(outputs) is not None,
            "plugin_plan_present": outputs.get("plugin_plan") is not None,
            "plugin_step_results_present": outputs.get("plugin_step_results") is not None,
            "plugin_permission_request_present": permission_request is not None,
            "plugin_permission_pending": permission_pending,
            "plugin_result_present": outputs.get("plugin_result") is not None,
            "plugin_failure_present": outputs.get("plugin_failure") is not None,
        },
    )

    # Neuromod adjustments: disposition never emits "explore"/"exploit"
    # (see disposition/run.py), so the pack carries neutral values.
    adjustments = {"dopamine": 0.0, "plasticity": 0.0}

    # Python 3 dataclasses with frozen=True require object.__setattr__ to bypass if not using replace()
    # or we can just initialize it in the constructor directly
    pack = MetaReflectionPack(
        episode_id=mbi.episode_id,
        consistency=consistency,
        execution=pack.execution,
        trace_summary=pack.trace_summary,
        neuromod_adjustments=adjustments,
        checks=checks,
    )

    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "meta",
            "meta_reflection_pack",
            episode_id=mbi.episode_id,
            summary=(
                f"disposition={disposition}"
                + (
                    f" reason={mbi.execution_pack.get('tool_reason')}"
                    if mbi.execution_pack.get("tool_reason")
                    else ""
                )
            ),
            consistency=consistency,
            disposition=disposition,
            status=status,
        )
    except Exception:
        pass
    return pack

"""AUTO fold phase — episode.auto fold + AutoOutcomePack emit."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
from typing import Any, Dict, Optional

from auto_loop._auto_state import (
    AutoInput,
    AutoOutcomePack,
    MemoryEnvelope,
    PromptPack,
    _hash_text,
)
from auto_loop.motor_adapter import effective_motor_action

_PLUGIN_EVIDENCE_KEYS = (
    "plugin_plan",
    "plugin_step_results",
    "plugin_permission_request",
    "plugin_result",
    "plugin_failure",
)
_PENDING_PERMISSION_STATES = frozenset(
    {"pending_input", "pending_approval", "permission_required"}
)
_PLUGIN_SCREEN_RECEIPTS_KEY = "plugin_screen_receipts"
_MAX_GUARDIAN_RECEIPT_BYTES = 2048
_MAX_GUARDIAN_DECISIONS = 32
_MAX_GUARDIAN_FIELD_CHARS = 128
_MAX_GUARDIAN_DECISION_CHARS = 32


def plugin_permission_is_pending(
    auto_payload: Mapping[str, Any],
    outputs: Mapping[str, Any],
) -> bool:
    """Identify a pending permission from AUTO state and the real HUD payload."""
    if not isinstance(outputs.get("plugin_permission_request"), Mapping):
        return False
    return any(
        str(auto_payload.get(key) or "").strip().lower() in _PENDING_PERMISSION_STATES
        for key in ("failure_kind", "cycle_kind")
    )


def _bounded_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _compact_guardian_receipt(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    compact = {
        "screened": bool(raw.get("screened")),
        "quarantined": _bounded_int(raw.get("quarantined")),
        "screened_fields": _bounded_int(raw.get("screened_fields")),
        "decisions": {},
    }
    raw_decisions = raw.get("decisions")
    if not isinstance(raw_decisions, Mapping):
        return compact
    for key, decision in raw_decisions.items():
        if len(compact["decisions"]) >= _MAX_GUARDIAN_DECISIONS:
            break
        field = str(key)[:_MAX_GUARDIAN_FIELD_CHARS]
        verdict = str(decision)[:_MAX_GUARDIAN_DECISION_CHARS]
        candidate = dict(compact)
        candidate["decisions"] = dict(compact["decisions"])
        candidate["decisions"][field] = verdict
        if (
            len(json.dumps(candidate, separators=(",", ":"), ensure_ascii=False))
            > _MAX_GUARDIAN_RECEIPT_BYTES
        ):
            break
        compact["decisions"][field] = verdict
    return compact


def _merge_guardian_receipts(first: Any, second: Any) -> dict[str, Any]:
    left = _compact_guardian_receipt(first)
    right = _compact_guardian_receipt(second)
    decisions = dict(left["decisions"])
    for key, value in right["decisions"].items():
        decisions.setdefault(key, value)
    return _compact_guardian_receipt(
        {
            "screened": left["screened"] or right["screened"],
            "quarantined": max(left["quarantined"], right["quarantined"]),
            "screened_fields": max(left["screened_fields"], right["screened_fields"]),
            "decisions": decisions,
        }
    )


def _compact_receipt_map(value: Any) -> dict[str, dict[str, Any]]:
    raw = value if isinstance(value, Mapping) else {}
    return {
        key: _compact_guardian_receipt(raw[key])
        for key in _PLUGIN_EVIDENCE_KEYS
        if isinstance(raw.get(key), Mapping)
    }


def _screen_plugin_value(
    value: Any,
    *,
    source: str,
    preserve_receipt: bool,
) -> tuple[Any, dict[str, Any] | None]:
    """Screen an external plugin value before it crosses the cognition fold."""
    from guardian.external_ingress import screen_receipt, screen_text

    def _jsonish(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: _jsonish(child) for key, child in value.items()}
        if isinstance(value, (list, tuple)):
            return [_jsonish(child) for child in value]
        return value

    if isinstance(value, Mapping):
        previous = value.get("guardian") if preserve_receipt else None
        screened = screen_receipt(_jsonish(value), source=source)
        guardian = screened.get("guardian")
        screened["guardian"] = (
            _merge_guardian_receipts(previous, guardian)
            if isinstance(previous, Mapping)
            else _compact_guardian_receipt(guardian)
        )
        return screened, None
    if type(value) in {list, tuple}:
        screened = screen_receipt({"value": _jsonish(value)}, source=source)
        return screened["value"], _compact_guardian_receipt(screened.get("guardian"))
    if isinstance(value, str):
        screened, decision = screen_text(value, source=source, field="plugin")
        return (
            screened,
            _compact_guardian_receipt(
                {
                    "screened": True,
                    "quarantined": int(decision == "quarantine"),
                    "screened_fields": 1,
                    "decisions": {"plugin": decision} if decision != "allow" else {},
                }
            ),
        )
    return value, None


def screen_plugin_evidence(
    value: Any,
    *,
    source: str = "plugin-foldback",
    preserve_receipt: bool = False,
) -> Any:
    return _screen_plugin_value(
        value,
        source=source,
        preserve_receipt=preserve_receipt,
    )[0]


def screen_plugin_outputs(
    outputs: Mapping[str, Any],
    *,
    preserve_receipts: bool = False,
) -> dict[str, Any]:
    """Return plugin output copies with every external string leaf screened."""
    screened = dict(outputs)
    prior_receipts = (
        _compact_receipt_map(screened.get(_PLUGIN_SCREEN_RECEIPTS_KEY))
        if preserve_receipts
        else {}
    )
    if not preserve_receipts:
        screened.pop(_PLUGIN_SCREEN_RECEIPTS_KEY, None)
    receipts = dict(prior_receipts)
    for key in _PLUGIN_EVIDENCE_KEYS:
        if key in screened and screened[key] is not None:
            screened_value, receipt = _screen_plugin_value(
                screened[key],
                source="plugin-foldback",
                preserve_receipt=preserve_receipts,
            )
            screened[key] = screened_value
            if receipt is not None:
                receipts[key] = (
                    _merge_guardian_receipts(prior_receipts.get(key), receipt)
                    if preserve_receipts and key in prior_receipts
                    else receipt
                )
    if receipts:
        screened[_PLUGIN_SCREEN_RECEIPTS_KEY] = receipts
    return screened


def plugin_execution_envelope(outputs: Mapping[str, Any]) -> dict[str, Any] | None:
    """Build the bounded provenance envelope retained beside ordinary tool output."""
    plan = outputs.get("plugin_plan")
    result = outputs.get("plugin_result")
    failure = outputs.get("plugin_failure")
    permission = outputs.get("plugin_permission_request")
    step_results = outputs.get("plugin_step_results")
    if not any(value is not None for value in (plan, result, failure, permission, step_results)):
        return None

    plan_dict = dict(plan) if isinstance(plan, Mapping) else {}
    result_dict = (
        dict(result)
        if isinstance(result, Mapping)
        else dict(failure)
        if isinstance(failure, Mapping)
        else {}
    )
    plan_id = str(plan_dict.get("plan_id") or result_dict.get("plan_id") or "")
    authority_digest = str(
        plan_dict.get("authority_digest") or result_dict.get("authority_digest") or ""
    )
    compact_steps = []
    for raw in step_results if isinstance(step_results, (list, tuple)) else ():
        if not isinstance(raw, Mapping):
            continue
        compact_steps.append(
            {
                "step_id": raw.get("step_id"),
                "candidate_id": raw.get("candidate_id"),
                "status": raw.get("status"),
                "summary": raw.get("summary"),
            }
        )
    compact_permission = None
    if isinstance(permission, Mapping):
        compact_permission = {
            key: permission.get(key)
            for key in (
                "request_id",
                "plan_id",
                "authority_digest",
                "revision",
                "display",
            )
            if permission.get(key) is not None
        }
    screen_receipts = _compact_receipt_map(outputs.get(_PLUGIN_SCREEN_RECEIPTS_KEY))
    return {
        "plan_id": plan_id or None,
        "authority_digest": authority_digest or None,
        "steps": compact_steps,
        "permission": compact_permission,
        "screen_receipts": screen_receipts or None,
    }


def format_remembered_knowledge_for_hash(results) -> str:
    from auto_loop.mril_tools import format_remembered_knowledge_block

    if not results:
        return ""
    return format_remembered_knowledge_block(results)


def _fold_episode_auto(
    ai: AutoInput,
    *,
    status: str,
    cycle_kind: str,
    memory_envelope: MemoryEnvelope,
    model_profile: Optional[str],
    model_id: Optional[str],
    prompt_pack: PromptPack,
    exec_result: Dict[str, Any],
    runtime_ms: int,
) -> dict:
    outputs = screen_plugin_outputs(
        dict(exec_result.get("outputs") or {}),
        preserve_receipts=False,
    )
    mem_dict = {
        "memory_required": memory_envelope.memory_required,
        "memory_status": memory_envelope.memory_status,
        "memory_reason": memory_envelope.memory_reason,
        "memory_scope": list(memory_envelope.memory_scope),
        "items": list(memory_envelope.items),
        "error": memory_envelope.error,
        # Cut 1: diagnostic receipts (DME activation traces) — fold-only,
        # never part of the prompt pack or remembered-knowledge block
        "receipts": list(getattr(memory_envelope, "receipts", ()) or ()),
    }
    rk = outputs.get("remembered_knowledge_tool_results")
    rk_text = format_remembered_knowledge_for_hash(rk) if rk else None
    folded = {
        "episode_id": ai.episode_id,
        # Preserve Global A's full source separately from Meta A's source projection.
        "source_action_goal": deepcopy(ai.source_action_goal),
        "action": ai.execution_pack.get("action"),
        # Echo the pack AUTO actually executed so Meta-B consistency compares the
        # meta plan against an independent Auto-side copy (mirrors auto_outcome_from_fold);
        # without it auto_pack == the meta pack and pack/action/tool drift can never fire.
        "execution_pack": dict(ai.execution_pack),
        "status": status,
        "failure_kind": exec_result.get("failure_kind"),
        "failure_reason": exec_result.get("failure_reason"),
        "memory": mem_dict,
        "model_profile": model_profile,
        "model_id": model_id,
        "prompt_pack": {
            "prompt_type": prompt_pack.prompt_type,
            "response_mode": prompt_pack.response_mode,
            "system_prompt_hash": _hash_text(prompt_pack.system_prompt),
            "user_prompt_hash": _hash_text(prompt_pack.user_prompt),
            "remembered_knowledge_hash": _hash_text(rk_text),
            "tool_block_hash": _hash_text(prompt_pack.context_blocks.get("tool_block")),
        },
        "outputs": {
            "response_text": outputs.get("response_text"),
            # The exact queue receipt is the durable boundary between Chat's route and
            # the independently leased specialist lane. Preserve it for reply correlation,
            # Omega's episode pack, and restart-safe diagnostics.
            "specialist_handoff": outputs.get("specialist_handoff"),
            # The reply's deterministic provenance and exact SRAM intake receipt
            # belong to the episode as well as the user-facing response text.
            "deterministic_response_kind": outputs.get("deterministic_response_kind"),
            "memory_intake_records": outputs.get("memory_intake_records"),
            "tool_result": outputs.get("tool_result"),
            "tool_result_summary": outputs.get("tool_result_summary"),
            # Preserve the deterministic execution trace alongside the result. These fields
            # are bounded operational metadata; downstream folds use them to explain a failed
            # tool/apply without reconstructing the executor call.
            "tool_status": outputs.get("tool_status"),
            "tool_error": outputs.get("tool_error"),
            "tool_args": outputs.get("tool_args"),
            "apply_result": outputs.get("apply_result"),
            "apply_status": outputs.get("apply_status"),
            "apply_error": outputs.get("apply_error"),
            "image_artifact": outputs.get("image_artifact"),
            "document_artifact": outputs.get("document_artifact"),
            "remembered_knowledge_tool_results": outputs.get("remembered_knowledge_tool_results"),
            "association_use_receipt": outputs.get("association_use_receipt"),
            # LEARN backlink: keep the artifact id/domain so the episode<->artifact
            # link survives the fold into episode.auto.outputs (deepsleep consume).
            "knowledge_artifact_id": outputs.get("knowledge_artifact_id"),
            "knowledge_domain": outputs.get("knowledge_domain"),
            # Phase 6: keep the USE_SKILL dispatch result so the skill foldback (6K) +
            # consolidation (6L) survive the fold into episode.auto.outputs (None otherwise).
            "skill_result": outputs.get("skill_result"),
            "skill_dispatch": outputs.get("skill_dispatch"),
            "skill_status": outputs.get("skill_status"),
            "skill_objective": outputs.get("skill_objective"),
            "skill_guard_status": outputs.get("skill_guard_status"),
            "skill_self_mod_violations": outputs.get("skill_self_mod_violations"),
            # The work behind an operator reply (2026-09-05): the tools the chat
            # brain called, with results and diffs, and its think trace. They ride
            # episode.auto.outputs so the finished chat.reply carries them as
            # thinking.steps / thinking.reasoning_summary (the poll path and a
            # reloaded HUD repaint the safe trace from there) and the journal keeps
            # what was done.
            # Live 2026-09-05 19:47: the first tool turn streamed every frame and
            # then landed with thinking.steps=[] because this list dropped them.
            "operator_tool_calls": outputs.get("operator_tool_calls"),
            "reasoning": outputs.get("reasoning"),
            **{key: outputs.get(key) for key in _PLUGIN_EVIDENCE_KEYS},
            _PLUGIN_SCREEN_RECEIPTS_KEY: outputs.get(_PLUGIN_SCREEN_RECEIPTS_KEY),
            "plugin_execution": plugin_execution_envelope(outputs),
        },
        "telemetry": {
            "cycle_kind": cycle_kind,
            "runtime_ms": runtime_ms,
            "tool_runtime_ms": outputs.get("tool_runtime_ms"),
            "memory_runtime_ms": None,
            "warnings": list(exec_result.get("warnings") or []),
        },
    }
    if ai.execution_pack.get("action") == "BROWSER_ACTION":
        for key in ("browser_run", "browser_workflow"):
            if key in outputs:
                folded["outputs"][key] = outputs[key]
    if "agent_run" in outputs:
        # The terminal-agent batch's receipt is what Omega judges to decide the next batch.
        folded["outputs"]["agent_run"] = outputs["agent_run"]
    if ai.execution_pack.get("action") == "WINDOW_REFLECTION" and "window_reflection" in outputs:
        folded["outputs"]["window_reflection"] = outputs["window_reflection"]
    if ai.execution_pack.get("action") == "EXECUTE_MOTOR_PROGRAM":
        action = effective_motor_action(ai.execution_pack)
        folded["effective_action"] = action
        folded["outputs"]["effective_action"] = action
        folded["telemetry"]["effective_action"] = action
    return folded


def _emit_fold_summary(folded: dict) -> None:
    try:
        from guardian.terminal_feedback import emit_data

        tele = folded.get("telemetry") or {}
        emit_data(
            "auto",
            "fold_episode_auto",
            episode_id=str(folded.get("episode_id") or ""),
            summary=f"status={folded.get('status')}",
            status=folded.get("status"),
            cycle_kind=tele.get("cycle_kind"),
            failure_kind=folded.get("failure_kind"),
            runtime_ms=tele.get("runtime_ms"),
        )
    except Exception:
        pass


def auto_outcome_from_fold(
    ai: AutoInput,
    folded: dict,
) -> AutoOutcomePack:
    """PHASE 6 — emit AutoOutcomePack (echo immutable fields)."""
    telemetry = {
        "runtime_ms": (folded.get("telemetry") or {}).get("runtime_ms"),
        "warnings": (folded.get("telemetry") or {}).get("warnings") or [],
    }
    if folded.get("action") == "EXECUTE_MOTOR_PROGRAM":
        telemetry["effective_action"] = (folded.get("telemetry") or {}).get("effective_action")
    auto_emit = {
        "status": folded.get("status"),
        "cycle_kind": folded.get("telemetry", {}).get("cycle_kind"),
        "memory_status": (folded.get("memory") or {}).get("memory_status"),
        "model_profile": folded.get("model_profile"),
        "outputs": dict(folded.get("outputs") or {}),
        "failure_kind": folded.get("failure_kind"),
        "failure_reason": folded.get("failure_reason"),
        "telemetry": telemetry,
    }
    pack = AutoOutcomePack(
        episode_id=ai.episode_id,
        source_action_goal=deepcopy(ai.source_action_goal),
        execution_pack=dict(ai.execution_pack),
        auto=auto_emit,
    )
    try:
        from guardian.terminal_feedback import emit_data

        cycle_kind = str(auto_emit.get("cycle_kind") or "")
        outcome_failed = (
            bool(auto_emit.get("failure_kind"))
            and cycle_kind
            not in (
                "partial",
                "skipped",
                "idle",
            )
            and auto_emit.get("status") not in ("partial", "skipped", "idle")
        )
        emit_data(
            "auto",
            "auto_outcome_pack",
            episode_id=ai.episode_id,
            summary=f"status={auto_emit.get('status')}",
            status="fail" if outcome_failed else auto_emit.get("status"),
            cycle_kind=cycle_kind,
            failure_kind=auto_emit.get("failure_kind"),
            failure_reason=auto_emit.get("failure_reason"),
            model_id=folded.get("model_id"),
            runtime_ms=auto_emit.get("telemetry", {}).get("runtime_ms"),
            ok=not outcome_failed,
        )
    except Exception:
        pass
    return pack


def run(
    ai: AutoInput,
    *,
    status: str,
    cycle_kind: str,
    memory_envelope: MemoryEnvelope,
    model_profile: Optional[str],
    model_id: Optional[str],
    prompt_pack: PromptPack,
    exec_result: Dict[str, Any],
    runtime_ms: int,
) -> AutoOutcomePack:
    folded = _fold_episode_auto(
        ai,
        status=status,
        cycle_kind=cycle_kind,
        memory_envelope=memory_envelope,
        model_profile=model_profile,
        model_id=model_id,
        prompt_pack=prompt_pack,
        exec_result=exec_result,
        runtime_ms=runtime_ms,
    )
    _emit_fold_summary(folded)
    return auto_outcome_from_fold(ai, folded)

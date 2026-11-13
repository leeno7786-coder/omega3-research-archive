"""Auto - consume a SkillExecutionPack and execute it through the brain seat.

AUTO never contacts an executor before the dispatch validates and the guard
passes. The default executor is the same runtime brain used by chat; the legacy
Qwen bridge is retired.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from schema.qwen_skill_dispatch import build_qwen_skill_dispatch

log = logging.getLogger("omega.auto_loop.skill")


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    reason: str = ""


def default_skill_guard(dispatch) -> GuardResult:
    """DEPRECATED minimal guard — superseded by the real skill_execution_guard (now the
    dispatch_skill_pack default). Retained only for the unit tests that exercise it directly.
    Enforces the sandbox invariant for self-modification + destructive-without-confirmation."""
    sc = dispatch.safety.get("safety_class")
    if sc == "self_modification":
        return GuardResult(False, "self_modification denied (agentic-execution sandbox invariant)")
    if dispatch.safety.get("destructive_allowed") and not dispatch.safety.get(
        "requires_confirmation"
    ):
        return GuardResult(False, "destructive execution without confirmation denied")
    return GuardResult(True)



def _build_skill_event_handler(dispatch, *, narrator=None):
    """Capture builder artifacts into episode SRAM as execution events arrive.

    ``narrator`` is retained as an ignored keyword for callers from the retired
    narration path; artifact intake is now the only event-side effect.
    """
    del narrator
    ep_id = str(getattr(dispatch, "episode_id", "") or "")
    objective = str(getattr(dispatch, "objective", "") or "")

    def _handle(evt: dict) -> None:
        if not isinstance(evt, dict):
            raise TypeError("builder event must be a mapping")
        data = evt.get("data")
        if str(evt.get("type") or "") == "ledger" and isinstance(data, dict):
            paths = [
                item.get("path") if isinstance(item, dict) else item
                for item in (data.get("touched_files") or ())
            ]
            if paths:
                from memory.dev_artifact_intake import hold_dev_artifact_snapshots

                hold_dev_artifact_snapshots(
                    episode_id=ep_id,
                    objective=objective,
                    paths=paths,
                )
    return _handle


def default_brain_skill_invoker(
    directive: str,
    dispatch,
    *,
    client=None,
    should_stop: Callable[[], bool] | None = None,
    jail_root: str | None = None,
    pipeline_id: int | None = None,
    work_kind: str | None = None,
    cycle_id: str | None = None,
    job_id: str | None = None,
    permission_mode: str | None = None,
    thread_id: str = "",
) -> dict:
    """Run the bounded dispatch on the same Mistral brain seat as chat.

    ``thread_id`` is the cognitive thread of the episode running the dispatch. A job the
    dispatch opens records it, so operator mail sent while it runs can be addressed to it
    (cross-lane T7).

    The existing skill selection, guard, checkpoint and normalized foldback stay
    authoritative. Only the execution transport changes: native tool calls are
    answered locally and appended to one chat-seat transcript.
    """
    from auto_loop.execute.brain_skill_executor import run_brain_skill_dispatch

    on_event = _build_skill_event_handler(dispatch)
    result = run_brain_skill_dispatch(
        directive,
        dispatch,
        client=client,
        workspace_root=jail_root if jail_root is not None else operator_workspace_root(),
        on_event=on_event,
        should_stop=should_stop,
        pipeline_id=pipeline_id,
        work_kind=work_kind,
        cycle_id=cycle_id,
        job_id=job_id,
        permission_mode=permission_mode,
        thread_id=thread_id,
    )
    ledger = result.get("ledger") if isinstance(result, dict) else None
    if isinstance(ledger, dict):
        on_event({"type": "ledger", "data": ledger})
    return result


def operator_workspace_root() -> Optional[str]:
    """Return the active trusted folder for operator skill execution.

    The environment override is kept for isolated test/deployment harnesses;
    normal installs use the persisted operator grant and fail closed until one
    is selected with ``/cd``.
    """
    import os

    ws = os.environ.get("OMEGA_OPERATOR_WORKSPACE")
    if ws and ws.strip():
        return ws.strip()
    try:
        from core.trusted_folders import active_trusted_folder

        active = active_trusted_folder()
    except Exception:
        active = None
    return str(active) if active else None


def _autonomous_skill_exec_enabled() -> bool:
    """Whether the mind may execute skills AUTONOMOUSLY — without a fresh operator token —
    bounded by the jail.

    Operator doctrine 2026-06-25: "let the mind execute in the jail." The jail is the safety
    boundary, not a per-task confirmation: a confirmed dispatch still passes through the
    workspace jail (root disjoint from OMEGA_ROOT — Omega's own source stays invisible), the
    self-mod denylist, the per-class command policy, and the forbidden-tool set. confirmed=True
    here ONLY satisfies the guard's confirmation gate; it never widens what may be written.

    Flag OMEGA_AUTONOMOUS_SKILL_EXEC (default OFF). Enable it explicitly only
    after the deployment has accepted autonomous workspace mutation."""
    import os

    return os.environ.get("OMEGA_AUTONOMOUS_SKILL_EXEC", "off").strip().lower() not in (
        "0",
        "false",
        "off",
        "no",
        "",
    )


def skill_dispatch_confirmed(source_action_goal: dict | None) -> bool:
    """Whether bounded write/command work is authorized without a fresh operator token.

    Operator-initiated episodes confirm immediately. Self-driven skill continuations
    (execution_continuation with a prior_episode chain) inherit the same bounded authorization
    so multi-cycle inspect->patch->test can proceed without re-prompting. With autonomous
    execution enabled (the live default), ANY self-driven episode self-authorizes — the mind
    executes in the jail (see _autonomous_skill_exec_enabled)."""
    sag = dict(source_action_goal or {})
    opened_by = str(sag.get("opened_by") or "")
    if opened_by.startswith("operator"):
        return True
    if opened_by == "execution_continuation" and str(sag.get("prior_episode") or "").strip():
        return True
    if _autonomous_skill_exec_enabled():
        return True
    return False


def default_command_policy(command: Any) -> bool:
    """Reject shell composition and redirection before command_safe execution."""
    import re

    text = str(command or "")
    if not text.strip():
        return False
    return re.search(r"(?:&&|\|\||[|;&<>`]|\$\(|\$\{|%[^%]+%)", text) is None


def _find_skill(pack, catalog: Optional[list]):
    if catalog is None:
        from runtime.execution_skills import load_execution_skills

        catalog, _ = load_execution_skills()
    sid = pack.selected_skill.get("skill_id")
    for s in catalog:
        if s.skill_id == sid:
            return s
    return None


def _emit_skill_dispatch(
    *,
    status: str,
    dispatch_id: str,
    reason: str = "",
    dispatch=None,
    guard_status=None,
    qwen_result=None,
) -> None:
    try:
        from guardian.terminal_feedback import emit_data

        skill_id = ""
        skill_name = ""
        if dispatch is not None:
            sel = dict(getattr(dispatch, "selected_skill", None) or {})
            skill_id = str(sel.get("skill_id") or "")
            skill_name = str(sel.get("skill_name") or "")
        emit_data(
            "auto",
            "skill_dispatch",
            summary=f"status={status} skill={skill_name or skill_id or 'unknown'}",
            status=status,
            dispatch_id=dispatch_id,
            reason=reason,
            skill_id=skill_id,
            skill_name=skill_name,
            guard_allowed=bool((guard_status or {}).get("allowed")),
            qwen_status=str((qwen_result or {}).get("status") or ""),
        )
    except Exception:
        pass


def _outcome(
    status: str,
    dispatch_id: str,
    reason: str = "",
    *,
    dispatch=None,
    qwen_raw=None,
    qwen_result=None,
    directive=None,
    guard_status=None,
    mind_checkpoint_id=None,
) -> dict:
    _emit_skill_dispatch(
        status=status,
        dispatch_id=dispatch_id,
        reason=reason,
        dispatch=dispatch,
        guard_status=guard_status,
        qwen_result=qwen_result,
    )
    return {
        "status": status,  # rejected | blocked | requires_confirmation | executed | invoker_error
        "dispatch_id": dispatch_id,
        "reason": reason,
        "dispatch": dispatch.to_dict() if dispatch is not None else None,
        "directive": directive,
        "guard_status": guard_status,
        "qwen_raw": qwen_raw,
        "qwen_result": qwen_result,  # normalized QwenSkillExecutionResult (Phase 6J)
        "mind_checkpoint_id": mind_checkpoint_id,  # set when a reversibility checkpoint was taken
    }


def dispatch_skill_pack(
    pack,
    *,
    catalog: Optional[list] = None,
    guard: Callable = None,
    invoker: Callable = None,
    confirmed: bool = False,
    command_policy: Callable = None,
    workspace_root: Optional[str] = None,
    allow_self_modification: bool = False,
    checkpoint_fn: Callable = None,
    restore_fn: Callable = None,
    motor_program: dict | None = None,
    pipeline_id: int | None = None,
    work_kind: str | None = None,
    cycle_id: str | None = None,
    job_id: str | None = None,
    permission_mode: str | None = None,
    thread_id: str = "",
) -> dict:
    """Run a SkillExecutionPack through AUTO and the resident brain. The default guard is the real general
    skill_execution_guard (6I) — safety class, forbidden-tool, confirmation, command policy,
    workspace jail, limits — fed the policy dependencies (confirmed / command_policy /
    workspace_root / allow_self_modification). An injected `guard` overrides it (tests/custom).
    executor is invoked ONLY after the dispatch validates AND the guard returns allowed."""
    perrs = pack.validate()
    if perrs:
        return _outcome("rejected", pack.dispatch_id, "; ".join(perrs))

    skill = _find_skill(pack, catalog)
    if skill is None:
        return _outcome("rejected", pack.dispatch_id, "selected skill not found")
    if not skill.selectable:
        return _outcome("rejected", pack.dispatch_id, "selected skill is disabled")
    # the skill must still MATCH the pack (a stale pack referencing a renamed skill is rejected)
    if skill.name != pack.selected_skill.get(
        "skill_name"
    ) or skill.skill_id != pack.selected_skill.get("skill_id"):
        return _outcome("rejected", pack.dispatch_id, "selected skill does not match pack")
    motor_contract = None
    if motor_program is not None:
        from schema.qwen_skill_dispatch import motor_authority

        try:
            motor_contract = motor_authority(motor_program)
        except (TypeError, ValueError) as exc:
            return _outcome(
                "rejected",
                pack.dispatch_id,
                f"invalid motor program authority: {exc}",
            )
    if (
        pack.selected_skill.get("skill_class") != skill.skill_class
        or pack.selected_skill.get("source_path") != skill.source_path
        or pack.selected_skill.get("execution_kind") not in skill.execution_kinds
        or tuple(pack.allowed_tools) != tuple(skill.allowed_tools)
        or not set(skill.forbidden_tools).issubset(set(pack.forbidden_tools))
        or set(skill.allowed_tools).intersection(skill.forbidden_tools)
        or dict(pack.limits) != dict(skill.limits)
        or dict(pack.expected_output_schema) != dict(skill.output_schema)
        or (
            motor_contract is None
            and (
                tuple(pack.evidence_required) != tuple(skill.evidence_required)
                or tuple(pack.completion_criteria) != tuple(skill.completion_criteria)
            )
        )
        or (
            motor_contract is not None
            and (
                pack.objective != motor_contract[0]
                or tuple(pack.evidence_required) != tuple(motor_contract[1])
                or tuple(pack.completion_criteria) != tuple(motor_contract[2])
            )
        )
    ):
        return _outcome(
            "rejected",
            pack.dispatch_id,
            "skill execution pack does not match the current skill contract",
        )
    from schema.skill_execution_pack import safety_envelope

    expected_safety = safety_envelope(
        skill.safety_class,
        requires_confirmation_for=tuple(skill.requires_confirmation_for),
    )
    if dict(pack.safety) != expected_safety:
        return _outcome(
            "rejected",
            pack.dispatch_id,
            "skill execution pack safety contract is stale or tampered",
        )

    dispatch = build_qwen_skill_dispatch(pack, skill, motor_program=motor_program)
    derrs = dispatch.validate()
    if derrs:
        return _outcome("rejected", pack.dispatch_id, "; ".join(derrs), dispatch=dispatch)

    if permission_mode is not None:
        from auto_loop.execute.chat_permissions import skill_permission_denial

        mode_denial = skill_permission_denial(
            permission_mode,
            str(dispatch.safety.get("safety_class") or ""),
        )
        if mode_denial:
            return _outcome(
                "blocked",
                pack.dispatch_id,
                mode_denial,
                dispatch=dispatch,
                guard_status={
                    "allowed": False,
                    "status": "blocked",
                    "reason": mode_denial,
                    "permission_mode": permission_mode,
                },
            )

    # GUARD before any qwen contact — the real general guard by default; an injected guard
    # (tests/custom) overrides. The guard's structured status rides on every outcome.
    if guard is None:
        from auto_loop.execute.skill_execution_guards import skill_execution_guard

        def guard(d):
            return skill_execution_guard(
                d,
                confirmed=confirmed,
                command_policy=command_policy,
                workspace_root=workspace_root,
                allow_self_modification=allow_self_modification,
            )

    g = guard(dispatch)
    guard_status = (
        g.to_dict() if hasattr(g, "to_dict") else {"allowed": g.allowed, "reason": g.reason}
    )
    if not g.allowed:
        status = getattr(g, "status", "blocked") or "blocked"  # "blocked" | "requires_confirmation"
        return _outcome(
            status, pack.dispatch_id, g.reason, dispatch=dispatch, guard_status=guard_status
        )

    # REVERSIBILITY (High #3): a write/destructive dispatch (guard.checkpoint_required) is wrapped
    # in a mind-state checkpoint so a FAILED run can be rolled back wholesale. Fail CLOSED — if the
    # checkpoint cannot be taken, BLOCK rather than run unprotected. Injected seams (tests) bypass the
    # live flag; the live checkpoint/restore is flag-gated until the skill path goes live.
    checkpoint_id = None
    if getattr(g, "checkpoint_required", False):
        if checkpoint_fn is None and restore_fn is None:
            from guardian.mind_checkpoint import checkpoint_mind_state, restore_mind_state

            checkpoint_fn, restore_fn = checkpoint_mind_state, restore_mind_state
        if checkpoint_fn is None or restore_fn is None:
            return _outcome(
                "blocked",
                pack.dispatch_id,
                "mind checkpoint/restore is unavailable (fail-closed)",
                dispatch=dispatch,
                guard_status=guard_status,
            )
        try:
            cp = checkpoint_fn(label=f"skill:{dispatch.dispatch_id}")
            checkpoint_id = getattr(cp, "checkpoint_id", None)
            if not checkpoint_id:
                raise RuntimeError("checkpoint did not return an id")
        except Exception as exc:  # noqa: BLE001 — no reversibility ⇒ do not run
            return _outcome(
                "blocked",
                pack.dispatch_id,
                f"mind checkpoint failed (fail-closed): {exc}",
                dispatch=dispatch,
                guard_status=guard_status,
            )

    from organ.promptorgan import PromptContext, phrase

    directive = phrase(
        PromptContext(
            kind="agent",
            question=dispatch.objective,
            data=dispatch,
        )
    ).system
    try:
        if invoker is None:
            qwen_raw = default_brain_skill_invoker(
                directive,
                dispatch,
                jail_root=workspace_root,
                pipeline_id=pipeline_id,
                work_kind=work_kind,
                cycle_id=cycle_id,
                job_id=job_id,
                permission_mode=permission_mode,
                thread_id=thread_id,
            )
        else:
            qwen_raw = invoker(directive, dispatch)
    except Exception as exc:  # noqa: BLE001 — invoker failure is an outcome, not a crash
        if checkpoint_id is not None and restore_fn is not None:
            try:
                restore_fn(checkpoint_id)
            except Exception:  # noqa: BLE001 — restore best-effort; the failure is already terminal
                log.warning("mind restore after invoker_error failed", exc_info=True)
        return _outcome(
            "invoker_error",
            pack.dispatch_id,
            str(exc),
            dispatch=dispatch,
            directive=directive,
            guard_status=guard_status,
            mind_checkpoint_id=checkpoint_id,
        )

    # normalize the raw bridge result into the QwenSkillExecutionResult contract (6J)
    from schema.qwen_skill_result import normalize_qwen_result

    result = normalize_qwen_result(qwen_raw, dispatch=dispatch, guard_status=guard_status)
    # roll the mind back on ANY non-success outcome — fail-closed reversibility for a write dispatch
    if checkpoint_id is not None and restore_fn is not None and result.status != "success":
        try:
            restore_fn(checkpoint_id)
        except Exception:  # noqa: BLE001
            log.warning("mind restore after %s failed", result.status, exc_info=True)
    return _outcome(
        "executed",
        pack.dispatch_id,
        "",
        dispatch=dispatch,
        qwen_raw=qwen_raw,
        qwen_result=result.to_dict(),
        directive=directive,
        guard_status=guard_status,
        mind_checkpoint_id=checkpoint_id,
    )

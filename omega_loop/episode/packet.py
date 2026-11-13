"""Deterministic OmegaEpisodeInput assembly (Global B packet builder)."""

from __future__ import annotations

from typing import Any, Mapping

from auto_loop.fold.run import plugin_execution_envelope, screen_plugin_outputs
from deepsleep_loop.mastery_store import load_mastery, normalize_domain_id
from global_loop.assemble.run import GlobalBInput, auto_inner
from omega_loop.episode.types import MasteryView, OmegaEpisodeInput, bounded_browser_evidence, bounded_browser_run

_RESPONSE_ACTIONS = frozenset({"RESPONSE", "PROACTIVE_RESPONSE", "ASK_OPERATOR"})
_SYSTEM_EVENT_CONTRIBUTORS = frozenset({"dev_rollback"})
_OPERATOR_TEXT_CONTRIBUTORS = (
    "operator_query",
    "operator_command",
    "operator_response",
    "operator_message",
)
_ASK_TEXT_CONTRIBUTORS = (
    "clarification_gap",
    "operator_priority",
    "requires_operator_input",
)


def _preferred_text_ids_for_action(
    action: str,
    pressure: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    if action != "ASK_OPERATOR":
        return _OPERATOR_TEXT_CONTRIBUTORS
    reason = ""
    if isinstance(pressure, Mapping):
        reason = str(pressure.get("reason") or pressure.get("opened_by") or "").strip()
    if reason in _ASK_TEXT_CONTRIBUTORS:
        return (reason,) + tuple(i for i in _ASK_TEXT_CONTRIBUTORS if i != reason)
    return _ASK_TEXT_CONTRIBUTORS


def _recall_tuple(raw: Any) -> tuple[dict, ...]:
    if not raw:
        return ()
    if isinstance(raw, tuple):
        items = raw
    elif isinstance(raw, list):
        items = raw
    else:
        return ()
    return tuple(dict(item) for item in items if isinstance(item, Mapping))


def _recall_error(auto_inner_dict: Mapping[str, Any]) -> str | None:
    mem = auto_inner_dict.get("memory")
    if isinstance(mem, Mapping):
        err = mem.get("error")
        if err:
            return str(err)
    fk = auto_inner_dict.get("failure_kind")
    if fk == "memory_continuity_substrate_invalid":
        reason = auto_inner_dict.get("failure_reason")
        if reason:
            return str(reason)
    return None


def _row_text(row: Mapping[str, Any]) -> str:
    meta = dict(row.get("metadata") or row.get("detail") or {})
    return str(meta.get("question") or meta.get("message") or "").strip()


def _row_id(row: Mapping[str, Any]) -> str:
    return str(row.get("id") or row.get("type") or "").strip()


def _seed_from_pressure(
    pressure: Mapping[str, Any] | None,
    *,
    preferred_ids: tuple[str, ...] = (),
) -> str | None:
    if not isinstance(pressure, Mapping):
        return None
    rows = [row for row in pressure.get("contributors") or [] if isinstance(row, Mapping)]
    for wanted in preferred_ids:
        for row in rows:
            if _row_id(row) != wanted:
                continue
            candidate = _row_text(row)
            if candidate:
                return candidate
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if _row_id(row) in _SYSTEM_EVENT_CONTRIBUTORS:
            continue
        candidate = _row_text(row)
        if candidate:
            return candidate
    return None


def onboarding_context_from_pressure(
    pressure: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the bounded onboarding context carried by its bootstrap contributor."""
    if not isinstance(pressure, Mapping):
        return None
    for row in pressure.get("contributors") or ():
        if not isinstance(row, Mapping):
            continue
        detail = row.get("detail") or row.get("metadata") or {}
        if not isinstance(detail, Mapping):
            continue
        if str(detail.get("origin") or "") != "onboarding_bootstrap":
            continue
        raw = detail.get("onboarding_context")
        if not isinstance(raw, Mapping):
            continue
        categories: list[dict[str, Any]] = []
        for item in list(raw.get("categories") or ())[:16]:
            if not isinstance(item, Mapping):
                continue
            title = str(item.get("title") or item.get("subcategory") or "").strip()[:160]
            try:
                lobe = int(item.get("lobe"))
                y = int(item.get("y"))
            except (TypeError, ValueError):
                continue
            if not title or lobe not in (2, 8) or y < 0:
                continue
            normalized = dict(item)
            normalized["title"] = title
            normalized["lobe"] = lobe
            normalized["y"] = y
            normalized.pop("subcategory", None)
            categories.append(normalized)
        if not categories:
            return None
        context = dict(raw)
        context["occupation"] = str(raw.get("occupation") or "").strip()[:160]
        if raw.get("operator_id"):
            context["operator_id"] = str(raw.get("operator_id") or "").strip()[:80]
        context["categories"] = categories
        if isinstance(raw.get("source_citation"), Mapping):
            context["source_citation"] = dict(raw["source_citation"])
        else:
            context["source_citation"] = {}
        return context
    return None


def _operator_text(
    execution_pack: Mapping[str, Any],
    action: str,
    *,
    pressure: Mapping[str, Any] | None = None,
) -> str | None:
    if action not in _RESPONSE_ACTIONS:
        return None
    inputs = dict(execution_pack.get("execution_inputs") or {})
    # SYSTEM-QUERY EXCHANGE (operator 2026-07-11): when the operator is answering a
    # question Omega itself asked, the fold must see EVERY turn of that exchange —
    # not the bare answer. Without the question, the mind reasons over the answer
    # with amnesia about what it asked ("I don't recall what specific question I
    # asked you" — live confabulation seam). The contributor carries the question
    # verbatim (system_query_text); render the whole thread as operator_text.
    for row in (pressure or {}).get("contributors") or []:
        if not isinstance(row, dict):
            continue
        detail = dict(row.get("detail") or row.get("metadata") or {})
        held = detail.get("held_instruction")
        if isinstance(held, Mapping) and str(held.get("job_id") or ""):
            # Words that outlived their build (cross-lane T7): the fold reasons over them
            # beside the host's record of how that build settled.
            words = str(detail.get("message") or detail.get("text") or "").strip()
            if words:
                from global_loop.intent.context import held_instruction_context

                return f"{held_instruction_context(held)[1]}\nOperator: {words}"
        question = str(detail.get("system_query_text") or "").strip()
        if question:
            answer = str(detail.get("message") or detail.get("text") or "").strip()
            if answer:
                return f"You (Omega) asked the operator: {question}\nOperator answered: {answer}"
    # RESOLVE_QUERY (answering an operator reply) carries the operator's message
    # nested under resolution_context — surface it so Omega sees the answer.
    rc = inputs.get("resolution_context")
    if isinstance(rc, Mapping):
        msg = rc.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
    # The operator's WHOLE command rides the pressure contributor VERBATIM. Prefer it
    # so an ACK fold-back reasons over the complete request and sets pressure on all of
    # it — a derived/partial execution_input must not shadow the full command. Falls
    # back to the execution inputs / memory query only when no contributor message is
    # present (e.g. a system-opened RESPOND episode).
    preferred = _preferred_text_ids_for_action(action, pressure)
    seed = _seed_from_pressure(pressure, preferred_ids=preferred)
    if seed:
        return seed
    for key in ("query", "question", "message", "operator_message", "text"):
        val = inputs.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    mq = execution_pack.get("memory_query")
    if isinstance(mq, str) and mq.strip():
        return mq.strip()
    return None


def _prior_mastery_for_domain(domain: str | None) -> MasteryView | None:
    if not domain or not str(domain).strip():
        return None
    domain_id = normalize_domain_id(domain)
    if not domain_id:
        return None
    store = load_mastery()
    record = store.get(domain_id)
    if record is None:
        return None
    return MasteryView(
        domain=str(record.domain_name or domain_id),
        knowledge_mastery=float(record.knowledge_mastery),
        application_mastery=float(record.application_mastery),
        target_mastery=float(record.target_mastery),
    )


def _execution_experience_view(cap: int = 6) -> tuple[dict, ...]:
    """The mind's DOING track record for metacognition — per-domain
    {domain, attempts, successes, last_outcome} from the execution ledger,
    so the fold reasons over how its builds have fared."""
    from memory.execution_experience import experience_summary

    return tuple(experience_summary(cap_domains=cap))


def build_learning_map(cap_topics: int | None = None) -> dict:
    """The per-episode DELTA substrate Omega diffs against to self-direct.

    For every domain in the mastery tree with an OPEN gap (knowledge OR application below
    the operational floor), surface BOTH deltas vs target — ``knowledge_delta`` ("learned")
    and ``application_delta`` ("execution") — plus whether the topic is still EMPTY (a
    subtopic Omega created but has not touched), least-known first, with a rollup count.

    This is DATA, not direction: we hand Omega the diff (+ its tools) and let IT decide what
    to do — learn, perceive its environment, build, ask the operator, or idle. No menu, no
    forced advance (see feedback_minimal_scaffolding_omega_self_directs)."""
    from pressure_loop.pressure_field import (
        OPERATIONAL_MASTERY,
        OPERATIONAL_PRACTICE_BAND,
    )

    recs = load_mastery()

    topics: list[dict] = []
    mastered = 0
    for rec in recs.values():
        k = float(rec.knowledge_mastery)
        a = float(rec.application_mastery)
        t = float(rec.target_mastery)
        floor = OPERATIONAL_MASTERY if OPERATIONAL_PRACTICE_BAND else t
        if k >= floor and a >= floor:  # both closed -> mastered, not an open gap
            mastered += 1
            continue
        topics.append(
            {
                "domain": str(rec.domain_name or rec.domain_id),
                "parent": str(rec.parent or ""),
                "knowledge": round(k, 2),
                "application": round(a, 2),
                "target": round(t, 2),
                "knowledge_delta": round(max(0.0, t - k), 2),  # how much is still un-LEARNED
                "application_delta": round(max(0.0, t - a), 2),  # how much is still un-EXECUTED
                "empty": (k == 0.0 and a == 0.0),  # untouched topic it created
                "status": str(rec.status or ""),
            }
        )

    topics.sort(key=lambda r: (r["knowledge"], r["application"]))  # least-known first
    if cap_topics is not None and cap_topics >= 0:
        topics = topics[:cap_topics]
    return {
        "topics": topics,
        "total": len(topics),
        "learned_count": sum(1 for r in topics if not r["empty"]),
        "empty_count": sum(1 for r in topics if r["empty"]),
        "mastered_count": mastered,
    }


def _foldback_num(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _mastery_delta_block(domain) -> dict:
    """mastery_delta {domain, knowledge_before/after, application_before/after, remaining_debt}
    from the mastery store + its recent per-episode deltas."""
    d = str(domain or "").strip()
    pm = _prior_mastery_for_domain(d) if d else None
    if pm is None:
        return {
            "domain": d,
            "knowledge_before": 0.0,
            "knowledge_after": 0.0,
            "application_before": 0.0,
            "application_after": 0.0,
            "remaining_debt": 0.0,
        }
    k = float(pm.knowledge_mastery)
    a = float(pm.application_mastery)
    t = float(pm.target_mastery)
    dk = da = 0.0
    from deepsleep_loop.mastery_store import load_mastery, normalize_domain_id

    rec = load_mastery().get(normalize_domain_id(d))
    if rec is not None:
        rkd = list(getattr(rec, "recent_knowledge_deltas", ()) or ())
        rad = list(getattr(rec, "recent_application_deltas", ()) or ())
        dk = float(rkd[-1]) if rkd else 0.0
        da = float(rad[-1]) if rad else 0.0
    return {
        "domain": d,
        "knowledge_before": round(k - dk, 3),
        "knowledge_after": round(k, 3),
        "application_before": round(a - da, 3),
        "application_after": round(a, 3),
        "remaining_debt": round(max(0.0, t - k), 3),
    }


def _trajectory_influencer_block() -> dict:
    """The window's self-authored trajectory, surfaced as a per-episode INFLUENCER (guides
    choices via the prompt — NOT via pressure, 2026-06-30 decouple)."""
    from pressure_loop.temporal_store import read_trajectory

    traj = read_trajectory() or {}
    if not isinstance(traj, dict) or not traj:
        return {}
    return {
        "active_domain": str(traj.get("target_domain") or ""),
        "active_goal": str(traj.get("target_capability") or traj.get("intent") or ""),
        "last_decision": str(traj.get("decision") or traj.get("intent") or ""),
        "confidence": traj.get("confidence"),
        "reason": str(traj.get("rationale") or "")[:300],
    }


def _conduct_view(influencer: Mapping) -> dict:
    """The trajectory joined with where its domain sits on the mastery arc: the data behind
    Omega's CONDUCT guidance (operator 2026-09-21 — the window decides the trajectory, and that
    decision guides which of the four lanes each episode chooses).

    ``{}`` when no trajectory is active: there is nothing to guide by. A trajectory whose domain
    has no mastery record is phase ``new`` — never touched, not learned to zero. The phase comes
    from ``mastery_phase``, the same reading the LEARN topic cascade's stopping rule acts on.
    Keys avoid the legacy "goal"/"goal_state"/"target" names the v2 outcome-shape guard forbids
    under ``episode.global_``.
    """
    domain = str((influencer or {}).get("active_domain") or "").strip()
    if not domain:
        return {}
    from cognition.topic_resolution.resolve_topic import mastery_phase

    view = mastery_phase(domain) or {"phase": "new"}
    return {
        "domain": domain,
        "capability": str(influencer.get("active_goal") or ""),
        "reason": str(influencer.get("reason") or ""),
        **view,
    }


def _assemble_foldback_packet(gbi, exec_block, outputs, applied, domain) -> dict:
    """The per-episode autonomy foldback packet — the DATA the mind reasons over to self-direct
    (pressure_surface · episode_foldback meta-b/global-b · mastery_delta · trajectory_influencer
    · conduct).
    INFORMATIONAL, never directive. Source failures propagate to episode capture."""
    contributors: dict = {}
    for row in (getattr(gbi, "pressure", None) or {}).get("contributors") or []:
        if isinstance(row, Mapping):
            ctype = str(row.get("type") or row.get("id") or "")
            w = row.get("weight")
            if ctype and _foldback_num(w):
                contributors[ctype] = float(w)
    from pressure_loop.pressure_surface import build_pressure_surface

    pressure_surface = build_pressure_surface(contributors, domain=str(domain or ""))
    outputs = screen_plugin_outputs(
        dict(outputs or {}),
        preserve_receipts=True,
    )
    eb = dict(exec_block or {})
    src_goal = dict(getattr(gbi, "source_action_goal", None) or {})
    app = dict(applied or {})
    trace_summary = dict(dict(getattr(gbi, "meta_reflection", None) or {}).get("trace_summary") or {})
    global_outcome = dict(dict(getattr(gbi, "global_outcome", None) or {}).get("outcome") or {})
    plugin_evidence = {
        key: outputs.get(key)
        for key in (
            "plugin_plan",
            "plugin_step_results",
            "plugin_permission_request",
            "plugin_result",
            "plugin_failure",
        )
        if outputs.get(key) is not None
    }
    trajectory_influencer = _trajectory_influencer_block()
    return {
        "pressure_surface": pressure_surface,
        "episode_foldback": {
            "meta_b": {
                "trace_summary": {
                    key: bool(trace_summary.get(key))
                    for key in ("tool_observation_present", "pre_perception_present", "post_observation_present")
                },
                "outcome": str(eb.get("result") or "none"),
                "tool_or_skill_used": str(eb.get("tool") or eb.get("action") or ""),
                "validation": str(eb.get("disposition") or ""),
                "failure_class": str(eb.get("failure_reason"))[:1000]
                if eb.get("failure_reason")
                else None,
            },
            "global_b": {
                "omega_lane": getattr(gbi, "omega_lane", None) or "chat",
                "status": str(global_outcome.get("status") or eb.get("status") or "")[:100],
                # key is goal_INTENT (not goal_state): the v2 outcome-shape guard forbids the
                # legacy "goal"/"goal_state"/"target" keys anywhere in episode.global_, and this
                # packet rides under global_["omega_packet"].
                "goal_intent": str(src_goal.get("intent") or ""),
                "summary": str(global_outcome.get("summary") or app.get("summary") or app.get("digest") or "")[:1000],
                "artifacts": list((outputs or {}).keys())[:8],
                "followups": str(src_goal.get("reason") or "")[:1000],
            },
        },
        "plugin_execution": plugin_execution_envelope(outputs),
        "plugin_evidence": plugin_evidence,
        "mastery_delta": _mastery_delta_block(domain),
        "trajectory_influencer": trajectory_influencer,
        "conduct": _conduct_view(trajectory_influencer),
    }


def build_omega_episode_input(
    gbi: GlobalBInput,
    meta_reflection: Mapping[str, Any],
    *,
    prior_builds: tuple = (),
    build_in_progress: tuple = (),
    rollback_events: tuple = (),
    motor_program: Mapping[str, Any] | None = None,
) -> OmegaEpisodeInput:
    """Assemble the per-episode Omega packet from fold artifacts (no model call)."""
    auto_inner_dict = auto_inner(gbi.auto_outcome)
    raw_outputs = dict(auto_inner_dict.get("outputs") or {})
    outputs = screen_plugin_outputs(
        raw_outputs,
        preserve_receipts=True,
    )
    browser_lane = gbi.omega_lane == "browser"
    if browser_lane:
        # Browser raw responses may contain images, DOM, and credentials. Only
        # the bounded post-action receipt belongs in this persistent packet.
        outputs = {key: (bounded_browser_run(value) if key == "browser_run" else bounded_browser_evidence(value))
                   for key, value in outputs.items() if key in {"browser_run", "tool_result"}}
    exec_block = dict(meta_reflection.get("execution") or {})
    execution_pack = dict(gbi.execution_pack or {})
    execution_inputs = dict(execution_pack.get("execution_inputs") or {})
    transported_program = execution_inputs.get("motor_program")
    if isinstance(transported_program, Mapping) and transported_program.get("shadow") is True:
        transported_program = None
    if isinstance(motor_program, Mapping) and motor_program.get("shadow") is True:
        motor_program = None
    if motor_program is not None and transported_program is not None:
        from schema.motor_program import MotorProgram

        attached = MotorProgram.from_dict(motor_program).to_dict()
        transported = MotorProgram.from_dict(transported_program).to_dict()
        if attached != transported:
            raise ValueError(
                "conflicting motor program representations in episode and execution inputs"
            )
    effective_motor_program = motor_program if motor_program is not None else transported_program
    intent_decision = dict(gbi.source_action_goal or {})
    intent_name = str(intent_decision.get("intent") or "")
    raw_neuromod = auto_inner_dict.get("neuromod")
    if not isinstance(raw_neuromod, Mapping):
        raw_neuromod = (gbi.pressure or {}).get("neuromod_state")
    neuromod_state = dict(raw_neuromod) if isinstance(raw_neuromod, Mapping) else {}

    action = str(execution_pack.get("action") or exec_block.get("action") or "")
    reflection_action = action
    if action == "EXECUTE_MOTOR_PROGRAM":
        reflection_action = str(execution_inputs.get("motor_transport") or "")
    domain_raw = str(execution_inputs.get("domain") or "")
    domain = domain_raw.strip() or None

    mb_result = str(exec_block.get("result") or "none")
    outcome = mb_result if mb_result in ("success", "failure", "partial") else "none"
    failure_reason = exec_block.get("failure_reason")
    if outcome != "failure":
        failure_reason = None
    elif failure_reason is not None:
        failure_reason = str(failure_reason)

    raw_tool = outputs.get("plugin_result")
    if raw_tool is None:
        raw_tool = outputs.get("tool_result")
    raw_tool_result = dict(raw_tool) if isinstance(raw_tool, Mapping) else raw_tool

    applied = None
    current_wip = None  # the CURRENT yield's mid-flight state, folded into build_in_progress below
    if reflection_action == "APPLY":
        apply_result = outputs.get("apply_result")
        apply_result = dict(apply_result) if isinstance(apply_result, Mapping) else {}
        ledger = apply_result.get("ledger")
        touched = ledger.get("touched_files") if isinstance(ledger, Mapping) else None
        files_touched = len(touched) if isinstance(touched, (list, tuple)) else 0
        applied = {
            "status": str(outputs.get("apply_status") or "none"),
            "summary": str(apply_result.get("summary") or ""),
            # The bridge reports work via its keep/rollback ledger (touched_files);
            # a raw git_diff is optional. Either signals real change.
            "has_diff": bool(apply_result.get("git_diff")) or files_touched > 0,
            "files_touched": files_touched,
            "error": outputs.get("apply_error"),
        }
    elif reflection_action == "USE_SKILL":
        # Phase 6: a skill-mediated qwen execution is applied work — it GROUNDS application_mastery
        # exactly like APPLY did (the skill path retires APPLY). Build the same `applied` shape from
        # the normalized skill result (files surface via the bridge ledger -> files_changed, 6P).
        qr = outputs.get("skill_result")
        qr = dict(qr) if isinstance(qr, Mapping) else {}
        files = qr.get("files_changed") or ()
        files = list(files) if isinstance(files, (list, tuple)) else []
        files_touched = len(files)
        digest = str(qr.get("progress_digest") or "").strip()
        qsummary = str(qr.get("summary") or "").strip()
        applied = {
            "status": str(outputs.get("skill_status") or qr.get("status") or "none"),
            # qwen's own summary is frequently empty/whitespace; fall back to the brain-derived
            # digest so Omega's cognition sees WHAT was built — file names + tool trail — not a
            # blank (operator 2026-06-27: the foldback reached Omega gutted, just status + count).
            "summary": qsummary or digest,
            "has_diff": files_touched > 0 or bool(qr.get("diff_summary")),
            "files_touched": files_touched,
            "files": [str(f).replace("\\", "/").rsplit("/", 1)[-1] for f in files[:12]],
            "digest": digest,
            "error": qr.get("failure_class"),
        }
        # FOLD-BACK: a skill that YIELDED / is PARTIAL hit its turn budget with work owed. Surface
        # the CURRENT build's mid-flight state as build_in_progress so THIS episode's Omega inference
        # reflects on the real progress — objective, digest, files, tests, errors — and decides how
        # to proceed via the fold-back prompt pack (continue / enhance / ask / stop / done). Without
        # this the packet only carried PRE-build recall, so a fresh yield showed "(none)" and the
        # mind defaulted to a failure read (operator live finding 2026-07-01).
        _wip_status = str(applied.get("status") or "").lower()
        if _wip_status in ("yielded", "partial"):
            _obj = str(
                (execution_pack.get("execution_inputs") or {}).get("objective")
                or outputs.get("skill_objective")
                or ""
            )
            _errs = [str(b) for b in (qr.get("unresolved_blockers") or ()) if str(b).strip()]
            if not _errs and qr.get("failure_class"):
                _errs = [str(qr.get("failure_class"))]
            # ACTUAL TEST LOGS: run the build's tests against what's on disk so the mind sees real
            # pass/fail + failing-test detail (the bridge emits only the tool name, not the output).
            # With this it can decide RETRY_WITH_CORRECTION instead of asking the operator blind.
            from omega_loop.episode.workspace_tests import collect_test_report

            _test_report = collect_test_report(files) or {}
            _test_results = (
                [_test_report.get("summary", "")] + list(_test_report.get("failures") or ())
                if _test_report.get("ran")
                else [str(t) for t in (qr.get("tests_run") or ())][:20]
            )
            if _test_report.get("failures"):
                _errs = (_errs + [str(x) for x in _test_report["failures"]])[:12]
            current_wip = {
                "objective": _obj,
                "progress_digest": digest,
                "current_state": qsummary or digest,
                "files_changed": [str(f) for f in files[:20]],
                "test_results": [str(t) for t in _test_results][:20],
                "test_report": _test_report,
                "errors": _errs[:12],
                "iterations": int(qr.get("rounds_used") or 0),
                "rounds_used": int(qr.get("rounds_used") or 0),
                "open_questions": [str(b) for b in (qr.get("unresolved_blockers") or ())][:10],
                "confidence": qr.get("confidence"),
            }
            # Run the battery here so the initial Global B packet is useful. The complete
            # execution report replaces this provisional row before Omega inference.
            if "verification" not in current_wip:
                from core.build_verification import verify_build_against_request

                current_wip["verification"] = verify_build_against_request(
                    original_request=current_wip.get("original_request") or _obj,
                    files=files,
                    test_report=_test_report if _test_report.get("ran") else None,
                )

    elif reflection_action == "PLUGIN_OPERATION":
        # A signed MCP call is applied execution for Omega's comprehension
        # layer, even though it does not mutate the workspace. Keep the
        # screened provider result in the same `applied` lane used by APPLY and
        # USE_SKILL so the fold prompt can reason from what actually happened.
        plugin_failure = str(outputs.get("plugin_failure") or "").strip()
        permission_pending = isinstance(outputs.get("plugin_permission_request"), Mapping)
        result_status = "pending_approval" if permission_pending else ("failed" if plugin_failure else "completed")
        step_results = outputs.get("plugin_step_results")
        completed_steps = (
            sum(
                isinstance(row, Mapping) and row.get("status") == "completed"
                for row in step_results
            )
            if isinstance(step_results, (list, tuple))
            else 0
        )
        plugin_summary = (
            plugin_failure
            or f"plugin operation completed ({completed_steps} step(s))"
        )
        applied = {
            "status": result_status,
            "summary": plugin_summary,
            "has_diff": False,
            "files_touched": 0,
            "result": raw_tool_result,
            "error": plugin_failure or None,
        }

    proactive_dispatch = None
    proactive_policy = None
    if str(execution_inputs.get("presentation_kind") or "") == "PRESENT_RESULT":
        proactive_dispatch = {
            key: execution_inputs.get(key)
            for key in (
                "presentation_kind",
                "proactive_thread_id",
                "proactive_policy_id",
                "proactive_application_id",
                "operator_policy",
            )
        }
        raw_policy = execution_inputs.get("operator_policy")
        if isinstance(raw_policy, Mapping):
            proactive_policy = dict(raw_policy)
    if proactive_policy is None and intent_name in {
        "RESOLVE_QUERY",
        "LEARN",
        "PROACTIVE_RESPONSE",
    }:
        thread_id = ""
        for row in (gbi.pressure or {}).get("contributors") or ():
            if not isinstance(row, Mapping):
                continue
            detail = row.get("detail") or row.get("metadata") or {}
            if isinstance(detail, Mapping) and detail.get("proactive_thread_id"):
                thread_id = str(detail["proactive_thread_id"])
                break
        from pressure_loop.proactive_lifecycle import active_policy

        # Policies are conversation-scoped. Never borrow the globally latest
        # policy when this episode did not carry its thread identity; doing so
        # lets unrelated learning advance another conversation's lifecycle.
        active = active_policy(thread_id=thread_id) if thread_id else None
        if active is not None:
            proactive_policy = active.to_dict()

    return OmegaEpisodeInput(
        omega_lane=gbi.omega_lane,
        original_objective=str(gbi.request_context.get("text") or gbi.request_context.get("objective") or ""),
        # Perception may have been collected while Global A classified an operator
        # turn for Browser. That turn still belongs to Chat; only the separately
        # opened Browser episode may carry browser evidence into Omega's packet.
        browser_perception=execution_inputs.get("browser_perception") if browser_lane else None,
        browser_run=outputs.get("browser_run") if browser_lane else None,
        agent_run=outputs.get("agent_run"),
        window_context=gbi.window_view,
        episode_id=str(gbi.episode_id or ""),
        intent=str(intent_decision.get("intent") or ""),
        action=action,
        tool=execution_pack.get("tool"),
        domain=domain,
        operator_text=(gbi.request_context.get("current_instruction") if gbi.omega_lane == "browser" else _operator_text(
            execution_pack,
            action,
            pressure={
                "reason": (gbi.pressure or {}).get("reason"),
                "contributors": list((gbi.pressure or {}).get("contributors") or []),
            },
        )),
        outcome=outcome,
        failure_reason=failure_reason,
        raw_tool_result=raw_tool_result,
        # Fetched-knowledge comprehension (operator 2026-07-11): already committed to
        # memory post-AUTO (receipt inside) — the fold reasons over the named/learned
        # form, not just the raw page dump.
        fetched_knowledge=(
            dict(outputs["fetched_knowledge"])
            if isinstance(outputs.get("fetched_knowledge"), Mapping)
            else None
        ),
        recall=_recall_tuple(outputs.get("remembered_knowledge_tool_results")),
        recall_error=_recall_error(auto_inner_dict),
        prior_mastery=_prior_mastery_for_domain(domain),
        applied=applied,
        neuromod_state=neuromod_state,
        execution_experience=_execution_experience_view(),
        learning_map=build_learning_map(cap_topics=30),
        prior_builds=tuple(prior_builds or ()),
        # current yield first (freshest), then any prior mid-flight context from recall.
        build_in_progress=((current_wip,) if current_wip else ()) + tuple(build_in_progress or ()),
        open_questions=_open_questions_view(),
        rollback_events=_recall_tuple(rollback_events),
        foldback_packet=_assemble_foldback_packet(gbi, exec_block, outputs, applied, domain),
        # Omega's OWN reply this turn (for the post-chat identity reflection to see both sides).
        response_text=(str(outputs.get("response_text")) if outputs.get("response_text") else None),
        motor_program=(
            dict(effective_motor_program) if isinstance(effective_motor_program, Mapping) else None
        ),
        originating_pressure=(
            dict(execution_inputs["emergent_pressure"])
            if isinstance(execution_inputs.get("emergent_pressure"), Mapping)
            else None
        ),
        proactive_policy=proactive_policy,
        proactive_dispatch=proactive_dispatch,
        onboarding_context=onboarding_context_from_pressure(gbi.pressure),
    )


def _open_questions_view(cap: int = 3) -> tuple[dict, ...]:
    """The operator questions Omega is still awaiting an answer to (open-questions journal),
    so EVERY per-episode prompt carries 'you have a pending ask'."""
    from pressure_loop.open_questions import list_open

    rows: list[dict] = []
    for q in list_open()[:cap]:
        text = str(getattr(q, "question_text", "") or "").strip()
        if not text:
            continue
        rows.append(
            {
                "question": text[:300],
                "windows_survived": int(getattr(q, "windows_survived", 0) or 0),
            }
        )
    return tuple(rows)


# Mail bound into an Omega pass must outlast the whole fold — Meta B, Global B, the Omega
# inference (a reasoning seat can take minutes), DeepSleep and the close — because it is
# acknowledged only once the fold's outcome is durable. A lease that runs out first loses
# nothing: acknowledgement is refused, the lease expires, and a later episode reasons
# over the mail again. That is at-least-once delivery, and binding performs no external
# effect, so a repeat costs cognition only.
_OMEGA_MAIL_LEASE_S = 1800.0
# Membership is budgeted on the EXACT rendered block (prompts.CROSS_LANE_MAIL_BLOCK_CHARS)
# and capped in rows (types.CROSS_LANE_MAIL_ROWS), both applied during selection. A raw
# serialized-payload budget is not the prompt size — escaping expands it — so it is not
# used here. Required mail that does not fit is RECORDED for prerequisite enforcement.


def _emit_mail_degraded(summary: str, **fields: Any) -> None:
    try:
        from guardian.terminal_feedback import emit_data

        emit_data("omega", "cross_lane_mail", status="degraded", ok=False,
                  summary=summary[:200], **fields)
    except Exception:  # noqa: BLE001 - telemetry never fails cognition
        pass


def _mail_record(episode: Any) -> dict[str, Any] | None:
    record = dict(getattr(episode, "global_", None) or {}).get("omega_mail")
    return dict(record) if isinstance(record, Mapping) else None


def omitted_required_omega_mail(episode: Any) -> tuple[str, ...]:
    """Required mail this fold recorded as omitted — the input to §6 enforcement."""
    record = _mail_record(episode) or {}
    return tuple(str(one) for one in (record.get("omitted_required_mail_ids") or ()) if str(one))


def _store_mail_record(episode: Any, record: Mapping[str, Any]) -> None:
    global_ = dict(getattr(episode, "global_", None) or {})
    global_["omega_mail"] = dict(record)
    episode.global_ = global_


def held_omega_mail(episode: Any) -> tuple[dict, ...]:
    """The bound mail this episode may render RIGHT NOW — ownership checked in the store.

    THE ownership boundary, shared by every path that turns a binding into prompt text:
    the binder's re-entry, and the readback that builds each inference prompt (retries
    included). A saved packet or a cached binding records what was bound; neither proves
    this episode still holds it. The episode may have released its mail after a failed
    fold, or its leases may have run out and been taken by another episode — and checking
    only at re-binding once let the real prompt path render a message another episode now
    owned (review 2026-09-16).

    "none" was a decided-empty membership; released, acknowledged, failed, lost and
    unavailable all mean this episode holds nothing it may render. Ownership is all or
    nothing: a partly lost binding is handed back whole and recorded as
    ``ownership_lost``, never rendered as a different set than the one decided.
    """
    from global_loop import intersections as mail_store
    from omega_loop.episode.types import bounded_mail

    record = _mail_record(episode)
    if record is None or record.get("disposition") != "bound":
        return ()
    episode_id = str(getattr(episode, "episode_id", "") or "")
    receipts = list(record.get("receipts") or ())
    try:
        held = mail_store.owned_mail(receipts, episode_id=episode_id)
    except ValueError:
        held = []
    if receipts and len(held) == len(receipts):
        return bounded_mail(list(record.get("mail") or ()))
    try:
        mail_store.release_mail(receipts, episode_id=episode_id)
    except ValueError:
        pass
    lost = dict(record)
    lost["disposition"] = "ownership_lost"
    _store_mail_record(episode, lost)
    _emit_mail_degraded("episode no longer holds its bound mail; binding dropped",
                        episode_id=episode_id, thread_id=str(record.get("thread_id") or ""))
    return ()


def omega_mail_fits(rows: list[dict[str, Any]]) -> bool:
    """Whether these mail rows fit Omega's mail slot as the EXACT block Omega will read.

    Framing, escaping and provenance included. The one admission rule: the binder admits
    each candidate through it, and operator ingress refuses an instruction that would not
    fit even alone, since no fold could ever admit it.
    """
    from omega_loop.episode import prompts
    from omega_loop.episode.types import bounded_mail

    block = prompts.render_cross_lane_block(bounded_mail(rows))
    return len(block) <= prompts.CROSS_LANE_MAIL_BLOCK_CHARS


def fold_identity(episode: Any) -> str:
    """The STABLE identity of this episode's fold, or "" when it has none.

    A restart replays work as a new episode with a new ``episode_id`` (carriers are not
    restored), so nothing keyed on the episode survives it. What does survive is the work
    the episode ignited on: the host-minted identity its dominant contributor carries — a
    successor's ``successor_id``, an operator turn's ``cycle_id``, else its queue identity.
    The same contributor igniting again IS the same fold. Host-derived end to end.

    A stable identity also REQUIRES the contributor's global arrival sequence — every queued
    ignition has one — because that birth order is what lets a store refuse a replay whose
    evidence it has evicted (:func:`fold_origin_seq`). An episode without both gets
    in-process replay only.
    """
    return _fold_origin(episode)[0]


def fold_origin_seq(episode: Any) -> int:
    """The fold's birth order: its ignition contributor's global arrival sequence, or 0."""
    return _fold_origin(episode)[1]


def _fold_origin(episode: Any) -> tuple[str, int]:
    import hashlib

    opened_by = str(getattr(episode, "opened_by", "") or "")
    rows = [row for row in (dict(getattr(episode, "pressure", None) or {}).get("contributors") or ())
            if isinstance(row, Mapping)]
    rows.sort(key=lambda row: str(row.get("id") or row.get("type") or "") != opened_by)
    for row in rows:
        detail = row.get("detail") or row.get("metadata") or {}
        if not isinstance(detail, Mapping):
            continue
        seq = detail.get("arrival_seq")
        if type(seq) is not int or seq <= 0:
            continue
        for key in ("successor_id", "cycle_id", "queue_id"):
            origin = str(detail.get(key) or "").strip()
            if origin:
                return (hashlib.sha256(f"omega-fold:{key}:{origin}".encode("utf-8"))
                        .hexdigest()[:40], seq)
    return "", 0


# A fold in one of these standings is a REPLAY that may not draw new conclusions: the stores
# can no longer vouch for its first attempt (beyond the horizon), that attempt already ran
# its course (reconciled), it is alive in this very process (held elsewhere), or part of the
# mail it reasoned over is gone (abandoned — it would conclude WITHOUT its original required
# mail set, and no transaction record need exist to stop it). Refusing only the BINDING left
# such a replay free to accept memory and publish (review 2026-09-17): it must do neither.
# An existing authoritative transaction record may still be completed.
_REFUSED_STANDINGS = frozenset({"beyond_replay_horizon", "reconciled", "held_elsewhere",
                                "abandoned"})


class FoldReplayRefused(RuntimeError):
    """A refused replay with NO frozen result to act on: nothing of it may be applied."""


def fold_replay_refused(episode: Any) -> str:
    """The standing that bars this fold from accepting memory or publishing, or ""."""
    standing = str(dict(_mail_record(episode) or {}).get("disposition") or "")
    return standing if standing in _REFUSED_STANDINGS else ""


def while_omega_mail_owned(episode: Any, action):
    """Run ``action`` only while this fold still owns every mail row it bound — ownership
    checked and the action run in ONE critical section of the mail store. A fold that bound
    nothing just runs it. Raises ``intersections.MailOwnershipLost`` otherwise, after
    recording the loss on the carrier."""
    from global_loop import intersections as mail_store

    record = _mail_record(episode)
    if record is None or record.get("disposition") != "bound":
        if record is not None and record.get("disposition") == "ownership_lost":
            raise mail_store.MailOwnershipLost("mail ownership lost before the action")
        return action()
    try:
        return mail_store.while_owned(list(record.get("receipts") or ()),
                                      episode_id=str(getattr(episode, "episode_id", "") or ""),
                                      action=action)
    except mail_store.MailOwnershipLost:
        held_omega_mail(episode)      # records ownership_lost and hands back what it can
        raise


def omega_mail_ownership_lost(episode: Any) -> bool:
    """Whether this fold bound mail it no longer owns — checked at PUBLICATION (T8).

    Refusing a late acknowledgement protects only the mail's deletion. A fold whose lease
    lapsed during inference reasoned over words another fold may now own, so nothing derived
    from them — no memory commit, no lane-entry mail, no continuation — may be published.
    ``held_omega_mail`` performs the check against the store and records the loss.
    """
    record = _mail_record(episode)
    if record is None or record.get("disposition") not in {"bound", "ownership_lost"}:
        return False
    if record.get("disposition") == "ownership_lost":
        return True
    held_omega_mail(episode)
    return dict(_mail_record(episode) or {}).get("disposition") != "bound"


def bind_omega_mail(
    episode: Any,
    *,
    lane: str,
    job_id: str = "",
    allow_unaddressed: bool = True,
) -> tuple[dict, ...]:
    """Claim this episode's omega_reason mail ONCE; record the binding on the carrier.

    Integration spec section 3. Returns the bound mail as packet rows.

    **Membership is decided at the first binding.** The cutoff is the store's high-water
    sequence read before claiming, so mail pinned afterwards belongs to a later episode.
    A re-entered fold reads back the recorded set and never claims again, so it
    reconciles exactly the inputs it started with — even when that set was empty.

    **Across a restart (T8).** The carrier's record dies with the process, so the bound set
    is ALSO recorded in the mail store under the fold's stable identity
    (:func:`fold_identity`), in the same publication as the claim. A replay of the fold — a
    new episode — finds that binding before claiming and takes over the identical set
    (``intersections.claim_for_fold``); a fold whose mail was already acknowledged binds
    nothing, and a partly available set is abandoned rather than silently changed. A fold
    with no stable identity keeps in-process replay only.

    Ownership comes from the store's atomic lease, so two episodes of one thread folding
    at once cannot both bind the same mail. A corrupt store is recorded as UNAVAILABLE
    rather than read as empty, and required mail left over the budget is recorded as
    omitted so prerequisite enforcement can refuse a dependent action.
    """
    from global_loop import intersections as mail_store
    from omega_loop.episode.types import bounded_mail

    thread_id = str(getattr(episode, "thread_id", "") or "")
    episode_id = str(getattr(episode, "episode_id", "") or "")
    existing = _mail_record(episode)
    if existing is not None and existing.get("disposition"):
        # Re-entry never claims again: membership was decided at the first binding. What
        # it may still render is whatever it still OWNS.
        return held_omega_mail(episode)
    if not thread_id or not episode_id:
        return ()

    fold_id = fold_identity(episode)
    record: dict[str, Any] = {"thread_id": thread_id, "lane": lane, "job_id": job_id,
                              "fold_id": fold_id}
    try:
        from omega_loop.episode.types import CROSS_LANE_MAIL_ROWS

        # Cutoff, claim and the fold's durable binding: one critical section of the store.
        bound = mail_store.claim_for_fold(
            fold_id=fold_id, lane=lane, thread_id=thread_id, episode_id=episode_id,
            job_id=job_id, lease_s=_OMEGA_MAIL_LEASE_S, max_rows=CROSS_LANE_MAIL_ROWS,
            admit=omega_mail_fits, origin_seq=fold_origin_seq(episode),
            allow_unaddressed=allow_unaddressed)
        claimed, refused, cutoff = bound["claimed"], bound["refused"], bound["cutoff_seq"]
        # From the claim's OWN refusals, decided under its lock. Re-reading the store
        # afterwards lost the omission whenever another consumer leased a refused required
        # message in between (review 2026-09-16).
        omitted = [row["mail_id"] for row in refused if row["requirement"] == "required"]
    except ValueError as exc:
        record.update(disposition="unavailable", error=str(exc)[:300], cutoff_seq=0,
                      bound_mail_ids=[], receipts=[], mail=[], omitted_required_mail_ids=[])
        _emit_mail_degraded("cross-lane mail store unavailable at binding",
                            episode_id=episode_id, thread_id=thread_id)
    else:
        outcome = bound["disposition"]
        if outcome in {"abandoned", "held_elsewhere", "beyond_replay_horizon"}:
            _emit_mail_degraded(f"fold replay could not rebind its mail ({outcome})",
                                episode_id=episode_id, thread_id=thread_id)
        record.update(
            # "replayed" holds leases exactly like "bound"; the rest hold nothing.
            disposition="bound" if claimed else ("none" if outcome == "bound" else outcome),
            replayed=outcome == "replayed",
            cutoff_seq=cutoff,
            bound_mail_ids=[row["mail_id"] for row in claimed],
            # Host bookkeeping for acknowledgement; never rendered into the prompt.
            receipts=[{"mail_id": row["mail_id"], "lease_id": row["lease_id"],
                       "generation": row["generation"]} for row in claimed],
            mail=list(bounded_mail(claimed)),
            omitted_required_mail_ids=omitted,
        )
        if omitted:
            _emit_mail_degraded("required cross-lane mail omitted over the payload budget",
                                episode_id=episode_id, thread_id=thread_id, count=len(omitted))
    _store_mail_record(episode, record)
    return bounded_mail(record["mail"])


def acknowledge_omega_mail(episode: Any) -> int:
    """End the lifecycle of the mail this episode bound — AFTER its outcome is durable.

    Wired to ``EpisodeStore.close``'s ``after_archive`` seam, which fires only when the
    episode's journal row was actually archived. Failure-soft by design: if the lease
    ran out during a long fold, the acknowledgement is refused, the mail expires, and a
    later episode reasons over it again. That is at-least-once delivery, not loss, and it
    must never fail a close whose required work already committed.
    """
    record = _mail_record(episode)
    if record is None or record.get("disposition") != "bound":
        return 0
    from global_loop import intersections as mail_store

    episode_id = str(getattr(episode, "episode_id", "") or "")
    specialist_handoffs = [
        str(dict(row.get("payload") or {}).get("handoff_id") or "")
        for row in record.get("mail") or ()
        if isinstance(row, Mapping)
        and row.get("purpose") == "specialist_lane_handoff"
        and dict(row.get("payload") or {}).get("handoff_id")
    ]
    if specialist_handoffs:
        try:
            from pressure_loop import cognition_queue

            # Publish queue ownership first. If the process dies between the two stores,
            # the pending marker can be reconciled against the still-present mail row.
            cognition_queue.prepare_handoff_mail_absorption(specialist_handoffs)
        except Exception as exc:  # noqa: BLE001 - the still-leased mail can be replayed
            record.update(disposition="handoff_settlement_failed", error=str(exc)[:300])
            _emit_mail_degraded(
                f"specialist handoff queue ownership could not be persisted: "
                f"{type(exc).__name__}: {exc}",
                episode_id=episode_id, thread_id=str(record.get("thread_id") or ""),
            )
            _store_mail_record(episode, record)
            return 0
    try:
        count = mail_store.acknowledge_mail(list(record.get("receipts") or ()),
                                            episode_id=episode_id)
    except ValueError as exc:
        record.update(disposition="acknowledge_failed", error=str(exc)[:300])
        _emit_mail_degraded("bound cross-lane mail could not be acknowledged",
                            episode_id=episode_id, thread_id=str(record.get("thread_id") or ""))
        count = 0
    else:
        record["disposition"] = "acknowledged"
        if specialist_handoffs and count == len(list(record.get("receipts") or ())):
            try:
                from pressure_loop import cognition_queue

                cognition_queue.complete_handoff_mail_absorption(specialist_handoffs)
            except Exception as exc:  # noqa: BLE001 - pending marker reconciles after restart
                _emit_mail_degraded(
                    f"specialist handoff queue suppression needs reconciliation: "
                    f"{type(exc).__name__}: {exc}",
                    episode_id=episode_id, thread_id=str(record.get("thread_id") or ""),
                )
            try:
                from pressure_loop import cognition_queue

                cognition_queue.mark_handoff_mail_acknowledged(specialist_handoffs)
            except Exception as exc:  # noqa: BLE001 - boot reconciliation retries this cleanup
                _emit_mail_degraded(
                    f"specialist handoff receipt settlement failed: {type(exc).__name__}: {exc}",
                    episode_id=episode_id, thread_id=str(record.get("thread_id") or ""),
                )
    _store_mail_record(episode, record)
    return count


def release_omega_mail(episode: Any) -> int:
    """A fold that produced no valid control hands its bound mail back NOW.

    Without this the mail would sit leased for the whole lease, so the operator's words
    would wait half an hour for an episode that had already given up on them.
    """
    record = _mail_record(episode)
    if record is None or record.get("disposition") != "bound":
        return 0
    from global_loop import intersections as mail_store

    episode_id = str(getattr(episode, "episode_id", "") or "")
    try:
        count = mail_store.release_mail(list(record.get("receipts") or ()),
                                        episode_id=episode_id)
    except ValueError as exc:
        record.update(disposition="release_failed", error=str(exc)[:300])
        _emit_mail_degraded("bound cross-lane mail could not be released",
                            episode_id=episode_id, thread_id=str(record.get("thread_id") or ""))
        count = 0
    else:
        record["disposition"] = "released"
    _store_mail_record(episode, record)
    return count


def attach_omega_packet(
    episode,
    gbi: GlobalBInput,
    meta_reflection: Mapping[str, Any],
) -> OmegaEpisodeInput:
    """Build OmegaEpisodeInput and attach to episode.global_['omega_packet']."""
    # The artifact recall (skill_recall_context.prior_builds, attached at Meta-A selection —
    # which runs BEFORE this Omega fold) — surfaced into the packet so Omega's inference sees
    # what it has already built for this kind of objective (operator 2026-06-25).
    episode_meta = dict(getattr(episode, "meta", None) or {})
    from dataclasses import replace

    gbi = replace(gbi, global_outcome=dict((getattr(episode, "global_", None) or {}).get("outcome") or {}))
    _recall = dict(episode_meta.get("skill_recall_context") or {})
    packet = build_omega_episode_input(
        gbi,
        meta_reflection,
        prior_builds=tuple(_recall.get("prior_builds") or ()),
        # An UNFINISHED build for this objective — so Omega's fold decides continue|finish.
        build_in_progress=tuple(_recall.get("build_in_progress") or ()),
        rollback_events=tuple(episode_meta.get("dev_rollback_events") or ()),
        motor_program=(
            dict(episode_meta["motor_program"])
            if isinstance(episode_meta.get("motor_program"), Mapping)
            and episode_meta["motor_program"].get("shadow") is not True
            else None
        ),
    )
    # CROSS-LANE MAIL (T6): bound once for this episode's thread BEFORE the packet is
    # serialized, because the serialized packet is the only one Omega ever reads.
    execution_inputs = dict(dict(getattr(gbi, "execution_pack", None) or {})
                            .get("execution_inputs") or {})
    # The fold is addressed by the job its batch ACTUALLY ran (cross-lane T7). A first batch
    # claims its job during AUTO, so only the host-built agent_run receipt names it; Meta A's
    # execution_inputs carry a job id only for a continuation. Without the receipt, operator
    # mail pinned for a build mid-batch skipped the very fold that reasons over that batch.
    ran_job = str(dict(packet.agent_run or {}).get("job_id") or "")
    auto_outputs = dict(auto_inner(gbi.auto_outcome).get("outputs") or {})
    handoff_only_chat = (
        packet.omega_lane == "chat"
        and isinstance(auto_outputs.get("specialist_handoff"), Mapping)
    )
    bound = bind_omega_mail(
        episode,
        lane=str(packet.omega_lane or "chat"),
        job_id=ran_job or str(execution_inputs.get("job_id") or ""),
        allow_unaddressed=not handoff_only_chat,
    )
    if bound:
        packet = replace(packet, cross_lane_mail=bound)
    omitted = omitted_required_omega_mail(episode)
    if omitted:
        packet = replace(packet, omitted_required_mail_ids=omitted)
    global_ = dict(getattr(episode, "global_", None) or {})
    global_["omega_packet"] = packet.to_dict()
    episode.global_ = global_
    return packet


def capture_execution_request_context(
    episode: Any,
    *,
    request_text: str = "",
) -> dict[str, str]:
    """Snapshot the immutable request before fold-back updates the SRAM report."""
    meta = dict(getattr(episode, "meta", None) or {})
    captured = meta.get("execution_request_context")
    if isinstance(captured, Mapping):
        return dict(captured)
    raw_program = meta.get("motor_program")
    outcome_contract = (
        dict(raw_program.get("outcome_contract") or {}) if isinstance(raw_program, Mapping) else {}
    )
    objective = str(outcome_contract.get("objective") or "").strip()
    execution_pack = dict(meta.get("execution_action_pack") or {})
    origin = str(
        getattr(episode, "opened_by", None)
        or dict(getattr(episode, "pressure", None) or {}).get("opened_by")
        or dict(execution_pack.get("source_action_goal") or {}).get("opened_by")
        or "omega"
    ).strip()
    text = str(request_text or "").strip()

    from memory.execution_report import current_execution_report

    report = current_execution_report()
    if report is not None and (not objective or report.build_task == objective):
        if report.origin_opened_by:
            origin = report.origin_opened_by
        if report.original_request:
            text = report.original_request

    context = {
        "origin": origin or "omega",
        "text": text or objective,
        "objective": objective,
    }
    if context["text"]:
        meta["execution_request_context"] = dict(context)
        episode.meta = meta
    return context


def refresh_omega_execution_foldback(
    episode: Any,
    *,
    request_text: str = "",
    request_origin: str = "",
) -> dict[str, Any] | None:
    """Attach canonical post-agent evidence/verdict to Omega's existing packet."""
    from auto_loop.motor_adapter import execution_evidence_from_fold
    from schema.execution_foldback import ExecutionFoldback
    from schema.motor_program import MotorProgram

    episode_id = str(getattr(episode, "episode_id", None) or "").strip()
    meta = dict(getattr(episode, "meta", None) or {})
    raw_program = meta.get("motor_program")
    if not isinstance(raw_program, Mapping) or raw_program.get("shadow") is True:
        return None
    program = MotorProgram.from_dict(raw_program)
    if not episode_id or program.episode_id != episode_id:
        raise ValueError("post-execution foldback motor episode_id mismatch")

    execution_pack = dict(meta.get("execution_action_pack") or {})
    if str(execution_pack.get("episode_id") or "") != episode_id:
        raise ValueError("post-execution foldback execution pack episode_id mismatch")
    execution_inputs = dict(execution_pack.get("execution_inputs") or {})
    transported = execution_inputs.get("motor_program")
    if isinstance(transported, Mapping):
        if MotorProgram.from_dict(transported).to_dict() != program.to_dict():
            raise ValueError("post-execution foldback transported motor program mismatch")

    skill_pack = dict(execution_inputs.get("skill_execution_pack") or {})
    if not skill_pack:
        return None
    if str(skill_pack.get("episode_id") or "") != episode_id:
        raise ValueError("post-execution foldback skill pack episode_id mismatch")
    dispatch_id = str(skill_pack.get("dispatch_id") or "").strip()
    selected_skill = dict(skill_pack.get("selected_skill") or {})
    if not dispatch_id:
        raise ValueError("post-execution foldback is missing dispatch_id")

    auto = dict(getattr(episode, "auto", None) or {})
    if str(auto.get("episode_id") or "") != episode_id:
        raise ValueError("post-execution foldback AUTO episode_id mismatch")
    outputs = dict(auto.get("outputs") or {})
    result = dict(outputs.get("qwen_skill_result") or outputs.get("skill_result") or {})
    if str(result.get("episode_id") or "") != episode_id:
        raise ValueError("post-execution foldback result episode_id mismatch")
    if str(result.get("dispatch_id") or "") != dispatch_id:
        raise ValueError("post-execution foldback result dispatch_id mismatch")
    for key in ("skill_id", "skill_name"):
        expected = str(selected_skill.get(key) or "")
        if expected and str(result.get(key) or "") != expected:
            raise ValueError(f"post-execution foldback result {key} mismatch")

    global_ = dict(getattr(episode, "global_", None) or {})
    skill_outcome = dict(global_.get("skill_outcome") or {})
    if str(skill_outcome.get("episode_id") or "") != episode_id:
        raise ValueError("post-execution foldback skill outcome episode_id mismatch")
    if str(skill_outcome.get("dispatch_id") or "") != dispatch_id:
        raise ValueError("post-execution foldback skill outcome dispatch_id mismatch")

    evidence = execution_evidence_from_fold(episode)
    if evidence.episode_id != episode_id or evidence.program_id != program.program_id:
        raise ValueError("post-execution foldback factual evidence lineage mismatch")

    verification = meta.get("execution_verification")
    if not isinstance(verification, Mapping):
        candidate = skill_outcome.get("verification")
        verification = candidate if isinstance(candidate, Mapping) else None
    verification_dict = dict(verification) if isinstance(verification, Mapping) else None

    blockers = [str(row) for row in (evidence.blockers or ()) if str(row).strip()]
    blockers.extend(
        str(row)
        for row in (skill_outcome.get("unresolved_blockers") or ())
        if str(row).strip() and str(row) not in blockers
    )
    if verification_dict is not None and not bool(verification_dict.get("all_green")):
        for check in verification_dict.get("checks") or ():
            if not isinstance(check, Mapping) or bool(check.get("ok", check.get("passed", False))):
                continue
            label = str(check.get("name") or check.get("check") or "verification_failed")
            blocker = f"verification_failed:{label}"
            if blocker not in blockers:
                blockers.append(blocker)

    satisfied = bool(
        skill_outcome.get("intent_satisfied")
        and skill_outcome.get("objective_complete")
        and evidence.status == "success"
        and not blockers
        and (verification_dict is None or bool(verification_dict.get("all_green")))
    )
    objective = program.outcome_contract.objective
    request_context = capture_execution_request_context(
        episode,
        request_text=request_text,
    )
    origin = str(request_origin or request_context["origin"]).strip() or "omega"
    original_text = str(request_text or request_context["text"]).strip() or objective

    contract = ExecutionFoldback(
        episode_id=episode_id,
        request={"origin": origin, "text": original_text, "objective": objective},
        motor={
            "program_id": program.program_id,
            "outcome_id": program.outcome_id,
            "acceptance_criteria": list(program.outcome_contract.acceptance_criteria),
        },
        dispatch={
            "episode_id": episode_id,
            "dispatch_id": dispatch_id,
            "skill_id": str(selected_skill.get("skill_id") or ""),
            "skill_name": str(selected_skill.get("skill_name") or ""),
            "status": str(result.get("status") or ""),
        },
        execution_evidence=evidence.to_dict(),
        verification=verification_dict,
        verdict={
            "satisfied": satisfied,
            "more_work_needed": bool(not satisfied),
            "blockers": blockers,
            "summary": str(skill_outcome.get("summary") or evidence.progress_digest or ""),
        },
    ).to_dict()

    packet = dict(global_.get("omega_packet") or {})
    if not packet:
        raise ValueError("post-execution foldback requires an attached omega_packet")
    if str(packet.get("episode_id") or "") != episode_id:
        raise ValueError("post-execution foldback omega packet episode_id mismatch")
    packet["execution_foldback"] = contract
    global_["omega_packet"] = packet
    episode.global_ = global_
    return contract

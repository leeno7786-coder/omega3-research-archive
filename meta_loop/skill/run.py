"""Meta A — select the exact ExecutionSkill for a USE_SKILL intent (Phase 6F).

Deterministic scoring over the ExecutionSkill catalog. Meta A SELECTS; it never executes,
never calls qwen, never writes a qwen prompt, never chooses qwen's internal tool sequence.
A failed selection returns a structured failure_reason (no silent drop).
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from schema.selected_skill import SelectedSkillDecision

# Build/produce verbs — ALL mean WRITE ("build a script", "create a module", "scaffold a
# package", etc.). Named once so _needed_kind can also use them to override a TEST-noun match
# (see below), and so the WRITE marker set stays in sync.
_BUILD_VERBS: tuple[str, ...] = (
    "implement",
    "write code",
    "write a",
    "write the",
    "build",
    "rebuild",
    "create",
    "make a",
    "generate",
    "scaffold",
    "add a",
    "new script",
    "new file",
)

# Task text -> the execution_kind it needs. Priority order: specific before generic, so
# "review this code" -> REVIEW (not INSPECT), "run the tests" -> TEST, etc.
_KIND_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "MAINTAIN",
        (
            "skill-system",
            "maintain",
            "skill system",
            "modify a skill",
            "create a skill",
            "edit a skill",
        ),
    ),
    ("REFACTOR", ("refactor", "restructure", "clean up", "simplify the")),
    ("REVIEW", ("review", "audit", "critique", "code review")),
    ("TEST", ("run test", "run the test", "run tests", "tests", "pytest", "test suite", "testing")),
    ("DEBUG", ("debug", "diagnose")),
    ("REPAIR", ("fix", "repair", "bug", "broken", "resolve")),
    ("PATCH", ("patch", "apply patch", "edit the")),
    # Common build/produce verbs ALL mean WRITE — "build a script", "create a module", "make a
    # helper", "write a X" etc. (operator live finding 2026-06-28: only "build the" matched, so
    # "build a python script" fell through to INSPECT and a read_only planner was selected for a
    # build). MAINTAIN's "create a skill" is checked first, so skill-system creates still route there.
    ("WRITE", _BUILD_VERBS),
    ("PLAN", ("plan", "design", "outline", "roadmap")),
    ("FETCH", ("fetch", "download", "web research", "research online")),
    ("SEARCH", ("search", "grep", "look for")),
    (
        "INSPECT",
        ("inspect", "examine", "look at", "where", "find", "locate", "understand", "explore the"),
    ),
    ("REPORT", ("report", "summarize", "explain")),
)

# The skill_class that NATURALLY serves each execution_kind — a strong intent signal so
# "run tests" -> testing_skill (not a repair skill that merely lists TEST), "review" ->
# review_skill, "inspect" -> planning/inspection, etc.
_KIND_CLASS_AFFINITY: dict[str, frozenset] = {
    "INSPECT": frozenset({"inspection_skill", "planning_skill"}),
    "SEARCH": frozenset({"inspection_skill"}),
    "PLAN": frozenset({"planning_skill"}),
    "REVIEW": frozenset({"review_skill"}),
    "TEST": frozenset({"testing_skill"}),
    "DEBUG": frozenset({"debugging_skill"}),
    "REPAIR": frozenset({"repair_skill", "debugging_skill"}),
    "REFACTOR": frozenset({"refactor_skill"}),
    "PATCH": frozenset({"repair_skill", "refactor_skill"}),
    "WRITE": frozenset({"repair_skill"}),
    "MAINTAIN": frozenset({"maintenance_skill"}),
    "FETCH": frozenset({"research_skill"}),
    "VERIFY": frozenset({"verification_skill", "testing_skill"}),
    "REPORT": frozenset({"review_skill", "planning_skill"}),
}

# Execution kinds that mutate the world — a read_only skill cannot serve them.
_WRITE_KINDS = frozenset({"PATCH", "WRITE", "RUN", "REPAIR", "REFACTOR", "MAINTAIN", "APPLY"})
_WRITE_TOOLS = frozenset(
    {
        "write_file",
        "patch_file",
        "run_command",
        "cli.write_file",
        "cli.patch_file",
        "cli.run_command",
    }
)

_STOP = frozenset(
    {
        "the",
        "a",
        "an",
        "this",
        "that",
        "and",
        "to",
        "of",
        "in",
        "on",
        "for",
        "me",
        "my",
        "is",
        "it",
        "please",
        "code",
        "repo",
    }
)


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", str(text or "").lower()) if t and t not in _STOP}


def _needed_kind(text: str) -> str:
    # whole-word match so "patch" matches `apply patch` but NOT "dis-patch", "fix" the
    # word but not "prefix", etc.
    t = str(text or "").lower()

    def _hit(markers: Iterable[str]) -> bool:
        return any(re.search(r"\b" + re.escape(m) + r"\b", t) for m in markers)

    for kind, markers in _KIND_MARKERS:
        if _hit(markers):
            # A build/create objective is fundamentally WRITE even when it also NAMES tests as
            # part of the deliverable ("build an app WITH pytest tests"). TEST is for RUNNING
            # existing tests, not for building something that includes them — so a WRITE build
            # verb overrides a TEST-noun match. Operator live finding 2026-07-01: "build a todo
            # app with pytest tests" routed to testing-strategy (TEST); the whole build then
            # failed the TEST evidence gate (tests_run empty) and its real work was discarded.
            if kind == "TEST" and _hit(_BUILD_VERBS):
                return "WRITE"
            return kind
    return "INSPECT"  # default agentic ask = inspect


def _strength(skill, strengths: Optional[dict]) -> float:
    if not strengths:
        return 0.5
    return float(strengths.get(skill.skill_id, 0.5))


def _score_skill(
    skill, *, task_text: str, target: str, needed_kind: str, needs_write: bool, strengths
) -> tuple[float, str, list[str]]:
    task_tokens = _tokens(task_text) | _tokens(target)
    skill_tokens = (
        _tokens(skill.name) | _tokens(skill.skill_class) | {d.lower() for d in skill.domains}
    )
    matched = sorted(task_tokens & skill_tokens)

    # 0.30 intent_match — the skill_class that naturally serves the needed kind scores
    # full; otherwise fall back to task/name/domain token overlap.
    if skill.skill_class in _KIND_CLASS_AFFINITY.get(needed_kind, frozenset()):
        intent_match = 1.0
    else:
        intent_match = min(1.0, len(matched) / 2.0)
    # 0.20 execution_kind_match — does the skill perform the kind the task needs?
    has_kind = needed_kind in skill.execution_kinds
    execution_kind_match = 1.0 if has_kind else 0.0
    # 0.15 target_domain_match
    target_domain_match = 1.0 if (task_tokens & {d.lower() for d in skill.domains}) else 0.3
    # 0.15 tool_requirement_match — write tasks need a write-capable tool
    has_write_tool = bool(set(skill.allowed_tools) & _WRITE_TOOLS)
    tool_requirement_match = (1.0 if has_write_tool else 0.0) if needs_write else 1.0
    # 0.10 safety_compatibility — prefer the least-privileged class that fits
    if needs_write:
        safety_compatibility = (
            1.0 if skill.safety_class in ("write_safe", "command_safe", "destructive") else 0.0
        )
    else:
        safety_compatibility = 1.0 if skill.safety_class in ("read_only", "network_read") else 0.7
    # 0.10 capability_strength
    strength = _strength(skill, strengths)

    score = (
        0.30 * intent_match
        + 0.20 * execution_kind_match
        + 0.15 * target_domain_match
        + 0.15 * tool_requirement_match
        + 0.10 * safety_compatibility
        + 0.10 * strength
    )
    chosen_kind = (
        needed_kind
        if has_kind
        else (skill.execution_kinds[0] if skill.execution_kinds else needed_kind)
    )
    return score, chosen_kind, matched


def _objective_relevance(skill, task_tokens: set) -> int:
    """Tie-break signal: how many objective terms the skill is relevant to, read from
    name + class + domains + DESCRIPTION. Richer than the score's intent_match (which only
    sees name/class/domains and is bypassed entirely when class-affinity already gives 1.0),
    so an objective that NAMES a specialty (e.g. "security audit") routes to that specialist
    instead of losing a score-tie to an alphabetically-earlier off-domain skill."""
    pool = (
        _tokens(skill.name)
        | _tokens(getattr(skill, "skill_class", "") or "")
        | {str(d).lower() for d in skill.domains}
        | _tokens(getattr(skill, "description", "") or "")
    )
    return len(task_tokens & pool)


def _generality(skill, domain_freq) -> int:
    """Tie-break signal: how GENERAL-purpose the skill is — the catalog frequency of its RAREST
    domain. A skill carrying a unique domain (freq 1: security, creative-direction, model-quality)
    is specialized; one whose every domain is widely shared (engineering, planning) is general.
    For an objective that matches NO skill's specialty, prefer the most general candidate over a
    narrow one that merely shares the needed kind — the deferred 'domain-aware ranking among
    equals' (6F). Derived from the catalog, not a hardcoded list."""
    doms = [str(d).lower() for d in skill.domains]
    return min((int(domain_freq.get(d, 0)) for d in doms), default=0)


def select_skill_for_intent(
    *,
    objective: str,
    target: str = "",
    pressure_reason: str = "",
    constraints: Iterable[str] = (),
    trajectory: Optional[dict] = None,
    catalog: Optional[list] = None,
    strengths: Optional[dict] = None,
    allow_self_modification: bool = False,
    recall_context: Optional[dict] = None,
) -> SelectedSkillDecision:
    """Pick the highest-scoring selectable ExecutionSkill for a USE_SKILL objective.

    ``catalog`` defaults to the bundled selectable execution skills. Returns a
    SelectedSkillDecision; on no safe match returns selected=False + a failure_reason.

    ``recall_context`` (7N, deterministic memory taxonomy) is an optional, additive nudge:
    a per-skill ``skill_boosts`` / ``skill_penalties`` map recalled from prior workflows +
    failures. It adjusts each skill's score AFTER the deterministic fit score and annotates
    the reason. ``None`` (the default / flag-OFF) -> byte-identical to the pre-7N selection.
    """
    if catalog is None:
        from runtime.execution_skills import selectable_execution_skills

        catalog = selectable_execution_skills()
    if not catalog:
        return SelectedSkillDecision.failed("skill_catalog_unavailable")

    task_text = f"{objective} {pressure_reason}".strip()
    needed_kind = _needed_kind(task_text or target)
    needs_write = needed_kind in _WRITE_KINDS

    # Tie-break inputs (computed once): the objective's tokens, and per-domain frequency across the
    # selectable catalog (the "generality" denominator). Both feed the domain-aware tie-break below.
    from collections import Counter

    task_tokens = _tokens(task_text) | _tokens(target)
    domain_freq: Counter = Counter()
    for _s in catalog:
        if getattr(_s, "selectable", True):
            for _d in _s.domains:
                domain_freq[str(_d).lower()] += 1

    scored: list[tuple[float, float, object, str, list[str], str]] = []
    unsafe_capable_existed = False
    for skill in catalog:
        if not skill.selectable:  # disabled cannot be selected
            continue
        capable = needed_kind in skill.execution_kinds
        # self_modification blocked unless explicitly authorized
        if skill.safety_class == "self_modification" and not allow_self_modification:
            if capable:
                unsafe_capable_existed = True
            continue
        # read_only skills cannot serve a write/patch task
        if needs_write and skill.safety_class == "read_only":
            if capable:
                unsafe_capable_existed = True
            continue
        score, chosen_kind, matched = _score_skill(
            skill,
            task_text=task_text,
            target=target,
            needed_kind=needed_kind,
            needs_write=needs_write,
            strengths=strengths,
        )
        fit_score = score
        # 7N: recall context is an additive nudge applied AFTER the deterministic fit score.
        # None (flag-OFF) -> note stays "" and score is unchanged -> byte-identical.
        note = _recall_adjust_note(skill, recall_context)
        if note:
            score += note[1]
        scored.append((score, fit_score, skill, chosen_kind, matched, note[0] if note else ""))

    if not scored:
        return SelectedSkillDecision.failed(
            "unsafe_skill_required" if unsafe_capable_existed else "no_matching_skill"
        )

    # tie-break (among EQUAL scores only — never overrides a higher fit score):
    #   deterministic fit score desc
    #   → objective-RELEVANCE desc  (the objective's words match this skill's name/class/domains/desc)
    #   → GENERALITY desc           (least-specialized wins when the objective matches no specialty)
    #   → recall-adjusted score desc (learned history refines an already-relevant choice)
    #   → capability_strength desc  (proven capability)
    #   → skill_name asc, skill_id asc  (final deterministic fallback)
    # Relevance + generality are the deferred "domain-aware ranking among equals" (6F): a generic
    # objective now prefers a codebase-general skill (e.g. omega-planner-executor) over a narrow
    # specialist (auditing-security) that merely shares the kind and sorted earlier alphabetically;
    # an objective that NAMES a specialty routes to that specialist via description relevance.
    scored.sort(
        key=lambda r: (
            -r[1],
            -_objective_relevance(r[2], task_tokens),
            -_generality(r[2], domain_freq),
            -r[0],
            -_strength(r[2], strengths),
            r[2].name,
            r[2].skill_id,
        )
    )
    best_score, _fit_score, best, chosen_kind, matched, note = scored[0]
    if best_score <= 0.0:
        return SelectedSkillDecision.failed("no_matching_skill")

    reason = (
        f"selected {best.name} for {needed_kind} (score {best_score:.3f}; matched {matched or '—'})"
    )
    if note:
        reason += f" [{note}]"
    return SelectedSkillDecision.from_skill(
        best,
        score=best_score,
        execution_kind=chosen_kind,
        reason=reason,
        matched_terms=tuple(matched),
    )


def select_named_skill(name: str, *, objective: str, catalog: list) -> SelectedSkillDecision:
    """Bind the ExecutionSkill the operator selected by slash command — never a scored one.

    The skill must be in the selection catalog (bundled, or a trusted user skill) and
    enabled. Its execution kind is the objective's kind when the skill declares it,
    otherwise the skill's first declared kind; the skill's own contract bounds the tools.
    """
    from runtime.qwen_skills import normalize_skill_name

    wanted = normalize_skill_name(name)
    for skill in catalog:
        if normalize_skill_name(skill.name) != wanted:
            continue
        kinds = tuple(skill.execution_kinds)
        if not skill.selectable or not kinds:
            return SelectedSkillDecision.failed(f"selected_skill_disabled:{wanted}")
        needed = _needed_kind(objective)
        kind = needed if needed in kinds else kinds[0]
        return SelectedSkillDecision.from_skill(
            skill,
            score=1.0,
            execution_kind=kind,
            reason=f"operator selected /skill:{wanted} for {kind}",
        )
    return SelectedSkillDecision.failed(f"selected_skill_unavailable:{wanted}")


def _recall_adjust_note(skill, recall_context: Optional[dict]) -> Optional[tuple[str, float]]:
    """Return (reason_note, score_delta) for a skill given recall context, or None.

    boost - penalty is the additive delta; the note describes the recalled history. None
    when there is no recall context or no recalled signal for this skill (-> no adjustment).
    """
    if not recall_context:
        return None
    boost = float((recall_context.get("skill_boosts") or {}).get(skill.name, 0.0))
    penalty = float((recall_context.get("skill_penalties") or {}).get(skill.name, 0.0))
    if not boost and not penalty:
        return None
    if boost and penalty:
        note = "recall: mixed history"
    elif boost:
        note = "recall: proven workflow"
    else:
        note = "recall: prior failure (cautioned)"
    return note, boost - penalty


def _bundled_catalog():
    from runtime.execution_skills import selectable_execution_skills
    from runtime.qwen_skills import BUNDLED_SKILL_DIRS

    return selectable_execution_skills(roots=[BUNDLED_SKILL_DIRS[0]], config={})


def _user_skill_roots() -> list:
    from core.paths import OMEGA_ROOT

    return [
        OMEGA_ROOT / "skills",
        OMEGA_ROOT / "guardian" / "state" / "live-chat" / "skills",
    ]


def _user_skill_trust_policy() -> tuple[list[str], bool]:
    from core.skill_execution_config import user_skill_trust_policy

    return user_skill_trust_policy()


def _selection_catalog() -> list:
    """Bundled selectable skills plus explicitly trusted user skills (deduped by skill_id)."""
    by_id = {s.skill_id: s for s in _bundled_catalog()}
    from runtime.user_execution_skills import selectable_user_execution_skills

    trust_names, trust_all = _user_skill_trust_policy()
    user_roots = [str(p) for p in _user_skill_roots()]
    user = selectable_user_execution_skills(
        roots=user_roots,
        trust_approved=trust_names,
        trust_all=trust_all if trust_all else None,
    )
    for skill in user:
        by_id.setdefault(skill.skill_id, skill)
    return sorted(by_id.values(), key=lambda s: s.name)


def _skill_source_action_goal(episode, pack_dict: dict) -> dict:
    """Carry opener + continuation chain into the SkillExecutionPack for Auto's guard.

    The opener recorded here is the build's ORIGIN, not the episode's raw
    provenance (Phase 4 cut 9 review). ``auto_loop.skill.run.skill_dispatch_
    confirmed`` reads it to answer "was this authorized by the operator", and an
    agent-lane episode is opened by ``agent_motor_dispatch`` while running work
    the operator asked for by name. Today the autonomous-execution default hides
    the difference; with ``OMEGA_AUTONOMOUS_SKILL_EXEC`` off — the posture where
    only operator-opened episodes self-authorize — the hop would have made every
    operator build unauthorized the moment it left the chat lane.
    """
    from pressure_loop.agent_lane import carried_origin_opened_by

    sag = dict(pack_dict.get("source_action_goal") or {})
    pressure = dict(getattr(episode, "pressure", None) or {})
    opened_by = str(getattr(episode, "opened_by", None) or pressure.get("opened_by") or "")
    sag.setdefault("opened_by", carried_origin_opened_by(episode) or opened_by)
    if opened_by == "execution_continuation":
        for c in pressure.get("contributors") or []:
            if c.get("id") != opened_by:
                continue
            detail = dict(c.get("detail") or {})
            if detail.get("prior_episode"):
                sag.setdefault("prior_episode", str(detail["prior_episode"]))
            if detail.get("continuation_depth") is not None:
                sag.setdefault("continuation_depth", int(detail["continuation_depth"]))
            break
    return sag


def attach_skill_execution_pack(episode, pack_dict: dict, *, catalog=None) -> Optional[dict]:
    """Phase 6O wiring — for a USE_SKILL episode, select the ExecutionSkill for the objective,
    build the SkillExecutionPack, embed it in the execution pack's inputs (so AUTO can load it)
    and attach it to episode.meta['skill_execution_pack'] (for the foldback). Returns the pack
    dict, or None if no skill matched (AUTO then surfaces missing_skill_pack). Only ever reached
    for a USE_SKILL action, i.e. when OMEGA_USE_SKILL_ROUTING is ON."""
    from schema.skill_execution_pack import SkillPackError, build_skill_execution_pack

    inputs = dict(pack_dict.get("execution_inputs") or {})
    objective = str(inputs.get("objective") or inputs.get("message") or "").strip()
    episode.meta = dict(getattr(episode, "meta", None) or {})
    if not objective:
        from memory.build_objective import current_build_objective

        prepared = current_build_objective()
        if prepared is not None:
            objective = prepared.objective
            episode.meta["prepared_build_objective"] = objective
        if not objective:
            episode.meta["skill_selection_failure"] = "no objective for skill selection"
            return None

    cat = catalog if catalog is not None else _selection_catalog()
    from memory.capability_catalog import capability_strengths_for_catalog

    strengths = capability_strengths_for_catalog(cat)
    # 7N: recall deterministic selection context (flag-OFF -> None -> byte-identical).
    recall_context = _maybe_recall_context(episode, objective)
    selection_objective = objective
    if str(pack_dict.get("action") or "") == "EXECUTE_MOTOR_PROGRAM":
        emergent = inputs.get("emergent_pressure")
        if isinstance(emergent, dict):
            proposed = emergent.get("proposed_action")
            proposed = proposed if isinstance(proposed, dict) else {}
            capabilities = [
                str(value).strip()
                for value in proposed.get("required_capabilities") or ()
                if str(value).strip()
            ]
            transformation = str(proposed.get("intended_transformation") or "").strip()
            prefix = (
                "build implement and verify"
                if set(capabilities) & {"building", "code_execution", "patching"}
                else ""
            )
            selection_objective = " ".join(
                value
                for value in (
                    prefix,
                    objective,
                    transformation,
                    " ".join(capabilities),
                )
                if value
            )
    selected_name = str(inputs.get("selected_skill") or "").strip()
    if selected_name:
        decision = select_named_skill(selected_name, objective=objective, catalog=cat)
    else:
        decision = select_skill_for_intent(
            objective=selection_objective,
            catalog=cat,
            strengths=strengths,
            recall_context=recall_context,
        )
    if not decision.selected:
        episode.meta["skill_selection_failure"] = decision.failure_reason or "no_matching_skill"
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "meta",
                "skill_selection",
                status="fail",
                summary=f"skill selection failed: {decision.failure_reason or 'no_matching_skill'}",
                episode_id=str(getattr(episode, "episode_id", "") or ""),
                failure_reason=decision.failure_reason or "no_matching_skill",
            )
        except Exception:
            pass
        return None

    # carry the episode's opener so Auto's guard can tell operator-authorized work (which
    # confirms its bounded writes) from autonomous work (which holds writes for confirmation).
    sag = _skill_source_action_goal(episode, pack_dict)
    try:
        from core.skill_execution_config import global_forbidden_tools

        skill_pack = build_skill_execution_pack(
            episode_id=str(getattr(episode, "episode_id", "") or pack_dict.get("episode_id") or ""),
            objective=objective,
            decision=decision,
            source_action_goal=sag,
            global_forbidden_tools=global_forbidden_tools(),
        )
    except SkillPackError as exc:
        episode.meta["skill_selection_failure"] = str(exc)
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "meta",
                "skill_selection",
                status="fail",
                summary=f"skill selection failed: {exc}",
                episode_id=str(getattr(episode, "episode_id", "") or ""),
                failure_reason=str(exc),
            )
        except Exception:
            pass
        return None

    sp_dict = skill_pack.to_dict()
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "meta",
            "skill_selection",
            status="ok",
            summary=(decision.reason or "")[:150],
            episode_id=str(getattr(episode, "episode_id", "") or ""),
            skill_id=decision.skill_id,
            score=f"{decision.score:.3f}",
        )
    except Exception:
        pass
    inputs["skill_execution_pack"] = sp_dict
    pack_dict["execution_inputs"] = inputs
    episode.meta["skill_execution_pack"] = sp_dict
    # 7N: attach the recall evidence + hashes alongside the pack (flag-ON only -> the key is
    # absent flag-OFF -> byte-identical). The frozen pack schema is untouched; this is a
    # sibling metadata entry in the episode meta, where the pack itself also lives.
    if recall_context is not None:
        episode.meta["skill_recall_context"] = {
            "query_hash": recall_context.get("query_hash", ""),
            "snapshot_hash": recall_context.get("snapshot_hash", ""),
            "evidence_event_ids": list(recall_context.get("evidence_event_ids") or []),
            "cautions": list(recall_context.get("cautions") or []),
            "applied_penalties": dict(recall_context.get("skill_penalties") or {}),
            "applied_boosts": dict(recall_context.get("skill_boosts") or {}),
            # The ARTIFACT recall — WHAT this kind of objective produced before — carried so
            # Omega's episode cognition can reason over "I've already built this," not just be
            # quietly more confident (operator 2026-06-25: the recall must reach Omega's
            # inference, not only bias skill selection).
            "prior_builds": list(recall_context.get("prior_builds") or []),
            # The FOLD-BACK recall — an UNFINISHED build's progress digest — carried so Omega
            # decides continue|finish and the resume directive re-grounds qwen (it isn't lost
            # when the model reloads).
            "build_in_progress": list(recall_context.get("build_in_progress") or []),
        }
    return sp_dict


def _maybe_recall_context(episode, objective: str) -> Optional[dict]:
    """7N: recall deterministic selection context for the objective when the taxonomy is
    wired into the live cascade (``taxonomy_enabled()``).

    Flag-OFF -> None (byte-identical, no recall, no graph read). Failure-soft: a recall /
    graph error degrades to None AND records a warning on the episode (spec 7N: "if recall
    fails, Meta can continue with degraded selection but must record warning"). Guardian-free.
    """
    from memory.taxonomy.config import taxonomy_enabled

    if not taxonomy_enabled():
        return None
    try:
        from memory.taxonomy.meta_recall import recall_skill_context

        return recall_skill_context(objective)
    except Exception as exc:  # noqa: BLE001
        episode.meta["skill_recall_warning"] = f"recall_failed: {type(exc).__name__}: {exc}"
        return None

"""QwenSkillExecutionDispatch — persisted compatibility schema for AUTO's brain directive.

Auto builds this from a SkillExecutionPack + the loaded SKILL.md body. It is the ONLY
thing the executor receives: the full skill markdown is pushed here (the brain never reads
skills/ directly). The ``qwen`` type and field names remain because durable Phase 6 records
already use them; they do not select a second model or transport.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


QWEN_SKILL_DISPATCH_SCHEMA_VERSION = "omega.qwen_skill_execution_dispatch.v1"

# The non-negotiable boundary statements every directive must carry (work-order 6H).
_COMMON_BOUNDARY_STATEMENTS = (
    "You are executing a bounded Omega Auto dispatch.",
    "You are Omega's current brain model on the existing chat seat; any Qwen names in legacy "
    "schema identifiers are compatibility labels, not another model or lane.",
    "Do not create new top-level goals.",
    "Do not continue beyond this dispatch.",
    "Use only the allowed tools.",
    "Do not use forbidden tools.",
    "Do not write canonical Omega memory.",
)

_DEVELOPMENT_BOUNDARY_STATEMENTS = (
    # The #1 failure mode is NARRATING a finished build instead of building it — a model prints a
    # report ("4 tests passed", a file tree) without ever calling write/run. These statements make
    # the work mandatory and the evidence ground-truthed to real tool calls.
    "ACTUALLY perform the work with your tools: WRITE the files (do not just print or describe "
    "their contents), run the commands, run the tests. Do NOT narrate, plan, or summarize what you "
    "WOULD do — DO it, step by step, with tool calls.",
    "Every file you report must be one you actually wrote with a tool; every test result must come "
    "from a test you actually ran. NEVER invent file names, test counts, or pass/fail numbers.",
    "A final summary or report that is not backed by real tool calls is a FABRICATION and is "
    "rejected as a failure — create the real artifacts first, then report ONLY what your tools "
    "actually returned.",
    "Return structured evidence matching the expected output schema, populated from your REAL tool "
    "results — not from claims.",
    # Environment hygiene (operator 2026-07-10): dependencies live in a project-local
    # venv or nowhere — never the shared interpreter, never pip --target into the
    # project. Kept to ONE line: the first paragraph-length version diluted the
    # ACT-with-tools imperative and the 4B regressed to narrating (live 19:27).
    "Prefer the standard library; if you must install packages, use a project-local "
    ".venv — never pip install into the system Python or --target the project dir.",
    # Incremental writes (2026-07-10): one mega-write blows the response budget
    # mid-JSON — the file lands truncated/corrupt and the run dies with no
    # closing message (observed live: a 500-line test file cut off mid-string).
    "Write files INCREMENTALLY: several small write_file/edit_file calls of "
    "roughly <=80 lines each — NEVER one giant write of a whole large file.",
)

_TERMINAL_BOUNDARY_STATEMENTS = ("Stop after success, blocker, safety denial, or max step limit.",)

# Backward-compatible exported name: these are the development-lane boundaries.
BOUNDARY_STATEMENTS = (
    _COMMON_BOUNDARY_STATEMENTS + _DEVELOPMENT_BOUNDARY_STATEMENTS + _TERMINAL_BOUNDARY_STATEMENTS
)

READ_ONLY_BOUNDARY_STATEMENTS = (
    _COMMON_BOUNDARY_STATEMENTS
    + (
        "READ-ONLY ANALYSIS ROUTE: inspect the requested target and return findings; do not "
        "modify or restore the workspace.",
        "Use real read, search, diff, or fetch tool results as factual evidence for every finding.",
        "An evidence-backed report completes the objective; workspace changes and test execution "
        "are not completion requirements unless the skill explicitly declares an allowed read-only "
        "verification tool.",
        "Do not call edit, write, command, test, restore, or other mutating tools.",
        "Return structured evidence matching the expected output schema, populated only from real "
        "observations rather than invented claims.",
    )
    + _TERMINAL_BOUNDARY_STATEMENTS
)


@dataclass(frozen=True)
class QwenSkillExecutionDispatch:
    episode_id: str
    dispatch_id: str
    skill_name: str
    skill_id: str
    skill_class: str
    execution_kind: str
    objective: str
    skill_markdown: str
    allowed_tools: tuple
    forbidden_tools: tuple
    safety: dict
    limits: dict
    evidence_required: tuple
    completion_criteria: tuple
    expected_output_schema: dict
    source_action_goal: dict
    source_skill_execution_pack: dict
    motor_program: dict = field(default_factory=dict)
    execution_route: str = "development"
    type: str = "qwen_skill_execution_dispatch"
    schema_version: str = QWEN_SKILL_DISPATCH_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "episode_id": self.episode_id,
            "dispatch_id": self.dispatch_id,
            "skill_name": self.skill_name,
            "skill_id": self.skill_id,
            "skill_class": self.skill_class,
            "execution_kind": self.execution_kind,
            "execution_route": self.execution_route,
            "objective": self.objective,
            "skill_markdown": self.skill_markdown,
            "allowed_tools": list(self.allowed_tools),
            "forbidden_tools": list(self.forbidden_tools),
            "safety": dict(self.safety),
            "limits": dict(self.limits),
            "evidence_required": list(self.evidence_required),
            "completion_criteria": list(self.completion_criteria),
            "expected_output_schema": dict(self.expected_output_schema),
            "source_action_goal": dict(self.source_action_goal),
            "source_skill_execution_pack": dict(self.source_skill_execution_pack),
            "motor_program": dict(self.motor_program),
            "schema_version": self.schema_version,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.dispatch_id:
            errors.append("missing dispatch_id")
        if not self.episode_id:
            errors.append("missing episode_id")
        if not self.skill_id:
            errors.append("missing skill_id")
        if not self.skill_markdown:
            errors.append("missing skill_markdown (skill body must be pushed to the executor)")
        if not self.objective:
            errors.append("missing objective")
        if self.execution_route not in {"read_only", "development"}:
            errors.append(f"unknown execution_route: {self.execution_route!r}")
        return errors


# The full DEV TOOLSET the executor always gets (operator doctrine 2026-06-25: "expose it
# to its dev toolset and let the system pick relevant skills to ENHANCE the process, not
# CONTROL the model"). Omega contract names that alias to the executor's tools (read_file,
# grep_search, edit_file, write_file, execute_command, …). Safety is the WORKSPACE JAIL +
# the dangerous-command denylist + the self_modification guard — NOT a per-skill whitelist.
_DEV_TOOLSET = (
    "read_files",
    "search_repo",
    "edit_file",
    "write_file",
    "run_command",
    "run_tests",
    "inspect_git_diff",
    "restore_project_from_memory",
)


def _execution_route(pack) -> str:
    safety_class = str((pack.safety or {}).get("safety_class") or "").strip().lower()
    return "read_only" if safety_class in {"read_only", "network_read"} else "development"


def _agent_toolset(
    pack_tools,
    *,
    execution_route: str,
    safety_class: str = "",
) -> tuple:
    declared = tuple(dict.fromkeys(tuple(pack_tools or ())))
    if execution_route == "read_only":
        return declared
    toolset = list(dict.fromkeys(declared + _DEV_TOOLSET))
    # A write_safe skill may edit bounded files, but it is not an arbitrary
    # shell authority. The dedicated test runner is still permitted because it
    # accepts only a recognized runner command and applies the portable Python
    # environment; arbitrary shell execution remains reserved for command_safe
    # or a higher class. Restoring a project is also an overwrite operation and
    # is reserved for the destructive/desktop routes.
    if safety_class == "write_safe":
        toolset = [
            name
            for name in toolset
            if name not in {"run_command", "restore_project_from_memory"}
        ]
    elif safety_class == "command_safe":
        toolset = [name for name in toolset if name != "restore_project_from_memory"]
    return tuple(toolset)


_PLAN_REQUIRED_TERMS = frozenset(
    {
        "plan",
        "planning",
        "roadmap",
        "design",
        "architecture",
        "architectural",
        "spec",
        "specification",
    }
)
_PLAN_EVIDENCE_CHECKS = frozenset({"plan", "plan_present"})


def _objective_requests_plan(objective: str) -> bool:
    words = set(re.findall(r"[a-z0-9]+", str(objective or "").lower()))
    return bool(words & _PLAN_REQUIRED_TERMS)


def _filter_motor_evidence_requirement(objective: str, value: Any) -> str | None:
    item = str(value or "").strip()
    if not item:
        return None
    normalized = "_".join(re.findall(r"[a-z0-9]+", item.lower()))
    if normalized in _PLAN_EVIDENCE_CHECKS and not _objective_requests_plan(objective):
        return None
    return item


def motor_authority(motor_program: dict) -> tuple[str, tuple, tuple, dict]:
    """Return directive authority derived solely from the canonical MotorProgram."""
    from schema.motor_program import MotorProgram

    program = MotorProgram.from_dict(motor_program)
    evidence = program.evidence_contract.to_dict()
    objective = program.outcome_contract.objective
    filtered_required = [
        filtered
        for raw in (
            evidence["required_artifacts"]
            + evidence["required_source_proof"]
            + evidence["required_checks"]
        )
        if (filtered := _filter_motor_evidence_requirement(objective, raw)) is not None
    ]
    required = tuple(dict.fromkeys(filtered_required))
    return (
        objective,
        required,
        tuple(program.outcome_contract.acceptance_criteria),
        evidence,
    )


_MOTOR_SOURCE_AUTHORITY_KEYS = frozenset(
    {
        "objective",
        "desired_outcome",
        "outcome",
        "acceptance_criteria",
        "constraints",
        "priorities",
        "pathway",
        "evidence_contract",
    }
)


def _sanitize_motor_source_action_goal(value: Any) -> dict:
    return {
        key: item
        for key, item in dict(value or {}).items()
        if key not in _MOTOR_SOURCE_AUTHORITY_KEYS
    }


def build_qwen_skill_dispatch(
    pack, skill, *, motor_program: dict | None = None
) -> QwenSkillExecutionDispatch:
    """Assemble the dispatch from a SkillExecutionPack + the loaded ExecutionSkill (whose
    body_markdown is pushed to the brain executor). Caller is responsible for validating that the
    skill matches the pack + is still enabled.

    Development skills get the full dev toolset. Read-only and network-read skills retain
    their exact declared tool envelope so analysis is an explicit terminal execution route,
    not a development run that happens to make no changes."""
    objective = pack.objective
    evidence_required = tuple(pack.evidence_required)
    completion_criteria = tuple(pack.completion_criteria)
    canonical_motor_program = dict(motor_program or {})
    execution_route = _execution_route(pack)
    source_action_goal = dict(pack.source_action_goal)
    source_skill_execution_pack = pack.to_dict()
    if canonical_motor_program:
        objective, evidence_required, completion_criteria, _ = motor_authority(
            canonical_motor_program
        )
        source_skill_execution_pack.update(
            objective=objective,
            evidence_required=list(evidence_required),
            completion_criteria=list(completion_criteria),
        )
        source_action_goal = _sanitize_motor_source_action_goal(source_action_goal)
        source_skill_execution_pack["source_action_goal"] = _sanitize_motor_source_action_goal(
            source_skill_execution_pack.get("source_action_goal")
        )
    return QwenSkillExecutionDispatch(
        episode_id=pack.episode_id,
        dispatch_id=pack.dispatch_id,
        skill_name=pack.selected_skill["skill_name"],
        skill_id=pack.selected_skill["skill_id"],
        skill_class=pack.selected_skill["skill_class"],
        execution_kind=pack.selected_skill["execution_kind"],
        objective=objective,
        skill_markdown=skill.body_markdown,
        allowed_tools=_agent_toolset(
            pack.allowed_tools,
            execution_route=execution_route,
            safety_class=str((pack.safety or {}).get("safety_class") or ""),
        ),
        forbidden_tools=tuple(pack.forbidden_tools),
        safety=dict(pack.safety),
        limits=dict(pack.limits),
        evidence_required=evidence_required,
        completion_criteria=completion_criteria,
        expected_output_schema=dict(pack.expected_output_schema),
        source_action_goal=source_action_goal,
        source_skill_execution_pack=source_skill_execution_pack,
        motor_program=canonical_motor_program,
        execution_route=execution_route,
    )


def _turn_budget_section() -> str:
    """Tell the executor its agentic TURN budget up front so it wraps up cleanly. When it cannot
    finish in the budget it must, BEFORE the limit, post a concise PROGRESS REPORT (done / remaining
    / blockers / next step) rather than stopping silently or rushing a broken final edit — the
    fold-back saves that report + the files and RESUMES the model from them. Budget is read from
    OMEGA_BRAIN_TOOL_TURNS, with QWEN_FOLD_TURNS retained as a compatibility fallback,
    default 25."""
    import os

    try:
        turns = int(
            os.environ.get("OMEGA_BRAIN_TOOL_TURNS")
            or os.environ.get("QWEN_FOLD_TURNS")
            or 25
        )
    except (TypeError, ValueError):
        turns = 25
    return (
        f"## Turn budget\n"
        f"You have about {turns} agentic turns for this dispatch. Aim to FINISH the task within them "
        f"(all tests passing is the goal). If you cannot finish, do NOT stop silently and do NOT rush "
        f"a broken final edit — a few turns BEFORE the limit, post a concise PROGRESS REPORT as your "
        f"message: what you completed, what remains, any blockers, and the exact next step. Your files "
        f"and that report are saved and you will be resumed to continue from them."
    )


def _read_only_turn_budget_section() -> str:
    """Bound an analysis run without importing build/test completion semantics."""
    import os

    try:
        turns = int(
            os.environ.get("OMEGA_BRAIN_TOOL_TURNS")
            or os.environ.get("QWEN_FOLD_TURNS")
            or 25
        )
    except (TypeError, ValueError):
        turns = 25
    return (
        "## Turn budget\n"
        f"You have about {turns} agentic turns for this read-only dispatch. Gather the required "
        "evidence, deliver the final findings and summary, then stop. If evidence is unavailable, "
        "return the specific blocker before the limit. No file change is needed for success."
    )


def build_directive(dispatch: QwenSkillExecutionDispatch) -> str:
    """Build the brain executor's full directive.

    The SKILL.md body is pushed inline, followed by the objective, bounded tool/safety/limit
    envelope, required evidence, output schema, and explicit boundary statements.  The class
    name is retained because persisted Phase 6 records use that schema identifier.
    """
    objective = dispatch.objective
    evidence_required = tuple(dispatch.evidence_required)
    completion_criteria = tuple(dispatch.completion_criteria)
    motor_evidence: dict | None = None
    if dispatch.motor_program:
        objective, evidence_required, completion_criteria, motor_evidence = motor_authority(
            dispatch.motor_program
        )

    lines: list[str] = []
    lines.append("# Omega Auto Skill Dispatch")
    # Legacy/diagnostic callers may provide a duck-typed dispatch without the additive
    # route field; those objects predate the split and retain development behavior.
    read_only = getattr(dispatch, "execution_route", "development") == "read_only"
    boundaries = READ_ONLY_BOUNDARY_STATEMENTS if read_only else BOUNDARY_STATEMENTS
    for s in boundaries:
        lines.append(f"- {s}")
    lines.append("")
    lines.append(f"episode_id: {dispatch.episode_id}")
    lines.append(f"dispatch_id: {dispatch.dispatch_id}")
    lines.append(
        f"skill: {dispatch.skill_name} ({dispatch.skill_class}, {dispatch.execution_kind})"
    )
    lines.append(f"execution_route: {getattr(dispatch, 'execution_route', 'development')}")
    lines.append("")
    lines.append(f"## Objective\n{objective}")
    lines.append("")
    # THE BAR, STATED UP FRONT (operator 2026-07-31). The mind already authors a
    # per-objective bar — outcome_contract.acceptance_criteria — but it used to render
    # as a bare comma-joined "## Completion criteria" tail line BELOW Allowed tools /
    # Forbidden tools / Safety / Limits, indistinguishable from a skill's boilerplate.
    # A 4B anchors on what LEADS. So the mind's criteria now sit with the Objective,
    # labelled as Omega's own statement of done, carrying the shortfall instruction
    # that lets a miss come back as a REPORT the mind decides on. This is what
    # replaced the retired deterministic evidence gate; see
    # auto_loop/execute/success_criteria.py.
    #
    # Provenance matters: only a MOTOR PROGRAM's acceptance_criteria are the mind's.
    # A skill's completion_criteria stay below as guidance — operator: skills
    # "should become modifiers/enhancers not law for a build".
    try:
        from auto_loop.execute.success_criteria import render_brief

        _brief = render_brief(
            dispatch.motor_program if dispatch.motor_program else None,
            execution_kind=dispatch.execution_kind,
            limits=None if read_only else dispatch.limits,
            allow_default=not read_only,
        )
    except Exception:
        _brief = ""
    if _brief:
        lines.append(f"## {_brief}")
        lines.append("")
    lines.append(_read_only_turn_budget_section() if read_only else _turn_budget_section())
    lines.append("")
    if dispatch.motor_program:
        lines.append("## Omega motor program")
        lines.append(
            "You may not change the objective, acceptance criteria, pathway, or evidence contract. "
            "Execute this program and return only factual limb evidence."
        )
        lines.append(json.dumps(dispatch.motor_program, ensure_ascii=False, sort_keys=True))
        lines.append("")
    # KEYWORD-MATCHED SKILLS — the operator-doctrine "skill choice for every command"
    # path: the top relevant skills from the FULL catalog (decoupled from the strict
    # single-skill selection above), injected as GUIDANCE/playbooks. The executor, with
    # its read/grep/edit/write/shell tools, applies the steps that fit. Failure-soft: a
    # matcher hiccup leaves the directive byte-identical to the legacy single-skill form.
    _matched = ""
    if not read_only:
        try:
            from auto_loop.skill.skill_index import skill_context_for_command

            _matched = skill_context_for_command(objective)
        except Exception:
            _matched = ""
    if _matched:
        lines.append("## Matched skills (guidance for this command)")
        lines.append(_matched)
        lines.append("")
    # Prompt construction is write-free. No project is selected yet, so restoring files here
    # could write into whichever workspace an earlier job recorded; the executor restores
    # through restore_project_from_memory after start_job(project_root) (AGENTS.md §7).
    # RESUME context (fold-back build loop): if a prior build for THIS objective YIELDED at a
    # turn-budget checkpoint, thread its progress digest so qwen picks up ORIENTED — it does not
    # redo finished files and is not lost when the model reloads (the context-compactor). Self-
    # contained: an unavailable report cannot be mistaken for no prior work.
    _resume = ""
    if not read_only:
        _resume = _resume_context_for(objective)
    if _resume:
        # A red battery flips the header: "already built part of this" under a refuted "done"
        # claim reads as "nothing left to do" (live 2026-07-03: five victory-echo cycles).
        if _resume.lstrip().startswith("- VERIFICATION FAILED"):
            lines.append(
                "## Resume — prior work FAILED verification. Fix the RED checks below; do NOT start over."
            )
        else:
            lines.append(
                "## Resume — a prior session already built part of this. CONTINUE it; do NOT start over."
            )
        lines.append(_resume)
        lines.append(
            "The file contents above are the workspace as it is now; a file marked missing may "
            "only need restoring. Select the project with `start_job`, then call "
            "`restore_project_from_memory` before editing, and continue once it returns ok."
        )
        # Convergence (live 2026-06-28): on resume the model kept RE-PLANNING and reorganizing —
        # moving fib.py into src/fib/, rewriting the layout each round — instead of finishing. The
        # weak "do NOT redo it" header was ignored. Pin the existing structure HARD so the model
        # continues the work rather than re-architecting it.
        lines.append(
            "These files ALREADY EXIST and their layout is FINAL. Do NOT move, rename, delete, or "
            "recreate any of them, and do NOT reorganize or re-architect the project structure — that "
            "decision is already made. Read the existing files first, then make ONLY the minimal "
            "changes needed to finish the remaining work and make the tests pass, then stop. Do not "
            "re-plan the project from scratch or second-guess the chosen layout."
        )
        lines.append("")
    _completed = ""
    if not read_only:
        _completed = _completed_project_context_for(objective)
    if _completed:
        lines.append("## Prior completed project")
        lines.append(_completed)
        lines.append(
            "Use this recalled artifact as the baseline when the operator asks to continue or "
            "update that completed project. The workspace may be fresh: before listing, reading, "
            "or editing the workspace, call `restore_project_from_memory` with the recalled "
            "project name or objective. That tool restores the latest verified dev-artifact "
            "snapshots, not the completed-space archive. Continue only after "
            "the tool returns ok, then make the requested continuation changes."
        )
        lines.append("")
    lines.append(f"## Allowed tools\n{', '.join(dispatch.allowed_tools) or '(none)'}")
    lines.append(f"## Forbidden tools\n{', '.join(dispatch.forbidden_tools) or '(none)'}")
    lines.append(f"## Safety\n{json.dumps(dispatch.safety, sort_keys=True)}")
    lines.append(
        f"## Limits\nmax_steps={dispatch.limits.get('max_steps')} "
        f"max_duration_ms={dispatch.limits.get('max_duration_ms')} "
        f"max_files_changed={dispatch.limits.get('max_files_changed')}"
    )
    if motor_evidence is not None:
        lines.append(
            "## Evidence required\n"
            + json.dumps(motor_evidence, ensure_ascii=False, sort_keys=True)
        )
    else:
        lines.append(f"## Evidence required\n{', '.join(evidence_required) or '(none)'}")
    # When a motor program is present these ARE the mind's acceptance criteria and have
    # already led the directive as the SUCCESS CRITERIA bar; otherwise they come from the
    # SKILL, which is a modifier — helpful shape, never the verdict on the build.
    if completion_criteria and not dispatch.motor_program:
        lines.append(
            "## Skill guidance — completion criteria the skill suggests\n"
            "Helpful shape from the skill, not the bar for this build: "
            + ", ".join(completion_criteria)
        )
    else:
        lines.append(f"## Completion criteria\n{', '.join(completion_criteria) or '(none)'}")
    lines.append("## Expected output schema")
    if read_only:
        lines.append(
            "Report ONLY what your allowed observation tools actually established. Every finding "
            "and summary claim must trace to real read/search/diff/fetch evidence; an evidence-backed "
            "report is the completed artifact for this route."
        )
    else:
        lines.append(
            "Report ONLY what your tools actually produced — every value here must reflect a real tool "
            "result (a file you wrote, a test that actually ran), never an invented one. Reporting "
            "success without the matching tool calls is rejected as a fabrication."
        )
    lines.append(json.dumps(dispatch.expected_output_schema, sort_keys=True))
    lines.append("")
    lines.append("## Skill (SKILL.md body)")
    lines.append(dispatch.skill_markdown)
    return "\n".join(lines)


def _project_record_ledger(objective: str) -> str:
    """File ledger from latest verified canonical artifact snapshots for this objective."""
    if not str(objective or "").strip():
        return ""
    try:
        from memory.dev_project_store import project_artifact_paths

        files = project_artifact_paths(objective)
        if not files:
            return ""
        more = f" (+{len(files) - 12} more)" if len(files) > 12 else ""
        return (
            f"  [project record — files your prior cycles produced: {', '.join(files[:12])}{more}]"
        )
    except Exception:
        return ""


def _resume_row(digest: str, files: Any) -> str:
    flist = ", ".join(str(f) for f in (files or [])[:8])
    line = f"- {digest}" if digest else "- (prior progress)"
    if flist:
        line += f"  [files already written: {flist}]"
    return line


def _completed_project_row(build: dict[str, Any]) -> str:
    objective = str(build.get("objective") or "").strip()
    summary = str(build.get("summary") or "").strip()
    files = ", ".join(str(f) for f in (build.get("files_changed") or [])[:10])
    parts = []
    if objective:
        parts.append(f"objective: {objective}")
    if summary:
        parts.append(f"summary: {summary}")
    if files:
        parts.append(f"files: {files}")
    return "- " + "; ".join(parts) if parts else "- (completed project recalled)"


def _completed_project_context_for(objective: str) -> str:
    """Recall successful prior build artifacts for continuation/update requests.

    This is distinct from ``_resume_context_for``: completed projects were already ported out
    of the scratch workspace, so the executor needs a memory-backed baseline rather than an
    unfinished progress digest.
    """
    from memory.taxonomy.config import taxonomy_enabled

    if not taxonomy_enabled():
        return ""
    from memory.taxonomy.meta_recall import recall_skill_context

    builds = list(recall_skill_context(objective).get("prior_builds") or [])[:3]
    rows = [_completed_project_row(b) for b in builds if isinstance(b, dict)]
    return "\n".join(rows)


def _workspace_file_state(
    files_changed: Any, *, max_per_file: int = 6000, max_total: int = 40000
) -> str:
    """The ACTUAL current contents of the build's files, read from the jailed workspace, so the
    executor resumes seeing EXACTLY what it already wrote — not a lossy digest (operator 2026-07-22).

    Read exactly the report's paths. A missing file is current evidence; a failed
    read retains its cause. A same-named file in another project is never a substitute.
    The prompt explicitly marks bounded excerpts and omitted files.

    The paths are persisted data, so each one is resolved under the operator's trusted
    folders before it is read; one outside them (a revoked grant, a malformed report) is
    marked unavailable, never read and never reported as missing.
    """
    from pathlib import Path

    from core.hud_path_guard import _operator_workspace_root, _operator_workspace_roots
    from core.trusted_folders import resolve_trusted_path

    names = [str(f).strip() for f in (files_changed or []) if str(f).strip()]
    if not names:
        return ""
    ws = _operator_workspace_root()
    ws = Path(ws) if ws else None
    if ws is None and any(not Path(name).is_absolute() for name in names):
        raise ValueError("relative resume file paths require a selected operator workspace")
    roots = tuple(dict.fromkeys((*_operator_workspace_roots(), *((ws,) if ws is not None else ()))))
    blocks: list[str] = []
    total = 0
    seen: set[str] = set()
    for name in names[:12]:
        try:
            path = resolve_trusted_path(name, active_root=ws, roots=roots)
        except ValueError:
            path = Path(name)
            text = "(unavailable — outside trusted folders)"
        else:
            text = None
        if str(path) in seen:
            continue
        seen.add(str(path))
        if text is None:
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                text = "(file missing)"
        if len(text) > max_per_file:
            text = text[:max_per_file] + f"\n… (truncated — {len(text)} chars total)"
        try:
            rel = path.relative_to(ws) if ws is not None else path
        except ValueError:
            rel = path
        block = f"── {rel} ──\n{text}"
        if blocks and total + len(block) > max_total:
            blocks.append("── (remaining files omitted — context budget) ──")
            break
        blocks.append(block)
        total += len(block)
    if len(names) > 12:
        blocks.append(f"── ({len(names) - 12} remaining files omitted — context budget) ──")
    return "\n".join(blocks)


def _resume_context_for(objective: str) -> str:
    """Render the current episode's complete SRAM report for a stateless builder rerun."""
    from memory.execution_report import current_execution_report

    report = current_execution_report()
    if report is None or report.build_task != str(objective or "").strip():
        return ""

    def _line(label: str, values: tuple[str, ...]) -> str:
        return f"- {label}: " + ("; ".join(values) if values else "(none reported)")

    verification = dict(report.verification or {})
    red = tuple(
        f"{check.get('name') or 'verification'}: {str(check.get('detail') or '')[:160]}"
        for check in (verification.get("checks") or ())
        if isinstance(check, dict) and not check.get("ok")
    )
    parts = [
        "EXECUTION REPORT — this is the complete cold-resume record from the prior run:",
        f"- BUILD TASK: {report.build_task}",
        _line("PASS GATE", report.pass_gate),
        _line("DID", report.did),
        _line("DID NOT", report.did_not),
        _line("SUCCEEDED", report.succeeded),
        _line("FAILED", report.failed),
        f"- SUMMARY: {report.summary or '(none reported)'}",
        f"- PROGRESS DIGEST: {report.progress_digest or '(none reported)'}",
    ]
    if report.original_request:
        parts.append(f"- OPERATOR'S ORIGINAL REQUEST: {report.original_request}")
    if red:
        parts.extend(
            [
                "- VERIFICATION IS RED — these unmet checks are part of the work order:",
                "    " + "; ".join(red),
            ]
        )
    elif verification.get("all_green"):
        parts.append("- VERIFICATION: all reported checks are green")
    if report.omega_reason:
        parts.append(f"- Omega's reason for re-execution: {report.omega_reason}")
    if report.omega_intent:
        parts.append(f"- Omega's re-execution intent: {report.omega_intent}")
    if report.recent_messages:
        parts.append("- BUILDER'S LAST MESSAGES:\n    " + "\n    ".join(report.recent_messages[-6:]))
    file_state = _workspace_file_state(report.files_changed)
    if file_state:
        parts.append(
            "- CURRENT STATE OF YOUR FILES (the actual contents on disk RIGHT NOW — edit THESE in "
            "place; do not recreate or re-architect what already exists):\n" + file_state
        )
        parts.append(
            "  Work from the CURRENT FILE CONTENTS above — verified current as of this dispatch.\n"
            "  Edit them IN PLACE and take the next constructive action against Omega's reason."
        )
    else:
        parts.append(
            "  Read the current workspace files first, then take the next constructive action "
            "against Omega's reason."
        )
    return "\n".join(parts)

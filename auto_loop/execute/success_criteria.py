"""The bar a build is judged against — AUTHORED BY THE MIND, not hardcoded.

A skill dispatch used to carry two different notions of success. The agent
decided it was finished on its own judgement; the mind then applied a fixed,
ledger-backed rule it had never told the agent. An agent could narrate a
complete build in good faith and fail closed as ``skill_no_evidence`` — live
2026-07-31 (episode b06b85ac): *"a WRITE skill reported success but wrote 0
files"*, 138 seconds spent, nothing banked.

The cure the operator chose is not a stricter gate. It is to make the criteria
part of the MIND'S OUTPUT: Omega states what success looks like for THIS
objective, from what it has learned, the agent is told that bar before it
starts, and the run is judged against it. Operator 2026-07-31:

    "take the deterministic gate out and add to the model's output for the
     agent what success looks like, that makes it a real learning process from
     knowledge it gained, it sets a bar for itself and then tries to reach it"

So the bar is per-objective and improves as Omega's knowledge does, instead of
being a constant the agent never sees.

WHERE THIS IS WIRED (all four seams, or the bar is just prose):

  1. AUTHORED — the mind already writes the bar: ``outcome_contract.acceptance_
     criteria`` is a model-authored field of the motor program (see
     omega_loop/motor/prompts.py). Nothing new was invented for it.
  2. DELIVERED - ``render_brief`` is called by ``schema.qwen_skill_dispatch.
     build_directive`` and placed WITH the Objective, above the tool/safety
     envelope. It used to render as a comma-joined ``## Completion criteria``
     tail line below Limits, indistinguishable from a skill's boilerplate; a 4B
     anchors on what leads.
  3. REPORTED — ``SHORTFALL_INSTRUCTION`` teaches one ``UNMET: <criterion> -
     <blocker>`` line per miss, and ``brain_skill_executor._model_summary`` lifts those into
     ``unresolved_blockers``. That parser/prompt pair is a CONTRACT; drift on
     either side silently drops the report (tests/core/test_success_criteria_
     directive.py pins it).
  4. DECIDED — a blocker forces satisfied=False / more_work_needed=True in the
     foldback verdict, and ``meta_loop.skill.foldback`` takes its skill_success +
     blockers branch: verification_needed, continuation pressure, episode stays
     OPEN for the mind. That branch already existed and was STARVED — the CLI
     bridge never populated the field it reads. Falling short is now a report the
     mind decides on, not a hard failure and not an unbounded retry.

A SKILL's completion_criteria are deliberately NOT a source here. Operator
2026-07-31: skills "should become modifiers/enhancers not law for a build, they
should never affect a build's outcome" — they render below, as guidance.

⚠️ WHAT THIS GIVES UP. The removed evidence gate existed for a measured reason
(operator 2026-06-27): qwen sometimes NARRATES a finished build — inventing file
lists and passing-test counts — without ever calling write/run, then declares
success. The ledger was the only thing that caught it. With the deterministic
rule gone, a fabricated done is caught only if the mind's authored criteria are
specific enough to expose it. That is the trade, taken deliberately.

An HONEST agent that misses the bar is now handled well (it reports, the mind
decides). A DISHONEST one that narrates a finished build and declares no blockers
is not caught here at all — the directive's anti-fabrication boundary statements
and the deterministic battery (core.build_verification) are what remain.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

# Kinds whose work is expected to land on disk. Retained ONLY to help the mind
# author a sensible default bar when it has not stated one — never used to fail
# a run.
MUTATING_KINDS = frozenset({"WRITE", "PATCH", "ARTIFACT", "REPAIR", "REFACTOR", "APPLY"})


def normalize_kind(execution_kind: Any) -> str:
    return str(execution_kind or "").strip().upper()


def mind_authored_criteria(pack: Optional[Mapping[str, Any]]) -> str:
    """The mind's own statement of done for this objective, if it made one.

    Accepts either a flat mapping carrying one of the criteria keys, or a whole
    MOTOR PROGRAM — whose ``outcome_contract.acceptance_criteria`` IS the mind's
    authored bar (a model-authored field; see omega_loop/motor/prompts.py). That
    is the live source: Omega already writes a per-objective bar there, it simply
    never reached the agent AS a bar.
    """
    if not isinstance(pack, Mapping):
        return ""
    contract = pack.get("outcome_contract")
    sources: list[Mapping[str, Any]] = [pack]
    if isinstance(contract, Mapping):
        sources.insert(0, contract)
    for source in sources:
        for key in ("success_criteria", "success_looks_like", "acceptance_criteria"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, (list, tuple)):
                items = [str(v).strip() for v in value if str(v).strip()]
                if items:
                    return "\n".join(f"- {item}" for item in items)
    return ""


def default_criteria(execution_kind: Any) -> str:
    """A minimal bar for when the mind authored none.

    Deliberately thin: this is a prompt-side hint so the agent still knows the
    shape of done, NOT a gate. Nothing here fails a run."""
    kind = normalize_kind(execution_kind)
    if kind in MUTATING_KINDS:
        return (
            "Write your changes to disk with the file tools — describing a change "
            "is not making it. State plainly at the end what you actually created "
            "or modified, and what you did not get to."
        )
    if kind == "TEST":
        return (
            "Run the tests rather than predicting their result, and report what "
            "actually passed and failed."
        )
    return ""


# The shortfall clause. Operator 2026-07-31: "if it doesn't reach it, it can then
# let omega mind know and the system can decide to try again or do something else
# instead of it just looping until it finally or never meets the endgoal." The
# agent cannot report a shortfall it was never told to look for, so the
# instruction ships WITH the bar.
SHORTFALL_INSTRUCTION = (
    "Before you finish, check your own work against the criteria above. If you met "
    "them all, say so and stop.\n"
    "If you fell short, do NOT keep circling. Stop, say which criteria you met, and "
    "end your final message with ONE LINE PER criterion you did not meet, in exactly "
    "this form:\n"
    "    UNMET: <the criterion, copied> - <what actually blocked it>\n"
    "Those lines become `unresolved_blockers` on your result: Omega reads them and "
    "decides whether to continue, change approach, or stop. Omit them entirely when "
    "you met the bar. Falling short and saying so is a REPORT, not a failure — "
    "claiming done when you are not is the only real failure."
)


def render_brief(
    pack: Optional[Mapping[str, Any]] = None,
    *,
    execution_kind: Any = "",
    limits: Optional[Mapping[str, Any]] = None,
    allow_default: bool = True,
) -> str:
    """The success section handed to the agent before it starts.

    Prefers the mind's authored criteria; falls back to the thin default so an
    agent is never dispatched with no notion of the target at all.
    ``allow_default=False`` suppresses that fallback for routes whose evidence is
    not files (read-only analysis), which must not be handed a write-shaped bar.
    """
    body = mind_authored_criteria(pack)
    authored = bool(body)
    if not body and allow_default:
        body = default_criteria(execution_kind)
    lines: list[str] = []
    if body:
        lines.append(body)
    max_files = int((limits or {}).get("max_files_changed") or 0)
    if max_files > 0:
        lines.append(f"Keep the change scoped to about {max_files} files.")
    if not lines:
        return ""
    header = (
        "SUCCESS CRITERIA — Omega's own statement of what done looks like for this "
        "objective. This is the bar you are working to:"
        if authored
        else "SUCCESS CRITERIA — what done looks like for this objective:"
    )
    rendered = "\n".join(line if line.startswith("-") else f"- {line}" for line in lines)
    return f"{header}\n{rendered}\n\n{SHORTFALL_INSTRUCTION}"


__all__ = [
    "MUTATING_KINDS",
    "SHORTFALL_INSTRUCTION",
    "default_criteria",
    "mind_authored_criteria",
    "normalize_kind",
    "render_brief",
]

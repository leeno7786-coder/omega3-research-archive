"""User-facing chat adapter for the pressure-led rebuild path."""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Mapping, Optional

from pressure_loop.contributors import (
    ALLOWED_INTENTS,
    from_operator_message,
    infer_operator_command_kind,
)
from pressure_loop.cognition_queue import (
    abandon_arrival_reservation,
    next_arrival_seq,
    reserve_plugin_permission_arrival,
    reserve_operator_arrival,
)
from pressure_loop.operator_pressure import (
    detect_redirect_directive,
    redirect_directive_enabled,
    seed_trajectory_from_operator_directive,
)
from pressure_loop.runtime import PressureRuntime, get_default_runtime, set_default_runtime

log = logging.getLogger("omega.pressure_loop.operator_chat")


def _directive_intents() -> frozenset[str]:
    from core.directives_config import allowed_directive_intents

    return allowed_directive_intents()


_OPERATOR_CHAT_PREVIEW: dict[str, int] | None = None


def _operator_chat_preview_cfg() -> dict[str, int]:
    global _OPERATOR_CHAT_PREVIEW
    if _OPERATOR_CHAT_PREVIEW is not None:
        return _OPERATOR_CHAT_PREVIEW
    defaults = {"memory_snippet_limit": 3, "text_compact_limit": 150}
    try:
        from config.loader import load_subsystem_bundle

        preview = (load_subsystem_bundle("operator_chat").get("config") or {}).get(
            "operator_chat", {}
        ).get("thinking_preview") or {}
        cfg = dict(defaults)
        for key in defaults:
            if key in preview:
                cfg[key] = int(preview[key])
        _OPERATOR_CHAT_PREVIEW = cfg
        return cfg
    except Exception:
        _OPERATOR_CHAT_PREVIEW = defaults
        return defaults


def _compact_text(value: Any, *, limit: int | None = None) -> str:
    if limit is None:
        limit = _operator_chat_preview_cfg()["text_compact_limit"]
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _public_reasoning_summary(
    *,
    route: dict[str, Any],
    prompt_pack: dict[str, Any],
    steps: list[dict[str, Any]],
    memory_count: int,
) -> str:
    """Describe the reasoning pass without exposing provider-private thinking."""
    if str(prompt_pack.get("response_mode") or "").strip().lower() != "reasoning":
        return ""
    clauses = ["Reviewed the request"]
    if memory_count:
        clauses.append(
            f"using {memory_count} recalled "
            f"{'memory hit' if memory_count == 1 else 'memory hits'}"
        )
    else:
        clauses.append("without recalled memory hits")
    tools = []
    for step in steps:
        name = str(step.get("tool") or "").strip()
        if name and name not in tools:
            tools.append(name)
    if tools:
        clauses.append(
            f"with {', '.join(tools[:3])}"
            + (" and other tools" if len(tools) > 3 else "")
        )
    action = str(route.get("action") or "").strip()
    if action:
        clauses.append(f"before composing the {action.lower()} response")
    return " ".join(clauses) + "."


def _public_reasoning_steps(
    *,
    route: dict[str, Any],
    prompt_pack: dict[str, Any],
    steps: list[dict[str, Any]],
    memory_scope: list[str],
    memory_count: int,
) -> list[dict[str, str]]:
    """Build the user-visible reasoning breakdown for a reasoning turn.

    The provider's hidden thinking channel is deliberately not copied into a
    HUD payload. These rows expose the observable decisions that grounded the
    answer: the classified intent, verified memory actually supplied, tools
    that ran, and the response action.
    """
    if str(prompt_pack.get("response_mode") or "").strip().lower() != "reasoning":
        return []

    intent = str(route.get("intent") or "reasoning").strip().lower()
    action = str(route.get("action") or "response").strip().lower()
    scope = ", ".join(scope for scope in memory_scope if scope) or "the configured recall lanes"
    if memory_count:
        memory_text = (
            f"Used {memory_count} verified {'memory hit' if memory_count == 1 else 'memory hits'} "
            f"from {scope}."
        )
    else:
        memory_text = "No verified memory hits were used for this response."

    tool_names: list[str] = []
    for step in steps:
        name = str(step.get("tool") or "").strip()
        if name and name not in tool_names:
            tool_names.append(name)
    tool_text = (
        f"Used {', '.join(tool_names)}."
        if tool_names
        else "No tools were used; the response stayed on the reasoning rail."
    )

    return [
        {"label": "Intent", "text": f"Treated this turn as {intent} and selected the {action} response path."},
        {"label": "Memory", "text": memory_text},
        {"label": "Tools", "text": tool_text},
        {"label": "Response", "text": "Composed the answer from the request and the verified context above."},
    ]


def _thinking_from_auto(
    *,
    auto: dict[str, Any],
    route: dict[str, Any],
    prompt_pack: dict[str, Any],
) -> dict[str, Any]:
    memory = dict(auto.get("memory") or {})
    raw_items = list(memory.get("items") or [])
    entries: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_items):
        if not isinstance(item, dict):
            continue
        memory_id = str(item.get("memory_id") or "").strip()
        if not memory_id:
            continue
        content = item.get("content") or item.get("summary") or item.get("text") or ""
        label = (
            item.get("title")
            or memory_id
            or item.get("id")
            or item.get("coord")
            or f"memory {idx + 1}"
        )
        coord = item.get("coord") or item.get("path") or ""
        preview = _compact_text(content, limit=140)
        if preview:
            entries.append(
                {
                    "label": _compact_text(label, limit=48),
                    "coord": _compact_text(coord, limit=48),
                    "preview": preview,
                }
            )

    memory_status = str(memory.get("memory_status") or "not_requested")
    memory_scope = [str(scope) for scope in memory.get("memory_scope") or []]
    # The work behind the answer (operator 2026-09-05): every tool the brain
    # called, with its result and the diff a write made. Provider-private
    # thinking never crosses this public payload boundary.
    # The socket streamed these live as chat.tool_call / chat.tool_result /
    # chat.reasoning_delta; riding the finished reply too means the poll path
    # and a reloaded HUD see the same trace.
    outputs = dict(auto.get("outputs") or {})
    reasoning = _compact_text(outputs.get("reasoning"), limit=2000)
    steps: list[dict[str, Any]] = []
    for row in list(outputs.get("operator_tool_calls") or []):
        if not isinstance(row, dict):
            continue
        steps.append(
            {
                "call_id": str(row.get("tool_call_id") or ""),
                "tool": str(row.get("tool_name") or ""),
                "args": dict(row.get("args") or {}),
                "ok": str(row.get("status") or "") == "success",
                "output": str(row.get("output") or "")[:4000],
                "error": str(row.get("error") or "")[:500],
                "diff": str(row.get("diff") or "")[:6000],
                "duration_ms": int(row.get("duration_ms") or 0),
            }
        )
        result_meta = row.get("result")
        setup = result_meta.get("browser_setup") if isinstance(result_meta, dict) else None
        if isinstance(setup, dict):
            steps[-1]["browser_setup"] = dict(setup)
        artifact_hold = row.get("dev_artifact_hold")
        if isinstance(artifact_hold, dict):
            steps[-1]["dev_artifact_hold"] = dict(artifact_hold)
        # The task's plan rows, so a reloaded HUD repaints the checklist from the
        # finished reply and not only from the live frame.
        plan = result_meta.get("plan") if isinstance(result_meta, dict) else None
        if isinstance(plan, list):
            steps[-1]["plan"] = [dict(step) for step in plan if isinstance(step, dict)]
    thinking = {
        "status": "memory_enriched" if entries else memory_status,
        "steps": steps,
        "reasoning_summary": _public_reasoning_summary(
            route=route,
            prompt_pack=prompt_pack,
            steps=steps,
            memory_count=len(entries),
        ),
        "reasoning_steps": _public_reasoning_steps(
            route=route,
            prompt_pack=prompt_pack,
            steps=steps,
            memory_scope=memory_scope,
            memory_count=len(entries),
        ),
        "memory": {
            "status": memory_status,
            "count": len(entries),
            "scope": memory_scope[:4],
            "entries": entries,
            "snippets": entries,
        },
        "prompt": {
            "prompt_type": prompt_pack.get("prompt_type"),
            "response_mode": prompt_pack.get("response_mode"),
        },
    }
    if isinstance(outputs.get("browser_workflow"), Mapping):
        # This is a host-authored coordination receipt, not page content.
        thinking["browser_workflow"] = dict(outputs["browser_workflow"])
    # The ordinary tool-ledger helper retains its internal trace for callers that
    # fold it into an episode. A reasoning-mode HUD payload gets only the grounded
    # summary/steps above, never provider-private prose.
    if str(prompt_pack.get("response_mode") or "").strip().lower() != "reasoning":
        thinking["reasoning"] = reasoning
    return thinking


def _runtime() -> PressureRuntime:
    try:
        return get_default_runtime()
    except RuntimeError:
        rt = PressureRuntime()
        set_default_runtime(rt)
        return rt


def _chat_ingress_class(
    text: str, *, directive: Optional[str] = None, intent: Optional[str] = None
) -> str:
    """Classify operator text for pressure ingress (not executive intent).

    Image and document creation never infer from message words — only from
    an explicit ``directive_intent`` sent by the HUD command affordance.
    """
    if directive in _directive_intents():
        return "operator_command"
    lowered = text.strip().lower()
    if not lowered:
        return "phatic"
    # A REDIRECT/pivot directive ("change of plan — focus on X") is an operator
    # command (it re-points the agenda), not chit-chat. Flag-gated: OFF keeps the
    # legacy classification (a redirect falls through to `phatic`). Checked before
    # the "?" branch so "switch to X?" still reads as a directive.
    if redirect_directive_enabled() and detect_redirect_directive(text):
        return "operator_command"
    # The deterministic speech-act detector recognises exactly two commands:
    # an explicit learning ask and an explicit "remember". Imperative work
    # ("build me X", "run the tests") and a go-ahead ("ok do it") are
    # CONVERSATION (operator 2026-09-05): they ride the RESPOND rail and the
    # chat brain decides, from the words and the recent turns, whether to call
    # a tool. Neither a verb regex nor an ``ack`` label routes a turn to a
    # build any more — which also retires the ack work-order-shape floor whose
    # only job was to keep a 26-word self-introduction labelled ``ack`` off the
    # command rail (first post-day-zero boot, 2026-07-19): nothing rides that
    # rail on a label now.
    if infer_operator_command_kind(lowered) is not None:
        return "operator_command"
    if lowered.endswith("?"):
        return "epistemic_request"
    # A definite non-task classification routes straight to a cascade response.
    if intent in ("question", "reasoning"):
        return "epistemic_request"
    if intent == "freeform":
        return "phatic"
    return "phatic"


# Deterministic build-steering classifier (fold-back, operator
# 2026-07-08). Conservative on purpose: only messages that clearly
# CHANGE the work steer the build; questions and chit-chat take the
# normal parked path and get answered at the next episode. No model in
# the loop — the classifier must behave identically every time.
def _report_unresolved_stop(cycles) -> None:
    """Say plainly that the stop is not durable yet. Never fails a halt that already landed."""
    log.error("the operator's stop is not durable for %s: it holds in this process and is "
              "retried, but a restart before it persists would let that work continue",
              list(cycles))
    try:
        from guardian.terminal_feedback import emit_data

        emit_data("pressure", "operator_stop_not_durable", summary=", ".join(cycles)[:200],
                  status="degraded", ok=False)
    except Exception:  # noqa: BLE001 - telemetry never fails the halt
        pass


def halt_operator_work(cycle_id: str = "", *, arrival_seq: Optional[int] = None) -> dict[str, Any]:
    """Stop what the operator can see running: the turn in flight and the jobs behind it.

    HUD Esc and the explicit stop control reach this function. Natural-language instructions
    remain ordinary Chat lane input and are classified by Global A.
    A job stays open between an agent task's batches, so interrupting a turn alone left the
    build running, and live 2026-09-15 the chat lane could not stop a job at all.

    When no running job belongs to the asking turn, every running job is stopped: the operator
    asked for a stop, and stopping nothing is the wrong reading of that.
    """
    from auto_loop.execute import job_control as jobs
    from pressure_loop.cognition_queue import next_arrival_seq
    from pressure_loop.operator_chat_responses import request_interrupt, request_work_halt
    from pressure_loop.pressure import record_work_stopped

    cycle = str(cycle_id or "").strip()
    if cycle:
        request_work_halt(cycle)
        request_interrupt(cycle)
    running = list(jobs.active_jobs())
    owned = [job for job in running if cycle and cycle in {job.cycle_id, job.owner_cycle_id}]
    targets = owned or running
    seq = arrival_seq if isinstance(arrival_seq, int) and arrival_seq > 0 else next_arrival_seq()
    stopped: list[str] = []
    for job in targets:
        for owner in (job.cycle_id, job.owner_cycle_id):
            # The batch in flight polls its own turn's work halt between calls.
            if owner:
                request_work_halt(owner)
                request_interrupt(owner)
        try:
            jobs.request_stop(job.job_id, source_cycle_id=cycle or job.cycle_id, arrival_seq=seq)
        except Exception:  # noqa: BLE001 - one unstoppable job never blocks the rest
            log.warning("stop request failed for job %s (failure-soft)", job.job_id, exc_info=True)
        else:
            stopped.append(job.job_id)
    # The halt is also DURABLE authority over the work's continuations (T12): a successor
    # staged, queued, in flight or republished after this moment must not resurrect what was
    # stopped, and the flags above are process-local with a lease. Recorded for the asking turn
    # and for every turn whose job was stopped — the same set the flags cover.
    #
    # The execution halt above has already landed, and it stands whatever the ledger does. But
    # a write that FAILED is not a cancellation that succeeded (review 2026-09-18): the stop is
    # retained in memory as authority for this process, retried by the pressure tick, and
    # REPORTED here — both to the caller and, loudly, to the operator's feed — rather than
    # silently discarded by a suppressed exception.
    halted_cycles = [cycle, *(owner for job in targets for owner in (job.cycle_id, job.owner_cycle_id))]
    record = record_work_stopped(halted_cycles, reason="operator stop")
    if record["unresolved"]:
        _report_unresolved_stop(record["unresolved"])
    return {"cycle_id": cycle, "stopped_jobs": stopped,
            "running_jobs": [job.job_id for job in running],
            "stop_recorded": not record["unresolved"],
            "unresolved_stops": list(record["unresolved"])}


# An explicit follow-up, not merely a sentence containing a pronoun. A live context
# plus a matching word does not establish a referent (review 2026-09-16): under the
# old rule "what time is it?" and "forget that, start a new project" both inherited
# the previous thread. The anaphor has to sit in object position after an action verb.
_FOLLOW_UP_VERB = (
    r"(?:do|redo|build|rebuild|make|remake|fix|run|rerun|continue|resume|finish|"
    r"complete|try|retry|apply|use|change|update|edit|add|remove|delete|rename|move|"
    r"test|deploy|commit|push|revert|undo|open|close|keep|ship|refactor)"
)
_ANAPHOR = r"(?:it|that|this|those|these|them|the\s+same)"
# The anaphor must be the verb's OBJECT — immediately after it — and must not read as a
# determiner. Allowing arbitrary text between verb and pronoun accepted "build a clock
# that displays UTC" and "run this SQL: SELECT 1" as follow-ups (review 2026-09-16), so
# the anaphor has to end the clause or be followed by a function word or a complement
# article ("now make it a pdf" is a real follow-up; "run this SQL" is not).
_FOLLOW_UP = re.compile(
    rf"^(?:now\s+|then\s+|also\s+|and\s+|please\s+|ok\s+|okay\s+)*{_FOLLOW_UP_VERB}\s+{_ANAPHOR}"
    r"(?=\s*(?:$|[.,;:!?]|(?:again|now|too|also|please|for|to|with|into|in|on|at|from|"
    r"and|then|instead|first|next|a|an|the)\b))",
    re.IGNORECASE,
)
# Whatever else the sentence says, these say the operator is LEAVING the thread.
_NEW_TOPIC = re.compile(
    r"\b(?:new|another|unrelated|different|instead|forget|nevermind|never\s+mind|"
    r"start\s+over|from\s+scratch|scratch\s+that|drop\s+that)\b",
    re.IGNORECASE,
)
# A turn that carries its OWN content is self-contained, whatever it looks like
# grammatically: in "run this: SELECT 1" and "make this a PDF: Hello world" the
# pronoun points FORWARD at material supplied in the message, not back at the previous
# turn, and both inherited an unrelated thread (review 2026-09-16). A colon with
# content after it, a quoted span, a code span or a line break all say the referent
# arrived with the request.
_INLINE_CONTENT = re.compile(r":\s*\S|[\"'`]|\n")


def _names_job(text: str, job_id: str) -> bool:
    """Whether ``text`` names ``job_id`` as a COMPLETE identifier, under the real job-id
    grammar (``job_control._ID``): job-777 is not job-7, and neither is job-7.backup. A dot
    that ENDS the sentence is punctuation, not part of the id: "add tests to job-7." names
    job-7, which the reference grammar already read that way (review-prep 2026-09-16)."""
    return bool(job_id) and re.search(
        rf"(?<![A-Za-z0-9._-]){re.escape(job_id)}(?![A-Za-z0-9_-])(?!\.[A-Za-z0-9._-])",
        text) is not None


# What an explicit job reference looks like even when no such job exists: an identifier
# joined to the word "job" (job-7, job_a.backup, job#12, job7) or a host-minted UUID, the
# shape of real job ids, which default to the operator turn's cycle id. Plain words such
# as "job" or "jobs" are not references.
_JOB_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9._-])"
    r"(?:job(?:[-_.#:][A-Za-z0-9]|[0-9])[A-Za-z0-9._-]*"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(?![A-Za-z0-9._-])",
    re.IGNORECASE,
)


# "for job cycle-xyz", "job id: 7f3e", "jobs job-a and job-b": the word names what follows
# as a job whatever its prefix (review 2026-09-16). Only an identifier-shaped word — one
# carrying a digit, dot, underscore or hyphen — or a known id counts, so "the job is done"
# and "nice job so far" name nothing.
_JOB_NAMED_AS = re.compile(
    r"(?<![A-Za-z0-9._-])jobs?(?:\s+id)?\s*[:#]?\s+[`'\"]?([A-Za-z0-9][A-Za-z0-9._-]*)",
    re.IGNORECASE,
)


def _job_references(text: str, known) -> tuple[set[str], set[str]]:
    """``(named, unresolved)``: the known job ids ``text`` names, and every explicit job
    reference in it that names none of them.

    An unresolved reference is still an ADDRESS — "for job-missing, add tests" points at a
    job, just not at one that exists. Reading it as "no job named" let a fallback select
    whichever build was running (review 2026-09-16). Sentence punctuation trailing a
    reference is not part of it.
    """
    known = {str(job_id) for job_id in known if job_id}
    named = {job_id for job_id in known if _names_job(text, job_id)}
    tokens = {match.group(0).rstrip("._-") for match in _JOB_REFERENCE.finditer(text)}
    tokens |= {
        token for token in (match.group(1).rstrip("._-") for match in _JOB_NAMED_AS.finditer(text))
        if token in known or re.search(r"[0-9._-]", token)
    }
    return named, {token for token in tokens if token and token not in known}


def _thread_for_job(job_id: str) -> str:
    """The thread a job runs in.

    The job's own record answers first: it is written when the job opens, so it holds
    while a batch is still EXECUTING, when no continuation is staged and the successor
    ledger has nothing to say (review 2026-09-16). The ledger — durable and fsynced —
    answers for a record opened before jobs recorded their thread.
    """
    if not job_id:
        return ""
    try:
        from auto_loop.execute import job_control as jobs

        recorded = jobs.get_job(job_id).thread_id
    except KeyError:
        recorded = ""
    except Exception:  # noqa: BLE001 - binding a thread never fails the operator's turn
        log.warning("could not read job %s while binding a thread", job_id, exc_info=True)
        recorded = ""
    if recorded:
        return recorded
    from pressure_loop.pressure import staged_signals

    for contributor in staged_signals() or ():
        meta = dict(getattr(contributor, "metadata", None) or {})
        if str(meta.get("agent_job_id") or "") == job_id:
            return str(meta.get("thread_id") or "")
    return ""


def _named_job_thread(text: str) -> str | None:
    """Resolve the thread of a job this turn names. Three distinct outcomes:

    ``None``  this turn names no job at all — binding continues to other rules.
    ``""``    it names one, but the reference is AMBIGUOUS or UNRESOLVED — binding
              stops here and the turn begins a new thread.
    a thread  exactly one thread is named, unambiguously.

    Collapsing the middle case into "no match" let "update job-7 and job-88 to use the
    same compiler" fall through and inherit an unrelated referential thread, and
    discarding unknown threads let one resolved job win while another named job's thread
    was unknown (review 2026-09-16). Ids match as COMPLETE identifiers under the real
    job-id grammar (``job_control._ID`` admits letters, digits, dot, underscore and
    hyphen): job-777 is not job-7, and neither is job-7.backup.
    """
    from pressure_loop.pressure import staged_signals

    threads: dict[str, str] = {}
    for contributor in staged_signals() or ():
        meta = dict(getattr(contributor, "metadata", None) or {})
        job_id = str(meta.get("agent_job_id") or "")
        if job_id:
            threads.setdefault(job_id, str(meta.get("thread_id") or ""))
    try:
        from auto_loop.execute import job_control as jobs

        for job in jobs.active_jobs() or ():
            job_id = str(getattr(job, "job_id", "") or "")
            recorded = str(getattr(job, "thread_id", "") or "")
            if job_id and recorded:
                # The job's record answers while its batch executes (T7).
                threads[job_id] = recorded
            elif job_id:
                threads.setdefault(job_id, "")
    except Exception:  # noqa: BLE001 - binding a thread never fails the operator's turn
        log.warning("could not read active jobs while binding a thread", exc_info=True)

    named_jobs, unresolved = _job_references(text, threads)
    if unresolved:
        # It names a job, but not one this binder can resolve: an unresolved reference
        # terminates binding exactly like an ambiguous one.
        return ""
    named = {threads[job_id] for job_id in named_jobs}
    if not named:
        return None
    # One thread, and it is actually known: anything else is an unresolved reference.
    return named.pop() if len(named) == 1 and "" not in named else ""


def _ingress_decision(clean: str, directive_intent) -> tuple:
    """Classify one operator turn and decide whether it may ANSWER a pending question.

    Returns ``(directive, chat_intent, ingress_class, answer_eligible,
    deterministic_command_kind)`` — the last one because the telemetry and contributor
    metadata downstream still report which deterministic command was recognised.

    ONE decision, computed once and reused by every path (review 2026-09-16). The
    paused branch used to approximate this rule with its own condition, and the two
    drifted: deterministic commands — "learn about volcanoes", "remember that my
    bicycle is blue" — inherited a pending agent question's thread while paused even
    though normal ingress excludes commands and directives from answering. Parity has
    to be structural, not two expressions that happen to agree.

    Everything here is deterministic: an explicit directive, the operator-command
    grammar, and ``classify_chat_intent`` (itself the deterministic floor). No model
    is queried, so this is safe to run before the pause branch.
    """
    directive = (directive_intent or "").strip().upper() or None
    if directive and directive not in ALLOWED_INTENTS:
        raise ValueError(f"unsupported directive_intent: {directive}")
    # DETERMINISTIC directive commands (IMAGE_CREATE / DOCUMENT_CREATE from HUD panels)
    # are obeyed verbatim by Global A and never select a RESPONSE prompt pack, so they
    # skip classification entirely. Explicit learn/remember speech acts are equally
    # deterministic.
    deterministic_command_kind = None if directive else infer_operator_command_kind(clean)
    if directive or deterministic_command_kind is not None:
        chat_intent = None
    else:
        from auto_loop.promptpack.intent_classifier import classify_chat_intent

        chat_intent = classify_chat_intent(clean)
    ingress_class = _chat_ingress_class(clean, directive=directive, intent=chat_intent)
    # Commands and directives never consume the pending query — they keep their own
    # semantics and the question stays open. An ack is the one command that answers.
    answer_eligible = directive is None and (
        ingress_class != "operator_command" or chat_intent == "ack")
    return (directive, chat_intent, ingress_class, answer_eligible,
            deterministic_command_kind)


def _bind_thread(text, pending, msg) -> str:
    """Select the existing thread this operator turn continues, or "" to begin one.

    T3 is a BINDER, never an identity generator (spec section 1): every value it can
    return already exists — recorded on the question, or carried by a staged successor,
    or named by the recent-route context. The host selects; a model may only help
    interpret the referent. Ambiguity resolves to a NEW thread, because a wrong
    continuation corrupts a thread while a spurious root merely fails to join one.
    """
    clean = str(text or "").strip()

    # An answer to the agent's own question continues the thread recorded ON the
    # question. ask_operator publishes during AUTO, before the asking episode is
    # journaled and before any successor is staged, so neither of those can be the
    # authority; successor admission also keeps only a digest. An explicit answer that
    # still cannot be resolved binds NOTHING — falling through to reference matching
    # bound answers to unrelated threads (review 2026-09-16).
    if pending and str((pending or {}).get("kind") or "") == "agent_question":
        thread = str((pending or {}).get("thread_id") or "")
        if thread:
            return thread
        return _thread_for_job(str(
            (msg or {}).get("job_id")
            or dict((pending or {}).get("meta") or {}).get("job_id")
            or ""))

    # A turn that names a job continues that job's thread. An ambiguous or unresolved
    # job reference TERMINATES binding — it must not fall through and inherit whatever
    # the recent context happened to describe (review 2026-09-16).
    if clean:
        named = _named_job_thread(clean)
        if named is not None:
            return named

    # An explicit referential follow-up continues the thread the recent-route context
    # DESCRIBES. The context and the journal have no identity linkage, so the newest
    # journal row is never the answer: with four lanes live it is routinely another
    # lane's thread, and reply delivery stamps the context at AUTO before the answering
    # episode archives.
    if (clean
            and _FOLLOW_UP.search(clean) is not None
            and _NEW_TOPIC.search(clean) is None
            and _INLINE_CONTENT.search(clean) is None):
        from global_loop.intent.context import previous_action, previous_thread

        if str(previous_action() or "").strip():
            return str(previous_thread() or "")

    # Anything else begins a new thread: EpisodeStore.open roots it at its own episode.
    return ""


def _agent_answer_metadata(text, pending) -> dict:
    if not pending or str(pending.get("kind") or "") != "agent_question":
        return {}
    fields = {
        "operator_route_pin": "CODING_AGENT",
        "agent_question_answer": str(text or "").strip()[:4000],
        "system_query_id": str(pending.get("question_id") or ""),
        "system_query_text": str(pending.get("question_text") or ""),
        "system_query_episode_id": str(pending.get("episode_id") or ""),
    }
    job_id = str(dict(pending.get("meta") or {}).get("job_id") or "")
    if job_id:
        from auto_loop.execute import job_control

        job = job_control.get_job(job_id)
        thread_id = str(pending.get("thread_id") or "")
        if thread_id and job.thread_id and thread_id != job.thread_id:
            raise ValueError("agent question and job belong to different threads")
        fields.update(job_id=job_id, job_cycle_id=job.cycle_id)
    return fields


def _question_answer_failure(cycle_id, error, reason) -> dict:
    message = f"Answer not delivered: {str(reason)[:500]}"
    return {
        "ok": False, "queued": False, "cycle_id": cycle_id,
        "message": message, "delivery": {"text": message, "rendered_by": "operator_chat"},
        "route": {"opened_by": "operator_response", "intent": None, "action": None},
        "prompt_pack": {}, "error": error,
    }


class QuestionNotPending(ValueError):
    """The selected question lost its pending transition before this answer attached."""


def _attach_question_answer(pending, text, *, cycle_id):
    """Attach once and prepare cognition-safe question context from the actual result."""
    if pending is None:
        return None
    from pressure_loop.system_messages import attach_chat_answer

    try:
        updated = attach_chat_answer(pending["question_id"], text, answer_episode_id=cycle_id)
        if updated is None:
            raise QuestionNotPending("the selected question is no longer pending")
        context = {
            **pending,
            "proactive_thread_id": str(updated.get("proactive_thread_id") or ""),
            "proactive_policy_id": str(updated.get("proactive_policy_id") or ""),
        }
        kind = str(pending.get("kind") or "")
        if kind in ("external_reminder", "send_approval_request"):
            context["question_text"] = ""
            context["external_kind"] = kind
            context["source_ref"] = str(pending.get("source_ref") or "")
            if kind == "send_approval_request":
                outcome = updated.get("send_outcome") or {}
                context["send_outcome"] = str(outcome.get("summary") or outcome.get("outcome") or "")
        return context
    except Exception:
        log.exception("system-query chat fold-back failed")
        raise


def _question_answer_metadata(question) -> dict:
    if question is None:
        return {}
    metadata = {
        "system_query_id": str(question["question_id"]),
        "system_query_text": str(question.get("question_text") or ""),
        "system_query_episode_id": str(question.get("episode_id") or ""),
        # WHAT was answered. A graded result presentation is the operator's verdict on that
        # episode's outcome; an answer to a question is a policy and grades nothing
        # (memory/operator_feedback.py). The chat box states no verdict, so none is carried.
        "system_query_kind": str(question.get("kind") or ""),
        "proactive_thread_id": str(question.get("proactive_thread_id") or ""),
        "proactive_policy_id": str(question.get("proactive_policy_id") or ""),
    }
    for key in ("external_kind", "source_ref", "send_outcome"):
        if question.get(key):
            metadata[key] = str(question[key])
    return metadata


def handle_operator_chat(text: str, **kwargs: Any) -> dict[str, Any]:
    """Hold transport arrival order through screening and durable admission."""
    cycle_id = str(kwargs.get("cycle_id") or uuid.uuid4()).strip()
    arrival_seq = reserve_operator_arrival(cycle_id, kwargs.get("arrival_seq"),
                                          existing_only=kwargs.pop("arrival_reserved", False))
    kwargs.update(text=text, cycle_id=cycle_id, arrival_seq=arrival_seq)
    try:
        result = _screen_and_enqueue_operator_chat(**kwargs)
    except QuestionNotPending as primary:
        abandon_arrival_reservation(arrival_seq, cycle_id=cycle_id)
        return _question_answer_failure(cycle_id, "question_not_pending", str(primary))
    except BaseException as primary:
        try:
            abandon_arrival_reservation(arrival_seq, cycle_id=cycle_id)
        except BaseException as cleanup:
            primary.add_note(f"Operator arrival cleanup failed: {cleanup!r}")
        raise
    else:
        # Successful enqueue consumed it atomically; rejected/error intake
        # explicitly frees only this turn's reservation.
        abandon_arrival_reservation(arrival_seq, cycle_id=cycle_id)
        return result


def _screen_and_enqueue_operator_chat(
    text: str,
    *,
    runtime: Optional[PressureRuntime] = None,
    cycle_id: Optional[str] = None,
    directive_intent: Optional[str] = None,
    weight: Optional[float] = None,
    format: Optional[str] = None,
    template: Optional[str] = None,
    upload_ref: Optional[str] = None,
    upload_refs: Optional[list[str]] = None,
    visual_intake: Optional[Mapping[str, Any]] = None,
    plugin_operation: Optional[Mapping[str, Any]] = None,
    arrival_seq: Optional[int] = None,
    question_id: Optional[str] = None,
    **_: Any,
) -> dict[str, Any]:
    """Validate and enqueue one operator chat turn for the cognition chain.

    Flow:
      1. Append the contributor to the durable FIFO — the single gate (§4.0).
      2. The pressure loop leases it; a successful episode open acknowledges it.
      3. AUTO delivers text/image/document output on the asynchronous response rail.

    ``arrival_seq`` is the order the operator actually SENT this turn, stamped
    at the HTTP boundary before any variable-latency work. Everything below —
    the SourceLedger write, the Guardian screen, the classifier call with its
    15 s timeout — runs concurrently on threadpool threads, so without it the
    line would record completion order rather than arrival order (§4.0 "in the
    order it arrived"; audit §1.4). Callers with no ordering context may omit
    it and get plain FIFO append.

    ``directive_intent`` is required for deterministic IMAGE_CREATE /
    DOCUMENT_CREATE routing from HUD command panels. Global A obeys it
    verbatim and never re-infers those intents from message text.
    """
    clean = (text or "").strip()
    if not clean:
        raise ValueError("text required")
    if plugin_operation is not None and not isinstance(plugin_operation, Mapping):
        raise ValueError("plugin_operation must be an object")
    # NOTE: the operator turn is NOT appended to the hot buffer here anymore. If it were,
    # the HOT recall lane would score it THIS turn (the "ingress echo") — the same
    # backwards anomaly as fact ingest. The user turn is now held in the SRAM bucket and
    # graduated to the hot buffer at DeepSleep/close (memory.episode_sram), after recall.

    if arrival_seq is None:
        arrival_seq = next_arrival_seq()
    cycle_id = str(cycle_id or uuid.uuid4()).strip()
    if not cycle_id:
        raise ValueError("cycle_id required")

    # Help is a deterministic operator command. It must paint in the chat
    # feed immediately instead of spending a model turn (or disappearing when
    # a provider returns an empty completion).
    if clean.casefold() == "/help":
        from hud.qwen_skills import chat_help_message

        message = chat_help_message()
        return {
            "ok": True,
            "queued": False,
            "cycle_id": cycle_id,
            "message": message,
            "delivery": {"text": message, "rendered_by": "operator_command"},
            "route": {"opened_by": "operator_command", "intent": "RESPOND", "action": "RESPONSE"},
            "prompt_pack": {"command": "/help"},
        }

    from auto_loop.execute.chat_permissions import handle_chat_permission_command

    permission_command = handle_chat_permission_command(clean)
    if permission_command is not None:
        message = str(permission_command.get("message") or "")
        try:
            from guardian.terminal_feedback import emit_data

            status = dict(permission_command.get("status") or {})
            emit_data(
                "pressure",
                "chat_permission_mode",
                status="changed" if permission_command.get("changed") else "read",
                ok=True,
                command=str(permission_command.get("command") or ""),
                mode=str(status.get("mode") or ""),
                label=str(status.get("label") or ""),
                cycle_id=cycle_id,
            )
        except Exception:
            log.debug("chat permission audit event failed", exc_info=True)
        return {
            "ok": True,
            "queued": False,
            "cycle_id": cycle_id,
            "message": message,
            "delivery": {"text": message, "rendered_by": "chat_permission"},
            "route": {"opened_by": "operator_command", "intent": "RESPOND", "action": "RESPONSE"},
            "prompt_pack": {"command": permission_command.get("command")},
            "chat_permission": dict(permission_command.get("status") or {}),
        }

    # Trusted-folder controls are explicit operator commands, not model work.
    # Handle them before SourceLedger, classification, memory recall, or the
    # cognition queue so a path grant can never be inferred from prose.
    try:
        from core.trusted_folders import handle_trusted_folder_command

        trusted_command = handle_trusted_folder_command(clean)
    except Exception:
        log.exception("trusted-folder command handling failed")
        raise
    if trusted_command is not None:
        message = str(trusted_command.get("message") or "")
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "trusted_folder_command",
                status="ok" if trusted_command.get("ok") else "failed",
                ok=bool(trusted_command.get("ok")),
                command=clean.split(None, 1)[0].casefold() if clean else "",
                cycle_id=cycle_id,
            )
        except Exception:
            log.debug("trusted-folder command audit failed", exc_info=True)
        return {
            "ok": bool(trusted_command.get("ok")),
            "queued": False,
            "cycle_id": cycle_id,
            "message": message,
            "delivery": {"text": message, "rendered_by": "trusted_folders"},
            "route": {"opened_by": "operator_command", "intent": "RESPOND", "action": "RESPONSE"},
            "prompt_pack": {},
            "trusted_folders": dict(trusted_command),
        }

    # An explicit /skill selection is the operator naming the agent skill for this task.
    # It never reaches Global A's intent router: the task rides USE_SKILL into the agent
    # lane and Meta A binds exactly that skill. A selection that cannot run is answered
    # here, before SourceLedger or the cognition queue.
    selected_skill = ""
    if not directive_intent:
        from hud.qwen_skills import resolve_skill_command

        skill_command = resolve_skill_command(clean)
        if skill_command is not None:
            if not skill_command["ok"]:
                message = str(skill_command["message"])
                return {
                    "ok": True,
                    "queued": False,
                    "cycle_id": cycle_id,
                    "message": message,
                    "delivery": {"text": message, "rendered_by": "skill_command"},
                    "route": {"opened_by": "operator_command", "intent": None, "action": None},
                    "prompt_pack": {},
                }
            directive_intent = "USE_SKILL"
            selected_skill = str(skill_command["skill"])
            clean = str(skill_command["objective"])

    try:
        from memory.source_ledger.store import record_operator_chat_source

        source_citation = record_operator_chat_source(clean, cycle_id=cycle_id)
    except Exception as exc:
        log.warning("operator chat SourceLedger capture failed: %s", exc)
        return {
            "ok": False,
            "queued": False,
            "message": "",
            "delivery": {"text": "", "rendered_by": "none"},
            "route": {"opened_by": "operator", "intent": None, "action": None},
            "prompt_pack": {},
            "error": "source_ledger_unavailable",
        }

    from guardian.external_ingress import screen_text

    screened, guardian_decision = screen_text(
        clean,
        source="operator_chat",
        field="message",
    )
    if guardian_decision != "allow":
        return {
            "ok": False,
            "queued": False,
            "message": "",
            "delivery": {"text": "", "rendered_by": "none"},
            "route": {"opened_by": "operator", "intent": None, "action": None},
            "prompt_pack": {},
            "error": (
                "guardian_quarantined" if guardian_decision == "quarantine" else "guardian_withheld"
            ),
        }
    clean = screened

    # Deterministic attachment interpretation is part of the episode, not a HUD
    # response shortcut.  It runs only after the external chat text has a ledger
    # citation and Guardian ALLOW.  The result rides the contributor into AUTO so
    # normal response delivery, execution evidence, foldback, and DeepSleep all
    # observe the same output.
    attachment_refs: list[str] = []
    for ref in [upload_ref, *(upload_refs or [])]:
        cleaned_ref = str(ref or "").strip()
        if cleaned_ref and cleaned_ref not in attachment_refs:
            attachment_refs.append(cleaned_ref)
    if not upload_ref and attachment_refs:
        upload_ref = attachment_refs[0]

    # Only payload validation belongs to transport. Interpretation and memory
    # admission run at AUTO with this turn's pressure-owned SRAM binding.
    from pressure_loop.operator_vision import prepare_visual_intake

    queued_visual = prepare_visual_intake(visual_intake, upload_refs=attachment_refs)

    # Operator-provided source URL (operator 2026-07-22: "make any url i give it
    # plausible"). ANY URL in the operator's text — a directive, or an answer to
    # Omega's own ASK for a source — is stashed as the authoritative fetch target
    # for the next LEARN, so web_fetch direct-fetches it instead of fuzzy-resolving
    # the topic phrase to Wikipedia. One-shot + fail-soft: capture never blocks a
    # chat turn and self-clears once the next LEARN consumes it.
    try:
        from pressure_loop import learn_source_url

        learn_source_url.capture_from_text(clean)
    except Exception:
        log.debug("operator source-url capture failed", exc_info=True)

    # PERCEPTION BEFORE CONSOLIDATION, structurally (operator 2026-07-16): the operator's
    # stated facts + this turn are NOT written to system memory here. They ride the
    # contributor into the episode, are HELD in the per-episode SRAM bucket
    # (memory.episode_sram, opened at episode open), and flush into system memory ALL AT
    # ONCE at DeepSleep/close — after this episode's recall. So this turn's recall reads
    # only prior memory and can never surface this turn's own intake ("Omega remembers
    # something before it's told"). No pre-cascade write, no recall-time filter band-aid.

    # User input resets the idle clock (operator 2026-07-09) — the mind must not
    # auto-wake mid-conversation. Every accepted turn enters the durable queue;
    # command handling additionally wakes the loop at enqueue time.
    try:
        from pressure_loop import idle_wake

        idle_wake.record_activity("operator")
    except Exception:
        pass

    # Classify and decide answer eligibility BEFORE the pause branch, so the paused and
    # the normal path share one decision instead of two approximations of it.
    (directive, chat_intent, ingress_class, answer_eligible,
     deterministic_command_kind) = _ingress_decision(clean, directive_intent)

    pending_question = None
    answer_metadata = {}
    if answer_eligible:
        from pressure_loop.system_messages import current_outreach

        pending_question = (current_outreach() if question_id is None
                            else current_outreach(question_id=question_id))
        if question_id is not None and pending_question is None:
            return _question_answer_failure(
                cycle_id, "question_not_pending", "the selected question is not pending")
        try:
            answer_metadata = _agent_answer_metadata(clean, pending_question)
        except Exception as exc:
            log.warning("agent question job could not be resolved", exc_info=True)
            return _question_answer_failure(
                cycle_id, "agent_question_job_unavailable", f"{type(exc).__name__}: {exc}")

    from pressure_loop.pressure_loop_pause import is_pressure_loop_paused

    if is_pressure_loop_paused():
        # A pause means the LINE HOLDS — it does not mean the turn evaporates
        # (AGENTS.md §4.0 "nothing is lost"). This used to return without
        # queueing, after the SourceLedger citation had already been written,
        # so the utterance was recorded as received and then discarded and the
        # operator had to retype it (audit §1.10). The queue is durable
        # precisely so that waiting costs nothing.
        from pressure_loop.cognition_queue import enqueue_input

        paused_payload: dict[str, Any] = {
            "message": clean,
            "ingress_class": "answer_to_question" if pending_question else "epistemic_request",
            "weight": 1.0,
            "cycle_id": cycle_id,
            "arrival_seq": arrival_seq,
            "source_citation": source_citation,
            "chat_bubble_waiting": True,
        }
        paused_payload.update(answer_metadata)
        if queued_visual:
            paused_payload["visual_intake"] = queued_visual
        from auto_loop.execute.chat_permissions import load_chat_permission_mode

        paused_payload["chat_permission_mode"] = load_chat_permission_mode()
        if directive_intent:
            paused_payload["directive_intent"] = directive_intent
        if selected_skill:
            paused_payload["selected_skill"] = selected_skill
        if plugin_operation is not None:
            paused_payload["plugin_operation"] = dict(plugin_operation)
        for key, value in (
            ("format", format),
            ("template", template),
            ("upload_ref", upload_ref),
        ):
            if value:
                paused_payload[key] = value
        paused_question = _attach_question_answer(pending_question, clean, cycle_id=cycle_id)
        paused_payload.update(_question_answer_metadata(paused_question))
        paused_thread = _bind_thread(clean, paused_question, paused_payload)
        if paused_thread:
            paused_payload["thread_id"] = paused_thread
        paused_turn = from_operator_message(paused_payload)
        queued_while_paused = enqueue_input(paused_turn, operator_activity=True, admission_cycle_id=cycle_id)
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "operator_chat",
                status="hold",
                summary="substrate_paused; turn holds its place in line",
                ok=True,
                queued=queued_while_paused,
                cycle_id=cycle_id,
                opened_by="operator",
            )
        except Exception:
            pass
        return {
            "ok": True,
            "queued": queued_while_paused,
            "cycle_id": cycle_id,
            "message": "",
            "delivery": {"text": "", "rendered_by": "none"},
            "route": {"opened_by": "operator", "intent": None, "action": None},
            "prompt_pack": {},
            "status": "substrate_paused",
        }

    # Routing belongs to Global A: its intent router reads this turn, with the
    # previous assistant action, when the episode opens, and a browser route binds
    # the open workflow there. Ingress handles only the explicit close of an open
    # browser workflow, before cognition is enqueued, so a close phrase cannot
    # trigger another browser action or episodic recall.
    try:
        from meta_loop.browser.workflow import (
            is_browser_completion_command,
            load_browser_workflow,
            prepare_browser_turn,
        )

        stored_workflow = load_browser_workflow()
        closable_workflow = stored_workflow and stored_workflow.get("status") in {
            "active",
            "waiting_for_operator",
            "blocked",
        }
        if closable_workflow and is_browser_completion_command(clean):
            closed = prepare_browser_turn(clean, cycle_id=cycle_id)
            browser_workflow_receipt = dict(closed.get("workflow") or {})
            message = "Browser workflow closed."
            return {
                "ok": True,
                "queued": False,
                "cycle_id": cycle_id,
                "message": message,
                "delivery": {"text": message, "rendered_by": "browser_workflow"},
                "route": {"opened_by": "operator_command", "intent": "RESPOND", "action": "RESPONSE"},
                "prompt_pack": {},
                "browser_workflow": browser_workflow_receipt,
            }
    except Exception:
        log.exception("browser workflow ingress failed")
        raise

    # Chat intent / ingress class / answer eligibility were decided ONCE above, before
    # the pause branch (_ingress_decision). They are threaded to the prompt pack via the
    # contributor metadata (msg["chat_intent"] below) so AUTO never re-classifies and the
    # memory recall stays deterministic (operator 2026-06-26).

    # ── System-query fold-back (operator 2026-07-10) ─────────────────────────
    # A conversational turn that arrives while a system-initiated query is
    # PENDING is the operator answering it (latest-open-wins — the same rule
    # the open-questions journal codifies). Consume the pending query: mark
    # the system message + journal answered, flip ingress to
    # answer_to_question so the turn rides the existing RESOLVE_QUERY rail,
    # and thread the Q+A pair onto the contributor so the reply and the chat
    # fold ground on the system's own question instead of treating the answer
    # as a fresh topic. Commands and directives never consume the pending
    # query — they keep their own semantics and the question stays open.
    pending_system_query: Optional[dict[str, Any]] = None
    if answer_eligible:
        pending_system_query = _attach_question_answer(pending_question, clean, cycle_id=cycle_id)
        if pending_system_query:
            ingress_class = "answer_to_question"

    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "pressure",
            "classify_chat_intent",
            summary=(
                f"classification={chat_intent}"
                if chat_intent is not None
                else f"classification=deterministic:{deterministic_command_kind or directive or 'fallback'}"
            ),
            chat_intent=(chat_intent or deterministic_command_kind or directive or "fallback"),
            ok=(
                chat_intent is not None
                or deterministic_command_kind is not None
                or directive is not None
            ),
            cycle_id=cycle_id,
        )
    except Exception:
        pass

    try:
        from guardian.terminal_feedback import emit_phase

        emit_phase(
            "pressure",
            "operator_chat",
            status="enter",
            summary=_compact_text(clean, limit=80),
            ingress_class=ingress_class,
            directive_intent=directive,
        )
    except Exception:
        pass

    rt = runtime or _runtime()
    shared_state = rt.shared_state

    # The LAST_INGRESS_PRESSURE mirror is NOT published. It was a second gate:
    # a single SharedState slot, so each new turn overwrote the last
    # (newest-wins) against the queue's oldest-first, and the contributor
    # rebuilt from it out-ranked the durable queued turn at ignition and
    # replaced its metadata with a strict subset (AGENTS.md §4.0; audit §1.2).
    # Everything the operator's turn carries now travels on ONE object: the
    # contributor built from `msg` below and enqueued into the durable line.

    msg: dict[str, Any] = {
        "message": clean,
        "ingress_class": ingress_class,
        "weight": 1.0,
        "source_citation": dict(source_citation),
        "cycle_id": cycle_id,
        # Send order, stamped before any variable-latency work (§4.0).
        "arrival_seq": arrival_seq,
        # The HUD chat send always opens a bubble that polls /api/chat/responses
        # on this cycle_id. The close-time RESOLVE_QUERY delivery reads this to
        # resolve the bubble instead of routing to the proactive feed (which is
        # the rail for PANEL answers, where no bubble waits).
        "chat_bubble_waiting": True,
    }
    from auto_loop.execute.chat_permissions import load_chat_permission_mode

    # Snapshot the user mode with the queued turn. A later global mode change
    # must not silently reinterpret work that was already sent.
    msg["chat_permission_mode"] = load_chat_permission_mode()
    msg.update(answer_metadata)
    msg.update(_question_answer_metadata(pending_system_query))
    if chat_intent:
        msg["chat_intent"] = (
            chat_intent  # threaded to AUTO's prompt-pack selection (no re-classify)
        )
    if directive:
        msg["directive_intent"] = directive
    if selected_skill:
        msg["selected_skill"] = selected_skill
    if format:
        msg["format"] = str(format).lower()
    if template:
        msg["template"] = str(template)
    if upload_ref:
        msg["upload_ref"] = str(upload_ref)
    if queued_visual:
        msg["visual_intake"] = queued_visual
    if plugin_operation is not None:
        if not isinstance(plugin_operation, Mapping):
            raise ValueError("plugin_operation must be an object")
        msg["plugin_operation"] = dict(plugin_operation)
    if weight is not None:
        msg["weight"] = float(weight)

    # THREAD IDENTITY (spec section 1, T3). The host selects which existing thread this
    # turn continues; stamped only when one resolved, so absence means exactly "begin a
    # new thread" and EpisodeStore.open roots it at this episode.
    bound_thread = _bind_thread(clean, pending_system_query, msg)
    if bound_thread:
        msg["thread_id"] = bound_thread

    contributor = from_operator_message(msg)

    # ── Chat text NEVER runs the cascade on the operator's thread (operator
    # 2026-07-03: "fold it back the same way every other input gets folded back —
    # run the cascade after the action"). Enqueue as top pressure for the next
    # episode; the reply delivers AT AUTO (cognition_chain._deliver_chat_reply_at_auto
    # resolves the bubble the moment the text exists) and Meta B → Global B →
    # Omega identity fold → DeepSleep run BEHIND the delivered reply. The HUD
    # resolves this queued shape via the /api/chat/responses poll keyed on
    # cycle_id. Text, image, and document results all return through that rail.
    from pressure_loop.cognition_queue import enqueue_input

    enqueue_input(contributor, operator_activity=True, admission_cycle_id=cycle_id)
    # A REDIRECT/pivot directive ALSO seeds the trajectory immediately (approach A,
    # flag-gated): the directive text is free-text guidance META_A reads next cycle,
    # so the system re-points NOW instead of waiting the ~10-episode window. Omega
    # still owns the real subcategory + the final `alter` and supersedes this seed
    # at window close. (Restored 2026-09-05: it left with the retired ack path.)
    if redirect_directive_enabled() and detect_redirect_directive(clean):
        seed_trajectory_from_operator_directive(shared_state, {"text": clean, "domain": ""})
    queued_action = {
        "IMAGE_CREATE": "IMAGE_CREATION",
        "DOCUMENT_CREATE": "DOCUMENT_CREATION",
    }.get(str(directive or ""))
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "pressure",
            "operator_chat",
            status="ok",
            summary="chat_queued_deliver_at_auto",
            ok=True,
            opened_by=contributor.type,
            cycle_id=cycle_id,
        )
    except Exception:
        pass
    return {
        "ok": True,
        "queued": True,
        "cycle_id": cycle_id,
        "message": "",
        "delivery": {"text": "", "rendered_by": "queued"},
        "route": {
            "opened_by": contributor.type,
            "intent": directive,
            "action": queued_action,
        },
        "prompt_pack": {},
        "error": "",
    }

def handle_plugin_permission_decision(
    *,
    request_id: str,
    expected_revision: str,
    decision: str,
    arrival_seq: Optional[int] = None,
) -> dict[str, Any]:
    """Stamp and enqueue a structured permission decision; never ignite or act."""
    from pressure_loop.plugin_continuations import (
        PluginPermissionDecision,
        enqueue_plugin_permission_decision,
    )

    stamped_arrival = next_arrival_seq() if arrival_seq is None else arrival_seq
    try:
        reserve_plugin_permission_arrival(
            request_id,
            expected_revision,
            stamped_arrival,
        )
        queued = enqueue_plugin_permission_decision(
            PluginPermissionDecision(
                request_id=request_id,
                expected_revision=expected_revision,
                decision=decision,
            ),
            arrival_seq=stamped_arrival,
        )
    except BaseException as exc:
        try:
            abandon_arrival_reservation(
                stamped_arrival,
                request_id=request_id,
                expected_revision=expected_revision,
            )
        except BaseException as cleanup_error:
            exc.add_note(f"plugin_permission_arrival_cleanup_failed={cleanup_error!r}")
        raise
    return {"ok": True, "queued": queued, "request_id": request_id}

"""Per-episode Omega contract types (Slice 1 — data plumbing only)."""

from __future__ import annotations

import json

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from schema.omega_control_decision import OMEGA_LANES


def bounded_browser_evidence(value: Any) -> dict | None:
    """Persist a small receipt, never arbitrary WebDriver response bodies.

    Page strings remain untrusted even when the browser reports success. Raw
    screenshots/DOM and credential-bearing parameter maps are not on this allowlist.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("browser evidence must be a mapping")
    from urllib.parse import urlsplit, urlunsplit

    receipt = {"trust": "untrusted_page_evidence"}
    for key in ("session_id", "tab_id", "window_handle",
                "observation_id", "perception_id", "status", "action", "selector",
                "url", "title", "ocr_text", "page_text", "text_status", "error", "result"):
        item = value.get(key)
        if not isinstance(item, (str, bool, int, float)):
            continue
        if isinstance(item, str):
            if "data:image" in item.lower():
                continue
            item = item[:{"ocr_text": 4000, "page_text": 6000}.get(key, 1000)]
            if key == "url":
                try:
                    parts = urlsplit(item)
                    item = urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1],
                                       parts.path, "", ""))
                except ValueError:
                    item = ""
        receipt[key] = item
    receipt["screenshot_present"] = bool(value.get("screenshot_present") or
        value.get("screenshot_base64") or value.get("screenshot"))
    return receipt


AGENT_RUN_STATUSES = frozenset(
    {"batch_ran", "job_claimed", "done_claimed", "ask_operator", "blocked", "no_action"}
)


def _plan_rows(value: Any) -> list:
    """The task's plan, bounded: host-assigned step, the model's words, done or not."""
    rows = []
    for row in (value[:24] if isinstance(value, (list, tuple)) else ()):
        if not isinstance(row, Mapping):
            continue
        text = str(row.get("text") or "")[:300]
        if not text:
            continue
        rows.append({"step": int(row.get("step") or 0), "text": text,
                     "done": bool(row.get("done"))})
    return rows


def bounded_agent_run(value: Any) -> dict | None:
    """One terminal-agent batch's receipt, bounded: what ran, what it touched, what the project holds.

    The browser lane hands Omega a browser_run and lets it judge the objective. The agent lane had
    no such receipt, so the foldback saw "success" with no ledger and asked the operator what to do
    (live 2026-09-15: "Build a basic todo app using flask" claimed a job and stopped there).
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("agent run must be a mapping")
    status = str(value.get("status") or "")
    if status not in AGENT_RUN_STATUSES:
        raise ValueError("invalid agent run status")
    actions = []
    rows = value.get("actions")
    for row in (rows[:32] if isinstance(rows, (list, tuple)) else ()):
        if not isinstance(row, Mapping):
            continue
        actions.append({
            "step": int(row.get("step") or 0),
            "call_id": str(row.get("call_id") or "")[:120],
            "tool": str(row.get("tool") or "")[:60],
            "args": str(row.get("args") or "")[:300],
            "ok": bool(row.get("ok")),
            "summary": str(row.get("summary") or "")[:400],
            "error": str(row.get("error") or "")[:300],
        })

    def _paths(key: str, limit: int) -> list:
        raw = value.get(key)
        if not isinstance(raw, (list, tuple)):
            return []
        return [str(item)[:300] for item in raw[:limit] if str(item or "").strip()]

    return {
        "status": status,
        "steps": int(value.get("steps") or 0),
        "job_id": str(value.get("job_id") or "")[:128],
        "project_root": str(value.get("project_root") or "")[:500],
        "actions": actions,
        "touched_files": _paths("touched_files", 32),
        # This is a WHITELIST: a field it does not name never reaches Omega. The task's
        # own ledger and its plan are the evidence a batch-shaped receipt could not give.
        "task_touched_files": _paths("task_touched_files", 64),
        "plan": _plan_rows(value.get("plan")),
        "plan_complete": bool(value.get("plan_complete")),
        "project_files": _paths("project_files", 64),
        "options": _paths("options", 6),
        "operator_notes": _paths("operator_notes", 8),
        "summary": str(value.get("summary") or "")[:2000],
        "question": str(value.get("question") or "")[:1000],
        "stopped_reason": str(value.get("stopped_reason") or "")[:300],
    }


def bounded_browser_run(value: Any) -> dict | None:
    """The agent run's receipt, bounded: ledger rows, final page, its claim; never pixels."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("browser run must be a mapping")
    from schema.browser_perception import public_url

    status = str(value.get("status") or "")
    if status not in {"done", "ask_operator", "budget_exhausted", "blocked", "inference_failed", "stopped",
                      "busy", "note_queued"}:
        raise ValueError("invalid browser run status")
    actions = []
    for row in (value.get("actions") or ())[:32] if isinstance(value.get("actions"), (list, tuple)) else ():
        if not isinstance(row, Mapping):
            continue
        actions.append({"step": int(row.get("step") or 0), "call_id": str(row.get("call_id") or "")[:120],
                        "action": str(row.get("action") or "")[:40],
                        "summary": str(row.get("summary") or "")[:200], "ok": bool(row.get("ok")),
                        "error": str(row.get("error") or "")[:300], "url": public_url(str(row.get("url") or ""))})
    final = value.get("final_page") if isinstance(value.get("final_page"), Mapping) else {}
    return {
        "trust": "untrusted_page_evidence",
        "status": status,
        "steps": int(value.get("steps") or 0),
        "actions": actions,
        "final_page": {"url": public_url(str(final.get("url") or "")), "title": str(final.get("title") or "")[:500],
                       "text": str(final.get("text") or "")[:2500]},
        "summary": str(value.get("summary") or "")[:2000],
        "question": str(value.get("question") or "")[:1000],
        "stopped_reason": str(value.get("stopped_reason") or "")[:300],
        "operator_notes_seen": int(value.get("operator_notes_seen") or 0),
    }


@dataclass(frozen=True)
class MasteryView:
    domain: str
    knowledge_mastery: float
    application_mastery: float
    target_mastery: float


# The most cross-lane mail rows one Omega slot carries. Enforced at SELECTION by the
# binder — never by truncating here. Cutting rows after the claim once left a claimed,
# receipted message out of the packet while acknowledgement still deleted it.
CROSS_LANE_MAIL_ROWS = 32


def _mail_payload(row: Mapping[str, Any]) -> dict:
    """Render run evidence once; the durable mail retains every proof field.

    Verified run recall repeats its SourceLedger citation inside source_provenance.
    The prompt carries the complete citation plus provenance's distinct metadata,
    preserving every evidence row, excerpt, and run scope. Only exact JSON duplicates
    are omitted; conflicting values remain visible. This is pure and idempotent so
    admission, packet readback, and restart replay all measure/render the same payload.
    Canonical key ordering also keeps rendering stable when a restart reads sorted JSON.
    """
    payload = dict(row.get("payload") or {})
    run = payload.get("run_memory")
    if (row.get("purpose") != "specialist_lane_handoff"
            or not isinstance(run, Mapping) or run.get("kind") != "run_scoped_recall"
            or not isinstance(run.get("evidence"), list) or not run["evidence"]):
        return payload
    evidence = []
    for record in run["evidence"]:
        if not isinstance(record, Mapping):
            evidence.append(record)
            continue
        citation = record.get("source_citation")
        provenance = record.get("source_provenance")
        if not isinstance(citation, Mapping) or not isinstance(provenance, Mapping):
            evidence.append(record)
            continue
        distinct = {
            key: value for key, value in provenance.items()
            if key not in citation or json.dumps(value, sort_keys=True, ensure_ascii=False)
            != json.dumps(citation[key], sort_keys=True, ensure_ascii=False)
        }
        evidence.append({**record, "source_provenance": distinct})
    payload["run_memory"] = {**run, "evidence": evidence}
    return json.loads(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def bounded_mail(value: Any) -> tuple[dict, ...]:
    """Cross-lane mail as the Omega packet carries it: RENDERABLE fields only.

    Lease internals — lease_id, generation, claimed_by, lease_until — and effect_id are
    host bookkeeping. They stay on the carrier's binding record and never reach the
    prompt. Anything that is not a row is dropped rather than guessed at. It NEVER
    truncates the row list: the row limit is a selection rule (CROSS_LANE_MAIL_ROWS).

    ``arrival_seq`` renders when the mail ARRIVED from its source: the operator's send order
    for operator mail, else the store's pin sequence. Rendering the pin sequence for operator
    mail would contradict the order the rows are listed in whenever screening finished out
    of order.
    """
    if not isinstance(value, (list, tuple)):
        return ()
    rows: list[dict] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        rows.append({
            "mail_id": str(row.get("mail_id") or ""),
            "purpose": str(row.get("purpose") or ""),
            "requirement": str(row.get("requirement") or ""),
            "source_actor": str(row.get("source_actor") or ""),
            "source_episode_id": str(row.get("source_episode_id") or ""),
            "arrival_seq": int(row.get("sent_seq") or 0) or int(row.get("arrival_seq") or 0),
            "payload": _mail_payload(row),
            "provenance": [str(ref) for ref in (row.get("provenance") or ())],
        })
    return tuple(rows)


COGNITIVE_DELTA_ROWS = 8
_DELTA_TEXT = 1200
_INJECTION_REQUIREMENTS = frozenset({"required", "preferred", "optional"})


def _delta_text(value: Any, limit: int = _DELTA_TEXT) -> str:
    return " ".join(str(value or "").split())[:limit] if isinstance(value, str) else ""


def bounded_decisions(value: Any) -> tuple[dict, ...]:
    """``{statement, rationale, supersedes_id?}`` rows; anything malformed is dropped.

    ``supersedes_id`` is only ever an id the model was SHOWN (a recalled memory id); it is
    carried by value as provenance and authors nothing.
    """
    rows: list[dict] = []
    for row in (value if isinstance(value, (list, tuple)) else ())[:COGNITIVE_DELTA_ROWS]:
        if not isinstance(row, Mapping):
            continue
        statement, rationale = _delta_text(row.get("statement")), _delta_text(row.get("rationale"))
        if not statement or not rationale:
            continue
        kept = {"statement": statement, "rationale": rationale}
        supersedes = _delta_text(row.get("supersedes_id"), 128)
        if supersedes:
            kept["supersedes_id"] = supersedes
        rows.append(kept)
    return tuple(rows)


def bounded_user_constraints(value: Any, *, episode_id: str,
                             known_sources: Iterable[str] = ()) -> tuple[dict, ...]:
    """``{statement, source_episode_id}`` rows. The HOST owns the source identity: a value
    the model names is kept only when it is one the fold was actually shown (the source of
    a bound mail row); anything else becomes this episode."""
    known = {str(one) for one in known_sources if one}
    rows: list[dict] = []
    for row in (value if isinstance(value, (list, tuple)) else ())[:COGNITIVE_DELTA_ROWS]:
        if not isinstance(row, Mapping):
            continue
        statement = _delta_text(row.get("statement"))
        if not statement:
            continue
        named = _delta_text(row.get("source_episode_id"), 128)
        rows.append({"statement": statement,
                     "source_episode_id": named if named in known else str(episode_id)})
    return tuple(rows)


def bounded_lane_injections(value: Any) -> tuple[dict, ...]:
    """``{target_lane, objective, constraints[], evidence_refs[], requirement}`` rows."""
    rows: list[dict] = []
    for row in (value if isinstance(value, (list, tuple)) else ())[:COGNITIVE_DELTA_ROWS]:
        if not isinstance(row, Mapping):
            continue
        lane, objective = str(row.get("target_lane") or ""), _delta_text(row.get("objective"), 4000)
        requirement = str(row.get("requirement") or "preferred")
        if lane not in OMEGA_LANES or not objective or requirement not in _INJECTION_REQUIREMENTS:
            continue

        def texts(key: str) -> list[str]:
            raw = row.get(key)
            return [text for text in (_delta_text(item) for item in
                                      (raw if isinstance(raw, (list, tuple)) else ())[:16]) if text]

        rows.append({"target_lane": lane, "objective": objective,
                     "constraints": texts("constraints"), "evidence_refs": texts("evidence_refs"),
                     "requirement": requirement})
    return tuple(rows)


@dataclass(frozen=True)
class OmegaEpisodeInput:
    episode_id: str
    intent: str
    action: str
    tool: str | None
    domain: str | None
    operator_text: str | None
    outcome: str
    failure_reason: str | None
    raw_tool_result: dict | None
    recall: tuple[dict, ...]
    recall_error: str | None
    prior_mastery: MasteryView | None
    # APPLY execution evidence (cognition+CLI merge): {status, summary, has_diff,
    # error}. None for non-APPLY actions; lets the fold-back ground application.
    applied: dict | None = None
    # Current chemistry is an advisory amplifier for mood, confidence,
    # persistence, memory, and learning. It never owns pressure, intent, or goal.
    neuromod_state: dict = field(default_factory=dict)
    # EXECUTION EXPERIENCE (operator 2026-07-08): the mind's track record of DOING —
    # bounded rows {domain, attempts, successes, last_outcome} from the execution
    # ledger, so metacognition reasons over what it has BUILT and how it fared, not
    # just what it has learned. The evolve loop's memory of action. Populated on every
    # episode; rendered in the agent lane's system prompt by _execution_experience_block.
    # Additive default ().
    execution_experience: tuple[dict, ...] = ()
    # FULL MASTERY MAP (operator 2026-07-08): every topic in memory with its
    # knowledge mastery vs application mastery and status (empty/learning/
    # mastered), so the mind sees the WHOLE landscape — what it has learned,
    # what it has applied, what is mastered, and what it can still learn — and
    # chooses for itself rather than an automatic picker choosing for it. Shape
    # = build_learning_map(). Populated on every episode; rendered in the agent lane's
    # system prompt by _learning_map_block. Additive default {}.
    learning_map: dict = field(default_factory=dict)
    # ARTIFACT recall (operator 2026-06-25): bounded rows of what THIS kind of objective
    # produced before — {skill_name, objective, summary, files_changed} recalled from prior
    # workflow_memory — so the per-episode prompt can show the mind "you have already built
    # this" and it reasons over the prior work. This is the recall reaching Omega's INFERENCE
    # (not just biasing skill selection). Additive default ().
    prior_builds: tuple[dict, ...] = ()
    # CROSS-LANE MAIL (integration spec section 3, T6): the omega_reason mail this
    # episode's Global B bound for its thread, as renderable rows (bounded_mail). An
    # operator payload is the operator's current intent for the thread; a lane payload
    # is evidence with provenance. Additive default () = byte-identical prompts.
    cross_lane_mail: tuple[dict, ...] = ()
    # Required mail refused at selection (over budget / row cap). Host bookkeeping for
    # §6 prerequisite enforcement; never an instruction. Additive default ().
    omitted_required_mail_ids: tuple[str, ...] = ()
    # T9: a WINDOW episode's lane-entry block. AUTO infers nothing for a window, so its
    # lane-entry mail is delivered at this fold's first inference. Like the bound mail it
    # comes from live ownership (the episode's LaneEntry), never from the saved packet.
    lane_entry_block: str = ""
    # FOLD-BACK recall (operator 2026-06-27): an UNFINISHED build's progress digest —
    # {objective, progress_digest, files_changed, rounds_used} recalled from an in_progress
    # workflow_memory. A build YIELDED at a budget checkpoint; this lets Omega be the QA/mind
    # that decides continue|finish, and the digest re-grounds qwen on resume (it isn't lost when
    # the model reloads). Additive default ().
    build_in_progress: tuple[dict, ...] = ()
    # OPEN OPERATOR QUESTIONS (operator 2026-06-27): the questions Omega has asked the operator
    # that are still UNANSWERED — {question, windows_survived} from the open-questions journal.
    # Threaded into every per-episode prompt so the mind KNOWS it has a pending ask (doesn't
    # re-ask, can proceed without it) until the operator answers in the HUD. Additive default ().
    open_questions: tuple[dict, ...] = ()
    # SESSION DEV ROLLBACK FACTS: authoritative file-state changes applied by the
    # rollback sandbox and drained onto this episode. Omega sees these so memory
    # and future reasoning do not assume reverted files are still current.
    rollback_events: tuple[dict, ...] = ()
    # AUTONOMY FOLDBACK PACKET (2026-06-30): the DATA the mind reasons over each episode —
    # {pressure_surface, episode_foldback{meta_b, global_b}, plugin_execution, plugin_evidence,
    # mastery_delta, trajectory_influencer, conduct}. Assembled in packet.py on every episode;
    # INFORMATIONAL (no directives). The agent and browser lanes carry it as data in their
    # evidence, and its conduct section renders as CONDUCT in every per-episode lane.
    # Additive default None.
    foldback_packet: dict | None = None
    # POST-CHAT REFLECTION (2026-07-01): Omega's OWN reply text for this turn. operator_text carries
    # the user's message; this carries Omega's answer, so the identity reflection can reason over BOTH
    # sides of the exchange. Additive default None keeps non-chat packs byte-identical.
    response_text: str | None = None
    # The exact canonical attempt context. The model may reflect on these values
    # but cannot author or replace their continuity identities.
    motor_program: dict | None = None
    originating_pressure: dict | None = None
    # POST-CHAT REFLECTION self-model: only PROMOTED identity preferences — bounded rows
    # {trait, pattern, evidence_count, strength}. Candidate evidence has a distinct field below.
    # Additive default () keeps non-reflection packs byte-identical.
    current_identity: tuple[dict, ...] = ()
    # Evidence awaiting promotion. These rows let the next reflection reuse the
    # same trait key when the exchange independently confirms it, but they are
    # explicitly not part of the current self-model or behavioral preference set.
    candidate_identity: tuple[dict, ...] = ()
    # FETCHED-KNOWLEDGE COMPREHENSION (operator 2026-07-11): the named sources +
    # learned prose distilled from web-fetched material by the post-AUTO inference,
    # ALREADY COMMITTED to memory (receipt inside) before this packet is built —
    # {kind, sources, learned, receipt}. The fold reasons over what was actually
    # learned and recorded, not the raw page dump. Additive default None keeps
    # non-fetch packs byte-identical.
    fetched_knowledge: dict | None = None
    # Durable proactive continuity.  The policy retains the operator's exact
    # wording; the dispatch appears only on a PROACTIVE_RESPONSE application
    # episode.  Both are bounded operational context, never hidden reasoning.
    proactive_policy: dict | None = None
    proactive_dispatch: dict | None = None
    # POST-EXECUTION FOLDBACK: lineage-matched request, motor contract,
    # dispatch/result evidence, deterministic verification and satisfaction verdict.
    execution_foldback: dict | None = None
    # FIRST-ONBOARDING FOLDBACK: Guardian-gated occupation/category references
    # attached only to the queued onboarding bootstrap episode. Omega proposes
    # concrete child topics; the host materializes them after this fold.
    onboarding_context: dict | None = None
    omega_lane: str = "chat"
    original_objective: str = ""
    browser_perception: dict | None = None
    browser_run: dict | None = None
    agent_run: dict | None = None
    # Window lane only: Global B's compacted view of the locked ten-episode window.
    window_context: dict | None = None

    def __post_init__(self) -> None:
        if self.omega_lane not in OMEGA_LANES:
            raise ValueError("invalid omega lane")
        if not isinstance(self.original_objective, str):
            raise ValueError("original objective must be text")
        if self.omega_lane == "browser" and len(self.original_objective) > 4000:
            raise ValueError("browser original objective must be at most 4000 characters")
        receipt = bounded_browser_evidence(self.browser_perception)
        if receipt is not None and self.omega_lane != "browser":
            raise ValueError("browser evidence requires browser lane")
        object.__setattr__(self, "browser_perception", receipt)
        run = bounded_browser_run(self.browser_run)
        if run is not None and self.omega_lane != "browser":
            raise ValueError("browser run requires browser lane")
        object.__setattr__(self, "browser_run", run)
        batch = bounded_agent_run(self.agent_run)
        if batch is not None and self.omega_lane != "agent":
            raise ValueError("agent run requires agent lane")
        object.__setattr__(self, "agent_run", batch)
        if (self.omega_lane == "window") != (self.window_context is not None):
            raise ValueError("window context requires the window lane")
        if self.window_context is not None:
            if not isinstance(self.window_context, Mapping) or not self.window_context.get("window_id"):
                raise ValueError("window context must name its window")
            object.__setattr__(self, "window_context", dict(self.window_context))
        events = tuple(
            dict(event) for event in (self.rollback_events or ()) if isinstance(event, Mapping)
        )
        object.__setattr__(self, "rollback_events", events)
        if not isinstance(self.neuromod_state, Mapping):
            raise ValueError("neuromod state must be a mapping")
        object.__setattr__(self, "neuromod_state", dict(self.neuromod_state))
        program = None
        pressure = None
        if self.motor_program is not None:
            from schema.motor_program import MotorProgram

            program = MotorProgram.from_dict(self.motor_program)
            if program.episode_id != self.episode_id:
                raise ValueError("motor program episode_id must match the episode")
            object.__setattr__(self, "motor_program", program.to_dict())
        if self.originating_pressure is not None:
            from schema.emergent_pressure import EmergentPressure

            pressure = EmergentPressure.from_dict(self.originating_pressure)
            object.__setattr__(self, "originating_pressure", pressure.to_dict())
        if (
            program is not None
            and pressure is not None
            and program.outcome_id != pressure.outcome_id
        ):
            raise ValueError("motor program and originating pressure outcome_id must match")
        if self.execution_foldback is not None:
            from schema.execution_foldback import ExecutionFoldback

            foldback = ExecutionFoldback.from_dict(self.execution_foldback)
            if foldback.episode_id != self.episode_id:
                raise ValueError("execution foldback episode_id must match the episode")
            if program is not None and (
                foldback.to_dict()["motor"]["program_id"] != program.program_id
            ):
                raise ValueError("execution foldback motor program_id must match the packet")
            object.__setattr__(self, "execution_foldback", foldback.to_dict())
        if self.onboarding_context is not None:
            if not isinstance(self.onboarding_context, Mapping):
                raise ValueError("onboarding context must be a mapping")
            object.__setattr__(self, "onboarding_context", dict(self.onboarding_context))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["recall"] = list(self.recall)
        d["execution_experience"] = [dict(r) for r in self.execution_experience]
        d["prior_builds"] = list(self.prior_builds)
        d["build_in_progress"] = list(self.build_in_progress)
        d["open_questions"] = list(self.open_questions)
        d["rollback_events"] = list(self.rollback_events)
        d["cross_lane_mail"] = [dict(row) for row in self.cross_lane_mail]
        d["omitted_required_mail_ids"] = list(self.omitted_required_mail_ids)
        return d


@dataclass(frozen=True)
class KnowledgeProduct:
    domain: str
    statement: str
    confidence: float


@dataclass(frozen=True)
class MasteryEffect:
    domain: str
    knowledge_mastery: float
    application_mastery: float
    rationale: str


@dataclass(frozen=True)
class OmegaIntentDecision:
    """Omega's prepared NEXT-action decision for a domain (Step 4) — a setpoint the
    next ignition's Global A may read to override the deterministic opened_by map.

    DISTINCT from ``pressure_loop.contributors.IntentDecision`` (Global A's CURRENT-
    episode output, written to ``episode.global_['intent_decision']``): this is the
    mind's prepared intent for the NEXT episode in this domain, stored in the
    SEPARATE ``pressure_loop/omega_intent.py`` overlay. ``intent`` is validated
    against the emittable safe set before this is ever constructed."""

    intent: str
    domain: str | None
    confidence: float
    reason: str
    set_by_episode: str
    ttl: int = 1


@dataclass(frozen=True)
class OmegaEpisodeResult:
    episode_id: str
    cognized: bool
    knowledge_to_ingest: tuple[KnowledgeProduct, ...]
    mastery_effects: tuple[MasteryEffect, ...]
    pressure_params: dict[str, float]
    response_text: str | None  # operator-facing reply (RESPONSE actions)
    synthesis: str | None  # tool-result summary (-> tool_result_summary)
    reflection: str | None
    # Step 4: Omega's prepared next-action decision (validated dict snapshot, or
    # None when the model named nothing / the intent was invalid). Optional with a
    # default so non-emitting episodes serialize inertly via asdict.
    intent_decision: dict[str, Any] | None = None
    # AUTONOMY RICHER SCHEMA (2026-06-30): the mind's self-report on THIS episode, all
    # validate-and-drop (None when absent/invalid, serialized inertly via asdict).
    #   episode_assessment {completed, quality, reason} — did it accomplish its aim.
    #   what_changed {learned, did, verified, failed} — concrete movement this episode.
    #   trajectory_influence {decision, domain, reason} — the per-episode steer the 10-window
    #     self-authoring aggregates (it does NOT touch pressure — 2026-06-30 decouple).
    episode_assessment: dict[str, Any] | None = None
    what_changed: dict[str, Any] | None = None
    trajectory_influence: dict[str, Any] | None = None
    # Evidence-grounded lifecycle decision for the pressure that caused the
    # attempt, plus an optional fully validated successor pressure.
    pressure_resolution: dict[str, Any] | None = None
    emergent_pressure: dict[str, Any] | None = None
    # POST-CHAT IDENTITY REFLECTION (2026-07-01): each item {pattern, evidence, status} is a
    # durable self-trait Omega evidenced in a chat exchange. Ridess the SAME stash->journal->window
    # path as mastery_effects; consolidated into the canonical identity lobe by DeepSleep. Additive
    # default () -> asdict byte-identical when unset.
    identity_patterns: tuple[dict[str, Any], ...] = ()
    # A RESOLVE_QUERY fold may structure the operator's raw policy for later
    # learning/application.  A dialogue application fold may emit its factual
    # result packet.  Defaults keep all legacy episode results inert.
    operator_policy_plan: dict[str, Any] | None = None
    proactive_result: dict[str, Any] | None = None
    # BUILD PASS-OFF (operator 2026-07-22): the mind's EXPLICIT acceptance of a build the
    # battery has verified complete and that is awaiting pass-off. True = accept it (port to
    # completed); False = keep refining it in-place. None = not addressed this fold — the
    # deterministic layer falls back to the intent signal. Additive default None -> asdict
    # byte-identical when unset.
    build_accepted: bool | None = None
    # Validated child-topic plan emitted only for an onboarding bootstrap fold.
    onboarding_subtopics: tuple[dict[str, Any], ...] = ()
    # Validated semantic control snapshot; consumers migrate to this envelope.
    control_decision: dict[str, Any] | None = None
    # Window lane only: the validated reflection outcome the archive and the
    # window-scope DeepSleep read (omega_loop.window.run.coerce_window_outcome).
    window_reflection: dict[str, Any] | None = None
    # COGNITIVE DELTA (cross-lane spec section 5, T8): additive, validate-and-drop like the
    # siblings above. Decisions and constraints commit through ingest_knowledge_products;
    # injections become lane_entry mail only after that acceptance. Defaults keep every
    # existing fold byte-identical.
    decisions: tuple[dict[str, Any], ...] = ()
    user_constraints: tuple[dict[str, Any], ...] = ()
    lane_injections: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OmegaEpisodeResult":
        """Rebuild a result from :meth:`to_dict` — the exact inverse, field for field.

        A fold's whole result is frozen in its durable manifest (cross-lane T8) so that a
        replay acts on the ORIGINAL conclusion everywhere; this is how it comes back.
        """
        from dataclasses import fields

        names = {f.name for f in fields(cls)}
        if not isinstance(payload, Mapping) or not {"episode_id", "cognized"} <= set(payload):
            raise ValueError("not a serialized OmegaEpisodeResult")
        values = {key: value for key, value in payload.items() if key in names}
        values["knowledge_to_ingest"] = tuple(
            KnowledgeProduct(**row) for row in values.get("knowledge_to_ingest") or ())
        values["mastery_effects"] = tuple(
            MasteryEffect(**row) for row in values.get("mastery_effects") or ())
        for key in ("identity_patterns", "onboarding_subtopics", "decisions", "user_constraints",
                    "lane_injections"):
            values[key] = tuple(dict(row) for row in values.get(key) or ())
        values.setdefault("pressure_params", {})
        for key in ("response_text", "synthesis", "reflection"):
            values.setdefault(key, None)
        return cls(**values)

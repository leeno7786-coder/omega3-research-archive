"""Episode journal substrate (Slice 5a).

Defines:
  - PriorEpisode: a frozen, JSON-serializable record of what Omega
    needs to reason over (intent/action/outcome + future trajectory/
    reflection placeholders).
  - prior_episode_from_episode(ep): defensive extractor from a
    CognitiveEpisode-like object.
"""

from __future__ import annotations

import logging
import json
import hashlib
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

_LOG = logging.getLogger(__name__)


#: The expected verified-success rate of a motor attempt with no prior attempts to measure it from.
#: Recorded with ``expected_success_source = "prior"`` wherever it is used, never as a measurement.
MOTOR_PRIOR_EXPECTED_SUCCESS = 0.5


class EpisodeJournalUnreadable(RuntimeError):
    """The episode journal exists but cannot be read, so its window is unknown, not empty.

    AGENTS.md section 2.1: "could not read" and "read, found nothing" must never share a return
    value. A missing journal is a genuinely empty history; a corrupt line, undecodable bytes or a
    non-file at the journal path is not, and the original cause travels as ``__cause__``.
    """


def _repo_root() -> Path:
    # pressure_loop/episode_journal.py -> repo root is parent of pressure_loop/.
    return Path(__file__).resolve().parent.parent


def _resolve_path(path: str | Path) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = _repo_root() / p
    return p


@dataclass(frozen=True)
class PriorEpisode:
    episode_id: str
    ignition_reason: str
    intent: Optional[str]
    action: Optional[str]
    outcome: Optional[dict]
    trajectory_update: Optional[dict]
    cascade_status: str = "completed"
    # Cross-lane integration spec section 1 — the cognitive thread this episode
    # belonged to, so a thread can be reconstructed from the durable rows after a
    # restart. Empty on rows written before T2: truthfully "no thread recorded",
    # never a thread named "".
    thread_id: str = ""
    # Phase 3 cut 3 — the lane this episode rode and the timekeeper's
    # finish-line stamp (Phase 4 cut 13: taken inside the serialized commit
    # region, not at the deepsleep gate). Journal rows are the ordering
    # authority for episode sequence: sort stamped rows with
    # core.timekeeper.finish_line_key (pre-cut-3 rows sort first, truthfully).
    # Their FILE order is not that authority — the archive append runs outside
    # the commit region, so rows can land out of stamp order by design.
    pipeline_id: int = -1
    work_kind: str = "autonomous"
    timekeeper: Optional[dict] = None
    # Phase 4 cut 12 — the memory epoch this episode read at, the epoch it
    # committed at (== timekeeper.commit_seq; the epoch has no second counter),
    # and the episodes that committed in between, whose writes this episode's
    # reads could not have contained. Legacy rows and rows written under
    # OMEGA_MEMORY_EPOCHS=0 carry the defaults, which read truthfully as
    # "no epoch was recorded" rather than as epoch zero.
    memory_epoch_start: int = 0
    memory_epoch_commit: int = 0
    causal_parent_episode_ids: Optional[list] = None
    # Phase 4 cut 13 review — whether that parent set is the whole interval.
    # Cut 13 stamps inside the commit region and archives OUTSIDE it, so a
    # lower-seq row can still be in flight when a later episode seals: a short
    # set became the ordinary case for overlapping lanes rather than an
    # anomaly. ``episodes_committed_between`` has always known; until this
    # field the answer died in the sealer and the durable row — the ordering
    # authority every consumer reads — carried a possibly-short list with
    # nothing to say so. ``None`` is "no epoch was sealed on this row" (legacy
    # rows, OMEGA_MEMORY_EPOCHS=0, episodes that never reached the region),
    # which is distinct from a sealed False.
    causal_parents_complete: Optional[bool] = None
    # WO-06 — the five independent outcome axes (schema.episode_record.EpisodeOutcome), sealed
    # when the row is built at close. Every success count reads THIS, never cascade_status:
    # "the cascade finished" and "the task succeeded" are different facts. ``None`` on rows
    # written before WO-06, which count as unknown — never as a success.
    outcome_axes: Optional[dict] = None

    def to_dict(self) -> dict[str, Any]:
        """Stable JSON shape for the journal JSONL line."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PriorEpisode":
        return cls(
            episode_id=str(d.get("episode_id", "")),
            ignition_reason=str(d.get("ignition_reason", "")),
            intent=d.get("intent"),
            action=d.get("action"),
            outcome=d.get("outcome"),
            trajectory_update=d.get("trajectory_update"),
            cascade_status=str(d.get("cascade_status") or "completed"),
            thread_id=str(d.get("thread_id", "") or ""),
            pipeline_id=int(d.get("pipeline_id", -1)),
            work_kind=str(d.get("work_kind") or "autonomous"),
            timekeeper=d.get("timekeeper") if isinstance(d.get("timekeeper"), dict) else None,
            memory_epoch_start=_as_int(d.get("memory_epoch_start")),
            memory_epoch_commit=_as_int(d.get("memory_epoch_commit")),
            causal_parent_episode_ids=(
                [str(x) for x in d.get("causal_parent_episode_ids")]
                if isinstance(d.get("causal_parent_episode_ids"), list)
                else None
            ),
            causal_parents_complete=(
                bool(d.get("causal_parents_complete"))
                if isinstance(d.get("causal_parents_complete"), bool)
                else None
            ),
            outcome_axes=(
                dict(d["outcome_axes"]) if isinstance(d.get("outcome_axes"), dict) else None
            ),
        )


def _as_int(value: Any) -> int:
    """Defensive int for journal rows written by any prior schema version."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _deep_get(d: Any, *keys: str) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def prior_episode_from_episode(ep: Any) -> PriorEpisode:
    """Extract a PriorEpisode record from an in-flight episode (defensive).

    The extractor never assumes fields exist; it treats missing data as None
    for optional slots and empty-string for ignition_reason correlation.
    """

    episode_id = str(getattr(ep, "episode_id", ""))

    opened_by = getattr(ep, "opened_by", None)
    if not isinstance(opened_by, str) or not opened_by.strip():
        pressure = getattr(ep, "pressure", None)
        opened_by = _deep_get(pressure, "opened_by")

    ignition_reason = str(opened_by or "")

    global_ = getattr(ep, "global_", None)
    intent = None
    if isinstance(global_, dict):
        intent = _deep_get(global_, "intent_decision", "intent") or _deep_get(
            global_, "action_goal", "intent"
        )
    intent = str(intent) if isinstance(intent, str) and intent.strip() else None

    meta = getattr(ep, "meta", None)
    action = None
    if isinstance(meta, dict):
        action = _deep_get(meta, "execution_action_pack", "action")
    action = str(action) if isinstance(action, str) and action.strip() else None

    outcome = None
    if isinstance(global_, dict):
        outcome = global_.get("outcome")
    outcome = outcome if isinstance(outcome, dict) else None

    # The journal records ONLY the structured episode trail (Global A -> Omega):
    # intent / action / outcome / trajectory_update. The model's reasoning prose
    # (omega.reflection) is deliberately NOT persisted — it bloated the omega
    # prompt (~69% of it) with verbatim prior CoT for zero decision value.
    omega = getattr(ep, "omega", None)
    trajectory_update = None
    if isinstance(omega, dict):
        trajectory_update = omega.get("trajectory_update")
    trajectory_update = trajectory_update if isinstance(trajectory_update, dict) else None

    raw_status = getattr(ep, "cascade_status", None)
    cascade_status = (
        str(raw_status) if isinstance(raw_status, str) and raw_status.strip() else "completed"
    )

    stamp = getattr(ep, "timekeeper", None)
    parents = getattr(ep, "causal_parent_episode_ids", None)
    return PriorEpisode(
        episode_id=episode_id,
        ignition_reason=ignition_reason,
        intent=intent,
        action=action,
        outcome=outcome,
        trajectory_update=trajectory_update,
        cascade_status=cascade_status,
        # Read straight off the carrier: the thread was decided at open, and
        # re-deriving it here could invent one for a carrier that never had it.
        thread_id=str(getattr(ep, "thread_id", "") or ""),
        pipeline_id=int(getattr(ep, "pipeline_id", -1)),
        work_kind=str(getattr(ep, "work_kind", "autonomous")),
        timekeeper=dict(stamp) if isinstance(stamp, dict) and stamp else None,
        # Cut 12: read straight off the carrier, never recomputed here. The
        # interval was sealed once, inside the commit region (cut 13), one
        # statement after the stamp that closes it; recomputing at row-build
        # time would re-scan a journal that has moved on and quietly widen the
        # set.
        #
        # An empty parent set is written as null, not [], to keep the row lean —
        # ``memory_epoch_commit`` is the "was this sealed" flag (0 = never
        # reached the gate, or epochs were off), so null is never ambiguous.
        memory_epoch_start=_as_int(getattr(ep, "memory_epoch_start", 0)),
        memory_epoch_commit=_as_int(getattr(ep, "memory_epoch_commit", 0)),
        causal_parent_episode_ids=(
            [str(x) for x in parents] if isinstance(parents, list) and parents else None
        ),
        # Cut 13 review: the qualifier on that set. Written only for rows that
        # actually sealed an epoch — an unsealed row has no interval to be
        # complete or short ABOUT, and claiming either would be inventing a
        # verdict the sealer never reached.
        causal_parents_complete=(
            bool(getattr(ep, "causal_parents_complete", False))
            if _as_int(getattr(ep, "memory_epoch_commit", 0)) > 0
            else None
        ),
        outcome_axes=episode_outcome_from_episode(ep).to_dict(),
    )


def _active_open_query_state() -> list[dict[str, Any]]:
    """Latest-per-id OPEN questions (failure-soft)."""
    try:
        from pressure_loop.open_questions import STATUS_OPEN, _latest_per_id, _load_all

        latest = _latest_per_id(_load_all())
        open_qs = [q for q in latest.values() if q.status == STATUS_OPEN]
        return [q.to_dict() for q in open_qs]
    except Exception as exc:  # noqa: BLE001 - failure-soft
        _LOG.debug("open_query_state enrichment failed (non-fatal): %s", exc)
        return []


def _normalized_evidence_text(value: Any) -> str:
    if isinstance(value, dict):
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    else:
        raw = str(value or "")
    return " ".join(re.findall(r"[a-z0-9]+", raw.lower()))


def _requirements_evidenced(required: Any, factual: Any) -> bool:
    requirements = [
        normalized for value in required or () if (normalized := _normalized_evidence_text(value))
    ]
    evidence = [
        normalized for value in factual or () if (normalized := _normalized_evidence_text(value))
    ]
    return all(any(requirement in fact for fact in evidence) for requirement in requirements)


def _lane_requirements_evidenced(required: Any, factual: Any, *, lane: str) -> bool:
    facts = tuple(factual or ())
    aliases = {
        "artifact": {"artifact", "artifacts", "files changed", "file changed"},
        "source": {"source", "sources", "source proof", "sources consulted", "url"},
    }
    for value in required or ():
        normalized = _normalized_evidence_text(value)
        if normalized in aliases.get(lane, set()):
            if not facts:
                return False
            continue
        if not _requirements_evidenced((value,), facts):
            return False
    return True


def _structured_check_passed(required: str, results: Any) -> bool:
    requirement = _normalized_evidence_text(required)
    matching: list[dict[str, Any]] = []
    for result in results or ():
        if not isinstance(result, dict):
            try:
                result = dict(result)
            except (TypeError, ValueError):
                continue
        requested = _normalized_evidence_text(result.get("requested"))
        if requested != requirement:
            continue
        matching.append(result)
    if not matching or any(str(row.get("status") or "") != "passed" for row in matching):
        return False
    for row in matching:
        provenance = row.get("executor_provenance")
        if not isinstance(provenance, dict):
            try:
                provenance = dict(provenance)
            except (TypeError, ValueError):
                return False
        source = str(provenance.get("source") or "")
        if source == "qwen_tool_call" and not str(provenance.get("tool_call_id") or ""):
            return False
        if source == "direct_tool" and not str(provenance.get("result_digest") or ""):
            return False
        if source not in {"qwen_tool_call", "direct_tool"}:
            return False
    return True


def _evidence_contract_admitted(program: Any, evidence: Any) -> bool:
    """Deterministically admit only factual evidence satisfying the program contract."""
    from schema.execution_evidence import ExecutionEvidence
    from schema.motor_program import MotorProgram

    if not isinstance(program, MotorProgram) or not isinstance(evidence, ExecutionEvidence):
        return False
    if evidence.program_id != program.program_id or evidence.episode_id != program.episode_id:
        return False
    if evidence.status != "success":
        return False
    if any(
        (
            evidence.blockers,
            evidence.environmental_contradictions,
            evidence.failure_kind,
            evidence.failure_reason,
        )
    ):
        return False

    contract = program.evidence_contract
    if not _lane_requirements_evidenced(
        contract.required_artifacts,
        (*evidence.artifacts, *evidence.files_changed),
        lane="artifact",
    ):
        return False
    if not _lane_requirements_evidenced(
        contract.required_source_proof,
        evidence.sources_consulted,
        lane="source",
    ):
        return False
    if not all(
        _structured_check_passed(required, evidence.verification_results)
        for required in contract.required_checks
    ):
        return False

    failure_facts = (
        *evidence.blockers,
        *evidence.environmental_contradictions,
        evidence.failure_kind,
        evidence.failure_reason,
        *evidence.verification_results,
    )
    for failure in contract.disqualifying_failures:
        normalized = _normalized_evidence_text(failure)
        if normalized and any(
            normalized in _normalized_evidence_text(fact) for fact in failure_facts
        ):
            return False
    return True


# Cap the raw tool_result (a full fetched web page) carried in the outcome
# artifacts. Unbounded, 10 of these dominated the window-reflection prompt
# (29,536 chars / ~85% of the block in the live incident) and drove each gemma
# pass to ~171s of prefill. Mirrors the per-episode prompt caps
# (omega_loop/episode/prompts.py _MAX_RESULT_CHARS = 6000). The compact
# tool_result_summary carries the signal Omega actually reflects on.
_MAX_TOOL_RESULT_CHARS = 6000


def _trim_outcome_for_reflection(outcome: Any) -> Any:
    """Shed the unbounded raw fetch from outcome.artifacts.tool_result while
    preserving every reflection-critical field (temporal, learning_state,
    observations, consistency, the summary). Non-dict outcomes pass through."""
    if not isinstance(outcome, dict):
        return outcome
    artifacts = outcome.get("artifacts")
    if not isinstance(artifacts, dict):
        return outcome

    raw = artifacts.get("tool_result")
    plugin_raw = artifacts.get("plugin_result")

    def _byte_length(value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, str):
            return len(value.encode("utf-8"))
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))

    def _cap_text(value: str) -> str:
        suffix = "…[capped]"
        encoded = value.encode("utf-8")
        if len(encoded) <= _MAX_TOOL_RESULT_CHARS:
            return value
        prefix_bytes = max(0, _MAX_TOOL_RESULT_CHARS - len(suffix.encode("utf-8")))
        prefix = encoded[:prefix_bytes].decode("utf-8", errors="ignore")
        return prefix + suffix

    raw_len = max(_byte_length(raw), _byte_length(plugin_raw))
    if raw_len <= _MAX_TOOL_RESULT_CHARS:
        return outcome  # nothing oversized — leave the outcome untouched

    summary = artifacts.get("tool_result_summary")
    trimmed_artifacts = dict(artifacts)
    if isinstance(summary, str) and summary.strip():
        # The summary already carries the grounded signal — drop the raw page.
        trimmed_artifacts["tool_result"] = None
        if "plugin_result" in trimmed_artifacts:
            trimmed_artifacts["plugin_result"] = None
    elif isinstance(raw, str):
        trimmed_artifacts["tool_result"] = _cap_text(raw)
    elif raw is not None:
        trimmed_artifacts["tool_result"] = (
            _cap_text(json.dumps(raw, ensure_ascii=False, default=str))
        )
    if plugin_raw is not None and _byte_length(plugin_raw) > _MAX_TOOL_RESULT_CHARS:
        if isinstance(plugin_raw, str):
            trimmed_artifacts["plugin_result"] = _cap_text(plugin_raw)
        else:
            trimmed_artifacts["plugin_result"] = _cap_text(
                json.dumps(plugin_raw, ensure_ascii=False, default=str)
            )
    trimmed = dict(outcome)
    trimmed["artifacts"] = trimmed_artifacts
    return trimmed


def compact_episode_for_reflection(body: dict[str, Any]) -> dict[str, Any]:
    """Compact enriched body for Omega reflection prompt only (not window store)."""
    if not isinstance(body, dict):
        return {}

    pressure = body.get("pressure") if isinstance(body.get("pressure"), dict) else {}
    execution = body.get("execution") if isinstance(body.get("execution"), dict) else {}
    brain_turns = body.get("brain_turns")
    turn_count = len(brain_turns) if isinstance(brain_turns, list) else 0

    compact_pressure: dict[str, Any] = {}
    if pressure.get("aggregate_pressure") is not None:
        compact_pressure["aggregate_pressure"] = pressure.get("aggregate_pressure")
    if pressure.get("opened_by") is not None:
        compact_pressure["opened_by"] = pressure.get("opened_by")

    pack = (
        execution.get("execution_action_pack")
        if isinstance(execution.get("execution_action_pack"), dict)
        else {}
    )
    compact_execution: dict[str, Any] = {}
    action = body.get("action") or pack.get("action")
    if action is not None:
        compact_execution["action"] = action
    if execution.get("state") is not None:
        compact_execution["state"] = execution.get("state")
    reason = pack.get("tool_reason") or execution.get("reason")
    if reason is not None:
        compact_execution["reason"] = reason

    compact: dict[str, Any] = {
        "episode_id": body.get("episode_id"),
        "intent": body.get("intent"),
        "action": body.get("action"),
        "outcome": _trim_outcome_for_reflection(body.get("outcome")),
        "cascade_status": body.get("cascade_status"),
        "topic_history": body.get("topic_history"),
        "neuromod": body.get("neuromod"),
        "brain_turn_count": turn_count,
        "pressure": compact_pressure,
        "execution": compact_execution,
    }
    if isinstance(body.get("trajectory_update"), dict):
        compact["trajectory_update"] = dict(body["trajectory_update"])

    # The operator Q&A thread survives compaction (operator decision
    # 2026-06-12): when the episode answers a system query (the text app's
    # respond path), Omega's PROMPT must see which question was closed and
    # what the operator answered — the response is signal for whatever the
    # system is trying to accomplish. Contributor metadata is otherwise
    # stripped here, so the thread gets its own compact block.
    for row in pressure.get("contributors") or []:
        if not isinstance(row, dict):
            continue
        detail = row.get("metadata") or row.get("detail") or {}
        if not isinstance(detail, dict):
            continue
        query_text = str(detail.get("system_query_text") or "").strip()
        if not query_text:
            continue
        compact["operator_thread"] = {
            "system_query_id": str(detail.get("system_query_id") or ""),
            "question": query_text,
            "answer": str(detail.get("message") or ""),
        }
        break

    if isinstance(brain_turns, list) and brain_turns:
        summary: dict[str, Any] = {}
        first = brain_turns[0] if isinstance(brain_turns[0], dict) else None
        last = brain_turns[-1] if isinstance(brain_turns[-1], dict) else None
        if first is not None:
            summary["first"] = dict(first)
        if last is not None and last is not first:
            summary["last"] = dict(last)
        if summary:
            compact["brain_turn_summary"] = summary

    return compact


# ── WO-06: outcome axes, stage trail, coverage, metacognition ────────────────
#
# Content assembly for the sealed EpisodeRecord (CONTRACTS.md section 3). WO-07's
# ``seal_episode_record`` assembles the record at the DeepSleep seam from these; nothing here
# writes, publishes or owns a clock. Each reads the carrier as the cascade left it.

#: The one cascade, in order (AGENTS.md section 3). Admission is the durable queue door.
PHASES: tuple[str, ...] = (
    "admission",
    "pressure",
    "global_a",
    "meta_a",
    "auto",
    "meta_b",
    "global_b",
    "omega",
    "deepsleep",
)

#: ``in_progress`` is DeepSleep's own row: the record is assembled when its terminus begins, so
#: it can never claim DeepSleep finished. That completion is DeepSleep's commit receipt.
PHASE_STATUSES: tuple[str, ...] = ("ran", "failed", "not_run", "deferred", "in_progress", "unknown")

_RESULT_AXES = {
    # Meta B's execution result -> (action_status, task_outcome)
    "success": ("succeeded", "success"),
    "partial": ("partial", "partial"),
    "failure": ("failed", "failure"),
    "pending_input": ("blocked", "blocked"),
}
_NOT_EXECUTED_AXES = {
    # Meta B's execution status when it recorded that nothing executed
    "idle": ("not_attempted", "not_applicable"),
    "skipped": ("skipped", "not_applicable"),
    # AUTO did not execute the plan Meta A produced (a critical consistency mismatch).
    "invalid": ("unknown", "unknown"),
}
_VERDICT_TO_VERIFICATION = {
    "passed": "passed",
    "failed": "failed",
    "partial": "partial",
    "not_performed": "not_performed",
    "unknown": "unknown",
    "no_criteria": "not_performed",
}


def _slots(ep: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    meta = getattr(ep, "meta", None)
    global_ = getattr(ep, "global_", None)
    return (dict(meta) if isinstance(meta, dict) else {},
            dict(global_) if isinstance(global_, dict) else {})


def _cascade_status_of(ep: Any) -> str:
    from schema.continuity_json import ContinuityContractError
    from schema.episode_record import CASCADE_STATUSES

    raw = getattr(ep, "cascade_status", None)
    status = str(raw).strip() if isinstance(raw, str) else ""
    if not status:
        return "unknown"
    if status not in CASCADE_STATUSES:
        raise ContinuityContractError(
            f"episode {getattr(ep, 'episode_id', '')!r} carries cascade_status {status!r}, which "
            f"is not one of {CASCADE_STATUSES}; an unrecognised status is not read as any of them"
        )
    return status


def _recorded_failure(meta: dict[str, Any]) -> str:
    failure = meta.get("cascade_failure")
    if isinstance(failure, dict) and (failure.get("error_type") or failure.get("message")):
        return f"{failure.get('error_type') or 'Error'}: {failure.get('message') or ''}".strip()
    if meta.get("motor_planning_failure"):
        return f"motor planning failed: {meta['motor_planning_failure']}"
    return ""


def check_phase_coverage(trail: Any) -> tuple[dict[str, Any], ...]:
    """Field coverage for the stage trail: every stage, in order, and a cause for each one that
    did not simply run. A stage missing from the trail is how a failure used to vanish."""
    from schema.continuity_json import ContinuityContractError

    rows = list(trail or ())
    names = [str(row.get("phase") or "") if isinstance(row, dict) else "" for row in rows]
    missing = [phase for phase in PHASES if phase not in names]
    if missing:
        raise ContinuityContractError(f"the stage trail omits {missing}; every stage must appear")
    if names != list(PHASES):
        raise ContinuityContractError(f"the stage trail must list {PHASES} once each, in order")
    for row in rows:
        status = str(row.get("status") or "")
        if status not in PHASE_STATUSES:
            raise ContinuityContractError(
                f"stage {row['phase']!r} has status {status!r}, not one of {PHASE_STATUSES}"
            )
        if status != "ran" and not str(row.get("cause") or "").strip():
            raise ContinuityContractError(
                f"stage {row['phase']!r} is {status!r} and gives no cause; a stage that did not "
                "run must say why"
            )
        if not isinstance(row.get("evidence"), list):
            raise ContinuityContractError(f"stage {row['phase']!r} evidence must be a list")
    return tuple(rows)


def _captured_phase_failures(episode_id: str, events: Any = None) -> dict[str, dict[str, Any]]:
    """Return phase-owner failures captured as episode events, keyed by phase."""
    if events is None and episode_id:
        from memory.episode_events import read_episode_events

        events = read_episode_events(episode_id)
    failures: dict[str, dict[str, Any]] = {}
    for event in events or ():
        kind = str(getattr(event, "event_kind", "") or "")
        phase = str(getattr(event, "phase", "") or "")
        if not (kind.endswith(".phase_failed") or kind in {
            "meta_a.contract_failed", "admission.dev_recall_failed",
        }):
            continue
        if phase not in {"admission", "global_a", "meta_a", "meta_b", "global_b", "omega"}:
            continue
        payload = getattr(event, "payload", {})
        payload = dict(payload) if isinstance(payload, Mapping) else {}
        failures[phase] = {
            "cause": str(payload.get("reason") or f"{phase} failed in its phase producer"),
            "evidence": [f"episode_event:{getattr(event, 'event_id', '')}"],
        }
    return failures


def phase_trail(ep: Any, *, events: Any = None) -> list[dict[str, Any]]:
    """Every stage of the one cascade, each with an explicit status and, when it did not run,
    the cause. Successful stages come from carrier slots; producer failures come from their
    captured phase-failure events, which remain authoritative even if a producer wrote a slot
    before it raised."""
    cascade = _cascade_status_of(ep)
    meta, global_ = _slots(ep)
    episode_id = str(getattr(ep, "episode_id", "") or "")
    captured_failures = _captured_phase_failures(episode_id, events)
    omega_failure = str(meta.get("omega_fold_failure") or "")
    slots = (
        ("admission", True, "the episode carrier"),
        ("pressure", bool(getattr(ep, "pressure", None)), "carrier.pressure"),
        ("global_a", bool(global_.get("intent_decision") or global_.get("action_goal")),
         "global_.intent_decision"),
        ("meta_a", bool(meta.get("execution_action_pack")), "meta.execution_action_pack"),
        ("auto", bool(getattr(ep, "auto", None)), "carrier.auto"),
        ("meta_b", bool(meta.get("reflection")), "meta.reflection"),
        ("global_b", bool(global_.get("outcome") or global_.get("omega_packet")),
         "global_.outcome"),
        ("omega", bool(global_.get("omega_result")) and not omega_failure, "global_.omega_result"),
    )
    failure = _recorded_failure(meta)
    rows: list[dict[str, Any]] = []
    blocked_by = ""
    for phase, present, slot in slots:
        captured = captured_failures.get(phase)
        if captured:
            rows.append({"phase": phase, "status": "failed", "cause": captured["cause"],
                         "evidence": list(captured["evidence"])})
            blocked_by = phase
        elif present:
            rows.append({"phase": phase, "status": "ran", "cause": "", "evidence": [slot]})
        elif blocked_by:
            rows.append({"phase": phase, "status": "not_run",
                         "cause": f"not run: {blocked_by} did not complete", "evidence": []})
        elif phase == "omega" and omega_failure:
            rows.append({"phase": phase, "status": "failed",
                         "cause": f"the Omega fold failed: {omega_failure}", "evidence": []})
            blocked_by = phase
        elif phase == "meta_b" and meta.get("meta_b_foldback_deferred"):
            rows.append({"phase": phase, "status": "deferred",
                         "cause": "Meta B fold-back was deferred while the pressure loop paused",
                         "evidence": []})
            blocked_by = phase
        elif cascade == "failed":
            rows.append({"phase": phase, "status": "failed",
                         "cause": failure or "the cascade failed at this stage; no cause was "
                         "recorded on the carrier", "evidence": []})
            blocked_by = phase
        elif cascade in {"deferred", "interrupted"}:
            rows.append({"phase": phase, "status": "not_run",
                         "cause": f"the cascade was {cascade} before this stage", "evidence": []})
            blocked_by = phase
        else:
            rows.append({"phase": phase, "status": "unknown",
                         "cause": f"no {slot} was recorded although the cascade is {cascade}",
                         "evidence": []})
    rows.append({"phase": "deepsleep", "status": "in_progress",
                 "cause": "this record is assembled as the DeepSleep terminus begins; DeepSleep's "
                 "completion is its own commit receipt", "evidence": []})
    return list(check_phase_coverage(rows))


def coverage_content(trail: Any) -> dict[str, Any]:
    """The record's coverage block from the stage trail: what was captured and what was not."""
    rows = check_phase_coverage(trail)
    return {
        "captured": [row["phase"] for row in rows if row["status"] == "ran"],
        "unrecorded": [row["phase"] for row in rows if row["status"] != "ran"],
        "inaccessible": [],
        "redacted": [],
        "causal_gaps": [f"{row['phase']} {row['status']}: {row['cause']}"
                        for row in rows if row["status"] != "ran"],
    }


def _meta_b_evidence(ep: Any) -> tuple[Optional[dict], Optional[dict]]:
    """Meta B's execution verdict and checks, preferring what Global B bound into its pack."""
    meta, global_ = _slots(ep)
    pack = global_.get("outcome")
    bound = (pack.get("phase_evidence") or {}).get("meta_b") if isinstance(pack, dict) else None
    if isinstance(bound, dict) and bound.get("state") == "observed":
        value = dict(bound.get("value") or {})
        execution = value.get("execution")
        return (dict(execution) if isinstance(execution, dict) else None,
                dict(value["checks"]) if isinstance(value.get("checks"), dict) else None)
    reflection = meta.get("reflection")
    if isinstance(reflection, dict) and isinstance(reflection.get("execution"), dict):
        checks = reflection.get("checks")
        return dict(reflection["execution"]), dict(checks) if isinstance(checks, dict) else None
    return None, None


def _verification_of(checks: Optional[dict]) -> str:
    from schema.continuity_json import ContinuityContractError

    if checks is None:
        return "unknown"
    verdict = str(checks.get("verdict") or "")
    if verdict not in _VERDICT_TO_VERIFICATION:
        raise ContinuityContractError(f"Meta B criteria checks report an unknown verdict {verdict!r}")
    return _VERDICT_TO_VERIFICATION[verdict]


def episode_outcome_from_episode(ep: Any):
    """The five independent outcome axes, from what each stage actually recorded.

    ``cascade_status`` is the cognition cascade; ``action_status`` is what the action did;
    ``task_outcome`` is whether the objective was met, and a failed acceptance check makes it a
    failure however the action ended; ``verification_status`` is whether a host check examined
    the current bytes; ``feedback_status`` is the operator's later word, which this cannot know.
    Missing evidence yields unknown or not_performed, never a success. An unrecognised producer
    value fails here with attribution instead of being read as something it is not.
    """
    from meta_loop.checks.run import METHOD, METHOD_VERSION
    from schema.continuity_json import ContinuityContractError
    from schema.episode_record import EpisodeOutcome

    cascade = _cascade_status_of(ep)
    execution, checks = _meta_b_evidence(ep)
    assessments: dict[str, Any] = {
        "cascade_status": {"author": "host", "note": "the cognition cascade status at close"},
    }
    if execution is None:
        trail = {row["phase"]: row for row in phase_trail(ep)}
        auto_row = trail["auto"]
        action = "not_attempted" if auto_row["status"] == "not_run" else "unknown"
        task, verification = "unknown", "not_performed"
        why = f"Meta B recorded no verdict ({trail['meta_b']['status']}: {trail['meta_b']['cause']})"
        assessments["action_status"] = {"author": "host", "note": why}
        assessments["task_outcome"] = {"author": "host", "note": why}
    else:
        result = str(execution.get("result") or "")
        status = str(execution.get("status") or "")
        cancellation = execution.get("cancellation")
        if isinstance(cancellation, dict) and cancellation:
            action, task, verification = "cancelled", "cancelled", "not_applicable"
        elif result == "none":
            if status not in _NOT_EXECUTED_AXES:
                raise ContinuityContractError(
                    f"Meta B recorded that nothing executed with status {status!r}, which is not "
                    f"one of {tuple(_NOT_EXECUTED_AXES)}"
                )
            action, task = _NOT_EXECUTED_AXES[status]
            verification = "not_applicable" if task == "not_applicable" else "not_performed"
        elif result in _RESULT_AXES:
            action, task = _RESULT_AXES[result]
            verification = _verification_of(checks)
            if verification == "failed" and task in {"success", "partial"}:
                task = "failure"  # the objective failed an acceptance criterion
        else:
            raise ContinuityContractError(
                f"Meta B execution result {result!r} is not one of "
                f"{('none', *_RESULT_AXES)}; it cannot be read as an outcome"
            )
        note = (f"result={result}; status={status}; disposition={execution.get('disposition')}"
                + (f"; stopped via {cancellation.get('source')}" if isinstance(cancellation, dict)
                   and cancellation else ""))
        assessments["action_status"] = {"author": "meta_b", "evidence_refs": ["meta_b.execution"],
                                        "note": note}
        assessments["task_outcome"] = {"author": "meta_b", "evidence_refs": ["meta_b.execution"],
                                       "note": note}
        if checks is not None:
            assessments["task_outcome"]["criteria_revision"] = str(checks.get("criteria_revision") or "")
            performed = [row for row in checks.get("results") or ()
                         if row.get("status") in {"passed", "failed"}]
            refs = [ref for row in performed for ref in row.get("evidence_event_ids") or ()]
            verification_assessment: dict[str, Any] = {
                "author": "meta_b",
                "verifier": f"{METHOD}@{METHOD_VERSION}",
                "criteria_revision": str(checks.get("criteria_revision") or ""),
                "note": f"criteria check verdict: {checks.get('verdict')}",
            }
            if refs:
                verification_assessment["check_refs"] = refs
                verification_assessment["artifact_revision"] = "; ".join(sorted({
                    f"{row['path']}@{row['checked_revision']}:{row['checked_sha256']}"
                    for row in performed
                }))
            assessments["verification_status"] = verification_assessment
    return EpisodeOutcome(
        cascade_status=cascade,
        action_status=action,
        task_outcome=task,
        verification_status=verification,
        feedback_status="not_received",
        assessments=assessments,
    )


def metacognition_content(ep: Any):
    """Omega's per-episode control, its attributed assessment, and its proposals.

    The control decision is re-validated through the shared ``OmegaControlDecision`` contract and
    kept verbatim. Everything Omega proposed — the successor and its resolution conditions,
    decisions, constraints, lane directions, trajectory steer — stays ``proposed``: acceptance or
    publication is proven later by downstream receipts, never by this record. A fold that failed
    or never ran is unavailable with its cause, not an empty assessment.
    """
    from schema.continuity_json import ContinuityContractError, Observation
    from schema.omega_control_decision import OmegaControlDecision

    meta, global_ = _slots(ep)
    failure = str(meta.get("omega_fold_failure") or "")
    if failure:
        return Observation.missing("unavailable", reason=f"the Omega fold failed: {failure}")
    result = global_.get("omega_result")
    if not isinstance(result, dict):
        return Observation.missing("not_captured",
                                   reason="no per-episode Omega result was recorded on the carrier")
    control_raw = result.get("control_decision")
    if not isinstance(control_raw, dict):
        return Observation.missing("unavailable",
                                   reason="the Omega result carries no validated control decision")
    try:
        control = OmegaControlDecision.from_dict(dict(control_raw))
    except ValueError as exc:
        raise ContinuityContractError(
            f"the recorded Omega control decision does not validate: {exc}"
        ) from exc
    mail = global_.get("omega_mail")
    mail_ids = [str(row.get("mail_id") or "") for row in (mail or {}).get("receipts") or ()
                if isinstance(row, dict)] if isinstance(mail, dict) else []
    source = {"episode_id": str(getattr(ep, "episode_id", "") or ""), "cross_lane_mail_ids": mail_ids}
    proposals: list[dict[str, Any]] = []
    pressure = control.pressure
    if pressure is not None:
        proposals.append({
            "kind": "successor",
            "status": "proposed",
            "intent": pressure.intent,
            "reason": pressure.reason,
            "desired_state": pressure.desired_state,
            "observed_state": pressure.observed_state,
            "resolution_conditions": list(pressure.resolution_conditions),
            "domain": pressure.domain,
            "topic": pressure.topic,
            "source_refs": source,
        })
    for kind, key in (("decision", "decisions"), ("user_constraint", "user_constraints"),
                      ("lane_injection", "lane_injections")):
        for row in result.get(key) or ():
            if isinstance(row, dict):
                proposals.append({**dict(row), "kind": kind, "status": "proposed",
                                  "source_refs": source})
    steer = result.get("trajectory_influence")
    if isinstance(steer, dict) and steer:
        proposals.append({**dict(steer), "kind": "trajectory_influence", "status": "proposed",
                          "source_refs": source})
    return Observation.observed({
        "control": control.to_dict(),
        "model_assessment": {
            "author": "model",
            "synthesis": result.get("synthesis"),
            "episode_assessment": result.get("episode_assessment"),
            "what_changed": result.get("what_changed"),
        },
        "proposals": proposals,
    })


def enriched_episode_from_episode(ep: Any) -> dict[str, Any]:
    """Build the maintained-window episode body from a closed episode."""
    base = prior_episode_from_episode(ep)
    trail = phase_trail(ep)

    pressure = getattr(ep, "pressure", None)
    pressure_dict = dict(pressure) if isinstance(pressure, dict) else {}

    meta = getattr(ep, "meta", None)
    execution: dict[str, Any] = {}
    program = None
    if isinstance(meta, dict):
        pack = meta.get("execution_action_pack")
        if isinstance(pack, dict):
            journal_pack = dict(pack)
            execution_inputs = journal_pack.get("execution_inputs")
            if isinstance(execution_inputs, dict):
                journal_inputs = dict(execution_inputs)
                journal_inputs.pop("motor_program", None)
                journal_pack["execution_inputs"] = journal_inputs
            execution["execution_action_pack"] = journal_pack
        from pressure_loop.agent_lane import handed_off

        handoff = handed_off(ep)
        if handoff:
            # Phase 4 cut 9: this episode PLANNED the program and handed it to
            # the agent lane. The program itself moved with the work, so no
            # attempt is derived below (an episode that never executed reads as
            # a FAILED attempt) — but what it authored is still part of the
            # record, so the handoff, its objective and the program identity
            # ride the row instead. The agent episode writes the attempt.
            execution["agent_lane_handoff"] = dict(handoff)
        candidate = meta.get("motor_program")
        program = (
            candidate
            if isinstance(candidate, dict) and candidate.get("shadow") is not True
            else None
        )
    state = getattr(ep, "state", None)
    if isinstance(state, str) and state.strip():
        execution["state"] = state

    global_ = getattr(ep, "global_", None)
    omega_result = global_.get("omega_result") if isinstance(global_, dict) else None

    brain_turns = getattr(ep, "brain_turns", None)
    turns = (
        [dict(t) for t in brain_turns if isinstance(t, dict)]
        if isinstance(brain_turns, list)
        else []
    )

    inputs = (
        execution.get("execution_action_pack", {}).get("execution_inputs")
        if isinstance(execution, dict)
        else None
    )
    topic_history = None
    if isinstance(inputs, dict) and inputs.get("domain"):
        topic_history = {
            "topic": inputs.get("query"),
            "domain": inputs.get("domain"),
            "source": inputs.get("topic_source"),
        }

    motor_attempt_assessment = None
    if isinstance(program, dict):
        from auto_loop.motor_adapter import execution_evidence_from_fold
        from schema.motor_program import MotorProgram

        evidence = execution_evidence_from_fold(ep)
        canonical_program = MotorProgram.from_dict(program)
        pathway = dict(program.get("pathway") or {})
        outcome_contract = dict(program.get("outcome_contract") or {})
        domain = str(
            (inputs or {}).get("domain")
            or (inputs or {}).get("query")
            # USE_SKILL builds carry no execution_inputs domain/query — fall to
            # the fold's own intent_decision before the junk "motor-program"
            # bucket (live 2026-07-19: a k8s build's credit landed on
            # "motor-program" instead of its real domain).
            or (
                (omega_result.get("intent_decision") or {}).get("domain")
                if isinstance(omega_result, dict)
                and isinstance(omega_result.get("intent_decision"), dict)
                else ""
            )
            or "motor-program"
        ).strip()
        omega_packet = global_.get("omega_packet") if isinstance(global_, dict) else None
        packet_mastery = (
            omega_packet.get("prior_mastery") if isinstance(omega_packet, dict) else None
        )
        domain_mastery = (
            dict(packet_mastery)
            if isinstance(packet_mastery, dict)
            and str(packet_mastery.get("domain") or domain) == domain
            else {}
        )
        if not domain_mastery:
            from deepsleep_loop.mastery_store import load_mastery, normalize_domain_id

            prior_record = load_mastery().get(normalize_domain_id(domain))
            if prior_record is not None:
                domain_mastery = prior_record.to_dict()
        pre_mastery = {
            "knowledge_mastery": float(
                domain_mastery.get("knowledge_mastery", domain_mastery.get("knowledge", 0.0))
            ),
            "application_mastery": float(
                domain_mastery.get("application_mastery", domain_mastery.get("application", 0.0))
            ),
        }
        episode_assessment = (
            dict(omega_result.get("episode_assessment") or {})
            if isinstance(omega_result, dict)
            else {}
        )
        claimed_effect = {}
        if isinstance(omega_result, dict):
            for effect in omega_result.get("mastery_effects") or ():
                if isinstance(effect, dict) and str(effect.get("domain") or "") == domain:
                    claimed_effect = effect
                    break
        failure_text = str(evidence.failure_kind or evidence.failure_reason or "").strip()
        failure_fingerprint = (
            hashlib.sha256(failure_text.encode("utf-8")).hexdigest()[:24] if failure_text else ""
        )
        prior_attempts = meta.get("prior_motor_attempts") or meta.get("prior_attempts") or ()
        prior_rows = [row for row in prior_attempts if isinstance(row, dict)]
        # Measured over the prior attempts, or the stated prior when there are none — and it says
        # which, so a first attempt's neutral 0.5 never reads back as a measured coin flip (the
        # WO-06 rule the neuromod expectation already follows).
        if prior_rows:
            expected_success = sum(
                1 for row in prior_rows if str(row.get("verification_verdict") or "") == "verified"
            ) / len(prior_rows)
            expected_success_source = "measured"
        else:
            expected_success = MOTOR_PRIOR_EXPECTED_SUCCESS
            expected_success_source = "prior"
        motor_attempt_assessment = {
            "program_id": str(program.get("program_id") or ""),
            "outcome_id": str(program.get("outcome_id") or ""),
            "episode_id": base.episode_id,
            "pathway_id": str(pathway.get("pathway_id") or ""),
            "pathway_revision": int(pathway.get("revision") or 1),
            "pathway_method": str(pathway.get("method") or ""),
            "material_signature": dict(program.get("material_signature") or {}),
            "task_context": str(
                outcome_contract.get("objective") or base.action or "motor attempt"
            ),
            "domain": domain,
            "verification_verdict": (
                "verified"
                if _evidence_contract_admitted(canonical_program, evidence)
                else "unverified"
            ),
            "execution_status": evidence.status,
            "execution_evidence": evidence.to_dict(),
            "completion_signals": list(canonical_program.evidence_contract.completion_signals),
            "pre_mastery": pre_mastery,
            "claimed_knowledge_delta": float(
                claimed_effect.get("knowledge_mastery", pre_mastery["knowledge_mastery"])
            )
            - pre_mastery["knowledge_mastery"],
            "claimed_application_delta": float(
                claimed_effect.get("application_mastery", pre_mastery["application_mastery"])
            )
            - pre_mastery["application_mastery"],
            "expected_success": expected_success,
            "expected_success_source": expected_success_source,
            "expected_success_attempts": len(prior_rows),
            "novelty": float(episode_assessment.get("novelty_score") or 0.0),
            "repeated_failure": bool(pathway.get("failed_predecessor_id")),
            "failure_fingerprint": failure_fingerprint,
        }

    body = {
        "episode_id": base.episode_id,
        "intent": base.intent,
        "action": base.action,
        "outcome": base.outcome,
        "trajectory_update": base.trajectory_update,
        "pressure": pressure_dict,
        "execution": execution,
        "motor_program": (
            {
                "program_id": program.get("program_id"),
                "parent_program_id": program.get("parent_program_id"),
                "outcome_id": program.get("outcome_id"),
                "attempt_number": program.get("attempt_number"),
                "fingerprint": program.get("fingerprint"),
                "pathway": dict(program.get("pathway") or {}),
                "evidence_contract": dict(program.get("evidence_contract") or {}),
            }
            if isinstance(program, dict)
            else None
        ),
        "motor_attempt_assessment": motor_attempt_assessment,
        "brain_turns": turns,
        "topic_history": topic_history,
        "open_query_state": _active_open_query_state(),
        "omega_result": omega_result if isinstance(omega_result, dict) else None,
        "cascade_status": base.cascade_status,
        # WO-06 — canonical detail for the sealed record: the five outcome axes, every stage's
        # status with a cause when it did not run, what the record covers, and Omega's control
        # and proposals. The reflection compaction keeps its own bounded contract.
        "outcome_axes": base.outcome_axes,
        "phase_trail": trail,
        "coverage": coverage_content(trail),
        "metacognition": metacognition_content(ep).to_dict(),
        "neuromod": (
            ep.auto["neuromod"]
            if isinstance(getattr(ep, "auto", None), dict)
            and isinstance(ep.auto.get("neuromod"), dict)
            else {}
        ),
        # Preserve the episode's temporal + learning_state slots (set by
        # cognition_chain via store.open); omega reflect/package and deepsleep
        # read these from episode_window[0] — dropping them fabricates {}.
        "temporal": dict(getattr(ep, "temporal", None) or {}),
        "learning_state": dict(getattr(ep, "learning_state", None) or {}),
        # Phase 3 cut 3 — lane + the timekeeper's finish-line stamp
        # (commit_seq, commit_ms, event_date, lane). Memory order is
        # finish-line arrival, not ignition order; consumers sort by
        # core.timekeeper.finish_line_key. Phase 4 cut 13: the stamp is taken
        # inside the commit region, which is AFTER this body is built — so it
        # is empty here and ``restamp_enriched_body`` fills it in before any
        # downstream reader sees the body.
        "pipeline_id": int(getattr(ep, "pipeline_id", -1)),
        "work_kind": str(getattr(ep, "work_kind", "autonomous")),
        "timekeeper": dict(getattr(ep, "timekeeper", None) or {}),
        # Phase 4 cut 12 — the memory epoch this episode read at, committed at,
        # and the episodes that committed in between. Rides the enriched body
        # for the same reason lane and timekeeper do: the window, the omega
        # pack and DeepSleep all read episode bodies, and an episode that
        # reasoned across an epoch change should be able to say so.
        "memory_epoch": {
            "start": base.memory_epoch_start,
            "commit": base.memory_epoch_commit,
            "causal_parent_episode_ids": list(base.causal_parent_episode_ids or ()),
            "causal_parents_complete": base.causal_parents_complete,
            "pin": dict(getattr(ep, "memory_epoch_pin", None) or {}),
        },
        # Surface AUTO outputs so DeepSleepInput.auto_outcome can resolve
        # knowledge_artifact_id (episode<->KnowledgeArtifact backlink on consolidate).
        "outputs": (
            dict(ep.auto["outputs"])
            if isinstance(getattr(ep, "auto", None), dict)
            and isinstance(ep.auto.get("outputs"), dict)
            else {}
        ),
    }
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "pressure",
            "enriched_episode",
            episode_id=base.episode_id,
            summary=f"cascade_status={base.cascade_status}",
            cascade_status=base.cascade_status,
            brain_turns_count=len(turns),
            ok=True,
        )
    except Exception:
        pass
    return body


def restamp_enriched_body(body: dict[str, Any] | None, ep: Any) -> dict[str, Any] | None:
    """Refresh an already-built enriched body's finish-line + epoch fields.

    Phase 4 cut 13 moved the stamp from deepsleep gate entry to the first
    statement inside the commit lock, which is AFTER ``EpisodeStore.close``
    builds the enriched body. Without this the body would carry ``timekeeper:
    {}`` and a zero commit epoch into the reflection window and
    ``_last_closed_body`` — silently, since both fields are optional by shape.
    Called once, inside the commit region, immediately after the stamp.

    Deliberately narrow: only the two blocks the stamp decides. Everything else
    in the body was true when it was built and re-deriving it here would
    re-enrich an episode mid-terminus for no reason.

    Recorded consequence, not a bug: the DeepSleep journal entry embeds the
    episode body it CONSOLIDATED FROM, and that copy is taken in the lane-local
    half, before this episode has crossed the finish line — so its embedded
    ``timekeeper`` is empty. The authoritative arrival record is the
    ``PriorEpisode`` row ``archive_episode`` writes after the commit region,
    which carries the stamp, and every consumer that needs finish-line order
    reads that row (``journal_window`` re-sorts by it).
    """
    if not isinstance(body, dict):
        return body
    base = prior_episode_from_episode(ep)
    stamp = getattr(ep, "timekeeper", None)
    body["timekeeper"] = dict(stamp) if isinstance(stamp, dict) else {}
    body["memory_epoch"] = {
        "start": base.memory_epoch_start,
        "commit": base.memory_epoch_commit,
        "causal_parent_episode_ids": list(base.causal_parent_episode_ids or ()),
        "causal_parents_complete": base.causal_parents_complete,
        "pin": dict(getattr(ep, "memory_epoch_pin", None) or {}),
    }
    return body


def _prior_episode_line(pe: PriorEpisode) -> str:
    """Internal helper: stable JSON line (sorted keys) for tests/debug."""
    return json.dumps(pe.to_dict(), ensure_ascii=False, sort_keys=True)


def archive_episode(ep: Any, path: str | Path | None = None) -> None:
    """Append exactly one PriorEpisode JSONL record (fail-hard, fsynced).

    Called from ``EpisodeStore.close()`` after per-episode journal write;
    a failed archive must propagate so the next episode cannot ignite on
    uncommitted continuity.
    """

    resolved = None
    try:
        from pressure_loop.policy import episode_journal_path

        resolved = _resolve_path(path or episode_journal_path())
        resolved.parent.mkdir(parents=True, exist_ok=True)

        from pressure_loop.durable_write import append_jsonl_line

        pe = prior_episode_from_episode(ep)
        append_jsonl_line(resolved, _prior_episode_line(pe))
        try:
            from guardian.terminal_feedback import emit_data, emit_guardian_event

            emit_data(
                "pressure",
                "archive_episode",
                episode_id=str(getattr(ep, "episode_id", "") or ""),
                path=str(resolved),
                ok=True,
            )
            emit_guardian_event(
                "omega.pressure",
                "archive_episode",
                episode_id=str(getattr(ep, "episode_id", "") or ""),
                path=str(resolved),
                ok=True,
            )
        except Exception:
            pass
    except Exception as exc:
        try:
            from guardian.terminal_feedback import emit_data, emit_guardian_event

            emit_data(
                "pressure",
                "archive_episode",
                episode_id=str(getattr(ep, "episode_id", "") or ""),
                path=str(resolved) if resolved is not None else "",
                ok=False,
                error=str(exc),
            )
            emit_guardian_event(
                "omega.pressure",
                "archive_episode",
                episode_id=str(getattr(ep, "episode_id", "") or ""),
                ok=False,
                error=str(exc),
            )
        except Exception:
            pass
        raise


def journal_episodes_by_ids(
    episode_ids: list[str], path: str | Path | None = None
) -> list[PriorEpisode]:
    """Load PriorEpisode records for the given episode_ids (journal order preserved)."""
    if not episode_ids:
        return []
    try:
        from pressure_loop.policy import episode_journal_path

        resolved = _resolve_path(path or episode_journal_path())
        if not resolved.is_file():
            return []

        wanted = {str(eid) for eid in episode_ids}
        found: dict[str, PriorEpisode] = {}
        for raw in resolved.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    continue
                eid = str(payload.get("episode_id", ""))
                if eid in wanted:
                    found[eid] = PriorEpisode.from_dict(payload)
            except Exception:
                continue
        result = [found[eid] for eid in episode_ids if eid in found]
        try:
            from guardian.terminal_feedback import emit_data, emit_guardian_event

            emit_data(
                "pressure",
                "journal_episodes_by_ids",
                path=str(resolved),
                ok=True,
                requested=len(episode_ids),
                found=len(result),
            )
            emit_guardian_event(
                "omega.pressure",
                "journal_episodes_by_ids",
                path=str(resolved),
                ok=True,
                requested=len(episode_ids),
                found=len(result),
            )
        except Exception:
            pass
        return result
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("journal episodes by ids failed (non-fatal): %s", exc)
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "journal_episodes_by_ids",
                ok=False,
                error=str(exc),
            )
        except Exception:
            pass
        return []


CAUSAL_PARENT_SCAN_LIMIT = 512


def episodes_committed_between(
    after_seq: int,
    through_seq: int,
    *,
    exclude_episode_id: str = "",
    path: str | Path | None = None,
    scan_limit: int = CAUSAL_PARENT_SCAN_LIMIT,
) -> tuple[list[str], bool]:
    """Episode ids whose finish-line ``commit_seq`` falls in ``(after, through]``.

    Phase 4 cut 12: this is the causal-parent set — the episodes that crossed
    the finish line while the asking episode was thinking, so their writes are
    provably absent from its reads. Returned oldest→newest by
    ``finish_line_key``, which is the same order memory landed in.

    Returns ``(ids, complete)``. ``complete`` is False when the scan could not
    prove it saw the whole interval:

    * the journal was unreadable, or the tail window (``scan_limit`` rows)
      filled without reaching a row at or below ``after_seq``;
    * **or fewer rows were found than the interval contains** (Phase 4 cut 13).
      ``commit_seq`` is dense — every stamp is one episode's terminus — so an
      interval of width N holds exactly N rows once they all land. Cut 13 moved
      the stamp inside the commit lock and deliberately left ``archive_episode``
      outside it, so an episode that stamped a moment ago may still be between
      the commit region and its journal append when this scan runs. That gap is
      real and it is brief; what it must never be is silent. Cut 12's docstring
      promised this flag would say so out loud instead of quietly under-
      reporting, and this is where it does.

    A caller that records ``complete=False`` is saying "this set may be short" —
    the alternative, reading an unbounded journal on every deepsleep gate,
    trades a rare honest gap for a permanent cost.

    Rows without a stamp (pre-cut-3) are skipped rather than treated as seq 0:
    they predate the counter entirely and cannot be placed in an interval.
    """
    try:
        low = int(after_seq)
        high = int(through_seq)
    except (TypeError, ValueError):
        return [], False
    if high <= low:
        # Nothing committed between admission and this episode's own stamp.
        # An empty set here is a RESULT, and a complete one.
        return [], True

    try:
        from pressure_loop.policy import episode_journal_path

        resolved = _resolve_path(path or episode_journal_path())
        excluded = str(exclude_episode_id or "")
        # The asking episode's own stamp closes the interval, and its row is
        # written AFTER the commit region (cut 13) — so it is expected to be
        # missing here and must not count against completeness.
        seen_in_interval: set[int] = {high} if excluded else set()

        if not resolved.is_file():
            # No journal file is only a complete answer when the interval holds
            # nothing but the asker's own stamp. A wider interval means other
            # episodes DID cross the line, and their rows are simply not here.
            return [], len(seen_in_interval) >= max(0, high - low)

        lines = resolved.read_text(encoding="utf-8").splitlines()
        limit = max(1, int(scan_limit))
        tail = lines[-limit:]
        reached_floor = len(tail) == len(lines)

        from core.timekeeper import finish_line_key

        rows: list[tuple[tuple[int, int], str]] = []
        for raw in tail:
            raw = raw.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            stamp = payload.get("timekeeper")
            if not isinstance(stamp, dict):
                continue
            try:
                seq = int(stamp.get("commit_seq") or 0)
            except (TypeError, ValueError):
                continue
            if seq <= 0:
                continue
            if seq <= low:
                reached_floor = True
                continue
            if seq > high:
                continue
            episode_id = str(payload.get("episode_id") or "")
            if not episode_id:
                continue
            seen_in_interval.add(seq)
            if episode_id == excluded:
                continue
            rows.append((finish_line_key(stamp), episode_id))

        # Cut 13: the interval is dense in commit_seq — every stamp is one
        # episode's terminus. Seeing fewer of its sequence numbers than it
        # contains means a row that WILL exist has not been appended yet (the
        # archive runs after the commit region) or never will (a failure-soft
        # archive that lost one). Either way the parent set is short, and saying
        # so is the whole contract of this flag.
        #
        # Cut 13 review — the third cause, and the only permanent one: a
        # terminus that stamped and then RAISED inside the commit region spends
        # a sequence number whose row is never written (the retry takes a fresh
        # stamp). Every episode whose interval spans that hole reports
        # incomplete for good. That is the flag erring loud on a terminus that
        # genuinely failed, bounded to the episodes thinking at that moment —
        # which is why False here means "this set may be short", never "a lane
        # is misbehaving right now".
        if len(seen_in_interval) < max(0, high - low):
            reached_floor = False

        rows.sort(key=lambda row: row[0])
        return [episode_id for _key, episode_id in rows], bool(reached_floor)
    except Exception as exc:  # noqa: BLE001 - the journal read is failure-soft
        _LOG.debug("causal parent scan failed (non-fatal): %s", exc)
        return [], False


def journal_window(n: int | None = None, path: str | Path | None = None) -> list[PriorEpisode]:
    """Read the last N PriorEpisode JSONL records (oldest -> newest).

    A journal that does not exist yet is an empty history. One that exists but cannot be read —
    not a file, undecodable bytes, a line that is not a PriorEpisode — raises
    ``EpisodeJournalUnreadable`` naming the file and line, because skipping it would report
    "that episode never happened" (AGENTS.md section 2.1).
    """
    from pressure_loop.policy import episode_journal_path, episode_journal_window_default

    resolved = _resolve_path(path or episode_journal_path())
    if not resolved.exists():
        return []
    if not resolved.is_file():
        raise EpisodeJournalUnreadable(f"the episode journal {resolved} is not a file")

    window = int(episode_journal_window_default() if n is None else n)
    if window <= 0:
        return []

    try:
        lines = resolved.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise EpisodeJournalUnreadable(f"the episode journal {resolved} cannot be read: {exc}") from exc

    start = max(0, len(lines) - window)
    out: list[PriorEpisode] = []
    for number, raw in enumerate(lines[start:], start=start + 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError(f"a journal row must be a JSON object, got {type(payload).__name__}")
            out.append(PriorEpisode.from_dict(payload))
        except Exception as exc:  # noqa: BLE001 - re-raised with its location and cause
            raise EpisodeJournalUnreadable(
                f"the episode journal {resolved} line {number} is not a readable PriorEpisode: {exc}"
            ) from exc
    # Phase 3 cut 3 — the timekeeper tells the true sequence: lane-parallel
    # episodes append in close-completion order, which can lag finish-line
    # arrival by a race; stamped rows re-sort by (commit_ms, commit_seq).
    # The sort is stable, so unstamped legacy rows keep their file order
    # ahead of every stamped row (they predate the timekeeper, truthfully).
    from core.timekeeper import finish_line_key

    out.sort(key=lambda pe: finish_line_key(pe.timekeeper))
    # journal_window is a READ, called on every HUD telemetry refresh (and by
    # gather/topic paths) — emitting a "journal_window ok" event on each read
    # floods the terminal with idle noise (operator 2026-07-09: "we need to quiet
    # it"). A read is not an event; stay silent on success. Journal WRITES keep
    # their own telemetry where an episode is actually journaled.
    return out

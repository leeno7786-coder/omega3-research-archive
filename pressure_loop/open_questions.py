"""
Open Questions — Omega's journal of unanswered questions to the operator.

Per operator directive (2026-05-14): when the response_engine dispatches
a curiosity-driven question to the user (tension_response,
follow_up_question, conversational_response, action_proposal), the
QUESTION ITSELF gets written to a journal entry that stays OPEN until
the operator answers. Omega can reflect on the open list across cycles
(meta-cog on its own unanswered questions, articulate further questions
on the same topic, see thinking patterns). When the operator's reply
arrives, the latest open question is marked answered and the answer
text is attached. Next omega cycle sees the answered question, can
process what was learned, and may produce new queries based on the
answer.

This module owns the canonical write surface for that journal:

    record_open_question(curiosity_id, act, question_text, ...)
        Called by response_engine.dispatch_response after successful
        dispatch. Returns the new question_id. Stores under
        guardian/state/omega_open_questions.jsonl per the Guardian
        sole-writer doctrine (writes go through guardian/ or memory/).

    attach_user_answer(answer_text, answered_cycle, ...)
        Called by BrainLoop.run at the start of every user-source
        cycle. Finds the most-recent OPEN question, marks it answered
        with the operator's text. Returns the question_id that was
        attached, or None if no open question exists. The
        "latest-open-wins" rule matches how operators actually think
        ("I'm answering the thing it just asked").

    list_open() / list_answered_recent(limit)
        Read APIs for omega phase_1 intake to surface to the evaluator.

    mark_resolved(question_id, by, note)
        Called by omega phase_2_evaluate after Omega has processed the
        answered question (linked back to source curiosity, integrated
        the answer into its model). Resolved questions stay in the log
        but won't be re-surfaced as needing attention.

ARCHITECTURAL RULES:
    - Append-only JSONL writes via the same atomic-append pattern used
      by curiosity_store + tool_decisions. tmp + fsync + atomic rename
      not needed here because each record is one line; partial writes
      get filtered on read by the malformed-line skip path.
    - Persist file lives at `guardian/state/omega_open_questions.jsonl`.
    - "Latest-open-wins" attachment rule is intentional. Topic-based
      similarity matching is a different system; this module stays
      deterministic.
    - When two consecutive Omega cycles dispatch questions before the
      operator replies, the older one stays OPEN — the operator's
      answer attaches only to the newer (latest-open). Older questions
      remain visible to omega for reflection; if they're really stale,
      omega phase_2 can call mark_resolved() with a "superseded" note.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass, fields
from typing import Any, Dict, List, Optional

from core.paths import OMEGA_ROOT
from guardian.terminal_feedback import phase_span

log = logging.getLogger("omega.pressure_loop.open_questions")

OPEN_QUESTIONS_PATH = OMEGA_ROOT / "guardian" / "state" / "omega_open_questions.jsonl"

_lock = threading.Lock()

# Status values
STATUS_OPEN = "open"
STATUS_ANSWERED = "answered"
STATUS_RESOLVED = "resolved"
STATUS_SUPERSEDED = "superseded"

_VALID_STATUSES = frozenset({STATUS_OPEN, STATUS_ANSWERED, STATUS_RESOLVED, STATUS_SUPERSEDED})


@dataclass
class OpenQuestion:
    """One question Omega asked the operator. Lives in the open-questions
    journal; status transitions on operator reply (open → answered) and
    omega processing (answered → resolved)."""

    question_id: str
    curiosity_id: str  # source curiosity that drove the dispatch
    act: str  # tension_response / follow_up_question / etc.
    question_text: str  # what Omega actually asked
    asked_cycle: str  # brain cycle_id from the dispatch
    asked_at: str  # ISO8601 UTC
    status: str  # one of _VALID_STATUSES
    answer_text: Optional[str] = None
    answered_cycle: Optional[str] = None
    answered_at: Optional[str] = None
    resolved_by: Optional[str] = None
    resolved_note: Optional[str] = None
    resolved_at: Optional[str] = None
    rationale: Optional[str] = None  # source curiosity's why-this-was-asked
    # Global operator outreach metadata. These optional fields let
    # Global leave a user-facing response open in the Omega journal
    # before it is spoken, then keep it open until the operator replies.
    global_operator_outreach: bool = False
    outreach_id: Optional[str] = None
    outreach_status: Optional[str] = None  # pending_speak | spoken
    fingerprint: Optional[str] = None
    response_seed: Optional[str] = None
    source: Optional[str] = None
    source_id: Optional[str] = None
    episode_id: Optional[str] = None
    source_action_cycle_id: Optional[str] = None
    urgency: Optional[float] = None
    extra: Optional[Dict[str, Any]] = None
    spoken_cycle: Optional[str] = None
    spoken_at: Optional[str] = None
    memory: Optional[Dict[str, Any]] = None
    asked_window_id: Optional[str] = None
    windows_survived: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Optional["OpenQuestion"]:
        if not isinstance(d, dict):
            return None
        if "question_id" not in d or "asked_at" not in d:
            return None
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in valid})


# ─────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    # Phase 2 §P2-S20: open question lifecycle timestamps route
    # through the temporal authority.
    from core.symphony_time import now_iso

    return now_iso()


def _new_question_id() -> str:
    return f"oq-{uuid.uuid4().hex[:12]}"


def _append(record: Dict[str, Any]) -> bool:
    """Atomic append + fsync. Returns True on success."""
    try:
        OPEN_QUESTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with _lock:
            with open(OPEN_QUESTIONS_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        return True
    except OSError as exc:
        log.warning("open_questions: append failed: %s", exc)
        return False


def _load_all() -> List[OpenQuestion]:
    """Return every record in chronological (file) order. Each
    question_id appears multiple times (one record per state change);
    the LATEST record per question_id is the current state."""
    if not OPEN_QUESTIONS_PATH.exists():
        return []
    out: List[OpenQuestion] = []
    with _lock:
        with open(OPEN_QUESTIONS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    parsed = json.loads(s)
                except json.JSONDecodeError:
                    continue
                q = OpenQuestion.from_dict(parsed)
                if q is not None:
                    out.append(q)
    return out


def _latest_per_id(records: List[OpenQuestion]) -> Dict[str, OpenQuestion]:
    """Reduce a chronological list to {question_id: latest_state}."""
    out: Dict[str, OpenQuestion] = {}
    for r in records:
        out[r.question_id] = r  # last write wins
    return out


def _copy_question(target: OpenQuestion, **overrides: Any) -> OpenQuestion:
    data = target.to_dict()
    data.update(overrides)
    return OpenQuestion.from_dict(data)  # type: ignore[return-value]


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def record_open_question(
    *,
    curiosity_id: str,
    act: str,
    question_text: str,
    asked_cycle: str,
    rationale: Optional[str] = None,
) -> Optional[str]:
    """Record a new open question. Called by `response_engine.
    dispatch_response` after a successful dispatch produces a cycle_id.

    Returns the new question_id, or None on persistence failure.
    """
    with phase_span(
        "open_questions", "record", curiosity_id=curiosity_id, act=act, cycle_id=asked_cycle or ""
    ) as span:
        if not (curiosity_id and act and question_text and asked_cycle):
            log.warning(
                "open_questions.record_open_question: missing required field "
                "(curiosity_id=%r act=%r question_text_len=%d asked_cycle=%r)",
                curiosity_id,
                act,
                len(question_text or ""),
                asked_cycle,
            )
            span.skip("missing required field")
            return None

        from pressure_loop.reflection_window import load_reflection_window

        question_id = _new_question_id()
        active_window = load_reflection_window()
        asked_window_id = active_window.window_id if active_window else None
        q = OpenQuestion(
            question_id=question_id,
            curiosity_id=curiosity_id,
            act=act,
            question_text=question_text.strip(),
            asked_cycle=asked_cycle,
            asked_at=_now_iso(),
            status=STATUS_OPEN,
            rationale=rationale,
            asked_window_id=asked_window_id,
            windows_survived=0,
        )
        if _append(q.to_dict()):
            log.info(
                "open_questions: opened %s (curiosity=%s act=%s asked_cycle=%s)",
                question_id,
                curiosity_id,
                act,
                asked_cycle,
            )
            span.set(question_id=question_id, asked_cycle=asked_cycle)
            return question_id
        from guardian.terminal_feedback import emit_data

        emit_data(
            "pressure",
            "record_open_question",
            status="fail",
            reason="append_failed",
            ok=False,
            error="append failed",
        )
        span.set(recovery="skip_cycle").fail("append failed")
        return None


def increment_windows_survived_for_open_questions() -> None:
    """After a window archives, increment windows_survived for every still-OPEN question."""
    records = _load_all()
    latest = _latest_per_id(records)
    for q in latest.values():
        if q.status != STATUS_OPEN:
            continue
        incremented = _copy_question(q, windows_survived=int(q.windows_survived or 0) + 1)
        _append(incremented.to_dict())


def find_open_for_curiosity(curiosity_id: str) -> Optional[OpenQuestion]:
    """Return the newest OPEN question for a source curiosity.

    This is the idempotency seam for proactive ASK paths. A suppressed
    speech attempt and a later successful dispatch may both refer to
    the same curiosity; the journal should remember "waiting on this"
    once instead of accumulating duplicate open questions.
    """
    if not curiosity_id:
        return None
    latest = _latest_per_id(_load_all())
    matches = [
        q for q in latest.values() if q.status == STATUS_OPEN and q.curiosity_id == curiosity_id
    ]
    if not matches:
        return None
    matches.sort(key=lambda q: q.asked_at, reverse=True)
    return matches[0]


def ensure_open_question(
    *,
    curiosity_id: str,
    act: str,
    question_text: str,
    asked_cycle: str,
    rationale: Optional[str] = None,
) -> Optional[str]:
    """Idempotently record an open operator question.

    Returns an existing open question id for ``curiosity_id`` when one
    is already waiting; otherwise appends a new OPEN record using the
    same required fields as ``record_open_question``.
    """
    with phase_span(
        "open_questions", "ensure", curiosity_id=curiosity_id, cycle_id=asked_cycle or ""
    ) as span:
        existing = find_open_for_curiosity(curiosity_id)
        if existing is not None:
            span.set(existing_question_id=existing.question_id, created=False)
            return existing.question_id
        result = record_open_question(
            curiosity_id=curiosity_id,
            act=act,
            question_text=question_text,
            asked_cycle=asked_cycle,
            rationale=rationale,
        )
        span.set(created=True, question_id=result)
        return result


def attach_user_answer(
    *,
    answer_text: str,
    answered_cycle: str,
) -> Optional[str]:
    """Find the latest OPEN question and mark it answered with the
    operator's reply. Returns the question_id that received the
    answer, or None if no open question exists.

    Called by `BrainLoop.run` at the start of every user-source cycle
    BEFORE phases run, so downstream phases (memory_operations,
    retrieve, store) can reference the freshly-attached answer through
    the journal if they need to.

    "Latest-open-wins" by `asked_at` desc.
    """
    with phase_span(
        "open_questions",
        "attach_answer",
        answer_chars=len(answer_text or ""),
        cycle_id=answered_cycle or "",
    ) as span:
        if not (answer_text and answered_cycle):
            span.skip("missing args")
            return None
        records = _load_all()
        if not records:
            span.skip("no records")
            return None
        latest = _latest_per_id(records)
        open_qs = [q for q in latest.values() if q.status == STATUS_OPEN]
        if not open_qs:
            span.skip("no open questions")
            return None
        open_qs.sort(key=lambda q: q.asked_at, reverse=True)
        target = open_qs[0]

        answered = _copy_question(
            target,
            status=STATUS_ANSWERED,
            answer_text=answer_text.strip(),
            answered_cycle=answered_cycle,
            answered_at=_now_iso(),
        )
        if _append(answered.to_dict()):
            log.info(
                "open_questions: attached answer to %s (curiosity=%s "
                "answered_cycle=%s answer_chars=%d)",
                target.question_id,
                target.curiosity_id,
                answered_cycle,
                len(answer_text),
            )
            span.set(question_id=target.question_id, curiosity_id=target.curiosity_id)
            return target.question_id
        span.set(recovery="skip_cycle").fail("append failed")
        return None


def mark_resolved(
    question_id: str,
    *,
    resolved_by: str,
    resolved_note: str = "",
) -> bool:
    """Omega has processed the answered question — link to source
    curiosity, integrate the answer, and any follow-up curiosities are
    already produced. Mark it resolved so the open-list view doesn't
    re-surface it. Append-only — original answered record stays.

    Idempotent: re-resolving an already-resolved question returns True
    without error.
    """
    with phase_span(
        "open_questions", "mark_resolved", question_id=question_id, cycle_id=question_id
    ) as span:
        if not question_id:
            span.skip("empty question_id")
            return False
        records = _load_all()
        latest = _latest_per_id(records)
        target = latest.get(question_id)
        if target is None:
            log.warning(
                "open_questions.mark_resolved: question_id=%r not found",
                question_id,
            )
            span.skip("not found")
            return False
        if target.status == STATUS_RESOLVED:
            span.skip("already resolved")
            return True

        resolved = _copy_question(
            target,
            status=STATUS_RESOLVED,
            resolved_by=resolved_by,
            resolved_note=resolved_note,
            resolved_at=_now_iso(),
        )
        if _append(resolved.to_dict()):
            span.set(resolved_by=resolved_by)
            return True
        span.set(recovery="skip_cycle").fail("append failed")
        return False


def mark_superseded(
    question_id: str,
    *,
    reason: str = "",
) -> bool:
    """Omega decides this open question is no longer worth pursuing
    (e.g., the underlying curiosity got resolved by other means, or a
    newer question subsumed it). Append a superseded record so the open
    list stops surfacing it."""
    with phase_span(
        "open_questions", "mark_superseded", question_id=question_id, cycle_id=question_id
    ) as span:
        if not question_id:
            span.skip("empty question_id")
            return False
        records = _load_all()
        latest = _latest_per_id(records)
        target = latest.get(question_id)
        if target is None or target.status == STATUS_SUPERSEDED:
            span.skip("not found or already superseded")
            return False

        superseded = _copy_question(
            target,
            status=STATUS_SUPERSEDED,
            resolved_by="omega:supersede",
            resolved_note=reason,
            resolved_at=_now_iso(),
        )
        if _append(superseded.to_dict()):
            span.set(reason=reason[:80] if reason else None)
            return True
        span.set(recovery="skip_cycle").fail("append failed")
        return False


def find_active_global_operator_outreach() -> Optional[OpenQuestion]:
    """Return the one OPEN Global operator outreach, newest first.

    OPEN includes both pending_speak and spoken-but-not-answered rows.
    The operator reply transitions the row to ANSWERED, removing it
    from this active view.
    """
    with phase_span("open_questions", "find_active_outreach", cycle_id="") as span:
        records = _load_all()
        latest = _latest_per_id(records)
        matches = [
            q for q in latest.values() if q.status == STATUS_OPEN and q.global_operator_outreach
        ]
        if not matches:
            span.skip("none")
            return None
        matches.sort(key=lambda q: q.asked_at, reverse=True)
        result = matches[0]
        span.set(question_id=result.question_id, curiosity_id=result.curiosity_id)
        return result


def ensure_global_operator_outreach(
    *,
    curiosity_id: str,
    act: str,
    question_text: str,
    asked_cycle: str,
    outreach_id: str,
    outreach_status: str,
    fingerprint: str,
    response_seed: str,
    source: str,
    source_id: str,
    episode_id: str,
    source_action_cycle_id: str = "",
    urgency: float = 0.0,
    extra: Optional[Dict[str, Any]] = None,
    rationale: Optional[str] = None,
) -> tuple[Optional[str], bool]:
    """Open a Global operator outreach row in the Omega journal.

    Returns ``(question_id, created)``. If any Global operator outreach
    is already OPEN, it returns that id with ``created=False`` to
    enforce the "one globally" rule in the journal itself.
    """
    with phase_span(
        "open_questions",
        "ensure_global_outreach",
        outreach_id=outreach_id,
        cycle_id=asked_cycle or "",
    ) as span:
        existing = find_active_global_operator_outreach()
        if existing is not None:
            span.set(existing_question_id=existing.question_id, created=False)
            return existing.question_id, False
        if not (curiosity_id and act and question_text and asked_cycle and outreach_id):
            span.skip("missing required field")
            return None, False
        question_id = record_open_question(
            curiosity_id=curiosity_id,
            act=act,
            question_text=question_text,
            asked_cycle=asked_cycle,
            rationale=rationale,
        )
        if not question_id:
            span.set(recovery="skip_cycle").fail("record failed")
            return None, False
        target = get(question_id)
        if target is None:
            # Tests often patch record_open_question as the seam being
            # asserted; in that case there is no real row to enrich.
            span.set(question_id=question_id, created=True)
            return question_id, True
        enriched = _copy_question(
            target,
            curiosity_id=curiosity_id,
            act=act,
            question_text=question_text.strip(),
            asked_cycle=asked_cycle,
            status=STATUS_OPEN,
            rationale=rationale,
            global_operator_outreach=True,
            outreach_id=outreach_id,
            outreach_status=outreach_status,
            fingerprint=fingerprint,
            response_seed=response_seed,
            source=source,
            source_id=source_id,
            episode_id=episode_id,
            source_action_cycle_id=source_action_cycle_id,
            urgency=float(urgency or 0.0),
            extra=dict(extra or {}),
        )
        if _append(enriched.to_dict()):
            span.set(question_id=question_id, created=True)
            return question_id, True
        span.set(recovery="skip_cycle").fail("append failed")
        return None, False


def mark_global_operator_outreach_spoken(
    question_id: str,
    *,
    dispatched_cycle: str,
) -> bool:
    """Mark a Global operator outreach row as spoken while leaving it OPEN."""
    with phase_span(
        "open_questions", "mark_spoken", question_id=question_id, cycle_id=dispatched_cycle or ""
    ) as span:
        target = get(question_id)
        if target is None or not target.global_operator_outreach:
            span.skip("not found or not global outreach")
            return False
        if target.status != STATUS_OPEN:
            span.skip("not open")
            return False
        if target.outreach_status == "spoken":
            span.skip("already spoken")
            return True
        spoken = _copy_question(
            target,
            outreach_status="spoken",
            spoken_cycle=dispatched_cycle,
            spoken_at=_now_iso(),
        )
        if _append(spoken.to_dict()):
            span.set(dispatched_cycle=dispatched_cycle)
            return True
        span.set(recovery="skip_cycle").fail("append failed")
        return False


def mark_global_operator_outreach_memory(
    question_id: str,
    *,
    memory: Dict[str, Any],
) -> bool:
    """Attach memory write result to an answered Global outreach row."""
    target = get(question_id)
    if target is None or not target.global_operator_outreach:
        return False
    updated = _copy_question(target, memory=dict(memory or {}))
    return _append(updated.to_dict())


def list_open() -> List[OpenQuestion]:
    """Return current OPEN questions (latest state per question_id),
    sorted by asked_at desc (newest first)."""
    records = _load_all()
    latest = _latest_per_id(records)
    out = [q for q in latest.values() if q.status == STATUS_OPEN]
    out.sort(key=lambda q: q.asked_at, reverse=True)
    return out


def list_answered_recent(limit: int = 10) -> List[OpenQuestion]:
    """Return ANSWERED-but-not-yet-RESOLVED questions for omega phase_2
    to process. Sorted by answered_at desc."""
    records = _load_all()
    latest = _latest_per_id(records)
    out = [q for q in latest.values() if q.status == STATUS_ANSWERED]
    out.sort(key=lambda q: (q.answered_at or "", q.asked_at), reverse=True)
    return out[: max(0, int(limit))]


def asked_curiosity_ids() -> set:
    """curiosity_ids that have a durable open-question record — i.e.
    Omega has already RECORDED this query (open, awaiting answer, or
    answered-and-processing). Excludes only the closed states
    (resolved / superseded).

    Operator spec (2026-05-29): an open query relieves its own pressure
    the moment it is recorded and is BYPASSED until resolved — it must
    never re-hold the loop. Recording is the relief; SPEAKING is a
    separate async concern (cue_desk.dispatch_pending_if_present speaks
    queued/pending_speak rows when the channel is free), so a
    pending_speak row is bypassed here too — pressure does NOT keep
    firing to "drive the speak" (that was the hang: queued questions hit
    `already_open`, never relieved, froze cognition). Because the marker
    lives in the durable jsonl, the bypass survives reboots.
    """
    latest = _latest_per_id(_load_all())
    out = set()
    for q in latest.values():
        if not q.curiosity_id:
            continue
        # Any open (recorded, regardless of spoken status) or answered
        # (being processed) row = recorded -> bypass. Closed states
        # (resolved/superseded) are done and must not bypass.
        if q.status in (STATUS_OPEN, STATUS_ANSWERED):
            out.add(q.curiosity_id)
    return out


def get(question_id: str) -> Optional[OpenQuestion]:
    """Return the latest-state record for one question_id."""
    if not question_id:
        return None
    records = _load_all()
    latest = _latest_per_id(records)
    return latest.get(question_id)


def status_counts() -> Dict[str, int]:
    """Diagnostic — count by status (latest state per question_id)."""
    records = _load_all()
    latest = _latest_per_id(records)
    out = {STATUS_OPEN: 0, STATUS_ANSWERED: 0, STATUS_RESOLVED: 0, STATUS_SUPERSEDED: 0}
    for q in latest.values():
        if q.status in out:
            out[q.status] += 1
    return out

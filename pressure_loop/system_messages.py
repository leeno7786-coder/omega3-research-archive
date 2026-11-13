"""Durable proactive questions/results and their operator responses.

Operator spec 2026-06-12 (infancy-stage proactive speech for the initial
release): this module is BOTH the queue for the mind's outbound queries and
the mechanism for attaching operator responses to them. The HUD surfaces
pending records in the right proactive pinboard and projects every record into
read-only Settings history.

Lifecycle (two-action baseline, operator decision 2026-06-12): a winning
ASK_OPERATOR episode's generated text lands here at episode close
(``cognition_chain._record_system_message``) with status ``pending`` (the
badge count). RESPOND attaches the reply AND feeds it back through the
operator input queue box as an ``operator_response`` contributor
(→ RESOLVE_QUERY) carrying the thread — Q and A travel together into the
next episode so the system can close the question out and use the answer as
signal. Responded records leave the active pinboard but remain durable. There
is no per-record ignore or delete action; only explicit reset/day-zero clears
the store.

Durable (``guardian/state/system_messages.json``, write-through) and
failure-soft: store I/O degrades and emits; it never halts cognition. The
day-zero factory wipe clears it with the rest of guardian/state.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

log = logging.getLogger("omega.pressure_loop.system_messages")

STATUS_PENDING = "pending"
STATUS_RESPONDED = "responded"

_MESSAGES: List[Dict[str, Any]] = []
_LOADED = False
# Whether the durable store could not be read. It is NOT a permanent verdict: every access
# retries the read until it succeeds (review 2026-09-16: one transient read denial disabled
# notices for the life of the process). While it stands, nothing is written over the file,
# and records created or changed meanwhile stay in memory until a successful read merges
# them with what the file holds.
_LOAD_FAILED = False
# Whether the in-memory store holds changes not yet durably published. A failed write is
# retryable work, not a lost write: every access retries it, and a caller that must know a
# record is durable publishes before trusting a cached copy (review 2026-09-16).
_DIRTY = False
_LOCK = threading.RLock()


def _store_path() -> Path:
    from core.paths import OMEGA_ROOT

    return OMEGA_ROOT / "guardian" / "state" / "system_messages.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit_degraded(operation: str, exc: Exception) -> None:
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "pressure",
            "system_messages",
            status="degraded",
            summary=f"{operation} persistence failed; in-memory store serving",
            ok=False,
            error=str(exc),
        )
    except Exception:
        pass


def _publish_atomically() -> None:
    """Replace the store file with the in-memory store in one step, or raise.

    Every write of the shared message file goes through here. Writing the file in place
    truncated it first, so a write that failed part-way destroyed messages that were already
    durable — including a notice some caller had already relied on (review 2026-09-16).
    """
    global _DIRTY
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(_MESSAGES, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    _DIRTY = False


def _persist() -> None:
    """Failure-soft publication: a failed write stays pending and is retried on next access."""
    global _DIRTY
    _DIRTY = True
    if _LOAD_FAILED:
        # The file holds messages this process could not read. Writing the in-memory subset
        # over it would destroy them; the change waits in memory for the merge on recovery.
        _emit_degraded("persist", RuntimeError("deferred: the store could not be read"))
        return
    try:
        _publish_atomically()
    except Exception as exc:
        _emit_degraded("persist", exc)


def _ensure_loaded() -> None:
    """Load the durable store once — and keep retrying for as long as it cannot be read.

    A successful read after a failure MERGES: the file's records keep their order, a record
    this process changed in memory meanwhile replaces its stored copy, and records created
    meanwhile follow. The merged store is then written back, so nothing made during the
    outage is lost and nothing already stored is overwritten.
    """
    global _LOADED, _LOAD_FAILED
    if _LOADED and not _LOAD_FAILED:
        if _DIRTY:
            _persist()        # a write that failed earlier is retried, never forgotten
        return
    try:
        path = _store_path()
        stored: List[Dict[str, Any]] = []
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("system messages store is not a list")
            stored = [d for d in raw if isinstance(d, dict)]
    except Exception as exc:
        if not _LOAD_FAILED:
            _emit_degraded("load", exc)
        _LOADED = True
        _LOAD_FAILED = True
        return
    recovering = _LOAD_FAILED
    stored_ids = {rec.get("id") for rec in stored}
    in_memory = {rec.get("id"): rec for rec in _MESSAGES}
    merged = [in_memory.get(rec.get("id"), rec) for rec in stored]
    merged += [rec for rec in _MESSAGES if rec.get("id") not in stored_ids]
    _MESSAGES[:] = merged
    _LOADED = True
    _LOAD_FAILED = False
    if _DIRTY or (recovering and in_memory):
        # Atomic, and retried on the next access if it fails: the merge never truncates the
        # file it just read.
        _persist()


def _persist_strict() -> None:
    """Persist atomically, or RAISE. For writes whose caller must know they are durable."""
    if _LOAD_FAILED:
        raise RuntimeError("system messages could not be read; refusing to write over them")
    _publish_atomically()


def append_system_notice_once(
    *,
    source_ref: str,
    text: str,
    episode_id: str,
    opened_by: str,
    kind: str,
    meta: Optional[Dict[str, Any]] = None,
    thread_id: str = "",
) -> tuple[Dict[str, Any], bool]:
    """Append ONE announcement for ``source_ref`` — durably and exactly once, or raise.

    Returns ``(record, created)``. The existing-notice check, the append and the durable
    write happen under this store's lock, so two concurrent callers cannot both append
    (review 2026-09-16: a check outside the lock let both see nothing). The write is strict:
    the ordinary path serves from memory when persistence fails, so a caller recording "the
    operator was told" could record a notice that a restart then loses. Here a failed write
    removes the record again and raises, and a store that could not be LOADED is refused
    outright rather than overwritten with an in-memory subset. The notice joins working
    memory only once it is durable.
    """
    if not str(source_ref or "").strip():
        raise ValueError("a once-only notice requires its source_ref")
    with _LOCK:
        _ensure_loaded()
        if _LOAD_FAILED:
            raise RuntimeError("system messages could not be loaded; refusing a notice that "
                               "could overwrite them")
        for rec in _MESSAGES:
            if rec.get("source_ref") == source_ref:
                if _DIRTY:
                    # A cached notice is not proof it is durable: the store holds writes that
                    # have not landed. Publish them now, or raise, so no caller records a
                    # notice as sent that a restart would not find.
                    _persist_strict()
                return dict(rec), False
        record: Dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "ts": _now_iso(),
            "episode_id": str(episode_id or ""),
            "thread_id": str(thread_id or ""),
            "opened_by": str(opened_by or ""),
            "text": str(text or ""),
            "seed_question": "",
            "status": STATUS_PENDING,
            "response": None,
            "kind": str(kind),
            "source_ref": str(source_ref),
            "expects_answer": False,
        }
        if meta:
            record["meta"] = dict(meta)
        _MESSAGES.append(record)
        try:
            _persist_strict()
        except BaseException:
            _MESSAGES.remove(record)
            raise
    try:
        from memory.hot_buffer import append_turn

        safe = _cognition_safe_text(record)
        if safe:
            append_turn("assistant", safe)
    except Exception:
        log.debug("hot buffer append for system notice failed", exc_info=True)
    try:
        from guardian.terminal_feedback import emit_data

        emit_data("pressure", "system_message_appended", episode_id=record["episode_id"] or None,
                  summary=record["text"][:80], message_id=record["id"],
                  opened_by=record["opened_by"])
    except Exception:
        pass
    return dict(record), True


def _find(message_id: str) -> Optional[Dict[str, Any]]:
    for rec in _MESSAGES:
        if str(rec.get("id")) == str(message_id):
            return rec
    return None


def _cognition_safe_text(record: Dict[str, Any]) -> str:
    """What the COGNITIVE stores may hold for this record.

    Round-4 P0: the append path previously wrote ``record["text"]`` into the
    proactive-lifecycle ledger, the open-questions journal, AND the hot buffer —
    for an external_reminder that text is EXTERNAL human-authored content (a
    calendar title), and all three stores feed prompts/memory. External records
    therefore contribute only a generic, provenance-bearing line (the SourceLedger
    artifact ref); the operator-facing badge (``record["text"]``) keeps the
    readable reminder — HUD surfaces are for human eyes, cognitive stores are not.
    """
    kind = str(record.get("kind") or "")
    if kind == "external_reminder":
        ref = str(record.get("source_ref") or "").strip() or "unrecorded"
        return f"(external reminder delivered; content held at source {ref})"
    return str(record.get("text") or "")


def _sync_open_question_journal(record: Dict[str, Any]) -> None:
    """Mirror a pending text-app query into the open-questions journal (failure-soft).

    The journal is a COGNITIVE store — external_reminder records contribute the
    provenance line, never the external text (round-4 P0)."""
    episode_id = str(record.get("episode_id") or "").strip()
    text = _cognition_safe_text(record).strip()
    if not episode_id or not text:
        return
    try:
        from pressure_loop.open_questions import ensure_open_question

        seed = str(record.get("seed_question") or "").strip()
        qid = ensure_open_question(
            curiosity_id=f"system_query:{episode_id}",
            act="ASK_OPERATOR",
            question_text=text,
            asked_cycle=episode_id,
            rationale=seed or None,
        )
        if qid:
            record["open_question_id"] = str(qid)
    except Exception:
        log.debug("open question journal sync failed", exc_info=True)


def _attach_journal_answer(record: Dict[str, Any], answer_text: str) -> None:
    """Mark the matching open-questions row answered when the operator replies."""
    clean = str(answer_text or "").strip()
    if not clean:
        return
    if record.get("expects_answer") is False:
        # An ANNOUNCEMENT never opened a journal row, so there is nothing here to
        # resolve — and the fallback below is "latest open question wins", which
        # would silently attach a reply about a finished build to whatever
        # unrelated question the mind actually has outstanding.
        return
    try:
        from pressure_loop.open_questions import attach_user_answer, mark_resolved

        qid = str(record.get("open_question_id") or "").strip()
        if qid:
            mark_resolved(
                qid,
                resolved_by="operator:system_messages",
                resolved_note=clean[:500],
            )
            return
        attach_user_answer(
            answer_text=clean,
            answered_cycle=str(record.get("episode_id") or record.get("id") or ""),
        )
    except Exception:
        log.debug("open question journal answer sync failed", exc_info=True)


def current_outreach(*, question_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Read an exact pending question, or the newest pending question when no id is given."""
    with _LOCK:
        _ensure_loaded()
        pending = [rec for rec in _MESSAGES if rec.get("status") == STATUS_PENDING
                   and (question_id is None or rec.get("id") == question_id)]
        if not pending:
            return None
        rec = pending[-1]
        text = str(rec.get("text") or "").strip()
        if not text:
            return None
        msg_id = str(rec.get("id") or "")
        return {
            "question_id": msg_id,
            "outreach_id": msg_id,
            "question_text": text,
            "outreach_status": "pending",
            "act": "ASK_OPERATOR",
            "status": STATUS_PENDING,
            "episode_id": str(rec.get("episode_id") or ""),
            # The asking episode's thread, authoritative for the whole answer lifecycle.
            "thread_id": str(rec.get("thread_id") or ""),
            "seed_question": str(rec.get("seed_question") or ""),
            "proactive_thread_id": str(rec.get("proactive_thread_id") or ""),
            "proactive_policy_id": str(rec.get("proactive_policy_id") or ""),
            # external-ingress tagging (2026-07-17): lets the acknowledgment consume
            # path refuse to launder external text into Omega-authored memory.
            "kind": str(rec.get("kind") or ""),
            "source_ref": str(rec.get("source_ref") or ""),
            # kind-specific bindings: an agent question carries its numbered options and the
            # job whose task is paused on it.
            "meta": dict(rec.get("meta") or {}),
        }


def append_system_query(
    *,
    text: str,
    episode_id: str,
    opened_by: str,
    seed_question: str = "",
    kind: str = "",
    source_ref: str = "",
    meta: Optional[Dict[str, Any]] = None,
    expects_answer: bool = True,
    thread_id: str = "",
) -> Dict[str, Any]:
    """Append one system query for the operator (status ``pending``).

    ``kind``/``source_ref`` (2026-07-17): external-ingress tagging. A record with
    kind="external_reminder" carries EXTERNAL human-authored text (e.g. a calendar
    title) with its SourceLedger artifact ref — the acknowledgment fold-back reads the
    tag and must NOT convert that text into ordinary Omega-authored memory.
    ``meta`` (round-5): kind-specific bindings (e.g. a send-approval request's
    draft/revision/spec) — durable with the record, opaque to this module.

    ``expects_answer=False`` (Phase 4 cut 9 review) says this record is an
    ANNOUNCEMENT, not a question: it still lands in the panel, still counts in
    the badge, still joins working memory — but it does NOT open a lifecycle ask
    thread and does NOT open a row in the open-questions journal. That
    distinction is load-bearing, not cosmetic: the journal enforces "ONE
    operator question at a time" (``pressure_field._selected_evidence_terminal``
    bypasses EVERY ask terminal while any row is open) and every Omega packet
    renders the open rows back to the mind. A statement nobody has any reason to
    answer therefore stays open forever, silencing the mind's ability to ask the
    operator anything and telling it, every episode, that it is waiting on an
    answer to "Finished on the agent lane". An operator who does reply anyway is
    unaffected — ``_bind_policy_for_record`` lazily creates the thread then."""
    with _LOCK:
        _ensure_loaded()
        record: Dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "ts": _now_iso(),
            "episode_id": str(episode_id or ""),
            # T3: the cognitive thread that asked. Recorded HERE because the asking
            # episode is published during AUTO — before it is journaled and before any
            # successor is staged — so an answer has no other authority to read
            # (review 2026-09-16). Empty on records written before T3.
            "thread_id": str(thread_id or ""),
            "opened_by": str(opened_by or ""),
            "text": str(text or ""),
            "seed_question": str(seed_question or ""),
            "status": STATUS_PENDING,
            "response": None,
        }
        if kind:
            record["kind"] = str(kind)
        if source_ref:
            record["source_ref"] = str(source_ref)
        if meta:
            record["meta"] = dict(meta)
        if expects_answer:
            try:
                from pressure_loop.proactive_lifecycle import record_ask

                # The lifecycle ledger is a COGNITIVE store: external_reminder records
                # contribute the provenance line, never the external text (round-4 P0).
                thread = record_ask(
                    prompt_id=str(record["id"]),
                    episode_id=str(record["episode_id"]),
                    question=_cognition_safe_text(record),
                    why=str(record["seed_question"]),
                    pressure_winner=str(record["opened_by"]),
                    expected_response="A bounded operator policy for what Omega should do next.",
                    options=(),
                )
                record["proactive_thread_id"] = thread.thread_id
            except Exception:
                # The lifecycle ledger is operational continuity, not the authority
                # to suppress a question. Keep the operator-facing ask durable and
                # let the answer path lazily upgrade it when the ledger recovers.
                log.warning(
                    "proactive lifecycle ask recording failed; preserving system query",
                    exc_info=True,
                )
        else:
            # Durable on the record, so a restart reads the same contract the
            # append decided — the answer path checks it before falling back to
            # "latest open question wins".
            record["expects_answer"] = False
        _MESSAGES.append(record)
        if expects_answer:
            _sync_open_question_journal(record)
        _persist()
        # The question was said TO the operator — it joins the live-session
        # working memory so subsequent turns (and the multiturn block) see it.
        # COGNITIVE store: an external_reminder contributes only the provenance
        # line — the external text never enters working memory (round-4 P0).
        try:
            from memory.hot_buffer import append_turn

            _safe = _cognition_safe_text(record)
            if _safe:
                append_turn("assistant", _safe)
        except Exception:
            log.debug("hot buffer append for system query failed", exc_info=True)
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "system_message_appended",
                episode_id=record["episode_id"] or None,
                summary=record["text"][:80],
                message_id=record["id"],
                opened_by=record["opened_by"],
            )
        except Exception:
            pass
        return dict(record)


def append_result_presentation(
    *,
    text: str,
    episode_id: str,
    opened_by: str,
    result_id: str,
    thread_id: str,
    mastery_delta: float = 0.0,
) -> Optional[Dict[str, Any]]:
    """Present a proactive result EXACTLY like a system question (operator
    2026-07-12): it lands in the questions panel, counts in the badge, and
    STAYS pending until the operator responds — the response is the teacher's
    grade, and only then does the lifecycle fold (see ``respond_to_message``).

    Idempotent by result_id (a restart must not re-present). No lifecycle
    ``record_ask`` — the presentation belongs to the EXISTING thread that
    asked the original question; the panel record carries the linkage.
    """
    with _LOCK:
        _ensure_loaded()
        rid = str(result_id or "").strip()
        if not rid or not str(text or "").strip():
            return None
        for rec in _MESSAGES:
            if str(rec.get("result_id") or "") == rid:
                return dict(rec)  # already presented — restart dedup
        record: Dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "ts": _now_iso(),
            "episode_id": str(episode_id or ""),
            "opened_by": str(opened_by or ""),
            "text": str(text or ""),
            "seed_question": "",
            "status": STATUS_PENDING,
            "response": None,
            "kind": "result_presentation",
            "result_id": rid,
            "proactive_thread_id": str(thread_id or ""),
            "mastery_delta": max(0.0, min(1.0, float(mastery_delta or 0.0))),
        }
        _MESSAGES.append(record)
        _persist()
        # The presentation was said TO the operator — live-session working memory.
        try:
            from memory.hot_buffer import append_turn

            append_turn("assistant", record["text"])
        except Exception:
            log.debug("hot buffer append for result presentation failed", exc_info=True)
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "result_presented_to_operator",
                episode_id=record["episode_id"] or None,
                summary=record["text"][:80],
                message_id=record["id"],
                result_id=rid,
            )
        except Exception:
            pass
        return dict(record)


def _grade_presented_result(rec: Dict[str, Any], *, operator_text: str) -> None:
    """The operator responded to a presented result — THAT is the fold trigger.

    The teach-back square (operator 2026-07-12): Omega presents, the teacher
    grades, and only the graded result folds (resolve/continue + mastery delta
    stashed at presentation time). Failure-soft: the grade must reach the mind
    (contributor path in the caller) even if the ledger fold hiccups.
    """
    try:
        from pressure_loop.proactive_lifecycle import fold_presented_result

        foldback = fold_presented_result(
            str(rec.get("result_id") or ""),
            mastery_delta=float(rec.get("mastery_delta") or 0.0),
        )
        if foldback is None:
            log.warning(
                "graded result %s had no foldable ledger state",
                rec.get("result_id"),
            )
    except Exception:
        log.warning(
            "proactive result grade fold failed; operator grade still delivered",
            exc_info=True,
        )
    del operator_text  # the grade text rides the contributor, not the ledger


def list_messages(*, include_all: bool = True) -> List[Dict[str, Any]]:
    """All messages, newest last (the panel renders this directly)."""
    with _LOCK:
        _ensure_loaded()
        del include_all  # one list in the infancy stage; flag reserved
        return [dict(rec) for rec in _MESSAGES]


def unread_count() -> int:
    """The badge number: queries still waiting on the operator."""
    with _LOCK:
        _ensure_loaded()
        return sum(1 for rec in _MESSAGES if rec.get("status") == STATUS_PENDING)


def _bind_policy_for_record(rec: Dict[str, Any], *, answer_episode_id: str, operator_text: str):
    """Bind an answer, lazily upgrading questions created before the ledger."""
    try:
        from pressure_loop.proactive_lifecycle import (
            bind_operator_policy,
            record_ask,
            thread_for_prompt,
        )

        prompt_id = str(rec.get("id") or "")
        if thread_for_prompt(prompt_id) is None:
            # Lazy upgrade goes through the same cognition-safe gate as the append
            # path — an external_reminder never lands its text here either.
            thread = record_ask(
                prompt_id=prompt_id,
                episode_id=str(rec.get("episode_id") or f"legacy:{prompt_id}"),
                question=_cognition_safe_text(rec) or "Legacy operator question",
                why=str(rec.get("seed_question") or ""),
                pressure_winner=str(rec.get("opened_by") or "ask_operator"),
                expected_response="A bounded operator policy for what Omega should do next.",
                options=(),
            )
            rec["proactive_thread_id"] = thread.thread_id
        return bind_operator_policy(
            prompt_id=prompt_id,
            answer_episode_id=answer_episode_id,
            operator_text=operator_text,
        )
    except Exception:
        # Answer delivery is primary. A lifecycle outage must never make the HUD
        # endpoint reject the operator or strand a pre-upgrade question.
        log.warning(
            "proactive policy binding failed; preserving operator answer",
            exc_info=True,
        )
        return None


def respond_to_message(
    message_id: str,
    response_text: str,
    *,
    cycle_id: str = "",
    source_citation: Optional[Mapping[str, Any]] = None,
    verdict: str = "",
) -> Optional[Dict[str, Any]]:
    """Attach the operator's reply AND feed it back through the system.

    The reply rides the operator input queue box as an ``operator_response``
    contributor (existing rail: → RESOLVE_QUERY) carrying the thread
    (``system_query_id`` + the system's original question), one per pressure
    cycle, supremacy-pinned, restart-survivable — the reply is never lost.
    Returns the updated record, or None for an unknown/empty id.
    """
    # The operator's stated verdict on a presented result (``supports`` / ``contradicts`` /
    # ``mixed``), or "" when they stated none. Checked BEFORE anything is written: an
    # unrecognised verdict is refused rather than dropped, which would record that the operator
    # stated nothing when they did (memory/operator_feedback.py).
    from memory.operator_feedback import checked_verdict

    stated_verdict = checked_verdict(verdict)
    with _LOCK:
        _ensure_loaded()
        clean = str(response_text or "").strip()
        rec = _find(message_id)
        if rec is None or not clean:
            return None
        # Idempotency is decided under the same store lock and BEFORE policy
        # binding/enqueue. Concurrent HUD submissions therefore observe the
        # already-completed response instead of minting conflicting events.
        if rec.get("status") == STATUS_RESPONDED:
            return dict(rec)
        answer_cycle_id = str(cycle_id or uuid.uuid4())
        is_result_grade = str(rec.get("kind") or "") == "result_presentation"
        policy = None
        if is_result_grade:
            # The operator GRADED a presented result — the fold trigger the
            # presentation was waiting on (operator 2026-07-12).
            _grade_presented_result(rec, operator_text=clean)
        else:
            policy = _bind_policy_for_record(
                rec, answer_episode_id=answer_cycle_id, operator_text=clean
            )
        rec["status"] = STATUS_RESPONDED
        rec["response"] = {"text": clean, "ts": _now_iso()}
        if stated_verdict:
            rec["response"]["verdict"] = stated_verdict
        if policy is not None:
            rec["proactive_thread_id"] = policy.thread_id
            rec["proactive_policy_id"] = policy.policy_id
        _persist()
        # The panel answer is NOT appended to the hot buffer here (operator 2026-07-16).
        # It rides its operator_response contributor (below) into the answering episode and
        # graduates to the hot buffer via the per-episode SRAM bucket at that episode's close —
        # AFTER recall (memory.episode_sram; operator_response is in _OPERATOR_CONTRIBUTOR_IDS).
        # Appending it synchronously here polluted the answering episode's OWN recall (the HOT
        # lane surfaced the just-given answer — the "remembers before it's told" anomaly) and
        # double-appended (submit + close-flush).

        from pressure_loop.contributors import from_operator_message
        from pressure_loop.cognition_queue import enqueue_input

        # EXTERNAL-INGRESS laundering guard (ratified 2026-07-17): an external_reminder's
        # text is EXTERNAL human-authored content — never thread it into the memory-bound
        # contributor (cognition_chain mints the threaded Q+A into canonical memory
        # Guardian-free). Its provenance home is the SourceLedger artifact (source_ref).
        _is_external = str(rec.get("kind") or "") == "external_reminder"
        contributor_payload = {
            "message": clean,
            "ingress_class": "answer_to_question",
            "weight": 1.0,
            "cycle_id": answer_cycle_id,
            "system_query_id": str(rec["id"]),
            "system_query_text": "" if _is_external else str(rec.get("text") or ""),
            "system_query_episode_id": str(rec.get("episode_id") or ""),
            # What the operator was replying TO, and the verdict they stated on it. Both are host
            # facts the admission seam turns into the graded episode's evidence
            # (memory/operator_feedback.py); a reply with no verdict carries none.
            "system_query_kind": str(rec.get("kind") or ""),
            "system_query_verdict": stated_verdict,
        }
        if source_citation:
            contributor_payload["source_citation"] = dict(source_citation)
        if _is_external:
            contributor_payload["external_kind"] = "external_reminder"
            contributor_payload["source_ref"] = str(rec.get("source_ref") or "")
        if policy is not None:
            contributor_payload.update(
                proactive_thread_id=policy.thread_id,
                proactive_policy_id=policy.policy_id,
            )
        elif is_result_grade and rec.get("proactive_thread_id"):
            # The grade folds over the presentation's own thread so the mind
            # reasons within the conversation that produced the result.
            contributor_payload["proactive_thread_id"] = str(rec["proactive_thread_id"])
        contributor = from_operator_message(contributor_payload)
        enqueue_input(contributor, operator_activity=True)
        _attach_journal_answer(rec, clean)
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "system_message_responded",
                summary=clean[:80],
                message_id=str(rec["id"]),
            )
        except Exception:
            pass
        return dict(rec)


def attach_chat_answer(
    message_id: str, response_text: str, *, answer_episode_id: str = ""
) -> Optional[Dict[str, Any]]:
    """Attach an operator reply that arrived through the MAIN CHAT box.

    Same record + open-questions journal transitions as ``respond_to_message``,
    but WITHOUT enqueueing a second contributor — the chat ingress already
    carries the message as its own contributor (with the thread fields riding
    its metadata). Returns the updated record, or None for an unknown id /
    empty text or a question another answer already consumed.
    """
    with _LOCK:
        _ensure_loaded()
        clean = str(response_text or "").strip()
        rec = _find(message_id)
        if rec is None or not clean:
            return None
        if rec.get("status") != STATUS_PENDING:
            return None
        policy = None
        if str(rec.get("kind") or "") == "result_presentation":
            # Chat-box grade of a presented result — same fold trigger as the
            # panel path (operator 2026-07-12).
            _grade_presented_result(rec, operator_text=clean)
        else:
            policy = _bind_policy_for_record(
                rec,
                answer_episode_id=(str(answer_episode_id or "").strip() or f"answer:{rec['id']}"),
                operator_text=clean,
            )
        rec["status"] = STATUS_RESPONDED
        rec["response"] = {"text": clean, "ts": _now_iso(), "via": "operator_chat"}
        if policy is not None:
            rec["proactive_thread_id"] = policy.thread_id
            rec["proactive_policy_id"] = policy.policy_id
        _persist()
        _attach_journal_answer(rec, clean)
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "system_message_responded",
                summary=clean[:80],
                message_id=str(rec["id"]),
                via="operator_chat",
            )
        except Exception:
            pass
        return dict(rec)


def close_request(message_id: str, *, outcome: str) -> Optional[Dict[str, Any]]:
    """Administratively close a pending request without operator input."""
    with _LOCK:
        _ensure_loaded()
        rec = _find(message_id)
        if rec is None or rec.get("status") == STATUS_RESPONDED:
            return dict(rec) if rec else None
        rec["status"] = STATUS_RESPONDED
        rec["response"] = {"text": f"[closed: {outcome}]", "ts": _now_iso()}
        _persist()
        return dict(rec)


def clear_messages() -> None:
    """Empty the store (tests / explicit operator reset)."""
    with _LOCK:
        _ensure_loaded()
        _MESSAGES.clear()
        _persist()


def _reset_cache_for_tests() -> None:
    """Drop the in-memory cache so the next access reloads from disk."""
    global _LOADED, _LOAD_FAILED, _DIRTY
    with _LOCK:
        _MESSAGES.clear()
        _LOADED = False
        _LOAD_FAILED = False
        _DIRTY = False

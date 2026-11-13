"""Operator chat responses — the system's ANSWER to an operator chat message.

The mirror of ``system_messages`` but the OPPOSITE direction. ``system_messages``
is "system asks / operator answers" (pending == waiting on the OPERATOR; feeds
the badge; a reply re-injects into pressure). THIS store is "operator asks /
system answers": pending == waiting on the SYSTEM to finish thinking, the record
is TERMINAL DISPLAY only — it must NEVER feed back into pressure and must NOT
inflate the operator-owed badge.

Why it exists: when the operator sends a chat message while an autonomous episode
is running, ``handle_operator_chat`` cannot ignite synchronously, so it returns
``queued``. The message ignites the NEXT episode (opened_by=operator_query,
intent=RESPOND); AUTO publishes the completed text or artifact. This store carries
that result back to the HUD, keyed by the ORIGINATING operator ``cycle_id`` so it
lands in the right chat bubble. The HUD polls it on the medium clock and resolves
the "thinking" block into the answer.

Durable (``guardian/state/operator_chat_responses.json``, write-through). A
failed read or write propagates to the owning episode; an answer is never
reported durable when only its process-local copy exists. The day-zero factory
wipe clears it with the rest of guardian/state.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from runtime.atomic_store import atomic_write_bytes

log = logging.getLogger("omega.pressure_loop.operator_chat_responses")

STATUS_PENDING = "pending"
STATUS_DELIVERED = "delivered"
_MAX_DELIVERED = 100
_MAX_RESPONSES = 1024
_MAX_LANE_UPDATES = 512
_LANE_UPDATE_STATUSES = frozenset(
    {"queued", "started", "progress", "needs_input", "completed", "failed"}
)
_TERMINAL_LANE_UPDATE_STATUSES = frozenset({"needs_input", "completed", "failed"})

_RESPONSES: List[Dict[str, Any]] = []
_LOADED = False
_LOCK = threading.RLock()
_LANE_UPDATES: List[Dict[str, Any]] = []
_LANE_UPDATE_ACKS: Dict[str, int] = {}
_LANE_UPDATES_LOADED = False
_INTERRUPTS: dict[str, float] = {}
_INTERRUPT_TTL_S = 300.0


def _prune_interrupts_locked(now: float | None = None) -> None:
    cutoff = (time.monotonic() if now is None else now) - _INTERRUPT_TTL_S
    for cycle_id in [cid for cid, requested_at in _INTERRUPTS.items() if requested_at < cutoff]:
        _INTERRUPTS.pop(cycle_id, None)


_WORK_HALT_TTL_S = 900.0
_WORK_HALTS: Dict[str, float] = {}


def _prune_work_halts_locked() -> None:
    cutoff = time.monotonic() - _WORK_HALT_TTL_S
    for cid in [cid for cid, at in _WORK_HALTS.items() if at < cutoff]:
        _WORK_HALTS.pop(cid, None)


def request_work_halt(cycle_id: str) -> bool:
    """Stop the WORK of one turn: its tool loop ends at the next safe boundary.

    Deliberately not request_interrupt: that one cancels playback and delivery and must never
    stop inference (pinned by test_global_playback_interrupt_does_not_cancel_cognition). Esc, the
    stop control and a plain "stop" in chat mean the work, so they raise this instead.
    """
    cid = str(cycle_id or "").strip()
    if not cid:
        return False
    with _LOCK:
        _prune_work_halts_locked()
        _WORK_HALTS[cid] = time.monotonic()
    return True


def work_halt_requested(cycle_id: str) -> bool:
    cid = str(cycle_id or "").strip()
    if not cid:
        return False
    with _LOCK:
        _prune_work_halts_locked()
        return cid in _WORK_HALTS


def clear_work_halt(cycle_id: str) -> None:
    with _LOCK:
        _WORK_HALTS.pop(str(cycle_id or "").strip(), None)


#: The frames a barge-in silences: the ones the operator HEARS. Everything else — the tool call,
#: its result, the browser's page, the redacted think marker — is only SEEN, and a barge-in means
#: "stop talking", never "stop showing me what you are doing". Blinding the surface for the rest of
#: the turn made an interruption look like work that died, which is the no-silent-stall failure this
#: substrate exists to prevent (demo track B3/B5/H4, 2026-09-22).
#:
#: Declared rather than inferred: a new frame kind must be named here to become silenceable, so it
#: can never accidentally keep talking after an interrupt either.
SPEAKABLE_FRAME_KINDS: frozenset[str] = frozenset({"chat.reply_delta", "chat.say"})


def request_interrupt(cycle_id: str) -> bool:
    """Stop the SPEECH of one existing chat turn (voice barge-in / stop).

    Delivery of what is spoken stops; the visible work frames keep flowing, the lane keeps
    running, and the durable answer still arrives — see ``SPEAKABLE_FRAME_KINDS`` and
    ``request_work_halt`` for the verb that stops the work itself.
    """
    cid = str(cycle_id or "").strip()
    if not cid:
        return False
    with _LOCK:
        _prune_interrupts_locked()
        _INTERRUPTS[cid] = time.monotonic()
    return True


def interrupt_requested(cycle_id: str) -> bool:
    cid = str(cycle_id or "").strip()
    if not cid:
        return False
    with _LOCK:
        _prune_interrupts_locked()
        return cid in _INTERRUPTS


def clear_interrupt(cycle_id: str) -> None:
    with _LOCK:
        _INTERRUPTS.pop(str(cycle_id or "").strip(), None)


def _store_path() -> Path:
    from core.paths import GUARDIAN_STATE_ROOT

    return GUARDIAN_STATE_ROOT / "operator_chat_responses.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _persist() -> None:
    path = _store_path()
    atomic_write_bytes(
        path,
        json.dumps(_RESPONSES, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def _lane_updates_path() -> Path:
    from core.paths import GUARDIAN_STATE_ROOT

    return GUARDIAN_STATE_ROOT / "operator_chat_lane_updates.json"


def _persist_lane_updates() -> None:
    atomic_write_bytes(
        _lane_updates_path(),
        json.dumps({
            "version": 1,
            "updates": _LANE_UPDATES,
            "acknowledged": _LANE_UPDATE_ACKS,
        }, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def _ensure_lane_updates_loaded() -> None:
    global _LANE_UPDATES_LOADED
    if _LANE_UPDATES_LOADED:
        return
    try:
        raw = json.loads(_lane_updates_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {"version": 1, "updates": [], "acknowledged": {}}
    if isinstance(raw, list):
        # Preserve updates written before delivery cursors were introduced.
        raw = {"version": 1, "updates": raw, "acknowledged": {}}
    if (not isinstance(raw, dict) or set(raw) != {"version", "updates", "acknowledged"}
            or raw.get("version") != 1 or not isinstance(raw.get("updates"), list)
            or any(not isinstance(row, dict) for row in raw["updates"])):
        raise ValueError("operator chat lane update store has an invalid envelope")
    if not isinstance(raw["acknowledged"], dict):
        raise ValueError("operator chat lane update acknowledgements must be an object")
    seen: set[tuple[str, str]] = set()
    sequences: Dict[str, int] = {}
    for index, row in enumerate(raw["updates"]):
        cycle_id = row.get("cycle_id")
        update_id = row.get("update_id")
        if not isinstance(cycle_id, str) or not cycle_id.strip():
            raise ValueError(f"operator chat lane update {index} has an invalid cycle_id")
        if not isinstance(update_id, str) or not update_id.strip():
            raise ValueError(f"operator chat lane update {index} has an invalid update_id")
        identity = (cycle_id, update_id)
        if identity in seen:
            raise ValueError(f"operator chat lane update {index} repeats its identity")
        seen.add(identity)
        seq = row.get("seq")
        if type(seq) is not int or seq != sequences.get(cycle_id, 0) + 1:
            raise ValueError(f"operator chat lane update {index} has a non-contiguous sequence")
        sequences[cycle_id] = seq
    acknowledged = raw["acknowledged"]
    if any(
        not isinstance(cycle_id, str) or not cycle_id.strip() or type(seq) is not int
        or seq < 0 or seq > sequences.get(cycle_id, 0)
        for cycle_id, seq in acknowledged.items()
    ):
        raise ValueError("operator chat lane update acknowledgement is outside its sequence")
    _LANE_UPDATES[:] = raw["updates"]
    _LANE_UPDATE_ACKS.clear()
    _LANE_UPDATE_ACKS.update(acknowledged)
    _LANE_UPDATES_LOADED = True


def _updates_for_cycle_locked(cycle_id: str) -> List[Dict[str, Any]]:
    _ensure_lane_updates_loaded()
    return [row for row in _LANE_UPDATES if str(row.get("cycle_id") or "") == cycle_id]


def _lane_work_snapshot(receipt: Dict[str, Any], updates: List[Dict[str, Any]]) -> Dict[str, Any]:
    snapshot = dict(receipt)
    latest = max(updates, key=lambda row: int(row.get("seq") or 0), default=None)
    snapshot["status"] = str(latest.get("status") if latest else "queued")
    snapshot["last_seq"] = int(latest.get("seq") or 0) if latest else 0
    return snapshot


def _response_snapshot_locked(rec: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = dict(rec)
    receipt = rec.get("lane_work")
    if isinstance(receipt, dict):
        updates = _updates_for_cycle_locked(str(rec.get("cycle_id") or ""))
        snapshot["lane_work"] = _lane_work_snapshot(receipt, updates)
    return snapshot


def _replace_responses(records: List[Dict[str, Any]]) -> None:
    """Commit one state change under the store lock, restoring cache on failure."""
    previous = list(_RESPONSES)
    _RESPONSES[:] = records
    try:
        _persist()
    except BaseException:
        _RESPONSES[:] = previous
        raise


def _ensure_loaded() -> None:
    global _LOADED
    if _LOADED:
        return
    try:
        raw = json.loads(_store_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = []
    if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
        raise ValueError("operator chat response store must be a list of response objects")
    cycles: set[str] = set()
    for index, row in enumerate(raw):
        cycle_id = row.get("cycle_id")
        if not isinstance(cycle_id, str) or not cycle_id or cycle_id != cycle_id.strip():
            raise ValueError(f"operator chat response {index} has an invalid cycle_id")
        if row.get("status") not in (STATUS_PENDING, STATUS_DELIVERED):
            raise ValueError(f"operator chat response {index} has an invalid status")
        if cycle_id in cycles:
            raise ValueError(f"operator chat response {index} repeats a cycle_id")
        cycles.add(cycle_id)
    _RESPONSES[:] = raw
    _LOADED = True


def _find(cycle_id: str) -> Optional[Dict[str, Any]]:
    for rec in _RESPONSES:
        if str(rec.get("cycle_id")) == str(cycle_id):
            return rec
    return None


_OPERATOR_CHAT_CONTRIBUTORS = (
    "operator_query",
    "operator_command",
    "operator_response",
    "plugin_permission_decision",
)


def waiting_chat_turn(pressure: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The operator turn this episode owes an answer to, or None.

    Returns ``{"cycle_id", "chat_bubble_waiting"}``.

    Extracted (RISK 5) because two rails now need the SAME answer and must not
    be able to derive it differently: ``cognition_chain._deliver_operator_chat_reply``
    decides which bubble the FINISHED reply lands in, and the response stage
    decides which bubble to STREAM tokens into. A stream that picked a different
    cycle than the reply would paint an answer into one bubble and finish it in
    another — the worst possible way to be wrong, because both halves look
    correct on their own.

    The rules, unchanged from where they were inlined:

    * an episode with no operator contributor owes nobody an answer — None;
    * ``operator_reply_delivered`` means the fast-reply rail already answered
      inline, so there is no bubble left to fill and no tokens to stream — None;
    * the FIRST contributor carrying a cycle_id owns the turn;
    * an operator episode whose contributors carry NO cycle_id is still an
      operator episode. It reports ``cycle_id=""`` rather than None, because a
      RESOLVE_QUERY answer with no chat bubble behind it still has somewhere to
      go (the proactive pinboard) — collapsing that case into None would
      silently retire that delivery path.
    """
    contributors = list((pressure or {}).get("contributors") or [])
    for row in contributors:
        # An Omega successor continuing agent or browser work carries the operator's
        # still-waiting bubble, so its tool frames and final answer land there.
        # A Browser or Coding Agent episode opened by the handoff carries the same
        # original cycle, so its live tokens and screenshots use that bubble too.
        if isinstance(row, dict) and row.get("id") == "omega_successor":
            detail = dict(row.get("detail") or {})
            if detail.get("chat_bubble_waiting") and detail.get("original_cycle_id"):
                return {"cycle_id": str(detail["original_cycle_id"]), "chat_bubble_waiting": True}
        if isinstance(row, dict):
            detail = dict(row.get("detail") or {})
            if detail.get("lane_work_handoff") and detail.get("original_cycle_id"):
                return {"cycle_id": str(detail["original_cycle_id"]), "chat_bubble_waiting": True}
    if not any(
        isinstance(row, dict)
        and str(row.get("id") or "") in _OPERATOR_CHAT_CONTRIBUTORS
        for row in contributors
    ):
        return None
    for row in contributors:
        if not isinstance(row, dict) or str(row.get("id") or "") not in _OPERATOR_CHAT_CONTRIBUTORS:
            continue
        detail = dict(row.get("detail") or {})
        if row.get("id") == "plugin_permission_decision":
            # The queue envelope's task_id is the originating chat cycle. The
            # synthetic plugin-permission:<request_id> cycle is for queue
            # identity only and must not strand the original HUD card.
            chat_cycle_id = str(detail.get("task_id") or "").strip()
            if chat_cycle_id:
                return {"cycle_id": chat_cycle_id, "chat_bubble_waiting": True}
        if not detail.get("cycle_id"):
            continue
        if detail.get("operator_reply_delivered"):
            return None
        return {
            "cycle_id": str(detail["cycle_id"]),
            "chat_bubble_waiting": bool(detail.get("chat_bubble_waiting")),
        }
    return {"cycle_id": "", "chat_bubble_waiting": False}


def reply_token_sink(
    cycle_id: str,
    episode_id: str,
    *,
    pipeline_id: int | None = None,
    work_kind: str = "",
    job_id: str | None = None,
) -> Any:
    """A callable that pushes response tokens to the waiting chat bubble.

    Handed to ``RuntimeClient.generate_completion_stream`` as ``on_token``. It
    owns nothing but a counter: the durable record still arrives whole through
    ``append_response``, and this only decides when the operator hears it.

    ``restart()`` resets the sequence for a SECOND generation attempt (a think-on
    reply that produced only thinking is retried think-off), so a renderer that
    treats ``seq == 0`` as "replace what you have" cannot end up with the two
    attempts concatenated.

    Never raises — see hud.chat_socket. Delivery is not a dependency of thought.
    """

    state = {"seq": 0, "attempt": 0, "events": 0}

    def _sink(token: str) -> None:
        if state["seq"] == 0 and state["attempt"] == 0:
            # The operator hears the answer from here; this is the number the demo bar names.
            try:
                from pressure_loop.turn_latency import mark

                mark(cycle_id, "first_token", pipeline_id=pipeline_id,
                     work_kind=work_kind or None, episode_id=episode_id or None)
            except Exception:
                log.debug("turn latency first_token mark not recorded", exc_info=True)
        # The answer's own prose is spoken sentence-by-sentence as it streams, so it is the
        # first thing a barge-in has to stop.
        if "chat.reply_delta" in SPEAKABLE_FRAME_KINDS and interrupt_requested(cycle_id):
            return
        try:
            from hud.chat_socket import publish_delta

            publish_delta(
                cycle_id=cycle_id,
                episode_id=episode_id,
                text=str(token),
                seq=state["seq"],
                attempt=state["attempt"],
                pipeline_id=pipeline_id,
                work_kind=work_kind,
                job_id=job_id,
            )
        except Exception:
            log.debug("reply token delta not delivered (non-fatal)", exc_info=True)
        finally:
            state["seq"] += 1

    def _restart() -> None:
        state["seq"] = 0
        state["attempt"] += 1

    def _event(kind: str, **fields: Any) -> None:
        """One live frame of the work behind the reply (a tool call, its result,
        a piece of the think trace). Same contract as the token sink: never
        raises, never stored — and after a barge-in, silent only if it SPEAKS
        (``SPEAKABLE_FRAME_KINDS``). The work stays visible while it runs."""
        if kind in SPEAKABLE_FRAME_KINDS and interrupt_requested(cycle_id):
            return
        try:
            from hud.chat_socket import publish_event

            publish_event(
                kind=kind,
                cycle_id=cycle_id,
                episode_id=episode_id,
                seq=state["events"],
                attempt=state["attempt"],
                pipeline_id=pipeline_id,
                work_kind=work_kind,
                job_id=fields.pop("job_id", job_id),
                **fields,
            )
        except Exception:
            log.debug("reply %s frame not delivered (non-fatal)", kind, exc_info=True)
        finally:
            state["events"] += 1

    def _reasoning(text: str) -> None:
        body = str(text or "")
        if body:
            _event("chat.reasoning_delta", text=body)

    def _bind_job(value: str) -> None:
        nonlocal job_id
        job_id = value

    _sink.restart = _restart  # type: ignore[attr-defined]
    _sink.event = _event  # type: ignore[attr-defined]
    _sink.reasoning = _reasoning  # type: ignore[attr-defined]
    _sink.bind_job = _bind_job  # type: ignore[attr-defined]
    return _sink


def remember_assistant_turn(episode_id: str, text: str) -> None:
    """Record a new reply in turn order; the owning delivery seam captures errors."""
    body = str(text or "").strip()
    if not body:
        return
    from memory.episode_sram import hold_turn_for_episode

    if hold_turn_for_episode(episode_id, "assistant", body):
        # the exchange is complete and its recall is done: graduate this
        # episode's turns to the hot buffer now, not at DeepSleep
        from memory.episode_sram import graduate_turns_now

        graduate_turns_now(episode_id)
        return
    from memory.hot_buffer import append_turn

    append_turn("assistant", body)


def _plugin_permission_hud_dict(value: Any) -> Optional[Dict[str, Any]]:
    """Persist only ``PluginPermissionRequest.to_hud_dict()``. Extra fields die here."""
    if value is None:
        return None
    from hud.chat_socket import plugin_permission_hud_payload

    return plugin_permission_hud_payload({"plugin_permission_request": value})


def append_response(
    *,
    cycle_id: str,
    episode_id: str,
    text: str,
    delivery: Optional[Dict[str, Any]] = None,
    route: Optional[Dict[str, Any]] = None,
    prompt_pack: Optional[Dict[str, Any]] = None,
    thinking: Optional[Dict[str, Any]] = None,
    image: Optional[Dict[str, Any]] = None,
    document: Optional[Dict[str, Any]] = None,
    workspace_assignment: Optional[Dict[str, Any]] = None,
    plugin_permission_request: Optional[Dict[str, Any]] = None,
    operator_question: Optional[Dict[str, Any]] = None,
    lane_work: Optional[Dict[str, Any]] = None,
    pipeline_id: int | None = None,
    work_kind: str = "",
    job_id: str | None = None,
) -> Optional[Dict[str, Any]]:
    """Record the system's answer to an operator chat message (status ``pending``).

    Keyed by the ORIGINATING operator ``cycle_id`` (NOT the answering episode_id)
    so the HUD can match it to the bubble it is waiting on. A blank cycle_id is
    a no-op; payload may be text, an image artifact, a document artifact, or a
    content-minimized plugin permission card."""
    cid = str(cycle_id or "").strip()
    body = str(text or "").strip()
    interrupted = bool((delivery or {}).get("interrupted"))
    image_payload = dict(image) if isinstance(image, dict) and image else None
    document_payload = dict(document) if isinstance(document, dict) and document else None
    assignment_payload = (
        dict(workspace_assignment)
        if isinstance(workspace_assignment, dict) and workspace_assignment
        else None
    )
    permission_payload = _plugin_permission_hud_dict(plugin_permission_request)
    lane_work_payload = None
    if lane_work is not None:
        if not isinstance(lane_work, dict):
            raise ValueError("lane_work must be an object")
        lane = str(lane_work.get("lane") or "")
        handoff_id = str(lane_work.get("handoff_id") or "")
        original_cycle_id = str(lane_work.get("original_cycle_id") or "")
        thread_id = str(lane_work.get("thread_id") or "")
        if lane not in {"browser", "agent"}:
            raise ValueError("lane_work lane must be browser or agent")
        if not handoff_id or original_cycle_id != cid or not thread_id:
            raise ValueError("lane_work receipt does not match the operator cycle")
        lane_work_payload = {
            "handoff_id": handoff_id,
            "lane": lane,
            "original_cycle_id": original_cycle_id,
            "thread_id": thread_id,
            "status": "queued",
            "updates": [],
        }
    if not cid or not (body or image_payload or document_payload or permission_payload):
        return None
    try:
        # The turn is answered; stamp it before taking the store lock so contention on the
        # store is not measured as thinking time.
        from pressure_loop.turn_latency import mark

        mark(cid, "reply", pipeline_id=pipeline_id, work_kind=work_kind or None,
             episode_id=str(episode_id or "") or None)
    except Exception:
        log.debug("turn latency reply mark not recorded", exc_info=True)
    with _LOCK:
        _ensure_loaded()
        existing = _find(cid)
        if existing is not None:
            # Idempotent: the same operator turn answered once. Keep the first.
            clear_interrupt(cid)
            return dict(existing)
        retained = _compact_delivered(_RESPONSES)
        if len(retained) >= _MAX_RESPONSES:
            raise RuntimeError("operator chat response capacity exhausted")
        record: Dict[str, Any] = {
            "cycle_id": cid,
            "episode_id": str(episode_id or ""),
            "ts": _now_iso(),
            "text": body,
            "status": STATUS_PENDING,
        }
        if pipeline_id is not None:
            from core.pipelines import validate_pipeline_id

            record["pipeline_id"] = validate_pipeline_id(pipeline_id)
        if work_kind:
            record["work_kind"] = str(work_kind)
        if job_id:
            record["job_id"] = str(job_id)
        if lane_work_payload is not None:
            record["lane_work"] = _lane_work_snapshot(
                lane_work_payload, _updates_for_cycle_locked(cid)
            )
        if image_payload is not None:
            record["image"] = image_payload
        if document_payload is not None:
            record["document"] = document_payload
        if assignment_payload is not None:
            record["workspace_assignment"] = assignment_payload
        if permission_payload is not None:
            record["plugin_permission_request"] = permission_payload
        if isinstance(operator_question, dict) and operator_question.get("question"):
            # The agent's own question and its numbered choices: the operator clicks one or
            # answers in their own words, and either way the task resumes (operator 2026-09-15).
            record["operator_question"] = {
                "question": str(operator_question.get("question") or "")[:2000],
                "options": [str(option)[:300] for option in
                            (operator_question.get("options") or ())][:6],
                "job_id": str(operator_question.get("job_id") or ""),
            }
        delivery_payload = dict(delivery) if isinstance(delivery, dict) else {}
        if interrupted:
            delivery_payload["interrupted"] = True
        if delivery_payload:
            record["delivery"] = delivery_payload
        if isinstance(route, dict) and route:
            record["route"] = dict(route)
        if isinstance(prompt_pack, dict) and prompt_pack:
            record["prompt_pack"] = dict(prompt_pack)
        if isinstance(thinking, dict) and thinking:
            record["thinking"] = dict(thinking)
        _replace_responses([*retained, record])
        if not interrupted:
            remember_assistant_turn(record["episode_id"], body)
        # Push the finished reply to its correlated chat transport. The
        # durable record above stays the source of truth — a client that was
        # disconnected still fetches it — so this is delivery, never storage.
        # Fails soft by contract: a dead socket must not stop the mind from
        # recording its own answer.
        try:
            from hud.chat_socket import publish_reply

            # Callback handoff is not delivery: a subscriber may filter this
            # cycle out, or its socket may disconnect before sending. The
            # transport acknowledges only after the exact reply is sent.
            publish_reply(dict(record))
        except Exception:
            log.debug("chat socket publish failed", exc_info=True)
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "pressure",
                "operator_chat_response_ready",
                episode_id=record["episode_id"] or None,
                summary=(body or "artifact ready")[:80],
                cycle_id=cid,
            )
        except Exception:
            pass
        clear_interrupt(cid)
        return dict(record)


def get_response(cycle_id: str) -> Optional[Dict[str, Any]]:
    """The answer for an operator turn, or None if it is not ready yet."""
    with _LOCK:
        _ensure_loaded()
        rec = _find(str(cycle_id or ""))
        return _response_snapshot_locked(rec) if rec is not None else None


def append_lane_update(
    *,
    cycle_id: str,
    episode_id: str,
    lane: str,
    handoff_id: str,
    status: str,
    text: str,
    update_id: str,
    question: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Durably append a correlated update for an admitted Browser or Coding Agent run.

    Updates have their own durable file because a very fast target lane can finish before
    Chat persists its acknowledgement. Replays of the same update identity return the
    original row, so websocket retries never duplicate transcript lines and a reconnect can
    resume from its sequence.
    """
    cid = str(cycle_id or "").strip()
    eid = str(episode_id or "").strip()
    lane_name = str(lane or "")
    admitted_handoff_id = str(handoff_id or "").strip()
    state = str(status or "")
    body = str(text or "").strip()
    identity = str(update_id or "").strip()
    if not cid or not eid or not identity or len(identity) > 256 or not admitted_handoff_id:
        raise ValueError("lane update requires bounded cycle, episode, handoff and update identities")
    if lane_name not in {"browser", "agent"}:
        raise ValueError("lane update lane must be browser or agent")
    if state not in _LANE_UPDATE_STATUSES:
        raise ValueError(f"unknown lane update status {state!r}")
    if not body:
        raise ValueError("lane update text is required")
    question_payload = None
    if question is not None:
        if not isinstance(question, dict) or not str(question.get("question") or "").strip():
            raise ValueError("lane update question must contain question text")
        question_payload = {
            "question": str(question["question"])[:2000],
            "options": [str(option)[:300] for option in (question.get("options") or ())][:6],
        }

    with _LOCK:
        _ensure_lane_updates_loaded()
        updates = _updates_for_cycle_locked(cid)
        if any(
            row.get("lane") != lane_name or row.get("handoff_id") != admitted_handoff_id
            for row in updates
        ):
            raise ValueError("lane update conflicts with the admitted handoff identity")
        for prior in updates:
            if isinstance(prior, dict) and prior.get("update_id") == identity:
                candidate = {
                    "update_id": identity,
                    "cycle_id": cid,
                    "episode_id": eid,
                    "lane": lane_name,
                    "handoff_id": admitted_handoff_id,
                    "status": state,
                    "text": body[:4000],
                    "question": question_payload,
                }
                if any(prior.get(key) != value for key, value in candidate.items()):
                    raise ValueError("lane update identity was reused with different content")
                response = _find(cid)
                if response is not None and isinstance(response.get("lane_work"), dict):
                    changed = [
                        {
                            **row,
                            "lane_work": _lane_work_snapshot(
                                row["lane_work"], updates
                            ),
                        }
                        if row is response else row
                        for row in _RESPONSES
                    ]
                    _replace_responses(changed)
                return dict(prior)
        if len(updates) >= _MAX_LANE_UPDATES:
            raise RuntimeError("operator lane update capacity exhausted")
        update = {
            "update_id": identity,
            "cycle_id": cid,
            "seq": len(updates) + 1,
            "episode_id": eid,
            "lane": lane_name,
            "handoff_id": admitted_handoff_id,
            "status": state,
            "text": body[:4000],
            "question": question_payload,
            "ts": _now_iso(),
        }
        if len(_LANE_UPDATES) >= _MAX_LANE_UPDATES * _MAX_RESPONSES:
            raise RuntimeError("operator chat lane update store capacity exhausted")
        _LANE_UPDATES.append(update)
        try:
            _persist_lane_updates()
        except BaseException:
            _LANE_UPDATES.pop()
            raise
        response = _find(cid)
        if response is not None and isinstance(response.get("lane_work"), dict):
            changed = [
                {
                    **row,
                    "lane_work": _lane_work_snapshot(row["lane_work"], [*updates, update]),
                }
                if row is response else row
                for row in _RESPONSES
            ]
            _replace_responses(changed)

    if response is not None:
        try:
            from hud.chat_socket import publish_lane_update

            publish_lane_update(update)
        except Exception:
            log.debug("chat lane update publish failed", exc_info=True)
    return dict(update)


def get_lane_updates(cycle_id: str, after_seq: int = 0) -> Dict[str, Any] | None:
    """Return durable updates after ``after_seq`` and the lane's current state."""
    cid = str(cycle_id or "").strip()
    if not cid or isinstance(after_seq, bool) or not isinstance(after_seq, int) or after_seq < 0:
        raise ValueError("lane update cursor must be a non-negative integer")
    with _LOCK:
        _ensure_loaded()
        rec = _find(cid)
        _ensure_lane_updates_loaded()
        updates_for_cycle = _updates_for_cycle_locked(cid)
        receipt = rec.get("lane_work") if rec is not None else None
        if not isinstance(receipt, dict) and not updates_for_cycle:
            return None
        updates = [
            dict(update)
            for update in updates_for_cycle
            if isinstance(update, dict) and int(update.get("seq") or 0) > after_seq
        ]
        latest = max(updates_for_cycle, key=lambda row: int(row.get("seq") or 0), default=None)
        lane = str((receipt or {}).get("lane") or (latest or {}).get("lane") or "")
        status = str((latest or {}).get("status") or (receipt or {}).get("status") or "queued")
        return {
            "cycle_id": cid,
            "lane": lane,
            "status": status,
            "done": status in _TERMINAL_LANE_UPDATE_STATUSES,
            "last_seq": int(latest.get("seq") or 0) if latest else 0,
            "acknowledged_seq": _LANE_UPDATE_ACKS.get(cid, 0),
            "updates": updates,
        }


def acknowledge_lane_updates(cycle_id: str, *, through_seq: int) -> int:
    """Persist the highest lane update the HUD rendered into the shared chat feed."""
    cid = str(cycle_id or "").strip()
    if not cid or type(through_seq) is not int or through_seq < 0:
        raise ValueError("lane update acknowledgement requires a cycle and non-negative sequence")
    with _LOCK:
        _ensure_lane_updates_loaded()
        rows = _updates_for_cycle_locked(cid)
        last_seq = max((int(row.get("seq") or 0) for row in rows), default=0)
        if through_seq > last_seq:
            raise ValueError("lane update acknowledgement is beyond the last update")
        previous = _LANE_UPDATE_ACKS.get(cid, 0)
        acknowledged = max(previous, through_seq)
        if acknowledged == previous:
            return acknowledged
        _LANE_UPDATE_ACKS[cid] = acknowledged
        try:
            _persist_lane_updates()
        except BaseException:
            if previous:
                _LANE_UPDATE_ACKS[cid] = previous
            else:
                _LANE_UPDATE_ACKS.pop(cid, None)
            raise
        return acknowledged


def list_active_lane_work() -> List[Dict[str, Any]]:
    """List live lane runs and completed runs with updates not yet rendered by Chat."""
    with _LOCK:
        _ensure_loaded()
        _ensure_lane_updates_loaded()
        cycles = {
            str(rec.get("cycle_id") or ""): (rec.get("lane_work"), rec.get("status"))
            for rec in _RESPONSES
            if isinstance(rec.get("lane_work"), dict)
        }
        for row in _LANE_UPDATES:
            cid = str(row.get("cycle_id") or "")
            if cid and cid not in cycles:
                cycles[cid] = (row, None)
        active = []
        for cycle_id, (state, response_status) in cycles.items():
            cycle_updates = _updates_for_cycle_locked(cycle_id)
            latest = max(cycle_updates, key=lambda row: int(row.get("seq") or 0), default=None)
            status = str((latest or {}).get("status") or (state or {}).get("status") or "queued")
            last_seq = int(latest.get("seq") or 0) if latest else 0
            acknowledged_seq = _LANE_UPDATE_ACKS.get(cycle_id, 0)
            acknowledged = response_status == STATUS_DELIVERED
            if (status not in _TERMINAL_LANE_UPDATE_STATUSES
                    or last_seq > acknowledged_seq or response_status == STATUS_PENDING):
                active.append({
                    "cycle_id": cycle_id,
                    "lane": str((latest or {}).get("lane") or (state or {}).get("lane") or ""),
                    "status": status,
                    "last_seq": last_seq,
                    "acknowledged_seq": acknowledged_seq,
                    "acknowledged": acknowledged,
                })
        return active


def list_undelivered(min_age_s: float = 10.0) -> List[Dict[str, Any]]:
    """Pending answers older than ``min_age_s`` — the HUD's orphan-sweep rail.

    A queued reply's delivery normally rides the per-cycle bubble poll, but that
    registration is in-memory in the browser: a HUD reload, a hidden tab, or a
    send path with no thinking block orphans the row forever (live miss
    2026-07-19 — a proactive-query answer generated ok and never painted). The
    sweep claims anything the bubble rail failed to. The age floor keeps the
    bubble poll winning the race for live turns."""
    now = datetime.now(timezone.utc)
    out: List[Dict[str, Any]] = []
    with _LOCK:
        _ensure_loaded()
        for rec in _RESPONSES:
            if rec.get("status") != STATUS_PENDING:
                continue
            try:
                age = (now - datetime.fromisoformat(str(rec.get("ts") or ""))).total_seconds()
            except ValueError:
                age = min_age_s  # unparsable ts: sweep rather than strand
            if age >= min_age_s:
                out.append(dict(rec))
    return out


def _compact_delivered(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build retained terminal display rows without mutating the committed cache."""
    delivered_idx = [
        i
        for i, rec in enumerate(records)
        if rec.get("status") == STATUS_DELIVERED
        and (
            not isinstance(rec.get("lane_work"), dict)
            or _lane_work_snapshot(
                rec["lane_work"], _updates_for_cycle_locked(str(rec.get("cycle_id") or ""))
            ).get("status") in _TERMINAL_LANE_UPDATE_STATUSES
        )
    ]
    excess = len(delivered_idx) - _MAX_DELIVERED
    if excess <= 0:
        return list(records)
    drop = set(delivered_idx[:excess])
    return [rec for i, rec in enumerate(records) if i not in drop]


def mark_delivered(cycle_id: str) -> Optional[Dict[str, Any]]:
    """Flip a record to ``delivered`` once the HUD has shown it (answers once)."""
    with _LOCK:
        _ensure_loaded()
        rec = _find(str(cycle_id or ""))
        if rec is None:
            return None
        delivered = {**rec, "status": STATUS_DELIVERED}
        changed = [delivered if row is rec else row for row in _RESPONSES]
        _replace_responses(_compact_delivered(changed))
        return dict(delivered)


def list_pending() -> List[Dict[str, Any]]:
    """Answers not yet shown to the operator (for a list-style poll)."""
    with _LOCK:
        _ensure_loaded()
        return [dict(rec) for rec in _RESPONSES if rec.get("status") == STATUS_PENDING]


def clear_responses() -> None:
    """Empty the store (tests / explicit reset)."""
    with _LOCK:
        _ensure_loaded()
        _replace_responses([])
        _ensure_lane_updates_loaded()
        _LANE_UPDATES.clear()
        _LANE_UPDATE_ACKS.clear()
        _persist_lane_updates()


def _reset_cache_for_tests() -> None:
    """Drop the in-memory cache so the next access reloads from disk."""
    global _LOADED, _LANE_UPDATES_LOADED
    with _LOCK:
        _RESPONSES.clear()
        _LANE_UPDATES.clear()
        _LANE_UPDATE_ACKS.clear()
        _LOADED = False
        _LANE_UPDATES_LOADED = False
        _INTERRUPTS.clear()
        _WORK_HALTS.clear()

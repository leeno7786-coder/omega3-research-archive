"""Seal one closing episode into its ``EpisodeRecord`` — pure content assembly (WO-07).

CONTRACTS.md section 3: "Sealing is pure content assembly called from DeepSleep. Canonical
writes remain in the existing memory path." This module owns no writer, no clock and no store.
It composes the assemblers WO-06 built (``pressure_loop.episode_journal``: the five outcome
axes, the stage trail and its coverage, Omega's validated control and proposals) with what the
carrier and its captured events already hold, and returns the frozen record. The terminus then
carries it through the one memory lane (episode SRAM -> DeepSleep drain -> canonical filesystem).

Three rules shape it:

* **It runs after the finish-line stamp.** ``temporal.finish_line`` is the stamp the commit
  region's first statement took, so a carrier without one is refused: an episode that has no
  place in the memory sequence cannot be given a record claiming one.
* **It never predicts its own future.** Nothing here names the canonical id the record will
  receive, the content hash it will carry, or a commit status. Those are external receipts,
  joined later by :mod:`memory.episode_commit_view` — a record cannot hash its own receipt.
* **Absence is stated, never filled.** A host identity the admission seam did not bind, a
  fold that failed, a pin that was never taken: each is an observation with its cause.

:func:`episode_record_id` is the one deterministic canonical identity for an episode's record.
It is derived from the episode id alone, so a retry, a recovery and a reader all name the same
record without carrying state — and the drain's ``verify_canonical_binding`` can recognise a
record written before a lost acknowledgement instead of writing a second one.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

from schema.continuity_json import (
    CanonicalRef,
    ContinuityContractError,
    Observation,
    require_text,
)
from schema.episode_event import EpisodeEvent
from schema.episode_record import EpisodeRecord

#: Canonical records of sealed episodes are named ``episode.<episode id>``.
EPISODE_RECORD_ID_PREFIX = "episode."

_UNSAFE = re.compile(r"[^\w]")

_NO_REQUEST = "no request was recorded for this episode"
_NO_CRITERIA_REVISION = "none: the objective recorded no acceptance contract"


def episode_record_id(episode_id: str) -> str:
    """The deterministic canonical record id for *episode_id*'s sealed record.

    Ids stay inside ``[\\w.]`` so the walker's by-id lookup accepts them. A substituted id also
    carries a digest of the original, so two episode ids can never collapse onto one record.
    """
    text = require_text(episode_id, where="episode_record_id.episode_id", limit=256)
    safe = _UNSAFE.sub("_", text)
    if safe != text:
        safe = f"{safe}.{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}"
    return f"{EPISODE_RECORD_ID_PREFIX}{safe}"


def _missing(reason: str) -> Observation:
    return Observation.missing("not_captured", reason=reason)


def _observed_text(value: Any, reason: str) -> Observation:
    text = str(value or "").strip()
    return Observation.observed(text) if text else _missing(reason)


def _admission_event(events: tuple[EpisodeEvent, ...]) -> EpisodeEvent | None:
    return next((event for event in events if event.phase == "admission"), None)


def _identity(episode: Any, events: tuple[EpisodeEvent, ...]) -> dict[str, Any]:
    episode_id = str(getattr(episode, "episode_id", "") or "")
    thread_id = str(getattr(episode, "thread_id", "") or "")
    if not thread_id:
        raise ContinuityContractError(
            f"episode {episode_id!r} carries no thread id; the host roots every episode in a "
            "thread at open, and a record cannot invent one"
        )
    global_ = getattr(episode, "global_", None)
    decision = global_.get("intent_decision") if isinstance(global_, Mapping) else None
    work_kind = str(getattr(episode, "work_kind", "") or "")
    lane = str((decision or {}).get("omega_lane") or "") if isinstance(decision, Mapping) else ""
    admission = _admission_event(events)
    why = ("the admission seam recorded no such identity for this episode"
           if admission is not None else "no admission event was captured for this episode")
    identity: dict[str, Any] = {
        "episode_id": episode_id,
        "thread_id": thread_id,
        "pipeline_id": int(getattr(episode, "pipeline_id", -1)),
        "lane": lane or work_kind,
        "work_kind": work_kind,
    }
    for name in ("turn_id", "continuation_id", "job_id", "attempt_id"):
        identity[name] = _observed_text(getattr(admission, name, "") if admission else "", why)
    return identity


def _temporal(episode: Any, events: tuple[EpisodeEvent, ...]) -> dict[str, Any]:
    from core.timekeeper import last_coverage_clock

    stamp = dict(getattr(episode, "timekeeper", None) or {})
    seq = stamp.get("commit_seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise ContinuityContractError(
            f"episode {getattr(episode, 'episode_id', '')!r} has no finish-line stamp; a record is "
            "sealed only inside the serialized commit region, after the stamp is taken"
        )
    admission = _admission_event(events)
    arrival = int(getattr(admission, "arrival_seq", 0) or 0) if admission else 0
    pin = dict(getattr(episode, "memory_epoch_pin", None) or {})
    generation = pin.get("coord_index_generation")
    surfaces = {
        "graph_snapshot_hash": str(pin.get("graph_snapshot_hash") or ""),
        "coord_index_generation": -1 if generation is None else int(generation),
        "dme_pack_hash": str(pin.get("dme_snapshot_hash") or ""),
    }
    unpinnable = sorted(
        {str(item) for item in pin.get("degraded") or ()}
        | {name for name, value in surfaces.items() if value in ("", -1)}
    )
    closed_at = str(getattr(episode, "closed_at", "") or "")
    return {
        "opened_at": _observed_text(getattr(episode, "opened_at", ""),
                                    "the carrier recorded no open time"),
        "closed_at": _observed_text(closed_at, "the carrier recorded no close time"),
        "queue_arrival_seq": (
            Observation.observed(arrival) if arrival else
            _missing("no queue arrival sequence was bound to this episode at admission")
        ),
        "finish_line": {
            "commit_seq": seq,
            "commit_ms": int(stamp.get("commit_ms") or 0),
            "event_date": str(stamp.get("event_date") or ""),
            "pipeline_id": int(stamp.get("pipeline_id", -1)),
            "work_kind": str(stamp.get("work_kind") or ""),
        },
        # The ONE ordering authority: the clock epoch this stamp was issued under, and the
        # stamp's own number. Never a second counter.
        "coverage_clock": {"clock_epoch_id": last_coverage_clock().clock_epoch_id,
                           "commit_seq": seq},
        "read_epoch_start": max(0, int(getattr(episode, "memory_epoch_start", 0) or 0)),
        "read_pin": {
            **surfaces,
            "unpinnable_surfaces": unpinnable,
            "attribution": "carrier_pin" if pin else "not_captured: no read pin was taken",
        },
        "concurrent_commits": {
            "episode_ids": [str(item) for item in getattr(episode, "causal_parent_episode_ids",
                                                          None) or ()],
            "complete": bool(getattr(episode, "causal_parents_complete", False)),
        },
    }


def _origin(episode: Any, events: tuple[EpisodeEvent, ...]) -> dict[str, Any]:
    from memory.cognition_capture import ADMISSION_ORIGIN, EPISODE_OPENED

    admitted = next((event for event in events if event.event_kind == ADMISSION_ORIGIN), None)
    opened = next((event for event in events if event.event_kind == EPISODE_OPENED), None)
    authority = admitted.payload.get("authority_revision") if admitted is not None else None
    trajectory = opened.payload.get("trajectory_reference") if opened is not None else None
    return {
        "trigger": {"opened_by": str(getattr(episode, "opened_by", "") or "unrecorded")},
        "authority_snapshot_ref": _observed_text(
            authority, "the admission recorded no connection authority revision"
        ),
        "initial_trajectory_ref": (
            Observation.coerce(trajectory, where="admission.trajectory_reference")
            if trajectory is not None
            else _missing("the admission recorded no initial trajectory reference")
        ),
    }


def _original_request(episode: Any, program: Any) -> str:
    pressure = getattr(episode, "pressure", None)
    for row in (pressure or {}).get("contributors") or () if isinstance(pressure, Mapping) else ():
        if not isinstance(row, Mapping):
            continue
        for source in (row.get("metadata"), row.get("detail"), row):
            message = source.get("message") if isinstance(source, Mapping) else None
            if isinstance(message, str) and message.strip():
                return message.strip()
    contract = program.get("outcome_contract") if isinstance(program, Mapping) else None
    objective = contract.get("objective") if isinstance(contract, Mapping) else None
    if isinstance(objective, str) and objective.strip():
        return objective.strip()
    global_ = getattr(episode, "global_", None)
    for key in ("intent_decision", "action_goal"):
        goal = global_.get(key) if isinstance(global_, Mapping) else None
        if isinstance(goal, Mapping) and goal.get("intent"):
            target = str(goal.get("target") or "").strip()
            return f"{goal['intent']} {target}".strip()
    return _NO_REQUEST


def _objective(episode: Any) -> dict[str, Any]:
    from meta_loop.checks.run import criteria_of
    from pressure_loop.episode_journal import _meta_b_evidence

    meta = getattr(episode, "meta", None)
    program = meta.get("motor_program") if isinstance(meta, Mapping) else None
    _execution, checks = _meta_b_evidence(episode)
    if isinstance(checks, Mapping) and checks.get("criteria_revision"):
        criteria = [str(item) for item in checks.get("criteria") or ()]
        revision = str(checks["criteria_revision"])
    else:
        found, revision = criteria_of(program)
        criteria = list(found)
    contract = program.get("outcome_contract") if isinstance(program, Mapping) else None
    preconditions = contract.get("preconditions") if isinstance(contract, Mapping) else None
    return {
        "original_request": _original_request(episode, program),
        "acceptance_criteria": criteria,
        "criteria_revision": revision or _NO_CRITERIA_REVISION,
        "preconditions": [str(item) for item in preconditions or ()],
    }


def _events(events: tuple[EpisodeEvent, ...]) -> dict[str, Any]:
    return {
        "refs": [
            CanonicalRef(record_id=event.event_id, record_version=1,
                         content_sha256=event.content_sha256())
            for event in events
        ],
        "by_value": list(events),
        "integrity": {event.event_id: event.content_sha256() for event in events},
        "coverage": {"retained": len(events), "referenced": len(events),
                     "unavailable_refs": [], "reason": ""},
    }


def _metacognition(episode: Any) -> dict[str, Any]:
    from pressure_loop.episode_journal import metacognition_content

    content = metacognition_content(episode)
    if content.is_observed:
        return dict(content.to_dict()["value"])
    return {"control": content, "model_assessment": {}, "proposals": []}


def _learning(episode_id: str, metacognition: Mapping[str, Any],
              effects: tuple[dict, ...]) -> dict[str, Any]:
    proposals = [
        {"proposal_id": f"{episode_id}:proposal:{index}", "kind": str(row.get("kind") or ""),
         "status": str(row.get("status") or "")}
        for index, row in enumerate(metacognition.get("proposals") or ())
        if isinstance(row, Mapping)
    ]
    identities = []
    receipts = []
    for effect in effects:
        effect_id = require_text(effect.get("effect_id"), where="effects[].effect_id", limit=512)
        identities.append({"effect_id": effect_id, "kind": str(effect.get("kind") or ""),
                           "target": str(effect.get("target") or "")})
        receipt = effect.get("receipt")
        if isinstance(receipt, Mapping) and receipt:
            receipts.append({"effect_id": effect_id, "store": str(effect.get("kind") or ""),
                             "receipt": dict(receipt)})
    return {"proposals": proposals, "effects": identities, "applied_receipts": receipts}


def seal_episode_record(episode, *, events: tuple[EpisodeEvent, ...],
                        effects: tuple[dict, ...]) -> EpisodeRecord:
    """Assemble the sealed record of one closing episode. Pure: reads, never writes.

    *events* are the episode's own captured events (``read_episode_events``), retained by value
    with their integrity digests. *effects* are the terminus's per-episode effects as
    ``{"effect_id", "kind", "target", "receipt"}``; a receipt is included only when its store
    already carries it, because the record lists what was applied, not what was intended.
    """
    from pressure_loop.episode_journal import (
        coverage_content,
        episode_outcome_from_episode,
        phase_trail,
    )

    episode_id = str(getattr(episode, "episode_id", "") or "")
    captured = tuple(events)
    for event in captured:
        if not isinstance(event, EpisodeEvent):
            raise ContinuityContractError("events must be captured EpisodeEvent values")
        if event.episode_id != episode_id:
            raise ContinuityContractError(
                f"event {event.event_id!r} belongs to {event.episode_id!r}, not {episode_id!r}"
            )
    temporal = _temporal(episode, captured)  # refuses an unstamped carrier first
    metacognition = _metacognition(episode)
    return EpisodeRecord(
        identity=_identity(episode, captured),
        temporal=temporal,
        origin=_origin(episode, captured),
        objective=_objective(episode),
        events=_events(captured),
        outcome=episode_outcome_from_episode(episode),
        metacognition=metacognition,
        learning=_learning(episode_id, metacognition, tuple(effects)),
        coverage=coverage_content(phase_trail(episode, events=captured)),
    )


__all__ = ["EPISODE_RECORD_ID_PREFIX", "episode_record_id", "seal_episode_record"]

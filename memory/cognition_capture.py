"""Phase-owner capture: admission, Global A and Meta A record their decisions (WO-04).

**This adds no substrate and no cognition.** Every function here is a thin, deterministic
translation of a decision its owner has ALREADY made into one WO-03 episode event, through
``memory.episode_events.record_event`` and nothing else. There is no model pass, no second
objective engine, no canonical writer, no scheduler and no new queue: WO-03's journal is the
durability, WO-03's SRAM bucket is the working state, and WO-07 still owns publication.

Why the capture lives here rather than inline in each phase: the five call sites share one
vocabulary (event kinds, operation identities, and how an absent signal is declared), and a
vocabulary split across ``cognition_chain``, ``global_loop`` and ``meta_loop`` is exactly how two
call sites come to disagree about what "the same decision" means.

**Absent is declared, never defaulted.** Every optional signal is a
:class:`schema.continuity_json.Observation`, so "the router returned 0.51" and "no router scored
this turn" can never be read as the same thing. A missing confidence is *not* ``0.0``, a missing
pin is *not* an empty snapshot, and a missing objective is *not* an empty string
(AGENTS.md §2.1).

**Capture succeeding is not planning succeeding.** A blocked or failed plan is recorded as an
observation of that outcome; the event's own status says only that the observation was recorded.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from memory.episode_events import capture_context, record_event
from schema.continuity_json import Observation
from schema.episode_event import ADMISSION_ORIGIN

#: The event kinds this order produces. One name per decision, declared once.
EPISODE_OPENED = "admission.episode_opened"
INTENT_SELECTED = "global_a.intent_selected"
INTENT_REVISED = "global_a.intent_revised"
ACTION_PACK = "meta_a.action_pack"
ACCEPTANCE_CRITERIA = "meta_a.acceptance_criteria"

#: A phase that FAILED gets its own kind, deliberately outside the decision lineage. WO-04A
#: recorded a failure as an empty-intent *revision*, which made a producer that selected nothing
#: supersede a real decision and become the decision in force (closure finding F1). A failure is
#: evidence about an attempt, not a decision, so it can never be the decision in force and can
#: never invalidate one.
GLOBAL_A_FAILED = "global_a.phase_failed"
META_A_FAILED = "meta_a.phase_failed"
CONTRACT_FAILED = "meta_a.contract_failed"
META_B_FAILED = "meta_b.phase_failed"
GLOBAL_B_FAILED = "global_b.phase_failed"
OMEGA_FAILED = "omega.phase_failed"
DEEPSLEEP_FAILED = "deepsleep.phase_failed"
DEV_RECALL_FAILED = "admission.dev_recall_failed"

CAPTURE_EVENT_KINDS = (
    ADMISSION_ORIGIN,
    EPISODE_OPENED,
    INTENT_SELECTED,
    INTENT_REVISED,
    ACTION_PACK,
    ACCEPTANCE_CRITERIA,
    GLOBAL_A_FAILED,
    META_A_FAILED,
    CONTRACT_FAILED,
    META_B_FAILED,
    GLOBAL_B_FAILED,
    OMEGA_FAILED,
    DEEPSLEEP_FAILED,
    DEV_RECALL_FAILED,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _episode_id(episode: Any) -> str:
    return _text(getattr(episode, "episode_id", ""))


def _observed(value: Any, *, absent_state: str, reason: str) -> Observation:
    """*value* when it is really there, otherwise a declared absence with its reason.

    ``absent_state`` distinguishes "this signal does not apply to this episode"
    (``not_applicable``) from "it applies but could not be read" (``unavailable``). Collapsing
    the two would make a degraded surface indistinguishable from an inapplicable one.
    """
    if value is None or value == "" or value == {} or value == ():
        return Observation.missing(absent_state, reason=reason)
    return Observation.observed(value)


def _metadata(contributor: Any) -> dict[str, Any]:
    return dict(getattr(contributor, "metadata", None) or {})


def _admitted_identities(episode: Any) -> dict[str, Any]:
    """The identities this EPISODE was admitted with, read back from its own origin event.

    Later phases must carry the same ``turn_id``/``continuation_id``/``job_id``/``arrival_seq``
    the queue admitted, or a Global A or Meta A event cannot be tied to the request that caused
    it. They are read from the episode's OWN durable origin event rather than from a thread-local
    carried across phases: four pipelines run concurrently, and a context that outlived its phase
    would hand one episode's identities to another. Keyed on the episode id, leakage is not
    possible by construction — a wrong episode simply has no origin event to read.

    Returns ``{}`` when this episode has no origin event, which is the honest answer for a phase
    driven outside the admission seam; no identity is guessed from the episode.
    """
    episode_id = _episode_id(episode)
    if not episode_id:
        return {}
    from memory.episode_events import read_episode_events

    for event in read_episode_events(episode_id):
        if event.event_kind != ADMISSION_ORIGIN:
            continue
        if event.episode_id != episode_id:          # defence in depth; the read already keys on it
            continue
        return {
            "thread_id": event.thread_id,
            "turn_id": event.turn_id,
            "continuation_id": event.continuation_id,
            "job_id": event.job_id,
            "arrival_seq": event.arrival_seq,
        }
    return {}


def _phase_context(episode: Any) -> dict[str, Any]:
    """Capture-context fields for a post-admission phase event of THIS episode."""
    identities = _admitted_identities(episode)
    thread_id = identities.get("thread_id") or _text(getattr(episode, "thread_id", "")) \
        or _episode_id(episode)
    return {
        "thread_id": thread_id,
        "turn_id": identities.get("turn_id") or "",
        "continuation_id": identities.get("continuation_id") or "",
        "job_id": identities.get("job_id") or "",
        "arrival_seq": int(identities.get("arrival_seq") or 0),
    }


def _current_intent_event(episode_id: str):
    """This episode's CURRENT intent decision — the latest selection OR revision, or None.

    Following only ``INTENT_SELECTED`` was a lineage bug: after A was revised to B, the latest
    *selection* was still A, so a third pass choosing A again compared equal to it and recorded
    nothing — even though the episode's current intent was B and returning to A is a real change.
    Worse, a third distinct choice C would have superseded A rather than B, describing a lineage
    that never happened. ``read_episode_events`` is ordered by sequence, so the last of the two
    kinds is the decision in force.
    """
    if not episode_id:
        return None
    from memory.episode_events import read_episode_events

    decisions = [
        event for event in read_episode_events(episode_id)
        if event.event_kind in (INTENT_SELECTED, INTENT_REVISED)
    ]
    return decisions[-1] if decisions else None


#: The decision evidence a re-run is compared on. Everything the producer actually decided or
#: returned, INCLUDING its own `reason`.
#:
#: WO-04A excluded `reason` because a revision rewrote that field with generated commentary, so
#: comparing it made every identical retry look like a change. Excluding it was the wrong half of
#: the fix (closure finding F2): the producer's reason carries real state — Global A builds it from
#: the ignition's aggregate pressure — so a changed reason is changed evidence, and discarding it
#: lost a genuine difference. `reason` is now the producer's own text, never rewritten, and any
#: generated commentary lives in `revision_note` where it cannot displace evidence.
_DECISION_EVIDENCE = (
    "planning_status",
    "intent",
    "omega_lane",
    "reason",
    "intent_confidence",
    "route",
    "router_confidence",
    "routing_revision",
)


def _same_decision(event: Any, payload: Mapping[str, Any]) -> bool:
    """Whether a re-run produced the same decision AND the same evidence for it.

    Compared as canonical bytes over the whole evidence set. Judging on intent and lane alone was
    finding C: a second route reaching the same intent with a different confidence compared equal,
    so the stored revision was replayed and the new evidence was silently discarded before the
    event API could ever see it. A changed route is a new decision even when it lands on the same
    intent.
    """
    from schema.continuity_json import canonical_bytes

    recorded = event.payload
    for field in _DECISION_EVIDENCE:
        if canonical_bytes(recorded.get(field)) != canonical_bytes(payload.get(field)):
            return False
    return True


def _intent_operation(event: Any, episode_id: str) -> str:
    """The operation token the stored decision was recorded under."""
    if event.event_kind == INTENT_REVISED:
        return f"global-a-intent-revision:{_text(event.payload.get('supersedes_event_id'))}"
    return f"global-a-intent:{episode_id}"


def _citation_id(metadata: Mapping[str, Any]) -> str:
    citation = metadata.get("source_citation")
    if isinstance(citation, Mapping):
        return _text(citation.get("id") or citation.get("citation_id"))
    return _text(citation)


def _pack_digest(pack: Mapping[str, Any]) -> tuple[str, str]:
    """``(digest, failure_reason)`` for the pack's own content identity.

    Returns ``("", "")`` when there is no pack at all, and ``("", reason)`` when a pack IS present
    but the canonical encoder refuses it. Those are different facts and the caller records them
    as different states; a single empty string for both would report an absent pack for one that
    really existed.
    """
    if not pack:
        return "", ""
    from schema.continuity_json import content_hash

    try:
        return content_hash(pack), ""
    except Exception as exc:  # noqa: BLE001 - a refused pack is a fact, not a crash
        return "", f"the action pack is present but could not be digested: {exc}"


def _epoch_pin_view(pin: Any) -> dict[str, Any] | None:
    """Every identity the real ``EpochPin`` carries, not a summary of it.

    An earlier version recorded only ``epoch``, ``captured_at`` and ``degraded``, silently
    dropping the graph, coordinate-index and DME surface identities the pin exists to name — the
    very things that make a recall reproducible against the snapshot this episode read at.
    Whatever the pin declares is recorded, so the pin gaining a surface does not quietly stop
    being captured.
    """
    if pin is None:
        return None
    view: dict[str, Any] = {}
    for name, value in vars(pin).items() if hasattr(pin, "__dict__") else ():
        if name.startswith("_"):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            view[name] = value
        elif isinstance(value, Mapping):
            view[name] = {str(k): (v if isinstance(v, (str, int, float, bool)) or v is None
                                   else str(v)) for k, v in value.items()}
        elif isinstance(value, (list, tuple, set)):
            view[name] = [str(item) for item in value]
        else:
            view[name] = str(value)
    return view or None


def _routing_revision() -> Observation:
    """The promoted intent-router pin's own identity, or a declared absence.

    Read from the pin rather than from the loaded router object, because the pin is what makes a
    route reproducible. When no router is promoted on this machine there is no revision to
    record, and inventing one would misattribute the decision.
    """
    try:
        from global_loop.intent import router as intent_router

        pin = intent_router.load_pin(intent_router.pin_path())
    except Exception as exc:  # noqa: BLE001 - an unresolvable pin is a fact, not a crash
        return Observation.missing(
            "unavailable", reason=f"the promoted intent-router pin could not be read: {exc}"
        )
    files = pin.get("files") if isinstance(pin, Mapping) else None
    revision = _text(pin.get("run")) if isinstance(pin, Mapping) else ""
    if not revision and isinstance(files, Mapping):
        from schema.continuity_json import content_hash

        revision = content_hash({str(k): str(v) for k, v in files.items()})[:32]
    return _observed(
        revision,
        absent_state="unavailable",
        reason="the promoted intent-router pin names no run identity",
    )


# ── admission ─────────────────────────────────────────────────────────────────


def capture_admission_origin(episode: Any, ignition: Any, contributor: Any):
    """Record WHERE this episode came from, before its request can be acknowledged.

    Called from the admission seam between ``EpisodeStore.open`` and the exact queue
    acknowledgement. The identities are read from the leased contributor and the opened carrier —
    never re-derived — so the event names the same request the queue is about to consume.
    """
    from pressure_loop.cognition_queue import serialize_input

    metadata = _metadata(contributor)
    episode_id = _episode_id(episode)
    # Some contributors have a closed content schema: their queue identity is
    # carried by the transport rather than duplicated inside metadata.
    queue_id = _text(serialize_input(contributor)["queue_id"])
    cycle_id = _text(metadata.get("cycle_id"))
    arrival_seq = metadata.get("arrival_seq")
    try:
        arrival = int(arrival_seq) if arrival_seq is not None else 0
    except (TypeError, ValueError):
        arrival = 0
    payload = {
        "episode_id": episode_id,
        "queue_id": queue_id,
        "cycle_id": cycle_id,
        "arrival_seq": arrival,
        "opened_by": _text(getattr(ignition, "opened_by", "")),
        "work_kind": _text(getattr(ignition, "work_kind", "")),
        "contributor_source": _text(getattr(contributor, "source", "")),
        "contributor_type": _text(getattr(contributor, "type", "")),
        "source_citation_id": _citation_id(metadata),
        "authority_revision": _text(
            metadata.get("connection_authority_revision") or metadata.get("authority_revision")
        ),
        "original_cycle_id": _text(metadata.get("original_cycle_id")),
        "aggregate_pressure": float(getattr(ignition, "aggregate_pressure", 0.0) or 0.0),
    }
    with capture_context(
        thread_id=_text(getattr(episode, "thread_id", "")) or episode_id,
        turn_id=_text(metadata.get("turn_id")),
        continuation_id=_text(metadata.get("successor_id") or metadata.get("continuation_id")),
        job_id=_text(metadata.get("agent_job_id") or metadata.get("job_id")),
        arrival_seq=arrival,
    ):
        return record_event(
            episode_id,
            operation_id=f"admission-origin:{queue_id or cycle_id or episode_id}",
            phase="admission",
            event_kind=ADMISSION_ORIGIN,
            payload=payload,
        )


def capture_episode_opened(episode: Any, *, pin: Any, trajectory: Any):
    """Record the episode's ACTUAL starting state: its opened time, memory pin and trajectory.

    The pin comes from ``memory.epoch.open_episode_epoch``'s real return — ``None`` when epochs
    are off or the carrier has no id — and is declared unavailable in that case rather than being
    replaced with a snapshot taken later. Commits after this instant are not part of this
    starting state.
    """
    episode_id = _episode_id(episode)
    pin_view = _epoch_pin_view(pin)
    payload = {
        "episode_id": episode_id,
        "opened_at": _text(getattr(episode, "opened_at", "")),
        "pipeline_id": int(getattr(episode, "pipeline_id", -1)),
        "work_kind": _text(getattr(episode, "work_kind", "")),
        "thread_id": _text(getattr(episode, "thread_id", "")),
        "opened_by": _text(getattr(episode, "opened_by", "")),
        "memory_epoch": _observed(
            pin_view,
            absent_state="unavailable",
            reason="no memory epoch pin was opened for this episode",
        ),
        "trajectory_reference": _observed(
            dict(trajectory) if isinstance(trajectory, Mapping) else None,
            absent_state="unavailable",
            reason="the ignition carried no trajectory snapshot",
        ),
    }
    with capture_context(**_phase_context(episode)):
        return record_event(
            episode_id,
            operation_id=f"episode-opened:{episode_id}",
            phase="admission",
            event_kind=EPISODE_OPENED,
            payload=payload,
        )


# ── Global A ──────────────────────────────────────────────────────────────────


def _intent_payload(episode_id: str, decision: Any, *, route: Any,
                    failure_reason: str) -> dict[str, Any]:
    """One record of what Global A decided, used for a selection AND for a revision.

    Built once so a correction cannot carry less evidence than the decision it replaced — which
    was finding C. Every optional signal is an Observation, so an absent one is declared.
    """
    route_view: Any = None
    router_confidence: Any = None
    if route is not None:
        # The route arrives as the dict Global A WROTE onto the episode (its own `to_dict`), so
        # what is recorded is the producer's real shape and not a re-derivation of it.
        route_view = dict(route) if isinstance(route, Mapping) else None
        router = dict((route_view or {}).get("router") or {})
        if router.get("confidence") is not None:
            router_confidence = float(router["confidence"])
    unrouted = "this episode was not an operator turn the intent router routes"
    failed = decision is None
    # FINDING B. A decision that does not exist has no confidence, and `float(... or 0.0)` claimed
    # one anyway. Absence is now declared with its reason; a decision that really scored 0.0 is
    # still `observed`, because a legitimate zero is data.
    if failed:
        confidence: Any = Observation.missing(
            "unavailable",
            reason=_text(failure_reason) or "no IntentDecision was produced, so none scored it",
        )
    else:
        confidence = Observation.observed(float(getattr(decision, "confidence", 0.0) or 0.0))
    return {
        "episode_id": episode_id,
        # A FAILED phase is recorded as a failure, not as a selection of nothing. No intent, lane
        # or confidence is claimed, and the reason is the failure itself, so the absence is on
        # record and the caller still raises.
        "planning_status": "failed" if failed else "selected",
        "intent": _text(getattr(decision, "intent", "")),
        "omega_lane": _text(getattr(decision, "omega_lane", "")),
        "reason": _text(failure_reason) or _text(getattr(decision, "reason", "")),
        "intent_confidence": confidence,
        "route": _observed(route_view, absent_state="not_applicable", reason=unrouted),
        "router_confidence": (
            Observation.observed(router_confidence)
            if router_confidence is not None
            else Observation.missing("not_applicable", reason=unrouted)
        ),
        "routing_revision": (
            _routing_revision()
            if route is not None
            else Observation.missing("not_applicable", reason=unrouted)
        ),
    }


def capture_intent_selected(episode: Any, decision: Any, *, route: Any,
                            failure_reason: str = "", invocation_id: str = ""):
    """Record what Global A decided — or that it failed to decide — with its full evidence.

    Three outcomes, and never a fourth:

    * no decision on record yet -> this is the selection;
    * the same decision AND the same evidence -> replay the occurrence in force, adding nothing;
    * anything about the decision or its evidence changed -> a NEW revision that references the
      occurrence it supersedes. The superseded event keeps its sealed bytes.

    A correction is therefore a new linked event, never an overwrite, and never a silent
    substitution of stale evidence for changed input (finding C).
    """
    episode_id = _episode_id(episode)
    if decision is None:
        # A phase that decided NOTHING is recorded as a failure, never as a decision — so it
        # cannot supersede the last valid decision or become the decision in force (WO-04A
        # closure finding F1). This stays the entry point Global A's handoff calls, so there is
        # one place a failed selection is reported from.
        return capture_phase_failure(
            episode, phase="global_a", event_kind=GLOBAL_A_FAILED,
            reason=_text(failure_reason) or "Global A produced no IntentDecision",
            invocation_id=invocation_id,
        )
    payload = _intent_payload(episode_id, decision, route=route, failure_reason=failure_reason)
    current = _current_intent_event(episode_id)

    if current is not None and _same_decision(current, payload):
        # An exact retry replays the occurrence IN FORCE under its own operation token, with its
        # own stored bytes, so WO-03's replay guard sees an unchanged input set and rehydrates it
        # idempotently rather than refusing a conflict.
        from schema.continuity_json import unfreeze

        with capture_context(**_phase_context(episode)):
            return record_event(
                episode_id,
                operation_id=_intent_operation(current, episode_id),
                phase="global_a",
                event_kind=current.event_kind,
                payload=unfreeze(current.payload),
            )

    if current is not None:
        # A CORRECTION. It carries the SAME evidence set as a selection — that is the whole point
        # of finding C — plus the identity of what it replaces.
        revision = dict(payload)
        revision["supersedes_event_id"] = current.event_id
        # `reason` stays the PRODUCER's own text (finding F2). The generated commentary goes in its
        # own field, so explaining a correction can never displace the evidence for it.
        revision["revision_note"] = "Global A reselected this episode's intent"
        with capture_context(**_phase_context(episode)):
            return record_event(
                episode_id,
                operation_id=f"global-a-intent-revision:{current.event_id}",
                phase="global_a",
                event_kind=INTENT_REVISED,
                payload=revision,
            )

    with capture_context(**_phase_context(episode)):
        return record_event(
            episode_id,
            operation_id=f"global-a-intent:{episode_id}",
            phase="global_a",
            event_kind=INTENT_SELECTED,
            payload=payload,
        )


# `capture_intent_revision` was REMOVED here (WO-04A closure finding F3). It was an exported
# writer that produced a revision carrying only labels, linkage and a reason — a weaker serializer
# than the runtime path beside it, able to write an event that would fail the completeness the
# runtime now guarantees. It had no caller: the runtime revision branch builds its payload from
# `_intent_payload`. One validation path, no second way in.


# ── Meta A ────────────────────────────────────────────────────────────────────


def new_invocation_id() -> str:
    """A fresh identity for ONE host invocation of a lifecycle phase.

    Minted by the handoff that is about to call a producer, and reused only by a RETRY OF THE
    RECORDING for that same invocation. It is the host's own bookkeeping — the same kind of thing
    as WO-03's ``boot_id`` — and it is deliberately not derived from anything the producer says.
    """
    import uuid

    return uuid.uuid4().hex


def capture_phase_failure(episode: Any, *, phase: str, event_kind: str, reason: str,
                          invocation_id: str = ""):
    """Record that a lifecycle phase FAILED, as its own occurrence.

    Its own event kind and its own operation token, both deliberately:

    * the kind keeps it out of the decision lineage, so a producer that decided nothing can never
      supersede a real decision or become the decision in force (finding F1, Global A);
    * the token is the HOST INVOCATION this failure came from, so a later failure never collides
      with an earlier SUCCESS's operation — which is what made a second Meta A failure raise
      ``EventIdentityConflict`` and record nothing at all (finding F1, Meta A) — and two real
      executions stay two occurrences, while a retry of the RECORDING for one invocation replays.

    r2 keyed the token on ``content_hash(reason)``, so two genuine executions whose errors read the
    same collapsed into one occurrence (closure finding R3-1). Equal diagnostics are not evidence
    that the producer did not run again; only the invocation identity can say that, and a producer
    does not get to decide its own occurrence identity by choosing its wording.

    An absent ``invocation_id`` mints a fresh one. That is the conservative direction: an
    unidentified attempt becomes its own occurrence rather than being merged into another.

    Nothing about the decision is claimed. Every signal a successful phase would have supplied is
    an explicit absence with this failure as its reason.
    """
    episode_id = _episode_id(episode)
    invocation = _text(invocation_id) or new_invocation_id()
    detail = _text(reason) or f"{phase} failed without a diagnostic"
    absent = Observation.missing("unavailable", reason=detail)
    payload = {
        "episode_id": episode_id,
        "phase": _text(phase),
        "planning_status": "failed",
        "invocation_id": invocation,
        "reason": detail,
        "intent": "",
        "omega_lane": "",
        "intent_confidence": absent,
        "route": absent,
        "router_confidence": absent,
        "routing_revision": absent,
    }
    with capture_context(**_phase_context(episode)):
        return record_event(
            episode_id,
            operation_id=f"{phase}-failure:{episode_id}:{invocation}",
            phase=_text(phase),
            event_kind=_text(event_kind),
            payload=payload,
        )


def capture_action_pack(episode: Any, pack: Mapping[str, Any] | None,
                       *, failure_reason: str = ""):
    """Record the action/tool Meta A selected and the constraints that came with it.

    Meta A stays deterministic: this reads its validated pack and writes it down. A skipped or
    failed plan reaches here as an absent pack and is recorded as that, not as a successful one.
    """
    episode_id = _episode_id(episode)
    data = dict(pack or {})
    inputs = dict(data.get("execution_inputs") or {})
    pressure = dict(inputs.get("emergent_pressure") or {})
    absent = "Meta A produced no action pack for this episode"
    digest, digest_failure = _pack_digest(data)
    payload = {
        "episode_id": episode_id,
        "intent": _text(dict(getattr(episode, "global_", None) or {})
                        .get("intent_decision", {}).get("intent")),
        "action": _text(data.get("action")),
        "tool": _observed(_text(data.get("tool")), absent_state="not_applicable",
                          reason="the selected action uses no tool"),
        "tool_reason": _text(data.get("tool_reason")),
        "execution_intent": _text(data.get("execution_intent")),
        "memory_required": bool(data.get("memory_required")),
        "memory_scope": [str(scope) for scope in (data.get("memory_scope") or ())],
        "motor_transport": _observed(
            _text(inputs.get("motor_transport")),
            absent_state="not_applicable",
            reason="the selected action declares no motor transport",
        ),
        # Both of these live under execution_inputs.emergent_pressure, which is the path
        # `meta_loop.execution_contract` itself reads. An earlier version looked for
        # `proposed_action` one level too shallow and therefore recorded an empty capability list
        # for a pack that really declared some.
        "required_capabilities": [
            str(name) for name in (dict(pressure.get("proposed_action") or {})
                                   .get("required_capabilities") or ())
        ],
        "preconditions": [
            str(condition) for condition in (pressure.get("resolution_conditions") or ())
        ],
        # PROVENANCE, not a synthesized identity. The action pack carries no revision of its own,
        # and an earlier version of this module recorded `f"{action}:{episode_id}"` as an
        # `observed` revision — a value this code invented, wearing the state that means "the
        # producer really returned this". That is exactly the fabrication the Observation contract
        # exists to prevent. What IS real is the pack's own content identity, so that is what is
        # recorded, named for what it is.
        "action_pack_revision": Observation.missing(
            "not_applicable",
            reason="the execution action pack declares no revision of its own",
        ),
        # Three distinct answers, never collapsed into one: a digest, "there was no pack"
        # (not_applicable), and "there WAS a pack but it could not be digested" (unavailable,
        # naming the failure). Reporting the third as the second would deny a pack that existed.
        "action_pack_content_sha256": (
            Observation.observed(digest) if digest
            else Observation.missing("unavailable", reason=digest_failure) if digest_failure
            else Observation.missing("not_applicable", reason=absent)
        ),
        # A RAISED producer failure and "Meta A returned nothing" are different facts, and the
        # diagnostic that caused the first must survive into the record (finding A).
        "planning_status": ("planned" if data else "failed" if failure_reason else "absent"),
        "reason": _text(failure_reason),
    }
    with capture_context(**_phase_context(episode)):
        return record_event(
            episode_id,
            operation_id=f"meta-a-pack:{episode_id}",
            phase="meta_a",
            event_kind=ACTION_PACK,
            payload=payload,
        )


def capture_acceptance_criteria(episode: Any, *, objective: str | None,
                                criteria: Sequence[str] | None, status: str,
                                reason: str = "", program_id: str = "",
                                criteria_revision: Any = None):
    """Record the bar BEFORE AUTO runs — or record honestly that no bar was set.

    The criteria come from the existing owner (``meta_loop.execution_contract``); nothing here
    authors them, so this introduces no second objective engine. Because the event is sealed by
    WO-03's contract, a later attempt to record different criteria for the same episode is an
    ``EventIdentityConflict``: the bar cannot be moved to make a result pass.
    """
    episode_id = _episode_id(episode)
    absent = reason or f"no execution contract was bound for this episode ({status})"
    payload = {
        "episode_id": episode_id,
        "status": _text(status),
        "reason": _text(reason),
        "program_id": _text(program_id),
        "objective": _observed(_text(objective), absent_state="not_applicable", reason=absent),
        "criteria": _observed(
            [str(item) for item in (criteria or ())] or None,
            absent_state="not_applicable",
            reason=absent,
        ),
        "criteria_revision": _observed(
            criteria_revision, absent_state="not_applicable", reason=absent
        ),
    }
    with capture_context(**_phase_context(episode)):
        return record_event(
            episode_id,
            operation_id=f"meta-a-criteria:{episode_id}",
            phase="meta_a",
            event_kind=ACCEPTANCE_CRITERIA,
            payload=payload,
        )


# ── WO-10: the exact input a window reflection admitted ───────────────────────

#: A window episode's admitted input is its sealed cohort. Recorded at the admission seam, when
#: ``window_lane.restore_window`` seals and hydrates it, so the reflection's own sealed record
#: names exactly which revisions it reasoned over — the host citation WO-10's history reads
#: (a reflection is a prior finding about THESE members, and duplicates of it add nothing).
WINDOW_MANIFEST_ADMITTED = "window.manifest_admitted"


def capture_window_manifest(episode: Any, manifest: Any):
    """Record the sealed cohort a window episode admitted, by exact reference.

    References and digests only. Like every capture here it reuses the accepted WO-03 API and
    nothing else: the caller decides whether it owns the episode (``restore_window`` records only
    when this thread owns one), and a thread bound to a DIFFERENT episode is refused loudly by
    ``record_event``.
    """
    from schema.continuity_json import content_hash

    episode_id = _episode_id(episode)
    if not episode_id:
        return None
    payload = {
        "episode_id": episode_id,
        "window_id": _text(manifest.window_id),
        "window_version": int(manifest.window_version),
        "manifest_sha256": content_hash(manifest.to_dict()),
        "snapshot_generation_id": _text(manifest.snapshot.generation_id),
        "complete": bool(manifest.complete),
        "member_ids": list(manifest.member_ids),
        "member_refs": [ref.to_dict() for ref in manifest.member_refs],
    }
    with capture_context(**_phase_context(episode)):
        return record_event(
            episode_id,
            operation_id=f"window-manifest:{episode_id}:{manifest.window_id}",
            phase="admission",
            event_kind=WINDOW_MANIFEST_ADMITTED,
            payload=payload,
        )


__all__ = [
    "WINDOW_MANIFEST_ADMITTED",
    "capture_window_manifest",
    "ACCEPTANCE_CRITERIA",
    "ACTION_PACK",
    "ADMISSION_ORIGIN",
    "CAPTURE_EVENT_KINDS",
    "CONTRACT_FAILED",
    "DEEPSLEEP_FAILED",
    "DEV_RECALL_FAILED",
    "EPISODE_OPENED",
    "GLOBAL_A_FAILED",
    "GLOBAL_B_FAILED",
    "META_A_FAILED",
    "META_B_FAILED",
    "OMEGA_FAILED",
    "INTENT_REVISED",
    "INTENT_SELECTED",
    "capture_acceptance_criteria",
    "capture_action_pack",
    "capture_admission_origin",
    "capture_phase_failure",
    "new_invocation_id",
    "capture_episode_opened",
    "capture_intent_selected",
]

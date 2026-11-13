"""Durable build-continuation context (historical module name: agent_lane).

THE live pain this cut exists to fix (operator, 2026-08-10): a build task's
episode ran motor planning AND the entire build while cognition had only one
physical execution path. The next chat waited minutes behind the build.

The cognition queue has four durable semantic FIFOs. Agent continuation work is
bound to the Coding Agent lane and physical pipeline P2; it cannot lease a Chat,
Browser, or Window worker. This module carries job context and receipts; the
cognition queue owns admission and lane-local serialization.

The split, exactly as ratified:

    chat episode                            agent episode (next tick)
    ────────────                            ─────────────────────────
    Global A → Meta A → motor planning      Global A → Meta A → *carried*
    │                                       program (NO re-planning)
    ├─ (1) enqueue the durable agent-line   → AUTO executes it
    │      contributor  ← DURABLE FIRST     → verification → foldback
    └─ (2) fold "dispatched", keep going    → its OWN deepsleep
       chat reply = the dispatch ack        → completion → the operator

**Ordering is law.** The durable enqueue happens BEFORE the fold, and nothing
between them may be skipped on a crash: if the process dies after the write,
the work is already waiting on the agent line and the chat episode simply
never told the operator it started; if it dies before the write, the whole ask
re-plans from the operator's turn (which is still queued, or already answered)
and re-enqueues. The row's identity is the CAUSAL PARENT episode
(``core.lanes.CAUSAL_PARENT_KEY``), so that re-enqueue merges with the row
already waiting instead of building the same thing twice.

**USE_TOOL stays inline.** A bounded tool call is the thing the answer is made
of — handing it to another lane would mean answering the operator with a
promise instead of an answer. Only USE_SKILL — the qwen-jail build, the labor
that measured in MINUTES — moves. Cascade order inside either episode is
untouched; this cut moves a boundary, it does not reorder a cascade.

**The agent episode obeys, it does not re-decide.** It rides the deterministic
directive rail — the IMAGE_CREATE / DOCUMENT_CREATE / APP_COMMAND precedent,
where a caller that already KNOWS the intent stamps ``directive_intent`` and
Global A obeys it verbatim instead of re-inferring from message words
(``global_loop/intent/run.py``). Here the caller knows more than the intent: it
carries the whole authored MotorProgram BY VALUE, so motor planning is replaced
by a rebind rather than re-run. Re-planning would be worse than wasteful — a
second planner pass could author a DIFFERENT program for work the operator has
already been told is under way.

**Rebinding is identity-only.** ``MotorProgram.fingerprint()`` is computed from
the outcome contract, pathway, execution plan and evidence contract — never
from the episode id — so binding the carried program to the agent episode
leaves the contract byte-identical while satisfying the identity assertions
that guard every downstream reader (AUTO intake, ``execution_evidence_from_
fold``, skill-foldback lineage) — each of which REQUIRES the program to name
the active episode.

Kill switch ``OMEGA_AGENT_LANE=0``: ``should_hand_off`` answers False and the
chat episode runs ``_start_auto`` inline exactly as it did before this cut.

**THE SWITCH GOVERNS MINTING, NOT HONOURING** (operator ruling 2026-08-11,
carried review risk 5). It used to govern both, and that made it a trap door
rather than a drain valve. A row already on the agent line when the switch
flipped was still leased by the tick and still opened an agent episode — the
routing in ``core.lanes`` is not gated on this switch and must not be, because
the line and the carrier have to agree about which lane a turn rides (cut 7).
But every READER was gated, so that episode found nothing:

  * ``restore_dispatch`` returned None, so the carried MotorProgram was never
    lifted onto the episode. ``carried_program`` then answered None and
    ``_start_motor_planning`` RE-PLANNED — authoring a different program for
    work the operator had already been told was under way, which is the exact
    outcome this module's "obey, do not re-decide" rule exists to prevent;
  * ``carried_origin_opened_by`` answered "", so the workspace port filed the
    operator's build under ``_practice`` and the build-complete notice never
    fired;
  * ``announce_completion`` returned None, so the work ended with NO receipt on
    a chat bubble that was resolved seconds after the ask.

Ruling: option (a) — the carried program executes, receipt intact. The two
reader seams (``restore_dispatch``, ``announce_completion``) are ungated, for
the same reason ``window_lane.restore_window`` is: an episode already dispatched
must always find its work, whatever the switch says now. Refusing loudly and
leaving the row on the line (option (b)) was rejected because the row is leased
by machinery this switch does not reach, so the refusal would have to be a
fail-hard on a turn the operator is waiting on, and the work would sit on the
line until someone noticed the brain was dying — losing the operator's work is
the thing the ruling forbids.

What the switch still turns off is every seam that MINTS agent work:
``should_hand_off`` (no new dispatch) and ``maybe_mint_continuation`` (no new
pass). The continuation refusal is LOUD — it emits its own skip row — because a
chain silently cut short and a build that simply finished are otherwise
indistinguishable (AGENTS.md §2.1). The operator still hears how it ended: with
no continuation minted, ``announce_completion`` fires and says "stopped short".
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from core.pipelines import CAUSAL_PARENT_KEY

AGENT_CONTINUATION = "agent_continuation"
AGENT_MOTOR_DISPATCH = "agent_motor_dispatch"
from pressure_loop.contributors import Contributor, make_contributor

log = logging.getLogger("omega.pressure_loop.agent_lane")

_OFF = {"0", "false", "no"}

# The episode.meta key the CHAT side stamps: "I planned this and gave it away."
# Deliberately distinct from the key the agent side reads, because several
# readers must tell the two apart — most sharply the motor-attempt close, which
# must never mint a phantom failed attempt for a program the episode handed off
# instead of running.
HANDOFF_META_KEY = "agent_lane_handoff"

# The episode.meta key the AGENT side carries: "this is the work I was given."
DISPATCH_META_KEY = "agent_lane_dispatch"

# The episode.meta key recording what ``restore_carried_pack`` replaced: the
# skill/action Meta A re-derived on this episode beside the ones that were
# actually dispatched, and whether they DIFFERED. Deliberately a compact record
# and not the discarded pack — it rides the journal, and a second multi-kilobyte
# pack per agent episode buys nothing the two ids do not already say.
PACK_REBIND_META_KEY = "agent_lane_pack_rebind"

# The episode.meta key an agent episode stamps when it hands the REST of an
# unfinished build to a fresh agent episode. It suppresses this episode's
# completion receipt: the work is not done, it moved.
CONTINUATION_META_KEY = "agent_lane_continuation"

# The system_messages record kind for an agent-lane completion receipt.
COMPLETION_KIND = "agent_work_complete"

# Chain depth is RECORDED, not capped (operator 2026-08-15). The in-episode loop
# no longer stops at a pass count either: a build runs AUTO -> Meta B -> Global B
# -> Omega -> AUTO for as long as Omega keeps returning APPLY, and ends when
# Omega returns build_accepted. The mind's judgement is the terminator.
#
# The old guard existed because the in-episode cap reset on every hop, so a build
# that yielded every time could hop lanes forever. With no in-episode cap there
# is nothing to reset and nothing to hop for — a build that is still going stays
# in the episode it started in. Depth still rides the carried metadata because it
# is the only thing that survives a hop, and an operator watching a build across
# a restart wants to know it is on pass four rather than pass one.
MAX_CHAIN_PASSES = 0  # 0 = uncapped; the value is kept so telemetry can read it


def agent_lane_enabled() -> bool:
    """Kill switch: OMEGA_AGENT_LANE=0 → the chat episode executes inline.

    Deliberately NOT coupled to OMEGA_LANE_PARALLEL / OMEGA_LANE_PRESSURE. With
    those off the handoff still works and still loses nothing — the agent
    episode is leased by the Agent lane's pressure worker. Its execution stays
    serialized with other Agent work while Chat, Browser, and Window proceed.

    Read ONLY by the seams that MINT agent work (see the module docstring's
    drain-valve ruling). The seams that HONOUR a row already on the lane do not
    consult it — a dispatch that is already durable is the operator's work, and
    a switch is not a reason to lose it.
    """
    return (os.environ.get("OMEGA_AGENT_LANE") or "1").strip().lower() not in _OFF


def _meta(episode) -> Dict[str, Any]:
    return dict(getattr(episode, "meta", None) or {})


def _write_meta(episode, meta: Dict[str, Any]) -> None:
    episode.meta = meta


def _episode_id(episode) -> str:
    return str(getattr(episode, "episode_id", "") or "").strip()


def _pressure_rows(episode) -> List[Dict[str, Any]]:
    pressure = dict(getattr(episode, "pressure", None) or {})
    return [row for row in (pressure.get("contributors") or []) if isinstance(row, dict)]


def _emit(event: str, **fields: Any) -> None:
    """Observability only — never the reason a handoff succeeds or fails."""
    try:
        from guardian.terminal_feedback import emit_data

        emit_data("pressure", event, **fields)
    except Exception:
        log.debug("agent lane telemetry failed", exc_info=True)


# ── the chat side: plan here, execute there ──────────────────────────────────


def transport_for(episode) -> str:
    """The executor transport this episode's plan would ride ("" when none).

    ``effective_motor_action`` is the one authority on that question: it
    resolves an EXECUTE_MOTOR_PROGRAM pack through its ``motor_transport`` and
    returns every other action verbatim, so USE_SKILL is recognised whether the
    plan reached it through the skill route or the motor route.
    """
    pack = dict(_meta(episode).get("execution_action_pack") or {})
    if not pack:
        return ""
    from auto_loop.motor_adapter import effective_motor_action

    try:
        return str(effective_motor_action(pack) or "")
    except (TypeError, ValueError):
        # A malformed transport is the motor adapter's business to reject where
        # it matters (AUTO). Here it simply is not a USE_SKILL handoff.
        return ""


def attached_program(episode) -> Optional[Dict[str, Any]]:
    """The canonical (non-shadow) program attached to this episode, or None.

    A shadow plan is observability only — it was never meant to be executed, so
    it is never handed to a lane that would execute it.
    """
    raw = _meta(episode).get("motor_program")
    if not isinstance(raw, dict) or raw.get("shadow") is True:
        return None
    return dict(raw)


def should_hand_off(episode) -> bool:
    """Should this planned program become durable continuation work?

    Three conditions remain necessary: the switch is on, the operator episode
    owns the request, and the plan is a USE_SKILL build with a real program
    attached. The result describes work; it never selects a physical worker.
    """
    if not agent_lane_enabled():
        return False
    # The operator's HUD turn is already on its execution lane.  Moving a plan
    # minted from that turn to the machine's agent line would split one request
    # across two queues and, historically, switch executors. Autonomous work may
    # still use the durable agent line; operator work never does.
    opened_by = str(getattr(episode, "opened_by", "") or "")
    if opened_by.startswith("operator"):
        return False
    for row in _pressure_rows(episode):
        if str(row.get("source") or "").lower() == "operator":
            return False
        if str(row.get("id") or row.get("type") or "").startswith("operator"):
            return False
    if _meta(episode).get(DISPATCH_META_KEY):
        # Defensive: an agent episode can never hand its own work on through
        # this seam (it is not a chat episode), and if the lane derivation ever
        # changed under us, re-dispatching the program it was told to execute
        # would be an infinite handoff loop rather than a lost build.
        return False
    if transport_for(episode) != "USE_SKILL":
        return False
    return attached_program(episode) is not None


def _operator_cycle_id(episode) -> str:
    for row in _pressure_rows(episode):
        if str(row.get("id") or row.get("type") or "") not in (
            "operator_query",
            "operator_command",
            "operator_response",
        ):
            continue
        detail = dict(row.get("detail") or row.get("metadata") or {})
        cycle_id = str(detail.get("cycle_id") or "").strip()
        if cycle_id:
            return cycle_id
    return ""


def _prior_attempts(episode) -> List[Dict[str, Any]]:
    """The motor-loop guard's attempt history, read in the planner's own order.

    The POST-CLOSE list wins when it exists. It is written by the motor-attempt
    close (``cognition_chain._apply_motor_loop_guard``) and is the only reading
    that makes the guard's limits span a chain: everything else on the episode
    is the history the PLANNER saw, which by construction excludes the attempt
    this episode just made. Carrying that pre-close list onto a continuation
    means every hop arrives with the same history, the equivalent/distinct
    counters never advance, and only ``MAX_CHAIN_PASSES`` stands between a
    repeating build and the operator. A dispatching CHAT episode never closes an
    attempt (its program moved), so it reads the planner's list exactly as
    before.
    """
    from pressure_loop.motor_loop_guard import ATTEMPTS_THROUGH_CLOSE_KEY

    meta = _meta(episode)
    pack = dict(meta.get("execution_action_pack") or {})
    inputs = dict(pack.get("execution_inputs") or {})
    rows = (
        meta.get(ATTEMPTS_THROUGH_CLOSE_KEY)
        or inputs.get("prior_attempts")
        or meta.get("prior_motor_attempts")
        or meta.get("prior_attempts")
        or ()
    )
    return [dict(row) for row in rows if isinstance(row, dict)]


def _objective(program: Dict[str, Any]) -> str:
    return str(dict(program.get("outcome_contract") or {}).get("objective") or "").strip()


def build_envelope(
    episode,
    *,
    original_request: str = "",
    chain_depth: int = 1,
    causal_root_episode_id: str = "",
) -> Dict[str, Any]:
    """Everything the agent episode needs to execute WITHOUT re-deciding anything.

    The program and the execution pack travel BY VALUE. They could have been
    left in a side store and referenced by id, but then the durable line would
    hold a promise rather than the work, and a store that lost the row would
    leave a queued dispatch pointing at nothing. The durable queue holds the job.

    ``causal_root_episode_id`` is the episode the OPERATOR spoke to; the
    immediate ``causal_parent_episode_id`` is whatever episode minted this row.
    They differ only along a continuation chain, and both are kept because they
    answer different questions: the parent is the dedup identity (a re-mint from
    the same episode must merge), the root is who to tell when it is finished.
    """
    program = attached_program(episode)
    if program is None:
        raise ValueError("an agent dispatch requires a canonical motor program")
    ep_id = _episode_id(episode)
    if not ep_id:
        raise ValueError("an agent dispatch requires the dispatching episode's id")
    from copy import deepcopy

    from core.symphony_time import now_iso

    # A DEEP copy, and the nested program is dropped: the envelope becomes the
    # durable row the instant it is enqueued, and ``hand_off_motor_program``
    # then pops the program out of the LIVE pack's execution_inputs. With a
    # shallow copy those are the same dict, so the row on disk (written at
    # enqueue) and the row in memory (mutated a moment later) disagreed about
    # their own payload, and the next write to the agent line would have
    # silently re-serialized the mutated one over the durable one. The program
    # rides at the top level; carrying it nested as well only doubled a
    # multi-kilobyte row in the line file and in every journal snapshot.
    pack = deepcopy(dict(_meta(episode).get("execution_action_pack") or {}))
    pack_inputs = pack.get("execution_inputs")
    if isinstance(pack_inputs, dict):
        pack_inputs.pop("motor_program", None)

    return {
        CAUSAL_PARENT_KEY: ep_id,
        "causal_root_episode_id": str(causal_root_episode_id or ep_id),
        "objective": _objective(program),
        "original_request": str(original_request or ""),
        # The ORIGIN provenance, carried because it is what makes this the
        # operator's build downstream: the workspace port sends operator work
        # to the operator's completed directory (and everything else to
        # _practice), and the build-complete notice fires only for an operator
        # origin. Without this the continuation would silently demote every build
        # it ran to autonomous practice.
        "origin_opened_by": str(
            getattr(episode, "opened_by", "")
            or dict(getattr(episode, "pressure", None) or {}).get("opened_by")
            or ""
        ),
        "operator_cycle_id": _operator_cycle_id(episode),
        # Deep, for the same reason as the pack: once enqueued this IS the
        # durable row, and nothing that happens to the dispatching episode
        # afterwards may reach inside it.
        "motor_program": deepcopy(program),
        "execution_action_pack": pack,
        "prior_motor_attempts": _prior_attempts(episode),
        "agent_chain_depth": int(chain_depth),
        "dispatched_at": now_iso(),
    }


def build_contributor(envelope: Dict[str, Any], *, contributor_type: str) -> Contributor:
    """The durable agent-line row for one envelope.

    ``source="omega"`` — this is the mind's own work, never the operator's
    utterance (``enqueue_input`` refuses an operator source outright).
    The metadata is deliberately FLAT-and-thin at the top level with the heavy
    payload nested under one key: Meta A merges every contributor's metadata
    into its planning context, so scattering ``motor_program`` /
    ``execution_action_pack`` across the top level would put them in front of
    readers that never asked for them.

    ``cycle_id`` names the DISPATCH, not the operator's chat cycle (which rides
    the envelope as ``operator_cycle_id``). A chat cycle id here would let a
    reply-delivery path mistake the agent episode for the operator's still-open
    bubble — which was resolved by the dispatch ack, seconds after they asked.
    """
    parent = str(envelope.get(CAUSAL_PARENT_KEY) or "")
    return make_contributor(
        "omega",
        contributor_type,
        1.0,
        {
            # The verbatim-obedience rail: Global A takes this intent as given
            # and never re-infers one from the message words.
            "directive_intent": "USE_SKILL",
            "message": str(envelope.get("original_request") or envelope.get("objective") or ""),
            "cycle_id": f"agent:{contributor_type}:{parent}",
            CAUSAL_PARENT_KEY: parent,
            DISPATCH_META_KEY: dict(envelope),
        },
    )


def hand_off_motor_program(episode, *, original_request: str = "") -> Dict[str, Any]:
    """Give this episode's planned build to the agent line. DURABLE WRITE FIRST.

    Returns the handoff record that was stamped onto the episode. Raises when
    the durable write fails — deliberately: the fold that follows this call
    tells the operator their build is under way, and saying that when nothing
    was queued would be a lie the system could not detect later. Fail-hard here
    means the cascade fails, the operator's turn stays in its line, and the ask
    is re-planned rather than silently dropped.

    Callable ONCE per episode: the program MOVES to the line (see below), so a
    second call finds nothing to hand off and says so. ``should_hand_off``
    answers False afterwards for the same reason.
    """
    envelope = build_envelope(episode, original_request=original_request)
    contributor = build_contributor(envelope, contributor_type=AGENT_MOTOR_DISPATCH)

    from pressure_loop.cognition_queue import enqueue_input

    queued = enqueue_input(contributor, operator_activity=False)

    program = envelope["motor_program"]
    handoff = {
        CAUSAL_PARENT_KEY: envelope[CAUSAL_PARENT_KEY],
        "causal_root_episode_id": envelope["causal_root_episode_id"],
        "contributor_type": AGENT_MOTOR_DISPATCH,
        "objective": envelope["objective"],
        "agent_chain_depth": envelope["agent_chain_depth"],
        # The program's identity stays on the record even though the program
        # itself leaves: the journal must still say WHAT this episode authored.
        "program_id": str(program.get("program_id") or ""),
        "program_fingerprint": str(program.get("fingerprint") or ""),
        # False means an identical dispatch was ALREADY waiting on the line — a
        # crash-resume between the durable write and the fold. The build still
        # happens exactly once; the fold below is simply re-run.
        "queued": bool(queued),
    }
    meta = _meta(episode)
    meta[HANDOFF_META_KEY] = dict(handoff)
    # The program MOVES; it is not copied. Every reader that finds an attached
    # program on a closed episode reads it as an attempt this episode made —
    # the motor-attempt close, the journal row, and the post-execution foldback
    # validator, which walks program → pack → skill pack → executor RESULT and
    # raises on the result that will never exist here (live 2026-08-10:
    # "post-execution foldback result episode_id mismatch" crashed the
    # dispatching episode's fold). Guarding each of them separately would leave
    # the next reader to discover the same thing; one honest move settles it.
    # The agent episode holds the only copy, in its durable line row.
    meta.pop("motor_program", None)
    pack = meta.get("execution_action_pack")
    if isinstance(pack, dict):
        inputs = pack.get("execution_inputs")
        if isinstance(inputs, dict):
            inputs.pop("motor_program", None)
    _write_meta(episode, meta)
    _emit(
        "agent_lane_dispatch",
        episode_id=envelope[CAUSAL_PARENT_KEY],
        summary=f"handed off to the agent lane: {envelope['objective'][:120]}",
        objective=envelope["objective"][:200],
        queued=bool(queued),
        chain_depth=envelope["agent_chain_depth"],
    )
    return handoff


def handed_off(episode) -> Optional[Dict[str, Any]]:
    """The handoff record when this episode gave its program away, else None."""
    record = _meta(episode).get(HANDOFF_META_KEY)
    return dict(record) if isinstance(record, dict) else None


# ── the agent side: obey the carried program ─────────────────────────────────


def restore_dispatch(episode) -> Optional[Dict[str, Any]]:
    """Lift the carried envelope off the ignition and onto the episode's meta.

    Called once, at episode open, so every later stage reads ONE place instead
    of re-scanning the pressure snapshot (and so the ignition's shape stops
    mattering to them). ``prior_motor_attempts`` is restored under the exact key
    the planner and the journal already read, which is what keeps the motor-loop
    guard's history continuous across the lane hop.

    NOT gated on OMEGA_AGENT_LANE (risk 5, ruling 2026-08-11) — the same rule
    ``window_lane.restore_window`` already follows. This episode exists because
    a durable row named a program; the switch cannot un-enqueue that row, so
    declining to lift it here does not cancel the work, it only makes the
    episode re-plan a DIFFERENT program for an ask the operator was already told
    was under way. See the module docstring.
    """
    for row in _pressure_rows(episode):
        detail = dict(row.get("detail") or row.get("metadata") or {})
        envelope = detail.get(DISPATCH_META_KEY)
        if not isinstance(envelope, dict) or not envelope.get("motor_program"):
            continue
        meta = _meta(episode)
        meta[DISPATCH_META_KEY] = dict(envelope)
        prior = [dict(r) for r in (envelope.get("prior_motor_attempts") or ()) if isinstance(r, dict)]
        if prior:
            meta["prior_motor_attempts"] = prior
        _write_meta(episode, meta)
        _emit(
            "agent_lane_execute",
            episode_id=_episode_id(episode),
            summary=f"executing carried program: {str(envelope.get('objective') or '')[:120]}",
            causal_parent=str(envelope.get(CAUSAL_PARENT_KEY) or ""),
            chain_depth=int(envelope.get("agent_chain_depth") or 1),
        )
        return dict(envelope)
    return None


def carried_dispatch(episode) -> Optional[Dict[str, Any]]:
    """The envelope this episode was given, or None for an ordinary episode."""
    envelope = _meta(episode).get(DISPATCH_META_KEY)
    return dict(envelope) if isinstance(envelope, dict) else None


def carried_program(episode):
    """The exact program this episode must execute, bound to THIS episode.

    Identity rebind only — see the module docstring. Returns None when this is
    not an agent episode, so the caller falls through to real motor planning.
    """
    envelope = carried_dispatch(episode)
    if envelope is None:
        return None
    raw = envelope.get("motor_program")
    if not isinstance(raw, dict):
        raise ValueError("carried agent dispatch has no motor program to execute")
    ep_id = _episode_id(episode)
    if not ep_id:
        raise ValueError("a carried motor program cannot be bound to an episode with no id")

    from dataclasses import replace

    from schema.motor_program import MotorProgram

    program = MotorProgram.from_dict(raw)
    if program.episode_id == ep_id:
        return program
    return replace(program, episode_id=ep_id)


# The episode.meta key holding what this build already knows about itself: prior
# objectives, artifacts, directories and files from every earlier run that
# touched this work.
RESUME_CONTEXT_META_KEY = "agent_resume_context"

# How many dev-lobe memories a resuming build carries. Bounded because this rides
# the execution pack into a prompt: enough to say where the work stood, not so
# much that the objective drowns in its own history.
RESUME_RECALL_LIMIT = 8


def recall_dev_context(episode) -> Optional[Dict[str, Any]]:
    """Give a resuming build back what it already learned about this work.

    Called once at agent-episode open. Without it, every agent episode starts
    blind: the dev lobe has been recording objectives, artifacts and execution
    reports all along (``memory/dev_artifact_intake``, ``memory/execution_report``,
    ``memory/build_objective`` all write coord [11, *, *]), but ordinary recall
    NEVER returns them — ``QuerySpec.denied_lobes`` defaults to (6, 11), so
    journal and dev are excluded unless a caller names them. The memory existed
    and nothing was asking for it.

    That default is right for conversation: the operator asking a question should
    not get build artifacts. It is exactly wrong for a build resuming its own
    work, which is why this asks explicitly (``allowed_lobes=[11]``,
    ``denied_lobes=[]``) rather than relaxing the default for everyone.

    This is what makes the loop long-horizon rather than merely long. The pass
    cap came off on 2026-08-15, so a build now runs until Omega accepts it — but
    an unbounded loop that starts cold every episode just spends more passes
    rediscovering the same directories. Operator: "that way omega can go right
    back into a build knowing exactly where it paused at."

    An empty successful recall is a first run. A failed read is recorded in the
    owning episode and raised with its cause, so unavailable history cannot be
    mistaken for a build that has no history.
    """
    envelope = carried_dispatch(episode)
    if envelope is None:
        return None
    program = dict(envelope.get("motor_program") or {})
    query = _objective(program) or carried_original_request(episode)
    if not query:
        return None

    ep_id = _episode_id(episode)
    try:
        from memory.api import QuerySpec, read_memory

        result = read_memory(
            QuerySpec(
                cycle_id=ep_id or "agent-resume",
                text=query,
                episode_id=ep_id,
                limit=RESUME_RECALL_LIMIT,
                # The dev lobe, and ONLY the dev lobe. A resuming build wants its
                # own history, not the conversation that asked for it.
                allowed_lobes=[11],
                denied_lobes=[],
            )
        )
    except Exception as exc:
        from memory.cognition_capture import DEV_RECALL_FAILED, capture_phase_failure

        reason = f"dev recall raised {type(exc).__name__}: {exc}"
        try:
            capture_phase_failure(
                episode, phase="admission", event_kind=DEV_RECALL_FAILED, reason=reason,
            )
        except Exception as capture_error:
            # Keep the failed read primary without replacing its existing cause.
            # The recording failure remains inspectable through exception context.
            if capture_error.__context__ is exc:
                capture_error.__context__ = exc.__context__
            raise exc
        _emit(
            "agent_resume_context",
            status="failed",
            episode_id=ep_id,
            summary=reason,
            ok=False,
        )
        raise

    items = [
        {
            "memory_id": str(getattr(c, "memory_id", "") or ""),
            "title": str(getattr(c, "title", "") or ""),
            "text": str(getattr(c, "text", "") or "")[:1200],
            "coord": list(getattr(c, "coord", None) or []),
        }
        for c in (getattr(result, "candidates", None) or ())
    ]
    items = [i for i in items if i["text"] or i["title"]]
    if not items:
        _emit(
            "agent_resume_context",
            status="ok",
            episode_id=ep_id,
            summary="no prior dev memory for this work — first run",
            recalled=0,
        )
        return None

    context = {"query": query, "recalled": len(items), "items": items}
    meta = _meta(episode)
    meta[RESUME_CONTEXT_META_KEY] = context
    _write_meta(episode, meta)
    _emit(
        "agent_resume_context",
        status="ok",
        episode_id=ep_id,
        summary=f"resuming with {len(items)} prior dev memories",
        recalled=len(items),
    )
    return context


def carried_resume_context(episode) -> Optional[Dict[str, Any]]:
    """What ``recall_dev_context`` put on the episode, for the executor's prompt."""
    value = _meta(episode).get(RESUME_CONTEXT_META_KEY)
    return dict(value) if isinstance(value, dict) else None


def restore_carried_pack(episode) -> Optional[Dict[str, Any]]:
    """Put the DISPATCHED execution pack back in front of the executor.

    Phase 4 risk 9 (ruling 2026-08-11). ``build_envelope`` has carried
    ``execution_action_pack`` since cut 9 and NOTHING read it: the agent episode
    ran its own Global A → Meta A and re-derived a pack from scratch. That was
    documented as harmless because ``attach_motor_program`` synchronizes the
    program's objective / evidence_required / completion_criteria onto whatever
    skill pack Meta A produced. It is not harmless — the program's authority
    covers the CONTRACT and says nothing about the SELECTION, and re-derivation
    is not a pure function of the ask:

      * ``meta_loop.skill.run.select_skill_for_intent`` weights
        ``capability_strength`` at 0.10, read from the memory strength index —
        which this build's own chat episode UPDATED at its terminus, between the
        dispatch and this episode. Two skills that tied when the program was
        authored do not tie any more;
      * ``_maybe_recall_context`` adds recalled per-skill boosts/penalties, from
        a memory that has moved;
      * the catalog itself is disk + trust policy (``_selection_catalog``), and
        the objective text Meta A scores is re-derived from the contributor
        message rather than from the pack that was planned;
      * and selection can FAIL outright (``skill_selection_failure`` → AUTO's
        ``missing_skill_pack``), stranding a build for which a complete, valid
        pack was sitting in the envelope the whole time.

    A different skill_id means different allowed/forbidden tools, a different
    safety envelope and different limits — the operator was told one thing was
    being built one way, and something else ran. That is the same class of error
    as re-planning the program, reached through the pack instead, so it gets the
    same answer: the dispatched pack WINS, rebound to this episode.

    Rebinding is identity-only (``schema.skill_execution_pack.
    rebind_skill_pack_to_episode``): the pack, its ``source_action_goal`` and its
    skill pack name THIS episode, and the skill pack's ``dispatch_id`` is
    recomputed because the foldback validators compare it against a result this
    episode's executor produces. Everything that constitutes the DECISION is
    verbatim.

    Called before ``attach_motor_program`` so the program's contract terms
    synchronize onto the CARRIED skill pack exactly as they did on the chat
    episode. Returns the pack that was installed, or None for an episode that
    carries no dispatch (or a dispatch minted before this field had a reader).

    The re-derived pack is not silently discarded: what Meta A chose, and
    whether it DIFFERED, is recorded under ``PACK_REBIND_META_KEY`` and emitted.
    A divergence is the evidence for this seam existing, and evidence that only
    shows up as an absence is not evidence (AGENTS.md §2.1).
    """
    envelope = carried_dispatch(episode)
    if envelope is None:
        return None
    carried = envelope.get("execution_action_pack")
    if not isinstance(carried, dict) or not carried:
        return None
    ep_id = _episode_id(episode)
    if not ep_id:
        raise ValueError("a carried execution pack cannot be bound to an episode with no id")

    from copy import deepcopy

    from schema.skill_execution_pack import rebind_skill_pack_to_episode

    meta = _meta(episode)
    rederived = meta.get("execution_action_pack")
    rederived = dict(rederived) if isinstance(rederived, dict) else {}

    pack = deepcopy(dict(carried))
    pack["episode_id"] = ep_id
    source = pack.get("source_action_goal")
    if isinstance(source, dict):
        source["episode_id"] = ep_id
    inputs = pack.get("execution_inputs")
    carried_skill_id = ""
    if isinstance(inputs, dict):
        skill_pack = inputs.get("skill_execution_pack")
        if isinstance(skill_pack, dict):
            rebound = rebind_skill_pack_to_episode(skill_pack, ep_id)
            inputs["skill_execution_pack"] = rebound
            # The mirror Meta A keeps for the foldback reads the same selection.
            meta["skill_execution_pack"] = deepcopy(rebound)
            carried_skill_id = str(dict(rebound.get("selected_skill") or {}).get("skill_id") or "")

    rederived_skill_id = str(
        dict(
            dict(
                dict(rederived.get("execution_inputs") or {}).get("skill_execution_pack") or {}
            ).get("selected_skill")
            or {}
        ).get("skill_id")
        or ""
    )
    record = {
        "carried_action": str(pack.get("action") or ""),
        "rederived_action": str(rederived.get("action") or ""),
        "carried_skill_id": carried_skill_id,
        "rederived_skill_id": rederived_skill_id,
        "diverged": bool(
            str(pack.get("action") or "") != str(rederived.get("action") or "")
            or carried_skill_id != rederived_skill_id
        ),
    }
    meta["execution_action_pack"] = pack
    meta[PACK_REBIND_META_KEY] = dict(record)
    _write_meta(episode, meta)
    _emit(
        "agent_lane_pack_rebind",
        episode_id=ep_id,
        summary=(
            "the dispatched execution pack wins over the re-derived one"
            + (" (they DIVERGED)" if record["diverged"] else "")
        ),
        **record,
    )
    return pack


def carried_origin_opened_by(episode) -> str:
    """The provenance that decides whether this is the OPERATOR's work."""
    envelope = carried_dispatch(episode)
    if envelope is None:
        return ""
    return str(envelope.get("origin_opened_by") or "").strip()


def carried_original_request(episode) -> str:
    envelope = carried_dispatch(episode)
    if envelope is None:
        return ""
    return str(envelope.get("original_request") or "").strip()


# ── the chain: unfinished work stays durable between episodes ────────────────


def maybe_mint_continuation(
    episode,
    result,
    *,
    in_episode_passes_exhausted: bool,
) -> Optional[Dict[str, Any]]:
    """Carry an unfinished agent build forward as a fresh agent episode.

    This is NOT the deterministic self-drive that was removed on 2026-06-25.
    Nothing here decides that more work is warranted: that decision is
    ``result.intent_decision`` — the MIND's, made by Omega's episode cognition
    over the executor's evidence and projected from its validated control
    envelope. The only thing this function
    decides is WHICH LANE the mind's continuation rides, and it fires only once
    the in-episode budget is spent, which is precisely when that continuation
    would otherwise have had to re-enter through generic pressure — on the meta
    lane, whose carrier is the mind's own stream, re-introducing exactly the
    cross-traffic this phase exists to end.

    Two guards, both hard: the in-episode cap must already be spent (so this can
    never pre-empt the cheaper in-episode pass), and the chain depth carried in
    the envelope caps how many episodes one ask may occupy.

    A third with the switch OFF: this seam MINTS a new agent row, which is
    exactly what the kill switch turns off (risk 5). It refuses LOUDLY — the
    skip row is emitted before the return — because the alternative reading of
    silence here is "the build finished", and the two must never look alike.
    The episode still files its receipt: with no continuation recorded,
    ``announce_completion`` runs and reports that it stopped short.
    """
    envelope = carried_dispatch(episode)
    if envelope is None:
        return None
    if not agent_lane_enabled():
        _emit(
            "agent_lane_continuation",
            status="skip",
            episode_id=_episode_id(episode),
            summary=(
                "OMEGA_AGENT_LANE=0: no further pass is minted; this episode's "
                "work stands and the operator hears that it stopped short"
            ),
            causal_root=str(
                envelope.get("causal_root_episode_id") or envelope.get(CAUSAL_PARENT_KEY) or ""
            ),
            chain_depth=int(envelope.get("agent_chain_depth") or 1),
        )
        return None
    if not in_episode_passes_exhausted:
        return None
    if getattr(result, "build_accepted", None) is True:
        return None
    decision = getattr(result, "intent_decision", None)
    if not isinstance(decision, dict):
        return None
    if str(decision.get("intent") or "").strip().upper() != "APPLY":
        return None
    reason = str(decision.get("reason") or "").strip()
    if not reason:
        return None

    if attached_program(episode) is None:
        # Nothing canonical to carry forward — the only way to reach this is
        # shadow mode (a plan that was never meant to execute), where a
        # continuation would be a promise with no program behind it.
        return None

    depth = int(envelope.get("agent_chain_depth") or 1)
    root = str(envelope.get("causal_root_episode_id") or envelope.get(CAUSAL_PARENT_KEY) or "")
    if MAX_CHAIN_PASSES and depth >= MAX_CHAIN_PASSES:
        _emit(
            "agent_lane_continuation",
            status="skip",
            episode_id=_episode_id(episode),
            summary=(
                f"chain guard held at pass {depth}/{MAX_CHAIN_PASSES}; "
                "the build stops here and the operator hears why"
            ),
            causal_root=root,
            chain_depth=depth,
        )
        return None

    forward = build_envelope(
        episode,
        original_request=str(envelope.get("original_request") or ""),
        chain_depth=depth + 1,
        causal_root_episode_id=root,
    )
    # The ORIGIN and the operator's cycle survive the whole chain: a build is
    # the operator's from the first ask to the last pass.
    forward["origin_opened_by"] = str(envelope.get("origin_opened_by") or "")
    forward["operator_cycle_id"] = str(envelope.get("operator_cycle_id") or "")
    forward["continuation_reason"] = reason[:500]
    contributor = build_contributor(forward, contributor_type=AGENT_CONTINUATION)

    from pressure_loop.cognition_queue import enqueue_input

    queued = enqueue_input(contributor, operator_activity=False)

    record = {
        CAUSAL_PARENT_KEY: forward[CAUSAL_PARENT_KEY],
        "causal_root_episode_id": root,
        "contributor_type": AGENT_CONTINUATION,
        "agent_chain_depth": depth + 1,
        "reason": reason[:500],
        "queued": bool(queued),
    }
    meta = _meta(episode)
    meta[CONTINUATION_META_KEY] = dict(record)
    _write_meta(episode, meta)
    _emit(
        "agent_lane_continuation",
        episode_id=_episode_id(episode),
        summary=f"pass {depth + 1}/{MAX_CHAIN_PASSES} queued: {reason[:120]}",
        causal_root=root,
        chain_depth=depth + 1,
        queued=bool(queued),
    )
    return record


# ── the operator hears about it ──────────────────────────────────────────────


def _completion_text(episode, envelope: Dict[str, Any], cascade_status: str) -> str:
    objective = str(envelope.get("objective") or "").strip() or "the work you asked for"
    global_ = dict(getattr(episode, "global_", None) or {})
    outcome = dict(global_.get("skill_outcome") or {})
    if outcome:
        complete = bool(outcome.get("objective_complete"))
    else:
        complete = str(cascade_status or "").strip() == "completed"
    if complete:
        return f"Finished on the agent lane — {objective}."
    blockers = [str(row).strip() for row in (outcome.get("unresolved_blockers") or ()) if str(row).strip()]
    if blockers:
        return f"Stopped short of finishing — {objective}. Unresolved: {'; '.join(blockers[:4])}."
    return f"Stopped short of finishing — {objective}."


def _already_announced(root: str) -> bool:
    from pressure_loop.system_messages import list_messages

    for record in list_messages():
        if str(record.get("kind") or "") != COMPLETION_KIND:
            continue
        meta = dict(record.get("meta") or {})
        if str(meta.get("causal_root_episode_id") or "") == root:
            return True
    return False


def announce_completion(episode, *, cascade_status: str = "completed") -> Optional[Dict[str, Any]]:
    """Tell the operator their dispatched work ended — the system_messages rail.

    The chat bubble that asked for this build was resolved seconds after they
    asked (the dispatch ack), so the ending needs a DURABLE, badge-counted
    record rather than a reply to a conversation that has long since moved on.
    That is exactly what the system-messages rail is: the channel the mind uses
    to speak unprompted, already used for the ASK artifact, calendar reminders
    and presented results.

    Silent in three cases, each for its own reason: an episode that is not an
    agent episode has nothing to report; an episode that handed the rest of the
    build to a continuation is not finished (its successor will announce); and
    an episode whose build lifecycle already told the operator how it ended must
    not say it twice — the deterministic build notice is the richer message and
    it wins.

    Idempotent by causal ROOT: one ask, one ending, however many agent episodes
    it took.

    NOT gated on OMEGA_AGENT_LANE (risk 5). The receipt is the ONLY thing that
    closes the loop on a request the operator was told would be reported back,
    and an episode that carries a dispatch ran the operator's build whatever the
    switch says now. Gating it made the switch a work-losing trap door: the
    build still ran, and its ending reached nobody.
    """
    envelope = carried_dispatch(episode)
    if envelope is None:
        return None
    meta = _meta(episode)
    if meta.get(CONTINUATION_META_KEY):
        return None
    if meta.get("operator_build_notice_posted"):
        return None
    root = str(envelope.get("causal_root_episode_id") or envelope.get(CAUSAL_PARENT_KEY) or "")
    if not root or _already_announced(root):
        return None

    text = _completion_text(episode, envelope, cascade_status)
    from pressure_loop.system_messages import append_system_query

    record = append_system_query(
        # An ANNOUNCEMENT, not a question (review finding). Opening a lifecycle
        # ask thread and an open-questions row for "your build finished" jammed
        # the operator ASK channel permanently: the journal enforces one open
        # operator question at a time, so a receipt nobody has any reason to
        # answer silenced every future ask the mind wanted to make — and every
        # Omega packet rendered it back as a question awaiting an answer.
        expects_answer=False,
        text=text,
        episode_id=_episode_id(episode),
        # The build's ORIGIN, not this episode's provenance: the operator asked
        # for it, and every reader of this record that cares whose work it was
        # asks that question of opened_by.
        opened_by=str(envelope.get("origin_opened_by") or ""),
        kind=COMPLETION_KIND,
        source_ref=root,
        meta={
            "causal_root_episode_id": root,
            CAUSAL_PARENT_KEY: str(envelope.get(CAUSAL_PARENT_KEY) or ""),
            "agent_episode_id": _episode_id(episode),
            "objective": str(envelope.get("objective") or "")[:400],
            "original_request": str(envelope.get("original_request") or "")[:2000],
            "operator_cycle_id": str(envelope.get("operator_cycle_id") or ""),
            "agent_chain_depth": int(envelope.get("agent_chain_depth") or 1),
            "cascade_status": str(cascade_status or ""),
        },
    )
    _emit(
        "agent_lane_completion",
        episode_id=_episode_id(episode),
        summary=text[:160],
        causal_root=root,
        message_id=str((record or {}).get("id") or ""),
    )
    return record

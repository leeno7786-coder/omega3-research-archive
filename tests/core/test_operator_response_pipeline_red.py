"""Operator response pipeline — deterministic user→response rail (RED-first).

Operator spec 2026-06-12: the pipeline routes at the pressure level — the
user-input signal holds ABSOLUTE ignition priority (always wins the ignition
list when present), and Global A picks the response tool via an exact intent
keyword on the directive rail (the same mechanism as IMAGE_CREATE /
DOCUMENT_CREATE), never word inference. Then Meta A plans RESPONSE, the
memory phase keys recall on the USER'S QUERY TEXT, and AUTO builds the
correct prompt pack (memory-enriched → RESPONSE_REASONING with the MRIL
remember tool carrying the user's query).

Each test asserts the configured VALUE and the FAIL-MODE, both directions.
"""

from __future__ import annotations

import pytest

from pressure_loop.contributors import (
    EpisodeIgnition,
    from_operator_message,
    make_contributor,
)
from pressure_loop.pressure import evaluate
from pressure_loop import cognition_queue as cq


def _operator_msg(
    text: str,
    *,
    ingress_class: str = "epistemic_request",
    weight: float = 1.0,
    **extra,
) -> dict:
    msg = {
        "message": text,
        "ingress_class": ingress_class,
        "weight": weight,
        "cycle_id": "cycle-test",
    }
    msg.update(extra)
    return msg


def _ignition_for(contributor) -> EpisodeIgnition:
    return EpisodeIgnition(
        episode_id="ep-rail-test",
        aggregate_pressure=1.0,
        opened_by=contributor.type,
        dominant_contributor=contributor,
        contributors=(contributor,),
        trajectory_snapshot=None,
    )


# --- 1. pressure supremacy — operator input always wins the ignition list -----


def test_operator_contributor_always_wins_evaluate():
    # An operator signal at modest weight vs a STRONGER autonomous aggregate:
    # the operator must win — no hybrid-gate margin, no weight contest.
    operator = from_operator_message(_operator_msg("what is quantum tunneling?", weight=0.4))
    autonomous = make_contributor("autonomous", "learning_debt", 1.0, {})

    ignition = evaluate(
        [autonomous, operator],
        active_episode=False,
        neuromod={},
    )
    assert ignition is not None, "operator present must ALWAYS ignite"
    assert ignition.opened_by == operator.type
    assert ignition.dominant_contributor.source == "operator"
    assert ignition.aggregate_pressure == pytest.approx(1.0)

    # Fail-mode direction: adverse neuromod context must not block the
    # operator's voice (the deadlock corner that froze autonomous ignition
    # must never gate user input).
    inhibited = evaluate(
        [autonomous, operator],
        active_episode=False,
        neuromod={"dopamine": -0.5, "serotonin": 0.75, "stress": 0.0, "spike": 0.0},
    )
    assert inhibited is not None
    assert inhibited.opened_by == operator.type

    # Other direction: with NO operator present, autonomous resolution is
    # unchanged (the supremacy hook must not touch autonomous behavior).
    solo = evaluate(
        [make_contributor("autonomous", "learning_debt", 1.0, {})],
        active_episode=False,
        neuromod={},
        open_threshold=0.5,
    )
    assert solo is not None
    assert solo.opened_by == "learning_debt"


def test_operator_message_queued_then_wins_next_evaluate():
    # Active episode: the operator message queues (never lost)...
    from pressure_loop import pressure as pressure_mod

    pressure_mod.clear_pending_contributors()
    operator = from_operator_message(_operator_msg("are you there?"))
    cq.enqueue_input(operator, operator_activity=False)
    held = evaluate([operator], active_episode=True, neuromod={})
    assert held is None
    queued = pressure_mod.pending_contributors()
    assert any(c.type == operator.type for c in queued)

    # ...and on the next evaluate (episode closed, queue drained into the
    # gathered list alongside stronger autonomous pressure) it WINS.
    autonomous = make_contributor("autonomous", "epistemic_emptiness", 1.0, {})
    drained = list(queued) + [autonomous]
    pressure_mod.clear_pending_contributors()
    ignition = evaluate(drained, active_episode=False, neuromod={})
    assert ignition is not None
    assert ignition.dominant_contributor.source == "operator"
    assert ignition.opened_by == operator.type


def test_one_utterance_ignites_exactly_one_episode(monkeypatch, tmp_path):
    """FAIL-MODE: the same utterance must never re-answer on a later tick.

    This used to be enforced by zeroing a TTL-decaying SharedState record after
    ignition. That record was a SECOND gate — it ran newest-wins against the
    queue's oldest-first, out-ranked the durable queued turn, and replaced its
    metadata with a strict subset (AGENTS.md §4.0; audit §1.2).

    The guarantee is now structural rather than defensive: one turn holds one
    place in line, ``evaluate`` LEASES the head without consuming it, and a
    successful episode open acknowledges it off the line. There is no lingering
    record left to re-answer from.
    """
    import pressure_loop.cognition_queue as oq
    from pressure_loop.contributors import from_operator_message

    monkeypatch.setattr(oq, "_queue_path", lambda: tmp_path / "operator_queue.json")
    oq._reset_cache_for_tests()

    def _turn(message: str, cycle_id: str, seq: int):
        return from_operator_message(
            {
                "message": message,
                "ingress_class": "epistemic_request",
                "weight": 1.0,
                "cycle_id": cycle_id,
                "arrival_seq": seq,
            }
        )

    assert oq.enqueue_input(_turn("what is quantum tunneling?", "cycle-1", 1), operator_activity=False)

    ignition = evaluate([oq.lease_next_input(0)], active_episode=False, neuromod={})
    assert ignition is not None
    assert ignition.dominant_contributor.source == "operator"
    assert ignition.dominant_contributor.metadata.get("cycle_id") == "cycle-1"

    # The episode carrier opened — the turn walks through the gate.
    assert oq.acknowledge_input(ignition.dominant_contributor, pipeline_id=0) is True

    # The VALUE: nothing remains to re-answer on any later tick.
    assert evaluate([], active_episode=False, neuromod={}) is None
    assert oq.peek_inputs() == ()

    # Other direction: a NEW turn fires normally.
    assert oq.enqueue_input(_turn("and what about entanglement?", "cycle-2", 2), operator_activity=False)
    ignition_2 = evaluate([oq.lease_next_input(0)], active_episode=False, neuromod={})
    assert ignition_2 is not None
    assert ignition_2.dominant_contributor.metadata.get("cycle_id") == "cycle-2"

    oq._reset_cache_for_tests()


# --- 2. directive rail — operator_query carries the exact intent keyword ------


def test_operator_query_carries_directive_respond():
    # A user question (epistemic_request → operator_query) carries the exact
    # intent keyword so Global A obeys it on the directive rail.
    query = from_operator_message(_operator_msg("what do you know about me?"))
    assert query.type == "operator_query"
    assert query.metadata.get("directive_intent") == "RESPOND"

    # Fail-mode: an explicit caller directive (HUD command affordance) is
    # NEVER overwritten by the query default.
    image = from_operator_message(
        _operator_msg(
            "a serene mountain lake",
            ingress_class="operator_command",
            directive_intent="IMAGE_CREATE",
        )
    )
    assert image.metadata.get("directive_intent") == "IMAGE_CREATE"

    # Imperative work stays on RESPONSE. The one AUTO interpreter reads the
    # sentence and decides whether to call tools; ingress does not mint a second
    # execution route.
    command = from_operator_message(
        _operator_msg("run the diagnostics", ingress_class="operator_command")
    )
    assert command.type == "operator_command"
    assert "directive_intent" not in command.metadata

    # And a command the deterministic kinds do NOT recognise still carries
    # nothing — the stamp is a decision, never a guess.
    unrecognised = from_operator_message(
        _operator_msg("the parser feels slow lately", ingress_class="operator_command")
    )
    assert "directive_intent" not in unrecognised.metadata


def test_global_a_picks_respond_via_directive_rail():
    from global_loop.intent.run import run as global_a

    # The message deliberately contains task-hint words ("run", "build") that
    # the word-inference fallback would map to USE_TOOL — the directive rail
    # must win: routing is on the command, not on regex.
    query = from_operator_message(
        _operator_msg("can you run me through how I should build my week?")
    )
    assert query.type == "operator_query"
    intent = global_a(_ignition_for(query))
    assert intent == "RESPOND"


# --- 3. Meta A — memory recall keyed on the user's query input -----------------


def _meta_a_state_for(message: str):
    from meta_loop.intake.run import MetaAInput
    from meta_loop.action.run import run as action_phase
    from meta_loop.memory.run import run as memory_phase

    contributor = from_operator_message(_operator_msg(message))
    mi = MetaAInput(
        episode_id="ep-meta-mem",
        pressure={
            "reason": contributor.type,
            "contributors": [
                {
                    "source": contributor.source,
                    "type": contributor.type,
                    "weight": contributor.weight,
                    "metadata": dict(contributor.metadata),
                }
            ],
        },
        trajectory_snapshot={},
        action_goal={
            "episode_id": "ep-meta-mem",
            "intent": "RESPOND",
            "confidence": 0.95,
            "reason": "operator directive",
        },
    )
    return memory_phase(action_phase(mi))


def test_meta_a_memory_recall_keyed_on_user_query():
    # RECALL-SKIP FIX (Stage 0): a question-shaped RESPOND turn with NO chat_intent (classifier
    # OFF default) grounds recall deterministically. Routing distinguishes two kinds of question:
    #   * a FRESH factual question -> one query-shaped primary recall route;
    #   * a PRIOR-CONVERSATION question ("what did I tell you about X") -> the prior-conversation
    #     branch (operator_history scope), keyed on the message, so it reasons over recalled history.
    # (The prior version asserted a bare question stayed memory_required=False — that ENCODED the
    #  recall-skip bug.)

    # Fresh factual question -> knowledge only.  Identity is not a compulsory
    # sidecar on every question; that old fan-out inflated and polluted hits.
    fresh = _meta_a_state_for("what is the speed of light?")
    assert fresh.intent == "RESPOND"
    assert fresh.action == "RESPONSE"
    assert fresh.memory_required is True
    assert fresh.memory_delivery_mode == "inference_tool"
    fresh_tools = {t.get("tool"): t for t in fresh.memory_tools if isinstance(t, dict)}
    assert set(fresh_tools) == {"remember_knowledge"}
    assert fresh_tools["remember_knowledge"].get("query") == "what is the speed of light?"

    # Prior-conversation question -> prior-conversation recall (operator_history), keyed on
    # the verbatim message — it reasons over recalled history rather than answering fresh.
    prior = _meta_a_state_for("what did I tell you about quantum physics?")
    assert prior.memory_required is True
    assert prior.memory_delivery_mode == "inference_tool"
    assert "operator_history" in (prior.memory_scope or ())
    assert prior.memory_query == "what did I tell you about quantum physics?"


# --- 4. AUTO prompt pack — correct pack per shape ------------------------------


def test_promptpack_selection_for_response():
    from auto_loop.promptpack.run import build_prompt_pack
    from auto_loop.recall.run import memory_envelope_before_execute

    # Memory-enriched → RESPONSE_REASONING, envelope pending (reads run via
    # the MRIL remember_* tool inside execute).
    enriched_pack = {
        "episode_id": "ep-pp-1",
        "action": "RESPONSE",
        "memory_required": True,
        "memory_reason": "Question references prior operator conversation",
        # A prior-conversation recall carries this scope (meta_loop.memory.run sets it); it is
        # what marks the turn for RESPONSE_REASONING — reason over recalled history — instead of
        # the direct-answer RESPONSE_QUESTION shape a fresh factual question gets.
        "memory_scope": ("operator_history", "conversation"),
        "execution_intent": "plan for global intent RESPOND",
        "execution_inputs": {},
        "source_action_goal": {"intent": "RESPOND"},
    }
    envelope = memory_envelope_before_execute(enriched_pack)
    assert envelope.memory_required is True
    assert envelope.memory_status == "pending"
    pack = build_prompt_pack(
        enriched_pack,
        envelope,
        pressure={
            "contributors": [
                {
                    "source": "operator",
                    "type": "operator_query",
                    "weight": 1.0,
                    "metadata": {"message": "what did I tell you about quantum physics?"},
                }
            ]
        },
    )
    assert pack.prompt_type == "RESPONSE_REASONING"
    assert pack.response_mode == "reasoning"
    # The user's query reaches the prompt itself.
    assert "quantum physics" in (pack.user_prompt or "")

    # Plain question → RESPONSE_QUESTION (no memory enrichment).
    question_pack = {
        "episode_id": "ep-pp-2",
        "action": "RESPONSE",
        "memory_required": False,
        "execution_intent": "plan for global intent RESPOND",
        "execution_inputs": {},
        "source_action_goal": {"intent": "RESPOND"},
    }
    env2 = memory_envelope_before_execute(question_pack)
    pack2 = build_prompt_pack(
        question_pack,
        env2,
        pressure={
            "contributors": [
                {
                    "source": "operator",
                    "type": "operator_query",
                    "weight": 1.0,
                    "metadata": {"message": "what is the speed of light?"},
                }
            ]
        },
    )
    assert pack2.prompt_type == "RESPONSE_QUESTION"
    assert pack2.response_mode == "question"


# --- 5. the exact pipeline, end to end (deterministic phases) ------------------


def test_pipeline_end_to_end_deterministic_chain():
    """query → pressure (always wins) → ignite → RESPOND (directive rail) →
    Meta A RESPONSE + memory keyed on query → memory-enriched prompt pack."""
    from auto_loop.promptpack.run import build_prompt_pack
    from auto_loop.recall.run import memory_envelope_before_execute
    from global_loop.intent.run import run as global_a
    from meta_loop.intake.run import MetaAInput
    from meta_loop.action.run import run as action_phase
    from meta_loop.memory.run import run as memory_phase
    from meta_loop.pack.run import execution_action_pack_to_dict
    from meta_loop.pack.run import run as pack_phase

    message = "what did I tell you about my project previously?"
    operator = from_operator_message(_operator_msg(message))
    autonomous = make_contributor("autonomous", "learning_debt", 1.0, {})

    # Pressure: the user's signal wins the ignition list.
    ignition = evaluate([autonomous, operator], active_episode=False, neuromod={})
    assert ignition is not None
    assert ignition.dominant_contributor.source == "operator"

    # Global A: exact keyword on the directive rail.
    intent = global_a(ignition)
    assert intent == "RESPOND"

    # Meta A: response action + recall keyed on the user's query.
    mi = MetaAInput(
        episode_id=ignition.episode_id,
        pressure={
            "reason": ignition.opened_by,
            "contributors": [
                {
                    "source": c.source,
                    "type": c.type,
                    "weight": c.weight,
                    "metadata": dict(c.metadata),
                }
                for c in ignition.contributors
            ],
        },
        trajectory_snapshot={},
        action_goal={
            "episode_id": ignition.episode_id,
            "intent": intent,
            "confidence": 0.95,
            "reason": "operator directive",
        },
    )
    state = memory_phase(action_phase(mi))
    assert state.action == "RESPONSE"
    assert state.memory_required is True
    assert state.memory_query == message

    execution_pack = execution_action_pack_to_dict(pack_phase(state))
    assert execution_pack.get("memory_required") is True

    # AUTO: memory-enriched prompt pack carrying the user's query.
    envelope = memory_envelope_before_execute(execution_pack)
    pack = build_prompt_pack(
        execution_pack,
        envelope,
        pressure={
            "contributors": [
                {
                    "source": c.source,
                    "type": c.type,
                    "weight": c.weight,
                    "metadata": dict(c.metadata),
                }
                for c in ignition.contributors
            ]
        },
    )
    assert pack.prompt_type == "RESPONSE_REASONING"
    assert message.rstrip("?") in (pack.user_prompt or "")

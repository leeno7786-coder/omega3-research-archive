"""Proactive ask pipeline (first half) — the mind's question competes, RED-first.

Operator spec 2026-06-12: when Omega's reasoning produces a query for the
operator, it becomes a pressure contributor on the NEXT cycle
(`operator_priority`, source "omega" — the reseed path). The semantics are
the deliberate INVERSE of operator supremacy:

- If it WINS the ignition contest → full route: Global A selects the tool
  for asking the user (PROACTIVE_RESPONSE), Meta A plans the asking action.
- If it LOSES (or an episode is already active) → it EVAPORATES: no queue,
  no re-stage, no pin — the question becomes nonexistent in that episode,
  respecting the mind's own attention. It returns only if a later reflection
  chooses to raise it again.

Each test asserts the configured VALUE and the FAIL-MODE, both directions.
"""

from __future__ import annotations

from deepsleep_loop._deepsleep_state import DeepSleepInput
from deepsleep_loop.reseed.run import generate_reseed
from pressure_loop.contributors import EpisodeIgnition, make_contributor
from pressure_loop.pressure import evaluate


def _ask_contributor(weight: float = 0.9, *, question: str | None = None):
    meta: dict = {"origin": "omega_pressure_feedback"}
    if question:
        meta["message"] = question
        meta["question"] = question
    return make_contributor("omega", "operator_priority", weight, meta)


def _ignition_for(contributor) -> EpisodeIgnition:
    return EpisodeIgnition(
        episode_id="ep-ask-test",
        aggregate_pressure=contributor.weight,
        opened_by=contributor.type,
        dominant_contributor=contributor,
        contributors=(contributor,),
        trajectory_snapshot=None,
    )


def _reset_pressure_state() -> None:
    from pressure_loop import pressure as pressure_mod

    pressure_mod.clear_pending_contributors()
    pressure_mod._RESEED_STAGING.clear()
    if hasattr(pressure_mod, "_RESEED_SUPPRESS"):
        pressure_mod._RESEED_SUPPRESS.clear()


# --- 1. WINS → full route to the asking tool -----------------------------------


def test_operator_priority_wins_routes_to_proactive_response():
    from global_loop.intent.run import run as global_a
    from meta_loop.action.run import _intent_to_action_map

    _reset_pressure_state()
    ask = _ask_contributor(0.9, question="What should I focus on?")
    weak = make_contributor("autonomous", "idle_probe_aggregate", 0.05, {})

    ignition = evaluate(
        [weak, ask],
        active_episode=False,
        neuromod={},
        open_threshold=0.5,
    )
    assert ignition is not None
    assert ignition.opened_by == "operator_priority"

    # Global A selects the ASK artifact — a FIRST-CLASS route (operator
    # directive 2026-06-12): never the IDLE catch-all (SELF_INQUIRY, the
    # old mis-route, was removed 2026-07-09 and collapsed to IDLE — the
    # mind's won ask must route, not fall back to quiet) and never folded
    # into PROACTIVE_RESPONSE (clean separation — every hop of the query
    # route stays independently auditable).
    intent = global_a(ignition)
    assert intent == "ASK_OPERATOR"
    assert intent != "IDLE"
    assert intent != "PROACTIVE_RESPONSE"

    # Meta A plans the asking action — its OWN action, not a shared one.
    assert _intent_to_action_map().get(intent) == "ASK_OPERATOR"

    from meta_loop.action.run import run as meta_a_run
    from meta_loop.intake.run import meta_a_input_from_episode
    from types import SimpleNamespace

    episode = SimpleNamespace(
        episode_id="ep-ask-meta",
        opened_by="operator_priority",
        global_={
            "intent_decision": {
                "episode_id": "ep-ask-meta",
                "intent": "ASK_OPERATOR",
                "confidence": 0.9,
                "reason": "test",
            }
        },
        pressure={
            "opened_by": "operator_priority",
            "contributors": [
                {
                    "id": "operator_priority",
                    "weight": 0.9,
                    "detail": {
                        "question": "What should I focus on?",
                        "message": "What should I focus on?",
                    },
                }
            ],
        },
    )
    mi = meta_a_input_from_episode(episode)
    state = meta_a_run(mi)
    assert state.execution_inputs.get("question") == "What should I focus on?"
    assert state.execution_inputs.get("message") == "What should I focus on?"


def test_ask_action_builds_its_own_prompt_pack():
    """Clean separation through AUTO too: the ask has its OWN pack type,
    never PROACTIVE_REASONING — the route stays auditable at every layer."""
    from auto_loop.promptpack.run import build_prompt_pack
    from auto_loop.recall.run import memory_envelope_before_execute

    pack = {
        "episode_id": "ep-ask-pp",
        "action": "ASK_OPERATOR",
        "memory_required": False,
        "execution_intent": "plan for global intent ASK_OPERATOR",
        "execution_inputs": {},
        "source_action_goal": {"intent": "ASK_OPERATOR"},
    }
    env = memory_envelope_before_execute(pack)
    pp = build_prompt_pack(
        pack,
        env,
        pressure={
            "contributors": [
                {
                    "source": "omega",
                    "type": "operator_priority",
                    "weight": 0.9,
                    "metadata": {"message": "What should I focus on?"},
                }
            ]
        },
    )
    assert pp.prompt_type == "ASK_OPERATOR_QUERY"
    assert pp.prompt_type != "PROACTIVE_REASONING"
    assert pp.response_mode == "question"
    # The mind's seed question reaches the prompt.
    assert "What should I focus on?" in (pp.user_prompt or "")


# --- 2. LOSES → evaporates everywhere ------------------------------------------


def test_operator_priority_loses_and_evaporates():
    from pressure_loop import pressure as pressure_mod

    _reset_pressure_state()
    ask = _ask_contributor(0.3, question="What should I focus on?")
    strong = make_contributor("autonomous", "learning_debt", 1.0, {})

    ignition = evaluate(
        [strong, ask],
        active_episode=False,
        neuromod={},
        open_threshold=0.5,
    )

    # The mind's ask is NOT the user's voice: source "omega" must NOT trip
    # operator supremacy — the stronger autonomous signal wins.
    assert ignition is not None
    assert ignition.opened_by == "learning_debt"

    # And the losing ask EVAPORATES: nothing queued, nothing re-staged —
    # the question is nonexistent in this episode.
    assert not any(c.type == "operator_priority" for c in pressure_mod.pending_contributors())
    assert not any(c.type == "operator_priority" for c in pressure_mod.staged_signals())


def test_operator_priority_dropped_when_episode_active():
    from pressure_loop import pressure as pressure_mod

    _reset_pressure_state()
    ask = _ask_contributor(0.9, question="Are you there?")

    held = evaluate([ask], active_episode=True, neuromod={})
    assert held is None
    # Unlike USER input (which queues so no message is ever lost), the
    # mind's ask is never pinned: an active episode means the moment passed.
    assert not any(c.type == "operator_priority" for c in pressure_mod.pending_contributors())


# --- 3. the reseed carries the model's actual question --------------------------


def test_reseed_carries_the_models_question(monkeypatch):
    question = "Operator — what should I prioritize learning for you?"

    ds = DeepSleepInput(
        window_id="window-ask-01",
        window_reflection={
            "reflection": {
                "reasoning": {"what": "w"},
                "trajectory": {},
                "pressure_feedback": {
                    "contributors_to_raise": [],
                    "contributors_to_lower": [],
                    "contributors_to_create": [
                        {
                            "type": "operator_priority",
                            "weight": 0.9,
                            "question": question,
                        }
                    ],
                },
            },
            "trajectory_update": {"decision": "idle"},
            "trajectory_diff": "FOLLOWING",
        },
        episode_window=[],
        trajectory_snapshot={},
        omega_reasoning={},
    )
    contributors = generate_reseed(ds)
    asks = [c for c in contributors if c.type == "operator_priority"]
    assert len(asks) == 1
    assert asks[0].source == "omega"
    # The model's question text RIDES the contributor — downstream Meta A
    # dominant-text and the prompt pack read metadata["message"] for free.
    assert asks[0].metadata.get("message") == question
    assert asks[0].metadata.get("question") == question

    # Other direction: an entry WITHOUT a question fabricates nothing.
    ds_bare = DeepSleepInput(
        window_id="window-ask-02",
        window_reflection={
            "reflection": {
                "reasoning": {"what": "w"},
                "trajectory": {},
                "pressure_feedback": {
                    "contributors_to_raise": [{"type": "operator_priority", "weight": 0.8}],
                    "contributors_to_lower": [],
                    "contributors_to_create": [],
                },
            },
            "trajectory_update": {"decision": "idle"},
            "trajectory_diff": "FOLLOWING",
        },
        episode_window=[],
        trajectory_snapshot={},
        omega_reasoning={},
    )
    bare = [c for c in generate_reseed(ds_bare) if c.type == "operator_priority"]
    assert len(bare) == 1
    assert "message" not in bare[0].metadata
    assert "question" not in bare[0].metadata

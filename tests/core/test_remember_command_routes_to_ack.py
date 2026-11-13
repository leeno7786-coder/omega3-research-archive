"""An explicit "remember this" command is an INGEST, not a question.

Live 2026-07-31 (episode 3238abd7): "Remember this too: Mitchell Ray is a
co-owner of Omega AI LLC, he has 2 daughters, he's married and he also attends
the University of Phoenix." was classified correctly at the pressure layer
(``classification=deterministic:remember``) and then ran as RESPONSE_REASONING
anyway. Recall returned nothing -- correctly, the facts were NEW -- so the
composer authored a refusal and the operator got both an acknowledgement and
"Not on record — I do not have any memory records regarding Mitchell Ray's
ownership of Omega AI LLC ..." in the same reply.

Nothing in the shape chain claimed a store command: "remember" is not in
_intent_to_pack, the text is not question-shaped, memory_required was False and
no ack keyword matched the execution_intent -- so it fell through to the final
RESPONSE_REASONING else-branch, which is the composer's door.

Operator ruling: "the remember pipeline doesn't need to go through composer or
chat, it just hits the deterministic line and goes to ingestion then foldback."
RESPONSE_ACK is that lane -- it pays for neither recall-answering nor answer
engineering, and it is the only shape that folds back through Omega
(cognition_chain.py:1105).
"""

from __future__ import annotations

import pytest

from auto_loop.promptpack.run import build_prompt_pack
from auto_loop.recall.run import memory_envelope_before_execute


def _pack_for(message: str, intent=None, *, memory_required: bool = False):
    execution_pack = {
        "episode_id": "ep-remember",
        "action": "RESPONSE",
        "memory_required": memory_required,
        "execution_intent": "plan for global intent RESPOND",
        "execution_inputs": {},
        "source_action_goal": {"intent": "RESPOND"},
    }
    env = memory_envelope_before_execute(execution_pack)
    meta = {"message": message}
    if intent:
        meta["chat_intent"] = intent
    return build_prompt_pack(
        execution_pack,
        env,
        pressure={
            "contributors": [
                {
                    "source": "operator",
                    "type": "operator_query",
                    "weight": 1.0,
                    "metadata": meta,
                }
            ]
        },
    )


LIVE_TURN = (
    "Remember this too: Mitchell Ray is a co-owner of Omega AI LLC, he has 2 "
    "daughters, he's married and he also attends the University of Phoenix."
)


def test_the_exact_live_remember_turn_acks_instead_of_reasoning():
    pack = _pack_for(LIVE_TURN)
    assert pack.prompt_type == "RESPONSE_ACK"
    assert pack.response_mode == "ack"


@pytest.mark.parametrize(
    "message",
    [
        "Remember that Mitchell is married.",
        "remember this: my sister is named Miranda",
        "Please remember I moved to Austin.",
        "Note that Abel starts school in August.",
    ],
)
def test_remember_directives_route_to_ack(message: str):
    assert _pack_for(message).prompt_type == "RESPONSE_ACK"


@pytest.mark.parametrize("fuzzy", ["reasoning", "question", "freeform"])
def test_remember_overrides_a_fuzzy_classifier_label(fuzzy: str):
    """Same deterministic-override pattern RESPONSE_TEMPORAL already uses: an
    explicit imperative directive is high precision and must beat a fuzzy or
    stale classifier label."""
    assert _pack_for(LIVE_TURN, fuzzy).prompt_type == "RESPONSE_ACK"


def test_a_question_about_memory_is_still_a_question():
    """The widening must not swallow genuine recall questions."""
    assert _pack_for("What do you remember about Noah?").prompt_type != "RESPONSE_ACK"


def test_merely_mentioning_the_word_remember_is_not_a_directive():
    """"remember" inside a question is not a store command."""
    assert _pack_for("Do you remember what I told you about Mitchell?").prompt_type != (
        "RESPONSE_ACK"
    )

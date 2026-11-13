"""Stage 0 boot-gate: read_memory IS invoked on a QUESTION-intent turn.

The recall-skip bug: a plain factual question ("who is your creator?") arriving as a
RESPOND turn with NO chat_intent (the classifier returned no label — runtime unreachable
or the flag disabled) left execution_pack["memory_required"]=False, so the AUTO recall gate
(auto_loop.execute.run.memory_recall_required) never ran run_forced_memory_tools and
read_memory was never called. This asserts the gate the whole program depends on.
"""

from __future__ import annotations

from meta_loop._meta_a_state import MetaAState
from meta_loop.memory.run import run as meta_memory_run
from meta_loop.pack.run import run as meta_pack_run, execution_action_pack_to_dict
from auto_loop.mril_tools.run import memory_recall_required, run_forced_memory_tools


# The four constraint flags the cascade always carries (meta_loop/action/run.py builds
# MetaAState with these). meta_loop.pack.run.validate_execution_action_pack requires them,
# so the boot-gate test must mirror the real state to build a valid execution pack. The
# plan's snippet used constraints={}; adapted to the real pack-validation contract.
_META_CONSTRAINTS = {
    "planning_only": True,
    "no_inference_in_meta": True,
    "no_execution_in_meta": True,
    "no_model_fields": True,
}


def _question_state(text: str) -> MetaAState:
    # A fresh operator chat turn classified RESPOND, with NO chat_intent in meta
    # (classifier returned no label — runtime unreachable or the flag disabled).
    return MetaAState(
        mi=None,
        episode_id="ep-recall-skip",
        intent="RESPOND",
        action="RESPONSE",
        source={"episode_id": "ep-recall-skip"},
        text=text,
        target_str=text,
        constraints=dict(_META_CONSTRAINTS),
        meta={},  # <-- no "chat_intent": the bug condition
    )


def test_recall_invoked_on_question_intent():
    state = meta_memory_run(_question_state("who is your creator?"))

    # 1. The Meta-A memory gate must mark the turn as needing recall.
    assert state.memory_required is True, (
        "QUESTION-intent turn left memory_required=False — recall is skipped"
    )

    # 2. The pack the gate produces must drive the AUTO recall gate True.
    pack = execution_action_pack_to_dict(meta_pack_run(state))
    assert memory_recall_required(pack) is True

    # 3. run_forced_memory_tools must actually call read_memory (injected spy).
    calls: list = []

    def _spy_read_memory(spec):
        calls.append(spec)

        class _R:
            candidates = []

        return _R()

    run_forced_memory_tools(pack, "ep-recall-skip", memory=_spy_read_memory)
    assert calls, "read_memory was never invoked on a QUESTION-intent turn"

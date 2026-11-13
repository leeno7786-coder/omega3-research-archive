"""Slice 3 — acceptance (rebuild path).

Full ``run_auto_front`` E2E tests removed: they pulled in ``memory.api`` /
``runtime_capability_broker`` paths that depended on ``guardian.terminal_feedback``,
which is retired on the rebuild branch. AUTO behavior is covered by
``test_slice3_runner_stops_at_autopack.py``, ``test_cognition_loop.py``, and
``tests/core/test_auto_*.py`` where present.
"""

from __future__ import annotations

from meta_loop.meta_a import MetaAInput, build_execution_action_pack, execution_action_pack_to_dict


def test_invariants_models_only_in_auto_execution():
    """Meta A pack stays model-free (AUTO owns inference fields)."""
    mi = MetaAInput(
        episode_id="ep_i",
        pressure={"reason": "x", "contributors": []},
        trajectory_snapshot={},
        action_goal={
            "episode_id": "ep_i",
            "intent": "IDLE",
            "target": None,
            "confidence": 1.0,
            "reason": "r",
        },
    )
    pack_dict = execution_action_pack_to_dict(build_execution_action_pack(mi))
    for banned in ("model", "llm", "prompt", "temperature"):
        assert banned not in pack_dict
        assert banned not in (pack_dict.get("execution_inputs") or {})

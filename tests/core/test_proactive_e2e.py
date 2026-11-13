"""Full proactive-response loop — autonomous status → main chat feed."""

from __future__ import annotations

from tests.helpers.pipeline_ownership import bind_ignition

import pytest

import cognition_chain as cl
from deepsleep_loop._deepsleep_state import DeepSleepInput
from deepsleep_loop.reseed.run import generate_reseed
from pressure_loop.episode import EpisodeStore
from pressure_loop.pressure import evaluate, gather_contributors, stage_reseed

SEED_MESSAGE = "Finished the first pass on quantum basics."
UPDATE_TEXT = "I completed a learning pass on quantum basics — moving to applications next."


@pytest.fixture()
def _cascade(monkeypatch):

    seen: list[dict] = []

    def _fake_generate(**kwargs):
        seen.append(
            {
                "operation": str(kwargs.get("operation") or ""),
                "system": str(kwargs.get("system") or ""),
                "prompt": str(kwargs.get("prompt") or ""),
            }
        )
        if str(kwargs.get("operation") or "") == "episode_cognition":
            import json

            return {"text": json.dumps({"response_text": UPDATE_TEXT})}
        return {"text": UPDATE_TEXT}

    monkeypatch.setattr("runtime.socket.invoke_runtime_generate", _fake_generate)
    monkeypatch.setattr("pressure_loop.pressure_field.build_registry", lambda: {})
    return seen


def test_full_proactive_loop_lands_in_chat_store(_cascade, monkeypatch, tmp_path):
    import pressure_loop.proactive_chat_messages as pcm

    monkeypatch.setattr(pcm, "_store_path", lambda: tmp_path / "pcm.json")
    pcm._reset_cache_for_tests()

    from pressure_loop import pressure as pressure_mod

    pressure_mod.clear_pending_contributors()
    pressure_mod._RESEED_STAGING.clear()

    store = EpisodeStore()

    ds = DeepSleepInput(
        window_id="w-proactive-e2e",
        window_reflection={
            "reflection": {
                "reasoning": {"what": "w"},
                "trajectory": {},
                "pressure_feedback": {
                    "contributors_to_raise": [],
                    "contributors_to_lower": [],
                    "contributors_to_create": [
                        {
                            "type": "proactive_update",
                            "weight": 0.7,
                            "message": SEED_MESSAGE,
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

    stage_reseed(generate_reseed(ds))
    gathered = gather_contributors(None)
    proactive = next(c for c in gathered if c.type == "proactive_update")
    assert proactive.metadata["lane"] == "chat"

    ignition = evaluate(gathered, active_episode=False, neuromod={}, open_threshold=0.5)
    assert ignition is not None
    assert ignition.opened_by == "proactive_update"

    episode = cl.ignite_episode(store, bind_ignition(ignition))
    assert episode is not None
    assert store.current() is None

    intent = (episode.global_ or {}).get("intent_decision", {}).get("intent")
    assert intent == "PROACTIVE_RESPONSE"
    pack = (episode.meta or {}).get("execution_action_pack", {})
    assert pack.get("action") == "PROACTIVE_RESPONSE"

    pending = pcm.list_pending()
    assert len(pending) == 1
    assert UPDATE_TEXT in pending[0]["text"]
    proactive_passes = [call for call in _cascade if call["operation"] == "auto_response"]
    assert proactive_passes
    assert any(
        "cognition-initiated proactive update" in call["system"]
        for call in proactive_passes
    )

    from pressure_loop.system_messages import list_messages

    assert list_messages() == []

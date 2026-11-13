"""Per-episode mastery deltas remain transient until canonical hydration.

Risk 8 (ruling 2026-08-11) moved the annotation from a second call made after
the terminus returned into the terminus's own serialized commit region, so the
delta and the body it describes are decided by one statement about one window.
These tests drive that seam rather than the removed helper.
"""

from __future__ import annotations

import cognition_chain as cl
from deepsleep_loop._deepsleep_state import DeepSleepOutcomePack
from pressure_loop.episode import EpisodeStore
from pressure_loop.reflection_window import ReflectionWindow, load_reflection_window
from tests.core.deepsleep_window_fixtures import patch_window_deepsleep_paths


def _sample_mastery_effects() -> dict:
    return {
        "python": {
            "domain_name": "Python",
            "knowledge_delta": 0.10,
            "application_delta": 0.0,
            "knowledge_mastery": 0.10,
            "application_mastery": 0.0,
            "status": "learning",
        }
    }


def _fake_pack(episode_id: str, *, mastery_effects: dict | None = None) -> DeepSleepOutcomePack:
    effects = dict(mastery_effects or _sample_mastery_effects())
    journal_entry = {"mastery_effects": effects}
    return DeepSleepOutcomePack(
        episode_id=episode_id,
        journal_entry=journal_entry,
        updated_memory=journal_entry,
        updated_identity={},
        updated_mastery={},
        updated_open_queries={},
        updated_trajectory={},
        pressure_seed=[],
        projection_state={},
        episode_closed=False,
        pressure_reset=False,
    )


def test_per_episode_deepsleep_appends_mastery_delta_to_window(tmp_path, monkeypatch):
    paths = patch_window_deepsleep_paths(monkeypatch, tmp_path)
    effects = _sample_mastery_effects()
    captured: list[str] = []

    def _fake_run(store, *, cascade_status="completed"):
        active = store.current()
        assert active is not None
        assert active.episode_id == "ep-delta-1"
        assert active.global_["action_goal"]["intent"] == "LEARN"
        captured.append(active.episode_id)
        # Phase 4 cut 13: the reflection-window append moved into the terminus's
        # serialized commit region, reached through before_finalize. This stub
        # stands in for that terminus, so it runs the same seam — including the
        # mastery delta, which risk 8 made part of the very same call.
        store.close(
            cascade_status=cascade_status,
            before_finalize=lambda ep, body: store.commit_reflection_window_append(
                ep, body, mastery_delta=effects
            ),
        )
        return _fake_pack(active.episode_id, mastery_effects=effects)

    monkeypatch.setattr("deepsleep_loop.loop.run_deepsleep_for_episode", _fake_run)

    store = EpisodeStore(
        reflection_window_path=paths["window_path"],
        trajectory_path=paths["trajectory_path"],
    )
    ep = store.open(
        opened_by="test", pressure={}, pipeline_id=0, work_kind="autonomous"
    )
    assert ep is not None
    ep.episode_id = "ep-delta-1"
    ep.global_ = {
        "action_goal": {"intent": "LEARN", "target": "Python"},
        "outcome": {
            "goal": {"intent": "LEARN", "target": "Python"},
            "outcome": {"goal_state": "completed", "status": "success"},
        },
    }
    ep.meta = {"execution_action_pack": {"action": "web_fetch"}}

    cl._run_per_episode_terminus(store, ep, cascade_status="completed")

    assert captured == ["ep-delta-1"]
    window = store.get_active_window()
    assert window is not None
    assert window.episode_mastery_deltas == [
        {"episode_id": "ep-delta-1", "mastery_effects": effects}
    ]

    loaded = load_reflection_window(paths["window_path"])
    assert loaded is not None
    assert loaded.episodes == ["ep-delta-1"]
    assert loaded.episode_mastery_deltas == []


def test_reflection_window_does_not_round_trip_episode_mastery_deltas():
    deltas = [
        {"episode_id": "ep-1", "mastery_effects": _sample_mastery_effects()}
    ]
    window = ReflectionWindow(
        window_id="win-deltas",
        started_at_episode="ep-1",
        trajectory_snapshot={"target_domain": "python"},
        episodes=["ep-1"],
        episode_bodies=[{"episode_id": "ep-1", "intent": "LEARN"}],
        episode_mastery_deltas=deltas,
    )

    restored = ReflectionWindow.from_dict(window.to_dict())
    assert restored.episode_mastery_deltas == []

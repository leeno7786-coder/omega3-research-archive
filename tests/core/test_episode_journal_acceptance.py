from __future__ import annotations

from pressure_loop.episode import EpisodeStore
from pressure_loop.episode_journal import journal_window


def test_episode_journal_acceptance_writes_prior_episode_on_close(tmp_path, monkeypatch):
    journal_path = tmp_path / "episode_journal.jsonl"

    # Route the failure-soft archive hook into a temp file.
    monkeypatch.setattr(
        "pressure_loop.policy.episode_journal_path",
        lambda policy=None: journal_path,
    )

    store = EpisodeStore()
    ep = store.open(
        opened_by="operator_command", pipeline_id=0, work_kind="operator_turn"
    )

    # LEARN-shaped episode: action_goal.intent=LEARN + execution_action_pack.action=USE_TOOL.
    ep.global_ = {
        "action_goal": {"intent": "LEARN"},
        "outcome": {"status": "success"},
    }
    ep.meta = {"execution_action_pack": {"action": "USE_TOOL"}}
    ep.omega = {}  # Slice 5b not built yet -> trajectory/reflection remain None.

    closed = store.close()
    assert closed is ep

    window = journal_window(50)
    assert len(window) == 1
    pe = window[0]

    assert pe.intent == "LEARN"
    assert pe.outcome is not None
    assert pe.trajectory_update is None
    assert "omega_reflection" not in pe.to_dict()

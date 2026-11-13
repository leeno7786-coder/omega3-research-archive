"""Rollback facts must enter the next cognitive episode."""

from __future__ import annotations

from types import SimpleNamespace


def _patch_queue(monkeypatch, tmp_path):
    from pressure_loop import system_event_queue

    monkeypatch.setattr(
        system_event_queue,
        "_store_path",
        lambda: tmp_path / "rollback_events.json",
    )
    system_event_queue._reset_cache_for_tests()
    return system_event_queue


def test_rollback_event_is_pressure_contributor_and_can_ignite(monkeypatch, tmp_path):
    queue = _patch_queue(monkeypatch, tmp_path)
    queue.enqueue_rollback_event(
        {
            "snapshot_id": "snap-1",
            "file_path": "F:/workspace/app.py",
            "scope": "operator_workspace",
            "action": "restore",
            "message": "rolled back F:/workspace/app.py",
        }
    )

    monkeypatch.setattr("pressure_loop.pressure_field.build_registry", lambda: {})
    monkeypatch.setattr("memory.taxonomy.config.taxonomy_pressure_enabled", lambda: False)

    from pressure_loop.pressure import evaluate, gather_contributors

    contributors = gather_contributors(shared_state=None)
    rollback = [c for c in contributors if c.type == "dev_rollback"]
    assert len(rollback) == 1
    assert rollback[0].source == "system"
    assert rollback[0].metadata["lane"] == "chat"
    from core.pipelines import lane_for_contributor

    assert lane_for_contributor(rollback[0]) == "chat"
    assert rollback[0].weight == 1.0
    assert rollback[0].metadata["rollback_events"][0]["snapshot_id"] == "snap-1"

    ignition = evaluate(contributors, active_episode=False, open_threshold=1.0, neuromod={})
    assert ignition is not None
    assert ignition.opened_by == "dev_rollback"

    from pressure_loop.contributors import make_contributor

    tied = evaluate(
        list(contributors) + [make_contributor("autonomous", "curiosity_gap", 1.0, {})],
        active_episode=False,
        open_threshold=1.0,
        neuromod={},
    )
    assert tied is not None
    assert tied.opened_by == "dev_rollback"


def test_next_episode_drains_rollback_event_into_episode_meta(monkeypatch, tmp_path):
    queue = _patch_queue(monkeypatch, tmp_path)
    queue.enqueue_rollback_event(
        {
            "snapshot_id": "snap-2",
            "file_path": "F:/workspace/created.py",
            "scope": "operator_workspace",
            "action": "delete_created_file",
            "message": "deleted created file F:/workspace/created.py",
        }
    )

    from cognition_chain import _attach_pending_system_events

    episode = SimpleNamespace(
        episode_id="ep-rollback",
        meta={},
        pressure={"contributors": []},
    )
    _attach_pending_system_events(episode)

    assert queue.peek_rollback_events() == ()
    assert episode.meta["dev_rollback_events"][0]["snapshot_id"] == "snap-2"
    contributors = episode.pressure["contributors"]
    assert contributors[0]["id"] == "dev_rollback"
    assert contributors[0]["detail"]["rollback_events"][0]["action"] == "delete_created_file"

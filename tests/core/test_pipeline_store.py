import threading
from dataclasses import FrozenInstanceError, replace

import pytest

from pressure_loop.contributors import EpisodeIgnition, make_contributor
from pressure_loop.episode import EpisodeStore


def test_distinct_semantic_lanes_can_hold_concurrent_episodes():
    store = EpisodeStore()
    first = store.open(opened_by="operator_query", pipeline_id=0, work_kind="operator_turn")
    opened = []
    thread = threading.Thread(target=lambda: opened.append(
        store.open(opened_by="lane_work_dispatch", pipeline_id=1, work_kind="operator_lane_work")
    ))
    thread.start()
    thread.join()
    assert store.current(pipeline_id=0) is first
    assert opened[0].pipeline_id == 1
    assert opened[0].work_kind == "operator_lane_work"


def test_one_physical_pipeline_cannot_hold_two_episodes():
    store = EpisodeStore()
    first = store.open(opened_by="agent_continuation", pipeline_id=2, work_kind="agent_continuation")
    with pytest.raises(RuntimeError, match="pipeline 2"):
        store.open(opened_by="agent_continuation", pipeline_id=2, work_kind="agent_continuation")
    with pytest.raises(RuntimeError, match="pipeline 2"):
        store.restore(replace(first, episode_id="other"))
    assert store.current(pipeline_id=2) is first


def test_unbound_observer_cannot_borrow_or_close_an_episode():
    store = EpisodeStore()
    first = store.open(opened_by="window_reflection_ready", pipeline_id=3, work_kind="reflection")
    observed = []
    def observer():
        observed.append(store.current())
        observed.append(store.current(pipeline_id=3))
        with pytest.raises(RuntimeError, match="owning cascade thread"):
            store.close()
    thread = threading.Thread(target=observer)
    thread.start()
    thread.join()
    assert observed == [None, first]


@pytest.mark.parametrize("pid", [-1, 4, True, "2"])
def test_physical_ids_are_validated(pid):
    store = EpisodeStore()
    with pytest.raises(ValueError):
        store.open(opened_by="operator_query", pipeline_id=pid, work_kind="operator_turn")
    with pytest.raises(ValueError):
        store.current(pipeline_id=pid)


def test_ignition_pipeline_binding_is_a_replacement():
    c = make_contributor("omega", "lane_work_dispatch", 1, {"lane": "agent"})
    ignition = EpisodeIgnition("contest", 1, c.type, c, (c,), None)
    bound = replace(ignition, pipeline_id=2, work_kind="operator_lane_work")
    assert ignition.pipeline_id == -1
    assert bound.pipeline_id == 2
    with pytest.raises(FrozenInstanceError):
        bound.pipeline_id = 0

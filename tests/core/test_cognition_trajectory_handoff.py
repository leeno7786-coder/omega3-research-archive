"""Episode pressure carries trajectory_snapshot from ignition into Omega."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from pressure_loop.contributors import Contributor, EpisodeIgnition
from tests.helpers.pipeline_ownership import bind_ignition


def test_ignite_episode_preserves_trajectory_snapshot():
    from cognition_chain import ignite_episode

    store = MagicMock()
    episode = MagicMock(
        episode_id="ep-traj", pipeline_id=0, work_kind="operator_turn"
    )
    store.open.return_value = episode
    store.current.return_value = None
    # One episode does not fill a 10-window — no windowed Omega reflection here.
    # (Without this, MagicMock returns a truthy window and ignite_episode fires
    # the Omega cascade, which now fails hard with no model.)
    store.consume_window_ready.return_value = None
    store.get_locked_window.return_value = None

    ignition = EpisodeIgnition(
        episode_id="ign-1",
        aggregate_pressure=0.8,
        opened_by="operator_command",
        dominant_contributor=Contributor(
            source="operator",
            type="operator_command",
            weight=0.8,
            metadata={},
        ),
        contributors=(),
        trajectory_snapshot={"target_domain": "python", "confidence": 0.7},
        neuromod_state={
            "dopamine": -0.2,
            "serotonin": 0.15,
            "stress": 0.4,
            "spike": 0.0,
        },
    )

    with patch("cognition_chain._start_global_a") as start_ga:
        ignite_episode(store, bind_ignition(ignition))

    if store.open.call_args.kwargs.get("pressure") is not None:
        pressure = store.open.call_args.kwargs["pressure"]
    else:
        pressure = store.open.call_args.args[0]
    assert pressure["aggregate_pressure"] == 0.8
    assert "aggregate" not in pressure
    assert pressure["trajectory_snapshot"] == {
        "target_domain": "python",
        "confidence": 0.7,
    }
    assert pressure["neuromod_state"] == {
        "dopamine": -0.2,
        "serotonin": 0.15,
        "stress": 0.4,
        "spike": 0.0,
    }


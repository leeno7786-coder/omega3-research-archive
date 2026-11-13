from __future__ import annotations

import pytest

from memory import episode_sram
from memory.build_objective import BuildObjective, current_build_objective, hold_build_objective


def test_practice_objective_is_episode_sram_state_not_a_process_global() -> None:
    episode_sram.open_bucket("ep-practice", pipeline_id=0)
    objective = BuildObjective(
        episode_id="ep-practice",
        objective="Build a focused parser practice project",
        origin_opened_by="omega_practice",
        practice_domain="python.parsers",
    )

    ref = hold_build_objective(objective)

    assert ref == "sram:ep-practice:0"
    assert current_build_objective() == objective
    item = episode_sram.pending()[0]
    assert item.envelope.type == "build_objective"
    assert item.envelope.coord == [11, 2, 0]


def test_build_objective_requires_an_open_episode() -> None:
    with pytest.raises(RuntimeError, match="no episode SRAM bucket is open"):
        hold_build_objective(
            BuildObjective(
                episode_id="ep-practice",
                objective="Build a focused parser practice project",
                origin_opened_by="omega_practice",
                practice_domain="python.parsers",
            )
        )

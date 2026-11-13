"""DeepSleep Phase 10 — reseed staging (episode close owned by EpisodeStore)."""

from __future__ import annotations

from typing import Any, Sequence

from pressure_loop.contributors import Contributor
from pressure_loop.pressure import stage_reseed


def finalize_window(
    store: Any,
    reseed_contributors: Sequence[Contributor],
) -> dict[str, bool]:
    """Stage reseed contributors after window consolidation.

    Episode close + pressure reset already ran at cascade end via
    EpisodeStore.close(); DeepSleep no longer owns close().
    """
    del store
    omega_reseed = tuple(
        contributor for contributor in reseed_contributors if contributor.source == "omega"
    )
    if omega_reseed:
        stage_reseed(omega_reseed)
    result = {
        "episode_closed": False,
        "pressure_reset": False,
    }
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "deepsleep",
            "finalize_window",
            summary=f"reseed={len(omega_reseed)}",
            reseed_count=len(omega_reseed),
            rejected_non_omega=len(reseed_contributors) - len(omega_reseed),
            staged=bool(omega_reseed),
            **result,
        )
    except Exception:
        pass
    return result

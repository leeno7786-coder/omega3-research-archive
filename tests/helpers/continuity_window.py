"""Real ten-member cohorts for the WO-09 window suites.

Every member is opened on a real ``EpisodeStore``, dressed by the real producers, and closed
through the real DeepSleep terminus (``tests.helpers.continuity_terminus``). The terminus is the
only writer: it seals each episode's record, drains it to canonical memory, rebuilds recall, and
then appends the episode to the collecting reflection window inside the serialized commit region
— which is where membership, order and the pinned canonical reference all come from. Nothing
here writes a window or a canonical record itself.

The outcome plan is deliberately mixed. A cohort built only from successes cannot tell a
membership rule from a success filter.
"""

from __future__ import annotations

import importlib
from typing import Any, Sequence

import pytest

from tests.helpers import continuity_terminus as ct

#: Ten members: successes, a failed acceptance check, an operator cancellation, legitimate idle
#: turns, and a cascade that failed after its action succeeded.
MIXED_PLAN: tuple[str, ...] = (
    "success",
    "failure",
    "cancelled",
    "idle",
    "success",
    "cascade_failed",
    "success",
    "failure",
    "idle",
    "success",
)


def membership():
    """The WO-09 module, or a failure that names what is missing."""
    try:
        return importlib.import_module("omega_loop.window.membership")
    except ModuleNotFoundError:
        pytest.fail(
            "omega_loop.window.membership does not exist: nothing seals a cohort against a "
            "snapshot or hydrates the exact canonical revisions a window was sealed with"
        )


def close_member(store, kind: str) -> Any:
    """Open, dress and close ONE real episode whose sealed outcome is *kind*."""
    carrier = ct.open_carrier(store)
    if kind == "failure":
        ct.dress_failed_build(carrier)
        ct.close(store)
        return carrier
    ct.dress_learn(carrier)
    execution = carrier.meta["reflection"]["execution"]
    if kind == "cancelled":
        execution.update(
            executed=False,
            result="failure",
            status="cancelled",
            disposition="cancelled",
            cancellation={"source": "operator", "reason": "the operator stopped the run"},
        )
    elif kind == "idle":
        execution.update(executed=False, result="none", status="idle", disposition="idle")
    elif kind not in ("success", "cascade_failed"):
        raise ValueError(f"unknown member kind {kind!r}")
    ct.close(store, cascade_status="failed" if kind == "cascade_failed" else "completed")
    return carrier


def close_cohort(store, plan: Sequence[str] = MIXED_PLAN) -> list[Any]:
    return [close_member(store, kind) for kind in plan]


def staged_cohorts() -> list[Any]:
    from pressure_loop import window_lane

    return list(window_lane.staged_windows())


def staged_cohort() -> Any:
    rows = staged_cohorts()
    assert len(rows) == 1, f"expected exactly one staged cohort, found {len(rows)}"
    return rows[0]


def open_window_episode(store, *, pipeline_id: int = 1) -> Any:
    """The episode the shared FIFO opens for a staged window: reflection work, not experience."""
    from memory import episode_sram
    from pressure_loop.window_lane import WINDOW_REFLECTION_READY

    episode = store.open(
        opened_by=WINDOW_REFLECTION_READY, pressure={}, pipeline_id=pipeline_id,
        work_kind="reflection",
    )
    episode_sram.open_bucket(episode.episode_id, pipeline_id=pipeline_id)
    return episode


def cutoff_of(carrier: Any) -> dict:
    """The cutoff a window records when *carrier*'s append fills it: read from its sealed record."""
    from memory.episode_commit_view import (
        canonical_episode_body,
        episode_record_ref,
        pending_episode_record_ref,
    )
    from pressure_loop.episode import _member_cutoff

    ref = episode_record_ref(carrier.episode_id)
    if ref is not None:
        body, _why = canonical_episode_body(ref.record_id)
        return _member_cutoff(body)
    # A record the drain never committed is not on disk; its stamp is on the carrier.
    assert pending_episode_record_ref(carrier.episode_id) is not None
    from core.timekeeper import last_coverage_clock

    return {"clock_epoch_id": last_coverage_clock().clock_epoch_id,
            "commit_seq": int(carrier.timekeeper["commit_seq"]),
            "closed_at": str(carrier.closed_at)}


def canonical_document_path(record_id: str):
    """Where the canonical writer put *record_id* (read through the recall index)."""
    from pathlib import Path

    from memory.engine.walker import load_coord_index, path_for_id

    load_coord_index()
    where = path_for_id(record_id)
    assert where, f"no canonical document is indexed under {record_id!r}"
    return Path(where)


def one_shot_crash(real, message: str):
    """A stand-in that raises once, then behaves exactly like *real*."""
    state = {"fired": False}

    def stand_in(*args, **kwargs):
        if not state["fired"]:
            state["fired"] = True
            raise RuntimeError(message)
        return real(*args, **kwargs)

    return stand_in

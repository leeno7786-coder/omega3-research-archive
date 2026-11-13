"""Give a phase-owner unit test the episode SRAM owner that WO-04 capture requires.

Global A and Meta A record their decisions into their episode's SRAM bucket (WO-04), and
``record_event`` refuses a caller that does not hold the matching live owner binding (WO-03A R1).
A test that drives one of those phases against a carrier with no bucket is therefore exercising a
state the cascade cannot produce: the admission seam always opens the bucket before any phase
runs. This helper supplies that owner explicitly, so the phase runs the way it really runs.

It is deliberately NOT a patch of the capture functions. Silencing the capture would make the
test pass while proving the opposite of what WO-04 requires.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator


def _episode_id(episode: Any) -> str:
    return str(getattr(episode, "episode_id", episode) or "")


@contextmanager
def owned(episode: Any, *, pipeline_id: int = 0) -> Iterator[None]:
    """Open ONE episode's SRAM bucket, bound to this thread, and tear it down after.

    One bucket, not several: a physical pipeline holds one episode, and the phase being driven has
    exactly one owner. A test that drives two phases for two carriers wraps each call.
    """
    from memory import episode_sram

    episode_id = _episode_id(episode)
    if not episode_id:
        raise ValueError("owned() needs an episode with an id")
    episode_sram.reset()
    try:
        episode_sram.open_bucket(episode_id, pipeline_id=pipeline_id)
        yield
    finally:
        episode_sram.reset()

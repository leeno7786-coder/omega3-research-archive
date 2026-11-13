"""The ONE serialized commit region — where memory order stops being luck.

Phase 4 cut 13. Phase 3 stamped the finish line at deepsleep GATE ENTRY and
that stamp equalled the landed order for exactly one reason: the same
process-wide lock was held from the gate all the way through the writes, so
nothing could arrive between the stamp and the commit. Cut 13 unlocks the
lane-local half of deepsleep — consolidation, mastery, semantic promotion,
memory writes into the episode's own SRAM bucket — so that a chat episode's
terminus no longer parks an agent episode. The instant that half runs
unserialized, gate-entry stamping becomes a lie: a lane can stamp first, spend
three seconds consolidating, and land AFTER a lane that stamped later.

So the stamp moves. It is the FIRST statement inside the commit lock, and
everything that lands does so while that lock is held. Stamp order is landed
order by construction — not by an argument about what else happens to be
serialized. That is the entire correctness content of this cut, and
``tests/core/test_lane_deepsleep.py`` is where two threads try to break it.

The epoch seal (cut 12) stays welded to the stamp, one statement later, for the
reason cut 12 recorded: the interval ``(start, commit]`` must be sealed before
any other lane can cross the line, or another episode's commit_seq lands inside
this episode's causal parents — the opposite of what a causal parent means.

Ordering law (``core.cognition_lock``): the caller reaches this region holding
at most its own lane lock. Never another lane's; never the commit lock first.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Dict, Iterator

log = logging.getLogger("omega.core.commit_region")


@contextmanager
def serialized_commit(episode: Any) -> Iterator[Dict[str, Any]]:
    """Enter the serialized commit region for one episode's terminus.

    Yields the finish-line stamp. Everything inside the ``with`` body lands
    under the ONE commit lock, in stamp order, for every lane.

    Fail-hard on both statements: an episode that cannot be stamped has no
    place in the memory sequence, and an epoch that cannot be sealed is a
    journal row that lies about what its episode could have known.
    """
    from core.cognition_lock import commit_lock
    from core.timekeeper import finish_line_stamp, mark_burned, mark_landed
    from memory.epoch import seal_episode_epoch

    with commit_lock():
        stamp = finish_line_stamp(episode.pipeline_id, episode.work_kind)
        episode.timekeeper = stamp
        try:
            seal_episode_epoch(episode)
            yield stamp
        except BaseException:
            # memory-organ Cut 13 (D-13-74): a stamped terminus that raises inside the region
            # SPENDS its number — burned, never reused, never claimed as landed coverage.
            mark_burned(stamp)
            raise
        else:
            # the shared-write body exited normally: the sequence has LANDED (in memory only —
            # zero file I/O on the commit path). stamp = reserved place in order; landed = completed.
            mark_landed(stamp)


@contextmanager
def replayed_commit(episode: Any, stamp: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """Re-enter the serialized region for an episode that ALREADY holds its place (WO-07).

    A terminus that raised after its sealed record committed is retried from the top, and
    minting a second stamp would give one episode two places in the memory sequence — the
    duplicate lobe-9 record WO-07 removed. This entrance takes the same ONE commit lock, puts
    the episode's recorded stamp back on the carrier, and mints nothing. Its number was spent
    by the attempt that committed it (landed or burned), so there is nothing to land or burn
    here. The epoch seal is not repeated: it belongs to that stamp and is already sealed.
    """
    from core.cognition_lock import commit_lock

    recorded = dict(stamp or {})
    seq = recorded.get("commit_seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise ValueError("replayed_commit requires the episode's recorded finish-line stamp")
    with commit_lock():
        episode.timekeeper = recorded
        yield recorded

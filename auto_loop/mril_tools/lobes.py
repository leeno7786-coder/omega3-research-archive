"""Lane/lobe scope helpers shared with memory.recall_lanes."""

from __future__ import annotations

from typing import List, Tuple

from auto_loop._auto_state import _LANE_LOBE, _SCOPE_ALIASES
from memory.recall_lanes import LANE_DEV, LANE_JOURNAL, LANE_SITUATIONAL


def scope_to_lobes(memory_scope: tuple) -> List[int]:
    """Map semantic memory_scope lane names to allowed lobe ints."""
    lobes: List[int] = []
    for raw in memory_scope or ():
        key = str(raw).strip().lower()
        lanes: Tuple[str, ...]
        if key in _SCOPE_ALIASES:
            lanes = _SCOPE_ALIASES[key]
        else:
            lanes = (key,)
        for lane in lanes:
            lobe = _LANE_LOBE.get(lane)
            if lobe is not None and lobe not in lobes:
                lobes.append(lobe)
    return lobes


def denied_lobes_for_scope(memory_scope: tuple) -> List[int]:
    """Situational (4), journal (6), and dev (11) are mind-process lobes — denied for
    user-facing recall unless the scope explicitly names that lane.

    Situational was leaking: it is entity-bearing (ENTITY_BEARING_LOBES = {1, 4}) and
    was NOT denied, so graph fan-out hopped into lobe-4 fold/observation records (e.g. a
    'fold_ingest' Big O note) and surfaced them on unrelated user queries. run.py's
    doctrine — and test_meta_freeform_recall — say situational stays out of user-facing
    recall; only the situational lane itself (remember_situation / operator_history /
    conversation scopes) opts it back in."""
    scope_lower = {str(s).strip().lower() for s in (memory_scope or ())}
    denied = [4, 6, 11]
    if LANE_SITUATIONAL in scope_lower:
        denied = [x for x in denied if x != 4]
    if LANE_JOURNAL in scope_lower:
        denied = [x for x in denied if x != 6]
    if LANE_DEV in scope_lower:
        denied = [x for x in denied if x != 11]
    return denied

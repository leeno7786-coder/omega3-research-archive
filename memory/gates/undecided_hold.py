"""Phase 5 — undecided / _unsorted holding subdomain routing."""

from __future__ import annotations

from typing import FrozenSet, List, Tuple

from memory.dewey_map import SUBDOMAIN_MAP, UNSORTED_SUBDOMAIN_Y


def route_undecided_subdomain(
    text: str,
    lobe: int,
    coord: List[int],
    label_id: str,
    *,
    dynamic_y_lobes: FrozenSet[int],
) -> Tuple[List[int], str, bool]:
    """When grammar cannot decide Y on a static lobe, route to _unsorted.

    Dynamic-Y lobes (2/8 when flagged) defer to the allocator instead.
    Returns ``(coord, label_id, undecided)``.
    """
    if lobe in dynamic_y_lobes:
        return coord, label_id, False
    try:
        from auto_loop.mril_tools.subdomain_helpers import subdomain_for
    except Exception:
        return coord, label_id, False
    if subdomain_for(text, lobe) is not None:
        return coord, label_id, False
    if (lobe, UNSORTED_SUBDOMAIN_Y) not in SUBDOMAIN_MAP:
        return coord, label_id, False
    y = UNSORTED_SUBDOMAIN_Y
    new_coord = [lobe, y] + list((coord or [0, 0, 0])[2:] or [0])
    return new_coord, f"{lobe}.{y:02d}", True

"""Slice 2 — deterministic ingest subdomain override (write-Y == recall-Y)."""

from __future__ import annotations

from typing import Tuple


def apply_subdomain_override(
    text: str,
    coord,
    label_id: str,
) -> Tuple[list, str]:
    """Resolve the write-Y from the recall grammar so ingest places what recall
    seeks (write-Y == recall-Y by construction).

    Authority contract: this only runs on the single-lobe ``gate.ingest``
    free-text path, where ``base._ingest_impl`` always seeds ``coord=[lobe,0,0]``
    (no producer Y to clobber) — so ``subdomain_for`` is the legitimate Y
    authority for free-text. Producer-declared coords (facet/modality/source-tag)
    are authoritative and reach storage via ``memory.api.write_memory``, which
    preserves the explicit coord and never calls this override; lobe-5 tool
    writes are pinned by ``registry_scope`` before this runs. If a future caller
    ever seeds a non-zero authoritative Y through a gate, guard this on
    ``coord[1] in (0, UNSORTED_SUBDOMAIN_Y)`` first.

    Returns the (possibly overridden) ``(coord, label_id)``. Pure; lazy-imports
    subdomain_for (cycle-proof). Helper None / non-helper lobe / Y already equal
    -> returned unchanged."""
    if not coord or len(coord) < 2:
        return coord, label_id
    try:
        from auto_loop.mril_tools.subdomain_helpers import subdomain_for
    except Exception:
        return coord, label_id  # import boundary only — NOT swallowing a routing error
    lobe = int(coord[0])
    helper_y = subdomain_for(text, lobe)
    if helper_y is None or int(coord[1]) == int(helper_y):
        return coord, label_id
    new_coord = [lobe, int(helper_y)] + list(coord[2:] or [0])
    return new_coord, f"{lobe}.{int(helper_y):02d}"

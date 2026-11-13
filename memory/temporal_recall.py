"""Date-range recall — the records created within a temporal window, most-recent-first.

RESPONSE_TEMPORAL answers "what did we build yesterday" by pulling every record whose ``observed_at``
(memory.observed_at — the real wall-clock creation date) falls in a parsed ``[start, end)`` window
(memory.temporal_range), across lobes, ranked most-recent-first (observed_at desc, write_ordinal as
a stable tie-break). The DATE is the filter; the content is "everything from that day".

This is a DEDICATED temporal query, deliberately separate from the lane/arbitrator/verifier recall
pipeline (which the operator's 2k-world calibration proved) — temporal is its own first-class
response, weighed in time and date, not a modifier bolted onto general recall. Pure given injected
``observed_at_map`` + ``load_meta``; the caller supplies the real sidecar + loader in production.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

# Excluded from "what did we build/do" recall: COGNITION-CYCLE TELEMETRY, not operator-facing work —
#   4 = situational (auto-observed 5W1H event stream: pressure_ignition / global_a_intent / auto_outcome)
#   6 = journal      (DeepSleep consolidation journal, sole-writer telemetry)
#   9 = temporal     (episode + trajectory cycle markers — internal state, not "what we built")
# KEPT (the actual work + learning): identity(0), personas(1), knowledge(2), reasoning(3), tools(5),
# vision(7), coding(8), workflows(10), dev(11). Live monitoring 2026-07-16 showed 4/6/9 flooding a
# "what did we build today" recall with cognition telemetry; ordinal recency ("what did I learn last")
# is served separately by the write-ordinal posture, so excluding lobe 9 here does not lose it.
# Lobes that are not EVENTS in the operator's day: situational (4) and journal
# (6) are mind-process, episodes (9) ride the remember_time lane, and identity
# (0) / tools (5) are catalogs whose observed_at is merely their (re)write time
# — live 2026-09-06: "what did I build yesterday?" listed the skill catalog
# re-registered at boot as things the operator had built.
_DEFAULT_EXCLUDE_LOBES = (0, 4, 5, 6, 9)


def _dt(iso: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None


def _trait_family_types() -> frozenset[str]:
    """The non-semantic trait family (memory.encoding.NON_SEMANTIC_TYPES)."""
    try:
        from memory.encoding import NON_SEMANTIC_TYPES

        return frozenset(str(t) for t in NON_SEMANTIC_TYPES)
    except Exception:  # noqa: BLE001 — a missing table excludes nothing
        return frozenset()


def recall_by_date_range(
    date_range: Optional[Mapping[str, Any]],
    *,
    observed_at_map: Mapping[str, str],
    load_meta: Callable[[str], Optional[Mapping[str, Any]]],
    limit: int = 12,
    exclude_lobes: Iterable[int] = (),
) -> List[Dict[str, Any]]:
    """Return records whose ``observed_at`` falls in ``date_range`` = ``{start, end, label}`` (from
    memory.temporal_range.parse_temporal_range), most-recent-first, capped at ``limit``. Each result
    carries the fields a dated render needs: memory_id, observed_at, coord, title, summary,
    write_ordinal. ``exclude_lobes`` drops whole lobes (e.g. situational/journal). Empty when
    ``date_range`` is None (no temporal phrase) or nothing falls in the window."""
    if not date_range:
        return []
    start_dt = _dt(date_range.get("start"))
    end_dt = _dt(date_range.get("end"))
    if start_dt is None or end_dt is None:
        return []
    exclude = {int(x) for x in (exclude_lobes or ())}

    rows: List[Dict[str, Any]] = []
    for mid, iso in (observed_at_map or {}).items():
        when = _dt(iso)
        if when is None or not (start_dt <= when < end_dt):
            continue
        meta = load_meta(mid)
        if not isinstance(meta, Mapping):
            continue
        coord = list(meta.get("coord") or [])
        if exclude and coord and int(coord[0]) in exclude:
            continue
        if str(meta.get("type") or "") in _trait_family_types():
            # Omega's own evolution ledger (emergent traits, trait observations)
            # is minted by DeepSleep in bulk and is not an event in the
            # operator's day. Live 2026-09-06: "what did I build yesterday?"
            # rendered eleven trait rows as things the operator had built.
            continue
        prov = meta.get("provenance") if isinstance(meta.get("provenance"), Mapping) else {}
        raw_ord = prov.get("write_ordinal")
        rows.append(
            {
                "memory_id": str(mid),
                "observed_at": str(iso),
                "coord": coord,
                "title": str(meta.get("title") or ""),
                "summary": str(meta.get("summary") or meta.get("text") or ""),
                "write_ordinal": int(raw_ord) if isinstance(raw_ord, (int, float)) else 0,
            }
        )

    # Most recent in the window first; write_ordinal breaks same-timestamp ties; id for determinism.
    rows.sort(key=lambda r: (r["observed_at"], r["write_ordinal"], r["memory_id"]), reverse=True)
    return rows[: max(1, int(limit))]


def working_now() -> datetime:
    """The operator's clock: LOCAL wall time, tz-aware. A day phrase ("yesterday")
    is the operator's day, not UTC's — live 2026-09-06 00:48 CDT, an artifact
    written 20:40 the previous evening (01:40Z) fell on "today" in UTC and
    "yesterday" recalled nothing. Comparisons stay correct because observed_at
    values are tz-aware too."""
    return datetime.now().astimezone()


def recall_temporal(
    query: str,
    now: Optional[datetime] = None,
    *,
    limit: int = 12,
    exclude_lobes: Iterable[int] = _DEFAULT_EXCLUDE_LOBES,
) -> Optional[Dict[str, Any]]:
    """LIVE date-range temporal recall: parse the window from ``query`` + the working clock, then pull
    the records created in it from the live observed_at sidecar + coord index. Returns
    ``{"records": [...], "label": str, "range": {...}}``, or None when the query carries no date phrase
    (caller falls back to ordinary recall). ``now`` defaults to the real working clock — wall-clock is
    correct here (live request, not the replayed substrate). Fail-soft: any fault -> None."""
    from memory.temporal_range import parse_temporal_range

    if now is None:
        now = working_now()
    date_range = parse_temporal_range(query, now)
    if not date_range:
        return None
    try:
        from memory.engine.validate_write import MEMORY_ROOT
        from memory.engine.walker import load_meta_by_path, path_for_id
        from memory.observed_at import all_observed_at

        observed = all_observed_at(MEMORY_ROOT / "indexes" / "observed_at.json")

        def _load(mid: str) -> Optional[Mapping[str, Any]]:
            p = path_for_id(mid)
            return load_meta_by_path(p) if p else None

        records = recall_by_date_range(
            date_range,
            observed_at_map=observed,
            load_meta=_load,
            limit=limit,
            exclude_lobes=exclude_lobes,
        )
    except Exception:  # noqa: BLE001 — temporal grounding is best-effort; never break the reply
        records = []
    return {
        "records": records,
        "label": str(date_range.get("label") or ""),
        "range": dict(date_range),
    }

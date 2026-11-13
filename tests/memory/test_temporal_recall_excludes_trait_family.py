"""The dated-records block is the operator's day, not Omega's evolution ledger.

Live 2026-09-06: "Do you remember what I built yesterday?" rendered the trait
records DeepSleep had promoted that day as a numbered list of things the operator
had built ("1. Identity Trait – collaborative_peer_elaboration ...").
"""

from __future__ import annotations

from memory.temporal_recall import recall_by_date_range

WINDOW = {"start": "2026-09-05T00:00:00+00:00", "end": "2026-09-06T00:00:00+00:00", "label": "yesterday"}

RECORDS = {
    "11.00.e718aec5": {"coord": [11, 0, 0], "type": "dev_artifact_snapshot", "title": "Builder artifact: countdown.py", "summary": "Builder captured countdown.py"},
    "trait.collab.abc": {"coord": [0, 1, 0], "type": "emergent_trait", "title": "Identity Trait - collaborative_peer_elaboration", "summary": "promoted"},
    "trait-observation.collab.def": {"coord": [0, 3, 0], "type": "trait_observation", "title": "Trait Observation - collaborative_peer_elaboration", "summary": "observed"},
    "9.01.abcdef01": {"coord": [9, 1, 0], "type": "episode", "title": "RESPOND: write a countdown script", "summary": "operator: write a countdown script → omega: done"},
}
OBSERVED = {mid: "2026-09-05T20:40:00+00:00" for mid in RECORDS}


def test_trait_family_records_never_enter_the_dated_block():
    rows = recall_by_date_range(WINDOW, observed_at_map=OBSERVED, load_meta=lambda mid: RECORDS.get(mid), limit=12)
    ids = {row["memory_id"] for row in rows}
    assert ids == {"11.00.e718aec5", "9.01.abcdef01"}


def test_lobe_exclusions_still_apply():
    rows = recall_by_date_range(WINDOW, observed_at_map=OBSERVED, load_meta=lambda mid: RECORDS.get(mid), limit=12, exclude_lobes=(11,))
    assert [row["memory_id"] for row in rows] == ["9.01.abcdef01"]


def test_catalog_lobes_are_not_events_by_default():
    """Live 2026-09-06: the skill catalog (lobe 5) re-registered at boot was
    rendered as things the operator had built yesterday."""
    from memory.temporal_recall import _DEFAULT_EXCLUDE_LOBES

    assert {0, 4, 5, 6, 9}.issubset(set(_DEFAULT_EXCLUDE_LOBES))
    records = dict(RECORDS)
    records["5.01.skill"] = {"coord": [5, 1, 0], "type": "skill", "title": "skill-creator", "summary": "Interactive skill authoring assistant"}
    records["0.00.seed"] = {"coord": [0, 0, 0], "type": "constitution", "title": "Omega — autonomous cognitive partner", "summary": "seed"}
    observed = {mid: "2026-09-05T20:40:00+00:00" for mid in records}
    rows = recall_by_date_range(WINDOW, observed_at_map=observed, load_meta=lambda mid: records.get(mid), limit=12, exclude_lobes=_DEFAULT_EXCLUDE_LOBES)
    assert {row["memory_id"] for row in rows} == {"11.00.e718aec5"}


def test_a_day_phrase_is_the_operators_local_day():
    """Live 2026-09-06 00:48 CDT: the countdown artifact written at 20:40 the
    previous evening (01:40Z) fell on "today" in UTC, so "yesterday" recalled
    nothing. The day window is cut in the operator's local time."""
    from datetime import datetime, timedelta, timezone

    from memory.temporal_range import parse_temporal_range
    from memory.temporal_recall import working_now

    now_local = datetime(2026, 9, 6, 0, 48, tzinfo=timezone(timedelta(hours=-5)))
    window = parse_temporal_range("what did I build yesterday", now_local)
    assert window["start"].endswith("-05:00") and window["end"].endswith("-05:00")
    rows = recall_by_date_range(
        window,
        observed_at_map={"11.00.e718aec5": "2026-09-06T01:40:13.212802+00:00"},
        load_meta=lambda mid: RECORDS[mid],
    )
    assert [row["memory_id"] for row in rows] == ["11.00.e718aec5"]
    assert working_now().tzinfo is not None and working_now().utcoffset() is not None

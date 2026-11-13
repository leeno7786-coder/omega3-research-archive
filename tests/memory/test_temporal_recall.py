"""Date-range recall — the records created within a temporal window, most-recent-first.

RESPONSE_TEMPORAL answers "what did we build yesterday" by pulling every record whose observed_at
(memory.observed_at, the real wall-clock creation date) falls in the parsed [start, end) window,
across lobes, ranked most-recent-first (observed_at desc, write_ordinal tie-break). The DATE is the
filter; content is "everything from that day". A dedicated query — it does not touch the proven
lane/arbitrator/verifier pipeline.
"""

from __future__ import annotations

from datetime import datetime, timezone

from memory.temporal_range import parse_temporal_range
from memory.temporal_recall import recall_by_date_range

NOW = datetime(2026, 7, 16, 14, 30, tzinfo=timezone.utc)  # Thursday


def _meta(mid, coord, title, summary, ordinal=0):
    return {
        "id": mid,
        "coord": list(coord),
        "title": title,
        "summary": summary,
        "provenance": {"write_ordinal": ordinal},
    }


def _corpus():
    metas = {
        "3.00.a": _meta("3.00.a", [3, 0, 0], "TODO app", "scaffolded the todo app", 10),
        "3.00.b": _meta("3.00.b", [3, 0, 0], "TODO tests", "added pytest coverage", 11),
        "2.00.c": _meta("2.00.c", [2, 0, 0], "Mars", "learned mars has two moons", 12),
        "3.00.old": _meta("3.00.old", [3, 0, 0], "Old", "last week's work", 5),
    }
    observed = {
        "3.00.a": "2026-07-15T09:00:00+00:00",  # yesterday
        "3.00.b": "2026-07-15T16:00:00+00:00",  # yesterday (later)
        "2.00.c": "2026-07-16T11:00:00+00:00",  # today
        "3.00.old": "2026-07-08T10:00:00+00:00",  # last week
    }
    return metas, observed


def _call(query, *, limit=12, exclude_lobes=()):
    metas, observed = _corpus()
    return recall_by_date_range(
        parse_temporal_range(query, NOW),
        observed_at_map=observed,
        load_meta=metas.get,
        limit=limit,
        exclude_lobes=exclude_lobes,
    )


def test_yesterday_returns_only_yesterdays_records_recent_first():
    out = _call("what did we build yesterday")
    ids = [r["memory_id"] for r in out]
    assert ids == ["3.00.b", "3.00.a"]  # both from the 15th, later one first
    assert all(r["observed_at"].startswith("2026-07-15") for r in out)


def test_today_excludes_yesterday_and_last_week():
    out = _call("what did we do today")
    assert [r["memory_id"] for r in out] == ["2.00.c"]


def test_this_week_spans_multiple_days():
    out = _call("what have we shipped this week")
    # this week = Mon 07-13 .. next Mon; includes the 15th (a,b) + 16th (c), NOT last week's old.
    assert set(r["memory_id"] for r in out) == {"3.00.a", "3.00.b", "2.00.c"}
    assert "3.00.old" not in [r["memory_id"] for r in out]


def test_records_carry_dated_fields_for_render():
    out = _call("what did we build yesterday")
    r = out[0]
    assert r["title"] and r["summary"] and r["observed_at"]
    assert r["coord"] == [3, 0, 0]


def test_limit_caps_results_keeping_most_recent():
    out = _call("what have we shipped this week", limit=1)
    assert len(out) == 1
    assert out[0]["memory_id"] == "2.00.c"  # most recent in the window


def test_exclude_lobes_filters():
    out = _call("what have we shipped this week", exclude_lobes=(2,))
    assert all(r["coord"][0] != 2 for r in out)  # knowledge lobe excluded


def test_none_range_returns_empty():
    metas, observed = _corpus()
    out = recall_by_date_range(None, observed_at_map=observed, load_meta=metas.get)
    assert out == []


def test_deterministic():
    outs = [_call("what did we build yesterday") for _ in range(3)]
    assert outs[0] == outs[1] == outs[2]


def test_recall_temporal_none_when_no_date_phrase():
    from memory.temporal_recall import recall_temporal

    assert recall_temporal("what is the capital of france", NOW) is None


def test_recall_temporal_wires_live_sidecar_and_index(monkeypatch):
    from memory.temporal_recall import recall_temporal

    metas, observed = _corpus()
    monkeypatch.setattr("memory.observed_at.all_observed_at", lambda p: observed)
    monkeypatch.setattr("memory.engine.walker.path_for_id", lambda mid: mid)  # id IS the path here
    monkeypatch.setattr("memory.engine.walker.load_meta_by_path", lambda p: metas.get(p))

    out = recall_temporal("what did we build yesterday", NOW, exclude_lobes=())
    assert out["label"] == "yesterday"
    assert [r["memory_id"] for r in out["records"]] == ["3.00.b", "3.00.a"]


def test_recall_temporal_excludes_cognition_telemetry_lobes_by_default(monkeypatch):
    from memory.temporal_recall import recall_temporal

    # Cognition-cycle telemetry (situational events lobe 4, journal lobe 6, episode/trajectory
    # markers lobe 9) must be dropped; real work (coding lobe 8) is kept.
    metas = {
        "8.00.work": _meta("8.00.work", [8, 0, 0], "code", "built the parser", 1),
        "4.00.evt": _meta("4.00.evt", [4, 0, 0], "situational", "Event auto_outcome", 2),
        "6.00.j": _meta("6.00.j", [6, 0, 0], "journal", "deepsleep journal", 3),
        "9.00.ep": _meta("9.00.ep", [9, 0, 0], "episode", "episode ee0c...", 4),
    }
    observed = {k: "2026-07-15T10:00:00+00:00" for k in metas}
    monkeypatch.setattr("memory.observed_at.all_observed_at", lambda p: observed)
    monkeypatch.setattr("memory.engine.walker.path_for_id", lambda mid: mid)
    monkeypatch.setattr("memory.engine.walker.load_meta_by_path", lambda p: metas.get(p))

    out = recall_temporal("what did we build yesterday", NOW)  # default exclude = (4, 6, 9)
    assert [r["memory_id"] for r in out["records"]] == ["8.00.work"]  # only real work survives

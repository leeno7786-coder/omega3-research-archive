"""Parse a temporal phrase into a wall-clock date RANGE relative to an injected working clock.

RESPONSE_TEMPORAL maps "what did we build yesterday / last week" to a half-open [start, end) UTC
range that date-range recall matches against records' observed_at. Pure + deterministic: `now` is
injected, so (query, now) -> the same range every time. `now` here is Thursday 2026-07-16 14:30 UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone

from memory.temporal_range import parse_temporal_range

NOW = datetime(2026, 7, 16, 14, 30, tzinfo=timezone.utc)  # a Thursday


def _r(query):
    return parse_temporal_range(query, NOW)


def test_no_temporal_phrase_returns_none():
    assert _r("tell me about the moons of mars") is None
    assert _r("") is None


def test_today():
    r = _r("what did we do today")
    assert r["start"] == "2026-07-16T00:00:00+00:00"
    assert r["end"] == "2026-07-17T00:00:00+00:00"
    assert r["label"] == "today"


def test_yesterday():
    r = _r("what did we build yesterday")
    assert r["start"] == "2026-07-15T00:00:00+00:00"
    assert r["end"] == "2026-07-16T00:00:00+00:00"
    assert r["label"] == "yesterday"


def test_this_week_starts_monday():
    r = _r("what have we shipped this week")
    assert r["start"] == "2026-07-13T00:00:00+00:00"  # Monday
    assert r["end"] == "2026-07-20T00:00:00+00:00"  # next Monday
    assert r["label"] == "this week"


def test_last_week_is_the_prior_calendar_week():
    r = _r("what did we work on last week")
    assert r["start"] == "2026-07-06T00:00:00+00:00"
    assert r["end"] == "2026-07-13T00:00:00+00:00"
    assert r["label"] == "last week"


def test_this_month():
    r = _r("everything from this month")
    assert r["start"] == "2026-07-01T00:00:00+00:00"
    assert r["end"] == "2026-08-01T00:00:00+00:00"
    assert r["label"] == "this month"


def test_last_month_crosses_boundary():
    r = _r("what did i learn last month")
    assert r["start"] == "2026-06-01T00:00:00+00:00"
    assert r["end"] == "2026-07-01T00:00:00+00:00"
    assert r["label"] == "last month"


def test_last_n_days_is_a_trailing_calendar_window():
    r = _r("what did we do in the last 3 days")
    assert r["start"] == "2026-07-14T00:00:00+00:00"  # today + 2 prior calendar days
    assert r["end"] == "2026-07-17T00:00:00+00:00"
    assert r["label"] == "the last 3 days"


def test_past_week_is_trailing_seven_days():
    r = _r("what did we do in the past week")
    assert r["start"] == "2026-07-10T00:00:00+00:00"  # 7 calendar days ending today
    assert r["end"] == "2026-07-17T00:00:00+00:00"


def test_weekday_resolves_to_most_recent_past_occurrence():
    # NOW is Thursday; "on monday" -> this week's Monday (2026-07-13), a single day.
    r = _r("what did we build on monday")
    assert r["start"] == "2026-07-13T00:00:00+00:00"
    assert r["end"] == "2026-07-14T00:00:00+00:00"
    assert r["label"] == "monday"


def test_weekday_today_resolves_to_today():
    # "on thursday" when NOW is Thursday -> today, not a week ago.
    r = _r("what happened on thursday")
    assert r["start"] == "2026-07-16T00:00:00+00:00"
    assert r["end"] == "2026-07-17T00:00:00+00:00"


def test_deterministic():
    outs = [_r("what did we build yesterday") for _ in range(3)]
    assert outs[0] == outs[1] == outs[2]


def test_is_temporal_query_detects_date_phrase():
    from memory.temporal_range import is_temporal_query

    assert is_temporal_query("what did we build yesterday", NOW) is True
    assert is_temporal_query("what is the capital of france", NOW) is False


def test_looks_like_temporal_recall_requires_date_and_recall_shape():
    from memory.temporal_range import looks_like_temporal_recall

    assert looks_like_temporal_recall("what did we build yesterday", NOW) is True
    assert looks_like_temporal_recall("recap what we shipped last week", NOW) is True
    assert looks_like_temporal_recall("summarize what we did today", NOW) is True
    # a bare date phrase with NO recall intent must NOT misroute to temporal
    assert looks_like_temporal_recall("schedule a meeting monday", NOW) is False
    # recall shape but NO date phrase -> not temporal (that's an ordinary question)
    assert looks_like_temporal_recall("what did we build", NOW) is False
    # neither
    assert looks_like_temporal_recall("what is the capital of france", NOW) is False

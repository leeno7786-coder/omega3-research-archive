"""Parse a temporal phrase in a query into a wall-clock date RANGE, relative to an injected clock.

RESPONSE_TEMPORAL answers "what did we build yesterday" / "what did we do last week" by mapping the
phrase to a half-open ``[start, end)`` UTC range, which date-range recall then matches against each
record's ``observed_at`` (memory.observed_at). Pure + deterministic: ``now`` is the injected working
clock, so the same ``(query, now)`` always yields the same range — no hidden wall-clock reads here.
Returns None when the query carries no date phrase (the caller falls back to ordinal recency or plain
recall). Day granularity, week starts Monday (ISO). This is the DETERMINISTIC detector; a learned
SetFit ``temporal`` label can route on top of it later, but the RANGE math stays here and pure.
"""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

_RELATIVE_NUMBERS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _day_start(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _month_start(dt: datetime, *, offset: int = 0) -> datetime:
    """First instant of the month `offset` months from `dt`'s month (offset may be negative)."""
    month_index = (dt.year * 12 + (dt.month - 1)) + offset
    year, month0 = divmod(month_index, 12)
    return dt.replace(year=year, month=month0 + 1, day=1, hour=0, minute=0, second=0, microsecond=0)


def _shift_months(dt: datetime, months: int) -> datetime:
    month_index = dt.year * 12 + (dt.month - 1) - months
    year, month0 = divmod(month_index, 12)
    day = min(dt.day, monthrange(year, month0 + 1)[1])
    return dt.replace(year=year, month=month0 + 1, day=day)


def _range(start: datetime, end: datetime, label: str) -> Dict[str, Any]:
    return {"start": start.isoformat(), "end": end.isoformat(), "label": label}


def parse_temporal_range(query: str, now: datetime) -> Optional[Dict[str, Any]]:
    """Map a temporal phrase to a half-open ``[start, end)`` UTC range + a human label, relative to
    ``now`` (the injected working clock). None if no date phrase is present. Ordinal recency ("last",
    "most recent") is intentionally NOT handled here — that is the write-ordinal lane's job."""
    text = " ".join(str(query or "").strip().lower().split())
    if not text:
        return None
    today0 = _day_start(now)

    if re.search(r"\btoday\b", text):
        return _range(today0, today0 + timedelta(days=1), "today")
    if re.search(r"\byesterday\b", text):
        return _range(today0 - timedelta(days=1), today0, "yesterday")

    exact = re.search(
        r"\b(a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d+)"
        r"\s+(day|week|month)s?\s+ago\b",
        text,
    )
    if exact:
        raw_count, unit = exact.groups()
        count = _RELATIVE_NUMBERS.get(raw_count, int(raw_count) if raw_count.isdigit() else 1)
        if unit == "day":
            target = today0 - timedelta(days=count)
        elif unit == "week":
            target = today0 - timedelta(weeks=count)
        else:
            target = _shift_months(today0, count)
        return _range(target, target + timedelta(days=1), exact.group(0))

    # Trailing windows: "past week", "last N days", "past N days" -> N calendar days ending today.
    m = re.search(r"\b(?:last|past)\s+(\d+)\s+days?\b", text)
    if m:
        n = max(1, int(m.group(1)))
        return _range(
            today0 - timedelta(days=n - 1), today0 + timedelta(days=1), f"the last {n} days"
        )
    if re.search(r"\bpast\s+week\b", text):
        return _range(today0 - timedelta(days=6), today0 + timedelta(days=1), "the past week")

    # Calendar weeks (Monday start).
    week_start = today0 - timedelta(days=today0.weekday())
    if re.search(r"\bthis\s+week\b", text):
        return _range(week_start, week_start + timedelta(days=7), "this week")
    if re.search(r"\blast\s+week\b", text):
        return _range(week_start - timedelta(days=7), week_start, "last week")

    # Calendar months.
    if re.search(r"\bthis\s+month\b", text):
        return _range(_month_start(now), _month_start(now, offset=1), "this month")
    if re.search(r"\blast\s+month\b", text):
        return _range(_month_start(now, offset=-1), _month_start(now), "last month")

    # Weekday names -> the most recent occurrence at or before today (today if it IS that weekday).
    for name, target in _WEEKDAYS.items():
        if re.search(rf"\b{name}\b", text):
            back = (today0.weekday() - target) % 7
            day = today0 - timedelta(days=back)
            return _range(day, day + timedelta(days=1), name)

    return None


def is_temporal_query(query: str, now: Optional[datetime] = None) -> bool:
    """True when the query carries a date-range temporal phrase (used by date-range RECALL). ``now``
    defaults to the real working clock — wall-clock is correct HERE because this is a live request
    decision, not part of the replayed/hashed substrate (unlike record bytes)."""
    if now is None:
        now = datetime.now(timezone.utc)
    return parse_temporal_range(query, now) is not None


# A date phrase ALONE ("schedule a meeting monday") is not a temporal-RECALL intent. The deterministic
# routing floor requires a date phrase AND a past-work-recall shape; the learned classifier is the
# primary router once the `temporal` label is trained, this only covers classifier-off / degraded.
_RECALL_SHAPE = re.compile(
    r"\b(what|which|recap|summar(?:y|ise|ize)|remind|catch me up|go over|run me through|"
    r"walk me through|show me|list|tell me what|did we|did i|have we|were we|what'?ve|what'?d)\b"
)


def looks_like_temporal_recall(query: str, now: Optional[datetime] = None) -> bool:
    """Conservative deterministic router-floor for RESPONSE_TEMPORAL: a date-range phrase PLUS a
    past-work-recall shape (so "schedule a meeting monday" does not misroute). The trained classifier
    `temporal` label is the primary signal; this holds the floor when the classifier is off."""
    return is_temporal_query(query, now) and bool(_RECALL_SHAPE.search(str(query or "").lower()))

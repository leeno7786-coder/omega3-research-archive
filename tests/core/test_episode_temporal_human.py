"""Human/local rendering of the episode temporal envelope for the mind's prompt.

Assertions are timezone-ROBUST (they don't hardcode a weekday/date that a CI machine's tz would
shift): shape + that the epoch is actually read (1-year-apart epochs render different years).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from core.episode_temporal import human_temporal, temporal_prompt_line


def _epoch(year: int) -> float:
    # Mid-year, midday UTC — no timezone offset on earth crosses into another year here.
    return datetime(year, 7, 1, 12, 0, tzinfo=timezone.utc).timestamp()


def test_prompt_line_shape():
    line = temporal_prompt_line({"wall_clock_seconds": _epoch(2026)})
    assert line.startswith("Current date & time: ")
    assert " at " in line
    assert ("AM" in line) or ("PM" in line)
    assert re.search(r"\b20\d\d\b", line)  # a 4-digit year is present


def test_reads_the_envelope_epoch_not_wall_clock_now():
    # Two envelopes a year apart must render different years — proves it reads the stamped epoch.
    y2026 = temporal_prompt_line({"wall_clock_seconds": _epoch(2026)})
    y2027 = temporal_prompt_line({"wall_clock_seconds": _epoch(2027)})
    assert "2026" in y2026 and "2027" in y2027
    assert y2026 != y2027


def test_human_temporal_structure():
    view = human_temporal({"wall_clock_seconds": _epoch(2026)})
    assert set(view) >= {"weekday", "human_date", "human_time", "tz", "local_iso", "human"}
    assert view["human"].startswith(view["weekday"])
    assert "2026" in view["human_date"]


def test_fallback_to_now_when_envelope_empty():
    # No usable epoch -> falls back to a fresh symphony_time read, so a line is ALWAYS present.
    assert temporal_prompt_line({}).startswith("Current date & time: ")
    assert temporal_prompt_line(None).startswith("Current date & time: ")


def test_fallback_override_is_deterministic():
    # human_temporal's now_wall_override drives the fallback rendering deterministically.
    view = human_temporal({}, now_wall_override=_epoch(2030))
    assert "2030" in view["human"]


def test_failure_soft_on_garbage():
    # Non-dict / bad epoch types must never raise — fall back to now, still return a line.
    assert temporal_prompt_line("not a dict").startswith("Current date & time: ")
    assert temporal_prompt_line({"wall_clock_seconds": "bad"}).startswith("Current date & time: ")
    assert temporal_prompt_line({"wall_clock_seconds": None}).startswith("Current date & time: ")
